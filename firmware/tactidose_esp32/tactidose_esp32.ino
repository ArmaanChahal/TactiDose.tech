/*
 * tactidose_esp32.ino -- TactiDose REFERENCE firmware for the ESP32 pill dropper (3 containers).
 *
 * Hackathon prototype, NOT a medical device: demo with candy / tokens only.
 * Wire protocol: docs/SERIAL_PROTOCOL.md (v1.1, DROP_SLOT). Mechanism, pins and tunables: config.h
 * (pins are PLACEHOLDERS). Wiring, calibration, flashing and tests: docs/HARDWARE_INTEGRATION.md.
 *
 * The state machine lives in TactiDoseCore.cpp (portable C++, also compiled natively for the
 * conformance harness in firmware/native); this file only wires it to the board.
 * Libraries: AccelStepper, ESP32Servo. Board: "ESP32 Dev Module" (esp32:esp32:esp32).
 */
#include "config.h"
#include "ConfigCheck.h"
#include "ArduinoHal.h"
#include "TactiDoseCore.h"

static ArduinoHal hal;
static tactidose::TactiDoseCore core(hal, tactidose::makeCoreConfig());

void setup() {
  hal.begin();  /* drivers released + release servo(s) commanded CLOSED first, then serial */
  core.begin(); /* waits for the servo travel, sends EVENT BOOT, auto-homes (non-blocking) */
}

void loop() {
  core.loop(); /* never blocks: stepper, release, drop sensor, buttons, serial */
  hal.showState(core.state());
}
