# TactiDose Serial Protocol — v1.1 (frozen contract)

This document is the **single source of truth** for the host ↔ ESP32 interface.
The Python host (`tactidose/hardware/protocol.py`), the Python ESP32 simulator
(`tactidose/hardware/simulator.py`) and the reference firmware
(`firmware/tactidose_esp32/`) all implement exactly this behaviour, and the
shared conformance suite (`tactidose/hardware/conformance.json`) checks it.

It extends the handoff document §14 with the details needed for two teams to
integrate without guessing. Additions relative to the handoff are marked **(added)**.

---

## 1. Transport

| Item | Value |
|---|---|
| Link | USB serial (CDC or UART bridge). Wi-Fi is out of scope for v1. |
| Baud / framing | 115200 baud, 8N1, no flow control |
| Encoding | ASCII, one message per line |
| Host → device terminator | `\n` (device MUST accept `\n`, `\r\n` or `\r`) |
| Device → host terminator | `\r\n` (Arduino `Serial.println`); host accepts `\n` or `\r\n` |
| Max line length | 64 characters excluding terminator. Longer input is discarded and answered with `ERR UNKNOWN_COMMAND`. |
| Case / whitespace | Host always sends UPPERCASE with single spaces. Device accepts commands case-insensitively, ignores leading/trailing whitespace and repeated spaces. Empty lines are ignored (no reply). |

**Noise rule.** The host only interprets lines that start with `OK`, `ERR` or `EVENT`
followed by a space (or end of line). Everything else — ESP32 ROM boot banner,
`#debug` prints, partial garbage after a reset — is logged and ignored.
Firmware debug output SHOULD be prefixed with `#`.

**Auto-reset.** Many ESP32 dev boards reset when the host opens the serial port
(DTR/RTS → EN/IO0). The host deasserts DTR/RTS before opening, tolerates a reset
anyway, and re-synchronises with `PING` + `STATUS` after connecting.

## 2. Slots and positions

* Slots are numbered `0 … N-1` where `N = NUM_SLOTS` (default **6**, allowed 2–12;
  must match on both sides — the device reports it in `STATUS`).
* **Slot 0 is the home position** (the compartment at the access gate right after homing).
* Slot `k` is centred at `k × 360/N` degrees from home.
* Firmware computes targets from the *absolute* home reference
  (`target_steps = round(k × steps_per_carousel_rev / N)`), never by adding
  per-slot increments, so fractional step counts do not accumulate.
* **User-facing numbering (added):** people hear and read "compartment `k+1`"
  for slot `k` (compartment 1 = slot 0 = home), matching the carousel diagram
  in the handoff. Only logs, the serial protocol and the technical admin panel use
  raw slot numbers. Physically label the carousel 1…N accordingly.

## 3. Device states

| State | Meaning | Gate | Homed | Busy |
|---|---|---|---|---|
| `BOOT` | Power-on, before homing starts | closed | no | no |
| `HOMING` | Seeking the home sensor | closed | no | **yes** |
| `READY` | Idle, position known, gate closed | closed | yes | no |
| `MOVING` | Carousel rotating towards a slot | closed | yes | **yes** |
| `AT_TARGET` | Arrived at slot inside `DISPENSE_SLOT`, settling before the gate opens | closed | yes | **yes** |
| `GATE_OPEN` | Gate open at the current slot | open | yes | no |
| `SAFE_STOP` | Stopped by `STOP` or the cancel button during motion; position treated as unknown | closed | no | no |
| `FAULT` | Homing or motion failure | closed (if possible) | no | no |

"Homed" means the device trusts its absolute position. Only `HOME` makes an
un-homed device homed again.

## 4. Commands (host → device)

```
PING
STATUS
HOME
MOVE_SLOT <n>
DISPENSE_SLOT <n>
OPEN_GATE
CLOSE_GATE
STOP
```

`DISPENSE_SLOT n` is a device-side macro: *verify gate closed → move to slot n →
report position reached → settle → open gate*. It is the command the host uses
for real dispensing. `MOVE_SLOT` / `OPEN_GATE` exist for testing, calibration and
the caregiver "present compartment for loading" action.

## 5. Messages (device → host)

| Message | Meaning |
|---|---|
| `OK PONG` | Reply to `PING` |
| `OK STATUS state=<STATE> homed=<0\|1> slot=<n\|-1> gate=<OPEN\|CLOSED> slots=<N> fw=<version>` **(added format)** | Reply to `STATUS`. Keys may appear in any order; unknown keys must be ignored by the host. `slot=-1` means between slots / unknown. |
| `OK HOMING` **(added)** | Homing started |
| `OK HOMED` | Home found; position = slot 0 |
| `OK READY` | Device entered `READY` (emitted on **every** transition into `READY`) |
| `OK MOVING <n>` | Motion towards slot n started |
| `OK AT_SLOT <n>` | Carousel stopped at slot n |
| `OK GATE_OPEN` | Gate is open (servo travel finished) |
| `OK GATE_CLOSED` | Gate is closed (servo travel finished) |
| `OK STOPPED` **(added)** | Device entered `SAFE_STOP` |
| `ERR INVALID_SLOT` | Slot argument missing, not an integer, or out of range |
| `ERR NOT_HOMED` | Motion/gate command while not homed (`BOOT`, `SAFE_STOP`, `FAULT`) |
| `ERR BUSY` | Command rejected because the device is `HOMING`, `MOVING` or `AT_TARGET` |
| `ERR HOME_TIMEOUT` | Home sensor not found within max travel/time → `FAULT` |
| `ERR MOTOR_FAULT` | Motion did not complete / position check failed → `FAULT` |
| `ERR INVALID_STATE` | Command not allowed in the current state (e.g. move while gate open) |
| `ERR UNKNOWN_COMMAND` | Unrecognised or over-long line |
| `ERR STOPPED` **(added)** | The in-flight `HOME`, `MOVE_SLOT` or `DISPENSE_SLOT` was interrupted by `STOP` or the cancel button |
| `EVENT CONFIRM_BUTTON` | Confirm button pressed (debounced, on press) |
| `EVENT CANCEL_BUTTON` | Cancel button pressed (debounced, on press) |
| `EVENT BOOT <fw-version>` **(added)** | Firmware just started (power-on, reset, brown-out). Lets the host detect resets. |

`OK` lines may be **unsolicited** state notifications (e.g. `OK HOMED` / `OK READY`
after boot, `OK GATE_CLOSED` after the gate auto-close timeout). `ERR HOME_TIMEOUT`
may be unsolicited when boot-time homing fails.

## 6. Command sequences

Exact line sequences the device emits (nothing else, apart from `#debug` lines):

| Command (from state) | Output |
|---|---|
| `PING` (any) | `OK PONG` |
| `STATUS` (any) | `OK STATUS state=… homed=… slot=… gate=… slots=… fw=…` |
| `HOME` (from `BOOT`/`READY`/`SAFE_STOP`/`FAULT`) | `OK HOMING` … `OK HOMED`, `OK READY` — or `OK HOMING` … `ERR HOME_TIMEOUT` (→ `FAULT`) |
| `MOVE_SLOT n` (from `READY`) | `OK MOVING n` … `OK AT_SLOT n`, `OK READY` — or `OK MOVING n` … `ERR MOTOR_FAULT` (→ `FAULT`) |
| `DISPENSE_SLOT n` (from `READY`) | `OK MOVING n` … `OK AT_SLOT n` … (settle) `OK GATE_OPEN` — or `ERR MOTOR_FAULT` |
| `OPEN_GATE` (from `READY`) | `OK GATE_OPEN` |
| `OPEN_GATE` (from `GATE_OPEN`) | `OK GATE_OPEN` (idempotent) |
| `CLOSE_GATE` (from `GATE_OPEN`) | `OK GATE_CLOSED`, `OK READY` |
| `CLOSE_GATE` (from `READY`/`BOOT`/`SAFE_STOP`/`FAULT`) | `OK GATE_CLOSED` (re-asserts closed; no state change) |
| `STOP` (while `HOMING`/`MOVING`/`AT_TARGET`) | `ERR STOPPED`, `OK STOPPED` |
| `STOP` (while `GATE_OPEN`) | `OK GATE_CLOSED`, `OK STOPPED` |
| `STOP` (from `BOOT`/`READY`/`SAFE_STOP`/`FAULT`) | `OK STOPPED` |

Moving to the slot the carousel is already at still emits `OK MOVING n` then `OK AT_SLOT n`.

**No home sensor (MVP fallback).** If the firmware is built without a home sensor, boot
assumes the carousel was aligned by hand (step counter = 0): `OK HOMING`, `OK HOMED`,
`OK READY` immediately. `HOME` then means "return to step 0 by dead reckoning": it moves
back to the counter's zero and reports `OK HOMING` … `OK HOMED`, `OK READY`. Re-align by
hand and power-cycle if the carousel has slipped.

## 7. Command acceptance table (normative)

Rows are commands, columns are the current state. "run" means the command is accepted.

| | BOOT | HOMING | READY | MOVING | AT_TARGET | GATE_OPEN | SAFE_STOP | FAULT |
|---|---|---|---|---|---|---|---|---|
| `PING` / `STATUS` | run | run | run | run | run | run | run | run |
| `HOME` | run | BUSY | run | BUSY | BUSY | INVALID_STATE | run | run |
| `MOVE_SLOT n` / `DISPENSE_SLOT n` | NOT_HOMED | BUSY | run | BUSY | BUSY | INVALID_STATE | NOT_HOMED | NOT_HOMED |
| `OPEN_GATE` | NOT_HOMED | BUSY | run | BUSY | BUSY | run (idempotent) | NOT_HOMED | NOT_HOMED |
| `CLOSE_GATE` | run | BUSY | run | BUSY | BUSY | run | run | run |
| `STOP` | run | run | run | run | run | run | run | run |

Precedence for `MOVE_SLOT`/`DISPENSE_SLOT`: argument validation first
(`ERR INVALID_SLOT` in **every** state), then the table.

## 8. Device-side behaviour rules

1. **Gate closed before any motion.** The firmware re-asserts the closed servo
   position before starting any move or homing.
2. **Gate travel is atomic.** Servo moves block for `GATE_TRAVEL_MS` (≤ 600 ms)
   before `OK GATE_OPEN` / `OK GATE_CLOSED` is sent. Serial input received meanwhile
   is processed afterwards. In particular a `STOP` or cancel press that arrives while a
   `DISPENSE_SLOT` gate is already travelling open is handled *after* `OK GATE_OPEN`: the
   dispense counts as successful (the compartment was accessible) and duplicate prevention applies.
3. **Motion is non-blocking.** `PING`, `STATUS` and `STOP` are answered while moving/homing.
4. **Settle before opening.** In `DISPENSE_SLOT`, wait `SETTLE_MS` (≈ 300 ms) in
   `AT_TARGET` after the motor stops, then open the gate. `STOP` during settle aborts
   with `ERR STOPPED` and the gate never opens.
5. **Homing.** Rotate slowly in one direction until the home sensor activates
   (debounced). The *seek* for the sensor edge is limited to 1.25 carousel revolutions; if the
   sensor is already active the device first leaves it (≤ 0.25 rev); the whole `HOME` is
   limited by `HOME_TIMEOUT_MS`. Any limit hit → `ERR HOME_TIMEOUT`, state `FAULT`. A sensor
   stuck active is never accepted as home. Optional back-off + slow re-approach.
6. **Motion timeout.** If a move does not finish within `2 × expected + 2 s`,
   stop, send `ERR MOTOR_FAULT`, enter `FAULT`.
7. **Gate auto-close (safety net).** If `GATE_OPEN` lasts longer than
   `GATE_MAX_OPEN_MS` (default 120 s) the device closes the gate itself:
   `OK GATE_CLOSED`, `OK READY`. The host normally closes it much sooner.
8. **Buttons** (debounced ≥ 30 ms, act on press):
   * Confirm → `EVENT CONFIRM_BUTTON` only. The host decides what it means.
   * Cancel → `EVENT CANCEL_BUTTON` **first**, then a local safety action:
     while `HOMING`/`MOVING`/`AT_TARGET` → `ERR STOPPED`, `OK STOPPED` (→ `SAFE_STOP`);
     while `GATE_OPEN` → `OK GATE_CLOSED`, `OK READY`; otherwise nothing.
9. **Boot.** Close gate first, then `EVENT BOOT <fw>` (sent once the closing travel has
   completed, i.e. about `GATE_TRAVEL_MS` after reset; a simulator may send it immediately),
   then auto-home if a home sensor is configured (`OK HOMING` … `OK HOMED`, `OK READY` or
   `ERR HOME_TIMEOUT`).
10. **Never trust the host for interlocks.** Every rule in §7 is enforced on the device
    even though the host also checks them.

## 9. Host-side rules

1. At most **one** normal command in flight. `STOP` may be sent at any time.
2. A command finishes on its first *terminal* message:

   | Command | Progress | Success | Failure (any of) |
   |---|---|---|---|
   | `PING` | – | `OK PONG` | `ERR UNKNOWN_COMMAND` |
   | `STATUS` | – | `OK STATUS …` | `ERR UNKNOWN_COMMAND` |
   | `HOME` | `OK HOMING` | `OK HOMED` | `ERR BUSY`, `ERR INVALID_STATE`, `ERR HOME_TIMEOUT`, `ERR MOTOR_FAULT`, `ERR STOPPED`, `ERR UNKNOWN_COMMAND` |
   | `MOVE_SLOT n` | `OK MOVING n` | `OK AT_SLOT n` | `ERR INVALID_SLOT`, `ERR NOT_HOMED`, `ERR BUSY`, `ERR INVALID_STATE`, `ERR MOTOR_FAULT`, `ERR STOPPED`, `ERR UNKNOWN_COMMAND` |
   | `DISPENSE_SLOT n` | `OK MOVING n`, `OK AT_SLOT n` | `OK GATE_OPEN` | same as `MOVE_SLOT` |
   | `OPEN_GATE` | – | `OK GATE_OPEN` | `ERR NOT_HOMED`, `ERR BUSY`, `ERR INVALID_STATE`, `ERR UNKNOWN_COMMAND` |
   | `CLOSE_GATE` | – | `OK GATE_CLOSED` | `ERR BUSY`, `ERR UNKNOWN_COMMAND` |
   | `STOP` | – | `OK STOPPED` | – |

   Any other line received while a command is in flight is an unsolicited
   notification: it updates the host's mirror of the device state but does not
   finish the command.
3. Per-command timeouts (defaults, configurable): `PING`/`STATUS` 2 s, `OPEN_GATE`/`CLOSE_GATE` 5 s,
   `STOP` 3 s, `MOVE_SLOT` 20 s, `DISPENSE_SLOT` 25 s, `HOME` 45 s.
4. **Outcome certainty.** A command that ended with `OK …`/`ERR …` is *definitive*.
   A timeout or disconnect after the bytes were written is *uncertain*: the device may
   have executed it. For `DISPENSE_SLOT`/`OPEN_GATE` an uncertain outcome means
   **the gate may be open** — the host must not record a successful dispense and must
   not automatically retry that dose (fail closed, caregiver review required).
5. `EVENT BOOT` while a command is in flight ends that command with the host-side code
   `DEVICE_RESET` (definitive: the gate closes on boot).
6. After a timeout the host sends `STATUS` to resynchronise before the next command.

## 10. Example session

```
<- EVENT BOOT 1.0.0
<- OK HOMING
<- OK HOMED
<- OK READY
-> PING
<- OK PONG
-> DISPENSE_SLOT 3
<- OK MOVING 3
<- OK AT_SLOT 3
<- OK GATE_OPEN
<- EVENT CONFIRM_BUTTON          (user pressed the big button)
-> CLOSE_GATE
<- OK GATE_CLOSED
<- OK READY
-> MOVE_SLOT 9
<- ERR INVALID_SLOT
-> OPEN_GATE
<- OK GATE_OPEN
-> MOVE_SLOT 1
<- ERR INVALID_STATE             (gate open → motion refused)
-> STOP
<- OK GATE_CLOSED                (gate was open, so it is closed first)
<- OK STOPPED
-> DISPENSE_SLOT 1
<- ERR NOT_HOMED                 (after STOP the device must re-home)
-> HOME
<- OK HOMING
<- OK HOMED
<- OK READY
```

> `STOP` always ends in `SAFE_STOP` (re-home required). The **cancel button** is gentler
> when the gate is open: it just closes the gate and returns to `READY` (§8.8).

## 11. Testing the contract

* `python -m tactidose hw-test --port COM5` runs the handoff §29 integration checklist on a real
  board (PING, STATUS, HOME, every slot, dispense/drop, STOP, unknown command; it moves the
  carousel and opens the gate).
* `python -m tactidose.hardware.conformance --target serial --port COM5` runs the `hardware_safe`
  conformance scenarios on a real board.
* `pytest tests/test_hw_conformance_sim.py` runs the full suite (including fault injection)
  against the Python simulator; `pytest tests/test_fw_native.py -m native` runs it against the
  natively compiled firmware core (Docker).
* Either jam model is conformant: a simulator may let the step counter run on while the carousel
  stays put (Python twin) or freeze it (native harness); only the reply sequences are normative.

## 12. v1.1 — `DROP_SLOT` (pill drop) **(added 2026-10-03)**

The v2 product drops **one pill** from one of **3 containers** instead of presenting a compartment at
an open gate. v1.1 adds one command; everything above is unchanged, so v1 firmware keeps working
(the host emulates a drop, see 12.5).

### 12.1 Command

```
DROP_SLOT <n>
```

Drop exactly one pill from container `n` (0-based; user-facing "container n+1") into the output
chute. Device-side macro: gate/release closed → move to slot `n` (designs with a fixed release per
container skip the motion and report it immediately) → settle → **release** (open, hold
`DROP_OPEN_MS`, close — atomic, ≤ ~1.5 s) → optional drop-sensor check → report.

### 12.2 Output

From `READY`:

```
OK MOVING n
OK AT_SLOT n
OK GATE_OPEN
OK GATE_CLOSED
OK DROPPED n        ← terminal success
OK READY
```

Failures: every `MOVE_SLOT` failure (`ERR INVALID_SLOT`, `NOT_HOMED`, `BUSY`, `INVALID_STATE`,
`MOTOR_FAULT`, `STOPPED`, `UNKNOWN_COMMAND`) plus:

| Message | Meaning |
|---|---|
| `ERR NO_PILL` | Only with a drop sensor: no pill passed the sensor during the release (container empty or jammed). Sent after `OK GATE_CLOSED`; the device then returns to `READY` (`OK READY`). |

Acceptance (§7): same row as `DISPENSE_SLOT`. Argument validation first (`ERR INVALID_SLOT` in every state).

### 12.3 Interruption

* `STOP` / cancel button **during motion or settle** → `ERR STOPPED`, `OK STOPPED` — the release
  never happened.
* The release itself is atomic: a `STOP` received during it is processed after `OK DROPPED n` /
  `ERR NO_PILL` and `OK READY`.
* Host rule: if `ERR STOPPED` or a reset (`EVENT BOOT`) arrives **after** `OK GATE_OPEN`, a pill may
  have dropped → the host records the drop as **UNCERTAIN** (fail closed).

### 12.4 STATUS additions

`OK STATUS … proto=1.1 drop_sensor=<0|1>` — v1 devices omit `proto`. Hosts must ignore unknown keys.

### 12.5 Host behaviour

* Device reports `proto ≥ 1.1` → the host sends `DROP_SLOT n` (timeout 30 s).
* Otherwise (v1 firmware) → `DISPENSE_SLOT n`, wait `drop_close_delay_ms` (default 1.5 s), `CLOSE_GATE`.
* `OK DROPPED n` → the host decrements that container's pill count. `ERR NO_PILL` → the host sets it
  to 0 and notifies "container empty". No sensor → `OK DROPPED` means "release cycle completed".

### 12.6 Hardware mapping guidance

| Mechanism | `MOVE`/`AT_SLOT` | "gate" (release) |
|---|---|---|
| Carousel (stepper) over one chute + trapdoor servo | stepper positions container `n` over the chute | trapdoor servo |
| 3 fixed containers, one dispensing wheel/servo each | no motion (report `OK MOVING n`, `OK AT_SLOT n` immediately) | servo of container `n` rotates one pocket |
| Drop sensor (optional, recommended) | – | IR break-beam in the chute sampled during the release |
