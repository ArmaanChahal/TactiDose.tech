/*
 * FakeHal.cpp -- simulated carousel physics + physical safety oracle for the native harness.
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
  jam_ = false;
  buttons_[0] = buttons_[1] = false;
  gateDeg_ = -1;
  gateSettledMs_ = 0;
  gateOpens_ = 0;
  violations_ = 0;
  lastViolation_[0] = '\0';
  powerOn(true);
}

void FakeHal::powerOn(bool sensorFitted) {
  bootMs_ = simMs_;
  sensorFitted_ = sensorFitted;
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

void FakeHal::violation(const char* what) {
  ++violations_;
  snprintf(lastViolation_, sizeof(lastViolation_), "%s@%llu", what, static_cast<unsigned long long>(simMs_));
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
  if (!gateFullyClosed()) violation("step_while_gate_not_closed");
  stepPos_ += direction * count;
  physPos_ += static_cast<long long>(direction * count);
}

/* ------------------------------------------------------------------ Hal: gate, inputs */

void FakeHal::servoWrite(uint8_t degrees) {
  const int deg = degrees;
  if (deg != phys_.servoClosedDeg) {
    if (motorMoving()) violation("gate_opened_while_moving");
    if (physicalSlot() < 0) violation("gate_opened_between_slots");
  }
  if (deg != gateDeg_) {
    if (deg == phys_.servoOpenDeg) ++gateOpens_;
    gateDeg_ = deg;
    gateSettledMs_ = simMs_ + phys_.gateTravelMs;
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

bool FakeHal::homeSensorActive() { return sensorReads(); }

bool FakeHal::buttonPressed(tactidose::Button button) { return buttons_[static_cast<int>(button)]; }

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
    if (!gateFullyClosed()) violation("boot_event_before_gate_closed");
  } else if (strcmp(line, "OK GATE_OPEN") == 0) {
    if (!gateFullyOpen()) violation("gate_open_reported_early");
  } else if (strcmp(line, "OK GATE_CLOSED") == 0) {
    if (!gateFullyClosed()) violation("gate_closed_reported_early");
  } else if (startsWith(line, "OK AT_SLOT ")) {
    if (motorMoving()) violation("at_slot_while_moving");
    /* Without a sensor the firmware trusts a hand alignment the simulation does not model. */
    if (sensorFitted_ && physicalSlot() != atoi(line + 11)) violation("at_slot_wrong_physical_slot");
  } else if (strcmp(line, "OK READY") == 0) {
    if (motorMoving()) violation("ready_while_moving");
  } else if (strcmp(line, "OK HOMED") == 0) {
    if (sensorFitted_ && physicalSlot() != 0) violation("homed_off_slot0");
  }
}

void FakeHal::describe(std::string& out) const {
  const char* gate = "unknown";
  if (gateDeg_ >= 0) {
    if (!gateSettled()) {
      gate = "moving";
    } else {
      gate = gateDeg_ == phys_.servoClosedDeg ? "closed" : "open";
    }
  }
  const char* sensorMode = sensor_ == SensorMode::kOk ? "ok" : (sensor_ == SensorMode::kDead ? "dead" : "stuck");
  char buf[512];
  snprintf(buf, sizeof(buf),
           "t=%llu millis=%lu pos=%lld phase=%lld phys_slot=%d sensor=%d sensor_mode=%s sensor_fitted=%d jam=%d "
           "gate=%s gate_deg=%d gate_opens=%lu driver=%d step_pos=%ld step_target=%ld speed=%ld rx_pending=%lu "
           "violations=%lu last_violation=%s",
           static_cast<unsigned long long>(simMs_), static_cast<unsigned long>(fwMillis()),
           physPos_, phase(), physicalSlot(), sensorReads() ? 1 : 0, sensorMode, sensorFitted_ ? 1 : 0, jam_ ? 1 : 0,
           gate, gateDeg_, static_cast<unsigned long>(gateOpens_), enabled_ ? 1 : 0, stepPos_, stepTarget_,
           static_cast<long>(speed_), static_cast<unsigned long>(rxCount_), static_cast<unsigned long>(violations_),
           lastViolation_[0] != '\0' ? lastViolation_ : "none");
  out.append(buf);
}

}  // namespace harness
