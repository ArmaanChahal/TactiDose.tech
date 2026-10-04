/*
 * FakeHal.h -- simulated carousel for the native conformance harness (not used on the ESP32).
 *
 * Physics (tactidose/hardware/conformance.json, "harness"):
 *   - 3200 steps per carousel revolution, 6 slots;
 *   - home sensor active while (position mod 3200) is in [0, 40) -- modes ok | dead | stuck;
 *   - !reset puts the carousel 1600 steps (180 deg) before the sensor (homing direction +);
 *   - jam: commanded steps do not move the carousel and moves never complete;
 *   - stepper: AccelStepper-like trapezoidal profile integrated per millisecond.
 * MCU-side state (step counter, driver enable, serial buffer) resets on every boot; the physical
 * state (carousel position, gate angle, jam, sensor mode) persists.
 *
 * It is also a physical safety oracle. It counts every violation of the protocol's physical
 * rules, reported by the harness's !physical directive:
 *   step_while_gate_not_closed    carousel moved while the gate was not fully closed (rule 8.1)
 *   gate_opened_while_moving      servo commanded open while the motor was running
 *   gate_opened_between_slots     servo commanded open with no compartment at the opening
 *   gate_open_reported_early      OK GATE_OPEN before the servo travel finished (rule 8.2)
 *   gate_closed_reported_early    OK GATE_CLOSED before the servo travel finished
 *   boot_event_before_gate_closed EVENT BOOT before the gate was closed (rule 8.9)
 *   at_slot_while_moving / at_slot_wrong_physical_slot / ready_while_moving / homed_off_slot0
 */
#ifndef TACTIDOSE_FAKE_HAL_H
#define TACTIDOSE_FAKE_HAL_H

#include <stddef.h>
#include <stdint.h>

#include <string>

#include "Hal.h"

namespace harness {

struct Physics {
  long stepsPerRev = 3200;
  long sensorZoneSteps = 40;
  long initialOffsetSteps = 1600;
  int numSlots = 6;
  int servoClosedDeg = 20;
  int servoOpenDeg = 90;
  uint32_t gateTravelMs = 400;
  long slotToleranceSteps = 20; /* |position - compartment centre| that still counts as aligned */
};

enum class SensorMode : uint8_t { kOk, kDead, kStuck };

class FakeHal : public tactidose::Hal {
 public:
  explicit FakeHal(const Physics& physics);

  /* ---- harness controls ---- */
  void resetAll();                 /* !reset: fresh device at the initial offset, time 0 */
  void powerOn(bool sensorFitted); /* !boot: MCU reset; physical state persists */
  void advanceOneMs() { ++simMs_; }
  uint64_t simMs() const { return simMs_; }
  void setMillisOffset(uint32_t offset) { millisOffset_ = offset; }
  bool queueRx(const char* data, size_t length); /* false if the RX buffer overflowed */
  void setButton(tactidose::Button button, bool pressed);
  void setSensor(SensorMode mode) { sensor_ = mode; }
  void setJam(bool on) { jam_ = on; }
  void setOutput(std::string* out) { out_ = out; }
  void setMuted(bool muted);
  uint32_t mutedLines() const { return mutedLines_; }
  int physicalSlot() const; /* compartment aligned with the opening, -1 if none */
  void describe(std::string& out) const;

  /* ---- tactidose::Hal ---- */
  uint32_t millis() override;
  void stepperSetMaxSpeed(float stepsPerSecond) override;
  void stepperSetAcceleration(float stepsPerSecondSquared) override;
  void stepperMoveTo(long absolutePosition) override;
  bool stepperRun() override;
  long stepperDistanceToGo() override { return stepTarget_ - stepPos_; }
  void stepperStop() override;
  long stepperCurrentPosition() override { return stepPos_; }
  void stepperSetCurrentPosition(long position) override;
  void stepperEnable(bool on) override { enabled_ = on; }
  void servoWrite(uint8_t degrees) override;
  bool homeSensorActive() override;
  bool buttonPressed(tactidose::Button button) override;
  int serialRead() override;
  void serialWriteLine(const char* line) override;

 private:
  long long phase() const; /* physical position mod stepsPerRev, in [0, stepsPerRev) */
  bool sensorReads() const;
  uint32_t fwMillis() const;
  bool gateSettled() const { return gateDeg_ >= 0 && simMs_ >= gateSettledMs_; }
  bool gateFullyClosed() const { return gateSettled() && gateDeg_ == phys_.servoClosedDeg; }
  bool gateFullyOpen() const { return gateSettled() && gateDeg_ == phys_.servoOpenDeg; }
  bool motorMoving() const { return stepPos_ != stepTarget_ || speed_ > 0.0; }
  void takeSteps(long direction, long count);
  void checkLine(const char* line);
  void violation(const char* what);

  Physics phys_;

  uint64_t simMs_ = 0;
  uint64_t bootMs_ = 0;
  uint32_t millisOffset_ = 0;

  long long physPos_ = 0;
  SensorMode sensor_ = SensorMode::kOk;
  bool jam_ = false;
  bool buttons_[2] = {false, false};
  int gateDeg_ = -1; /* last commanded angle (physical, persists); -1 = unknown */
  uint64_t gateSettledMs_ = 0;
  uint32_t gateOpens_ = 0;

  bool sensorFitted_ = true;
  long stepPos_ = 0;
  long stepTarget_ = 0;
  double speed_ = 0.0;
  double maxSpeed_ = 1.0;
  double accel_ = 1.0;
  double frac_ = 0.0;
  bool enabled_ = false;
  uint64_t lastRunMs_ = 0;

  static const size_t kRxSize = 4096;
  unsigned char rx_[kRxSize] = {};
  size_t rxHead_ = 0;
  size_t rxCount_ = 0;

  std::string* out_ = nullptr;
  bool muted_ = false;
  uint32_t mutedLines_ = 0;

  uint32_t violations_ = 0;
  char lastViolation_[64] = {};
};

}  // namespace harness

#endif  // TACTIDOSE_FAKE_HAL_H
