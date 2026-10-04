/*
 * ConfigCheck.h -- turns config.h into a tactidose::CoreConfig and rejects unsafe tunables at
 * compile time. Include after config.h. Pure C++11: firmware/native/build.sh compiles it natively
 * too (both mechanisms, both driver types), so a bad edit shows up without the ESP32 toolchain.
 *
 * Pin checks need the ESP32 target macros and live in ArduinoHal.cpp.
 */
#ifndef TACTIDOSE_CONFIG_CHECK_H
#define TACTIDOSE_CONFIG_CHECK_H

#include "TactiDoseCore.h"

namespace tactidose {
namespace config_check {

constexpr bool hasNoSpace(const char* s) { return *s == '\0' ? true : (*s != ' ' && hasNoSpace(s + 1)); }

/* Upper bound of a move's duration in seconds (the trapezoid formula is >= the triangle one). */
constexpr float moveSecondsUpperBound(float steps, float speed, float accel) {
  return steps / speed + speed / accel;
}

constexpr bool kCarousel = MECHANISM == MECHANISM_CAROUSEL;

constexpr float kStepsPerSlot = CAROUSEL_STEPS_PER_REV / static_cast<float>(NUM_SLOTS);
/* Targets are absolute and never wrap, so the longest move is slot 0 <-> slot N-1. */
constexpr float kWorstMoveSteps = kStepsPerSlot * static_cast<float>(NUM_SLOTS - 1);
constexpr float kWorstMoveLimitMs =
    MOTION_TIMEOUT_FACTOR * moveSecondsUpperBound(kWorstMoveSteps, MAX_SPEED_SPS, ACCELERATION_SPS2) * 1000.0f +
    static_cast<float>(MOTION_TIMEOUT_MARGIN_MS);
constexpr float kHomeMaxTravelMs = HOME_MAX_TRAVEL_REVS * CAROUSEL_STEPS_PER_REV / HOMING_SPEED_SPS * 1000.0f;

/* DROP_SLOT (§12.1): open, hold, close. The longest DROP_SLOT adds the worst move (carousel) and the settle. */
constexpr float kReleaseMs = 2.0f * static_cast<float>(GATE_TRAVEL_MS) + static_cast<float>(DROP_OPEN_MS);
constexpr float kWorstDropMs = (kCarousel ? kWorstMoveLimitMs : 0.0f) + static_cast<float>(SETTLE_MS) + kReleaseMs;

/* MECHANISM_PER_CONTAINER_SERVO lists (config.h keeps them defined for both mechanisms). */
constexpr int kReleasePins[] = {PIN_RELEASE_SERVOS};
constexpr int kReleasePinCount = static_cast<int>(sizeof(kReleasePins) / sizeof(kReleasePins[0]));
constexpr int kServoTrim[] = {SERVO_TRIM_DEG};
constexpr int kServoTrimCount = static_cast<int>(sizeof(kServoTrim) / sizeof(kServoTrim[0]));
constexpr bool angleOk(int degrees) { return degrees >= 0 && degrees <= 180; }
constexpr bool trimsOk(int i) {
  return i >= kServoTrimCount ? true
                              : (kServoTrim[i] >= -45 && kServoTrim[i] <= 45 &&
                                 angleOk(SERVO_CLOSED_DEG + kServoTrim[i]) && angleOk(SERVO_OPEN_DEG + kServoTrim[i]) &&
                                 trimsOk(i + 1));
}

}  // namespace config_check
}  // namespace tactidose

static_assert(MECHANISM == MECHANISM_CAROUSEL || MECHANISM == MECHANISM_PER_CONTAINER_SERVO,
              "MECHANISM must be MECHANISM_CAROUSEL or MECHANISM_PER_CONTAINER_SERVO");
static_assert(NUM_SLOTS >= 2 && NUM_SLOTS <= 12, "NUM_SLOTS must be 2..12 (SERIAL_PROTOCOL.md section 2)");
static_assert(tactidose::config_check::hasNoSpace(FW_VERSION) && sizeof(FW_VERSION) > 1 && sizeof(FW_VERSION) <= 32,
              "FW_VERSION must be 1..31 characters without spaces (EVENT BOOT <fw>)");

/* ---- carousel: stepper, homing, motion timeouts ---- */
static_assert(DRIVER_TYPE == DRIVER_STEP_DIR || DRIVER_TYPE == DRIVER_ULN2003,
              "DRIVER_TYPE must be DRIVER_STEP_DIR or DRIVER_ULN2003");
static_assert(!tactidose::config_check::kCarousel || tactidose::config_check::kStepsPerSlot >= 20.0f,
              "fewer than 20 steps per container: check MICROSTEPS, GEAR_RATIO and NUM_SLOTS");
static_assert(!tactidose::config_check::kCarousel || (MAX_SPEED_SPS > 0.0f && ACCELERATION_SPS2 > 0.0f),
              "speeds and acceleration must be positive");
static_assert(!tactidose::config_check::kCarousel || (HOMING_SPEED_SPS > 0.0f && HOMING_SPEED_SPS <= MAX_SPEED_SPS),
              "HOMING_SPEED_SPS must be positive and not above MAX_SPEED_SPS");
static_assert(!tactidose::config_check::kCarousel ||
                  (HOMING_SLOW_SPEED_SPS > 0.0f && HOMING_SLOW_SPEED_SPS <= HOMING_SPEED_SPS),
              "HOMING_SLOW_SPEED_SPS must be positive and not above HOMING_SPEED_SPS");
static_assert(HOMING_DIR == 1 || HOMING_DIR == -1, "HOMING_DIR must be +1 or -1");
static_assert(!tactidose::config_check::kCarousel || (HOME_MAX_TRAVEL_REVS >= 1.0f && HOME_MAX_TRAVEL_REVS <= 2.0f),
              "HOME_MAX_TRAVEL_REVS must be 1..2 revolutions (protocol: 1.25)");
static_assert(!tactidose::config_check::kCarousel ||
                  tactidose::config_check::kHomeMaxTravelMs < static_cast<float>(HOME_TIMEOUT_MS),
              "HOME_MAX_TRAVEL_REVS at HOMING_SPEED_SPS takes longer than HOME_TIMEOUT_MS: homing could give up "
              "before one full turn. Raise HOMING_SPEED_SPS or HOME_TIMEOUT_MS");
static_assert(HOME_TIMEOUT_MS + 5000UL <= HOST_HOME_TIMEOUT_MS,
              "HOME_TIMEOUT_MS must end >= 5 s before the host HOME timeout (HOST_HOME_TIMEOUT_MS)");
static_assert(!tactidose::config_check::kCarousel ||
                  (HOME_BACKOFF_STEPS >= 0 &&
                   static_cast<float>(HOME_BACKOFF_STEPS) < HOME_RELEASE_MAX_REVS * CAROUSEL_STEPS_PER_REV),
              "HOME_BACKOFF_STEPS must be >= 0 and smaller than HOME_RELEASE_MAX_REVS of travel");
static_assert(HOME_RELEASE_MAX_REVS > 0.0f && HOME_RELEASE_MAX_REVS <= 0.5f, "HOME_RELEASE_MAX_REVS must be (0, 0.5]");
static_assert(HOME_DEBOUNCE_MS >= 0 && HOME_DEBOUNCE_MS <= 100, "HOME_DEBOUNCE_MS must be 0..100");
static_assert(!tactidose::config_check::kCarousel ||
                  tactidose::config_check::kWorstMoveLimitMs < static_cast<float>(HOST_MOVE_TIMEOUT_MS),
              "the motion timeout of the longest move (slot 0 <-> N-1) exceeds the host MOVE_SLOT timeout: the "
              "host would see an uncertain TIMEOUT instead of ERR MOTOR_FAULT. Raise MAX_SPEED_SPS");
static_assert(MOTION_TIMEOUT_FACTOR >= 1.5f, "MOTION_TIMEOUT_FACTOR below 1.5 risks false MOTOR_FAULTs");

/* ---- release (gate) servo(s) and DROP_SLOT ---- */
static_assert(GATE_TRAVEL_MS > 0 && GATE_TRAVEL_MS <= 600, "GATE_TRAVEL_MS must be 1..600 ms (rule 8.2)");
static_assert(SETTLE_MS >= 0 && SETTLE_MS <= 2000, "SETTLE_MS must be 0..2000 ms (rule 8.4: ~300)");
static_assert(DROP_OPEN_MS >= 50 && DROP_OPEN_MS <= 1000, "DROP_OPEN_MS must be 50..1000 ms");
static_assert(tactidose::config_check::kReleaseMs <= 2000.0f,
              "the release (2 x GATE_TRAVEL_MS + DROP_OPEN_MS) must stay <= 2 s (protocol 12.1: about 1.5 s); "
              "serial input, including STOP, waits for it");
static_assert(tactidose::config_check::kWorstDropMs + 5000.0f <= static_cast<float>(HOST_DROP_TIMEOUT_MS),
              "the longest DROP_SLOT (move limit + SETTLE_MS + release) must end >= 5 s before the host DROP_SLOT "
              "timeout (HOST_DROP_TIMEOUT_MS)");
static_assert(GATE_MAX_OPEN_MS >= 10000UL && GATE_MAX_OPEN_MS <= 600000UL,
              "GATE_MAX_OPEN_MS must be 10 s..10 min (rule 8.7: default 120 s)");
static_assert(DEBOUNCE_MS >= 30 && DEBOUNCE_MS <= 200, "buttons must be debounced 30..200 ms (rule 8.8)");
static_assert(SERVO_CLOSED_DEG >= 0 && SERVO_CLOSED_DEG <= 180 && SERVO_OPEN_DEG >= 0 && SERVO_OPEN_DEG <= 180,
              "servo angles must be 0..180");
static_assert(SERVO_CLOSED_DEG != SERVO_OPEN_DEG, "SERVO_CLOSED_DEG and SERVO_OPEN_DEG must differ");
static_assert(SERVO_MIN_PULSE_US >= 400 && SERVO_MAX_PULSE_US <= 2600 && SERVO_MIN_PULSE_US < SERVO_MAX_PULSE_US,
              "servo pulse range must lie within 400..2600 us");
static_assert(tactidose::config_check::kCarousel || tactidose::config_check::kReleasePinCount == NUM_SLOTS,
              "PIN_RELEASE_SERVOS must list exactly NUM_SLOTS GPIOs (one servo per container)");
static_assert(tactidose::config_check::kCarousel || tactidose::config_check::kServoTrimCount == NUM_SLOTS,
              "SERVO_TRIM_DEG must have exactly NUM_SLOTS entries");
static_assert(tactidose::config_check::kCarousel || tactidose::config_check::trimsOk(0),
              "SERVO_TRIM_DEG entries must be -45..45 and keep SERVO_CLOSED_DEG / SERVO_OPEN_DEG within 0..180");

/* ---- drop sensor ---- */
static_assert(HAS_DROP_SENSOR == 0 || HAS_DROP_SENSOR == 1, "HAS_DROP_SENSOR must be 0 or 1");
static_assert(DROP_SENSOR_ACTIVE_LOW == 0 || DROP_SENSOR_ACTIVE_LOW == 1, "DROP_SENSOR_ACTIVE_LOW must be 0 or 1");

namespace tactidose {

/* The core configuration described by config.h. */
inline CoreConfig makeCoreConfig() {
  CoreConfig c;
  c.fwVersion = FW_VERSION;
  c.mechanism = MECHANISM == MECHANISM_PER_CONTAINER_SERVO ? Mechanism::kPerContainerServo : Mechanism::kCarousel;
  c.numSlots = NUM_SLOTS;
  c.stepsPerRev = CAROUSEL_STEPS_PER_REV;
  c.maxSpeed = MAX_SPEED_SPS;
  c.acceleration = ACCELERATION_SPS2;
  c.hasHomeSensor = MECHANISM == MECHANISM_CAROUSEL && HAS_HOME_SENSOR != 0;
  c.autoHomeOnBoot = AUTO_HOME_ON_BOOT != 0;
  c.homingDir = HOMING_DIR;
  c.homingSpeed = HOMING_SPEED_SPS;
  c.homingSlowSpeed = HOMING_SLOW_SPEED_SPS;
  c.homeMaxTravelRevs = HOME_MAX_TRAVEL_REVS;
  c.homeTimeoutMs = HOME_TIMEOUT_MS;
  c.homeBackoffSteps = HOME_BACKOFF_STEPS;
  c.homeReleaseMaxRevs = HOME_RELEASE_MAX_REVS;
  c.homeOffsetSteps = HOME_OFFSET_STEPS;
  c.homeDebounceMs = HOME_DEBOUNCE_MS;
  c.verifySlotWithHomeSensor = VERIFY_SLOT_WITH_HOME_SENSOR != 0;
  c.settleMs = SETTLE_MS;
  c.gateTravelMs = GATE_TRAVEL_MS;
  c.servoClosedDeg = SERVO_CLOSED_DEG;
  c.servoOpenDeg = SERVO_OPEN_DEG;
  c.gateMaxOpenMs = GATE_MAX_OPEN_MS;
  c.dropOpenMs = DROP_OPEN_MS;
  c.hasDropSensor = HAS_DROP_SENSOR != 0;
  c.debounceMs = DEBOUNCE_MS;
  c.motionTimeoutFactor = MOTION_TIMEOUT_FACTOR;
  c.motionTimeoutMarginMs = MOTION_TIMEOUT_MARGIN_MS;
  c.holdWhenIdle = STEPPER_HOLD_WHEN_IDLE != 0;
  c.debugLog = DEBUG_LOG != 0;
  return c;
}

}  // namespace tactidose

#endif  // TACTIDOSE_CONFIG_CHECK_H
