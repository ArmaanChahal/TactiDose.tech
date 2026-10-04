/*
 * ArduinoHal.h -- tactidose::Hal for a real ESP32: AccelStepper (STEP/DIR driver or ULN2003 +
 * 28BYJ-48) and ESP32Servo. Pins and polarities come from config.h.
 *
 * REFERENCE FIRMWARE for a hackathon prototype -- adapt config.h to your wiring.
 * NOT a medical device: candy/tokens only.
 *
 * Libraries (Arduino Library Manager): "AccelStepper" (Mike McCauley), "ESP32Servo" (Kevin Harrington).
 * Board: "ESP32 Dev Module" (esp32:esp32:esp32) or your exact board.
 * Compiled only by the Arduino toolchain; the native harness uses firmware/native/FakeHal instead.
 */
#ifndef TACTIDOSE_ARDUINO_HAL_H
#define TACTIDOSE_ARDUINO_HAL_H

#if defined(ARDUINO)

#include <AccelStepper.h>
#include <Arduino.h>
#include <ESP32Servo.h>

#include "Hal.h"
#include "TactiDoseCore.h"

class ArduinoHal : public tactidose::Hal {
 public:
  ArduinoHal();

  /* Call first in setup(): drivers released, gate servo commanded CLOSED, inputs, serial. */
  void begin();
  /* Optional status LED (PIN_STATUS_LED): call from loop(). */
  void showState(tactidose::DeviceState state);

  uint32_t millis() override;
  void stepperSetMaxSpeed(float stepsPerSecond) override;
  void stepperSetAcceleration(float stepsPerSecondSquared) override;
  void stepperMoveTo(long absolutePosition) override;
  bool stepperRun() override;
  long stepperDistanceToGo() override;
  void stepperStop() override;
  long stepperCurrentPosition() override;
  void stepperSetCurrentPosition(long position) override;
  void stepperEnable(bool on) override;
  void servoWrite(uint8_t degrees) override;
  bool homeSensorActive() override;
  bool buttonPressed(tactidose::Button button) override;
  int serialRead() override;
  void serialWriteLine(const char* line) override;

 private:
  AccelStepper stepper_;
  Servo servo_;
  bool ledOn_;
};

#endif  // ARDUINO
#endif  // TACTIDOSE_ARDUINO_HAL_H
