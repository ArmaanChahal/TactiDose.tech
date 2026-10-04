/*
 * FakeHal.cpp -- simulated dispenser physics + physical safety oracle for the native harness.
 * See FakeHal.h for the model.
 */
#include "FakeHal.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace harness {

namespace {

bool startsWith(const char* s, const char* prefix) { return strncmp(s, prefix, strlen(prefix)) == 0; }

}  // namespace

FakeHal::FakeHal(const Physics& physics) : phys_(physics) { resetAll(); }

void FakeHal::resetAll() {
  simMs_ = 0;
  bootMs_ = 0;
  physPos_ = -static_cast<long long>(phys_.initialOffsetSteps);
  sensor_ = SensorMode::kOk;
  dropSensor_ = DropSensorMode::kOk;
  jam_ = false;
  buttons_[0] = buttons_[1] = false;
  for (int g = 0; g < kMaxContainers; ++g) {
    gateDeg_[g] = -1;
    gateSettledMs_[g] = 0;
    gateOpens_[g] = 0;
    gateReleased_[g] = false;
    pills_[g] = phys_.initialPills;
  }
  pillsDropped_ = 0;
  dropPulses_ = 0;
  beamFromMs_ = 0;
  beamUntilMs_ = 0;
  violations_ = 0;
  lastViolation_[0] = '\0';
  powerOn(true, true);
}

void FakeHal::powerOn(bool homeSensorFitted, bool dropSensorFitted) {
  bootMs_ = simMs_;
  homeSensorFitted_ = homeSensorFitted;
  dropSensorFitted_ = dropSensorFitted;
  stepPos_ = 0;
  stepTarget_ = 0;
  speed_ = 0.0;
  maxSpeed_ = 1.0;
  accel_ = 1.0;
  frac_ = 0.0;
  enabled_ = false;
  lastRunMs_ = simMs_;
  rxHead_ = 0;
  rxCount_ = 0;
  fallsThisDrop_ = 0;
  fallContainer_ = -1;
}

void FakeHal::advanceOneMs() {
  ++simMs_;
  releasePills();
}

bool FakeHal::queueRx(const char* data, size_t length) {
  bool ok = true;
  for (size_t i = 0; i < length; ++i) {
    if (rxCount_ == kRxSize) {
      ok = false; /* a real UART drops bytes when its buffer is full */
      continue;
    }
    rx_[(rxHead_ + rxCount_) % kRxSize] = static_cast<unsigned char>(data[i]);
    ++rxCount_;
  }
  return ok;
}

void FakeHal::setButton(tactidose::Button button, bool pressed) { buttons_[static_cast<int>(button)] = pressed; }

void FakeHal::setMuted(bool muted) {
  muted_ = muted;
  mutedLines_ = 0;
}

long long FakeHal::phase() const {
  const long long rev = phys_.stepsPerRev;
  long long m = physPos_ % rev;
  if (m < 0) m += rev;
  return m;
}

int FakeHal::physicalSlot() const {
  const double spacing = static_cast<double>(phys_.stepsPerRev) / static_cast<double>(phys_.numSlots);
  const double m = static_cast<double>(phase());
  const long long nearest = llround(m / spacing);
  if (fabs(m - static_cast<double>(nearest) * spacing) > static_cast<double>(phys_.slotToleranceSteps)) return -1;
  return static_cast<int>(nearest % phys_.numSlots);
}

bool FakeHal::allGatesClosed() const {
  for (int g = 0; g < gateCount(); ++g) {
    if (!gateFullyClosed(g)) return false;
  }
  return true;
}

int FakeHal::openGate() const {
  for (int g = 0; g < gateCount(); ++g) {
    if (gateDeg_[g] >= 0 && gateDeg_[g] != phys_.servoClosedDeg) return g;
  }
  return -1;
}

void FakeHal::violation(const char* what) {
  ++violations_;
  snprintf(lastViolation_, sizeof(lastViolation_), "%s@%llu", what, static_cast<unsigned long long>(simMs_));
}

/* ------------------------------------------------------------------ physics: pills */

void FakeHal::releasePills() {
  for (int g = 0; g < gateCount(); ++g) {
    if (gateReleased_[g] || !gateFullyOpen(g)) continue;
    gateReleased_[g] = true; /* one pill per opening */
    const int container = phys_.perContainer ? g : physicalSlot();
    if (container < 0 || pills_[container] <= 0) continue;
    --pills_[container];
    ++pillsDropped_;
    ++fallsThisDrop_;
    fallContainer_ = container;
    beamFromMs_ = simMs_ + phys_.pillFallMs;
    beamUntilMs_ = beamFromMs_ + phys_.pillPulseMs;
    if (dropSensor_ == DropSensorMode::kOk) ++dropPulses_;
  }
}

bool FakeHal::beamInterrupted() const {
  switch (dropSensor_) {
    case DropSensorMode::kDead:
      return false;
    case DropSensorMode::kBlocked:
      return true;
    case DropSensorMode::kOk:
      break;
  }
  return simMs_ >= beamFromMs_ && simMs_ < beamUntilMs_;
}

/* ------------------------------------------------------------------ Hal: time */

uint32_t FakeHal::fwMillis() const {
  return static_cast<uint32_t>(millisOffset_ + static_cast<uint32_t>(simMs_ - bootMs_));
}

uint32_t FakeHal::millis() { return fwMillis(); }

/* ------------------------------------------------------------------ Hal: stepper */

void FakeHal::stepperSetMaxSpeed(float stepsPerSecond) {
  maxSpeed_ = stepsPerSecond > 0.0f ? static_cast<double>(stepsPerSecond) : 1.0;
}

void FakeHal::stepperSetAcceleration(float stepsPerSecondSquared) {
  accel_ = stepsPerSecondSquared > 0.0f ? static_cast<double>(stepsPerSecondSquared) : 1.0;
}

void FakeHal::stepperMoveTo(long absolutePosition) {
  stepTarget_ = absolutePosition;
  lastRunMs_ = simMs_; /* timing of a new move starts now */
}

void FakeHal::stepperStop() {
  stepTarget_ = stepPos_;
  speed_ = 0.0;
  frac_ = 0.0;
}

void FakeHal::stepperSetCurrentPosition(long position) {
  stepPos_ = position;
  stepTarget_ = position;
  speed_ = 0.0;
  frac_ = 0.0;
  lastRunMs_ = simMs_;
}

bool FakeHal::stepperRun() {
  uint64_t dtMs = simMs_ - lastRunMs_;
  lastRunMs_ = simMs_;
  const long remaining = stepTarget_ - stepPos_;
  if (remaining == 0) {
    speed_ = 0.0;
    frac_ = 0.0;
    return false;
  }
  if (dtMs == 0) return true;
  if (dtMs > 10) dtMs = 10;
  if (!enabled_ || jam_) { /* de-energised driver or jammed carousel: no motion, never completes */
    speed_ = 0.0;
    frac_ = 0.0;
    return true;
  }
  const double dt = static_cast<double>(dtMs) / 1000.0;
  const long distance = remaining < 0 ? -remaining : remaining;
  double v = speed_ + accel_ * dt;
  if (v > maxSpeed_) v = maxSpeed_;
  const double vStop = sqrt(2.0 * accel_ * static_cast<double>(distance)); /* fastest speed that still stops */
  if (v > vStop) v = vStop;
  frac_ += v * dt;
  long count = static_cast<long>(frac_);
  if (count > distance) count = distance;
  frac_ -= static_cast<double>(count);
  speed_ = v;
  if (count > 0) takeSteps(remaining < 0 ? -1 : 1, count);
  if (stepPos_ == stepTarget_) {
    speed_ = 0.0;
    frac_ = 0.0;
    return false;
  }
  return true;
}

void FakeHal::takeSteps(long direction, long count) {
  if (phys_.perContainer) violation("stepper_moved_without_carousel");
  if (!allGatesClosed()) violation("step_while_gate_not_closed");
  stepPos_ += direction * count;
  physPos_ += static_cast<long long>(direction * count);
}

/* ------------------------------------------------------------------ Hal: gates, inputs */

void FakeHal::servoWrite(uint8_t gate, uint8_t degrees) {
  const int g = gate;
  if (g >= gateCount()) {
    violation("bad_gate_index");
    return;
  }
  const int deg = degrees;
  if (deg != phys_.servoClosedDeg) {
    if (motorMoving()) violation("gate_opened_while_moving");
    if (!phys_.perContainer && physicalSlot() < 0) violation("gate_opened_between_slots");
    for (int other = 0; other < gateCount(); ++other) {
      if (other != g && gateDeg_[other] >= 0 && gateDeg_[other] != phys_.servoClosedDeg) violation("two_gates_open");
    }
  }
  if (deg != gateDeg_[g]) {
    if (deg == phys_.servoOpenDeg) ++gateOpens_[g];
    gateDeg_[g] = deg;
    gateSettledMs_[g] = simMs_ + phys_.gateTravelMs;
    gateReleased_[g] = false;
  }
}

bool FakeHal::sensorReads() const {
  switch (sensor_) {
    case SensorMode::kDead:
      return false;
    case SensorMode::kStuck:
      return true;
    case SensorMode::kOk:
      break;
  }
  return phase() < phys_.sensorZoneSteps;
}

bool FakeHal::homeSensorActive() { return !phys_.perContainer && sensorReads(); }

bool FakeHal::buttonPressed(tactidose::Button button) { return buttons_[static_cast<int>(button)]; }

bool FakeHal::dropSensorActive() { return beamInterrupted(); }

/* ------------------------------------------------------------------ Hal: serial */

int FakeHal::serialRead() {
  if (rxCount_ == 0) return -1;
  const int c = rx_[rxHead_];
  rxHead_ = (rxHead_ + 1) % kRxSize;
  --rxCount_;
  return c;
}

void FakeHal::serialWriteLine(const char* line) {
  checkLine(line);
  if (muted_) {
    ++mutedLines_;
    return;
  }
  if (out_ != nullptr) {
    out_->append(line);
    out_->push_back('\n');
  }
}

void FakeHal::checkLine(const char* line) {
  if (startsWith(line, "EVENT BOOT")) {
    if (!allGatesClosed()) violation("boot_event_before_gate_closed");
  } else if (strcmp(line, "OK GATE_OPEN") == 0) {
    const int g = openGate();
    if (g < 0 || !gateFullyOpen(g)) violation("gate_open_reported_early");
  } else if (strcmp(line, "OK GATE_CLOSED") == 0) {
    if (!allGatesClosed()) violation("gate_closed_reported_early");
  } else if (startsWith(line, "OK MOVING ")) {
    fallsThisDrop_ = 0; /* every DROP_SLOT starts with OK MOVING n */
    fallContainer_ = -1;
  } else if (startsWith(line, "OK AT_SLOT ")) {
    if (motorMoving()) violation("at_slot_while_moving");
    /* Without a sensor the firmware trusts a hand alignment the simulation does not model. */
    if (!phys_.perContainer && homeSensorFitted_ && physicalSlot() != atoi(line + 11)) {
      violation("at_slot_wrong_physical_slot");
    }
  } else if (startsWith(line, "OK DROPPED ") || strcmp(line, "ERR NO_PILL") == 0) {
    const bool dropped = line[0] == 'O';
    if (!allGatesClosed()) violation("verdict_before_gate_closed");
    if (fallsThisDrop_ > 1) violation("two_pills_in_one_drop");
    /* The verdict must match the physics when the firmware reads a working drop sensor. */
    if (dropSensorFitted_ && dropSensor_ == DropSensorMode::kOk) {
      if (dropped && fallsThisDrop_ == 0) violation("dropped_without_pill");
      if (!dropped && fallsThisDrop_ > 0) violation("no_pill_but_pill_fell");
      if (dropped && fallsThisDrop_ > 0 && (phys_.perContainer || homeSensorFitted_) &&
          fallContainer_ != atoi(line + 11)) {
        violation("dropped_wrong_container");
      }
    }
  } else if (strcmp(line, "OK READY") == 0) {
    if (motorMoving()) violation("ready_while_moving");
  } else if (strcmp(line, "OK HOMED") == 0) {
    if (!phys_.perContainer && homeSensorFitted_ && physicalSlot() != 0) violation("homed_off_slot0");
  }
}

void FakeHal::describe(std::string& out) const {
  bool unknown = false;
  bool moving = false;
  bool open = false;
  std::string gates;
  std::string opensByGate;
  uint32_t opens = 0;
  for (int g = 0; g < gateCount(); ++g) {
    const char* state = "unknown";
    if (gateDeg_[g] < 0) {
      unknown = true;
    } else if (!gateSettled(g)) {
      state = "moving";
      moving = true;
    } else if (gateDeg_[g] == phys_.servoClosedDeg) {
      state = "closed";
    } else {
      state = "open";
      open = true;
    }
    if (g > 0) {
      gates.push_back(',');
      opensByGate.push_back(',');
    }
    gates += state;
    opensByGate += std::to_string(gateOpens_[g]);
    opens += gateOpens_[g];
  }
  const char* gate = unknown ? "unknown" : (moving ? "moving" : (open ? "open" : "closed"));
  std::string pills;
  for (int c = 0; c < phys_.numSlots && c < kMaxContainers; ++c) {
    if (c > 0) pills.push_back(',');
    pills += std::to_string(pills_[c]);
  }
  const char* sensorMode = sensor_ == SensorMode::kOk ? "ok" : (sensor_ == SensorMode::kDead ? "dead" : "stuck");
  const char* dropMode =
      dropSensor_ == DropSensorMode::kOk ? "ok" : (dropSensor_ == DropSensorMode::kDead ? "dead" : "blocked");
  char buf[512];
  snprintf(buf, sizeof(buf),
           "t=%llu millis=%lu mechanism=%s pos=%lld phase=%lld phys_slot=%d sensor=%d sensor_mode=%s "
           "sensor_fitted=%d jam=%d gate=%s gate_deg=%d gate_opens=%lu driver=%d step_pos=%ld step_target=%ld "
           "speed=%ld rx_pending=%lu ",
           static_cast<unsigned long long>(simMs_), static_cast<unsigned long>(fwMillis()),
           phys_.perContainer ? "per_container" : "carousel", physPos_, phase(),
           phys_.perContainer ? -1 : physicalSlot(), sensorReads() ? 1 : 0, sensorMode, homeSensorFitted_ ? 1 : 0,
           jam_ ? 1 : 0, gate, gateDeg_[0], static_cast<unsigned long>(opens), enabled_ ? 1 : 0, stepPos_,
           stepTarget_, static_cast<long>(speed_), static_cast<unsigned long>(rxCount_));
  out.append(buf);
  out += "gates=" + gates + " gate_opens_by_gate=" + opensByGate + " pills=" + pills;
  snprintf(buf, sizeof(buf),
           " pills_dropped=%lu drop_pulses=%lu drop_sensor_mode=%s drop_sensor_fitted=%d beam=%d violations=%lu "
           "last_violation=%s",
           static_cast<unsigned long>(pillsDropped_), static_cast<unsigned long>(dropPulses_), dropMode,
           dropSensorFitted_ ? 1 : 0, beamInterrupted() ? 1 : 0, static_cast<unsigned long>(violations_),
           lastViolation_[0] != '\0' ? lastViolation_ : "none");
  out.append(buf);
}

}  // namespace harness
