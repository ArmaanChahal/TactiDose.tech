/*
 * TactiDoseCore.h -- TactiDose firmware state machine (portable C++, no Arduino includes).
 *
 * REFERENCE FIRMWARE for a hackathon prototype: the hardware teammate adapts config.h and
 * ArduinoHal; this core should not need changes. NOT a medical device: candy/tokens only.
 *
 * Implements docs/SERIAL_PROTOCOL.md (v1) exactly: line parsing identical to
 * tactidose/hardware/protocol.py parse_command, the states of §3, the replies of §5/§6, the
 * acceptance table of §7 and the device rules of §8. The same source is compiled for the ESP32
 * (Arduino) and natively for the conformance harness (firmware/native), which runs every
 * scenario of tactidose/hardware/conformance.json against it.
 *
 * Written in a conservative C++11 subset (no STL, no heap, no exceptions) so that it builds
 * with any Arduino-ESP32 core (2.x uses gnu++11, 3.x gnu++2b) and natively with -std=c++17.
 *
 * Design: loop() never blocks. Gate travel is modelled as an "atomic" phase (rule 8.2): while
 * the servo moves, serial input and button presses are held and processed afterwards (buttons
 * keep being debounced, so a short press during travel is not lost).
 */
#ifndef TACTIDOSE_CORE_H
#define TACTIDOSE_CORE_H

#include <stddef.h>
#include <stdint.h>

#include "Hal.h"

#if defined(__GNUC__)
#define TACTIDOSE_PRINTF(fmt_index, args_index) __attribute__((format(printf, fmt_index, args_index)))
#else
#define TACTIDOSE_PRINTF(fmt_index, args_index)
#endif

namespace tactidose {

static const size_t kMaxLineLength = 64; /* SERIAL_PROTOCOL.md §1, excluding the terminator */
static const uint8_t kMinSlots = 2;
static const uint8_t kMaxSlots = 12;
static const size_t kMaxSlotDigits = 3;

enum class DeviceState : uint8_t {
  kBoot,
  kHoming,
  kReady,
  kMoving,
  kAtTarget,
  kGateOpen,
  kSafeStop,
  kFault,
};

enum class Command : uint8_t {
  kNone,
  kPing,
  kStatus,
  kHome,
  kMoveSlot,
  kDispenseSlot,
  kOpenGate,
  kCloseGate,
  kStop,
};

enum class ErrorCode : uint8_t {
  kNone,
  kInvalidSlot,
  kNotHomed,
  kBusy,
  kHomeTimeout,
  kMotorFault,
  kInvalidState,
  kUnknownCommand,
  kStopped,
};

/* Result of parsing one host line (mirrors protocol.ParsedCommand). Exactly one of:
 *   empty                      -> blank line, ignore silently;
 *   error == kNone             -> valid command (slot >= 0 for MOVE_SLOT / DISPENSE_SLOT);
 *   error != kNone             -> reply "ERR <error>".
 * `command` is set whenever the command word was recognised, even with a bad argument. */
struct ParsedLine {
  bool empty;
  Command command;
  ErrorCode error;
  int slot;
};

/* Parse exactly like protocol.parse_command: lines longer than kMaxLineLength ->
 * UNKNOWN_COMMAND; case-insensitive; surrounding/repeated whitespace ignored (Python
 * str.isspace() set); slot = exactly one token of 1-3 ASCII digits with value < numSlots,
 * else INVALID_SLOT; extra arguments on other commands -> UNKNOWN_COMMAND. */
ParsedLine parseLine(const char* text, size_t length, uint8_t numSlots);

const char* stateName(DeviceState state);
const char* commandName(Command command);
const char* errorName(ErrorCode code);

/* Every tunable of the core. ConfigCheck.h fills it from config.h on the ESP32; the native
 * harness fills it from the conformance.json "harness" physics. Defaults = reference hardware
 * (NEMA17 200 steps x 16 microsteps, direct drive) and the Python simulator defaults. */
struct CoreConfig {
  const char* fwVersion = "1.0.0-ref"; /* no spaces: EVENT BOOT <fw>, STATUS fw=<fw> */
  uint8_t numSlots = 6;
  float stepsPerRev = 3200.0f; /* (micro)steps per carousel revolution, may be fractional */
  float maxSpeed = 1600.0f;    /* steps/s */
  float acceleration = 3200.0f; /* steps/s^2 */

  bool hasHomeSensor = true;    /* false: MVP dead-reckoning fallback (protocol §6) */
  bool autoHomeOnBoot = true;   /* false: stay in BOOT until the host sends HOME */
  int8_t homingDir = 1;         /* +1 / -1: step direction used to seek the sensor */
  float homingSpeed = 400.0f;   /* steps/s, first (fast) approach */
  float homingSlowSpeed = 100.0f; /* steps/s, re-approach after the back-off */
  float homeMaxTravelRevs = 1.25f; /* rule 8.5 */
  uint32_t homeTimeoutMs = 20000;  /* rule 8.5, whole HOME */
  long homeBackoffSteps = 100;     /* 0 = single approach, no back-off */
  float homeReleaseMaxRevs = 0.25f; /* max travel to leave an already-active sensor */
  long homeOffsetSteps = 0;        /* slot 0 centre relative to the sensor edge (+ step direction) */
  uint16_t homeDebounceMs = 10;
  bool verifySlotWithHomeSensor = false; /* on arrival the sensor must be active iff slot == 0 */

  uint16_t settleMs = 300;      /* rule 8.4 */
  uint16_t gateTravelMs = 400;  /* rule 8.2, <= 600 */
  uint8_t servoClosedDeg = 20;
  uint8_t servoOpenDeg = 90;
  uint32_t gateMaxOpenMs = 120000; /* rule 8.7 */
  uint16_t debounceMs = 30;        /* rule 8.8, buttons */

  float motionTimeoutFactor = 2.0f;    /* rule 8.6: limit = factor * expected + margin */
  uint32_t motionTimeoutMarginMs = 2000;
  bool holdWhenIdle = true;  /* keep the motor energised in READY / SAFE_STOP */
  bool debugLog = true;      /* "# ..." lines (ignored by the host) */
};

class TactiDoseCore {
 public:
  TactiDoseCore(Hal& hal, const CoreConfig& config);

  /* Arduino setup(): gate closes first, then "EVENT BOOT <fw>", then auto-home (rule 8.9). */
  void begin();
  /* Arduino loop(): call as often as possible. Never blocks. */
  void loop();

  DeviceState state() const { return state_; }
  bool homed() const { return homed_; }
  int slot() const { return slot_; }
  bool gateOpen() const { return gateOpen_; }
  bool gateMoving() const { return gateMoving_; }
  const CoreConfig& config() const { return cfg_; }

  /* Absolute step target of a slot: round(slot * stepsPerRev / numSlots) (protocol §2). */
  long slotTarget(int slot) const;
  /* Nominal duration of a move of `distance` steps (trapezoidal profile). */
  uint32_t expectedMoveMs(long distance, float maxSpeed) const;
  /* Rule 8.6 limit for such a move: factor * expected + margin. */
  uint32_t motionLimitMs(long distance, float maxSpeed) const;

 private:
  enum class Motion : uint8_t { kNone, kMove, kDispense, kHoming };
  enum class HomePhase : uint8_t { kIdle, kRelease, kSeek, kBackoff, kReapproach, kOffset, kDeadReckon };
  enum class GateDone : uint8_t { kNone, kBootClosed, kOpened, kClosedToReady, kClosedForStop };

  struct Debouncer {
    bool stable;
    bool raw;
    uint32_t changedMs;
  };

  void resetState();
  bool updateDebouncer(Debouncer& d, bool raw, uint32_t now);
  void serviceButtons(uint32_t now);
  void serviceSerial(uint32_t now);
  void serviceMotion(uint32_t now);
  void serviceHoming(uint32_t now);
  void serviceTimers(uint32_t now);
  void handleLine(uint32_t now);
  void execute(const ParsedLine& parsed, uint32_t now);
  void handleStop(uint32_t now);
  void onCancelPressed(uint32_t now);

  void bootSequence(uint32_t now);
  void startHoming(uint32_t now);
  void startHomePhase(HomePhase phase, int dir, float speed, long minTravel, long maxTravel, uint32_t now);
  void homeEdgeFound(uint32_t now);
  void finishHoming();
  void startMove(int slot, bool dispense, uint32_t now);
  void beginTimedMove(long target, float speed, uint32_t now);
  void arrive(uint32_t now);
  void startGateTravel(bool open, GateDone done, uint32_t now);
  void finishGateTravel(uint32_t now);

  void interruptMotion();
  void enterReady();
  void enterSafeStop();
  void enterFault(ErrorCode code);
  void applyDriverPower();
  long revsToSteps(float revs) const;

  void sendStatus();
  void emit(const char* line);
  void emitError(ErrorCode code);
  void debug(const char* fmt, ...) TACTIDOSE_PRINTF(2, 3);

  Hal* hal_;
  CoreConfig cfg_;

  DeviceState state_;
  bool homed_;
  int slot_;        /* slot at the gate, -1 = unknown / between slots */
  int targetSlot_;  /* destination of the current move */
  bool gateOpen_;   /* gate open or opening (or not yet known to be closed at boot) */
  bool driverOn_;

  bool gateMoving_;
  GateDone gateDone_;
  uint32_t gateStartMs_;
  uint32_t gateOpenedMs_;
  uint32_t settleStartMs_;

  Motion motion_;
  uint32_t motionStartMs_;
  uint32_t motionLimitMs_;

  HomePhase homePhase_;
  uint32_t homeStartMs_;
  long phaseStartPos_;
  long phaseMinTravel_;
  bool sensorRaw_;
  uint32_t sensorChangedMs_;

  Debouncer confirm_;
  Debouncer cancel_;
  bool confirmPending_;
  bool cancelPending_;

  char line_[kMaxLineLength];
  size_t lineLen_;
  bool lineOverflow_;
};

}  // namespace tactidose

#endif  // TACTIDOSE_CORE_H
