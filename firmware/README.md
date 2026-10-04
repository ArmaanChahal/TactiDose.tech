# TactiDose firmware (REFERENCE implementation)

Reference ESP32 firmware for the TactiDose carousel, plus a native test harness that runs the
same state machine on a simulated carousel. **Hackathon prototype, not a medical device**: demo
with candy/tokens only. The hardware teammate adapts `tactidose_esp32/config.h`; wiring,
calibration, flashing and troubleshooting are in [`docs/HARDWARE_INTEGRATION.md`](../docs/HARDWARE_INTEGRATION.md).
The wire protocol is frozen in [`docs/SERIAL_PROTOCOL.md`](../docs/SERIAL_PROTOCOL.md).

```text
firmware/
├── tactidose_esp32/          Arduino sketch (board "ESP32 Dev Module", libs AccelStepper + ESP32Servo)
│   ├── tactidose_esp32.ino   thin setup()/loop()
│   ├── config.h              ALL pins (PLACEHOLDERS) and tunables  <- edit this
│   ├── ConfigCheck.h         config.h -> CoreConfig, compile-time sanity checks
│   ├── Hal.h                 hardware abstraction used by the core
│   ├── ArduinoHal.h/.cpp     Hal on a real ESP32 (+ compile-time ESP32 pin-rule checks)
│   └── TactiDoseCore.h/.cpp  protocol state machine: portable C++11, no Arduino, no heap
├── native/                   conformance harness (Linux ELF, built with g++ in Docker)
│   ├── harness.cpp           stdin/stdout protocol of docs/ARCHITECTURE.md §7 (+ extensions)
│   ├── FakeHal.h/.cpp        simulated carousel physics + physical safety oracle
│   ├── config_check.cpp      runs the config.h static_asserts natively
│   ├── build.sh / build.ps1  -> native/bin/harness
│   └── bin/                  build output (git-ignored)
├── compile_esp32.sh / .ps1   real ESP32 compile check (arduino-cli in Docker, 4 config variants)
└── README.md
```

## Commands

```powershell
# Windows PowerShell, from the repository root
powershell -ExecutionPolicy Bypass -File firmware\native\build.ps1      # build the harness (Docker gcc:14)
python -m tactidose.hardware.conformance --target native                 # all scenarios vs the firmware core
python -m pytest tests/test_fw_native.py -q                              # + timing/variants/parser/physics tests
powershell -ExecutionPolicy Bypass -File firmware\compile_esp32.ps1      # ESP32 compile check (add -CaSubject Zscaler behind a TLS proxy)
```

```bash
# Linux / macOS / WSL / Git Bash
sh firmware/native/build.sh            # local g++ on Linux, Docker elsewhere (--docker / --local to force)
python -m tactidose.hardware.conformance_native build      # same, from Python (any OS)
bash firmware/compile_esp32.sh
```

`python -m tactidose.hardware.conformance --target serial --port COM5` runs the hardware-safe
scenarios against a flashed board.

## Design notes

* `TactiDoseCore` is driven by `loop()` and never blocks: stepper, homing phases, settle, gate
  travel, auto-close, debouncing and serial parsing are all small state updates. The same object
  runs on the ESP32 (`ArduinoHal`) and in the harness (`FakeHal`). The harness therefore tests
  the code that is flashed, not a model of it.
* Gate travel is "atomic" (protocol rule 8.2): serial input is held while the servo moves, and
  button presses are latched and handled right after it.
* Parsing (`parseLine`) is byte-for-byte equivalent to `tactidose/hardware/protocol.py
  parse_command`. `tests/test_fw_native.py` fuzzes both with thousands of lines.
* The core is C++11 (Arduino-ESP32 2.x compiles gnu++11; 3.x gnu++2b). `build.sh` checks this
  with `-std=c++11 -Wall -Wextra -Wpedantic -Wconversion -Werror`. The ESP32 compile check
  builds with `--warnings all`. As of esp32 core 3.3.12 the only warnings come from the
  ESP32Servo 3.2.1 library itself.

## Native harness protocol

Normative part: docs/ARCHITECTURE.md §7 (`> line`, `!reset`, `!boot ok|dead|none`, `!tick ms`,
`!button CONFIRM|CANCEL 1|0`, `!sensor ok|dead`, `!jam 1|0`, `!quit`; one `!ack <sim_ms>` per
input line). Extensions used by `tactidose/hardware/conformance_native.py` and the tests:

| Directive | Reply / effect |
|---|---|
| `!peek <max_ms>` | `!peek <n>`: the next n ms are silent if no input arrives (computed on a snapshot that is then restored). NativeTarget uses it to skip silent ticks without changing semantics. A Docker round trip costs ~1-8 ms and the runner ticks in 5 ms chunks, so this is what makes the suite take seconds instead of minutes |
| `!physical` | `!physical key=value ...`: carousel position, physical slot, sensor, gate, driver, oracle violations, firmware state |
| `!parsehex <hex> [slots]` | `!parse ok <CMD> <slot>` / `!parse err <ERR> <CMD>` / `!parse empty` (`-` = zero bytes) |
| `!rx <hex>` | deliver raw bytes (e.g. `\r` terminators, split lines) |
| `!sensor stuck` | home sensor always active (broken sensor) |
| `!set <key>=<value>` / `!defaults` | firmware settings for the next `!boot` (`homeBackoffSteps`, `homeOffsetSteps`, `verifySlot`, `debugLog`, `holdWhenIdle`, `autoHome`, `settleMs`, `millisOffset`, ...) / back to the start-up settings |

Command line: `harness [--millis-offset N] [--set key=value]...`. For interactive play:
`docker run -it --rm -v "<repo>:/work" -w /work gcc:14 /work/firmware/native/bin/harness`,
then type for example `!boot ok`, `!tick 6000`, `> DISPENSE_SLOT 3`, `!tick 3000`, `!physical`.
