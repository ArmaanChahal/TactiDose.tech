/*
 * Hal.h -- hardware abstraction used by the TactiDose firmware core.
 *
 * REFERENCE FIRMWARE for a hackathon prototype. NOT a medical device: demo with candy/tokens only.
 *
 * The core (TactiDoseCore) never touches a GPIO. It talks to this interface, which has two
 * implementations:
 *   - ArduinoHal (ArduinoHal.h/.cpp): AccelStepper + ESP32Servo on a real ESP32.
 *   - FakeHal    (firmware/native/): simulated carousel physics for the conformance harness.
 *
 * Conventions
 *   - Positions are motor (micro)steps, absolute, AccelStepper semantics.
 *   - Inputs are LOGICAL: homeSensorActive() is true while the sensor detects home and
 *     buttonPressed() is true while the button is held. Electrical polarity (active-low,
 *     pull-ups) is handled inside the HAL; debouncing is done by the core.
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

  /* ---- access-gate servo ---- */
  virtual void servoWrite(uint8_t degrees) = 0;

  /* ---- inputs (logical level, not debounced) ---- */
  virtual bool homeSensorActive() = 0;
  virtual bool buttonPressed(Button button) = 0;

  /* ---- serial link to the host ---- */
  /* Next received byte, or -1 if none is waiting. */
  virtual int serialRead() = 0;
  /* Write one line; the HAL appends the "\r\n" terminator. */
  virtual void serialWriteLine(const char* line) = 0;
};

}  // namespace tactidose

#endif  // TACTIDOSE_HAL_H
