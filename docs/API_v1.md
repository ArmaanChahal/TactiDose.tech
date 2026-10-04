# TactiDose HTTP API (v1 — superseded by docs/API.md v2)

Served by `python -m tactidose run` at `http://127.0.0.1:8000` (FastAPI; interactive docs at `/docs`).
The UI in `tactidose/ui/static/` uses only these endpoints.

* JSON everywhere. Errors: HTTP 4xx/5xx with `{"detail": "<human readable>"}`.
* Datetimes: ISO-8601 strings with offset. `*_local` fields are in the device timezone.
* **Caregiver PIN:** if `TACTIDOSE_CAREGIVER_PIN` is set, every non-GET endpoint under
  `/api/medications`, `/api/onboarding`, `/api/compartments`, `/api/schedules`,
  `/api/dose-events`, `/api/hardware`, `/api/demo` requires header `X-Caregiver-Pin: <pin>`
  (401 otherwise). Kiosk endpoints (`/api/intents`, `/api/state`, `/api/events`) never need it.
* **Demo-only:** `/api/demo/*` and `POST /api/hardware/command` return 403 unless
  `TACTIDOSE_DEMO_MODE=true` (the default for the hackathon).

## Shared objects

```jsonc
// DeviceSnapshot
{ "mode": "sim", "port": "sim://", "connected": true, "responsive": true,
  "state": "READY", "homed": true, "slot": 0, "target_slot": null, "gate": "CLOSED",
  "in_flight": null, "last_error": null, "fw_version": "sim-1.0.0", "num_slots_reported": 6,
  "last_rx_age_s": 0.4, "resets_seen": 1, "ready_for_motion": true, "compartment_label": "compartment 1" }

// DoseInfo
{ "event_id": 12, "label": "dose_12", "medication_id": 3, "medication_name": "Vitamin C (demo candy)",
  "strength": "1 piece", "instructions": "Demo only", "slot": 2, "compartment": "compartment 3",
  "compartment_number": 3, "scheduled_at": "2026-10-04T15:00:00+00:00",
  "scheduled_local": "2026-10-04T08:00:00-07:00", "status": "DUE", "dispensed_at": null,
  "confirmed_taken_at": null, "needs_review": false, "attempts": 0, "hardware_result": null }

// Reply (assistant)
{ "intent": "DISPENSE", "source": "ui", "text": "Your Vitamin C is ready ...", "kind": "success",
  "outcome": { /* DispenseOutcome/ConfirmOutcome/CancelOutcome/DueSummary .to_dict() */ }, "spoken": true }

// CommandResultView
{ "command": "MOVE_SLOT 3", "ok": true, "code": "AT_SLOT", "definitive": true,
  "gate_may_be_open": false, "elapsed_s": 1.73, "messages": ["OK MOVING 3", "OK AT_SLOT 3"],
  "hardware_result": "OK AT_SLOT" }
```

## Kiosk / device

| Method & path | Body | Response |
|---|---|---|
| `GET /api/health` | – | `{ok, version, config:{…public settings…}, db:{ok, backend}, hardware: DeviceSnapshot, voice:{enabled, listening, muted, error}, tts:{provider, elevenlabs, offline, cache_entries}, time:{now_local, tz, travelling}}` |
| `GET /api/state` | – | `{device: DeviceSnapshot, assistant:{phase, last_reply: Reply\|null, awaiting: DoseInfo\|null}, due: DueSummary, now_local, demo_mode, voice:{…}}` — `phase` ∈ `IDLE`, `PREPARING`, `AWAITING_CONFIRMATION`, `ATTENTION` |
| `POST /api/intents` | `{"intent": "CHECK_DUE"\|"DISPENSE"\|"CONFIRM_TAKEN"\|"REPEAT"\|"CANCEL"\|"HELP"\|"PRIMARY_ACTION", "source"?: "ui"\|"keyboard"}` **or** `{"text": "what do i take now", "source"?: "keyboard"}` | `Reply` (waits until handled, ≤ 60 s). 422 for unknown intent names. |
| `GET /api/events` | – | **SSE** stream. Each message: `event: <topic>` and `data: {"seq", "topic", "data", "ts"}`. On connect the last 50 events are replayed. Comment keep-alive every 15 s. |
| `GET /api/log?limit=100&topics=device.line,assistant.spoken` | – | `[BusEvent]` most recent last |

`DueSummary`: `{now_local, due:[DoseInfo], awaiting_confirmation:[DoseInfo], accessed:[DoseInfo], blocked:[{dose: DoseInfo, reason}], next_upcoming: DoseInfo|null}`.

## Medications (confirmed records only)

| Method & path | Body | Response |
|---|---|---|
| `GET /api/medications?include_inactive=false` | – | `[Medication]` |
| `POST /api/medications` | `{name, strength?, instructions_text?, warnings?: [str], confirmed: true, confirmed_by?: str}` | 201 `Medication`; 422 if `confirmed` is not `true` |
| `PATCH /api/medications/{id}` | any of `{name, strength, instructions_text, warnings}` + `confirmed: true` (+`confirmed_by`) | `Medication` |
| `DELETE /api/medications/{id}` | – | `{ok: true}` (archives: inactive, compartment cleared, schedules deactivated) |

`Medication`: `{medication_id, name, strength, instructions_text, warnings:[str], source: "manual"|"label_scan"|"demo_seed", confirmed_by_user, confirmed_by, confirmed_at, active, slot: int|null, compartment_number: int|null, schedules:[Schedule]}`

## Label onboarding (Gemini → UNCONFIRMED → human confirmation)

| Method & path | Body | Response |
|---|---|---|
| `POST /api/onboarding/scan` | multipart form, field `image` (JPEG/PNG/WebP, ≤ 8 MB) | `LabelScan` (always 200; failures have `status: "FAILED"` and a `user_message`) |
| `GET /api/onboarding/scans?status=PENDING_REVIEW` | – | `[LabelScan]` newest first |
| `POST /api/onboarding/scans/{id}/confirm` | `{name, strength?, instructions_text?, warnings?: [str], confirmed: true, confirmed_by?: str}` (the human-reviewed values) | 201 `Medication`; 409 if the scan is not PENDING_REVIEW; 422 if not confirmed |
| `POST /api/onboarding/scans/{id}/reject` | – | `LabelScan` |

`LabelScan`: `{scan_id, status: "PENDING_REVIEW"|"CONFIRMED"|"REJECTED"|"FAILED", extracted: {medication_name, strength, visible_instructions, warnings_visible:[str], confidence_notes, legible}|null, model, error, user_message, created_at, reviewed_at, medication_id}`.
When the extractor is not configured the scan returns `FAILED` with
`user_message: "Label scanning is not available (no Gemini API key). Please enter the information manually."`

## Compartments

| Method & path | Body | Response |
|---|---|---|
| `GET /api/compartments` | – | `[Compartment]` (all `num_slots`, ordered by slot) |
| `PUT /api/compartments/{slot}` | `{medication_id: int\|null}` | `[Compartment]` (a medication occupies at most one slot; assigning moves it) |
| `POST /api/compartments/{slot}/present` | – | `{ok, result: CommandResultView, device}` – rotates to the slot and opens the gate for **loading**. 409 if a dose is awaiting confirmation or the device is busy. |
| `POST /api/compartments/{slot}/loaded` | – | `{ok, result, device}` – closes the gate, stamps `loaded_at` |

`Compartment`: `{slot, compartment_number, compartment_id, medication_id|null, medication_name|null, active, loaded_at|null}`

## Schedules

| Method & path | Body | Response |
|---|---|---|
| `GET /api/schedules` | – | `[Schedule]` |
| `POST /api/schedules` | `{medication_id, time_of_day: "HH:MM", frequency?: "DAILY"\|"WEEKLY", days_of_week?: ["MON",…]}` | 201 `Schedule`; 422 if the medication is not confirmed/active or input invalid |
| `PATCH /api/schedules/{id}` | any of `{time_of_day, frequency, days_of_week, active}` | `Schedule` |
| `DELETE /api/schedules/{id}` | – | `{ok: true}` (deactivates; future events cancelled) |

`Schedule`: `{schedule_id, medication_id, medication_name, time_of_day, frequency, days_of_week:[str], active, created_at}`

## Dose events (caregiver log & review)

| Method & path | Body | Response |
|---|---|---|
| `GET /api/dose-events?date=YYYY-MM-DD` | – (default: today, local) | `[DoseEventView]` ordered by time |
| `POST /api/dose-events/{id}/resolve` | `{accessed: bool, note?: str, by?: str}` | `DoseEventView` – for HARDWARE_ERROR/needs_review: accessed → DISPENSED, not accessed → DUE |
| `POST /api/dose-events/{id}/skip` | `{note?: str, by?: str}` | `DoseEventView` (CANCELLED) |
| `POST /api/dose-events/{id}/mark-taken` | `{by?: str}` | `DoseEventView` (only from DISPENSED) |

`DoseEventView` = `DoseInfo` + `{schedule_id, dispense_source, confirm_source, review_note, missed_at, cancelled_at}`.

## Hardware

| Method & path | Body | Response |
|---|---|---|
| `GET /api/hardware` | – | `DeviceSnapshot` |
| `POST /api/hardware/home` | – | `{ok, result: CommandResultView, device}` |
| `POST /api/hardware/stop` | – | same (always allowed) |
| `POST /api/hardware/close-gate` | – | same |
| `POST /api/hardware/reconnect` | – | `{ok, device}` |
| `POST /api/hardware/command` *(demo)* | `{line: "MOVE_SLOT 3"}` | same; 422 if the line is not a valid protocol command |

## Demo controls (demo mode only)

| Method & path | Body | Response |
|---|---|---|
| `GET /api/demo/clock` | – | `{now_local, now_utc, offset_s, travelling, tz}` |
| `POST /api/demo/clock` | one of `{local_time: "08:00"}`, `{local_datetime: "2026-10-04T08:00"}`, `{offset_minutes: 30}`, `{reset: true}` | clock state (a scheduler tick runs immediately) |
| `POST /api/demo/jump-to-next-dose` | – | `{clock, due: DueSummary}` (travels to the next scheduled dose time) |
| `POST /api/demo/dose-now` | `{medication_id?: int}` | `{schedule: Schedule, event: DoseEventView}` – schedules the medication at the current (demo) time so it is due immediately |
| `POST /api/demo/reset` | `{reseed?: bool}` | `{ok}` – clears dose events (+ re-seeds demo data if asked), resets the clock |
| `POST /api/demo/seed` | – | `{ok, created: bool}` |
| `GET /api/demo/simulator` | – | `{available, physical:{angle_deg, slot, gate_open, state, ...}, faults:{home_sensor_dead, motor_jam, unresponsive, brownout_on_gate, disconnect}}` |
| `POST /api/demo/simulator` | one of `{fault: name, enabled: bool}`, `{press: "CONFIRM"\|"CANCEL"}`, `{reboot: true}` | same as GET; 409 when not in sim mode |

## Analytics

| Method & path | Body | Response |
|---|---|---|
| `GET /api/analytics/summary?days=7` | – | `{window_days, totals:{scheduled, taken, accessed_unconfirmed, missed, cancelled, hardware_errors, pending}, adherence_rate, avg_confirm_delay_minutes, by_day:[{date, scheduled, taken, missed, rate}], by_time_window:[{time_window, scheduled, missed, miss_rate}], device_errors:[{code, count}], source: "local"}` |
| `GET /api/analytics/snowflake` | – | `{configured, last_sync, pending, sent, last_error}` |
| `POST /api/analytics/snowflake/sync` | – | same (after one sync attempt) |
| `GET /api/analytics/snowflake/report` | – | `{configured, queries:[{name, description, columns:[str], rows:[[...]]}], error}` |

## Pages

| Path | Page |
|---|---|
| `/` | Kiosk / touchscreen view (blind/low-vision friendly) |
| `/caregiver` | Caregiver setup: today, medications, label scan, compartments, schedules, device, analytics |
| `/demo` | Demo operator panel: simulated voice, hardware console, simulator faults, clock travel |
| `/static/*` | Assets |
