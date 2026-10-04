/*
 * Hal.h -- hardware abstraction used by the TactiDose firmware core.
 *
 * REFERENCE FIRMWARE for a hackathon prototype. NOT a medical device: demo with candy/tokens only.
 *
 * The core (TactiDoseCore) never touches a GPIO. It talks to this interface, which has two
 * implementations:
 *   - ArduinoHal (ArduinoHal.h/.cpp): AccelStepper + ESP32Servo on a real ESP32.
 *   - FakeHal    (firmware/native/): simulated mechanism physics for the conformance harness.
 *
 * Conventions
 *   - Positions are motor (micro)steps, absolute, AccelStepper semantics. Builds without a stepper
 *     (MECHANISM_PER_CONTAINER_SERVO) implement the stepper calls as no-ops; the core never moves
 *     the stepper in that mechanism.
 *   - Release actuators ("gates") are numbered. MECHANISM_CAROUSEL has one gate servo (0) at the
 *     chute; MECHANISM_PER_CONTAINER_SERVO has one servo per container (gate n = container n = slot n).
 *   - Inputs are LOGICAL: homeSensorActive() is true while the sensor detects home,
 *     buttonPressed() while the button is held, dropSensorActive() while the beam is interrupted.
 *     Electrical polarity (active-low, pull-ups) is handled inside the HAL; debouncing is done by
 *     the core.
 *   - No call may block. The core calls stepperRun() on every loop() pass.
 */
#ifndef TACTIDOSE_HAL_H
#define TACTIDOSE_HAL_H

#include <stdint.h>

namespace tactidose {

enum class Button : uint8_t { kConfirm = 0, kCancel = 1 };

class Hal {
 public:
  virtual ~Hal() {}

  /* Milliseconds since boot. Wraps after ~49.7 days; the core only uses wrap-safe differences. */
  virtual uint32_t millis() = 0;

  /* ---- stepper (AccelStepper-like) ---- */
  virtual void stepperSetMaxSpeed(float stepsPerSecond) = 0;
  virtual void stepperSetAcceleration(float stepsPerSecondSquared) = 0;
  virtual void stepperMoveTo(long absolutePosition) = 0;
  /* Take the step(s) that are due now. Returns true while the target has not been reached. */
  virtual bool stepperRun() = 0;
  virtual long stepperDistanceToGo() = 0;
  /* Halt IMMEDIATELY (no deceleration ramp): no further steps, target := current position. */
  virtual void stepperStop() = 0;
  virtual long stepperCurrentPosition() = 0;
  /* Redefine the current position; also zeroes the speed and sets target := position. */
  virtual void stepperSetCurrentPosition(long position) = 0;
  /* Energise (true) or release (false) the motor driver / coils. */
  virtual void stepperEnable(bool on) = 0;

  /* ---- release actuators ("gates": access gate / trapdoor / dispensing-wheel servos) ---- */
  /* Command gate `gate` (numbering above) to `degrees`. */
  virtual void servoWrite(uint8_t gate, uint8_t degrees) = 0;

  /* ---- inputs (logical level, not debounced) ---- */
  virtual bool homeSensorActive() = 0;
  virtual bool buttonPressed(Button button) = 0;
  /* Drop sensor (optional IR break-beam across the output chute): true while the beam is
   * interrupted, and also once for an interruption that started and ended since the previous call
   * (a falling pill breaks the beam for a few ms only; ArduinoHal latches it in an interrupt).
   * The core samples it only during a DROP_SLOT release and only if CoreConfig::hasDropSensor. */
  virtual bool dropSensorActive() = 0;

  /* ---- buzzer (optional BUZZER extension, SERIAL_PROTOCOL.md section 13) ---- */
  /* Sound (true) or silence (false) the buzzer. Must return at once (no delay, no tone loop):
   * the core calls it from loop() and from command handling. Default: no buzzer fitted. */
  virtual void buzzerWrite(bool on) { (void)on; }

  /* ---- serial link to the host ---- */
  /* Next received byte, or -1 if none is waiting. */
  virtual int serialRead() = 0;
  /* Write one line; the HAL appends the "\r\n" terminator. */
  virtual void serialWriteLine(const char* line) = 0;
};

}  // namespace tactidose

#endif  // TACTIDOSE_HAL_H
