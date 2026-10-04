# TactiDose hardware integration guide (for the hardware teammate)

Audience: whoever builds the carousel, wires the ESP32 and flashes the firmware.
Read with `docs/SERIAL_PROTOCOL.md` (the frozen host <-> ESP32 contract) and the handoff
(`TactiDose_Hardware_Software_Handoff.md`, especially §8-§12, §22-§24 and §29).

> **Hackathon prototype - NOT a medical device.** Demo with candy, empty containers or labelled
> tokens only, never real medication (handoff §1, §24).

> **All pins in this document and in `firmware/tactidose_esp32/config.h` are PLACEHOLDERS** until
> the questions of handoff §34 are answered (exact ESP32 board, stepper, driver, servo,
> touchscreen, power supplies, home sensor, drive train). Section 2 lists what each answer
> changes.

---

## 0. TL;DR

1. Answer the §34 questions (section 2), then edit **only** `firmware/tactidose_esp32/config.h`
   (pins, driver type, microstepping, gear ratio, servo angles...).
2. Wire using a template from section 4 and the power rules of section 5. Use a **common
   ground** and separate supplies for the motor and servo; never use the ESP32 3.3 V pin for them.
3. Flash with Arduino IDE 2.x: board **ESP32 Dev Module**, libraries **AccelStepper** and
   **ESP32Servo** (section 8).
4. In Serial Monitor (115200 baud, "Newline") you should see
   `EVENT BOOT 1.0.0-ref`, `OK HOMING`, `OK HOMED`, `OK READY`. Then try `PING`, `STATUS`,
   `DISPENSE_SLOT 3`, `CLOSE_GATE`.
5. Calibrate the servo angles, the direction and the home offset (section 7).
6. Run the checks from the laptop (section 9):
   `python -m tactidose hw-test --port COM5` and
   `python -m tactidose.hardware.conformance --target serial --port COM5`.

## 1. What the firmware does (and does not do)

`firmware/tactidose_esp32/` is a **reference implementation**: adapt it rather than rewrite it.
The software team tested it against the shared protocol suite.

| File | Role | Edit? |
|---|---|---|
| `config.h` | every pin and tunable, with comments | **yes** |
| `tactidose_esp32.ino` | thin Arduino entry point (`setup()`/`loop()`) | rarely |
| `ArduinoHal.h/.cpp` | AccelStepper + ESP32Servo glue, compile-time pin checks | only for new hardware types |
| `ConfigCheck.h` | turns config.h into the core config; rejects unsafe values at compile time | no |
| `TactiDoseCore.h/.cpp` | the protocol state machine (portable C++, no Arduino) | avoid; if you must, re-run the native conformance suite (section 9.4) |
| `Hal.h` | interface between the core and the hardware | no |

Behaviour (normative details in `docs/SERIAL_PROTOCOL.md` §3-§8):

* **Deterministic and non-blocking.** `loop()` never waits. `PING`, `STATUS` and `STOP` are
  answered while the carousel moves.
* **Boot:** gate commanded closed first. After `GATE_TRAVEL_MS` it sends `EVENT BOOT <fw>`, then
  homes automatically (`OK HOMING` ... `OK HOMED`, `OK READY`).
* **Homing:** slow seek in `HOMING_DIR` until the sensor is stable for `HOME_DEBOUNCE_MS`. It then
  backs off `HOME_BACKOFF_STEPS` and re-approaches at `HOMING_SLOW_SPEED_SPS`, so the edge is
  repeatable. It gives up after 1.25 revolutions or `HOME_TIMEOUT_MS` (`ERR HOME_TIMEOUT`, state
  `FAULT`). A sensor that is already active when homing starts is first released; one that never
  releases (shorted, stuck) is treated as broken and is never accepted as "home".
* **Slot targets are absolute:** `round(k x CAROUSEL_STEPS_PER_REV / NUM_SLOTS)` from the home
  edge (+ `HOME_OFFSET_STEPS`), so fractional steps never accumulate. The carousel does not wrap
  around: slot 5 -> slot 0 turns back 5/6 of a revolution (safe even with cables on the carousel).
* **Interlocks enforced on the device**, whatever the host sends (protocol §7). The carousel
  never moves with the gate open; the gate never opens while moving, while homing or before
  homing; at most one motion at a time.
* `DISPENSE_SLOT n` = re-assert gate closed -> move -> `OK AT_SLOT n` -> settle `SETTLE_MS` ->
  open (`GATE_TRAVEL_MS`) -> `OK GATE_OPEN`.
* **Gate travel is atomic** (rule 8.2): while the servo moves, serial input waits and is
  processed afterwards. Button presses are still debounced and acted on right after the travel.
  A `STOP` arriving during the last 400 ms of a dispense therefore yields `OK GATE_OPEN` first
  (the gate did open, so the host must count it as accessed), then `OK GATE_CLOSED`, `OK STOPPED`.
* **Motion timeout** (rule 8.6): a move not finished after `2 x expected + 2 s` -> `ERR MOTOR_FAULT`.
* **Gate auto-close** after `GATE_MAX_OPEN_MS` (120 s) as a safety net (the host closes it after
  ~60 s anyway).
* **Buttons** (debounced 30 ms, act on press): CONFIRM only reports `EVENT CONFIRM_BUTTON`.
  CANCEL reports `EVENT CANCEL_BUTTON`, then stops motion (`ERR STOPPED`, `OK STOPPED`) or closes
  an open gate (`OK GATE_CLOSED`, `OK READY`).
* **Motor power:** energised while homing, moving, with the gate open (it holds the compartment
  at the opening) and while idle in `READY`/`SAFE_STOP`. Released in `BOOT` and `FAULT`, so a jam
  can be cleared by hand. `STEPPER_HOLD_WHEN_IDLE 0` also releases it when idle.

What it **cannot** do: an open-loop stepper cannot feel a jam or lost steps. The motion timeout
catches moves that never finish. The optional `VERIFY_SLOT_WITH_HOME_SENSOR` check compares the
home sensor with the expected position on every arrival. Re-homing (`HOME`, or a power cycle)
restores the reference. The host decides *whether* to dispense; the firmware only decides *how*
to move safely (handoff §15, §33). No AI output ever reaches it.

## 2. Answer handoff §34 first

| Question (§34) | What it changes in `config.h` |
|---|---|
| 1. Exact stepper motor | `DRIVER_TYPE`, `MOTOR_FULL_STEPS_PER_REV` (200 for 1.8 deg, 400 for 0.9 deg, 2048 for 28BYJ-48), speeds |
| 2. Exact driver board | `DRIVER_TYPE`, `MICROSTEPS` (= jumper setting), `ENABLE_ACTIVE_LOW`, `STEP_PULSE_US`, current limit (section 4.1) |
| 3. Servo model | `SERVO_MIN/MAX_PULSE_US`, `SERVO_CLOSED/OPEN_DEG`, `GATE_TRAVEL_MS`, servo supply current |
| 4. ESP32 board/version | the whole pin plan (section 3); other variants (S3/C3) have different forbidden pins |
| 5. Touchscreen model/controller | which SPI/I2C pins to keep free (section 3) |
| 6. Power supplies | section 5; whether the servo and the motor can share a supply |
| 7. Home sensor type | `HAS_HOME_SENSOR`, `PIN_HOME_SENSOR`, `HOME_SENSOR_ACTIVE_LOW`, `HOME_SENSOR_PULLUP`, `HOME_DEBOUNCE_MS` |
| 8. Materials / tools | mechanical tolerances -> `SETTLE_MS`, speeds, `HOME_BACKOFF_STEPS` |
| 9. Compartments you can build reliably | `NUM_SLOTS` (2-12) **and** `TACTIDOSE_NUM_SLOTS` on the host |
| 10. Direct drive or belt/gears | `GEAR_RATIO` |

## 3. Pin plan (classic ESP32-WROOM-32 DevKit, placeholders)

| Function | GPIO | Dir | Notes |
|---|---|---|---|
| `PIN_STEP` | 25 | out | STEP/DIR drivers |
| `PIN_DIR` | 26 | out | |
| `PIN_ENABLE` | 27 | out | add **10 kOhm pull-up EN -> 3.3 V** so the driver stays off while the ESP32 boots; `-1` if EN is hard-wired |
| `PIN_IN1..IN4` | 25, 26, 27, 33 | out | ULN2003 alternative (same pins as STEP/DIR/EN; only one driver type is used) |
| `PIN_SERVO` | 13 | out | 50 Hz PWM via ESP32Servo |
| `PIN_HOME_SENSOR` | 32 | in | internal pull-up, active LOW by default |
| `PIN_CONFIRM_BUTTON` | 4 | in | big tactile button to GND, internal pull-up |
| `PIN_CANCEL_BUTTON` | 16 | in | button to GND; **WROVER modules have no GPIO 16** -> e.g. 35 + external 10 kOhm pull-up; `-1` if not fitted |
| `PIN_STATUS_LED` | -1 | out | optional; many DevKits have an LED on GPIO 2 |
| USB serial (host link) | 1 (TX0), 3 (RX0) | - | never use |
| Keep free for a touchscreen | 18 (SCK), 19 (MISO), 23 (MOSI), 5 (CS); 21 (SDA), 22 (SCL) | - | handoff §22: reserve display pins **before** fixing motor pins |

ESP32 pin rules (the compile fails on the hard ones; `ArduinoHal.cpp` checks them):

* **Never GPIO 6-11**: they are wired to the SPI flash, and using them crashes the board.
* **GPIO 34-39 are input-only and have no internal pull-ups.** They can never be outputs. As
  inputs they need an external 10 kOhm pull-up to **3.3 V**, unless the sensor drives the line.
* **Strapping pins 0, 2, 5, 12, 15**: their level at reset selects the boot mode. Some of them
  output a signal while booting. Do not use them for STEP/DIR/ENABLE/coils/servo: the motor could
  twitch, the gate could jerk, or the board might not boot. **GPIO 12 pulled HIGH at reset selects
  1.8 V flash, and the board will not boot.** Override only with `ALLOW_STRAPPING_PINS 1`.
* GPIO 14 and 15 output PWM while booting: don't use them for motor signals.
* ESP32 GPIOs are **3.3 V only and not 5 V tolerant**. A 5 V sensor output needs a divider or
  an open-collector output pulled up to 3.3 V.
* Any two functions on one GPIO -> compile error.

## 4. Wiring templates

### 4.1 Template A: A4988 / DRV8825 (or TMC2208/2209 standalone) + NEMA17

```text
   12 V PSU (motor) ──┬───────────────────────── VMOT   ┌──────────────┐
                      │ + 100 µF electrolytic    GND    │  A4988 /     │ 1A ─┐
   GND ───────────────┴──────────────┬────────── GND    │  DRV8825     │ 1B ─┤ coil 1 ┐
                                     │                  │              │ 2A ─┐        │ NEMA17
   ESP32 3V3 ─────────────────────── │ ───────── VDD*   │              │ 2B ─┤ coil 2 ┘
   ESP32 GND ────────────────────────┘ (common ground)  │              │
   ESP32 GPIO25 ──────────────────────────────── STEP   │ MS1 MS2 MS3  │  microstep jumpers
   ESP32 GPIO26 ──────────────────────────────── DIR    │ RESET─SLEEP  │  (tie together)
   ESP32 GPIO27 ──────┬───────────────────────── EN     └──────────────┘
                      └── 10 kΩ ── 3V3   (driver off during boot)
   * DRV8825 has no VDD pin (internal regulator)
```

* **Coils:** find the two pairs with a multimeter (continuity within a pair). One pair goes to
  1A/1B and the other to 2A/2B. If the motor only vibrates, the pairs are mixed up.
* **Never connect or disconnect the motor while the driver is powered.** That destroys the driver.
* **100 µF (>= 47 µF) electrolytic across VMOT-GND, right at the driver.** Without it, voltage
  spikes kill the driver.
* **Current limit** (turn the trim pot, measure VREF between the pot and GND):
  * A4988: `I_max = VREF / (8 x R_sense)`. Read R_sense on the board: R050 (0.05 Ohm) ->
    I = 2.5 x VREF; R068 -> 1.84 x VREF; R100 (many clones) -> 1.25 x VREF.
  * DRV8825 (Pololu-style, 0.1 Ohm): `I_max = 2 x VREF`.
  * TMC2208/2209 modules: see the module's data (often `I_rms ~ 0.71 x VREF`).
  * Start at ~70 % of the motor's rated phase current. Raise it until moves are reliable, and
    keep the heatsink warm, not too hot to touch.
* **Microstepping:** `MICROSTEPS` in config.h **must equal** the jumpers. A4988 1/16 = MS1, MS2,
  MS3 all HIGH. DRV8825 1/16 = M2 HIGH only (all HIGH = 1/32). TMC2209 standalone: MS1/MS2 pins
  (default 1/8).
* A4988/DRV8825: RESET tied to SLEEP (both high) or the driver stays asleep.
* Motor supply: typically 12 V (A4988 8-35 V, DRV8825 8.2-45 V). The chopper driver limits the
  current, so a 2.8 V-rated NEMA17 on 12 V is normal.

### 4.2 Template B: ULN2003 board + 28BYJ-48 (5 V)

```text
   5 V PSU (>= 1 A) ── + ──── ULN2003 board "+" (5-12V)      ┌──────────┐
   GND ─────────────── - ──── ULN2003 board "-" ─────┐       │ 28BYJ-48 │
   ESP32 GND ────────────────────────────────────────┘       │  5-wire  │
   ESP32 GPIO25 ─── IN1        ULN2003 board                 │  plug    │
   ESP32 GPIO26 ─── IN2        (motor plugs into the white   └──────────┘
   ESP32 GPIO27 ─── IN3         keyed connector)
   ESP32 GPIO33 ─── IN4
```

* Set `#define DRIVER_TYPE DRIVER_ULN2003`. Wire IN1..IN4 straight; the firmware handles
  AccelStepper's IN1-IN3-IN2-IN4 coil order. `DIR_INVERT 1` reverses the direction.
* ~240 mA continuous while energised. Power the board from a **separate 5 V supply**, not the
  ESP32 3.3 V pin. Avoid the DevKit's 5V/VIN pin too, because USB power sags when the servo moves.
* 3.3 V logic on IN1..IN4 is fine for the ULN2003 at these currents.
* It gets warm when held energised. `STEPPER_HOLD_WHEN_IDLE 0` releases it when idle, at the
  risk of drift (re-home more often).
* Slow (~7 RPM at 500 half-steps/s). Defaults for this driver: 500 steps/s, 4096 steps/rev.

### 4.3 Gate servo (SG90 / MG90S / MG996R)

```text
   5-6 V servo PSU ──┬──────────── servo red (V+)
                     ═ 470-1000 µF electrolytic (at the servo)
   GND ──────────────┴──────────── servo brown/black (GND) ──── ESP32 GND (common ground)
   ESP32 GPIO13 ────────────────── servo orange/yellow (signal, 3.3 V logic is fine)
```

* Stall current: SG90 ~0.7 A, MG996R ~2.5 A. **Not from the ESP32 3.3 V pin; preferably not
  from the ESP32 5 V/VIN pin either.** Servo current spikes brown out the ESP32. The board then
  resets mid-dispense, which shows as `EVENT BOOT` and makes the host record a hardware error.
* The servo only latches the gate. It never turns the carousel (handoff §5, §11).
* Design the gate so it rests **closed** when unpowered, and so a twitch at power-up cannot
  expose a compartment. The firmware commands CLOSED as its first action, but a servo can jerk
  when power arrives.

### 4.4 Home sensor options (pick one; set `HAS_HOME_SENSOR 1`)

| Sensor | Wiring | config.h |
|---|---|---|
| **Hall switch + magnet** (best). Magnet on the carousel at compartment 1, sensor fixed at the frame, 2-5 mm gap. 3.3 V parts: TI DRV5013 / DRV5032, Diodes AH180x | VCC 3.3 V, GND, OUT -> GPIO32 (open-drain: internal pull-up) | `HOME_SENSOR_ACTIVE_LOW 1`, `HOME_SENSOR_PULLUP 1` |
| A3144 / KY-003 (needs >= 4.5 V!) | bare A3144: VCC 5 V, OUT (open collector) pulled up to **3.3 V only** (internal pull-up). **KY-003 modules pull OUT up to their VCC**, so do not power them from 5 V | same; A3144 is unipolar: the south pole must face the marked side |
| **Lever micro-switch** hit by a tab once per turn | COM -> GND, NO -> GPIO32 | `ACTIVE_LOW 1`, `PULLUP 1`; `HOME_DEBOUNCE_MS` 5-10 (bounce) |
| **Slotted optical interrupter** (flag on the carousel) | module VCC 3.3 V (5 V modules swing to 5 V -> level shift), OUT -> GPIO32 | many modules are HIGH when blocked -> `HOME_SENSOR_ACTIVE_LOW 0`; measure it |
| none (MVP fallback) | - | `HAS_HOME_SENSOR 0`: align compartment 1 with the opening by hand **before** power-on; `HOME` returns to step 0. Re-align and power-cycle after a slip |

The active zone must be narrower than `HOME_BACKOFF_STEPS` and much narrower than one
compartment. Mount the sensor rigidly: homing is only as repeatable as the sensor bracket.

### 4.5 Buttons

* CONFIRM: big arcade/tactile button, the main user input. CANCEL/help: a second, differently
  shaped button. Wire each between its GPIO and GND (internal pull-up, `BUTTON_ACTIVE_LOW 1`).
* With long wires, add 100 nF from the pin to GND. The firmware debounces for 30 ms.
* A button held during reset is ignored until it is released.

## 5. Power rules (handoff §8, §23)

1. **Never power the stepper, the driver's VMOT or the servo from the ESP32 3.3 V pin.** Never
   drive a motor directly from a GPIO.
2. **Common ground**: ESP32 GND, driver GND, motor PSU GND and servo PSU GND all connected.
3. Separate supplies: motor (12 V for NEMA17, 5 V for 28BYJ-48) and servo (5-6 V, >= 1 A for an
   SG90, >= 3 A for an MG996R). The ESP32 runs from USB (laptop) during the demo.
4. Bulk capacitors: >= 47-100 µF at the driver VMOT, 470-1000 µF at the servo.
5. Never plug or unplug a stepper while its driver is powered.
6. Test every actuator on its own before assembling (handoff §29), and watch the serial log for
   `Brownout detector was triggered` while the servo moves.
7. Rough budget: ESP32 ~100-250 mA (USB). 28BYJ-48 ~240 mA. NEMA17 up to the current limit per
   phase from 12 V (much less from the PSU thanks to the chopper). Servo 0.1 A moving, 0.7-2.5 A
   stalled.

## 6. Steps-per-slot math

```text
CAROUSEL_STEPS_PER_REV = MOTOR_FULL_STEPS_PER_REV x MICROSTEPS x GEAR_RATIO
slot k target          = round(k x CAROUSEL_STEPS_PER_REV / NUM_SLOTS)        (from the home edge)
```

| Setup | steps/rev | per slot (6) | targets slot 0..5 |
|---|---|---|---|
| NEMA17 1.8 deg, 1/16, direct (default) | 200 x 16 = 3200 | 533.33 | 0, 533, 1067, 1600, 2133, 2667 |
| NEMA17 1.8 deg, 1/16, 20T->60T belt (`GEAR_RATIO 3.0`) | 9600 | 1600 | 0, 1600, 3200, 4800, 6400, 8000 |
| 28BYJ-48 half-step, nominal 64:1 (default for ULN2003) | 2048 x 2 = 4096 | 682.67 | 0, 683, 1365, 2048, 2731, 3413 |
| 28BYJ-48 half-step, true 63.684:1 (`2037.886f`) | 4075.77 | 679.30 | 0, 679, 1359, 2038, 2717, 3396 |

Targets are always computed from the home reference, never by adding per-slot increments, so
the 0.33-step fractions never pile up. The 28BYJ-48's real gear ratio is not exactly 64:1. If
compartment 6 is visibly further off than compartment 2, use `2037.886f`.

Move time with a trapezoidal profile (accelerate, cruise, decelerate):
`t = d/v + v/a` when `d >= v^2/a`, else `2 sqrt(d/a)`. With the defaults (1600 steps/s,
3200 steps/s^2) one compartment takes 0.82 s and the longest move (slot 0 <-> 5) 2.17 s, so the
motion timeout is 2 x 2.17 + 2 = 6.3 s. `ConfigCheck.h` rejects tunables whose longest move would
exceed the host's 20 s MOVE timeout, or whose 1.25-turn homing would not fit `HOME_TIMEOUT_MS`.

## 7. Calibration (in this order)

Use Serial Monitor (115200 baud, line ending "Newline") or `python -m tactidose serial-console`.

1. **Servo angles, before mounting the horn.** Set `HAS_HOME_SENSOR 0` temporarily, so the
   board boots straight to READY without moving. Send `OPEN_GATE` / `CLOSE_GATE` and adjust
   `SERVO_CLOSED_DEG` / `SERVO_OPEN_DEG` (re-flash each time). Closed must block the opening
   fully. Open must give easy access and must not touch the carousel. If the servo buzzes at
   CLOSED it is pushing against the frame: back off 2-5 deg. Time the travel and set
   `GATE_TRAVEL_MS` = measured + ~100 ms (max 600).
2. **Direction.** Still without homing, send `MOVE_SLOT 1`. Compartment 2 must arrive at the
   opening. If the carousel turns the other way, set `DIR_INVERT 1` (or relabel the compartments).
3. **Home sensor.** Set `HAS_HOME_SENSOR 1`. Check the raw sensor with a multimeter or the
   sensor module's LED, then set `HOME_SENSOR_ACTIVE_LOW`. Power-cycle: the carousel seeks
   slowly, stops at the sensor, backs off and re-approaches. If it always takes the long way
   round, flip `HOMING_DIR`. `ERR HOME_TIMEOUT` = sensor not seen (polarity, gap, magnet pole,
   wiring) or carousel jammed.
4. **Home offset.** After `OK HOMED`, compartment 1 should be centred in the opening. If it is
   off by an angle theta (deg, positive = it must still move in the + direction), set
   `HOME_OFFSET_STEPS = theta / 360 x CAROUSEL_STEPS_PER_REV` and repeat until centred.
5. **Every slot.** `DISPENSE_SLOT k` + `CLOSE_GATE` for k = 0..5. Each compartment must be
   centred and the gate must open freely.
6. **Repeatability.** Run `MOVE_SLOT 0..5` ten times, then `HOME`. Compartment 1 must land at
   exactly the same place each time (handoff §29). Optionally enable
   `VERIFY_SLOT_WITH_HOME_SENSOR 1` once slot 0 lies inside the sensor's active zone.
7. **Speed.** If it stalls or skips: lower `MAX_SPEED_SPS` / `ACCELERATION_SPS2`, check the
   current limit and the mechanics. Faster is not better for this demo.

## 8. Flashing

**Arduino IDE 2.x**

1. File -> Preferences -> *Additional boards manager URLs*:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. Boards Manager: install **esp32 by Espressif Systems** (compile-tested with 3.3.12).
3. Library Manager: install **AccelStepper** (Mike McCauley, tested 1.64) and **ESP32Servo**
   (Kevin Harrington / John K. Bennett, tested 3.2.1).
4. Open `firmware/tactidose_esp32/tactidose_esp32.ino`. All files of the folder are compiled.
5. Tools -> Board -> **ESP32 Dev Module** (or your exact board) -> Port -> Upload.
6. If upload fails with *"Failed to connect ... waiting for packet header"*, hold the **BOOT**
   button while it says "Connecting...". Also check for a charge-only USB cable, and the
   CP210x / CH340 driver.

**arduino-cli**

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 --warnings all firmware/tactidose_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 firmware/tactidose_esp32
```

**Compile check without installing anything (Docker):** `firmware\compile_esp32.ps1` (Windows) or
`bash firmware/compile_esp32.sh` builds both driver types (plus an alternate configuration of each)
with all warnings. Behind a
TLS-inspecting proxy, add `-CaSubject Zscaler` (or `TACTIDOSE_EXTRA_CA_CERT=<pem>` for the .sh).
Docker cannot reach the USB port on Windows, so upload with the IDE or a native arduino-cli.

## 9. Testing

### 9.1 By hand (Serial Monitor, "Newline")

```text
-> PING                 <- OK PONG
-> STATUS               <- OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=6 fw=1.0.0-ref
-> DISPENSE_SLOT 3      <- OK MOVING 3 / OK AT_SLOT 3 / OK GATE_OPEN
-> MOVE_SLOT 1          <- ERR INVALID_STATE     (gate open: motion refused)
-> CLOSE_GATE           <- OK GATE_CLOSED / OK READY
-> MOVE_SLOT 9          <- ERR INVALID_SLOT
-> MOVE_SLOT 4  then quickly  STOP   <- OK MOVING 4 / ERR STOPPED / OK STOPPED
-> DISPENSE_SLOT 1      <- ERR NOT_HOMED         (after STOP the device must re-home)
-> HOME                 <- OK HOMING / OK HOMED / OK READY
   press CONFIRM        <- EVENT CONFIRM_BUTTON
   press CANCEL while moving   <- EVENT CANCEL_BUTTON / ERR STOPPED / OK STOPPED
```

Lines starting with `#` are firmware debug output (the host ignores them). Set `DEBUG_LOG 0` to
silence them.

### 9.2 From the laptop against the real board

```bash
python -m tactidose hw-test --port COM5                                   # handoff §29 checklist
python -m tactidose.hardware.conformance --target serial --port COM5      # every hardware_safe scenario
```

Both move the carousel and open the gate: keep hands clear, and load only candy/tokens. The
serial target only runs scenarios marked `hardware_safe` (no fault injection, real time).
Finally, run the complete demo 10 times. One failure in ten is a real problem (handoff §29).

### 9.3 Without hardware

```bash
powershell -File firmware\native\build.ps1            # or: sh firmware/native/build.sh  (Docker gcc:14)
python -m tactidose.hardware.conformance --target native
```

This runs the *same* `TactiDoseCore.cpp` on a simulated carousel (simulated time, a few seconds
for all 26 scenarios, including fault injection: dead sensor, jam, reboot, buttons). The simulated
physics also check physical safety rules (no stepping with the gate open, gate only opens at a
compartment ...). `pytest -m native tests/test_fw_native.py` adds timing, config-variant,
millis()-wrap and parser-equivalence tests.

### 9.4 After changing TactiDoseCore.cpp

Re-run 9.3 **and** `firmware\compile_esp32.ps1`. The protocol in `docs/SERIAL_PROTOCOL.md` is
frozen: if a scenario fails, the firmware is wrong, not the scenario.

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Board resets every time the laptop opens the port (`EVENT BOOT`, re-homing) | DevKit auto-reset via DTR/RTS -> EN. Expected: the host clears DTR/RTS before opening, waits for the boot, re-syncs with `PING`+`STATUS` and re-homes. Arduino Serial Monitor resets the board on every open. A 10 µF cap EN->GND stops it (then press BOOT to flash). |
| `Brownout detector was triggered`, or resets when the servo moves | Servo powered from the ESP32/USB. Use a separate 5-6 V supply + 470-1000 µF, short thick wires, common GND. |
| Wrong compartment / slowly drifting position | Lost steps: current limit too low (or too high -> thermal shutdown), speed/accel too high, binding carousel, loose grub screw/belt, `MICROSTEPS` not matching the jumpers, `GEAR_RATIO` wrong. `HOME` restores the reference; recalibrate `HOME_OFFSET_STEPS`. |
| Motor hums/vibrates but does not turn | NEMA17 coil pairs mixed up; 28BYJ-48 with the wrong `DRIVER_TYPE`; current too low; speed too high. |
| `ERR HOME_TIMEOUT` at boot | Sensor polarity (`HOME_SENSOR_ACTIVE_LOW`), gap/magnet pole (A3144 = south pole), wiring/pull-up, `HOMING_DIR`, carousel jammed, homing too slow for `HOME_TIMEOUT_MS`. `# home: ...` debug lines tell which phase failed. |
| Homed, but compartment 1 not centred | `HOME_OFFSET_STEPS` (section 7.4). |
| `ERR MOTOR_FAULT` | Move did not finish within 2 x expected + 2 s. Usually `loop()` blocked by added code (never use `delay()`), or the position check (`VERIFY_SLOT_WITH_HOME_SENSOR`) failed after lost steps. Clear the cause, then `HOME`. |
| Board does not boot / boot loops with the wiring attached | Something pulls a strapping pin (0, 2, 5, 12, 15) at reset, or uses GPIO 6-11. Fix the pin plan. |
| Garbage characters at power-up | ESP32 ROM boot messages (`ets Jun 8 2016 ... rst:0x1`). Harmless: the host ignores non-protocol lines. |
| Driver very hot | Current limit too high, missing heatsink. 28BYJ-48 warm when holding: `STEPPER_HOLD_WHEN_IDLE 0`. |
| No reply at all | Baud 115200, line ending must be `\n` (Serial Monitor "Newline"), right COM port, nothing else (Serial Monitor) holding the port. |

## 11. Physical safety (handoff §24)

Use candy/tokens only. Cover pinch points and gears. Keep fingers out of the carousel while it
moves (the firmware says nothing aloud, but the host announces motion). The gate defaults closed
after any reset. `STOP` and the CANCEL button stop motion immediately. Do not claim pill
counting or medical-grade safety.
