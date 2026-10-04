# TactiDose hardware integration guide (v2: 3-container pill dropper)

Audience: whoever builds the mechanism, wires the ESP32 and flashes the firmware.
Read with `docs/SERIAL_PROTOCOL.md` (the frozen host <-> ESP32 contract, **v1.1**, §12 `DROP_SLOT`)
and `docs/ARCHITECTURE.md` (v2 product: who may drop a pill and when).

> **Hackathon prototype - NOT a medical device.** Demo with candy or labelled tokens only, never
> real medication.

> **All pins in this document and in `firmware/tactidose_esp32/config.h` are PLACEHOLDERS** until
> the questions of section 3 are answered (exact ESP32 board, which mechanism, motor/servos, drop
> sensor, power supplies). Section 3 lists what each answer changes.

---

## 0. TL;DR

1. Choose the mechanism (section 2): **carousel** (one stepper + one release servo) or **one servo
   per container**. Set `MECHANISM`, `NUM_SLOTS 3` and the pins in
   `firmware/tactidose_esp32/config.h`. This is the only file you should need to edit.
2. Wire it with a template from section 5 and the power rules of section 6: **common ground**,
   separate supplies for the motor and the servos, never the ESP32 3.3 V pin.
3. Flash it with Arduino IDE 2.x: board **ESP32 Dev Module**, libraries **AccelStepper** and
   **ESP32Servo** (section 10).
4. In Serial Monitor (115200 baud, "Newline") you should see `EVENT BOOT 1.1.0-ref`, `OK HOMING`,
   `OK HOMED`, `OK READY`. Then try `PING`, `STATUS` (it must say `proto=1.1`) and `DROP_SLOT 0`.
5. Calibrate in the order of section 9: servo angles, carousel direction/home, `DROP_OPEN_MS`
   (exactly one pill per drop), drop sensor.
6. Run the checks from the laptop (section 11). Load candy first, because both checks drop pills:
   `python -m tactidose hw-test --port COM5` and
   `python -m tactidose.hardware.conformance --target serial --port COM5`.

## 1. What v2 changes for the hardware

| v1 (compartment carousel) | v2 (pill dropper) |
|---|---|
| 6 compartments; the gate opens and the user takes the dose | **3 containers**; the device **drops one pill** into an output chute or cup |
| `DISPENSE_SLOT n` leaves the gate open until `CLOSE_GATE` | **`DROP_SLOT n`**: move, settle, open, hold `DROP_OPEN_MS`, close, report `OK DROPPED n`, all on the device |
| one mechanism | **two mechanisms** (section 2); the host cannot tell them apart |
| no feedback after the gate opened | optional **IR break-beam drop sensor**: `ERR NO_PILL` when nothing fell (container empty or jammed) |
| `STATUS ... fw=1.0.0-ref` | `STATUS ... fw=1.1.0-ref proto=1.1 drop_sensor=0\|1` |

The v1 commands still work unchanged. A v1 board still works with the v2 app too: the host then
emulates a drop (section 12).

The firmware never decides *whether* to drop. The host does that: the schedule, the patient's
"Drop pill" button and the AI agent's requests all go through `DropService`. That service enforces
the global cooldown, the double-dose guard and the inventory. The firmware only decides *how* to
release one pill safely. No AI output ever reaches it.

## 2. Choose the mechanism

| | `MECHANISM_CAROUSEL` (default) | `MECHANISM_PER_CONTAINER_SERVO` |
|---|---|---|
| Idea | A stepper turns container *n* over one output chute. One servo works the release there: a trapdoor, or a shuttle with a single pill-sized hole. | 3 fixed containers, each with its own servo-driven dispensing wheel or flap over a common chute. |
| Parts | stepper + driver, 1 servo, home sensor (recommended) | 3 servos, no stepper, no home sensor |
| `HOME`, `MOVE_SLOT` | real motion: homing, then absolute moves | complete at once (`OK MOVING n`, `OK AT_SLOT n` in the same millisecond) |
| "gate" in the protocol | the one release servo | the servo of container *n* (`OPEN_GATE` opens the current container's servo) |
| Can detect | jams during motion (`ERR MOTOR_FAULT`, motion timeout) | nothing without the drop sensor: hobby servos give no feedback. **Fit the drop sensor.** |
| Power | 12 V (NEMA17) or 5 V (28BYJ-48) for the motor, plus 5-6 V for one servo | 5-6 V sized for **three** servos (section 5.4) |

**Exactly one pill per release cycle is the mechanism's job.** The firmware performs one
open-hold-close cycle per `DROP_SLOT`. A plain trapdoor under a full tube drops the whole stack.
Use a pocket that holds one pill: a shuttle plate with one pill-sized hole (carousel) or a
dispensing wheel with one pocket (per container). At CLOSED the pocket sits under the container
and fills with one pill. At OPEN it sits over the chute and the pill falls out.

## 3. Answer these first

| Question | What it changes in `config.h` |
|---|---|
| 1. Which mechanism (section 2)? | `MECHANISM` (or `-DMECHANISM=2` from the build) |
| 2. How many containers can you build reliably? (v2: 3) | `NUM_SLOTS` (2-12) **and** `TACTIDOSE_NUM_SLOTS` on the host |
| 3. Exact ESP32 board/version | the whole pin plan (section 4); S3/C3 variants have different forbidden pins |
| 4. Servo model(s) | `SERVO_MIN/MAX_PULSE_US`, `SERVO_CLOSED/OPEN_DEG`, `SERVO_TRIM_DEG`, `GATE_TRAVEL_MS`, servo supply current |
| 5. Drop sensor fitted? Which type? | `HAS_DROP_SENSOR`, `PIN_DROP_SENSOR`, `DROP_SENSOR_ACTIVE_LOW`, `DROP_SENSOR_PULLUP` |
| 6. Carousel: exact stepper and driver | `DRIVER_TYPE`, `MOTOR_FULL_STEPS_PER_REV`, `MICROSTEPS`, `ENABLE_ACTIVE_LOW`, `STEP_PULSE_US`, speeds, current limit |
| 7. Carousel: home sensor type | `HAS_HOME_SENSOR`, `PIN_HOME_SENSOR`, `HOME_SENSOR_ACTIVE_LOW`, `HOME_SENSOR_PULLUP`, `HOME_DEBOUNCE_MS` |
| 8. Carousel: direct drive or belt/gears | `GEAR_RATIO` |
| 9. Power supplies | section 6 |
| 10. Touchscreen (optional kiosk) model/controller | which SPI/I2C pins to keep free (section 4) |

## 4. Pin plan (classic ESP32-WROOM-32 DevKit, placeholders)

| Function | Carousel | Per-container servo | Notes |
|---|---|---|---|
| `PIN_STEP` / ULN2003 `PIN_IN1` | 25 | - | STEP/DIR driver / ULN2003 board |
| `PIN_DIR` / `PIN_IN2` | 26 | - | |
| `PIN_ENABLE` / `PIN_IN3` | 27 | - | EN: add a **10 kOhm pull-up EN -> 3.3 V** so the driver stays off while the ESP32 boots; `-1` if hard-wired |
| `PIN_IN4` | 33 (ULN2003 only) | - | |
| `PIN_SERVO` (release) | 13 | - | trapdoor / shuttle servo, 50 Hz PWM |
| `PIN_RELEASE_SERVOS` | - | **25, 26, 27** | containers 1, 2, 3 (slots 0, 1, 2), in this order; exactly `NUM_SLOTS` entries |
| `PIN_HOME_SENSOR` | 32 | - | internal pull-up, active LOW by default |
| `PIN_DROP_SENSOR` | 34 | 34 | input-only: **external 10 kOhm pull-up to 3.3 V required** (`DROP_SENSOR_PULLUP 0`) |
| `PIN_CONFIRM_BUTTON` | 4 | 4 | big tactile button to GND, internal pull-up |
| `PIN_CANCEL_BUTTON` | 16 | 16 | button to GND; **WROVER modules have no GPIO 16**: use e.g. 35 + external 10 kOhm pull-up; `-1` if not fitted |
| `PIN_STATUS_LED` | -1 | -1 | optional; many DevKits have an LED on GPIO 2 |
| USB serial (host link) | 1 (TX0), 3 (RX0) | same | never use |
| Keep free for a touchscreen | 18, 19, 23, 5 (SPI); 21, 22 (I2C) | same | reserve display pins **before** fixing motor pins |

ESP32 pin rules. The compile fails on the hard ones; `ArduinoHal.cpp` checks them:

* **Never GPIO 6-11**: they are wired to the SPI flash, and using them crashes the board.
* **GPIO 34-39 are input-only and have no internal pull-ups.** They can never drive a servo or a
  driver. As inputs they need an external 10 kOhm pull-up to **3.3 V**, unless the sensor drives
  the line itself.
* **Strapping pins 0, 2, 5, 12, 15**: their level at reset selects the boot mode, and some output
  a signal while booting. Do not use them for STEP/DIR/ENABLE/coils/servos: a motor could twitch,
  a release could jerk open (a pill drops!), or the board might not boot. **GPIO 12 pulled HIGH at
  reset selects 1.8 V flash, and the board will not boot.** Override only with
  `ALLOW_STRAPPING_PINS 1`.
* **GPIO 14 and 15 output PWM while booting**: never use them for motor signals or release servos
  (GPIO 14 is rejected for `PIN_RELEASE_SERVOS`).
* ESP32 GPIOs are **3.3 V only and not 5 V tolerant**. A 5 V sensor output needs a divider, or an
  open-collector output pulled up to 3.3 V.
* Any two functions on one GPIO -> compile error.

## 5. Wiring templates

### 5.1 Carousel, template A: A4988 / DRV8825 (or TMC2208/2209 standalone) + NEMA17

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
  * Start at ~70 % of the motor's rated phase current. Raise it until moves are reliable. The
    heatsink should get warm, not too hot to touch.
* **Microstepping:** `MICROSTEPS` in config.h **must equal** the jumpers. A4988 1/16 = MS1, MS2,
  MS3 all HIGH. DRV8825 1/16 = M2 HIGH only (all HIGH = 1/32). TMC2209 standalone: MS1/MS2 pins
  (default 1/8).
* A4988/DRV8825: tie RESET to SLEEP (both high), or the driver stays asleep.
* Motor supply: typically 12 V (A4988 8-35 V, DRV8825 8.2-45 V). The chopper driver limits the
  current, so a 2.8 V-rated NEMA17 on 12 V is normal.

### 5.2 Carousel, template B: ULN2003 board + 28BYJ-48 (5 V)

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
  ESP32 3.3 V pin. Avoid the DevKit's 5V/VIN pin too, because USB power sags when a servo moves.
* 3.3 V logic on IN1..IN4 is fine for the ULN2003 at these currents.
* It gets warm when held energised. `STEPPER_HOLD_WHEN_IDLE 0` releases it when idle, at the
  risk of drift (re-home more often).
* Slow (~7 RPM at 500 half-steps/s). Defaults for this driver: 500 steps/s, 4096 steps/rev.

### 5.3 Carousel: the release servo (SG90 / MG90S / MG996R)

```text
   5-6 V servo PSU ──┬──────────── servo red (V+)
                     ═ 470-1000 µF electrolytic (at the servo)
   GND ──────────────┴──────────── servo brown/black (GND) ──── ESP32 GND (common ground)
   ESP32 GPIO13 ────────────────── servo orange/yellow (signal, 3.3 V logic is fine)
```

* Stall current: SG90 ~0.7 A, MG996R ~2.5 A. **Not from the ESP32 3.3 V pin; preferably not
  from the ESP32 5 V/VIN pin either.** Servo current spikes brown out the ESP32. The board then
  resets mid-drop; the host sees `EVENT BOOT`, records the drop as UNCERTAIN and asks a caregiver
  to check.
* The servo only works the release. It never turns the carousel.
* Design the release so it rests **closed** when unpowered, and so that a twitch at power-up
  cannot let a pill go. The firmware commands CLOSED as its first action, but a servo can jerk when
  power arrives.

### 5.4 Per container: three release servos

```text
   5-6 V servo PSU, sized for THREE servos (>= 3 A for SG90/MG90S; MG996R: 3 x 2.5 A -> pick smaller servos)
      + ──┬─────────────────┬─────────────────┬────────────── servo 1, 2, 3 red (V+)
          ═ 1000-2200 µF    ═ 100-470 µF      ═ 100-470 µF     bulk cap on the rail + one near each servo
      - ──┴─────────────────┴─────────────────┴────────────── servo 1, 2, 3 brown/black ─── ESP32 GND
   ESP32 GPIO25 ───────────────────────────────────────────── servo 1 signal  (container 1 = slot 0)
   ESP32 GPIO26 ───────────────────────────────────────────── servo 2 signal  (container 2 = slot 1)
   ESP32 GPIO27 ───────────────────────────────────────────── servo 3 signal  (container 3 = slot 2)
```

* Set `#define MECHANISM MECHANISM_PER_CONTAINER_SERVO`. `PIN_RELEASE_SERVOS` lists one GPIO per
  container, container 1 first, exactly `NUM_SLOTS` of them. The compile fails otherwise.
* **Separate 5-6 V supply sized for all three servos**, never the ESP32 or USB 5 V. During normal
  operation only one servo moves at a time. At power-up, reset, `STOP` or a fault, all three are
  commanded CLOSED at the same moment and can stall together: budget the sum of the stall
  currents (3 x ~0.7 A for SG90). Use short, thick power wires, a bulk capacitor on the servo
  rail and a small one near each servo, and a **common ground** with the ESP32.
* Optional: 220-470 Ohm in series with each signal wire (protects the GPIO if a servo fails).
* `SERVO_CLOSED_DEG` / `SERVO_OPEN_DEG` are shared. `SERVO_TRIM_DEG` adds a per-container
  correction (-45..45 deg) for horn mounting differences (section 9.1).
* ESP32Servo drives up to 4 servos per hardware timer at 50 Hz; the firmware allocates as many
  timers as `NUM_SLOTS` needs.

### 5.5 IR break-beam drop sensor (optional, recommended)

```text
   Emitter (IR LED, 2 wires)            Receiver (3 wires, open collector)
     red   ── 3.3 V (or 5 V)              red    ── 3.3 V (or 5 V)
     black ── GND                         black  ── GND
                                          signal ── GPIO34 ──┬── 10 kΩ ── 3.3 V   (pull-up: required on GPIO 34-39)
                                                             └── 100 nF ── GND    (optional, long wires)
```

* Mount emitter and receiver facing each other **across the output chute, below the release**, so
  every pill must cross the beam. Every container must drop through the same beam.
* The beam must be narrower than the pill: a 3 mm/5 mm IR pair is fine for candy of 5 mm and
  larger. Use a slot aperture for smaller tokens. Shield the receiver from sunlight and IR
  remotes (a short tube around it).
* Pull the signal up to **3.3 V, never 5 V**. The ESP32 is not 5 V tolerant. With an
  open-collector receiver, its supply may be 5 V while the signal is pulled up to 3.3 V.
* Set `HAS_DROP_SENSOR 1`. `STATUS` then reports `drop_sensor=1`, and every `DROP_SLOT` that sees
  no pill ends in `ERR NO_PILL`.
* Polarity: open-collector break-beam receivers (for example Adafruit 2167/2168) read **LOW while
  the beam is interrupted** (`DROP_SENSOR_ACTIVE_LOW 1`). Other modules may be active-high. Check
  it after flashing: with an empty chute the boot debug line must say `# drop sensor: beam clear`
  (section 9.4).
* What the firmware counts as a pill: the beam going from **clear to interrupted** at any moment
  between the start of the release and the end of the closing travel. A falling pill breaks the
  beam for only a few ms, so `ArduinoHal` latches the edge in a pin interrupt. A beam that is
  already interrupted when the release starts (pill stuck in the chute, unplugged or misaligned
  receiver) never counts: the drop ends in `ERR NO_PILL`. The device fails closed: it never claims
  a drop it did not see.
* What the host does with it: `OK DROPPED n` -> pill count - 1. `ERR NO_PILL` -> the container's
  count is set to 0 and patient and caregivers get a "container empty" notification. Without a
  sensor, `OK DROPPED n` only means "the release cycle completed".

### 5.6 Carousel: home sensor options (pick one; set `HAS_HOME_SENSOR 1`)

| Sensor | Wiring | config.h |
|---|---|---|
| **Hall switch + magnet** (best). Magnet on the carousel at container 1, sensor fixed to the frame, 2-5 mm gap. 3.3 V parts: TI DRV5013 / DRV5032, Diodes AH180x | VCC 3.3 V, GND, OUT -> GPIO32 (open-drain: internal pull-up) | `HOME_SENSOR_ACTIVE_LOW 1`, `HOME_SENSOR_PULLUP 1` |
| A3144 / KY-003 (needs >= 4.5 V!) | bare A3144: VCC 5 V, OUT (open collector) pulled up to **3.3 V only** (internal pull-up). **KY-003 modules pull OUT up to their VCC**, so do not power them from 5 V | same; A3144 is unipolar: the south pole must face the marked side |
| **Lever micro-switch** hit by a tab once per turn | COM -> GND, NO -> GPIO32 | `ACTIVE_LOW 1`, `PULLUP 1`; `HOME_DEBOUNCE_MS` 5-10 (bounce) |
| **Slotted optical interrupter** (flag on the carousel) | module VCC 3.3 V (5 V modules swing to 5 V -> level shift), OUT -> GPIO32 | many modules are HIGH when blocked -> `HOME_SENSOR_ACTIVE_LOW 0`; measure it |
| none (MVP fallback) | - | `HAS_HOME_SENSOR 0`: align container 1 with the chute by hand **before** power-on; `HOME` returns to step 0. Re-align and power-cycle after a slip |

The active zone must be narrower than `HOME_BACKOFF_STEPS` and much narrower than one container.
Mount the sensor rigidly: homing is only as repeatable as the sensor bracket. The per-container
mechanism has no home sensor (nothing moves).

### 5.7 Buttons

* CONFIRM: big arcade/tactile button, the main local input. CANCEL/help: a second, differently
  shaped button. Wire each between its GPIO and GND (internal pull-up, `BUTTON_ACTIVE_LOW 1`).
* With long wires, add 100 nF from the pin to GND. The firmware debounces for 30 ms.
* A button held during reset is ignored until it is released.
* CANCEL during the motion or settle of a `DROP_SLOT` stops it before anything is released
  (`ERR STOPPED`, `OK STOPPED`). Once the release has started it completes first (section 8).

## 6. Power rules

1. **Never power the stepper, the driver's VMOT or a servo from the ESP32 3.3 V pin.** Never
   drive a motor directly from a GPIO.
2. **Common ground**: ESP32 GND, driver GND, motor PSU GND, servo PSU GND and drop sensor GND all
   connected.
3. Separate supplies: motor (12 V for NEMA17, 5 V for 28BYJ-48) and servos (5-6 V; >= 1 A for
   one SG90, >= 3 A for three; >= 3 A per MG996R). The ESP32 runs from USB (laptop) during the demo.
4. Bulk capacitors: >= 47-100 µF at the driver VMOT; 470-1000 µF at a single servo;
   1000-2200 µF on a three-servo rail plus 100-470 µF near each servo.
5. Never plug or unplug a stepper while its driver is powered.
6. Test every actuator on its own before assembling, and watch the serial log for
   `Brownout detector was triggered` while the servos move.
7. Rough budget: ESP32 ~100-250 mA (USB). 28BYJ-48 ~240 mA. NEMA17 up to the current limit per
   phase from 12 V (much less from the PSU thanks to the chopper). Each servo 0.1 A moving,
   0.7-2.5 A stalled. IR break-beam pair ~20 mA.

## 7. Steps-per-container math (carousel)

```text
CAROUSEL_STEPS_PER_REV = MOTOR_FULL_STEPS_PER_REV x MICROSTEPS x GEAR_RATIO
container k target     = round(k x CAROUSEL_STEPS_PER_REV / NUM_SLOTS)      (from the home edge)
```

| Setup | steps/rev | per container (3) | targets slot 0..2 |
|---|---|---|---|
| NEMA17 1.8 deg, 1/16, direct (default) | 200 x 16 = 3200 | 1066.67 | 0, 1067, 2133 |
| NEMA17 1.8 deg, 1/16, 20T->60T belt (`GEAR_RATIO 3.0`) | 9600 | 3200 | 0, 3200, 6400 |
| 28BYJ-48 half-step, nominal 64:1 (default for ULN2003) | 2048 x 2 = 4096 | 1365.33 | 0, 1365, 2731 |
| 28BYJ-48 half-step, true 63.684:1 (`2037.886f`) | 4075.77 | 1358.59 | 0, 1359, 2717 |

Targets are always computed from the home reference, never by adding per-container increments,
so the fractions never pile up. The carousel does not wrap around: slot 2 -> slot 0 turns back
2/3 of a revolution, which is safe even with cables on the carousel.

Move time with a trapezoidal profile (accelerate, cruise, decelerate):
`t = d/v + v/a` when `d >= v^2/a`, else `2 sqrt(d/a)`. With the defaults (1600 steps/s,
3200 steps/s^2, 3 containers), one container takes 1.17 s and the longest move (slot 0 <-> 2)
1.83 s, so the motion timeout is 2 x 1.83 + 2 = 5.7 s. `ConfigCheck.h` rejects tunables whose
longest move would exceed the host's 20 s MOVE timeout, whose longest `DROP_SLOT` would not end
5 s before the host's 30 s DROP timeout, or whose 1.25-turn homing would not fit
`HOME_TIMEOUT_MS`.

## 8. The release (`DROP_SLOT`) and `DROP_OPEN_MS`

```text
DROP_SLOT n   from READY
  release(s) re-asserted CLOSED
  carousel: move to container n       -> OK MOVING n ... OK AT_SLOT n     (per container: both at once)
  settle SETTLE_MS (300)              -- STOP / CANCEL here: ERR STOPPED, OK STOPPED, nothing released
  ── release, atomic ──────────────────────────────────────────────────────────────────────────────
  servo -> OPEN, wait GATE_TRAVEL_MS  -> OK GATE_OPEN
  hold DROP_OPEN_MS (500)                 the pill falls and crosses the drop sensor
  servo -> CLOSED, wait GATE_TRAVEL_MS -> OK GATE_CLOSED
  verdict                             -> OK DROPPED n   (or ERR NO_PILL with a sensor and no pill)
  ─────────────────────────────────────────────────────────────────────────────────────────────────
                                      -> OK READY
```

* With the defaults the release takes 400 + 500 + 400 = 1.3 s. It is **atomic**: serial input
  (including `STOP`, `PING` and `STATUS`) and button actions wait until `OK READY`, then they are
  processed in order. A `STOP` sent during the release therefore yields
  `OK GATE_CLOSED`, `OK DROPPED n`, `OK READY`, `OK STOPPED`.
* The host treats `ERR STOPPED` or `EVENT BOOT` *after* `OK GATE_OPEN` as **UNCERTAIN**: a pill
  may have dropped. The drop is flagged for caregiver review, the cooldown starts and nothing is
  retried automatically. This happens after a reset or brown-out during the release, so a solid
  power supply matters.
* Keep `2 x GATE_TRAVEL_MS + DROP_OPEN_MS` at about 1.5 s or less (the compile fails above 2 s).
  `DROP_OPEN_MS` must be 50-1000 ms.

## 9. Calibration (in this order)

Use Serial Monitor (115200 baud, line ending "Newline") or
`python -m serial.tools.miniterm COM5 115200 --eol LF`. `DEBUG_LOG 1` (the default) prints
`# ...` lines that explain what the firmware is doing; the host ignores them.

### 9.1 Servo angles (both mechanisms), before mounting the horns

* Carousel: set `HAS_HOME_SENSOR 0` temporarily, so the board boots straight to READY without
  moving. Per container: nothing moves anyway.
* Send `OPEN_GATE` / `CLOSE_GATE` and adjust `SERVO_CLOSED_DEG` / `SERVO_OPEN_DEG`, re-flashing
  each time. CLOSED must hold every pill back. OPEN must let the pocket's pill go, and must not
  touch the carousel. If a servo buzzes at CLOSED it is pushing against the frame: back off
  2-5 deg.
* Per container: `OPEN_GATE` moves the servo of the *current* container. Select it with
  `MOVE_SLOT k` (instant) first, e.g. `MOVE_SLOT 1`, `OPEN_GATE`, `CLOSE_GATE`. If one wheel needs
  different angles, set its entry in `SERVO_TRIM_DEG` (e.g. `0, 4, -3`) instead of changing the
  shared angles.
* Time the travel and set `GATE_TRAVEL_MS` = measured + ~100 ms (max 600). `OK GATE_OPEN` and
  `OK GATE_CLOSED` are only sent after this time.

### 9.2 Carousel only: direction, home, offset, repeatability

1. **Direction.** Still without homing, send `MOVE_SLOT 1`. Container 2 must arrive at the chute.
   If the carousel turns the other way, set `DIR_INVERT 1` (or relabel the containers).
2. **Home sensor.** Set `HAS_HOME_SENSOR 1`. Check the raw sensor with a multimeter or the module
   LED, then set `HOME_SENSOR_ACTIVE_LOW`. Power-cycle: the carousel seeks slowly, stops at the
   sensor, backs off and re-approaches. If it always takes the long way round, flip `HOMING_DIR`.
   `ERR HOME_TIMEOUT` = sensor not seen (polarity, gap, magnet pole, wiring) or carousel jammed.
3. **Home offset.** After `OK HOMED`, container 1 should be centred over the chute. If it is off
   by an angle theta (deg, positive = it must still move in the + direction), set
   `HOME_OFFSET_STEPS = theta / 360 x CAROUSEL_STEPS_PER_REV` and repeat until centred.
4. **Every container.** `MOVE_SLOT k` for k = 0..2: each container must sit centred over the
   chute, and the release must work freely.
5. **Repeatability.** Run `MOVE_SLOT 0..2` ten times, then `HOME`. Container 1 must land at
   exactly the same place each time. Optionally enable `VERIFY_SLOT_WITH_HOME_SENSOR 1` once slot 0
   lies inside the sensor's active zone.
6. **Speed.** If it stalls or skips: lower `MAX_SPEED_SPS` / `ACCELERATION_SPS2`, check the
   current limit and the mechanics. Faster is not better for this demo.

### 9.3 `DROP_OPEN_MS`: exactly one pill per drop

1. Fill each container with candy. Fit the drop sensor if you have one (`HAS_DROP_SENSOR 1`).
2. Send `DROP_SLOT k` ten times per container and count what comes out. With a sensor the debug
   output shows `# drop: pill seen N ms after the release started (release took M ms)`.
3. **Two pills at once:** the pocket is too big or the release stays open too long. Shorten
   `DROP_OPEN_MS` or fix the pocket. The firmware cannot count pills: one release = one pill must
   be true mechanically.
4. **Sometimes no pill** (or `ERR NO_PILL` with candy loaded): lengthen `DROP_OPEN_MS`, increase
   the opening angle, or smooth the pocket and chute so the pill does not stick.
5. With a sensor, N must stay well below M on every drop. A pill that crosses the beam after the
   closing travel is missed and reported as `ERR NO_PILL`. If N is close to M, move the sensor
   closer to the release or raise `DROP_OPEN_MS`.
6. Leave a margin: use the shortest `DROP_OPEN_MS` that releases reliably, plus ~150 ms.

### 9.4 Drop sensor check

1. Flash with `HAS_DROP_SENSOR 1` (and `DEBUG_LOG 1`). Empty chute -> reset the board ->
   `# drop sensor: beam clear`. Put a finger in the beam -> reset ->
   `# drop sensor: beam INTERRUPTED ...`. If they are swapped, flip `DROP_SENSOR_ACTIVE_LOW`.
2. Empty one container and send `DROP_SLOT k`: the reply must end with `ERR NO_PILL`, `OK READY`.
   Refill it: `OK DROPPED k`.
3. Drop ten pills from each container: ten `OK DROPPED` per container, no `ERR NO_PILL`.

## 10. Flashing

**Arduino IDE 2.x**

1. File -> Preferences -> *Additional boards manager URLs*:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. Boards Manager: install **esp32 by Espressif Systems** (compile-tested with 3.3.12).
3. Library Manager: install **AccelStepper** (Mike McCauley, tested 1.64) and **ESP32Servo**
   (Kevin Harrington / John K. Bennett, tested 3.2.1). The per-container build does not use
   AccelStepper, but installing it does no harm.
4. Open `firmware/tactidose_esp32/tactidose_esp32.ino`. All files of the folder are compiled.
5. Tools -> Board -> **ESP32 Dev Module** (or your exact board) -> Port -> Upload.
6. If upload fails with *"Failed to connect ... waiting for packet header"*, hold the **BOOT**
   button while it says "Connecting...". Also check for a charge-only USB cable, and the
   CP210x / CH340 driver.

**arduino-cli**

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 --warnings all firmware/tactidose_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p COM5 firmware/tactidose_esp32
# per-container build without editing config.h:
arduino-cli compile --fqbn esp32:esp32:esp32 --build-property "compiler.cpp.extra_flags=-DMECHANISM=2" firmware/tactidose_esp32
```

**Compile check without installing anything (Docker):** `firmware\compile_esp32.ps1` (Windows) or
`bash firmware/compile_esp32.sh` builds six variants with all warnings: carousel STEP/DIR, carousel
ULN2003 and per-container servo, each as shipped and with an alternate configuration (drop
sensor on/off, no EN pin, no cancel button, LED, `DIR_INVERT`, no home sensor ...). The core,
libraries and build cache stay in the Docker volume `tactidose-arduino`, so later runs need no
network. `-Update` refreshes them. Behind a TLS-inspecting proxy (Zscaler) add `-CaSubject
Zscaler`, or `TACTIDOSE_EXTRA_CA_CERT=<pem>` for the .sh. Docker cannot reach the USB port on
Windows, so upload with the IDE or a native arduino-cli.

Last result (2026-10-03, esp32 core 3.3.12, arduino-cli 1.5.2-rc.1, AccelStepper 1.64,
ESP32Servo 3.2.1): all six variants compile, with 0 warnings in the sketch files (the remaining
warnings come from the ESP32Servo library itself). The sketch uses ~295-300 KB (22 %) of flash.

## 11. Testing

### 11.1 By hand (Serial Monitor, "Newline")

```text
-> PING                 <- OK PONG
-> STATUS               <- OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=3 fw=1.1.0-ref proto=1.1 drop_sensor=0
-> DROP_SLOT 2          <- OK MOVING 2 / OK AT_SLOT 2 / OK GATE_OPEN / OK GATE_CLOSED / OK DROPPED 2 / OK READY
-> DROP_SLOT 3          <- ERR INVALID_SLOT     (3 containers: 0..2)
-> OPEN_GATE            <- OK GATE_OPEN
-> DROP_SLOT 0          <- ERR INVALID_STATE    (release open: nothing may move)
-> CLOSE_GATE           <- OK GATE_CLOSED / OK READY
-> DROP_SLOT 0  then quickly  STOP
                        <- OK MOVING 0 / ERR STOPPED / OK STOPPED      (stopped during motion or settle)
                        or ... OK GATE_OPEN / OK GATE_CLOSED / OK DROPPED 0 / OK READY / OK STOPPED
                                                                        (the release had already started)
-> DROP_SLOT 1          <- ERR NOT_HOMED        (after STOP the device must re-home)
-> HOME                 <- OK HOMING / OK HOMED / OK READY
   container 2 empty, drop sensor fitted:
-> DROP_SLOT 1          <- OK MOVING 1 / OK AT_SLOT 1 / OK GATE_OPEN / OK GATE_CLOSED / ERR NO_PILL / OK READY
   press CONFIRM        <- EVENT CONFIRM_BUTTON
   press CANCEL while a carousel move runs   <- EVENT CANCEL_BUTTON / ERR STOPPED / OK STOPPED
```

The v1 commands still work (`DISPENSE_SLOT n` leaves the release open until `CLOSE_GATE`). Lines
starting with `#` are firmware debug output. Set `DEBUG_LOG 0` to silence them.

### 11.2 From the laptop against the real board

Load candy/tokens into every container first: both commands drop pills. Close the Serial Monitor,
because only one program can hold the port.

```bash
python -m tactidose ports                                                 # which COM port is the ESP32?
python -m tactidose hw-test --port COM5                                   # integration checklist
python -m tactidose hw-test --port COM5 --interactive                     # + the CONFIRM/CANCEL buttons
python -m tactidose.hardware.conformance --target serial --port COM5      # every hardware_safe scenario
```

`hw-test` runs PING, STATUS, HOME, every `MOVE_SLOT`, `DISPENSE_SLOT` + `CLOSE_GATE`, **one
`DROP_SLOT` per container** (or the v1 emulation on old firmware), STOP during a move, HOME again
and an unknown command. It prints a PASS/FAIL table. An `ERR NO_PILL` row means a container was
empty or its pill did not reach the sensor. On `MECHANISM_PER_CONTAINER_SERVO` the row "STOP
during MOVE_SLOT" fails with "move finished before STOP could be sent". This is expected: that
mechanism has no motion to interrupt. Its STOP path (STOP during the settle, before anything is
released) is covered by the native tests.

The serial conformance target runs only the scenarios marked `hardware_safe` (no fault injection,
real time). The shared scenarios were written for the 6-slot harness: 9 of the 18 hardware-safe
scenarios check `slots=6`, use slots 3-5, or need a move that takes time (the per-container
mechanism has none). On the shipped 3-container builds, **both mechanisms**, run the 9 that apply
(the native tests check that these 9 pass on both builds):

```powershell
$applicable = "ping_variants_and_blank_lines", "unknown_and_overlong_commands", "move_to_current_slot",
    "invalid_slot_arguments", "interlocks_while_gate_open", "stop_when_idle_and_idempotent",
    "stop_with_gate_open_closes_gate", "home_from_ready_returns_to_slot_zero", "drop_slot_happy_path"
python -m tactidose.hardware.conformance --target serial --port COM5 @($applicable | ForEach-Object { "--scenario", $_ })
```

For the complete serial check of a carousel, flash a test build with `NUM_SLOTS 6`: positions 3-5
then fall between containers, which is harmless without pills. Or use the native harness (11.3).
Finally, run the complete demo 10 times. One failure in ten is a real problem.

To run the app against the board: `TACTIDOSE_HARDWARE_MODE=serial`, `TACTIDOSE_SERIAL_PORT=COM5`
(or `auto`), `TACTIDOSE_NUM_SLOTS=3` (must match `NUM_SLOTS`).

### 11.3 Without hardware

```bash
powershell -File firmware\native\build.ps1            # or: sh firmware/native/build.sh  (Docker gcc:14)
python -m tactidose.hardware.conformance --target native
python -m pytest tests/test_fw_native.py -q
```

This runs the *same* `TactiDoseCore.cpp` on a simulated dispenser in simulated time: all 32
protocol scenarios in a few seconds, including fault injection (dead home sensor, jam, reboot,
buttons, empty container). The simulation also checks physical safety rules: no stepping with the
release open, the release only opens over a container, at most one container servo open, and
`OK DROPPED` only when a pill really fell. The pytest file adds timing, release atomicity,
drop-sensor failures (dead, blocked), config variants, millis() wrap, parser equivalence and the
**per-container build** (harness setting `mechanism=servo`). To try it interactively:

```text
docker run -it --rm -v "<repo>:/work" -w /work gcc:14 /work/firmware/native/bin/harness --set mechanism=servo --set numSlots=3
!boot ok
!tick 1000
> DROP_SLOT 1
!tick 2000
!pills 1 0
> DROP_SLOT 1
!tick 2000
!physical
```

### 11.4 After changing TactiDoseCore.cpp

Re-run 11.3 **and** `firmware\compile_esp32.ps1`. The protocol in `docs/SERIAL_PROTOCOL.md` is
frozen: if a scenario fails, the firmware is wrong, not the scenario.

## 12. How the host drops pills (and how it copes with v1 firmware)

* After connecting, the host sends `STATUS`. If the reply contains `proto=1.1` (or newer), each
  drop is a single `DROP_SLOT n` with a 30 s timeout (`TACTIDOSE_TIMEOUT_DROP_S`).
* **v1 firmware** (no `proto` key, e.g. `fw=1.0.0-ref`): the host emulates the drop as one locked
  sequence: `DISPENSE_SLOT n` -> wait `drop_close_delay_ms` (`TACTIDOSE_DROP_CLOSE_DELAY_MS`,
  default 1500 ms) -> `CLOSE_GATE`. `OK GATE_OPEN` counts as dropped. v1 has no `ERR NO_PILL`, so
  a drop sensor is not used. The v1.1 firmware still accepts this sequence too.
* The outcome decides the record (`protocol.drop_certainty`):

  | Device reply | Host records | Inventory |
  |---|---|---|
  | `OK DROPPED n` (emulation: `OK GATE_OPEN`) | DROPPED, "pill dropped" notification | count - 1 |
  | `ERR NO_PILL` | FAILED (NO_PILL), "container empty" notification | set to 0 |
  | any other `ERR` before `OK GATE_OPEN` (`BUSY`, `NOT_HOMED`, `MOTOR_FAULT`, `STOPPED` ...) | FAILED, nothing released; scheduled doses retry while their window is open | unchanged |
  | `ERR STOPPED` / `EVENT BOOT` after `OK GATE_OPEN`, timeout, disconnect | UNCERTAIN + caregiver review, cooldown starts, no automatic retry | unchanged until reviewed |

* Before a drop, the host sends `HOME` itself when the device is not homed after a `STOP` or a reset
  (`hw_auto_home`), and `CLOSE_GATE` when a gate was left open. A device in `FAULT` (home timeout,
  motor fault) is refused (`DEVICE_UNAVAILABLE`) until a caregiver clears the cause and homes it
  from the care portal.

## 13. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Board resets every time the laptop opens the port (`EVENT BOOT`, re-homing) | DevKit auto-reset via DTR/RTS -> EN. Expected: the host clears DTR/RTS before opening, waits for the boot, re-syncs with `PING`+`STATUS` and re-homes. Arduino Serial Monitor resets the board on every open. A 10 µF cap EN->GND stops it (then press BOOT to flash). |
| `Brownout detector was triggered`, or resets when a servo moves | Servos powered from the ESP32/USB. Use a separate 5-6 V supply sized for all servos, bulk capacitors, short thick wires, common GND. A reset during a release makes the host record an UNCERTAIN drop. |
| `STATUS` has no `proto=1.1` | Old firmware (1.0.x) is flashed: the host falls back to the v1 emulation. Re-flash this firmware. |
| `ERR INVALID_SLOT` for container 3 | `NUM_SLOTS` too small, or the host's `TACTIDOSE_NUM_SLOTS` does not match the device's `slots=` |
| Two pills per drop | Pocket/shuttle too big or `DROP_OPEN_MS` too long (section 9.3). The firmware cannot count pills. |
| `ERR NO_PILL` although a pill came out | Sensor too far below the release (pill crosses after the close: raise `DROP_OPEN_MS`), beam wider than the pill, sensor polarity. `(beam never clear)` in the debug line or `beam INTERRUPTED` at boot = polarity, misalignment or something stuck in the chute. |
| `ERR NO_PILL` on every drop | `HAS_DROP_SENSOR 1` but no sensor, no pull-up or no power: GPIO 34 has no internal pull-up. |
| `OK DROPPED` but nothing came out | No drop sensor fitted (`drop_sensor=0`: the device cannot tell), a jammed wheel, or wrong servo angles. Fit the sensor. |
| Wrong container / slowly drifting position (carousel) | Lost steps: current limit too low (or too high -> thermal shutdown), speed/accel too high, binding carousel, loose grub screw/belt, `MICROSTEPS` not matching the jumpers, `GEAR_RATIO` wrong. `HOME` restores the reference; recalibrate `HOME_OFFSET_STEPS`. |
| Motor hums/vibrates but does not turn | NEMA17 coil pairs mixed up; 28BYJ-48 with the wrong `DRIVER_TYPE`; current too low; speed too high. |
| `ERR HOME_TIMEOUT` at boot | Sensor polarity (`HOME_SENSOR_ACTIVE_LOW`), gap/magnet pole (A3144 = south pole), wiring/pull-up, `HOMING_DIR`, carousel jammed, homing too slow for `HOME_TIMEOUT_MS`. `# home: ...` debug lines tell which phase failed. |
| `ERR MOTOR_FAULT` | A move did not finish within 2 x expected + 2 s. Usually `loop()` blocked by added code (never use `delay()`), or the position check (`VERIFY_SLOT_WITH_HOME_SENSOR`) failed after lost steps. Clear the cause, then `HOME`. |
| A servo twitches at power-up | Normal for hobby servos when power arrives. Design the release so a twitch cannot let a pill go; never use GPIO 0/2/5/12/14/15 for servos. |
| Board does not boot / boot loops with the wiring attached | Something pulls a strapping pin (0, 2, 5, 12, 15) at reset, or uses GPIO 6-11. Fix the pin plan. |
| Garbage characters at power-up | ESP32 ROM boot messages (`ets Jun 8 2016 ... rst:0x1`). Harmless: the host ignores non-protocol lines. |
| No reply at all | Baud 115200, line ending must be `\n` (Serial Monitor "Newline"), right COM port, nothing else (Serial Monitor) holding the port. |

## 14. Physical safety

Use candy/tokens only. Cover pinch points, gears and the servo horns. Keep fingers out of the
carousel and the chute while it moves (the host announces drops). The release defaults to closed
after any reset. `STOP` and the CANCEL button stop motion immediately. A release that has already
started finishes in ~1.3 s and closes again. Do not claim pill counting or medical-grade safety: the
drop sensor detects "something fell", not "the right pill fell".
