# CLAUDE.md — TactiDose.tech (with the well-being check-in)

TactiDose.tech is a hackathon prototype pill dispenser for blind and low-vision users. It is
**not a medical device**: demo it with candy only. An ESP32 drops one pill at a time from
**3 containers**. A FastAPI host decides *whether* a pill may drop and serves:

* the patient portal,
* the doctor/family portal,
* a kiosk,
* a demo panel.

The patient talks to an AI assistant (Gemini, with an offline rules fallback). PDF reports go to
the doctor. An optional, non-clinical **well-being check-in** (mood, stress, sleep, support) is
integrated from a separate package in `tactidose-wellbeing/`. It is offered after every pill
drop, and saved check-ins are stored next to the pill history.

Read next:

* `README.md` (setup, features)
* `docs/ARCHITECTURE.md` (v2; it wins over the original handoff doc)
* `docs/API.md` (HTTP)
* `docs/SERIAL_PROTOCOL.md` (v1.1)
* `learnings.md` (build history, owner decisions; read §2 before changing behaviour)
* `tactidose-wellbeing/CLAUDE.md` and `tactidose-wellbeing/README.md` for the check-in package

## Repository layout

```
CareBridge/                      repo root (github.com/ArmaanChahal/TactiDose.tech)
├── tactidose/                   main package: the host app (Python ≥3.10, FastAPI, SQLAlchemy)
│   ├── app.py                   Services container, build_services(), create_app(), startup/shutdown, pages
│   ├── __main__.py              CLI: python -m tactidose run | doctor | check-apis | seed-demo | ...
│   ├── config.py                Settings (pydantic-settings; TACTIDOSE_* env vars + .env)
│   ├── core/                    EventBus (topics), Clock (demo time travel), interfaces/protocols, phrases
│   ├── db/                      SQLAlchemy models, Database/session (SQLite or TiDB), seed, outbox
│   ├── hardware/                serial client, ESP32 simulator, protocol, conformance, self-test
│   ├── medication/              DropService (THE only path that drops pills), scheduler, notifications,
│   │                            compartments (inventory), catalog, onboarding (label scans), analytics
│   ├── agent/                   AgentService (conversation store + routing), Gemini agent, rules agent,
│   │                            tools bound to one patient, server STT/TTS, device voice loop
│   ├── reports/                 data → stats → narrative → PDF (fpdf2) → mailer (SMTP or .eml outbox)
│   ├── auth/                    scrypt passwords, sessions, care links, FastAPI permission deps
│   ├── api/                     one router per area (+ SSE events), error mapping in common.py
│   ├── voice/ audio/            offline Vosk recognition, intents, TTS chain (ElevenLabs → cache → OS voice)
│   ├── integrations/            gemini, elevenlabs, tidb, snowflake, netsafe (no-redirect clients), live_check
│   ├── wellbeing.py             ★ check-in bridge: after-drop offer, DB repository, identity, chat routing
│   └── ui/static/               plain HTML + ES modules: login, patient, care, kiosk, demo
├── tactidose-wellbeing/         ★ standalone check-in package (own pyproject, 138 tests, docs, examples)
│   └── src/tactidose_wellbeing/ domain/ (pure), service.py, contract.py, storage/, api/, agent/, cli.py
├── firmware/                    ESP32 Arduino firmware (tactidose_esp32/) + native g++ harness (native/)
├── tests/                       main pytest suite (~2,150 tests), fakes.py, conftest.py (hermetic env)
├── docs/                        ARCHITECTURE, API, SERIAL_PROTOCOL, HARDWARE_INTEGRATION, DEMO_SCRIPT (+ v1 copies)
├── pyproject.toml               main package; extras: voice, gemini, tidb, snowflake, dev, all
└── .env.example                 every setting, with cloud keys at the top
```

## Core design rules (do not break)

1. **AI interprets; deterministic code authorizes and actuates.** The agent can only call
   `request_pill`. `DropService.request_drop` re-checks every rule:
   * device present
   * target resolved
   * no pending review
   * global cooldown
   * dose not already satisfied
   * pills left
   * one drop at a time
   * hardware ready

   `medication/drops.py` is the **only** code path that sends `DROP_SLOT`.
2. **Fail closed.** If the DB is unreadable, the device is unavailable or a drop's outcome is
   uncertain, no further drop happens. An uncertain drop gets `UNCERTAIN` + `needs_review`, and
   the cooldown applies until a caregiver resolves it.
3. **Every pill is accounted for.** Each request gets one `pill_drops` row (DENIED included). The
   counts, dose events and notifications change in the same transaction.
4. **Least privilege.** `/api/patients/{pid}` checks the session user against `care_links`
   (`auth/deps.py`). SSE is filtered per user. The patient id always comes from the session,
   never from the request body.
5. **Offline first.** SQLite + simulator + rules agent + offline TTS/STT work with no internet.
   Gemini, ElevenLabs, SMTP, TiDB and Snowflake are optional layers with fallbacks.

## Runtime and wiring (`tactidose/app.py`)

`build_services(settings)` builds these in order:

* `Clock`, `EventBus`, `Database`
* hardware (`create_hardware`; in sim mode the client owns the simulator)
* `NotificationService`, `CompartmentService`, `MedicationCatalog`, `Scheduler`, `DropService`,
  `AuthService`
* the optional components, each wrapped in `_optional` so a failure only marks it as degraded:
  `AgentService`, `ReportService`, label extractor, `OnboardingService`, `SnowflakeSync`, and
  `wellbeing` (built by `tactidose.wellbeing.build_wellbeing`)

Tests inject fakes through the same keyword arguments.

Startup runs these steps, each guarded and listed in `/api/health` under `degraded` if it fails:

1. `create_all`
2. demo seed
3. `drops.recover_on_startup()`
4. `scheduler` thread: `Scheduler.tick()` then `DropService.run_scheduled_drops()`
5. `hardware.start()`
6. voice loop / Snowflake sync / STT preload

`create_app` then installs the error handlers, includes `api_router()`, **mounts the check-in API
at `/api/wellbeing`**, adds the page routes and mounts `/static`.

Threads:

* uvicorn loop (blocking work goes to the threadpool)
* `hw-reader` / `hw-supervisor`
* `sim-device`
* `scheduler`
* optional `voice`, `speaker`, `analytics-sync`

## Main flows

* **Drop** (`medication/drops.py`): the source is `schedule` | `manual` | `button` | `agent`.
  1. The checks run in a fixed order (ARCHITECTURE §5). The first failure writes a `DENIED` row
     with the reason, and nothing is sent to the device.
  2. On success, an in-flight row (`UNCERTAIN`, `completed_at` NULL) is written, then
     `DROP_SLOT n` is sent.
  3. The row is finalised from `protocol.drop_certainty`: `DROPPED`, `FAILED` or `UNCERTAIN`.
  4. Inventory, dose events and notifications are updated, and bus events are published after
     the commit.
* **Schedule**: `Scheduler` materialises `dose_events` from `schedules` (36 h horizon). Due doses
  auto-drop inside `[scheduled_at, scheduled_at + dose_late_minutes]`. After the window closes they
  become `MISSED`, with a notification. A manual drop shortly before a dose satisfies it, so there
  is no double dose.
* **Agent** (`agent/service.py`): `chat(patient_id, text)` works like this.
  1. Only patients may chat.
  2. The conversation is opened or continued (it rolls over after 30 min) and the user message is
     stored.
  3. Deterministic handling comes first: an emergency gets the 911 sentence, and "stop" calls
     `DropService.interrupt()`.
  4. Gemini runs with function calling, or the rules agent answers. A circuit breaker sends turns
     to the rules agent after a Gemini failure.
  5. Gemini's reply is checked against the real drop outcome and replaced if it lies.
  6. Every tool call and the reply are stored. **Conversations are visible to linked doctor/family
     and are summarised in reports.**
* **Reports** (`reports/`): `ReportService.generate(patient_id, days)` gathers data, computes stats
  (adherence, drops, denied, inventory) and writes a narrative (Gemini, or rules as the fallback).
  It renders the PDF into the `reports` table and sends it by SMTP, or writes an `.eml` to
  `data/outbox`.
* **Auth** (`auth/`): scrypt passwords and `td_session` cookie or bearer tokens (the DB stores
  their SHA-256).
  * Roles: patient, doctor, family.
  * Caregivers link to a patient with its id + link code.
  * Only doctor/family edit schedules, cooldown, containers and medications.
* **Hardware** (`hardware/`): line protocol over serial or TCP; or the Wi-Fi ESP32 (`hardware_mode=wifi`):
  * `wifi_device.WifiDispenser` is a `HardwareController` for three HTTP endpoints: `/dispense?pill=N`,
    `/lid?state=open|close`, and `GET /` for reachability. The IP (static `http://192.168.1.45`,
    override `TACTIDOSE_ESP32_URL`) and the paths are in `wifi_config.py`.
  * Drops still go through `DropService`. Results: 2xx = DROPPED, another status = FAILED, cannot
    connect = NOT_CONNECTED (never sent), no answer = UNCERTAIN + review.
  * Lid: `POST /api/device/lid` and the Open/Close lid buttons (`js/lid.js`).
  * No stop, home or buzzer endpoint: those calls return "not supported". `simulator.py` is a faithful ESP32
  model with fault injection. There are 32 conformance scenarios, run against both the simulator
  and the native firmware core.

## Well-being check-in integration (★ new)

### What the patient experiences

1. A pill drops. This can be a scheduled drop, the Drop button or the assistant.
2. The patient is asked: "Would you like a quick well-being check-in about your mood, stress and
   sleep? … Please say yes or no."
3. On **yes**, the check-in asks four questions, each with an optional note in the patient's own
   words:
   * mood (good / okay / low)
   * stress (low / medium / high)
   * sleep (good / okay / poor)
   * whether they want support from a person
4. The consent question at the start says that saved answers are visible to their doctor and
   family.
5. If the patient saves, the check-in appears in a new **Well-being check-ins** section of History
   in **both** portals, linked to the pill it followed ("After Vitamin C (container 1) dropped at
   …").
6. Only the patient can delete a check-in.

The patient can also start a check-in at any time with "start a well-being check-in" or the
Assistant-view button. That check-in has no linked drop.

### Pieces

* **`tactidose-wellbeing/`**: an independent package, `tactidose_wellbeing`.
  * Pure `domain/`: questions, deterministic parser, state machine, configurable urgent phrases.
  * `WellbeingService`: the only workflow entry point. It handles ownership, idempotency by
    `request_id`, step guards, expiry, persistence and events.
  * The `CheckinRepository` protocol, plus its own FastAPI app and agent adapter.
  * It knows nothing about TactiDose. The one host hook added for this integration is
    `WellbeingService(consent_notice=...)`, a sentence read with the consent question.
* **`tactidose/wellbeing.py`** is the host side. It reacts to drops but never influences them.

### Host-side pieces (`tactidose/wellbeing.py`)

* `build_wellbeing(settings, db=, clock=, bus=)` creates a `WellbeingBridge`.
  * It builds a `WellbeingService` on `DatabaseCheckinRepository` and an in-memory session store.
    Open, unsaved check-ins never touch the DB.
  * It passes `consent_notice=CONSENT_NOTICE` and uses the TactiDose clock.
  * It returns `None` when `TACTIDOSE_WELLBEING_ENABLED=false` or the package is not installed.
    The app then runs exactly as before.
* `DatabaseCheckinRepository` implements the package's `CheckinRepository` on the main database.
  * Tables (`db/models.py`): `wellbeing_checkins` (`record_id` unique, `patient_id`, `drop_id` →
    `pill_drops`, times, `support_requested`, share flags) and `wellbeing_answers` (`question_id`,
    `answer_value`, `status`, verbatim `note_text`).
  * Only finished check-ins the patient consented to save are written.
  * The drop link: `link_session(session_id, drop_id)` when the check-in starts, then written on
    save. The package derives `rec_<x>` from `ses_<x>`.
  * Saves and deletes publish `Topic.PATIENT_STATUS {reason: "wellbeing"}`.
  * Demo reset wipes both tables (`db/seed.DYNAMIC_MODELS`).
* **After-drop offer**: a bus listener on `Topic.DROP` reacts to `status == "DROPPED"`.
  * `offer_after_drop` makes one offer per drop, rate-limited by
    `wellbeing_after_drop_gap_minutes`, and not while a check-in is open.
  * It publishes `Topic.WELLBEING_PROMPT {user_id, patient_id, offer_id, drop_id, text}`, which
    SSE sends to **that patient only**.
  * Offers lapse after 30 min.
* `checkin_views(db, clock, pid, days)` is the read model behind
  **`GET /api/patients/{pid}/wellbeing`** (`api/checkins.py`, `PatientViewer`: patient + linked
  caregivers). It works even without the package installed.
* `TactiDoseIdentity` is the package's `IdentityProvider`, backed by TactiDose sessions.
  * A patient maps to the user id `tactidose-patient-<id>`.
  * A caregiver gets 403 `patient_only` on the package API, so they cannot run or delete
    check-ins. No session gets 401.
* `mount_wellbeing` mounts the package's own app at **`/api/wellbeing`** (`/api/wellbeing/v1/...`,
  Swagger at `/api/wellbeing/docs`). The patient UI deletes check-ins through it.

### Chat routing (`api/chat.py`)

`WellbeingBridge.handle_chat` runs **before** `AgentService.chat`.

* These always go to the agent, and a pending offer lapses:
  * emergency
  * "stop"/"cancel"
  * an explicit pill request
  * "what do I take now"
* With an **open check-in**, every other turn goes to the check-in.
* With a **pending offer**:
  * yes starts the check-in linked to the drop
  * no gets "Okay, no check-in this time."
  * anything else dismisses the offer and goes to the agent
* Otherwise only a start phrase or "read my check-ins" is taken.
* After an agent turn, `after_agent_turn(pid, text, out)` does one of three things:
  * appends the offer when that turn dropped a pill (`out["wellbeing"] = {kind: "offer",
    offer_id}`)
  * cancels an open check-in on "stop"
  * appends "Your well-being check-in is still open…"
* Check-in replies have the agent reply shape with `model: "wellbeing"`, `actions: []`, echo
  `messages` with string ids, and a `wellbeing` state object. **They are never written to
  `conversations`.** Check-ins reach caregivers only as saved records in the Well-being section,
  never as transcripts.

### UI

* `js/wellbeing.js` provides two things:
  * `createCheckinHistory` (the History section, used by `patient.js`, with Delete, and by
    `care/history.js`, read-only)
  * `speakAfter`, which says the offer once the "pill dropped" speech finishes
* `js/patient/assistant.js` and `js/kiosk.js` listen for `wellbeing.prompt`. They show and speak
  it, and dedupe by `offer_id` against the chat reply that may carry the same offer.
* `patient.html` adds the `#wellbeing-root` History section and the Assistant check-in button.
  `care.html` adds the `#cg-wellbeing-root` card in the History tab. Styles are in `css/base.css`
  (`.checkin-*`).

### Speech, health and settings

* Check-in replies can read the patient's note back, so they skip the server TTS (ElevenLabs)
  unless `TACTIDOSE_WELLBEING_SERVER_TTS=true`. The browser speaks them locally.
* The device-side voice loop does not run check-ins: its `Topic.SPOKEN` captions are broadcast to
  every portal. The offer still reaches the patient's portal or kiosk after a voice-loop drop.
* `/api/health` → `wellbeing: {available, mount, open_sessions, pending_offers, after_drop,
  server_tts}`.
* Settings (`TACTIDOSE_WELLBEING_*`):

  | Setting | Default | Meaning |
  |---|---|---|
  | `ENABLED` | true | turn the check-in on or off |
  | `AFTER_DROP` | true | offer a check-in after every drop |
  | `AFTER_DROP_GAP_MINUTES` | 120 | at most one offer per patient in this window |
  | `CONFIG_FILE` | none | urgent wording + *verified* crisis resources; none are built in |
  | `SESSION_TTL_S` | 1800 | inactivity timeout of an open check-in |
  | `SERVER_TTS` | false | speak check-in replies through the server TTS |

### Rules for check-in work

* Never let a check-in gate, delay or influence a medication action. The drop listener only
  *reacts* after the drop is committed. Never add scoring, diagnosis or inference.
* Saved answers are visible to linked doctor/family by owner decision (2026-10-04). Keep the
  consent notice truthful if that changes. Session-only answers, unconfirmed input and the
  patient's raw words are never stored or logged. The bridge logs only the patient id, step and
  status.
* Put check-in behaviour changes in the **package** (`domain/` or `service.py`), not in the
  bridge. Contract changes go through `contract.py` + `examples/generate_samples.py` +
  `docs/integration-contract.md`.
* `support.requested` returns a `handoff` with `contacted_anyone: false`. TactiDose does **not**
  notify anyone automatically. The answer is visible in the saved check-in ("Asked for support").

## Guided judge demo (`tactidose/guided/`, demo mode only)

MORNING / NOON / NIGHT maps to containers 1 / 2 / 3 and their seeded schedules (08:00 / 13:00 /
20:00).

### `runner.GuidedDemoRunner`

A deterministic state machine: no LLM picks the next step. It runs on one `guided-demo` thread per
patient and can be cancelled at every wait. Each slot goes:

1. **Ask:** "Do you want to take it?". `classify_yes_no` (built on `rules_agent.analyse`) reads the
   answer. Unclear twice counts as no.
2. **No:** `DropService.skip_dose` with the note "Declined…", so the dose becomes CANCELLED.
3. **Yes:** the simulated buzzer starts, then **one**
   `DropService.request_drop(source="schedule", dose_event_id=<slot dose>)`. A denial is said
   honestly with `describe_outcome`.
4. **If DROPPED:** "Did you take the pill?" → `PatientTools.execute(CONFIRM_PILL_TAKEN)`, so the
   dose goes DISPENSED → TAKEN.
5. **Check-in:** a free-text answer → `checkin.extract` (always rules; Gemini optional and
   validated).
   * Emergency or severe wording (rules only) → `phrases.EMERGENCY`, a `HEALTH_CONCERN`
     notification to the patient and care team, and the run ends.
6. **Pause** for `demo_pause_seconds`, then the next slot. After NIGHT: goodbye and a summary.

### Time

* Before each slot the demo clock moves **forward** with `Clock.travel_to` +
  `api.device.after_clock_change`, to `min(15, dose_early_minutes)` minutes before the dose. The
  window is open, but the scheduler's auto-drop starts only at `scheduled_at`, so the patient is
  asked first.
* Scheduled doses are never subject to the manual cooldown.
* There is **no new drop path and no relaxed rule.**
* `reset=True` (doctor/family only) moves the clock to the morning slot and runs `reset_demo`
  *there*, so no earlier dose turns MISSED.
* While a run is active, `WellbeingBridge.suppress_offers` pauses after-drop check-in offers.

### Storage, events, speech, UI

* **Storage:**
  * `guided_demo_slots`: answers, outcome, `drop_id`, `dose_event_id` and the extraction. Wiped by
    demo reset.
  * The transcript is a "Guided demo" conversation.
  * The pill and dose rows are the normal ones.
* **Events:** `Topic.DEMO_GUIDED` (SSE `demo.guided`, demo mode, users linked to the device's
  patient).
* **Speech:** each line carries an `audio_url` from `AgentService.speak` (ElevenLabs → cache →
  offline voice). The browser falls back to `speechSynthesis` and captions.
  * The fixed lines are `phrases.DEMO_*`, included in `CRITICAL_PHRASES` (`warm-tts-cache`).
* **API:** `api/guided.py`, under `/api/demo/guided/` (`start`, `answer`, `stop`, and `GET` for
  the state).
* **UI:** `js/guided.js`, the shared controls with a WebAudio `Buzzer` and the typed-answer
  fallback.
  * Kiosk: voice-first. It listens automatically after each question.
  * Demo panel: operator view with "fresh demo data".
* **CLI:**
  * `run --demo-pause-seconds N`
  * `guided-demo` (headless, simulator, scripted answers, prints the transcript and the DB rows;
    own data folder `<data_dir>/guided-demo`)
* **Settings:** `TACTIDOSE_DEMO_PAUSE_SECONDS` (7), `_ANSWER_TIMEOUT_S` (45), `_BUZZER_SECONDS`
  (5), `_CHECKIN_AI` (true).
* **Buzzer:** the runner only knows `tactidose.hardware.buzzer.Buzzer`
  (`on(ms)` / `off()` / `supports_hardware`).
  * Backends: `LaptopToneBuzzer` (default, the screen tone), `SerialBuzzer` (`BUZZER ON/OFF` via
    `HardwareClient`, falls back to the laptop tone on any failure), `BothBuzzer`, `NullBuzzer`.
    Chosen by `TACTIDOSE_BUZZER_BACKEND`.
  * Hardware values live in `tactidose/hardware/buzzer_config.py` and the firmware `config.h`
    "BUZZER (edit here)" block (`BUZZER_PIN` is a TODO, `-1`).
  * Protocol: §13 of `docs/SERIAL_PROTOCOL.md` (optional, probe-able, `STATUS`/`proto` unchanged).
  * Checklist: `docs/BUZZER.md`. CLI: `buzzer-test`.

### `checkin.py`

* `rules_extract` uses a keyword vocabulary with negation handling.
* `GeminiCheckinExtractor` follows the same pattern as the other Gemini jobs: a no-redirect client,
  the fallback model after a 404, and a circuit breaker on `agent_retry_after_s`.
* `validate` drops symptoms the patient did not say. Severity is never below the rules value, and
  only the rules decide `alert`.
* Never add advice or a diagnosis here.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,gemini,tidb,snowflake]"     # add "voice" where vosk has wheels; ".[all,dev]" = everything
pip install -e "./tactidose-wellbeing[dev]"        # optional check-in package

python -m tactidose run --sim                       # http://127.0.0.1:8000  (alex@/sam@/dr.lee@demo.tactidose, demo1234)
python -m tactidose doctor                          # environment / config check
python -m tactidose check-apis                      # live check of configured cloud keys

pytest -m "not native and not hardware"             # main suite (~70 s)
pytest tests/test_wellbeing_integration.py          # integration of the check-in
pytest tests/test_guided_demo.py                    # guided judge demo (simulator, frozen clock)
python -m tactidose guided-demo                     # headless guided demo with scripted answers
(cd tactidose-wellbeing && pytest)                  # check-in package suite (138 tests)
python -m tactidose conformance --target sim        # 40 protocol scenarios (32 + 8 buzzer)
sh firmware/native/build.sh --local                 # native firmware harness without Docker (macOS/Linux)
TACTIDOSE_NATIVE_HARNESS_CMD=$PWD/firmware/native/bin/harness python -m tactidose conformance --target native
```

Test notes:

* `tests/conftest.py` scrubs `TACTIDOSE_*` and cloud env vars and disables `.env`, so tests are
  hermetic.
* Several test modules import optional SDKs at import time (`google.genai`, `pymysql`,
  `sounddevice`/`numpy`), so install those extras before running the whole suite.
* `test_med_edges.py::test_eight_concurrent_requests_drop_exactly_one_pill` is timing-sensitive
  and occasionally fails on `main` too.
* Markers: `slow`, `native` (Docker/g++ firmware core), `hardware` (real ESP32).

## Conventions

* Services are thread-safe, start no threads themselves, and raise `medication.errors`
  (`ValidationError` 422 / `NotFoundError` 404 / `ConflictError` 409) for caregiver operations.
  They never raise for expected dose or hardware failures.
* Routers use `route_class=TactiRoute`, and errors map to `{"detail": "..."}` (`api/common.py`).
  Services come from `request.app.state.services`. Use `ServicesDep` and the permission deps in
  `auth/deps.py`.
* Use `clock.now()` everywhere (the demo clock can time-travel). Sessions and lockouts use real
  time.
* Cloud HTTP clients go through `integrations/netsafe.py` (no redirects with keys). Secrets only
  appear redacted.
* Replies may be spoken: keep them short, with no markdown or emoji (`agent/service.clean_reply`).
* Keep comment density and docstring style consistent with the surrounding module. Module
  docstrings document behaviour contracts.
