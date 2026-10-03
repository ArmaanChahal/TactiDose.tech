# TactiDose.tech — Hardware + Software Engineering Handoff

**Hackathon prototype | Assistive MedTech | Target user: blind / low-vision users**

> This document is designed to be handed to a hardware teammate or pasted into a fresh ChatGPT conversation. It describes the product, hardware responsibilities, software/hardware contract, physical mechanism, state machine, integration plan, failure handling, test plan, and MVP scope.

---

## 1. Project in One Sentence

**TactiDose.tech is a voice-first, motorized medication-access prototype that helps a blind or low-vision user access the correct pre-configured medication compartment at the correct scheduled time, confirm that it was taken, and prevent accidental duplicate dispensing.**

The system does **not** diagnose, prescribe, recommend dosage, or autonomously change medication instructions.

For the hackathon demo, use **candy, empty containers, labeled tokens, or other non-medication objects**.

---

## 2. The User Problem

Medication management can depend heavily on visual information:

- Medication containers can look or feel similar.
- Labels may contain small print.
- A user may know they have a medication due but still need to identify the correct physical container.
- Reminder apps can tell someone *what* to take without helping them physically locate the correct medication.
- A user may forget whether a scheduled dose was already accessed.
- Caregivers may want a simple adherence log without constantly supervising the person.

TactiDose adds a physical layer:

**voice command → schedule lookup → correct compartment selected → motorized positioning → controlled access → confirmation → event log**

The main hardware value is that the device moves the **correct pre-configured compartment to a single accessible opening** instead of requiring the user to visually search among multiple medications.

---

## 3. Core Product Boundary

### TactiDose SHOULD do

1. Store a pre-confirmed medication schedule.
2. Determine which scheduled compartment is due.
3. Rotate a carousel to the correct compartment.
4. Unlock/open one access gate.
5. Speak status and instructions.
6. Accept simple offline voice commands.
7. Record that a dose was accessed or confirmed.
8. Refuse a duplicate dispense for the same scheduled event.
9. Allow medication information to be scanned and extracted, but require confirmation before activation.
10. Produce adherence events for analytics.

### TactiDose MUST NOT do

- Diagnose a disease.
- Decide what medication a person should take.
- Invent or modify a dosage.
- Change a prescription based on an AI answer.
- Directly allow an LLM to control a motor.
- Treat speech recognition output as sufficient authorization for an unsafe action.
- Claim to be a certified medical device.
- Use real medication in the hackathon demo.

---

## 4. Intended User Experience

### 4.1 Normal dispensing flow

A user approaches the device and says:

> “What do I take now?”

The system:

1. Recognizes the command locally/offline.
2. Checks the current schedule in the application database.
3. Finds the due scheduled event.
4. Checks that the event has not already been dispensed.
5. Determines the assigned physical compartment.
6. Sends a deterministic command to the ESP32.
7. ESP32 rotates the carousel.
8. ESP32 verifies/assumes target position according to the chosen sensing method.
9. Servo opens or unlocks the access gate.
10. Audio says the medication is ready.
11. User removes the demo item.
12. User says “Taken” or presses a tactile confirmation button.
13. The event is stored as confirmed.

### 4.2 Duplicate-dose flow

If the user asks again for the same scheduled dose:

1. Backend looks up the scheduled event.
2. Event is already `DISPENSED` or `TAKEN`.
3. Backend sends **no motor command**.
4. Device says: “That scheduled dose has already been accessed.”

### 4.3 Medication onboarding flow

A caregiver/user shows a medication label to a camera.

1. Camera image is sent to Gemini.
2. Gemini extracts visible information into a structured object.
3. The application presents the extracted name/strength/instructions.
4. User/caregiver must explicitly confirm or correct it.
5. Only confirmed information is saved.
6. A compartment is assigned.
7. The physical compartment is loaded manually for the prototype.

Gemini assists with **transcription/data entry**, not prescribing.

---

## 5. Physical Concept

### Recommended MVP mechanism: rotating carousel

Use a circular disk divided into **6 compartments**.

```text
                    USER
                     ↓
              ┌─────────────┐
              │ ACCESS GATE │  ← servo-controlled
              └──────┬──────┘
                     │

                [ Compartment 1 ]
          [ 6 ]                     [ 2 ]

       [ 5 ]         HUB          [ 3 ]

                [ Compartment 4 ]

                     ●
               Stepper shaft
```

The stepper rotates the carousel until the desired compartment is aligned with the fixed access opening.

The servo should **not** rotate the entire carousel. Its only job should be a simple gate/latch.

### Why a carousel?

- One actuator can select multiple compartments.
- It produces an obvious physical demo.
- It reduces visual dependence.
- It is easier to control than a robot arm.
- The software can map `compartment_id → angular position`.

---

## 6. Recommended Hardware Architecture

### Primary controller

**ESP32**

Responsibilities:

- Stepper driver signals
- Servo signal
- Optional home/position sensor
- Tactile buttons
- Optional touchscreen
- Buzzer/status LED if useful
- USB serial or Wi-Fi communication with host computer
- Local emergency stop / cancel behavior

### Host computer

Use the team laptop for:

- Python backend
- Offline speech recognition
- Gemini API calls
- ElevenLabs API calls
- TiDB
- Snowflake integration
- Web/caregiver UI
- Camera input
- High-level scheduling logic

### Other boards

Arduino Uno/Nano/Mega and Raspberry Pi Pico are **not required** for the MVP.

Do not add boards just to use available hardware. Add a second MCU only if a real integration problem requires it.

---

## 7. Hardware Components

### Required

- ESP32 development board
- 1 stepper motor
- Matching stepper driver
- 1 servo
- External power supply appropriate for motor + servo
- Carousel structure
- 4–6 physical compartments
- Fixed access opening / chute / gate
- USB cable or Wi-Fi connection
- Laptop microphone or external microphone
- Speaker
- Camera/webcam for onboarding
- At least 1 large tactile button

### Strongly recommended

- Home-position sensor:
  - limit switch, or
  - Hall sensor + magnet, or
  - optical interrupter
- Second tactile button for cancel/help
- Large emergency stop or software disable switch
- Status LED or buzzer for development/testing
- Bearings / low-friction rotating surface if available

### Optional

- Touchscreen display
- Compartment-open sensor
- Item-present sensor
- Load cell
- Encoder

Do not add optional sensors unless the basic carousel is already reliable.

---

## 8. Stepper Driver Depends on the Motor You Own

The exact wiring cannot be specified until the stepper type is known.

### If the motor is a typical bipolar NEMA stepper

Likely use:

- A4988
- DRV8825
- TMC-series driver

ESP32 sends signals such as:

- `STEP`
- `DIR`
- optional `ENABLE`

### If the motor is a 28BYJ-48

It commonly uses a ULN2003 driver board and four control lines.

### Important electrical rule

**Do not power the stepper or normal servo from the ESP32 3.3 V pin.**

Use a suitable external motor/servo power source and follow the driver’s voltage/current requirements.

The controller and motor power system generally need a **common reference/ground** where required by the chosen driver arrangement.

Before final wiring, the hardware teammate should provide:

- stepper model/photo
- driver model/photo
- servo model
- touchscreen model
- available power supply voltage/current

Then exact pin assignments can be finalized.

---

## 9. Mechanical Design

### 9.1 Carousel

For 6 compartments:

- Full circle: 360°
- Spacing: `360 / 6 = 60°`
- Every logical slot is one 60° sector.

Logical mapping:

```text
Slot 0 →   0°
Slot 1 →  60°
Slot 2 → 120°
Slot 3 → 180°
Slot 4 → 240°
Slot 5 → 300°
```

The real number of motor steps depends on:

- motor steps/revolution
- microstepping
- gear ratio
- belt/pulley ratio if used

### Generic formula

```text
motor_steps_for_rotation =
    motor_steps_per_revolution
    × microstep_factor
    × gear_ratio
    × desired_angle / 360
```

Example only:

```text
200 step/rev motor
16× microstepping
1:1 drive

steps per full carousel revolution = 200 × 16 = 3200
steps per 60° compartment = 3200 / 6 ≈ 533.33
```

A fractional slot step count is undesirable if accumulated repeatedly.

Better options:

1. Choose mechanical gearing/microstepping that produces convenient positions.
2. Track absolute target positions and compensate.
3. Use a home sensor and periodic re-homing.
4. Use a sensor/encoder if available.

For a hackathon, **home sensor + absolute logical position** is a good balance.

---

## 10. Homing

Without homing, the system does not know which compartment is physically at the access gate after boot.

Recommended startup:

```text
BOOT
 ↓
Gate closes
 ↓
Stepper rotates slowly toward HOME
 ↓
Home sensor activates
 ↓
Stop
 ↓
Set current_position = 0
 ↓
READY
```

### Homing rules

- Home at low speed.
- Set a maximum homing travel/time.
- If the sensor is never triggered, enter `FAULT`.
- Never open the gate while homing.
- Optionally back away and re-approach slowly for repeatability.

If you cannot add a home sensor, you can manually align the carousel before powering on, but that is less reliable and should be treated as an MVP fallback.

---

## 11. Servo / Access Gate

The servo should control a **simple binary mechanical state**:

```text
LOCKED/CLOSED
OPEN
```

Avoid a complicated servo mechanism.

Example conceptual positions:

```text
SERVO_CLOSED = 20°
SERVO_OPEN   = 90°
```

These values are placeholders and must be calibrated mechanically.

Recommended behavior:

1. Close gate before any carousel motion.
2. Rotate carousel.
3. Stop motor.
4. Wait briefly for mechanical settling.
5. Open gate.
6. Keep open until:
   - confirmation/button event,
   - user cancel,
   - timeout,
   - or demo operator action.
7. Close gate.

Never rotate the carousel while the gate is physically interfering with a compartment.

---

## 12. Hardware State Machine

The ESP32 should have a small deterministic state machine.

```text
BOOT
  ↓
HOMING
  ↓
READY
  ↓
MOVING
  ↓
AT_TARGET
  ↓
GATE_OPEN
  ↓
READY
```

Fault transitions:

```text
HOMING  ──error──> FAULT
MOVING  ──error──> FAULT
ANY     ──STOP───> SAFE_STOP
```

Recommended states:

- `BOOT`
- `HOMING`
- `READY`
- `MOVING`
- `GATE_OPEN`
- `SAFE_STOP`
- `FAULT`

### Critical rule

**The ESP32 should reject logically invalid commands.**

Examples:

- `OPEN_GATE` while `MOVING` → reject
- `MOVE_SLOT` while gate is open → close first or reject
- second movement command while already moving → reject
- unknown slot number → reject

---

## 13. Software-to-Hardware Boundary

Do not let Gemini or any LLM produce raw GPIO/motor commands.

Use this architecture:

```text
User
  ↓
Offline speech recognition
  ↓
Python intent parser
  ↓
Application safety checks
  ↓
Medication schedule / TiDB
  ↓
Deterministic hardware command
  ↓
ESP32
  ↓
Stepper + Servo
```

Example:

User says:

> “What do I take now?”

The LLM should **not** return:

```text
rotate motor 180 degrees
open servo
```

Instead, the backend resolves:

```text
scheduled_event_id = dose_184
compartment_id = 3
allowed_to_dispense = true
```

Only then does deterministic software issue:

```text
DISPENSE_SLOT 3
```

---

## 14. Recommended Serial Command Protocol

For the hackathon, USB serial is easier to debug than a custom wireless protocol.

Use one text command per line.

### Host → ESP32

```text
PING
HOME
STATUS
MOVE_SLOT 0
MOVE_SLOT 1
MOVE_SLOT 2
MOVE_SLOT 3
MOVE_SLOT 4
MOVE_SLOT 5
OPEN_GATE
CLOSE_GATE
DISPENSE_SLOT 3
STOP
```

`DISPENSE_SLOT n` can be implemented as an ESP32 macro:

```text
verify gate closed
→ move to slot n
→ report position reached
→ open gate
```

### ESP32 → Host

```text
OK PONG
OK HOMED
OK MOVING 3
OK AT_SLOT 3
OK GATE_OPEN
OK GATE_CLOSED
OK READY
ERR INVALID_SLOT
ERR NOT_HOMED
ERR BUSY
ERR HOME_TIMEOUT
ERR MOTOR_FAULT
ERR INVALID_STATE
EVENT CONFIRM_BUTTON
EVENT CANCEL_BUTTON
```

### Why text commands?

- Easy to inspect in Serial Monitor.
- Easy to send from Python.
- Easy to test manually.
- Easy to debug during a hackathon.

Add JSON later only if necessary.

---

## 15. Example Python/ESP32 Interaction

Python conceptual flow:

```python
event = database.get_due_event(user_id)

if event is None:
    speak("You do not have a scheduled medication due right now.")
    return

if event.status in {"DISPENSED", "TAKEN"}:
    speak("That scheduled dose has already been accessed.")
    return

slot = event.compartment_id

serial.send(f"DISPENSE_SLOT {slot}\n")

response = wait_for("OK GATE_OPEN", timeout=10)

if response:
    database.mark_dispensed(event.id)
    speak("Your scheduled medication is ready.")
else:
    database.mark_hardware_error(event.id)
    speak("I could not prepare the compartment. Please ask for assistance.")
```

The backend decides **whether** dispensing is allowed.

The ESP32 decides **how** to move hardware safely.

---

## 16. Offline Voice Recognition

### Recommended hackathon architecture

Run speech recognition on the laptop, not on the ESP32.

```text
Microphone
  ↓
Offline speech-to-text
  ↓
Python
  ↓
Intent classification
```

Possible offline engine:

- Vosk

Keep the command vocabulary small.

### Minimum voice intents

```text
"What do I take now?"
"Dispense."
"Taken."
"Repeat."
"Cancel."
"Help."
```

Internally normalize them to:

```text
CHECK_DUE
DISPENSE
CONFIRM_TAKEN
REPEAT
CANCEL
HELP
```

For the MVP, deterministic keyword/intent matching is preferable to sending safety-critical commands through a generative model.

---

## 17. Audio Output

### ElevenLabs

Use ElevenLabs for natural spoken responses such as:

- “Your scheduled medication is ready.”
- “That dose has already been accessed.”
- “I detected a new medication label. Please review it before saving.”

### Offline fallback

Cache several critical prompts locally:

- “Cancelled.”
- “Please ask for assistance.”
- “That scheduled dose was already accessed.”
- “Network unavailable.”
- “Hardware error.”

This allows the core interaction to remain understandable if internet access is unstable.

---

## 18. Gemini Role

Gemini should be used where multimodal reasoning is actually useful.

### Good use

Input:

- photograph of medication label

Output schema:

```json
{
  "medication_name": "...",
  "strength": "...",
  "visible_instructions": "...",
  "warnings_visible": ["..."],
  "confidence_notes": "..."
}
```

Then show the information to the user/caregiver for confirmation.

### Bad use

Do NOT ask:

> “Based on this user’s condition, what should they take?”

Do NOT let Gemini autonomously convert uncertain text into an active dispensing schedule.

### Activation rule

```text
Gemini extraction
      ↓
UNCONFIRMED record
      ↓
Human review
      ↓
CONFIRMED record
      ↓
Eligible for scheduling
```

---

## 19. Suggested Data Model

### User

```text
user_id
display_name
accessibility_preferences
voice_enabled
created_at
```

### Medication

```text
medication_id
user_id
name
strength
instructions_text
source
confirmed_by_user
created_at
```

### Compartment

```text
compartment_id
device_id
slot_number
medication_id
active
```

### Schedule

```text
schedule_id
medication_id
time_of_day
frequency
active
```

### DoseEvent

```text
event_id
schedule_id
scheduled_at
compartment_id
status
dispensed_at
confirmed_taken_at
hardware_result
```

Possible statuses:

```text
SCHEDULED
DUE
DISPENSING
DISPENSED
TAKEN
MISSED
CANCELLED
HARDWARE_ERROR
```

---

## 20. TiDB vs Snowflake

Use them for different jobs.

### TiDB = operational/live application state

Use TiDB for:

- users
- medications
- compartments
- schedules
- current dose events
- immediate duplicate-dispense checks

The hardware/backend needs fast access to this state.

### Snowflake = historical analytics

Send de-identified/adherence events such as:

```text
scheduled_at
dispensed_at
confirmed_taken_at
delay_minutes
missed
device_id
```

Possible analytics:

- most frequently missed time window
- average delay between schedule and confirmation
- adherence trend by day
- device error frequency

Do not make both databases store the exact same information for no reason.

---

## 21. Touchscreen Role

The blind user should not be required to use the touchscreen.

Use it mainly for:

- setup
- caregiver/admin mode
- reviewing Gemini extraction
- assigning compartments
- changing schedules
- showing device status
- fallback controls

If a low-vision user interacts with it:

- very large text
- high contrast
- few buttons per screen
- large touch targets
- no color-only state indication
- spoken feedback
- clear cancel/back action

Example:

```text
┌──────────────────────────────┐
│        DEVICE READY          │
│                              │
│ NEXT EVENT: 2:00 PM          │
│ SLOT: 3                      │
│                              │
│ [ REPEAT ]      [ HELP ]     │
│                              │
│ [ CAREGIVER / SETUP ]        │
└──────────────────────────────┘
```

---

## 22. Suggested Hardware Pin Plan

**Do not treat this as final wiring.** It is only a planning template because the actual stepper driver and touchscreen are not yet specified.

Example logical assignments:

```text
GPIO_STEP
GPIO_DIR
GPIO_ENABLE
GPIO_SERVO
GPIO_HOME_SENSOR
GPIO_CONFIRM_BUTTON
GPIO_CANCEL_BUTTON
```

If the touchscreen uses SPI/I2C/UART, reserve the required pins before choosing final motor pins.

The hardware teammate should first identify:

1. exact ESP32 variant
2. exact touchscreen/controller
3. exact stepper driver
4. exact servo
5. home sensor type

Then produce a real wiring diagram.

---

## 23. Power Design

This is one of the most common hackathon failure points.

### Rules

- Do not drive a stepper directly from GPIO.
- Do not power a stepper from the ESP32 regulator.
- Avoid powering a normal servo from the ESP32 3.3 V rail.
- Use the correct motor driver.
- Use a supply that can handle motor/servo current.
- Account for servo current spikes.
- Keep logic and motor power wiring organized.
- Provide common ground/reference where the driver/control scheme requires it.
- Test motors individually before connecting the full mechanism.

A mechanically jammed carousel can cause a stepper to skip steps and lose its logical position, which is why homing is valuable.

---

## 24. Physical Safety / Prototype Constraints

Because this is a hackathon prototype:

- Use candy or labeled tokens, not real pills.
- Make the access opening large enough for easy retrieval.
- Cover exposed gears/rotating pinch points where possible.
- Keep fingers away from the carousel during motion.
- Add `STOP` behavior.
- Gate should default closed after reset where mechanically possible.
- Do not claim reliable pill counting unless you actually measure it.
- Do not claim medical-device-grade safety.

---

## 25. MVP Definition

The hardware MVP is complete when all of this works reliably:

### Demo Flow A — Scheduled dispense

```text
1. System homes.
2. User command is recognized.
3. Backend selects slot 3.
4. ESP32 receives DISPENSE_SLOT 3.
5. Gate is confirmed closed.
6. Carousel rotates to slot 3.
7. Gate opens.
8. Backend records DISPENSED.
9. User confirms TAKEN.
10. Backend records TAKEN.
```

### Demo Flow B — Duplicate prevention

```text
1. Same scheduled event is requested again.
2. Backend sees TAKEN.
3. No hardware movement occurs.
4. System speaks duplicate warning.
```

### Demo Flow C — Label onboarding

```text
1. Camera scans demo label.
2. Gemini extracts visible medication data.
3. UI shows UNCONFIRMED information.
4. Human confirms.
5. Medication is saved.
6. User assigns a compartment.
```

If these three flows work, stop adding features and polish the demonstration.

---

## 26. What NOT to Build Before the MVP Works

Do not spend time on:

- facial recognition
- custom medical model training
- autonomous prescription recommendations
- robot arms
- automatic pill counting
- mobile app + web app + touchscreen app simultaneously
- multiple microcontrollers
- complicated RAG
- voice biometrics
- computer-vision pill identification
- automatic bottle loading
- custom PCB

The rotating carousel must be boringly reliable first.

---

## 27. 4-Person Team Split

### Person 1 — Hardware / Embedded

Own:

- carousel
- motor driver
- servo gate
- homing
- ESP32 firmware
- serial protocol
- tactile buttons

### Person 2 — Python / Voice / AI

Own:

- FastAPI or Python backend
- offline speech recognition
- intent parsing
- Gemini onboarding
- ElevenLabs output
- serial client

### Person 3 — Data / Cloud

Own:

- TiDB schema
- scheduling/event logic
- duplicate prevention
- Snowflake event sync
- analytics query

### Person 4 — Frontend / Accessibility / Integration

Own:

- caregiver setup UI
- touchscreen/web UI
- high-contrast design
- demo control panel
- integration testing
- submission/presentation support

Everyone joins integration once the first hardware API is stable.

---

## 28. 24-Hour Integration Plan

### Hours 0–2

Freeze:

- 6-slot carousel
- host↔ESP32 command protocol
- basic database fields
- demo flows
- physical dimensions

Hardware person immediately prototypes motion.

### Hours 2–6

Hardware:

- stepper spins reliably
- servo gate moves
- carousel prototype exists
- serial `MOVE_SLOT` works

Software:

- offline voice works
- Python sends test serial commands
- DB schema exists
- basic UI exists

### Hours 6–9

First end-to-end integration:

```text
Python → DISPENSE_SLOT 3 → ESP32 → stepper → gate
```

Do this **before** advanced sponsor integrations.

### Hours 9–14

Add:

- due-event logic
- duplicate prevention
- Gemini extraction
- ElevenLabs
- TiDB

### Hours 14–18

Add:

- Snowflake analytics
- accessible UI
- physical polish
- home sensor if not already done

### Hours 18–21

Stress-test:

- reboot
- wrong slot
- repeated command
- gate open
- no internet
- serial disconnect
- motor timeout
- home failure

### Hours 21–24

Feature freeze.

Only:

- fix bugs
- improve enclosure
- prepare demo
- record backup demo video
- complete submission

---

## 29. Hardware Test Checklist

### Stepper only

- [ ] Motor rotates both directions.
- [ ] Motor driver does not overheat immediately.
- [ ] Correct current limit / driver setup.
- [ ] 10 repeated slot movements are consistent.
- [ ] Carousel does not bind mechanically.

### Homing

- [ ] Home sensor triggers reliably.
- [ ] Boot sequence finds home.
- [ ] Homing timeout produces `FAULT`.
- [ ] Re-home returns to same physical point.

### Servo

- [ ] Gate fully closes.
- [ ] Gate opens enough for retrieval.
- [ ] Servo does not block carousel rotation.
- [ ] Power supply remains stable when servo moves.

### Integration

- [ ] `PING`
- [ ] `HOME`
- [ ] `STATUS`
- [ ] every `MOVE_SLOT`
- [ ] `DISPENSE_SLOT`
- [ ] `STOP`
- [ ] confirm button
- [ ] cancel button
- [ ] unexpected serial command

### End-to-end

Run the complete demo **10 times**.

If one of ten attempts fails, treat it as a real problem.

---

## 30. Failure Handling

### Internet failure

Core deterministic dispensing should still be able to work if the schedule is cached/local.

Gemini/ElevenLabs can be unavailable without physically breaking the device.

### Gemini failure

Do not activate a medication record.

Show:

```text
Could not reliably read label.
Please enter or verify information manually.
```

### Speech failure

Fallback:

- large tactile button
- touchscreen button
- keyboard/demo operator control

### Motor/homing failure

- close gate if possible
- stop motion
- set hardware state to `FAULT`
- do not mark event as successfully dispensed
- tell user to request assistance

### Database failure

Do not repeatedly dispense an event when state cannot be safely determined.

For demo purposes, fail closed rather than guessing.

---

## 31. Suggested Firmware Pseudocode

```cpp
setup() {
    initPins();
    closeGate();
    initSerial();
    state = BOOT;
    homeCarousel();
}

loop() {
    readButtons();
    readSerialCommands();

    if (cancelPressed()) {
        emergencyStop();
    }
}

handleCommand(cmd) {
    if (cmd == "PING") {
        reply("OK PONG");
    }

    else if (cmd == "HOME") {
        requireGateClosed();
        homeCarousel();
    }

    else if (cmd startsWith "MOVE_SLOT") {
        requireState(READY);
        requireGateClosed();
        moveToSlot(parseSlot(cmd));
    }

    else if (cmd startsWith "DISPENSE_SLOT") {
        requireState(READY);
        closeGate();
        moveToSlot(parseSlot(cmd));
        openGate();
    }

    else if (cmd == "CLOSE_GATE") {
        closeGate();
    }

    else if (cmd == "STOP") {
        emergencyStop();
    }

    else {
        reply("ERR UNKNOWN_COMMAND");
    }
}
```

This is conceptual. The final implementation depends on the exact stepper library/driver.

---

## 32. Suggested Host-Side Module Structure

```text
tactidose/
│
├── app.py
├── config.py
│
├── voice/
│   ├── recognizer.py
│   └── intents.py
│
├── hardware/
│   ├── serial_client.py
│   └── commands.py
│
├── medication/
│   ├── scheduler.py
│   ├── safety.py
│   └── onboarding.py
│
├── integrations/
│   ├── gemini.py
│   ├── elevenlabs.py
│   ├── tidb.py
│   └── snowflake.py
│
└── ui/
    └── ...
```

Keep the hardware client independent from Gemini.

---

## 33. One Important Architectural Rule

The system should have **two different kinds of intelligence**:

### Probabilistic / AI

Good for:

- reading labels
- understanding natural language
- generating natural audio
- summarizing analytics

### Deterministic

Required for:

- deciding whether a scheduled event is eligible
- duplicate prevention
- slot mapping
- motor commands
- gate interlocks
- device states

**AI can interpret. Deterministic code authorizes and actuates.**

---

## 34. Questions the Hardware Teammate Must Answer Next

Before final circuit/wiring design, answer:

1. What exact stepper motor do we have?
2. What exact driver board do we have?
3. What servo model do we have?
4. What ESP32 board/version do we have?
5. What touchscreen model/controller do we have?
6. What power supplies do we have?
7. Do we have a limit switch, Hall sensor, reed switch, or optical sensor?
8. Can we laser-cut / 3D-print / use cardboard / foamboard / acrylic?
9. How many compartments can we mechanically build reliably?
10. Is the carousel directly coupled to the motor or belt/geared?

The answers determine the final wiring and motion calculations.

---

# 35. Copy/Paste Prompt for the Hardware Teammate’s ChatGPT

Copy everything below into a new chat **after attaching this document if possible**:

---

**PROMPT START**

We are building a 24-hour hackathon prototype called **TactiDose.tech**.

It is a voice-first motorized medication-access prototype for blind/low-vision users. A pre-confirmed schedule maps medications to physical carousel compartments. The backend decides which compartment is due; the ESP32 only performs deterministic physical operations.

Our intended mechanism is a rotating carousel with approximately 6 compartments. A stepper motor rotates the carousel. A servo controls one fixed access gate. We want a home-position sensor if possible.

The laptop handles Python, offline speech recognition, Gemini, ElevenLabs, TiDB, Snowflake, and scheduling. ESP32 handles stepper, servo, home sensor, buttons, and serial communication.

The planned serial interface is roughly:

Host → ESP32:
- PING
- HOME
- STATUS
- MOVE_SLOT n
- DISPENSE_SLOT n
- OPEN_GATE
- CLOSE_GATE
- STOP

ESP32 → Host:
- OK PONG
- OK HOMED
- OK MOVING n
- OK AT_SLOT n
- OK GATE_OPEN
- OK GATE_CLOSED
- OK READY
- ERR INVALID_SLOT
- ERR NOT_HOMED
- ERR BUSY
- ERR HOME_TIMEOUT
- ERR INVALID_STATE
- EVENT CONFIRM_BUTTON
- EVENT CANCEL_BUTTON

Safety/interlock rule:
- carousel must not move while gate is open
- gate is closed before motion
- device must home on startup if sensor exists
- motor failure must not be recorded as successful dispensing
- no AI/LLM is allowed to directly control GPIO or motor motion
- use only candy/tokens for the hackathon demo, not actual medication

I need you to help specifically with **hardware and embedded engineering**.

First ask me for / inspect these exact hardware details before giving final wiring:
1. ESP32 model
2. stepper motor model
3. stepper driver model
4. servo model
5. touchscreen model
6. available power supply
7. available home/limit sensors
8. materials/tools available for the carousel

Then help me produce:
1. final wiring diagram
2. safe power architecture
3. ESP32 pin map
4. carousel geometry
5. steps-per-slot calculation
6. homing logic
7. servo gate geometry
8. ESP32 firmware
9. serial command parser
10. hardware test procedure
11. fault-handling strategy
12. fastest build order for a 24-hour hackathon

Prioritize reliability and simplicity over adding more features. Do not assume a motor driver or voltage that I have not provided.

**PROMPT END**

---

## 36. Definition of Success

At the end of the hackathon, the project does **not** need to look like a commercial medical appliance.

It needs to reliably demonstrate:

```text
VOICE / USER ACTION
        ↓
PRE-CONFIRMED SCHEDULE
        ↓
DETERMINISTIC SAFETY CHECK
        ↓
CORRECT PHYSICAL COMPARTMENT
        ↓
STEPPER MOVEMENT
        ↓
SERVO ACCESS
        ↓
USER CONFIRMATION
        ↓
EVENT LOG
```

If this chain works every time, the core engineering thesis is proven.

---

## 37. Final Build Priority

If time becomes tight, prioritize in this exact order:

1. **Stepper moves repeatably**
2. **Gate opens/closes reliably**
3. **Homing or reliable startup alignment**
4. **Python can command ESP32**
5. **Backend correctly maps a due event to a slot**
6. **Duplicate dispense is blocked**
7. **Offline voice triggers the same deterministic flow**
8. **Gemini onboarding**
9. **ElevenLabs voice polish**
10. **TiDB/Snowflake sponsor integrations**
11. **Touchscreen polish**
12. **Extra sensors/features**

A plain-looking machine that works ten times in a row is much better than a beautiful machine that jams during the demo.
