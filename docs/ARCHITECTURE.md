# TactiDose — architecture v2 (product structure of 2026-10-03)

Audience: the software team and the engineers/agents implementing each module.
Read with `docs/SERIAL_PROTOCOL.md` (hardware contract, v1.1) and `docs/API.md` (HTTP contract, v2).
Where this document conflicts with the original handoff, **this document wins**.

## 1. Product structure

| Area | Behaviour |
|---|---|
| Device | ESP32 with **3 pill containers**. The host asks it to **drop one pill** from a container (`DROP_SLOT n`). **Shared dispenser** (Wi-Fi mode, `TACTIDOSE_SHARED_DEVICE`): every patient has their own device record (containers, counts, cooldown, schedules), all served by the one ESP32. |
| Drops | Triggered by the **schedule** (automatic, even if the patient forgets), the patient's **Drop button** in the app, or the **AI agent** on the patient's behalf. Every request and outcome is stored (`pill_drops`). Every successful drop decrements that container's `pill_count`. |
| Cooldown | **One global cooldown** per device: after *any* drop, *manual/agent/button* drops of *any* pill are refused for `devices.manual_cooldown_minutes` (default 60). Scheduled drops are not blocked by it. |
| Double-dose guard | A scheduled dose is **satisfied** by any drop of the same medication from `scheduled_at − dose_early_minutes` onward; a satisfied dose is never auto-dropped again. |
| Notifications | "Pill dropped" (and problems: denied, failed, uncertain, low stock, empty, missed) are stored per recipient, pushed live (SSE) and shown in the app. |
| Agent | The patient talks to it (voice or text). It checks the database (status, history) and decides whether to request a pill; the deterministic rules above have the final say. All patient↔agent messages, tool calls and results are stored. |
| Reports | PDF for the last *N* days (drops, schedule adherence, inventory, conversation summary), stored in the DB, viewable in both portals, emailable to the doctor. |
| Portals | **Patient** and **doctor/family** with login. Caregivers link to a patient with the patient's database ID + link code. Only doctor/family can edit schedules, cooldown, containers/refills and medications. |
| Well-being check-in | Optional, non-clinical (`tactidose-wellbeing/` package via `tactidose/wellbeing.py`). Offered to the patient after every `DROPPED` pill (rate-limited); saved check-ins go to `wellbeing_checkins` / `wellbeing_answers` linked to the drop, visible to the patient and linked doctor/family (`GET /api/patients/{pid}/wellbeing`), deletable by the patient only. Its turns are answered before the agent and never stored in `conversations`; it never gates or influences drops. See `CLAUDE.md`. |
| Optional extras | Gemini label scanning, Snowflake analytics, TiDB, blind-friendly kiosk screen, offline device-side voice loop — kept behind config, off the main flow. |

## 2. Principles

1. **AI interprets; deterministic code authorizes and actuates.** The agent can only call
   `request_pill`; `DropService` re-checks every rule. A model mistake cannot over-dispense.
2. **Fail closed.** DB unreadable, device unavailable or a drop outcome uncertain ⇒ no (further)
   drop; uncertain drops are flagged `needs_review` and the cooldown starts as if it dropped.
3. **Every pill is accounted for.** One `pill_drops` row per request (DENIED included); counts are
   changed in the same transaction as the drop record.
4. **Least privilege.** Every `/api/patients/{pid}` call checks the session user against
   `care_links`; SSE is filtered per user; only patients' conversations are stored.
5. **Offline first.** SQLite + simulator/serial + rule-based agent + offline TTS/STT run with no
   internet; Gemini, ElevenLabs, SMTP, TiDB and Snowflake are optional layers.

## 3. Package map (v2) and ownership

```
tactidose/
├── app.py                    Services container, FastAPI app factory, lifespan, page routes          [platform]
├── __main__.py               CLI                                                                      [platform]
├── config.py · core/{bus,clock,interfaces}.py · db/* · hardware/protocol.py · conformance.*            [foundation]
├── hardware/                 serial client, simulator (+DROP_SLOT, pill counts, drop sensor)           [hardware]
├── medication/
│   ├── drops.py              DropService (implements DropServiceAPI): rules, inventory, auto-drops      [domain]
│   ├── notifications.py      NotificationService (implements NotificationServiceAPI)                   [domain]
│   ├── scheduler.py          schedules -> dose_events (unchanged semantics) + caregiver-only editing    [domain]
│   ├── compartments.py       containers: assignment + inventory (refill, thresholds)                    [domain]
│   ├── catalog.py · onboarding.py · errors.py · safety.py (v1 helpers kept where useful)               [domain]
│   └── analytics.py          local adherence analytics (used by reports and the optional dashboard)    [integrations]
├── agent/
│   ├── service.py            AgentService (implements AgentServiceAPI): conversation store + routing    [agent]
│   ├── tools.py              tool schemas + executor bound to one patient                              [agent]
│   ├── gemini_agent.py       Gemini function-calling loop (google-genai)                               [agent]
│   ├── rules_agent.py        offline deterministic agent (voice/intents.py based)                       [agent]
│   └── voice.py              server-side STT (Vosk, PCM16) + reply TTS to WAV (ElevenLabs -> offline)  [agent]
├── reports/
│   ├── data.py · stats.py    gather + compute report data                                               [reports]
│   ├── narrative.py          Gemini factual summary of conversations, rules fallback                   [reports]
│   ├── pdf.py                PDF rendering (fpdf2)                                                      [reports]
│   ├── mailer.py             SMTP send with PDF attachment; .eml to data/outbox when SMTP is absent     [reports]
│   └── service.py            ReportService (implements ReportServiceAPI)                                [reports]
├── auth/
│   ├── passwords.py          scrypt hashing (stdlib)                                                    [platform]
│   ├── service.py            AuthService (implements AuthServiceAPI): register/login/sessions/links    [platform]
│   └── deps.py               FastAPI dependencies: current_user, require_patient_access, ...            [platform]
├── api/                      routers per docs/API.md v2, SSE with per-user filtering                   [platform]
├── wellbeing.py              optional check-in bridge: after-drop offer, DB repository, identity, chat routing [extras]
├── voice/ · audio/ · integrations/ · core/phrases.py   (wave 1, reused; v1 core/assistant.py removed)    [agent / extras]
└── ui/static/                login, patient portal, care portal, demo panel, optional kiosk           [ui]
firmware/                     reference ESP32 firmware (+DROP_SLOT, 3 containers)                        [hardware]
```

## 4. Runtime and threading

| Thread | Does |
|---|---|
| uvicorn loop | HTTP + SSE. Blocking work (DB, drops, Gemini, PDF) runs in the threadpool. |
| `hw-reader` / `hw-supervisor` | serial link (unchanged from v1). |
| `sim-device` | simulator in sim mode. |
| `scheduler` | every `scheduler_tick_s` (and immediately after schedule edits / demo clock travel): `Scheduler.tick()` then `DropService.run_scheduled_drops()`. |
| `speaker` / `voice` | optional device-side voice loop (laptop mic/speaker) feeding `AgentService.chat` for the device's patient. |
| `analytics-sync` | optional Snowflake outbox sync. |

`DropService` serialises all hardware drops with one lock (non-blocking acquire ⇒ `DENIED/IN_PROGRESS`)
and a DB claim; `interrupt()` (STOP) bypasses it.

## 5. Drop rules (deterministic) — `medication/drops.py`

`request_drop(patient_id, source, slot=None | medication_id=None, requested_by_user_id, conversation_id, dose_event_id)`

Checks, in order (the first failing check produces a `DENIED` row with that reason; nothing is sent
to the hardware):

1. **Device** — the patient has a device (`devices.user_id`); else `DEVICE_UNAVAILABLE`.
2. **Target** — resolve the container: by `slot` (0..num_slots-1) or by `medication_id` (its assigned
   active compartment). Unknown ⇒ `UNKNOWN_MEDICATION`; slot without an active, *confirmed*
   medication ⇒ `NO_MEDICATION`.
3. **Pending review** — an `UNCERTAIN` drop with `needs_review` on this device ⇒ `NEEDS_REVIEW`
   (a doctor/family member must resolve it first; prevents double dosing after a glitch).
4. **Cooldown** (sources `manual`, `agent`, `button`) — `now < last_drop_at + cooldown` where
   `last_drop_at` is the latest `DROPPED` *or* `UNCERTAIN` drop of **any** pill on the device ⇒
   `COOLDOWN` with `cooldown_remaining_s` / `next_allowed_at`. Cooldown 0 disables the check. This
   doctor/family cooldown is the **only** wait on request (no fixed per-pill floor, removed 2026-10-04).
5. **Scheduled satisfaction** (source `schedule`) — the dose event is already `DISPENSED/TAKEN`, or
   the same medication was `DROPPED/UNCERTAIN` at/after `min(scheduled_at − dose_early_minutes,
   scheduled_at − min_dose_interval_minutes)` ⇒ `ALREADY_SATISFIED` (the event is linked to that
   earlier drop and marked `DISPENSED`). Example: a manual drop at 07:20 satisfies the 08:00 dose
   (window opens 07:30, minimum interval 60 min), so no second pill drops at 08:00.
6. **Inventory** — `pill_count <= 0` ⇒ `EMPTY` (+ `EMPTY` notification).
7. **Concurrency** — the drop lock is held ⇒ `IN_PROGRESS`.
8. **Hardware readiness** — not connected / `FAULT` ⇒ `DEVICE_UNAVAILABLE`; `SAFE_STOP`/unhomed ⇒
   `HOME` first (if `hw_auto_home`); gate open ⇒ `CLOSE_GATE` first; failure ⇒ `DEVICE_UNAVAILABLE`.

Then: insert the `pill_drops` row as *in flight* (`status='UNCERTAIN'`, `completed_at` NULL — so a
crash mid-drop leaves an uncertain record, never a silent one), send `DROP_SLOT n` (or the v1
emulation `DISPENSE_SLOT n` → wait `drop_close_delay_ms` → `CLOSE_GATE` when the device does not
report `proto ≥ 1.1`), and finalise the row from `protocol.drop_certainty(result)`:

| Hardware outcome | `pill_drops.status` | Inventory | Dose event (if scheduled / matching) | Notifications |
|---|---|---|---|---|
| DROPPED | `DROPPED` | `pill_count − 1` | `DISPENSED`, `drop_id`, `dispensed_at` | PILL_DROPPED (+ LOW_STOCK / EMPTY when crossing thresholds) |
| NOT_DROPPED (`ERR …`) | `FAILED`, reason = code | unchanged | scheduled: `HARDWARE_ERROR`, retry at `now + auto_drop_retry_minutes` while in window | DROP_FAILED |
| `ERR NO_PILL` | `FAILED`, reason `NO_PILL` | set to 0 (the container is physically empty) | as above | EMPTY |
| UNCERTAIN | `UNCERTAIN`, `needs_review=True` | unchanged until reviewed | scheduled: `HARDWARE_ERROR`, `needs_review`, **no retry** | DROP_UNCERTAIN to patient and caregivers |

A manual/agent `DROPPED` drop also satisfies today's matching due/scheduled dose of that medication
whose window has opened (so the auto-drop will not repeat it).

Startup recovery: any `pill_drops` row left in-flight (no `completed_at`) ⇒ `UNCERTAIN` +
`needs_review`. Caregiver resolution (`resolve_drop(dropped: bool)`) adjusts inventory and clears the
review flag.

Every state change: same transaction ⇒ `pill_drops`, `compartments.pill_count`, `dose_events`,
`notifications`, `device_log`, `analytics_outbox` (optional); after commit ⇒ bus events.

## 6. Scheduled auto-drops

`run_scheduled_drops()` (scheduler thread): for each dose event on the device with
`scheduled_at ≤ now ≤ scheduled_at + dose_late_minutes`, status in {SCHEDULED, DUE, HARDWARE_ERROR
(without needs_review, `next_attempt_at ≤ now`)} and `devices.auto_drop_enabled` ⇒
`request_drop(source="schedule", dose_event_id=…)`, oldest first, one at a time. After the window
closes, undispensed doses become `MISSED` (Scheduler.refresh) with a MISSED_DOSE notification to
the patient and caregivers.

## 7. Conversational agent — `agent/`

* `AgentService.chat(patient_id, text, input_mode, conversation_id)`:
  1. open/continue a `conversations` row (a new one when the last message is > 30 min old);
  2. store the user message (`role="user"`, `input_mode`);
  3. run the provider (`gemini` via function calling, or `rules`), executing tool calls through
     `tools.py` bound to *this* patient (the model never chooses the patient id);
  4. store every tool call (`role="tool"`, `tool_name`, `tool_args`, `tool_result`) and the final
     reply (`role="assistant"`, `model`);
  5. publish `agent` events (drop events come from DropService, never from the agent); return `AgentReply`.
* **Tools** (JSON-schema function declarations):
  `get_patient_status()` → PatientStatus (containers, cooldown remaining, last drop, today's doses,
  next scheduled); `get_recent_drops(days≤14)`; `request_pill(container_number? | medication_name?,
  reason)` → DropOutcome (source `agent`); `confirm_pill_taken(medication_name?)` → marks the most
  recent DISPENSED dose TAKEN (optional extra signal).
* **System prompt rules:** you are the assistant inside the patient's pill dispenser; be brief and
  clear (replies may be spoken; ≤ 3 short sentences); always call `get_patient_status` before
  deciding about a pill; only `request_pill` when the patient asks for a pill (or confirms they
  want one) and status says it is allowed; never claim a pill dropped unless the tool returned
  DROPPED; explain refusals using the tool's message (cooldown time, empty container, already
  dropped); never diagnose, recommend, change doses or suggest extra pills; for symptoms or
  side-effects suggest contacting their doctor, and for emergencies (chest pain, trouble breathing,
  overdose) tell them to call emergency services (911) immediately; never reveal other people's data.
* **Rules agent (offline fallback):** `voice/intents.parse_intent` + keyword matching for container
  numbers / medication names; same tools, same storage; deterministic replies.
* **Voice:** browser mic → Web Speech API when available, otherwise 16 kHz PCM16 upload to
  `/api/agent/transcribe` (Vosk, full vocabulary). Replies: `speak=true` renders WAV via the TTS chain
  (ElevenLabs → cache → offline OS voice) served from `/api/agent/audio/{id}.wav`; the browser falls back
  to `speechSynthesis`. The optional device-side loop (laptop mic + speaker) reuses VoiceRecognizer +
  SpeakerService and routes text to `AgentService.chat` for the device's patient.

## 8. Reports — `reports/`

`ReportService.generate(patient_id, days, created_by_user_id)`:
* **Data** for `[now − days, now]`: patient + device, medications/containers/inventory, schedules, dose
  events (status counts, on-time vs late vs missed), all `pill_drops` (by source/status, denied
  reasons, uncertain), notifications of kind LOW_STOCK/EMPTY/MISSED, conversations + messages.
* **Stats** (stored in `reports.stats` JSON): scheduled doses, dropped (on time ≤ 15 min, late), missed,
  adherence rate = dispensed ÷ (dispensed + missed) for scheduled doses, manual / agent drops, denied
  requests by reason, uncertain drops, per-medication table, per-day table, current pill counts and
  estimated days of supply (pill_count ÷ scheduled doses/day).
* **Narrative:** Gemini (when configured and `report_ai_summary`) summarises the conversations
  factually — what the patient asked for, concerns/symptoms they *mentioned* (quoted, not
  interpreted), refused requests — with an explicit instruction not to diagnose or recommend;
  fallback: deterministic bullet summary. `narrative_source` records which.
* **PDF** (fpdf2): title page header (patient, period, generated at/by), summary tiles, adherence-by-day
  bar chart + table, per-medication table, missed / failed / uncertain list, inventory, narrative, selected
  conversation excerpts (timestamped), footer disclaimer ("Prototype — not a medical device …") and page
  numbers. Unicode-safe (TTF font when available, else latin-1 sanitising).
* Stored in `reports.pdf` (LONGBLOB on TiDB); `REPORT_READY` notification to the creator.
* **Send:** `send(report_id, to_email=None)` ⇒ every linked doctor's email (or the given address);
  SMTP with STARTTLS/SSL; when SMTP is not configured the message is written to
  `data/outbox/report-<id>-<ts>.eml` and the delivery status is `SAVED`. Each attempt is a
  `report_deliveries` row.

## 9. Accounts, access and sessions — `auth/`

* Passwords: `hashlib.scrypt` (n=2¹⁴, r=8, p=1, 16-byte salt), stored as
  `scrypt$n$r$p$salt_b64$hash_b64`; constant-time compare; minimum 8 characters.
* Sessions: `secrets.token_urlsafe(32)`; DB stores SHA-256 of the token; TTL `session_ttl_hours`
  (sliding `last_seen_at`); cookie `td_session` HttpOnly, SameSite=Lax, Secure when `cookie_secure`.
* Registration: patient ⇒ gets `link_code` (8 chars, unambiguous alphabet) and, if the configured
  device has no real patient yet, the device is bound to them. Doctor/family ⇒ link later via
  `POST /api/care/links {patient_id, link_code}`.
* Permission matrix:

| Action | Patient (self) | Linked doctor/family | Others |
|---|---|---|---|
| View status, containers, schedules, drops, doses, conversations, reports, notifications | ✓ | ✓ | ✗ |
| Drop a pill (manual) / chat with the agent | ✓ | ✗ | ✗ |
| Edit schedules, cooldown, containers/refills, medications, resolve reviews, skip doses | ✗ | ✓ | ✗ |
| Generate a report / send it to the doctor | ✓ | ✓ | ✗ |
| Device home/reconnect | ✗ | ✓ | ✗ |
| Device stop | ✓ | ✓ | ✗ |

* Demo mode seeds: patient `alex@demo.tactidose`, family `sam@demo.tactidose`, doctor
  `dr.lee@demo.tactidose`, password `demo1234` (configurable), linked, with 3 demo "pills" (candy).

## 10. Notifications

`NotificationService.notify(patient_id, kind, title, body, data, to_patient, to_caregivers)` stores one
row per recipient and publishes `Topic.NOTIFICATION`; the SSE endpoint forwards it to that recipient.
The portals show a live toast, a bell list and (with permission) a browser `Notification`; the patient
portal also speaks "pill dropped" when audio is enabled.

| Kind | Patient | Caregivers |
|---|---|---|
| PILL_DROPPED | ✓ | if `notify_caregivers_on_drop` |
| DROP_DENIED (cooldown etc.) | (shown inline, not stored) | ✗ |
| DROP_FAILED / DROP_UNCERTAIN / DEVICE_ALERT | ✓ | ✓ |
| LOW_STOCK / EMPTY | ✓ | ✓ |
| MISSED_DOSE | ✓ | ✓ |
| REPORT_READY / REPORT_SENT | creator | creator |

## 11. Wiring (`app.py`)

```python
settings, clock, bus, db = Settings(), Clock(settings.timezone), EventBus(), Database(settings)
hardware, sim     = create_hardware(settings, bus=bus, clock=clock)
notifications     = NotificationService(db, settings, clock, bus=bus)
compartments      = CompartmentService(db, settings, bus=bus, clock=clock)
catalog           = MedicationCatalog(db, settings, clock, bus=bus)
scheduler         = Scheduler(db, clock, settings, bus=bus, notifications=notifications)
drops             = DropService(db, hardware, clock, settings, notifications=notifications, bus=bus)
auth              = AuthService(db, settings, clock, bus=bus)
agent             = AgentService(db, drops, clock, settings, bus=bus)          # provider per settings
reports           = ReportService(db, clock, settings, auth=auth, notifications=notifications, bus=bus)
extractor/onboarding/speaker/recognizer/analytics_sync  — optional extras
startup: create_all → seed (demo) → drops.recover_on_startup() → scheduler loop → hardware.start() → extras
```

## 12. Failure handling (v2)

| Failure | Behaviour |
|---|---|
| Internet down | Drops, schedule, cooldown, rule-based agent, offline TTS, PDF generation all work; Gemini → rules agent and rules narrative; ElevenLabs → offline voice; SMTP → `.eml` saved in `data/outbox`. |
| DB error | `DENIED/DB_ERROR`, nothing sent to the device. |
| Device disconnected / FAULT | `DENIED/DEVICE_UNAVAILABLE`; scheduled doses retry until their window closes, then MISSED + notification. |
| Uncertain drop | `UNCERTAIN` + `needs_review`; cooldown applies; further drops `NEEDS_REVIEW` until a caregiver resolves it. |
| Empty container | `DENIED/EMPTY` (or `ERR NO_PILL` from the drop sensor) + EMPTY notification. |
| Agent tool error | reply "I can't do that right now — please use the Drop button or ask your caregiver."; logged. |
| Gemini error, quota or blocked network | the rules agent answers (`model="rules (fallback)"`) and Gemini is skipped for `agent_retry_after_s` (§13). |

## 13. Facts from the wave-1 build (binding for v2 work)

**Hardware (`hardware/serial_client.py`, `simulator.py`)**
* `hardware, sim = create_hardware(settings, bus=bus, clock=clock)`. In sim mode the
  **HardwareClient owns the SimulatedDevice**: `hardware.start()` opens the in-process link and
  boots the sim; `hardware.close()` closes it. The app must not start/close `sim` itself; `sim` is
  only for the demo panel (`physical()`, `faults()`, `set_fault()`, `press()`, `reboot()`).
* `start()` never blocks or raises; `connected` becomes True only after a PING+STATUS handshake.
  Auto-home only right after (re)connecting. `reconnect() -> bool`. `exchange_raw()` is diagnostics only.
* A concurrent user command gets `BUSY_LOCAL` immediately (it waits only for a heartbeat probe).
  After a TIMEOUT the next command first resyncs with STATUS; if that fails the command is not sent
  (`NOT_CONNECTED`). A write that raises counts as written (`DISCONNECTED`, uncertain).
* Event listeners also receive unsolicited `ERR HOME_TIMEOUT` / `ERR MOTOR_FAULT`.
* Every command line is published on `Topic.DEVICE_LINE {dir:"tx"}` (heartbeat PINGs included).

**Domain (`medication/`)** — all services are thread-safe, start no threads, raise
`errors.ValidationError` (422) / `NotFoundError` (404) / `ConflictError` (409) for caregiver ops and
never raise for expected dose/hardware failures. Serializers: `scheduler.schedule_to_dict`,
`catalog.medication_to_dict`, `compartments.compartment_to_dict`, `drops.drop_to_view` / `drops.dose_to_view`,
`onboarding.scan_to_dict`. Schedule edits keep still-matching occurrences, delete stale untouched
future events, cancel other stale open ones, and reset `Schedule.created_at` (no invented past
MISSED doses). `log_event(..., at=clock.now())` keeps audit rows on the demo clock.

**Voice/audio (`voice/`, `audio/`; the v1 `core/assistant.py` was replaced by `agent/voice_loop.py`)** — `Topic.SPOKEN` is published only by
`SpeakerService` (once per utterance, right before playback). The physical cancel button only
*enqueues* CANCEL (the firmware already stopped locally); UI/voice/API cancel calls `interrupt()`
synchronously. `VoiceRecognizer.start()` blocks while the Vosk model loads (1–16 s): call it last,
or from a background thread. Offline Windows speech costs ~1.9 s per new sentence (cached after).
Raw Vosk text keeps `[unk]` tokens; pills are never dispensed/confirmed from text containing `[unk]`.

**Integrations** — `create_label_extractor(settings)` returns None when disabled/no key.
`SnowflakeSync` is safe to construct when not configured. Gemini: default sampling everywhere (no
`temperature`; a low temperature can make Gemini 3 models loop or truncate).

**Cloud calls (API-keys round)**
* **No redirects with a key.** Every cloud HTTP client is built with `integrations/netsafe.py`
  (`NO_REDIRECTS` for httpx, `gemini_http_options` for google-genai): keys travel in custom headers
  (`x-goog-api-key`, `xi-api-key`), which httpx keeps on cross-origin redirects. Any 3xx means
  `BLOCKED_BY_NETWORK` (a web filter's sign-in or block page). `classify_status` /
  `classify_exception` map failures to the netsafe codes (`OK`, `NOT_CONFIGURED`,
  `BLOCKED_BY_NETWORK`, `TLS_ERROR`, `TIMEOUT`, `NETWORK_ERROR`, `INVALID_KEY`, `PERMISSION_DENIED`,
  `QUOTA_EXCEEDED`, `NOT_FOUND`, `BAD_REQUEST`, `SERVER_ERROR`, `ERROR`) for logs, fallbacks and
  `check-apis`. Secrets only appear as `netsafe.redact(...)`.
* **`python -m tactidose check-apis [--only gemini,elevenlabs,snowflake,tidb,smtp] [--json]`**
  (`integrations/live_check.py`) sends one tiny live request per *configured* service. Rows are
  `gemini`, `gemini-agent`, `elevenlabs`, `snowflake`, `tidb` and `smtp`, after a first line with
  the `.env` path it read. It exits 0 unless a configured row failed (`OK`, `WARN` and
  `NOT_CONFIGURED` pass).
* **Assistant circuit breaker** (`AgentService`): after a Gemini failure, the rules agent answers
  at once (`model="rules (fallback)"`) for `agent_retry_after_s` seconds (default 60). This is
  monotonic time, so demo clock travel does not count; 0 = try Gemini every turn. The first turn
  after the pause tries Gemini again, and a success closes the breaker. `/api/health` shows
  `agent.gemini_retry_in_s` and `agent.gemini_last_error` (a netsafe code). `agent_thinking_level`
  ("" = the model's default) sets the agent's Gemini thinking level.
* **ElevenLabs auto voice** (`elevenlabs_auto_voice`, default on): when the account cannot use the
  configured voice (e.g. the legacy default "George" on accounts created after March 2026), the
  client lists the account's voices, switches to the first premade one, logs the
  `ELEVENLABS_VOICE_ID` to set and retries once. If the list cannot be fetched, the original error
  stands and the offline voice speaks.
* **TiDB CA** (`db/session.tidb_ssl_ca`): `TIDB_SSL_CA` if set, else the first existing file named
  by the `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` environment variables (a TLS-inspecting proxy's
  bundle), else certifi. The engine and `init-db` share it.
* **Turn guard: negated fragments** (`rules_agent.analyse`): a fragment that names what NOT to drop
  sets `drop_negated`, so `turn_guard` blocks `request_pill` for every provider. That is a typed
  clause starting with "not" (but not "not feeling / well / good"), or, anywhere, "not" followed
  by the / this / my / those / these / that or a pill word. Examples: "not the calcium", "no, not
  that one", "can I have my pill, not the calcium". A hedge that ends its own clause ("I'm not
  sure, drop my pill") negates nothing; unpunctuated speech ("im not sure drop my pill") fails
  closed. For such a request the rules agent asks "Which pill would you like?" without the negated
  containers. It also never drops a medication named right after "not" ("my pill not calcium"),
  which the text-only guard cannot recognise for Gemini.

**UI serving** — mount `tactidose/ui/static` at `/static`; on Windows call
`mimetypes.add_type("text/javascript", ".js")` (plus `.css`, `.svg`) **before** mounting, or
browsers refuse ES modules. SSE: `event: <topic>` + JSON data, `Cache-Control: no-cache`,
`X-Accel-Buffering: no`, no gzip.

**Native harness (`firmware/native`)** — protocol in `ARCHITECTURE_v1.md` §7 plus v1.1 directive
`!pills <slot> <count>` (sets a container's physical pill count; drop sensor present by default,
20 pills per container) and the optional `!peek <max_ms>` speed-up used by `NativeTarget`.
`!boot none` leaves the physical sensor mode unchanged.

**v2 status (after wave 2)** — `DROP_SLOT` is implemented in the simulator, the host client, `commands`
and the firmware (both mechanisms); all 32 conformance scenarios pass on the simulator and the native
core. `hardware.drop_slot()` returns at `OK DROPPED n` / `ERR NO_PILL`; against v1 firmware it holds
the command lock for the whole `DISPENSE_SLOT` + `drop_close_delay_ms` + `CLOSE_GATE` emulation.
Simulator fault `brownout_on_release` resets the board after `OK GATE_OPEN` (host: UNCERTAIN).
`sim.set_pills()` changes the *physical* simulated count only, never `compartments.pill_count`.
The v1 consent/gate flow (`medication/dispense.py`) and the v1 assistant (`core/assistant.py`) were
deleted: `DropService` is the only code path that drops pills.

**Notifications beyond §10** — `DEVICE_ALERT` once per scheduled dose whose auto-drop is refused as
`DEVICE_UNAVAILABLE`; scheduled requests DENIED for `EMPTY`, `NO_MEDICATION`, `UNKNOWN_MEDICATION`
or `DEVICE_UNAVAILABLE` set `next_attempt_at = now + auto_drop_retry_minutes` on the DUE dose and
retry until the window closes. `NotificationService.notify(..., user_ids=[...])` targets single
accounts (used for REPORT_READY / REPORT_SENT to the creator).

**Accounts** — sessions and lockouts use the clock *without* the demo travel offset, so time travel
never signs anyone out. Login and link-code attempts are rate-limited (5 failures → 30 s, HTTP 429).
