/*
 * TactiDoseCore.cpp -- TactiDose firmware state machine (portable C++, no Arduino includes).
 *
 * REFERENCE FIRMWARE for a hackathon prototype. NOT a medical device: candy/tokens only.
 * Normative behaviour: docs/SERIAL_PROTOCOL.md. Section references (§, rule 8.x) point there.
 */
#include "TactiDoseCore.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>

namespace tactidose {

namespace {

const char* const kStateNames[] = {"BOOT", "HOMING", "READY", "MOVING", "AT_TARGET", "GATE_OPEN", "SAFE_STOP", "FAULT"};

struct CommandEntry {
  const char* name;
  Command command;
};

const CommandEntry kCommands[] = {
    {"PING", Command::kPing},
    {"STATUS", Command::kStatus},
    {"HOME", Command::kHome},
    {"MOVE_SLOT", Command::kMoveSlot},
    {"DISPENSE_SLOT", Command::kDispenseSlot},
    {"OPEN_GATE", Command::kOpenGate},
    {"CLOSE_GATE", Command::kCloseGate},
    {"STOP", Command::kStop},
};

/* ASCII characters for which Python's str.isspace() is true (protocol.py tokenises with str.split()). */
inline bool isSpace(unsigned char c) {
  return c == ' ' || (c >= 0x09 && c <= 0x0D) || (c >= 0x1C && c <= 0x1F);
}

inline unsigned char toUpper(unsigned char c) {
  return (c >= 'a' && c <= 'z') ? static_cast<unsigned char>(c - ('a' - 'A')) : c;
}

/* Case-insensitive comparison of a (not NUL-terminated) token with an upper-case name. */
bool tokenEquals(const char* token, size_t length, const char* name) {
  size_t i = 0;
  for (; i < length; ++i) {
    if (name[i] == '\0' || toUpper(static_cast<unsigned char>(token[i])) != static_cast<unsigned char>(name[i])) {
      return false;
    }
  }
  return name[i] == '\0';
}

inline bool isSlotCommand(Command c) { return c == Command::kMoveSlot || c == Command::kDispenseSlot; }

/* Wrap-safe: at least `duration` ms have passed since `since` (millis() wraps every ~49.7 days). */
inline bool elapsed(uint32_t now, uint32_t since, uint32_t duration) {
  return static_cast<uint32_t>(now - since) >= duration;
}

inline long absLong(long v) { return v < 0 ? -v : v; }

}  // namespace

/* ------------------------------------------------------------------------- parsing */

ParsedLine parseLine(const char* text, size_t length, uint8_t numSlots) {
  ParsedLine r;
  r.empty = false;
  r.command = Command::kNone;
  r.error = ErrorCode::kNone;
  r.slot = -1;
  if (length > kMaxLineLength) {
    r.error = ErrorCode::kUnknownCommand;
    return r;
  }
  size_t starts[3] = {0, 0, 0};
  size_t lengths[3] = {0, 0, 0};
  size_t count = 0;
  size_t i = 0;
  while (i < length) {
    while (i < length && isSpace(static_cast<unsigned char>(text[i]))) ++i;
    if (i >= length) break;
    const size_t start = i;
    while (i < length && !isSpace(static_cast<unsigned char>(text[i]))) ++i;
    if (count < 3) {
      starts[count] = start;
      lengths[count] = i - start;
    }
    ++count;
  }
  if (count == 0) {
    r.empty = true;
    return r;
  }
  for (size_t k = 0; k < sizeof(kCommands) / sizeof(kCommands[0]); ++k) {
    if (tokenEquals(text + starts[0], lengths[0], kCommands[k].name)) {
      r.command = kCommands[k].command;
      break;
    }
  }
  if (r.command == Command::kNone) {
    r.error = ErrorCode::kUnknownCommand;
    return r;
  }
  if (isSlotCommand(r.command)) {
    /* Argument validation comes before any state check (§7 precedence). */
    if (count != 2 || lengths[1] > kMaxSlotDigits) {
      r.error = ErrorCode::kInvalidSlot;
      return r;
    }
    int value = 0;
    for (size_t k = 0; k < lengths[1]; ++k) {
      const char c = text[starts[1] + k];
      if (c < '0' || c > '9') {
        r.error = ErrorCode::kInvalidSlot;
        return r;
      }
      value = value * 10 + (c - '0');
    }
    if (value >= static_cast<int>(numSlots)) {
      r.error = ErrorCode::kInvalidSlot;
      return r;
    }
    r.slot = value;
    return r;
  }
  if (count > 1) r.error = ErrorCode::kUnknownCommand;
  return r;
}

const char* stateName(DeviceState state) { return kStateNames[static_cast<uint8_t>(state)]; }

const char* commandName(Command command) {
  for (size_t k = 0; k < sizeof(kCommands) / sizeof(kCommands[0]); ++k) {
    if (kCommands[k].command == command) return kCommands[k].name;
  }
  return "NONE";
}

const char* errorName(ErrorCode code) {
  switch (code) {
    case ErrorCode::kInvalidSlot: return "INVALID_SLOT";
    case ErrorCode::kNotHomed: return "NOT_HOMED";
    case ErrorCode::kBusy: return "BUSY";
    case ErrorCode::kHomeTimeout: return "HOME_TIMEOUT";
    case ErrorCode::kMotorFault: return "MOTOR_FAULT";
    case ErrorCode::kInvalidState: return "INVALID_STATE";
    case ErrorCode::kUnknownCommand: return "UNKNOWN_COMMAND";
    case ErrorCode::kStopped: return "STOPPED";
    case ErrorCode::kNone: break;
  }
  return "NONE";
}

/* ------------------------------------------------------------------------- lifecycle */

TactiDoseCore::TactiDoseCore(Hal& hal, const CoreConfig& config) : hal_(&hal), cfg_(config) { resetState(); }

void TactiDoseCore::resetState() {
  state_ = DeviceState::kBoot;
  homed_ = false;
  slot_ = -1;
  targetSlot_ = -1;
  gateOpen_ = true; /* unknown until the boot close has finished: fail closed */
  driverOn_ = false;
  gateMoving_ = false;
  gateDone_ = GateDone::kNone;
  gateStartMs_ = 0;
  gateOpenedMs_ = 0;
  settleStartMs_ = 0;
  motion_ = Motion::kNone;
  motionStartMs_ = 0;
  motionLimitMs_ = 0;
  homePhase_ = HomePhase::kIdle;
  homeStartMs_ = 0;
  phaseStartPos_ = 0;
  phaseMinTravel_ = 0;
  sensorRaw_ = false;
  sensorChangedMs_ = 0;
  confirm_ = Debouncer{false, false, 0};
  cancel_ = Debouncer{false, false, 0};
  confirmPending_ = false;
  cancelPending_ = false;
  lineLen_ = 0;
  lineOverflow_ = false;
}

void TactiDoseCore::begin() {
  resetState();
  if (cfg_.numSlots < kMinSlots) cfg_.numSlots = kMinSlots;
  if (cfg_.numSlots > kMaxSlots) cfg_.numSlots = kMaxSlots;
  const uint32_t now = hal_->millis();
  hal_->stepperEnable(false);
  hal_->stepperSetMaxSpeed(cfg_.maxSpeed);
  hal_->stepperSetAcceleration(cfg_.acceleration);
  hal_->stepperSetCurrentPosition(0);
  /* A button held through reset must be released before it can fire. */
  const bool confirmRaw = hal_->buttonPressed(Button::kConfirm);
  const bool cancelRaw = hal_->buttonPressed(Button::kCancel);
  confirm_ = Debouncer{confirmRaw, confirmRaw, now};
  cancel_ = Debouncer{cancelRaw, cancelRaw, now};
  debug("TactiDose REFERENCE firmware %s - hackathon prototype, NOT a medical device", cfg_.fwVersion);
  debug("slots=%u steps_per_rev=%ld home_sensor=%s auto_home=%s", static_cast<unsigned>(cfg_.numSlots),
        revsToSteps(1.0f), cfg_.hasHomeSensor ? "yes" : "no", cfg_.autoHomeOnBoot ? "yes" : "no");
  /* Rule 8.9: close the gate first. Its position is unknown after a reset, so wait the full travel. */
  startGateTravel(false, GateDone::kBootClosed, now);
}

void TactiDoseCore::loop() {
  const uint32_t now = hal_->millis();
  serviceButtons(now); /* debounced continuously, also during gate travel */
  if (gateMoving_) {
    if (!elapsed(now, gateStartMs_, cfg_.gateTravelMs)) return; /* rule 8.2: travel is atomic */
    finishGateTravel(now);
  }
  serviceMotion(now);
  serviceTimers(now);
  if (!gateMoving_ && cancelPending_) {
    cancelPending_ = false;
    onCancelPressed(now);
  }
  if (!gateMoving_ && confirmPending_) {
    confirmPending_ = false;
    emit("EVENT CONFIRM_BUTTON"); /* rule 8.8: event only, the host decides */
  }
  serviceSerial(now);
  applyDriverPower();
}

void TactiDoseCore::bootSequence(uint32_t now) {
  char line[48];
  snprintf(line, sizeof(line), "EVENT BOOT %s", cfg_.fwVersion);
  emit(line);
  if (!cfg_.autoHomeOnBoot) {
    debug("auto-home disabled: waiting for HOME");
    return;
  }
  if (!cfg_.hasHomeSensor) {
    /* MVP fallback (§6): the carousel was aligned by hand, step counter 0 = slot 0. */
    emit("OK HOMING");
    homeStartMs_ = now;
    hal_->stepperSetCurrentPosition(0);
    finishHoming();
    return;
  }
  startHoming(now);
}

/* ------------------------------------------------------------------------- inputs */

bool TactiDoseCore::updateDebouncer(Debouncer& d, bool raw, uint32_t now) {
  if (raw != d.raw) {
    d.raw = raw;
    d.changedMs = now;
    return false;
  }
  if (raw != d.stable && elapsed(now, d.changedMs, cfg_.debounceMs)) {
    d.stable = raw;
    return raw; /* act on press only */
  }
  return false;
}

void TactiDoseCore::serviceButtons(uint32_t now) {
  if (updateDebouncer(confirm_, hal_->buttonPressed(Button::kConfirm), now)) confirmPending_ = true;
  if (updateDebouncer(cancel_, hal_->buttonPressed(Button::kCancel), now)) cancelPending_ = true;
}

void TactiDoseCore::onCancelPressed(uint32_t now) {
  emit("EVENT CANCEL_BUTTON"); /* rule 8.8: the event first, then the local safety action */
  switch (state_) {
    case DeviceState::kHoming:
    case DeviceState::kMoving:
    case DeviceState::kAtTarget:
      interruptMotion();
      break;
    case DeviceState::kGateOpen:
      startGateTravel(false, GateDone::kClosedToReady, now);
      break;
    default:
      break;
  }
}

void TactiDoseCore::serviceSerial(uint32_t now) {
  for (int budget = 256; budget > 0 && !gateMoving_; --budget) {
    const int c = hal_->serialRead();
    if (c < 0) return;
    if (c == '\n' || c == '\r') {
      handleLine(now); /* "\r\n" yields an extra empty line, which is ignored */
      continue;
    }
    if (lineLen_ < kMaxLineLength) {
      line_[lineLen_++] = static_cast<char>(c);
    } else {
      lineOverflow_ = true;
    }
  }
}

void TactiDoseCore::handleLine(uint32_t now) {
  const bool overflow = lineOverflow_;
  const size_t length = lineLen_;
  lineLen_ = 0;
  lineOverflow_ = false;
  if (overflow) {
    emitError(ErrorCode::kUnknownCommand); /* §1: over-long input is discarded */
    return;
  }
  const ParsedLine parsed = parseLine(line_, length, cfg_.numSlots);
  if (parsed.empty) return;
  if (parsed.error != ErrorCode::kNone) {
    emitError(parsed.error);
    return;
  }
  execute(parsed, now);
}

/* ------------------------------------------------------------------------- commands (§7 table) */

void TactiDoseCore::execute(const ParsedLine& parsed, uint32_t now) {
  const bool busy =
      state_ == DeviceState::kHoming || state_ == DeviceState::kMoving || state_ == DeviceState::kAtTarget;
  const bool gateOpenState = state_ == DeviceState::kGateOpen;
  const bool unhomed = !homed_ || state_ == DeviceState::kBoot || state_ == DeviceState::kSafeStop ||
                       state_ == DeviceState::kFault;
  switch (parsed.command) {
    case Command::kPing:
      emit("OK PONG");
      return;
    case Command::kStatus:
      sendStatus();
      return;
    case Command::kStop:
      handleStop(now);
      return;
    case Command::kHome:
      if (busy) {
        emitError(ErrorCode::kBusy);
      } else if (gateOpenState) {
        emitError(ErrorCode::kInvalidState);
      } else {
        startHoming(now);
      }
      return;
    case Command::kMoveSlot:
    case Command::kDispenseSlot:
      if (busy) {
        emitError(ErrorCode::kBusy);
      } else if (gateOpenState) {
        emitError(ErrorCode::kInvalidState);
      } else if (unhomed) {
        emitError(ErrorCode::kNotHomed);
      } else {
        startMove(parsed.slot, parsed.command == Command::kDispenseSlot, now);
      }
      return;
    case Command::kOpenGate:
      if (busy) {
        emitError(ErrorCode::kBusy);
      } else if (gateOpenState) {
        emit("OK GATE_OPEN"); /* idempotent; does not restart the auto-close timer */
      } else if (unhomed) {
        emitError(ErrorCode::kNotHomed);
      } else {
        startGateTravel(true, GateDone::kOpened, now);
      }
      return;
    case Command::kCloseGate:
      if (busy) {
        emitError(ErrorCode::kBusy);
      } else if (gateOpenState) {
        startGateTravel(false, GateDone::kClosedToReady, now);
      } else {
        hal_->servoWrite(cfg_.servoClosedDeg); /* re-assert closed, no state change */
        emit("OK GATE_CLOSED");
      }
      return;
    case Command::kNone:
      break;
  }
  emitError(ErrorCode::kUnknownCommand);
}

void TactiDoseCore::handleStop(uint32_t now) {
  switch (state_) {
    case DeviceState::kHoming:
    case DeviceState::kMoving:
    case DeviceState::kAtTarget:
      interruptMotion(); /* ERR STOPPED, OK STOPPED */
      return;
    case DeviceState::kGateOpen:
      startGateTravel(false, GateDone::kClosedForStop, now); /* OK GATE_CLOSED, OK STOPPED */
      return;
    default:
      enterSafeStop(); /* OK STOPPED */
      return;
  }
}

/* ------------------------------------------------------------------------- homing (rule 8.5) */

void TactiDoseCore::startHoming(uint32_t now) {
  hal_->servoWrite(cfg_.servoClosedDeg); /* rule 8.1 */
  state_ = DeviceState::kHoming;
  homed_ = false;
  slot_ = -1;
  motion_ = Motion::kHoming;
  homeStartMs_ = now;
  applyDriverPower();
  emit("OK HOMING");
  if (!cfg_.hasHomeSensor) {
    /* No sensor: "return to step 0 by dead reckoning" (§6). */
    homePhase_ = HomePhase::kDeadReckon;
    beginTimedMove(0, cfg_.maxSpeed, now);
    return;
  }
  if (hal_->homeSensorActive()) {
    /* Already on the sensor: leave it first so the edge is always found while approaching. A
     * sensor that never releases (shorted, stuck switch) must not pass as "home". */
    startHomePhase(HomePhase::kRelease, -cfg_.homingDir, cfg_.homingSpeed, cfg_.homeBackoffSteps,
                   revsToSteps(cfg_.homeReleaseMaxRevs), now);
  } else {
    startHomePhase(HomePhase::kSeek, cfg_.homingDir, cfg_.homingSpeed, 0, revsToSteps(cfg_.homeMaxTravelRevs),
                   now);
  }
}

void TactiDoseCore::startHomePhase(HomePhase phase, int dir, float speed, long minTravel, long maxTravel,
                                   uint32_t now) {
  homePhase_ = phase;
  hal_->stepperSetMaxSpeed(speed);
  phaseStartPos_ = hal_->stepperCurrentPosition();
  phaseMinTravel_ = minTravel;
  hal_->stepperMoveTo(phaseStartPos_ + static_cast<long>(dir) * maxTravel);
  sensorRaw_ = hal_->homeSensorActive();
  sensorChangedMs_ = now;
}

void TactiDoseCore::serviceHoming(uint32_t now) {
  if (homePhase_ == HomePhase::kDeadReckon || homePhase_ == HomePhase::kOffset) {
    if (hal_->stepperDistanceToGo() == 0) {
      finishHoming();
    } else if (elapsed(now, motionStartMs_, motionLimitMs_)) {
      debug("home: move to step 0 did not complete within %lu ms", static_cast<unsigned long>(motionLimitMs_));
      enterFault(ErrorCode::kMotorFault);
    }
    return;
  }
  if (elapsed(now, homeStartMs_, cfg_.homeTimeoutMs)) {
    debug("home: no home edge within HOME_TIMEOUT_MS=%lu", static_cast<unsigned long>(cfg_.homeTimeoutMs));
    enterFault(ErrorCode::kHomeTimeout);
    return;
  }
  const bool raw = hal_->homeSensorActive();
  if (raw != sensorRaw_) {
    sensorRaw_ = raw;
    sensorChangedMs_ = now;
  }
  const bool steady = elapsed(now, sensorChangedMs_, cfg_.homeDebounceMs);
  const long travelled = absLong(hal_->stepperCurrentPosition() - phaseStartPos_);
  switch (homePhase_) {
    case HomePhase::kRelease:
    case HomePhase::kBackoff:
      if (!raw && steady && travelled >= phaseMinTravel_) {
        hal_->stepperStop();
        if (homePhase_ == HomePhase::kRelease) {
          startHomePhase(HomePhase::kSeek, cfg_.homingDir, cfg_.homingSpeed, 0,
                         revsToSteps(cfg_.homeMaxTravelRevs), now);
        } else {
          startHomePhase(HomePhase::kReapproach, cfg_.homingDir, cfg_.homingSlowSpeed, 0,
                         travelled + revsToSteps(cfg_.homeReleaseMaxRevs), now);
        }
      } else if (hal_->stepperDistanceToGo() == 0) {
        debug("home: sensor still active after %ld steps (stuck sensor?)", travelled);
        enterFault(ErrorCode::kHomeTimeout);
      }
      return;
    case HomePhase::kSeek:
    case HomePhase::kReapproach:
      if (raw && steady) {
        hal_->stepperStop();
        if (homePhase_ == HomePhase::kSeek && cfg_.homeBackoffSteps > 0) {
          startHomePhase(HomePhase::kBackoff, -cfg_.homingDir, cfg_.homingSpeed, cfg_.homeBackoffSteps,
                         revsToSteps(cfg_.homeReleaseMaxRevs), now);
        } else {
          homeEdgeFound(now);
        }
      } else if (hal_->stepperDistanceToGo() == 0) {
        debug("home: sensor not found within %ld steps", travelled);
        enterFault(ErrorCode::kHomeTimeout);
      }
      return;
    default:
      return;
  }
}

void TactiDoseCore::homeEdgeFound(uint32_t now) {
  /* The edge is the home reference; slot 0 lies homeOffsetSteps beyond it in + step direction. */
  hal_->stepperSetCurrentPosition(-cfg_.homeOffsetSteps);
  if (cfg_.homeOffsetSteps != 0) {
    homePhase_ = HomePhase::kOffset;
    beginTimedMove(0, cfg_.maxSpeed, now);
    return;
  }
  finishHoming();
}

void TactiDoseCore::finishHoming() {
  homePhase_ = HomePhase::kIdle;
  motion_ = Motion::kNone;
  homed_ = true;
  slot_ = 0;
  debug("home: done after %lu ms", static_cast<unsigned long>(hal_->millis() - homeStartMs_));
  emit("OK HOMED");
  enterReady();
}

/* ------------------------------------------------------------------------- slot moves */

void TactiDoseCore::startMove(int slot, bool dispense, uint32_t now) {
  hal_->servoWrite(cfg_.servoClosedDeg); /* rule 8.1: re-assert closed (already closed in READY) */
  state_ = DeviceState::kMoving;
  slot_ = -1;
  targetSlot_ = slot;
  motion_ = dispense ? Motion::kDispense : Motion::kMove;
  applyDriverPower();
  beginTimedMove(slotTarget(slot), cfg_.maxSpeed, now);
  char line[24];
  snprintf(line, sizeof(line), "OK MOVING %d", slot);
  emit(line);
}

void TactiDoseCore::beginTimedMove(long target, float speed, uint32_t now) {
  hal_->stepperSetMaxSpeed(speed);
  const long distance = target - hal_->stepperCurrentPosition();
  hal_->stepperMoveTo(target);
  motionStartMs_ = now;
  motionLimitMs_ = motionLimitMs(distance, speed);
}

void TactiDoseCore::serviceMotion(uint32_t now) {
  if (motion_ == Motion::kNone) return;
  hal_->stepperRun();
  if (state_ == DeviceState::kHoming) {
    serviceHoming(now);
    return;
  }
  if (state_ != DeviceState::kMoving) return;
  if (hal_->stepperDistanceToGo() == 0) {
    arrive(now);
  } else if (elapsed(now, motionStartMs_, motionLimitMs_)) {
    /* Rule 8.6: an open-loop stepper cannot see a jam, but a move that never completes can. */
    debug("move: slot %d not reached within %lu ms (%ld steps to go)", targetSlot_,
          static_cast<unsigned long>(motionLimitMs_), hal_->stepperDistanceToGo());
    enterFault(ErrorCode::kMotorFault);
  }
}

void TactiDoseCore::arrive(uint32_t now) {
  if (cfg_.verifySlotWithHomeSensor && cfg_.hasHomeSensor) {
    const bool active = hal_->homeSensorActive();
    if (active != (targetSlot_ == 0)) {
      debug("move: home sensor %s at slot %d - position lost", active ? "active" : "inactive", targetSlot_);
      enterFault(ErrorCode::kMotorFault);
      return;
    }
  }
  slot_ = targetSlot_;
  const bool dispense = motion_ == Motion::kDispense;
  motion_ = Motion::kNone;
  char line[24];
  snprintf(line, sizeof(line), "OK AT_SLOT %d", slot_);
  emit(line);
  if (dispense) {
    state_ = DeviceState::kAtTarget; /* rule 8.4: settle, then open */
    settleStartMs_ = now;
  } else {
    enterReady();
  }
}

void TactiDoseCore::serviceTimers(uint32_t now) {
  if (state_ == DeviceState::kAtTarget && elapsed(now, settleStartMs_, cfg_.settleMs)) {
    startGateTravel(true, GateDone::kOpened, now);
  } else if (state_ == DeviceState::kGateOpen && elapsed(now, gateOpenedMs_, cfg_.gateMaxOpenMs)) {
    debug("gate open for %lu ms: closing it (safety net)", static_cast<unsigned long>(cfg_.gateMaxOpenMs));
    startGateTravel(false, GateDone::kClosedToReady, now); /* rule 8.7 */
  }
}

/* ------------------------------------------------------------------------- gate */

void TactiDoseCore::startGateTravel(bool open, GateDone done, uint32_t now) {
  hal_->servoWrite(open ? cfg_.servoOpenDeg : cfg_.servoClosedDeg);
  if (open) gateOpen_ = true; /* an opening gate counts as open */
  gateMoving_ = true;
  gateDone_ = done;
  gateStartMs_ = now;
  applyDriverPower(); /* hold the carousel while the gate moves */
}

void TactiDoseCore::finishGateTravel(uint32_t now) {
  gateMoving_ = false;
  const GateDone done = gateDone_;
  gateDone_ = GateDone::kNone;
  switch (done) {
    case GateDone::kBootClosed:
      gateOpen_ = false;
      bootSequence(now);
      break;
    case GateDone::kOpened:
      gateOpen_ = true;
      state_ = DeviceState::kGateOpen;
      gateOpenedMs_ = now;
      emit("OK GATE_OPEN");
      break;
    case GateDone::kClosedToReady:
      gateOpen_ = false;
      emit("OK GATE_CLOSED");
      enterReady();
      break;
    case GateDone::kClosedForStop:
      gateOpen_ = false;
      emit("OK GATE_CLOSED");
      enterSafeStop();
      break;
    case GateDone::kNone:
      break;
  }
}

/* ------------------------------------------------------------------------- state entries */

void TactiDoseCore::interruptMotion() {
  hal_->stepperStop();
  emitError(ErrorCode::kStopped);
  enterSafeStop();
}

void TactiDoseCore::enterReady() {
  state_ = DeviceState::kReady;
  motion_ = Motion::kNone;
  hal_->stepperSetMaxSpeed(cfg_.maxSpeed);
  applyDriverPower();
  emit("OK READY"); /* emitted on every transition into READY (§5) */
}

void TactiDoseCore::enterSafeStop() {
  hal_->stepperStop();
  hal_->servoWrite(cfg_.servoClosedDeg);
  state_ = DeviceState::kSafeStop;
  homed_ = false; /* position treated as unknown: only HOME makes the device homed again */
  slot_ = -1;
  motion_ = Motion::kNone;
  homePhase_ = HomePhase::kIdle;
  applyDriverPower();
  emit("OK STOPPED");
}

void TactiDoseCore::enterFault(ErrorCode code) {
  hal_->stepperStop();
  hal_->servoWrite(cfg_.servoClosedDeg); /* "closed (if possible)": motion only ever runs with it closed */
  state_ = DeviceState::kFault;
  homed_ = false;
  slot_ = -1;
  motion_ = Motion::kNone;
  homePhase_ = HomePhase::kIdle;
  applyDriverPower();
  emitError(code);
}

void TactiDoseCore::applyDriverPower() {
  bool on = false;
  switch (state_) {
    case DeviceState::kHoming:
    case DeviceState::kMoving:
    case DeviceState::kAtTarget:
    case DeviceState::kGateOpen:
      on = true; /* hold the compartment at the opening while it is accessible */
      break;
    case DeviceState::kReady:
    case DeviceState::kSafeStop:
      on = cfg_.holdWhenIdle || gateMoving_;
      break;
    case DeviceState::kBoot:
    case DeviceState::kFault:
      on = false; /* FAULT: release so a jam can be cleared by hand; position is unknown anyway */
      break;
  }
  if (on != driverOn_) {
    driverOn_ = on;
    hal_->stepperEnable(on);
  }
}

/* ------------------------------------------------------------------------- helpers */

long TactiDoseCore::slotTarget(int slot) const {
  const float steps = static_cast<float>(slot) * cfg_.stepsPerRev / static_cast<float>(cfg_.numSlots);
  return static_cast<long>(steps + 0.5f);
}

uint32_t TactiDoseCore::expectedMoveMs(long distance, float maxSpeed) const {
  const float d = static_cast<float>(absLong(distance));
  const float a = cfg_.acceleration;
  if (d <= 0.0f || maxSpeed <= 0.0f || a <= 0.0f) return 0;
  float seconds;
  if (d >= maxSpeed * maxSpeed / a) {
    seconds = d / maxSpeed + maxSpeed / a; /* trapezoid: accelerate, cruise, decelerate */
  } else {
    seconds = 2.0f * sqrtf(d / a); /* triangle: never reaches maxSpeed */
  }
  return static_cast<uint32_t>(seconds * 1000.0f + 0.5f);
}

uint32_t TactiDoseCore::motionLimitMs(long distance, float maxSpeed) const {
  const float expected = static_cast<float>(expectedMoveMs(distance, maxSpeed));
  return static_cast<uint32_t>(cfg_.motionTimeoutFactor * expected + 0.5f) + cfg_.motionTimeoutMarginMs;
}

long TactiDoseCore::revsToSteps(float revs) const { return static_cast<long>(revs * cfg_.stepsPerRev + 0.5f); }

void TactiDoseCore::sendStatus() {
  char line[112];
  snprintf(line, sizeof(line), "OK STATUS state=%s homed=%d slot=%d gate=%s slots=%u fw=%s", stateName(state_),
           homed_ ? 1 : 0, slot_, gateOpen_ ? "OPEN" : "CLOSED", static_cast<unsigned>(cfg_.numSlots),
           cfg_.fwVersion);
  emit(line);
}

void TactiDoseCore::emit(const char* line) { hal_->serialWriteLine(line); }

void TactiDoseCore::emitError(ErrorCode code) {
  char line[32];
  snprintf(line, sizeof(line), "ERR %s", errorName(code));
  emit(line);
}

void TactiDoseCore::debug(const char* fmt, ...) {
  if (!cfg_.debugLog) return;
  char line[120];
  line[0] = '#';
  line[1] = ' ';
  va_list args;
  va_start(args, fmt);
  vsnprintf(line + 2, sizeof(line) - 2, fmt, args);
  va_end(args);
  emit(line);
}

}  // namespace tactidose
