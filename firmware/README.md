# TactiDose firmware (REFERENCE implementation, protocol v1.1)

Reference ESP32 firmware for the TactiDose pill dropper (3 containers, `DROP_SLOT`), plus a native
test harness that runs the same state machine on a simulated dispenser. **Hackathon prototype, not
a medical device**: demo with candy/tokens only. The hardware teammate adapts
`tactidose_esp32/config.h` (mechanism, pins, tunables). Wiring, calibration, flashing and
troubleshooting are in [`docs/HARDWARE_INTEGRATION.md`](../docs/HARDWARE_INTEGRATION.md). The wire
protocol is frozen in [`docs/SERIAL_PROTOCOL.md`](../docs/SERIAL_PROTOCOL.md) (§12: `DROP_SLOT`).

```text
firmware/
├── tactidose_esp32/          Arduino sketch (board "ESP32 Dev Module", libs AccelStepper + ESP32Servo)
│   ├── tactidose_esp32.ino   thin setup()/loop()
│   ├── config.h              mechanism, ALL pins (PLACEHOLDERS) and tunables  <- edit this
│   ├── ConfigCheck.h         config.h -> CoreConfig, compile-time sanity checks
│   ├── Hal.h                 hardware abstraction used by the core
│   ├── ArduinoHal.h/.cpp     Hal on a real ESP32 (+ compile-time ESP32 pin-rule checks)
│   └── TactiDoseCore.h/.cpp  protocol state machine: portable C++11, no Arduino, no heap
├── native/                   conformance harness (Linux ELF, built with g++ in Docker)
│   ├── harness.cpp           stdin/stdout protocol of docs/ARCHITECTURE_v1.md §7 + !pills (+ extensions)
│   ├── FakeHal.h/.cpp        simulated mechanism, pills and drop sensor + physical safety oracle
│   ├── config_check.cpp      runs the config.h static_asserts natively
│   ├── build.sh / build.ps1  -> native/bin/harness
│   └── bin/                  build output (git-ignored)
├── compile_esp32.sh / .ps1   real ESP32 compile check (arduino-cli in Docker, 6 config variants)
└── README.md
```

## Mechanisms

| `MECHANISM` | Hardware | `HOME` / `MOVE_SLOT` | Release ("gate") |
|---|---|---|---|
| `MECHANISM_CAROUSEL` (default) | stepper carousel over one output chute, one release servo, home sensor | real motion | the one servo at the chute |
| `MECHANISM_PER_CONTAINER_SERVO` (`-DMECHANISM=2`) | 3 fixed containers, one servo each, no stepper | complete at once, same messages | the servo of container *n* |

`DROP_SLOT n` = re-assert closed -> move (carousel) -> settle -> **release** (open, hold
`DROP_OPEN_MS`, close; atomic) -> `OK DROPPED n`, or `ERR NO_PILL` when the optional IR
break-beam drop sensor (`HAS_DROP_SENSOR 1`) saw no pill -> `OK READY`.

## Commands

```powershell
# Windows PowerShell, from the repository root
powershell -ExecutionPolicy Bypass -File firmware\native\build.ps1      # build the harness (Docker gcc:14)
python -m tactidose.hardware.conformance --target native                 # all 32 scenarios vs the firmware core
python -m pytest tests/test_fw_native.py -q                              # + timing/variants/parser/physics/per-container tests
powershell -ExecutionPolicy Bypass -File firmware\compile_esp32.ps1 -CaSubject Zscaler   # ESP32 compile check, 6 variants
```

```bash
# Linux / macOS / WSL / Git Bash
sh firmware/native/build.sh            # local g++ on Linux, Docker elsewhere (--docker / --local to force)
python -m tactidose.hardware.conformance_native build      # same, from Python (any OS)
bash firmware/compile_esp32.sh         # TACTIDOSE_EXTRA_CA_CERT=<pem> behind a TLS-inspecting proxy
```

`python -m tactidose.hardware.conformance --target serial --port COM5` runs the hardware-safe
scenarios against a flashed board (see HARDWARE_INTEGRATION.md §11.2 for the subset that applies
to 3 containers); `python -m tactidose hw-test --port COM5` runs the integration checklist.

## Design notes

* `TactiDoseCore` is driven by `loop()` and never blocks: stepper, homing phases, settle, gate
  travel, the release hold, auto-close, debouncing, drop-sensor sampling and serial parsing are
  all small state updates. The same object runs on the ESP32 (`ArduinoHal`) and in the harness
  (`FakeHal`). The harness therefore tests the code that is flashed, not a model of it.
* Gate travel is "atomic" (protocol rule 8.2), and so is the whole `DROP_SLOT` release (§12.3):
  serial input (including `STOP`) is held and button presses are latched, then handled right
  after `OK READY`.
* Drop detection counts only a **clear -> interrupted** transition of the beam during the release.
  A beam that is interrupted from the start never confirms a pill, so the drop fails closed with
  `ERR NO_PILL`. `ArduinoHal` latches short beam interruptions in a pin interrupt.
* Parsing (`parseLine`) is byte-for-byte equivalent to `tactidose/hardware/protocol.py
  parse_command` (including `DROP_SLOT`). `tests/test_fw_native.py` fuzzes both with thousands of
  lines.
* The core is C++11 (Arduino-ESP32 2.x compiles gnu++11; 3.x gnu++2b). `build.sh` checks this
  with `-std=c++11 -Wall -Wextra -Wpedantic -Wconversion -Werror`, and runs the config.h
  static_asserts for both mechanisms and both driver types. The ESP32 compile check builds six
  variants with `--warnings all`. As of esp32 core 3.3.12 the only warnings come from the
  ESP32Servo 3.2.1 library itself.

## Native harness protocol

Normative part: docs/ARCHITECTURE_v1.md §7 (`> line`, `!reset`, `!boot ok|dead|none`, `!tick ms`,
`!button CONFIRM|CANCEL 1|0`, `!sensor ok|dead`, `!jam 1|0`, `!quit`; one `!ack <sim_ms>` per
input line) plus the v1.1 directive `!pills <slot> <count>` (docs/ARCHITECTURE.md §13). The
harness device is a 6-slot carousel with a drop sensor and 20 pills per container. Extensions
used by `tactidose/hardware/conformance_native.py` and the tests:

| Directive | Reply / effect |
|---|---|
| `!peek <max_ms>` | `!peek <n>`: the next n ms are silent if no input arrives (computed on a snapshot that is then restored). NativeTarget uses it to skip silent ticks without changing semantics. A Docker round trip costs ~1-8 ms and the runner ticks in 5 ms chunks, so this is what makes the suite take seconds instead of minutes |
| `!physical` | `!physical key=value ...`: mechanism, carousel position, physical slot, sensors, gates (`gates=`, `gate_opens_by_gate=`), `pills=`, `pills_dropped=`, `drop_pulses=`, driver, oracle violations, firmware state (`fw_releasing=` ...) |
| `!parsehex <hex> [slots]` | `!parse ok <CMD> <slot>` / `!parse err <ERR> <CMD>` / `!parse empty` (`-` = zero bytes) |
| `!rx <hex>` | deliver raw bytes (e.g. `\r` terminators, split lines) |
| `!sensor stuck` | home sensor always active (broken sensor) |
| `!dropsensor ok\|dead\|blocked` | drop sensor works / never sees a pill / beam permanently interrupted |
| `!set <key>=<value>` / `!defaults` | firmware settings for the next `!reset`/`!boot` (`mechanism=carousel\|servo`, `numSlots`, `dropOpenMs`, `dropSensor`, `homeBackoffSteps`, `homeOffsetSteps`, `verifySlot`, `debugLog`, `holdWhenIdle`, `autoHome`, `settleMs`, `millisOffset`, ...) / back to the start-up settings. `mechanism` and `numSlots` also reshape the simulated mechanism |

Command line: `harness [--millis-offset N] [--set key=value]...`. For interactive play:
`docker run -it --rm -v "<repo>:/work" -w /work gcc:14 /work/firmware/native/bin/harness`,
then type for example `!boot ok`, `!tick 6000`, `> DROP_SLOT 3`, `!tick 3000`, `!physical`. Add
`--set mechanism=servo --set numSlots=3` for the per-container build.
