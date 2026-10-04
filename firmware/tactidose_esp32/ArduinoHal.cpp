/*
 * ArduinoHal.cpp -- tactidose::Hal on a real ESP32 (AccelStepper + ESP32Servo).
 *
 * REFERENCE FIRMWARE for a hackathon prototype -- adapt config.h to your wiring.
 * NOT a medical device: candy/tokens only.
 */
#if defined(ARDUINO)

#include "ArduinoHal.h"

#include "config.h"

#ifndef ALLOW_STRAPPING_PINS
/* 1 = permit strapping pins (0, 2, 5, 12, 15) for motor/servo signals. Only if you have no
 * alternative and have checked that the boot-time level/toggling on that pin is harmless. */
#define ALLOW_STRAPPING_PINS 0
#endif

#if MECHANISM == MECHANISM_PER_CONTAINER_SERVO
namespace {
constexpr int kReleasePins[] = {PIN_RELEASE_SERVOS};
constexpr int kReleasePinCount = static_cast<int>(sizeof(kReleasePins) / sizeof(kReleasePins[0]));
constexpr int kServoTrim[] = {SERVO_TRIM_DEG};

/* Logical angle of the core + this container's trim, clamped to the servo range. */
int trimmed(uint8_t gate, uint8_t degrees) {
  const int deg = static_cast<int>(degrees) + kServoTrim[gate];
  return deg < 0 ? 0 : (deg > 180 ? 180 : deg);
}
}  // namespace
#endif

#if HAS_DROP_SENSOR
namespace {
/* Set by the pin interrupt when the beam becomes interrupted; read and cleared by dropSensorActive(). */
volatile bool gDropLatched = false;
void IRAM_ATTR onDropSensorEdge() { gDropLatched = true; }
}  // namespace
#endif

#if BUZZER_PIN >= 0 && BUZZER_TYPE == BUZZER_PASSIVE && !(defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3)
/* Passive buzzer on Arduino-ESP32 2.x: the highest LEDC channel, away from the ones ESP32Servo takes first. */
static const uint8_t kBuzzerLedcChannel = 15;
#endif

/* ------------------------------------------------------------------ compile-time pin checks */

#if defined(CONFIG_IDF_TARGET_ESP32) /* classic ESP32 (ESP32-WROOM/WROVER, "ESP32 Dev Module") */
namespace {
constexpr bool isFlashPin(int p) { return p >= 6 && p <= 11; }
constexpr bool isInputOnly(int p) { return p >= 34 && p <= 39; }
constexpr bool isStrapping(int p) { return p == 0 || p == 2 || p == 5 || p == 12 || p == 15; }
constexpr bool isUart0(int p) { return p == 1 || p == 3; }
constexpr bool okOutput(int p) { return p < 0 || (p <= 39 && !isFlashPin(p) && !isInputOnly(p) && !isUart0(p)); }
constexpr bool okCriticalOutput(int p) {
  return okOutput(p) && (ALLOW_STRAPPING_PINS != 0 || p < 0 || !isStrapping(p));
}
constexpr bool okInput(int p, bool pullup) {
  return p < 0 || (p <= 39 && !isFlashPin(p) && !isUart0(p) && !(pullup && isInputOnly(p)));
}
#if MECHANISM == MECHANISM_PER_CONTAINER_SERVO
/* GPIO 14 is also excluded: it outputs PWM while the ESP32 boots and would twitch a release servo. */
constexpr bool releasePinsOk(int i) {
  return i >= kReleasePinCount ? true
                               : (kReleasePins[i] >= 0 && okCriticalOutput(kReleasePins[i]) && kReleasePins[i] != 14 &&
                                  releasePinsOk(i + 1));
}
#endif

constexpr int kUsedPins[] = {
#if MECHANISM == MECHANISM_CAROUSEL
#if DRIVER_TYPE == DRIVER_STEP_DIR
    PIN_STEP, PIN_DIR, PIN_ENABLE,
#else
    PIN_IN1, PIN_IN2, PIN_IN3, PIN_IN4,
#endif
    PIN_SERVO,
#if HAS_HOME_SENSOR
    PIN_HOME_SENSOR,
#endif
#else
    PIN_RELEASE_SERVOS,
#endif
#if HAS_DROP_SENSOR
    PIN_DROP_SENSOR,
#endif
#if BUZZER_PIN >= 0
    BUZZER_PIN,
#endif
    PIN_CONFIRM_BUTTON, PIN_CANCEL_BUTTON, PIN_STATUS_LED};
constexpr int kUsedPinCount = static_cast<int>(sizeof(kUsedPins) / sizeof(kUsedPins[0]));
constexpr bool pinsDistinct(int i, int j) {
  return i >= kUsedPinCount   ? true
         : j >= kUsedPinCount ? pinsDistinct(i + 1, i + 2)
         : (kUsedPins[i] >= 0 && kUsedPins[i] == kUsedPins[j]) ? false
                                                                : pinsDistinct(i, j + 1);
}
}  // namespace

#if MECHANISM == MECHANISM_CAROUSEL
#if DRIVER_TYPE == DRIVER_STEP_DIR
static_assert(PIN_STEP >= 0 && PIN_DIR >= 0, "PIN_STEP and PIN_DIR are required");
static_assert(okCriticalOutput(PIN_STEP) && okCriticalOutput(PIN_DIR) && okCriticalOutput(PIN_ENABLE),
              "STEP/DIR/ENABLE pin: not 6-11 (flash), 34-39 (input-only), 1/3 (USB serial) or a strapping pin "
              "0/2/5/12/15 (set ALLOW_STRAPPING_PINS 1 to override)");
#else
static_assert(PIN_IN1 >= 0 && PIN_IN2 >= 0 && PIN_IN3 >= 0 && PIN_IN4 >= 0, "PIN_IN1..PIN_IN4 are required");
static_assert(okCriticalOutput(PIN_IN1) && okCriticalOutput(PIN_IN2) && okCriticalOutput(PIN_IN3) &&
                  okCriticalOutput(PIN_IN4),
              "ULN2003 IN1..IN4 pin: not 6-11 (flash), 34-39 (input-only), 1/3 (USB serial) or a strapping pin "
              "0/2/5/12/15 (set ALLOW_STRAPPING_PINS 1 to override)");
#endif
static_assert(PIN_SERVO >= 0 && okCriticalOutput(PIN_SERVO),
              "PIN_SERVO: not 6-11, 34-39, 1/3 or a strapping pin (the gate could twitch open during boot)");
#if HAS_HOME_SENSOR
static_assert(PIN_HOME_SENSOR >= 0 && okInput(PIN_HOME_SENSOR, HOME_SENSOR_PULLUP != 0),
              "PIN_HOME_SENSOR: not 6-11 or 1/3; GPIO 34-39 have no internal pull-up (set HOME_SENSOR_PULLUP 0 "
              "and fit an external 10k pull-up)");
#endif
#else
static_assert(releasePinsOk(0),
              "PIN_RELEASE_SERVOS: every pin must be an output-capable GPIO, not 6-11 (flash), 34-39 (input-only), "
              "1/3 (USB serial), 14 or a strapping pin 0/2/5/12/15 (a release servo could twitch during boot)");
#endif
#if HAS_DROP_SENSOR
static_assert(PIN_DROP_SENSOR >= 0 && okInput(PIN_DROP_SENSOR, DROP_SENSOR_PULLUP != 0),
              "PIN_DROP_SENSOR: not 6-11 or 1/3; GPIO 34-39 have no internal pull-up (set DROP_SENSOR_PULLUP 0 "
              "and fit an external 10k pull-up to 3.3 V)");
#endif
static_assert(okOutput(PIN_STATUS_LED), "PIN_STATUS_LED: not 6-11 (flash), 34-39 (input-only) or 1/3");
static_assert(okOutput(BUZZER_PIN), "BUZZER_PIN: not 6-11 (flash), 34-39 (input-only) or 1/3");
static_assert(PIN_CONFIRM_BUTTON >= 0 && okInput(PIN_CONFIRM_BUTTON, BUTTON_PULLUP != 0),
              "PIN_CONFIRM_BUTTON: required; not 6-11 or 1/3; GPIO 34-39 need BUTTON_PULLUP 0 + external pull-up");
static_assert(okInput(PIN_CANCEL_BUTTON, BUTTON_PULLUP != 0),
              "PIN_CANCEL_BUTTON: not 6-11 or 1/3; GPIO 34-39 need BUTTON_PULLUP 0 + external pull-up");
static_assert(pinsDistinct(0, 1), "two functions share one GPIO in config.h");
#endif  // CONFIG_IDF_TARGET_ESP32 (other variants: re-check the pin plan by hand)

/* ------------------------------------------------------------------ construction */

#if MECHANISM == MECHANISM_PER_CONTAINER_SERVO
ArduinoHal::ArduinoHal() : ledOn_(false) {}
#elif DRIVER_TYPE == DRIVER_STEP_DIR
ArduinoHal::ArduinoHal() : stepper_(AccelStepper::DRIVER, PIN_STEP, PIN_DIR, 0xff, 0xff, false), ledOn_(false) {}
#elif DIR_INVERT
/* Reversed coil order = reversed rotation. */
ArduinoHal::ArduinoHal()
    : stepper_(AccelStepper::HALF4WIRE, PIN_IN4, PIN_IN2, PIN_IN3, PIN_IN1, false), ledOn_(false) {}
#else
/* AccelStepper needs the 28BYJ-48 coils in the order IN1, IN3, IN2, IN4. */
ArduinoHal::ArduinoHal()
    : stepper_(AccelStepper::HALF4WIRE, PIN_IN1, PIN_IN3, PIN_IN2, PIN_IN4, false), ledOn_(false) {}
#endif

void ArduinoHal::begin() {
  /* 1. Motor outputs to a defined, released state before anything else. */
#if MECHANISM == MECHANISM_CAROUSEL
#if DRIVER_TYPE == DRIVER_STEP_DIR
#if PIN_ENABLE >= 0
  /* EN is driven here, not through AccelStepper::setEnablePin(), which briefly writes the
   * *enabled* level. Level first, then output, so the driver never switches on during boot. */
  digitalWrite(PIN_ENABLE, ENABLE_ACTIVE_LOW ? HIGH : LOW);
  pinMode(PIN_ENABLE, OUTPUT);
  digitalWrite(PIN_ENABLE, ENABLE_ACTIVE_LOW ? HIGH : LOW);
#endif
  pinMode(PIN_STEP, OUTPUT);
  digitalWrite(PIN_STEP, LOW);
  pinMode(PIN_DIR, OUTPUT);
  digitalWrite(PIN_DIR, LOW);
  stepper_.setPinsInverted(DIR_INVERT != 0, false, false);
  stepper_.setMinPulseWidth(STEP_PULSE_US);
#else
  const uint8_t coils[] = {PIN_IN1, PIN_IN2, PIN_IN3, PIN_IN4};
  for (uint8_t pin : coils) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }
  stepper_.disableOutputs(); /* all coils off */
#endif
#endif

  /* 2. Release servo(s): command CLOSED right away; the core waits GATE_TRAVEL_MS before EVENT BOOT. */
#if MECHANISM == MECHANISM_CAROUSEL
  ESP32PWM::allocateTimer(0);
  servo_.setPeriodHertz(50);
  servo_.attach(PIN_SERVO, SERVO_MIN_PULSE_US, SERVO_MAX_PULSE_US);
  servo_.write(SERVO_CLOSED_DEG);
#else
  for (int timer = 0; timer < (NUM_SLOTS + 3) / 4; ++timer) {
    ESP32PWM::allocateTimer(timer); /* up to 4 servo channels share one 50 Hz timer */
  }
  for (uint8_t i = 0; i < NUM_SLOTS; ++i) {
    servos_[i].setPeriodHertz(50);
    servos_[i].attach(kReleasePins[i], SERVO_MIN_PULSE_US, SERVO_MAX_PULSE_US);
    servos_[i].write(trimmed(i, SERVO_CLOSED_DEG));
  }
#endif

  /* 3. Inputs. */
#if MECHANISM == MECHANISM_CAROUSEL && HAS_HOME_SENSOR
  pinMode(PIN_HOME_SENSOR, HOME_SENSOR_PULLUP ? INPUT_PULLUP : INPUT);
#endif
#if HAS_DROP_SENSOR
  pinMode(PIN_DROP_SENSOR, DROP_SENSOR_PULLUP ? INPUT_PULLUP : INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_DROP_SENSOR), onDropSensorEdge,
                  DROP_SENSOR_ACTIVE_LOW ? FALLING : RISING);
#endif
  pinMode(PIN_CONFIRM_BUTTON, BUTTON_PULLUP ? INPUT_PULLUP : INPUT);
#if PIN_CANCEL_BUTTON >= 0
  pinMode(PIN_CANCEL_BUTTON, BUTTON_PULLUP ? INPUT_PULLUP : INPUT);
#endif
#if PIN_STATUS_LED >= 0
  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);
#endif
  /* Buzzer (config.h BUZZER block): silent from the start. */
#if BUZZER_PIN >= 0 && BUZZER_TYPE == BUZZER_ACTIVE
  digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_HIGH ? LOW : HIGH);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_HIGH ? LOW : HIGH);
#elif BUZZER_PIN >= 0 && BUZZER_TYPE == BUZZER_PASSIVE
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(BUZZER_PIN, BUZZER_TONE_HZ, 8); /* Arduino-ESP32 3.x: pin-based LEDC API */
  ledcWriteTone(BUZZER_PIN, 0);
#else
  ledcSetup(kBuzzerLedcChannel, BUZZER_TONE_HZ, 8); /* Arduino-ESP32 2.x: channel-based LEDC API */
  ledcAttachPin(BUZZER_PIN, kBuzzerLedcChannel);
  ledcWriteTone(kBuzzerLedcChannel, 0);
#endif
#endif

  /* 4. Host link. A large TX buffer keeps Serial.println from blocking loop() (and the stepper). */
#if !ARDUINO_USB_CDC_ON_BOOT
  Serial.setTxBufferSize(512);
#endif
  Serial.begin(SERIAL_BAUD);
}

void ArduinoHal::showState(tactidose::DeviceState state) {
#if PIN_STATUS_LED >= 0
  const uint32_t now = ::millis();
  bool on;
  switch (state) {
    case tactidose::DeviceState::kReady:
    case tactidose::DeviceState::kGateOpen:
      on = true;
      break;
    case tactidose::DeviceState::kFault:
      on = (now / 125U) % 2U == 0U;
      break;
    case tactidose::DeviceState::kHoming:
    case tactidose::DeviceState::kMoving:
    case tactidose::DeviceState::kAtTarget:
      on = (now / 500U) % 2U == 0U;
      break;
    default: /* BOOT, SAFE_STOP: short blip once a second */
      on = (now % 1000U) < 100U;
      break;
  }
  if (on != ledOn_) {
    ledOn_ = on;
    digitalWrite(PIN_STATUS_LED, on ? HIGH : LOW);
  }
#else
  (void)state;
#endif
}

/* ------------------------------------------------------------------ tactidose::Hal */

uint32_t ArduinoHal::millis() { return static_cast<uint32_t>(::millis()); }

#if MECHANISM == MECHANISM_CAROUSEL

void ArduinoHal::stepperSetMaxSpeed(float stepsPerSecond) { stepper_.setMaxSpeed(stepsPerSecond); }

void ArduinoHal::stepperSetAcceleration(float stepsPerSecondSquared) {
  stepper_.setAcceleration(stepsPerSecondSquared);
}

void ArduinoHal::stepperMoveTo(long absolutePosition) { stepper_.moveTo(absolutePosition); }

bool ArduinoHal::stepperRun() { return stepper_.run(); }

long ArduinoHal::stepperDistanceToGo() { return stepper_.distanceToGo(); }

/* AccelStepper::stop() decelerates; an emergency stop must not take more steps. */
void ArduinoHal::stepperStop() { stepper_.setCurrentPosition(stepper_.currentPosition()); }

long ArduinoHal::stepperCurrentPosition() { return stepper_.currentPosition(); }

void ArduinoHal::stepperSetCurrentPosition(long position) { stepper_.setCurrentPosition(position); }

void ArduinoHal::stepperEnable(bool on) {
#if DRIVER_TYPE == DRIVER_STEP_DIR
#if PIN_ENABLE >= 0
  digitalWrite(PIN_ENABLE, on == (ENABLE_ACTIVE_LOW == 0) ? HIGH : LOW);
#endif
  if (on) stepper_.enableOutputs(); /* (re)configures STEP/DIR as outputs */
#else
  /* ULN2003: releasing = all four coils off. Energising takes effect with the next step. */
  if (on) {
    stepper_.enableOutputs();
  } else {
    stepper_.disableOutputs();
  }
#endif
}

void ArduinoHal::servoWrite(uint8_t gate, uint8_t degrees) {
  (void)gate; /* one release servo: gate 0 */
  servo_.write(degrees);
}

bool ArduinoHal::homeSensorActive() {
#if HAS_HOME_SENSOR
  const int level = digitalRead(PIN_HOME_SENSOR);
  return HOME_SENSOR_ACTIVE_LOW ? level == LOW : level == HIGH;
#else
  return false;
#endif
}

#else  // MECHANISM_PER_CONTAINER_SERVO: no stepper, no home sensor (the core never uses them)

void ArduinoHal::stepperSetMaxSpeed(float) {}
void ArduinoHal::stepperSetAcceleration(float) {}
void ArduinoHal::stepperMoveTo(long) {}
bool ArduinoHal::stepperRun() { return false; }
long ArduinoHal::stepperDistanceToGo() { return 0; }
void ArduinoHal::stepperStop() {}
long ArduinoHal::stepperCurrentPosition() { return 0; }
void ArduinoHal::stepperSetCurrentPosition(long) {}
void ArduinoHal::stepperEnable(bool) {}

void ArduinoHal::servoWrite(uint8_t gate, uint8_t degrees) {
  if (gate < NUM_SLOTS) servos_[gate].write(trimmed(gate, degrees));
}

bool ArduinoHal::homeSensorActive() { return false; }

#endif  // MECHANISM

bool ArduinoHal::buttonPressed(tactidose::Button button) {
  const int pin = button == tactidose::Button::kConfirm ? PIN_CONFIRM_BUTTON : PIN_CANCEL_BUTTON;
  if (pin < 0) return false;
  const int level = digitalRead(pin);
  return BUTTON_ACTIVE_LOW ? level == LOW : level == HIGH;
}

bool ArduinoHal::dropSensorActive() {
#if HAS_DROP_SENSOR
  const int level = digitalRead(PIN_DROP_SENSOR);
  if (gDropLatched) {
    gDropLatched = false; /* interrupted at least once since the previous call */
    return true;
  }
  return DROP_SENSOR_ACTIVE_LOW ? level == LOW : level == HIGH;
#else
  return false;
#endif
}

int ArduinoHal::serialRead() { return Serial.read(); /* -1 when nothing is waiting */ }

void ArduinoHal::serialWriteLine(const char* line) { Serial.println(line); }

void ArduinoHal::buzzerWrite(bool on) {
#if BUZZER_PIN >= 0 && BUZZER_TYPE == BUZZER_ACTIVE
  digitalWrite(BUZZER_PIN, (on == (BUZZER_ACTIVE_HIGH != 0)) ? HIGH : LOW);
#elif BUZZER_PIN >= 0 && BUZZER_TYPE == BUZZER_PASSIVE
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWriteTone(BUZZER_PIN, on ? BUZZER_TONE_HZ : 0);
#else
  ledcWriteTone(kBuzzerLedcChannel, on ? BUZZER_TONE_HZ : 0);
#endif
#else
  (void)on; /* BUZZER_PIN -1 (TODO): no buzzer fitted; the core answers ERR NO_BUZZER anyway */
#endif
}

#endif  // ARDUINO
