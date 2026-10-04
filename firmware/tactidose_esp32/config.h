/*
 * config.h -- ALL tunables of the TactiDose REFERENCE firmware (pins, mechanics, motion, gate, timing).
 *
 * Hackathon prototype, NOT a medical device: demo with candy / tokens only.
 *
 * >>> PINS ARE PLACEHOLDERS <<< until the hardware questions of the handoff (§34) are answered:
 *     exact ESP32 board, stepper + driver, servo, touchscreen (and which pins it needs), power
 *     supply, home-sensor type, direct drive or belt/gears. Wiring templates, the pin plan and the
 *     calibration procedure are in docs/HARDWARE_INTEGRATION.md.
 *
 * Classic ESP32 (ESP32-WROOM-32 DevKit) pin rules -- ArduinoHal.cpp enforces the hard ones at
 * compile time:
 *   - NEVER use GPIO 6-11: they are wired to the SPI flash (the board stops booting).
 *   - GPIO 34-39 are INPUT-ONLY and have NO internal pull-ups: never outputs; inputs need an
 *     external pull-up (10 kOhm to 3.3 V) unless the sensor drives the line itself.
 *   - Avoid the strapping pins 0, 2, 5, 12, 15 for critical outputs (STEP/DIR/ENABLE/coils/servo):
 *     they are sampled at reset and some toggle during boot (motor twitch, failed boot; GPIO 12
 *     pulled HIGH at reset selects 1.8 V flash and the board will not boot).
 *   - GPIO 1/3 are the USB serial port (TX0/RX0) used by the host link: never use them.
 *   - GPIO 14/15 output a PWM signal while booting: do not use them for motor signals.
 *   - GPIO 16/17 do not exist on WROVER modules (PSRAM). 18/19/23 (+5) are the default SPI and
 *     21/22 the default I2C pins: keep them free if the touchscreen needs them.
 *   - Other variants (ESP32-S3/C3/C6...) have different rules -- re-check the pin plan.
 *
 * Tunables marked [host] must match the host settings (tactidose/config.py, .env).
 */
#ifndef TACTIDOSE_CONFIG_H
#define TACTIDOSE_CONFIG_H

/* ================================================================ identity */

/* Reported in "EVENT BOOT <fw>" and "STATUS ... fw=<fw>". No spaces. Bump when behaviour changes. */
#define FW_VERSION "1.0.0-ref"

/* USB serial link: 115200 8N1 (SERIAL_PROTOCOL.md §1). [host] TACTIDOSE_SERIAL_BAUD */
#define SERIAL_BAUD 115200

/* "# ..." debug lines on the serial port (the host logs and ignores them). 0 = silent. */
#define DEBUG_LOG 1

/* 1 = let ArduinoHal.cpp accept strapping pins (0, 2, 5, 12, 15) for motor/servo signals. Only if
 * there is no alternative and you have checked the pin's boot-time behaviour on your board. */
#define ALLOW_STRAPPING_PINS 0

/* ================================================================ carousel */

/* Number of compartments, 2..12. [host] TACTIDOSE_NUM_SLOTS (the device reports it in STATUS). */
#define NUM_SLOTS 6

/* ================================================================ stepper driver */

#define DRIVER_STEP_DIR 1 /* A4988 / DRV8825 / TMC2208 / TMC2209 (standalone STEP/DIR) + bipolar NEMA17 */
#define DRIVER_ULN2003 2  /* ULN2003 board + 28BYJ-48 (5 V unipolar), 4-wire half-step */
#ifndef DRIVER_TYPE       /* can be overridden from the build: -DDRIVER_TYPE=2 */
#define DRIVER_TYPE DRIVER_STEP_DIR
#endif

/* --- STEP/DIR driver pins (DRIVER_STEP_DIR) --- */
#define PIN_STEP 25
#define PIN_DIR 26
/* -1 if EN is hard-wired. Fit a 10 kOhm pull-up EN->3.3 V so the driver stays OFF while the ESP32
 * boots (A4988/DRV8825 pull EN low internally = enabled). */
#define PIN_ENABLE 27
#define ENABLE_ACTIVE_LOW 1 /* A4988, DRV8825, TMC2208/2209 standalone: EN low = driver on */
#define STEP_PULSE_US 3     /* STEP high time: A4988 >= 1 us, DRV8825 >= 1.9 us, TMC >= 0.1 us */

/* --- ULN2003 pins (DRIVER_ULN2003): ESP32 GPIO -> IN1..IN4 on the driver board --- */
#define PIN_IN1 25
#define PIN_IN2 26
#define PIN_IN3 27
#define PIN_IN4 33

/* 1 = reverse the rotation direction. This also reverses the homing direction and the direction
 * in which compartments 1..N are counted, so re-check both after changing it. */
#define DIR_INVERT 0

/* ================================================================ steps per carousel revolution */

#if DRIVER_TYPE == DRIVER_STEP_DIR
/* 1.8 deg motor = 200 full steps (0.9 deg = 400). */
#define MOTOR_FULL_STEPS_PER_REV 200.0f
/* Driver microstep setting (MS1..MS3 jumpers): A4988 1/2/4/8/16, DRV8825 up to 32, TMC2209
 * standalone default 8. Must match the jumpers or every move is off by that factor. */
#define MICROSTEPS 16
#else
/* 28BYJ-48 output shaft: 32 steps x ~64:1 gearbox = 2048 full steps, driven in half-steps
 * (x2) = 4096 steps per revolution. The real gearbox is 63.684:1 (2037.886 full steps); use
 * 2037.886f if the carousel drifts after several turns. */
#define MOTOR_FULL_STEPS_PER_REV 2048.0f
#define MICROSTEPS 2
#endif

/* Motor-shaft turns per carousel turn (belt or gears). 1.0 = carousel on the motor shaft;
 * 20T pulley on the motor driving a 60T pulley on the carousel = 3.0. */
#define GEAR_RATIO 1.0f

/* Derived: steps for one full carousel revolution. Slot k target = round(k * this / NUM_SLOTS). */
#define CAROUSEL_STEPS_PER_REV (MOTOR_FULL_STEPS_PER_REV * MICROSTEPS * GEAR_RATIO)

/* ================================================================ motion (steps/s, steps/s^2) */

#if DRIVER_TYPE == DRIVER_STEP_DIR
#define MAX_SPEED_SPS 1600.0f     /* 0.5 carousel rev/s at 3200 steps/rev */
#define ACCELERATION_SPS2 3200.0f /* reaches full speed in 0.5 s */
#define HOMING_SPEED_SPS 400.0f   /* slow first approach to the home sensor */
#define HOMING_SLOW_SPEED_SPS 100.0f /* re-approach after the back-off (repeatable edge) */
#else
#define MAX_SPEED_SPS 500.0f      /* a 28BYJ-48 stalls above ~600-1000 half-steps/s under load */
#define ACCELERATION_SPS2 500.0f
#define HOMING_SPEED_SPS 350.0f
#define HOMING_SLOW_SPEED_SPS 120.0f
#endif

/* Rule 8.6: a move that is not finished after FACTOR x expected time + MARGIN -> ERR MOTOR_FAULT. */
#define MOTION_TIMEOUT_FACTOR 2.0f
#define MOTION_TIMEOUT_MARGIN_MS 2000UL

/* 1 = keep the motor energised while idle (READY / SAFE_STOP) so the carousel cannot be pushed
 * out of position while someone reaches into the opening. 0 = release the coils when idle (cooler,
 * esp. 28BYJ-48) -- position may then drift; re-home more often. The motor is always energised
 * while moving and while the gate is open, and released in FAULT. */
#define STEPPER_HOLD_WHEN_IDLE 1

/* ================================================================ homing (rule 8.5) */

/* 0 = no home sensor (MVP fallback): align compartment 1 (slot 0) with the opening by hand before
 * power-on; HOME then just returns to step 0. Re-align and power-cycle if the carousel slips. */
#define HAS_HOME_SENSOR 1
/* Hall sensor (A3144 / KY-003, magnet on the carousel), lever micro-switch, or slotted optical
 * interrupter -- see docs/HARDWARE_INTEGRATION.md. */
#define PIN_HOME_SENSOR 32
/* 1: LOW = at home (A3144 open-collector, switch to GND, most opto modules). 0: HIGH = at home. */
#define HOME_SENSOR_ACTIVE_LOW 1
/* Internal pull-up on the sensor pin. Not available on GPIO 34-39 (use an external 10 kOhm). */
#define HOME_SENSOR_PULLUP 1
/* 1 = home automatically at power-on (rule 8.9). 0 = stay in BOOT until the host sends HOME. */
#define AUTO_HOME_ON_BOOT 1
/* +1 / -1: step direction used to seek the sensor. */
#define HOMING_DIR 1
/* Give up after this much travel without finding the sensor (1.25 rev per protocol) ... */
#define HOME_MAX_TRAVEL_REVS 1.25f
/* ... or after this long for the whole HOME, whichever comes first -> ERR HOME_TIMEOUT. */
#define HOME_TIMEOUT_MS 20000UL
/* After the first hit, back off this many steps and re-approach at HOMING_SLOW_SPEED_SPS for a
 * repeatable edge. 0 = single approach. Must exceed the width of the sensor's active zone. */
#define HOME_BACKOFF_STEPS 100
/* If the sensor is already active when homing starts, move away at most this far to release it;
 * a sensor that never releases is treated as broken (ERR HOME_TIMEOUT, FAULT). */
#define HOME_RELEASE_MAX_REVS 0.25f
/* Calibration: where the centre of compartment 1 (slot 0) is relative to the sensor edge, in
 * steps in the + direction. Found with MOVE_SLOT 0 + a ruler, see HARDWARE_INTEGRATION.md. */
#define HOME_OFFSET_STEPS 0
/* Sensor reading must be stable this long to count as an edge (hall/optical: a few ms; a
 * mechanical switch bounces for ~1-5 ms). */
#define HOME_DEBOUNCE_MS 10
/* 1 = extra position check: on every arrival the home sensor must be active exactly at slot 0
 * (catches lost steps). Enable only after calibration, when slot 0 sits inside the sensor zone. */
#define VERIFY_SLOT_WITH_HOME_SENSOR 0

/* ================================================================ gate servo */

#define PIN_SERVO 13
/* Calibrate mechanically (handoff §11). Closed must fully block the opening, open must not touch
 * the carousel. Keep them apart by at least ~30 deg. */
#define SERVO_CLOSED_DEG 20
#define SERVO_OPEN_DEG 90
/* Pulse range for 0..180 deg. SG90/MG90S: ~500-2400 us. Narrow it if the servo buzzes at the ends. */
#define SERVO_MIN_PULSE_US 500
#define SERVO_MAX_PULSE_US 2400
/* Time the servo needs to travel between closed and open (rule 8.2: <= 600 ms). OK GATE_OPEN /
 * OK GATE_CLOSED are sent only after this time; measure it and add a margin. */
#define GATE_TRAVEL_MS 400
/* DISPENSE_SLOT: wait this long after the carousel stops before opening the gate (rule 8.4). */
#define SETTLE_MS 300
/* Safety net (rule 8.7): the device closes the gate itself after this long (host closes it sooner). */
#define GATE_MAX_OPEN_MS 120000UL

/* ================================================================ buttons (rule 8.8) */

/* Big tactile CONFIRM button (required) and CANCEL/help button (-1 if not fitted). */
#define PIN_CONFIRM_BUTTON 4
#define PIN_CANCEL_BUTTON 16 /* WROVER modules have no GPIO 16: use e.g. 35 + external pull-up */
#define BUTTON_ACTIVE_LOW 1  /* button between the pin and GND */
#define BUTTON_PULLUP 1      /* internal pull-up (not on GPIO 34-39) */
#define DEBOUNCE_MS 30       /* >= 30 ms */

/* ================================================================ status LED (optional) */

/* -1 = none. Many DevKits have an LED on GPIO 2 (strapping pin: fine for an LED, never for motors).
 * Solid = READY / gate open, slow blink = moving / homing, fast blink = FAULT. */
#define PIN_STATUS_LED -1

/* ================================================================ host timeouts (checked at compile time) */

/* [host] TACTIDOSE_TIMEOUT_MOVE_S / TACTIDOSE_TIMEOUT_HOME_S in ms. The firmware must report
 * ERR MOTOR_FAULT / ERR HOME_TIMEOUT before the host gives up, otherwise the host only sees a
 * TIMEOUT (outcome uncertain -> caregiver review). ConfigCheck.h verifies this. */
#define HOST_MOVE_TIMEOUT_MS 20000UL
#define HOST_HOME_TIMEOUT_MS 45000UL

#endif  // TACTIDOSE_CONFIG_H
