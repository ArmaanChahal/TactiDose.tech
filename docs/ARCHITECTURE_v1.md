# TactiDose host software — architecture v1 (superseded)

> **Superseded by `docs/ARCHITECTURE.md` (v2, 2026-10-03).** Kept because the hardware client,
> simulator, conformance harness protocol (§7) and voice/TTS internals described here are still
> accurate. Dispensing, the assistant dialogue and the HTTP API changed in v2.

Audience: the software team (and the agents/people implementing each module).
Read with `docs/SERIAL_PROTOCOL.md` (hardware contract) and `docs/API.md` (HTTP contract).

## 1. Principles (from the handoff, made concrete)

1. **AI interprets, deterministic code authorizes and actuates** (§33). Gemini, Vosk and
   ElevenLabs only produce *data*. Eligibility, duplicate prevention, slot mapping and
   motor commands live in `medication/` and `hardware/` and never depend on model output.
2. **Fail closed.** If the database, the hardware link or the outcome of a command is
   uncertain, nothing is dispensed and the user is told to ask for assistance. An uncertain
   dispense is never recorded as successful and never retried automatically.
3. **Speech is a request, not an authorization.** A recognized phrase becomes an `Intent`;
   the schedule + dose state decide whether anything moves.
4. **Offline first.** SQLite + simulator/serial + Vosk + cached/offline TTS run with no
   internet. Gemini, ElevenLabs, TiDB and Snowflake are optional layers.
5. **One serialization point.** All user actions flow through the assistant's single action
   worker; the dose service additionally holds a lock and uses a DB compare-and-set, so a
   dose can never be dispensed twice even under concurrent requests.

## 2. Package map and ownership

```
tactidose/
├── __main__.py              CLI (run, simulator, serial-console, hw-test, init-db, seed-demo, doctor, ...)   [app]
├── app.py                   FastAPI app factory, Services container, lifespan wiring                        [app]
├── config.py                Settings (env / .env)                                                           [foundation]
├── core/
│   ├── bus.py               EventBus (pub/sub, SSE feed)                                                    [foundation]
│   ├── clock.py             Clock (timezone, demo time travel)                                              [foundation]
│   ├── interfaces.py        Cross-module protocols and outcome dataclasses                                  [foundation]
│   ├── assistant.py         Intent orchestrator: action worker, dialogue, gate timer, button mapping        [voice]
│   └── phrases.py           Every spoken sentence (single place; critical ones pre-cached)                  [voice]
├── hardware/
│   ├── protocol.py          Wire format, parsing, classification (pure)                                     [foundation]
│   ├── conformance.json     Shared protocol scenarios                                                       [foundation]
│   ├── commands.py          Re-exports Command builders (handoff §32 layout)                                [hardware]
│   ├── transports.py        Transport abstraction: pyserial (COMx, socket://, loop://) + in-process sim      [hardware]
│   ├── ports.py             ESP32 USB auto-detection (VID/PID)                                              [hardware]
│   ├── serial_client.py     HardwareClient (implements HardwareController), NullHardware, create_hardware()  [hardware]
│   ├── simulator.py         VirtualESP32 (tick-driven firmware twin) + SimulatedDevice (real-time + faults)
│   │                        + ConformanceSimTarget                                                          [hardware]
│   ├── conformance.py       Conformance runner, ConformanceTarget protocol, CLI                             [foundation]
│   ├── conformance_native.py NativeTarget: drives the firmware/native harness over stdin/stdout             [firmware]
│   └── selftest.py          `hw-test` checklist against a real board (handoff §29) + SerialConformanceTarget [hardware]
├── db/
│   ├── models.py, types.py, session.py, outbox.py, devlog.py                                                [foundation]
│   └── seed.py              Demo data (candy/tokens only)                                                   [app]
├── medication/
│   ├── scheduler.py         Schedules -> DoseEvents (materialize, DUE/MISSED transitions)                   [domain]
│   ├── safety.py            Deterministic eligibility rules (pure functions)                                [domain]
│   ├── dispense.py          DoseService (implements DoseServiceAPI) + caregiver dose operations             [domain]
│   ├── compartments.py      Slot <-> medication assignment                                                  [domain]
│   ├── catalog.py           Medication CRUD (confirmed records only)                                        [domain]
│   ├── onboarding.py        Label scan -> UNCONFIRMED LabelScan -> human confirm -> Medication              [domain]
│   └── analytics.py         Local adherence analytics (works without Snowflake)                             [integrations]
├── voice/
│   ├── intents.py           Deterministic text -> Intent parser + Vosk grammar                              [voice]
│   └── recognizer.py        Vosk + sounddevice listener (half-duplex mute while speaking)                   [voice]
├── audio/
│   ├── speaker.py           SpeakerService (implements Speaker): queue, captions, fallback chain            [voice]
│   ├── cache.py             TTS disk cache                                                                  [voice]
│   ├── playback.py          PCM/WAV playback (sounddevice, winsound fallback)                               [voice]
│   └── offline_tts.py       OS speech (Windows SAPI via PowerShell, macOS `say`, Linux `espeak`)            [voice]
├── integrations/
│   ├── elevenlabs.py        ElevenLabs REST client (httpx)                                                  [voice]
│   ├── gemini.py            GeminiLabelExtractor, FakeLabelExtractor, create_label_extractor()              [integrations]
│   ├── tidb.py              TiDB helpers (connection check, doctor info)                                    [integrations]
│   └── snowflake.py         Outbox -> Snowflake sync worker, DDL, analytics report                          [integrations]
├── api/                     FastAPI routers (see docs/API.md)                                               [app]
└── ui/static/               Kiosk, caregiver, demo panel (vanilla HTML/CSS/JS, no CDN)                       [ui]
firmware/                    Reference ESP32 firmware + native test harness                                  [firmware]
analytics/snowflake_queries.sql                                                                              [integrations]
tests/                       pytest; files are prefixed by owner: test_hw_*, test_med_*, test_voice_*, ...
```

## 3. Runtime and threading model

| Thread | Owner | Does |
|---|---|---|
| uvicorn event loop | app | HTTP + SSE. Blocking work runs in the threadpool. |
| `hw-reader` | HardwareClient | Reads serial lines, updates the DeviceSnapshot, resolves the in-flight command, dispatches EVENTs. |
| `hw-supervisor` | HardwareClient | Connect / handshake (PING+STATUS) / heartbeat / reconnect with backoff / auto-home. |
| `sim-device` | SimulatedDevice | Ticks the VirtualESP32 in (scaled) real time; serves the in-process transport. |
| `assistant` | Assistant | Single action worker: pops intents, calls DoseService (blocking), speaks replies, runs the gate-open timer. |
| `speaker` | SpeakerService | Plays queued utterances in order. Exposes `is_speaking` for half-duplex muting. |
| `voice` (+ PortAudio callback) | VoiceRecognizer | Mic -> Vosk -> text -> `Assistant.handle_text`. Drops audio while speaking. |
| `scheduler` | app (calls Scheduler.tick) | Every `scheduler_tick_s`: materialize events, SCHEDULED->DUE, ->MISSED. |
| `analytics-sync` | SnowflakeSync | Every `analytics_sync_interval_s`: drain outbox -> MERGE into Snowflake. |

Rules: hardware EVENT callbacks run on `hw-reader` and must only enqueue work.
`HardwareController.stop()` is the only hardware call allowed to bypass the command lock.

## 4. Construction (wiring contract used by `app.py`)

```python
settings = Settings()
clock    = Clock(settings.timezone)
bus      = EventBus()
db       = Database(settings); db.create_all()

hardware, sim = create_hardware(settings, bus=bus, clock=clock)   # hardware/serial_client.py
#   hardware_mode="sim"    -> (HardwareClient over in-process SimulatedDevice transport, SimulatedDevice)
#   hardware_mode="serial" -> (HardwareClient over pyserial/auto-detected port, None)
#   hardware_mode="none"   -> (NullHardware, None)  # every command -> NOT_CONNECTED

compartments = CompartmentService(db, settings, bus=bus)          # ensure_device() creates user/device/slots
catalog      = MedicationCatalog(db, settings, clock, bus=bus)
scheduler    = Scheduler(db, clock, settings, bus=bus)
dose         = DoseService(db, hardware, clock, settings, bus=bus)
extractor    = create_label_extractor(settings)                   # None when disabled
onboarding   = OnboardingService(db, extractor, catalog, settings, clock, bus=bus)
speaker      = SpeakerService(settings, bus)                      # ElevenLabs -> cache -> offline -> captions only
assistant    = Assistant(dose, speaker, bus, settings, clock)
hardware.add_event_listener(assistant.on_hardware_event)
recognizer   = VoiceRecognizer(settings, on_text=assistant.handle_voice_text,
                               is_muted=lambda: speaker.is_speaking, bus=bus)
analytics    = SnowflakeSync(db, settings, clock, bus=bus) if settings.snowflake_configured else None

startup: compartments.ensure_device(); dose.recover_on_startup(); scheduler.tick();
         hardware.start(); speaker.start(); assistant.start(); recognizer.start(); analytics.start()
shutdown in reverse order; every start()/close() is idempotent and must not raise.
```

All services accept fakes in tests (`FakeHardware`, `FakeSpeaker`, `FakeExtractor` live in
`tests/fakes.py`).

### Constructor / method signatures (normative)

```python
# hardware/simulator.py
@dataclass
class SimConfig:
    num_slots: int = 6; steps_per_rev: int = 3200; initial_offset_steps: int = 1600
    max_speed_sps: float = 1600; accel_sps2: float = 3200; homing_speed_sps: float = 400
    settle_ms: int = 300; gate_travel_ms: int = 400; home_timeout_ms: int = 20000
    gate_max_open_ms: int = 120000; debounce_ms: int = 30; sensor_zone_steps: int = 40
    fw_version: str = "sim-1.0.0"; home_sensor: str = "ok"   # ok | dead | none
class VirtualESP32:                       # deterministic, no threads, no wall clock
    def __init__(self, config: SimConfig | None = None) -> None
    def boot(self, sensor: str | None = None) -> None
    def feed_line(self, line: str) -> None
    def tick(self, ms: int = 1) -> None
    def set_button(self, name: str, pressed: bool) -> None      # "CONFIRM" | "CANCEL"
    def set_sensor(self, mode: str) -> None                     # "ok" | "dead"
    def set_jam(self, on: bool) -> None
    def drain_output(self) -> list[str]                         # lines emitted since last drain
    def physical(self) -> dict                                   # angle_deg, slot, gate_open, state, ...
class SimulatedDevice:                    # real-time wrapper (thread) + fault injection
    def __init__(self, settings: Settings, bus: EventBus | None = None, config: SimConfig | None = None)
    def start(self) -> None; def close(self) -> None
    def open_transport(self) -> Transport                       # in-process byte pipe
    def set_fault(self, name: str, enabled: bool) -> None       # home_sensor_dead | motor_jam | unresponsive | brownout_on_gate | disconnect
    def faults(self) -> dict[str, bool]
    def press(self, name: str) -> None; def reboot(self) -> None; def physical(self) -> dict

# hardware/serial_client.py
class HardwareClient:                     # implements HardwareController
    def __init__(self, settings, *, bus=None, clock=None, transport_factory: Callable[[], Transport] | None = None, mode: str = "serial")
def create_hardware(settings, *, bus=None, clock=None) -> tuple[HardwareController, SimulatedDevice | None]

# medication/*
class CompartmentService:  __init__(db, settings, bus=None); ensure_device() -> None; list() -> list[dict]; assign(slot: int, medication_id: int | None) -> list[dict]
class MedicationCatalog:   __init__(db, settings, clock, bus=None); list(include_inactive=False) -> list[dict]; get(id) -> dict;
                           create(fields: dict, *, confirmed: bool, confirmed_by: str | None, source="manual", scan_id=None) -> dict;
                           update(id, fields: dict, *, confirmed: bool, confirmed_by=None) -> dict; archive(id) -> None
class Scheduler:           __init__(db, clock, settings, bus=None); tick() -> int; list_schedules() -> list[dict];
                           create_schedule(medication_id, time_of_day, frequency="DAILY", days_of_week=None) -> dict;
                           update_schedule(schedule_id, **fields) -> dict; deactivate_schedule(schedule_id) -> None
class DoseService:         __init__(db, hardware, clock, settings, bus=None)   # implements DoseServiceAPI
                           recover_on_startup() -> int; list_events(local_date: date | None = None) -> list[dict]
                           resolve_review(event_id, *, accessed: bool, note: str | None, by: str | None) -> dict
                           skip_dose(event_id, *, note=None, by=None) -> dict
                           mark_taken_by_caregiver(event_id, *, by=None) -> dict
                           present_compartment(slot: int, *, by=None) -> CommandResult      # caregiver loading mode
                           finish_loading(slot: int, *, by=None) -> CommandResult
                           create_demo_dose_now(medication_id: int | None = None) -> dict  # demo mode helper
class OnboardingService:   __init__(db, extractor: LabelExtractor | None, catalog, settings, clock, bus=None)
                           scan(image: bytes, mime_type: str) -> dict; list_scans(status=None) -> list[dict]
                           confirm_scan(scan_id, fields: dict, *, confirmed: bool, confirmed_by=None) -> dict
                           reject_scan(scan_id, *, by=None) -> dict
# medication/analytics.py
def local_summary(db, clock, settings, days: int = 7) -> dict

# core/assistant.py
class Assistant:           __init__(dose: DoseServiceAPI, speaker: Speaker, bus, settings, clock)
                           start(); close()
                           submit(intent: Intent, source: IntentSource, *, text: str = "", wait: bool = False, timeout: float = 60) -> Reply | None
                           handle_text(text: str, source: IntentSource, confidence: float = 1.0, *, wait=False) -> Reply | None
                           handle_voice_text(text: str, confidence: float) -> None
                           on_hardware_event(msg: Message) -> None
                           state() -> dict      # phase, last_reply, awaiting dose
# voice/intents.py
def parse_intent(text: str) -> ParsedIntent;   GRAMMAR_PHRASES: list[str]
# voice/recognizer.py
class VoiceRecognizer:     __init__(settings, *, on_text: Callable[[str, float], None], is_muted: Callable[[], bool], bus=None)
                           start() -> bool (False = unavailable, logged + published, never raises); close(); status() -> dict
# audio/speaker.py
class SpeakerService:      __init__(settings, bus, *, tts=None, offline=None, player=None)   # implements Speaker
                           warm_cache(texts: Iterable[str] | None = None) -> dict; status() -> dict
# integrations/*
class ElevenLabsClient:    __init__(api_key, voice_id, model_id, output_format, timeout_s); synthesize(text) -> bytes  # raises ElevenLabsError
def create_label_extractor(settings) -> LabelExtractor | None
class SnowflakeSync:       __init__(db, settings, clock, bus=None); start(); close(); sync_once() -> dict; status() -> dict; report() -> dict
```

## 5. Dose event lifecycle (deterministic)

```
             tick/now≥start              claim (CAS)               OK GATE_OPEN             "Taken"/button
SCHEDULED ───────────────► DUE ───────────────────► DISPENSING ─────────────► DISPENSED ───────────────► TAKEN
    │                       │                           │  ERR (definitive)          │
    │                       │                           ├──────────► HARDWARE_ERROR ─┤ (retry allowed if
    │                       │                           │  TIMEOUT/DISCONNECT        │  !needs_review and
    │                       │                           ├──────────► HARDWARE_ERROR  │  attempts < max)
    │                       │                           │  (needs_review = gate may be open → locked)
    │                       │                           │  ERR STOPPED (user cancel) → back to DUE
    └──────── now > end ────┴──────────────► MISSED (also HARDWARE_ERROR without review)
caregiver: skip → CANCELLED · resolve review → DISPENSED (accessed) or DUE (not accessed)
startup recovery: any DISPENSING → HARDWARE_ERROR(needs_review, "UNCERTAIN RESTART")
```

Window: `start = scheduled_at − dose_early_minutes`, `end = scheduled_at + dose_late_minutes`.

**Eligibility (all must hold) — `medication/safety.py`:**
1. `start ≤ now ≤ end`.
2. status ∈ {SCHEDULED, DUE}, or HARDWARE_ERROR with `needs_review = False` and `attempts < max_dispense_attempts`.
3. medication `active` and `confirmed_by_user`; schedule `active`.
4. medication has an active compartment on this device with `0 ≤ slot < num_slots`
   (resolved **now**, not at event generation).
5. no event on this device is DISPENSING.
6. the same medication was not DISPENSED/TAKEN within `min_dose_interval_minutes` (TOO_SOON).

**Selection:** earliest `scheduled_at`, then lowest slot. **When nothing is eligible**, the
decision is, in priority order: IN_PROGRESS → BLOCKED(NEEDS_REVIEW) → DUPLICATE (an in-window
dose is DISPENSED/TAKEN, or TOO_SOON) → BLOCKED(NO_COMPARTMENT/UNCONFIRMED/INACTIVE) → NOTHING_DUE.

**Claim:** `UPDATE dose_events SET status='DISPENSING', attempts=attempts+1 … WHERE event_id=:id
AND status=:expected` — proceed only if exactly one row changed.

**Hardware preparation before the claim** (no dose state change on failure → HARDWARE_UNAVAILABLE):
not connected → refuse; FAULT → refuse (caregiver re-home); SAFE_STOP/BOOT/not homed → `HOME`
if `hw_auto_home`; GATE_OPEN → `CLOSE_GATE`; HOMING → wait for READY up to `timeout_home_s`.

**Outcome mapping of `DISPENSE_SLOT n`:**

| Result | Dose becomes | needs_review | User hears |
|---|---|---|---|
| `OK GATE_OPEN` | DISPENSED (`dispensed_at`) | – | ready + how to confirm |
| `ERR STOPPED` | DUE | – | cancelled, nothing dispensed |
| other `ERR …`, `DEVICE_RESET`, `NOT_CONNECTED` | HARDWARE_ERROR | only if attempts ≥ max | could not prepare, ask for assistance |
| `TIMEOUT`, `DISCONNECTED` (uncertain) | HARDWARE_ERROR | **True** | could not prepare, ask for assistance |

Every status change: same transaction → `enqueue_adherence(...)` (outbox) and `log_event(...)`
(device_log); after commit → `bus.publish(Topic.DOSE_UPDATED, ...)`.

**Confirm ("Taken"/button):** the most recent DISPENSED dose on the device with
`dispensed_at ≥ now − confirm_window_minutes` → TAKEN (`confirmed_taken_at`), then close the gate
if it is open. If none: ALREADY_CONFIRMED when the latest accessed dose is TAKEN within the window,
else NOTHING_TO_CONFIRM.

**Cancel:** if a long-running command is in flight → `interrupt()` sends `STOP` immediately
(the in-flight dispense returns `ERR STOPPED` → dose back to DUE); if the gate is open →
`CLOSE_GATE` (dose stays DISPENSED — it was accessible, so duplicate prevention still applies).

## 6. Assistant behaviour (dialogue contract)

* All intents go through `Assistant.submit` → one worker → handler → `Reply` → `Speaker.say` +
  `Topic.SPOKEN` + `Topic.ASSISTANT_STATE`. `REPEAT` replays the last reply.
* `CHECK_DUE` announces what is due and asks for consent ("Say 'dispense' or press the big
  button"); it dispenses directly only when `check_due_auto_dispense=True`.
* Before motion the assistant says "Preparing … please keep your hands clear" (via the
  `on_motion_start` callback of `dispense_next`).
* After DISPENSED, a gate timer (`gate_open_timeout_s`, monotonic) enqueues `GATE_TIMEOUT`
  → close gate → "I've closed the compartment. If you took your dose, say 'taken'."
* Hardware events: `EVENT CONFIRM_BUTTON` → `PRIMARY_ACTION` (confirm if awaiting confirmation,
  else dispense if due, else check); `EVENT CANCEL_BUTTON` → `CANCEL`;
  `EVENT BOOT` → notice "device restarted"; unsolicited FAULT → "The device needs attention".
* `CANCEL` must take effect immediately even while the worker is blocked in a dispense:
  `submit()` calls `dose.interrupt(source)` synchronously before enqueueing.
* Voice: text below `voice_min_confidence` or with no intent is ignored silently unless it
  contained real words (then "Sorry, I didn't catch that…"); negated phrases ("I haven't taken
  it") never confirm.
* Every sentence lives in `core/phrases.py`; `CRITICAL_PHRASES` (static, no names) is what
  `warm-tts-cache` pre-renders for offline use. Handoff-mandated wording is kept verbatim:
  "That scheduled dose has already been accessed.", "I could not prepare the compartment.
  Please ask for assistance.", "Cancelled.", "Please ask for assistance.", "Network unavailable.",
  "Hardware error.", "You do not have a scheduled medication due right now."

## 7. Conformance harness protocol (Python runner ↔ native firmware build)

The native harness binary (`firmware/native/`) wraps the firmware core with a fake HAL and
simulated time. It reads commands on **stdin**, one per line, and writes to **stdout**:

| stdin | Meaning |
|---|---|
| `> <text>` | Deliver `<text>` to the firmware as one serial line (`>` alone = empty line). |
| `!reset` | Fresh device: physical carousel at the initial offset (1600 steps before home), sensor ok, jam off, buttons released, firmware not booted, sim time 0. Sent by the runner before every scenario. |
| `!boot ok\|dead\|none` | (Re)initialise the firmware core (`setup()`) with that home-sensor mode; keeps the physical position. |
| `!tick <ms>` | Advance simulated time `<ms>` milliseconds, calling `loop()` every 1 ms. |
| `!button CONFIRM\|CANCEL 1\|0` | Set the button pin pressed (1) / released (0). |
| `!sensor ok\|dead` | Home sensor works / never triggers. |
| `!jam 1\|0` | Motor jammed: commanded steps do not move the carousel and moves never complete. |
| `!quit` | Exit. |

stdout: every firmware serial line verbatim (without `\r`), and after processing each stdin line
exactly one `!ack <sim_time_ms>` line. The runner treats lines starting with `!` as harness
lines and lines starting with `#` as firmware debug output.

The Python `VirtualESP32` exposes the same operations as methods, so `hardware/conformance.py`
drives both through one `ConformanceTarget` interface; a third target drives a real board over
serial for scenarios marked `hardware_safe` (real time, no fault injection).

## 8. Error-handling summary (handoff §30)

| Failure | Behaviour |
|---|---|
| Internet down | Dispensing unaffected with SQLite. TTS → cached audio → OS voice. Gemini scan → "Could not reliably read label…"; manual entry still works. Snowflake rows wait in the outbox. |
| TiDB unreachable (when used) | `DB_ERROR` → no motor command, "I can't check your schedule right now…" |
| Serial disconnect | Reconnect loop with backoff; in-flight dispense → uncertain → HARDWARE_ERROR(needs_review). |
| Device reset (`EVENT BOOT`) | In-flight command → `DEVICE_RESET`; auto-home on reconnect. |
| Home failure / motor fault | Device in FAULT → dispensing refused until caregiver re-homes. |
| Speech recognition unavailable | Big button, kiosk buttons, demo text box all produce the same intents. |
| Process crash mid-dispense | Startup recovery marks DISPENSING → HARDWARE_ERROR(needs_review). |
