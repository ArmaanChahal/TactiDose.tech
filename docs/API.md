# TactiDose HTTP API (v2)

Served by `python -m tactidose run` at `http://127.0.0.1:8000` (FastAPI; interactive docs at `/docs`).
The UI in `tactidose/ui/static/` uses only these endpoints.

* JSON everywhere unless stated. Errors: HTTP 4xx/5xx with `{"detail": "<human readable>"}`.
* Datetimes: ISO-8601 with offset. `*_local` fields are in the device timezone.
* **Authentication:** `POST /api/auth/login` sets an HttpOnly cookie `td_session` (SameSite=Lax) and also
  returns the token; API clients may send `Authorization: Bearer <token>` instead. Every endpoint except
  `/api/health`, `/api/auth/login` and `/api/auth/register` requires a session (401 otherwise).
* **Roles:** `patient`, `doctor`, `family` (doctor and family are *caregivers*).
* **Access rule for `/api/patients/{pid}/…`:** allowed for the patient themself (`user_id == pid`) and for
  caregivers linked to `pid` (`care_links`); 403 otherwise. Endpoints marked **(caregiver)** additionally
  require a linked doctor/family account — patients can view but not change schedules, cooldown,
  containers/refills or medications. Endpoints marked **(patient)** are only for the patient themself.
* **Demo-only** endpoints (`/api/demo/*`, raw hardware commands) return 403 unless `TACTIDOSE_DEMO_MODE=true`;
  in demo mode they also require a logged-in user.

## Shared objects

```jsonc
// User
{ "user_id": 1, "email": "alex@demo.tactidose", "display_name": "Alex Rivera", "role": "patient",
  "phone": null, "created_at": "2026-10-03T22:00:00+00:00" }

// ContainerInfo  (container_number = slot + 1)
{ "slot": 0, "container_number": 1, "compartment_id": 1, "medication_id": 3,
  "medication_name": "Vitamin C (demo candy)", "strength": "1 piece", "pill_count": 12, "capacity": 30,
  "low_stock_threshold": 3, "low_stock": false, "empty": false, "loaded_at": null }

// DropOutcome
{ "status": "DROPPED" | "DENIED" | "FAILED" | "UNCERTAIN",
  "reason": null | "COOLDOWN" | "EMPTY" | "NO_MEDICATION" | "UNKNOWN_MEDICATION" | "ALREADY_SATISFIED" |
            "IN_PROGRESS" | "DEVICE_UNAVAILABLE" | "NEEDS_REVIEW" | "NOT_ALLOWED" | "DB_ERROR" | "<hardware code>",
  "message": "Vitamin C dropped from container 1.",           // deterministic, human readable
  "drop_id": 41, "slot": 0, "container_number": 1, "medication_id": 3, "medication_name": "Vitamin C (demo candy)",
  "source": "manual" | "agent" | "schedule" | "button" | "demo",
  "pill_count_after": 11, "cooldown_remaining_s": 3600, "next_allowed_at": "2026-10-04T09:05:00-07:00",
  "hardware": "OK DROPPED" | null }

// PillDropView (one row of pill_drops)
{ "drop_id": 41, "patient_id": 1, "requested_at": "...", "completed_at": "...", "requested_local": "...",
  "slot": 0, "container_number": 1, "medication_id": 3, "medication_name": "Vitamin C (demo candy)",
  "source": "manual", "status": "DROPPED", "reason": null, "hardware_result": "OK DROPPED",
  "pill_count_before": 12, "pill_count_after": 11, "dose_event_id": 17, "conversation_id": null,
  "requested_by_user_id": 1, "needs_review": false, "review_note": null }

// DoseView (one scheduled occurrence)
{ "event_id": 17, "schedule_id": 2, "medication_id": 3, "medication_name": "...", "slot": 0,
  "container_number": 1, "scheduled_at": "...", "scheduled_local": "...", "status": "DISPENSED",
  "drop_id": 41, "dispensed_at": "...", "dispense_source": "manual", "confirmed_taken_at": null,
  "missed_at": null, "needs_review": false, "attempts": 1, "hardware_result": "OK DROPPED" }

// PatientStatus
{ "patient_id": 1, "display_name": "Alex Rivera", "now_local": "...",
  "containers": [ContainerInfo], "cooldown_minutes": 60, "cooldown_remaining_s": 0,
  "next_manual_allowed_at": null, "last_drop": PillDropView | null,
  "today": [DoseView], "next_scheduled": DoseView | null, "auto_drop_enabled": true,
  "device": DeviceSnapshot, "alerts": [{"kind": "LOW_STOCK", "message": "Container 2 has 2 pills left."}] }

// DeviceSnapshot
{ "mode": "sim", "port": "sim://", "connected": true, "responsive": true, "state": "READY", "homed": true,
  "slot": 0, "target_slot": null, "gate": "CLOSED", "in_flight": null, "last_error": null,
  "fw_version": "sim-1.1.0", "num_slots_reported": 3, "last_rx_age_s": 0.4, "resets_seen": 1,
  "ready_for_motion": true, "proto": "1.1" }

// Notification
{ "notification_id": 9, "patient_id": 1, "kind": "PILL_DROPPED", "title": "Pill dropped",
  "body": "Vitamin C (demo candy) dropped from container 1 at 8:00 AM.", "data": {"drop_id": 41},
  "created_at": "...", "read_at": null }

// AgentReply
{ "conversation_id": 5, "text": "Your Vitamin C was dropped at 8:00 AM, so I can't give another one yet...",
  "actions": [DropOutcome], "model": "gemini-3.8-flash" | "rules" | "rules (fallback)" | "rules (safety)", "audio_url": "/api/agent/audio/abc123.wav" | null,
  "messages": [Message] }

// Message (conversation log)
{ "message_id": 77, "conversation_id": 5, "role": "user" | "assistant" | "tool", "content": "Can I have my pill?",
  "input_mode": "voice" | "text" | null, "tool_name": null | "request_pill", "tool_args": {...} | null,
  "tool_result": {...} | null, "created_at": "..." }

// ReportMeta
{ "report_id": 3, "patient_id": 1, "title": "TactiDose report — Alex Rivera — last 7 days", "days": 7,
  "period_start": "...", "period_end": "...", "status": "READY", "pdf_size": 48211,
  "created_at": "...", "created_by_user_id": 2, "stats": {...}, "narrative": "...",
  "narrative_source": "gemini" | "rules", "pdf_url": "/api/reports/3/pdf",
  "deliveries": [{"delivery_id": 1, "to_email": "dr.lee@demo.tactidose", "status": "SENT" | "SAVED" | "FAILED", "error": null, "created_at": "..."}] }
```

## Auth & accounts

| Method & path | Body | Response |
|---|---|---|
| `POST /api/auth/register` | `{email, password (≥ 8 chars), display_name, role: "patient"\|"doctor"\|"family", phone?}` | 201 `{user, token, patient?: {patient_id, link_code}}` (logs in; sets cookie). 409 if the email exists; 403 if registration is disabled. |
| `POST /api/auth/login` | `{email, password}` | `{user, token}` (sets cookie). 401 on bad credentials (same message for unknown email and wrong password); 429 with `Retry-After` after 5 failures for one email (30 s lockout). |
| `POST /api/auth/logout` | – | `{ok: true}` (revokes the session, clears cookie) |
| `GET /api/auth/me` | – | `{user, patient?: {patient_id, link_code, device_id}, patients?: [CarePatient]}` — `patient` for patients, `patients` for caregivers |
| `GET /api/care/patients` *(caregiver)* | – | `[CarePatient]` where `CarePatient = {patient_id, display_name, relationship, last_drop: PillDropView\|null, unread_alerts: int, adherence_7d: float\|null}` |
| `POST /api/care/links` *(caregiver)* | `{patient_id, link_code}` | 201 `CarePatient` — link by the patient's database ID + the link code shown in the patient portal. 404/403 on mismatch; 429 with `Retry-After` after 5 wrong codes (30 s). |
| `DELETE /api/care/links/{patient_id}` *(caregiver)* | – | `{ok: true}` |

## Patient data (`/api/patients/{pid}/…`)

| Method & path | Body | Response |
|---|---|---|
| `GET …/status` | – | `PatientStatus` |
| `GET …/containers` | – | `[ContainerInfo]` (all slots, ordered) |
| `PUT …/containers/{slot}` *(caregiver)* | any of `{medication_id: int\|null, pill_count, capacity, low_stock_threshold}` | `ContainerInfo` |
| `POST …/containers/{slot}/refill` *(caregiver)* | `{set: int}` or `{add: int}` | `ContainerInfo` |
| `GET …/medications` | – | `[Medication]` |
| `POST …/medications` *(caregiver)* | `{name, strength?, instructions_text?, warnings?: [str], confirmed: true}` | 201 `Medication` (422 unless `confirmed` is `true`) |
| `PATCH …/medications/{mid}` *(caregiver)* | fields + `confirmed: true` | `Medication` |
| `DELETE …/medications/{mid}` *(caregiver)* | – | `{ok: true}` (archive) |
| `GET …/schedules` | – | `[Schedule]` |
| `POST …/schedules` *(caregiver)* | `{medication_id, time_of_day: "HH:MM", frequency?: "DAILY"\|"WEEKLY", days_of_week?: [..]}` | 201 `Schedule` |
| `PATCH …/schedules/{sid}` *(caregiver)* | any of `{time_of_day, frequency, days_of_week, active}` | `Schedule` |
| `DELETE …/schedules/{sid}` *(caregiver)* | – | `{ok: true}` |
| `GET …/settings` | – | `{manual_cooldown_minutes, auto_drop_enabled, device_id, num_slots}` |
| `PATCH …/settings` *(caregiver)* | any of `{manual_cooldown_minutes (0–1440), auto_drop_enabled}` | settings |
| `POST …/drops` *(patient)* | `{slot}` or `{medication_id}` | `DropOutcome` (HTTP 200 for DENIED/FAILED too — read `status`) |
| `GET …/drops?days=7&status=` | – | `[PillDropView]` newest first |
| `POST …/drops/{drop_id}/resolve` *(caregiver)* | `{dropped: bool, note?}` | `PillDropView` — resolves an UNCERTAIN drop (adjusts the pill count if it did drop) |
| `GET …/doses?date=YYYY-MM-DD` | – | `[DoseView]` (default today, local) |
| `POST …/doses/{event_id}/skip` *(caregiver)* | `{note?}` | `DoseView` (CANCELLED) |
| `GET …/conversations?limit=50` | – | `[{conversation_id, started_at, last_message_at, channel, title, message_count}]` |
| `GET …/conversations/{cid}/messages` | – | `[Message]` |
| `GET …/reports` | – | `[ReportMeta]` newest first |
| `POST …/reports` | `{days: 1–90}` | 201 `ReportMeta` (generated synchronously) |

Patient-only conversations: only messages exchanged between the patient and the agent are stored;
caregivers *read* them through the endpoints above but never create conversation records.

## Agent (patient only)

| Method & path | Body | Response |
|---|---|---|
| `POST /api/agent/chat` | `{text, conversation_id?, input_mode?: "text"\|"voice", speak?: bool}` | `AgentReply` (`audio_url` set when `speak` and TTS is available) |
| `POST /api/agent/transcribe` | raw 16-bit little-endian mono PCM at 16 kHz (`Content-Type: application/octet-stream`, ≤ 30 s) | `{text, confidence, engine: "vosk"}`; 503 if the offline recognizer is unavailable |
| `GET /api/agent/audio/{audio_id}.wav` | – | `audio/wav` (short-lived, only for the requesting patient) |

## Reports

| Method & path | Body | Response |
|---|---|---|
| `GET /api/reports/{rid}` | – | `ReportMeta` |
| `GET /api/reports/{rid}/pdf` | – | `application/pdf` (inline; `?download=1` for attachment) |
| `POST /api/reports/{rid}/send` | `{to_email?: str}` — omitted = every linked doctor's email | `{deliveries: [Delivery]}` — `SENT` via SMTP, `SAVED` (no SMTP configured: .eml written to `data/outbox/`), `FAILED`; 422 when no address is given and no doctor is linked |

Access to `/api/reports/{rid}…` follows the report's patient (patient themself or linked caregivers).

## Notifications & live events

| Method & path | Body | Response |
|---|---|---|
| `GET /api/notifications?unread=true&limit=50` | – | `[Notification]` for the current user |
| `POST /api/notifications/read` | `{ids?: [int]}` (omitted = all) | `{updated: int}` |
| `GET /api/events` | – | **SSE**, filtered per user: a patient receives events about themself; a caregiver about linked patients. Each message is `event: <bus topic>` + `data: {"seq", "topic", "data", "ts"}` (the same envelope as v1), comment keep-alive every 15 s, the last 50 *permitted* events replayed on connect. Topics (exact strings from `core/bus.py`): `notification` (Notification; only to its recipient `user_id`), `drop.updated` (PillDropView), `patient.status` (`{patient_id, reason}` = refetch hint), `agent.message` (`{patient_id, conversation_id, message_id, role}`), `report.updated` (`{patient_id, report_id, status}`), `device.state` (DeviceSnapshot; users linked to the device's patient). Demo mode adds `device.line`, `device.event`, `sim.physical`, `clock.changed`, `system.notice` for users linked to the device's patient. |

## Device & demo

| Method & path | Body | Response |
|---|---|---|
| `GET /api/device` | – | `DeviceSnapshot` (patient of the device or linked caregiver) |
| `POST /api/device/home` *(caregiver)* | – | `{ok, result: CommandResultView, device}` |
| `POST /api/device/stop` | – | same (patient or caregiver; always allowed) |
| `POST /api/device/reconnect` *(caregiver)* | – | `{ok, device}` |
| `POST /api/demo/command` *(demo)* | `{line: "DROP_SLOT 1"}` | `{ok, result, device}` |
| `GET/POST /api/demo/clock` *(demo)* | `{local_time: "08:00"}` (today) \| `{local_datetime: "2026-10-05T08:00"}` \| `{offset_minutes: 30}` (absolute offset from real time) \| `{reset: true}` | `{now_local, now_utc, offset_s, travelling, tz}` (runs a scheduler tick). Sessions ignore demo travel, so jumping never signs anyone out. |
| `POST /api/demo/jump-to-next-dose` *(demo)* | – | `{clock, next: DoseView\|null}` |
| `GET/POST /api/demo/simulator` *(demo)* | `{fault, enabled}` \| `{press: "CONFIRM"\|"CANCEL"}` \| `{reboot: true}` \| `{pills: {slot, count}}` | `{available, physical:{angle_deg, slot, target_slot, gate_open, state, releasing, pills:[physical count per container], pills_dropped, drop_sensor, proto, num_slots, fw_version, ...}, faults:{home_sensor_dead, motor_jam, unresponsive, brownout_on_gate, brownout_on_release, disconnect}}` — simulated *physical* pill counts are separate from the database's `pill_count` |
| `POST /api/demo/reset` *(demo)* | `{reseed?: bool}` | `{ok}` — wipes drops, doses, conversations, reports, notifications **and sessions** (everyone, including the operator, must sign in again) |

## Optional extras (kept from the handoff, off the main flow)

| Method & path | Notes |
|---|---|
| `POST /api/patients/{pid}/scans` *(caregiver)* | multipart `image` → Gemini label extraction → UNCONFIRMED scan (never a medication until confirmed) |
| `POST /api/patients/{pid}/scans/{scan_id}/confirm` *(caregiver)* / `…/reject` | human-reviewed fields → confirmed `Medication` |
| `GET /api/analytics/summary?patient_id=&days=` | local adherence analytics |
| `GET /api/analytics/snowflake` / `POST …/sync` | Snowflake outbox status / force sync (when configured) |
| `GET /api/health` | `{ok, version, demo_mode, db, hardware, agent, tts, smtp, time, ...}` (no auth; no personal data) |

## Pages

| Path | Page |
|---|---|
| `/` | Redirects to `/login`, `/patient` or `/care` depending on the session |
| `/login` | Sign in / register (patient, doctor or family) |
| `/patient` | Patient portal: status, drop buttons (cooldown), agent chat (voice + text), schedule (read-only), history, notifications, reports |
| `/care` | Doctor/family portal: linked patients, schedules/cooldown/containers editing, conversations, reports + email |
| `/kiosk` | Optional blind-friendly device screen (patient session) |
| `/demo` | Demo operator panel (demo mode) |
