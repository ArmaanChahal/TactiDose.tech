/*
 * FakeHal.h -- simulated dispenser mechanism for the native conformance harness (not used on the ESP32).
 *
 * Physics (tactidose/hardware/conformance.json, "harness"):
 *   - carousel: 3200 steps per revolution, 6 slots; home sensor active while (position mod 3200) is
 *     in [0, 40) -- modes ok | dead | stuck; !reset puts the carousel 1600 steps (180 deg) before the
 *     sensor (homing direction +); jam: commanded steps do not move the carousel and moves never
 *     complete; stepper: AccelStepper-like trapezoidal profile integrated per millisecond;
 *   - per-container mechanism (Physics::perContainer): no carousel, one release servo per container;
 *   - pills: every container starts with 20. When a release servo reaches fully open over a
 *     container (carousel: the compartment aligned with the chute; per-container: its own) with
 *     pills > 0, exactly one pill falls: the count drops by one and, pillFallMs later, the drop
 *     sensor (IR break-beam in the chute) is interrupted for pillPulseMs. Drop sensor modes:
 *     ok | dead (never interrupted) | blocked (always interrupted).
 * MCU-side state (step counter, driver enable, serial buffer) resets on every boot; the physical
 * state (carousel position, gate angles, pills, jam, sensor modes) persists.
 *
 * It is also a physical safety oracle. It counts every violation of the protocol's physical
 * rules, reported by the harness's !physical directive:
 *   step_while_gate_not_closed    carousel moved while a gate was not fully closed (rule 8.1)
 *   gate_opened_while_moving      servo commanded open while the motor was running
 *   gate_opened_between_slots     carousel: servo commanded open with no compartment at the chute
 *   two_gates_open                per-container: a second release opened while another was not closed
 *   bad_gate_index                servo index outside the mechanism's gates
 *   stepper_moved_without_carousel  per-container: the firmware took steps
 *   gate_open_reported_early      OK GATE_OPEN before the servo travel finished (rule 8.2)
 *   gate_closed_reported_early    OK GATE_CLOSED before every gate was fully closed
 *   boot_event_before_gate_closed EVENT BOOT before the gates were closed (rule 8.9)
 *   verdict_before_gate_closed    OK DROPPED / ERR NO_PILL while a gate was not fully closed
 *   dropped_without_pill / dropped_wrong_container / no_pill_but_pill_fell / two_pills_in_one_drop
 *                                 (drop sensor fitted and working) the verdict contradicts the physics
 *   at_slot_while_moving / at_slot_wrong_physical_slot / ready_while_moving / homed_off_slot0
 */
#ifndef TACTIDOSE_FAKE_HAL_H
#define TACTIDOSE_FAKE_HAL_H

#include <stddef.h>
#include <stdint.h>

#include <string>

#include "Hal.h"

namespace harness {

static const int kMaxContainers = 12; /* = tactidose::kMaxSlots */

struct Physics {
  long stepsPerRev = 3200;
  long sensorZoneSteps = 40;
  long initialOffsetSteps = 1600;
  int numSlots = 6;
  int servoClosedDeg = 20;
  int servoOpenDeg = 90;
  uint32_t gateTravelMs = 400;
  long slotToleranceSteps = 20; /* |position - compartment centre| that still counts as aligned */
  bool perContainer = false;    /* fixed containers with one release servo each, no carousel */
  int initialPills = 20;
  uint32_t pillFallMs = 40;     /* gate fully open -> the pill reaches the drop sensor */
  uint32_t pillPulseMs = 4;     /* how long a falling pill interrupts the beam */
};

enum class SensorMode : uint8_t { kOk, kDead, kStuck };
enum class DropSensorMode : uint8_t { kOk, kDead, kBlocked };

class FakeHal : public tactidose::Hal {
 public:
  explicit FakeHal(const Physics& physics);

  /* ---- harness controls ---- */
  void configure(const Physics& physics) { phys_ = physics; } /* parameters only, state is kept */
  void resetAll();                 /* !reset: fresh device at the initial offset, full containers, time 0 */
  /* !boot: MCU reset; physical state persists. The flags tell the oracle what the firmware reads. */
  void powerOn(bool homeSensorFitted, bool dropSensorFitted);
  void advanceOneMs();
  uint64_t simMs() const { return simMs_; }
  void setMillisOffset(uint32_t offset) { millisOffset_ = offset; }
  bool queueRx(const char* data, size_t length); /* false if the RX buffer overflowed */
  void setButton(tactidose::Button button, bool pressed);
  void setSensor(SensorMode mode) { sensor_ = mode; }
  void setDropSensor(DropSensorMode mode) { dropSensor_ = mode; }
  void setJam(bool on) { jam_ = on; }
  void setPills(int container, int count) { pills_[container] = count; }
  void setOutput(std::string* out) { out_ = out; }
  void setMuted(bool muted);
  uint32_t mutedLines() const { return mutedLines_; }
  int physicalSlot() const; /* carousel compartment aligned with the chute, -1 if none */
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
  void servoWrite(uint8_t gate, uint8_t degrees) override;
  bool homeSensorActive() override;
  bool buttonPressed(tactidose::Button button) override;
  bool dropSensorActive() override;
  void buzzerWrite(bool on) override { buzzerOn_ = on; } /* recorded only (conformance uses BUZZER queries) */
  bool buzzerOn() const { return buzzerOn_; }
  int serialRead() override;
  void serialWriteLine(const char* line) override;

 private:
  bool buzzerOn_ = false;
  long long phase() const; /* physical position mod stepsPerRev, in [0, stepsPerRev) */
  bool sensorReads() const;
  bool beamInterrupted() const;
  uint32_t fwMillis() const;
  int gateCount() const { return phys_.perContainer ? phys_.numSlots : 1; }
  bool gateSettled(int g) const { return gateDeg_[g] >= 0 && simMs_ >= gateSettledMs_[g]; }
  bool gateFullyClosed(int g) const { return gateSettled(g) && gateDeg_[g] == phys_.servoClosedDeg; }
  bool gateFullyOpen(int g) const { return gateSettled(g) && gateDeg_[g] == phys_.servoOpenDeg; }
  bool allGatesClosed() const;
  int openGate() const; /* gate commanded to a non-closed angle, -1 if none */
  bool motorMoving() const { return stepPos_ != stepTarget_ || speed_ > 0.0; }
  void takeSteps(long direction, long count);
  void releasePills();
  void checkLine(const char* line);
  void violation(const char* what);

  Physics phys_;

  uint64_t simMs_ = 0;
  uint64_t bootMs_ = 0;
  uint32_t millisOffset_ = 0;

  long long physPos_ = 0;
  SensorMode sensor_ = SensorMode::kOk;
  DropSensorMode dropSensor_ = DropSensorMode::kOk;
  bool jam_ = false;
  bool buttons_[2] = {false, false};
  int gateDeg_[kMaxContainers] = {};          /* last commanded angle (physical, persists); -1 = unknown */
  uint64_t gateSettledMs_[kMaxContainers] = {};
  uint32_t gateOpens_[kMaxContainers] = {};
  bool gateReleased_[kMaxContainers] = {};    /* this opening has already let its pill go */

  int pills_[kMaxContainers] = {};
  uint32_t pillsDropped_ = 0;
  uint32_t dropPulses_ = 0;
  uint64_t beamUntilMs_ = 0;    /* beam interrupted while beamFromMs_ <= t < beamUntilMs_ */
  uint64_t beamFromMs_ = 0;
  int fallsThisDrop_ = 0;       /* pills fallen since the last OK MOVING (one DROP_SLOT) */
  int fallContainer_ = -1;

  bool homeSensorFitted_ = true;
  bool dropSensorFitted_ = true;
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
