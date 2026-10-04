# tactidose-wellbeing

An optional, voice-friendly **well-being check-in module** for the TactiDose prototype,
a voice-first medication-access system for blind and low-vision users.

The check-in asks four questions, one at a time:

| Question | Options |
|---|---|
| Mood | good, okay, low |
| Stress | low, medium, high |
| Sleep | good, okay, poor |
| Would you like support from a person? | yes, no |

After a mood, stress or sleep answer, the user may add an **optional note** in their own words.
The note is read back and saved only after the user confirms it.

> **This is not a clinical tool.** Answers are informal self-reported observations. The module
> computes no scores, makes no diagnosis, infers no condition, and is not a therapist or
> counselling chatbot. It does not detect crises or monitor anyone.

The module is standalone. It runs without the main TactiDose repository, hardware, cloud
credentials, or an LLM. It accepts **text** and returns **structured data plus `speech_text`**.
The host app owns the microphone, speech recognition, text-to-speech and UI.

---

## Contents

1. [Setup and run](#1-setup-and-run)
2. [Architecture](#2-architecture)
3. [Conversation behaviour](#3-conversation-behaviour)
4. [Python usage](#4-python-usage)
5. [REST API](#5-rest-api)
6. [Agent integration](#6-agent-integration)
7. [Data model and privacy](#7-data-model-and-privacy)
8. [Retry safety (idempotency)](#8-retry-safety-idempotency)
9. [Support and urgent-support behaviour](#9-support-and-urgent-support-behaviour)
10. [Integration guide for the TactiDose team](#10-integration-guide-for-the-tactidose-team)
11. [Limitations and remaining production work](#11-limitations-and-remaining-production-work)

---

## 1. Setup and run

Requires **Python 3.11+**, declared in `pyproject.toml`. Tested with Python 3.14.

```bash
cd tactidose-wellbeing
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest                                    # run the test suite (137 tests)

tactidose-wellbeing demo                  # scripted synthetic scenarios (service layer)
tactidose-wellbeing demo --scenario retry # one scenario: completed|skipped|cancelled|session_only|support|retry
tactidose-wellbeing interactive           # type your own answers

python examples/python_usage.py           # direct Python import integration
python examples/mock_host.py              # mock TactiDose host using the REST API
python examples/mock_orchestrator.py      # mock multi-agent orchestrator using WellbeingAgent
python examples/mock_orchestrator.py --all-scenarios

tactidose-wellbeing serve --dev-auth      # REST API on http://127.0.0.1:8080 (local dev identity)
#   Swagger UI:  http://127.0.0.1:8080/docs
#   OpenAPI:     http://127.0.0.1:8080/openapi.json  (a copy is in docs/openapi.json)
python examples/mock_host.py --base-url http://127.0.0.1:8080

python examples/generate_samples.py       # regenerate docs/openapi.json and docs/samples/*.json
```

The demos cover:

1. A completed, saved check-in.
2. A skipped question.
3. A cancellation.
4. A session-only check-in that saves nothing.
5. A support request.
6. A repeated request handled without duplication.

They also show ambiguous answers, note confirmation and correction, medication access staying
independent, and history deletion.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TACTIDOSE_WELLBEING_DB_PATH` | `data/wellbeing.sqlite3` | SQLite file (`:memory:` = non-durable) |
| `TACTIDOSE_WELLBEING_AUTH_MODE` | `unconfigured` | `unconfigured` (refuse all user calls), `dev`, `static_tokens` |
| `TACTIDOSE_WELLBEING_API_TOKENS_FILE` | – | JSON `{"<token>": "<user_id>"}` for `static_tokens` |
| `TACTIDOSE_WELLBEING_CONFIG_FILE` | – | Safety/support JSON (see `config/wellbeing.example.json`) |
| `TACTIDOSE_WELLBEING_SESSION_TTL_SECONDS` | `1800` | Inactivity timeout; also how long ended sessions are kept for retries |
| `TACTIDOSE_WELLBEING_HOST` / `_PORT` | `127.0.0.1` / `8080` | Bind address (localhost by default) |

---

## 2. Architecture

```
            Python import      REST (FastAPI)        Agent adapter        CLI demo
                 │           api/app.py + auth.py   agent/adapter.py      cli.py
                 └──────────────────┬───────────────────┬──────────────────┘
                                    ▼
                      service.py  WellbeingService      ◄── the ONLY workflow entry point
                      ownership · idempotency · expiry · persistence · events
                                    │
                 ┌──────────────────┴─────────────────────┐
                 ▼                                        ▼
   domain/  (pure, no I/O)                      storage/  (injected interfaces)
   questions.py   question catalog, wording     base.py     CheckinRepository, SessionStore
   parsing.py     deterministic parsing         sqlite.py   SQLiteCheckinRepository
   state_machine  server-controlled states      memory.py   in-memory repo + session store
   safety.py      configurable urgent response
   session.py     session / record models
```

| Module | Responsibility |
|---|---|
| `domain/questions.py` | The four questions, their options and synonyms, and follow-up wording specific to each question and answer. |
| `domain/parsing.py` | Deterministic text parsing: exact answers, candidates that need confirmation, unclear input, yes/no, and control phrases. |
| `domain/state_machine.py` | All conversation transitions and speech text. Pure: mutates a session and returns an `Outcome`. |
| `domain/safety.py` | Phrase-list urgent-support response and support-handoff wording. Contacts come from configuration only. |
| `contract.py` | Versioned Pydantic request/response schemas (`schema_version: "1.0"`). |
| `service.py` | Application service: ownership checks, idempotency, guards, expiry, persistence, events, logging hygiene. |
| `storage/` | `CheckinRepository` (durable, consented records) and `SessionStore` (in-progress state, never durable). |
| `api/` | Thin FastAPI routes plus identity providers. No business logic. |
| `agent/` | `WellbeingAgent.handle(request, context)`, a framework-independent adapter. No business logic. |
| `bootstrap.py` | Composition root (`build_service`, `build_app`). |
| `demo.py`, `cli.py` | Shared synthetic scenarios and the command-line demo. |

**Dependency injection.** `WellbeingService(repository, sessions, safety=..., clock=..., id_factory=...)`.
To move to TiDB, implement `CheckinRepository` (seven methods, all scoped by `user_id`) and pass it
in. The workflow does not change. No cloud SDKs are included.

---

## 3. Conversation behaviour

### States

`awaiting_consent` → `awaiting_answer` ⇄ `awaiting_answer_confirmation` → `awaiting_note_offer`
→ (`awaiting_note_text`) → `awaiting_note_confirmation` → next question … → `awaiting_finish`
→ `completed`.

`cancel` leads to `cancelled` from any active state. Inactivity leads to `expired`.

| Session status | Asking the user | Useful actions |
|---|---|---|
| `awaiting_consent` | Save answers *and notes*, or session-only? | `answer` yes/no, `confirm`, `reject`, `skip` (= don't save) |
| `awaiting_answer` | The current question | `answer`, `skip` |
| `awaiting_answer_confirmation` | "Should I record your mood as good?" | `confirm`, `reject`, `answer` |
| `awaiting_note_offer` | e.g. "Would you like to share what is making you feel low?" | `answer` (yes / no / the note itself), `add_note`, `confirm`, `reject`, `skip` |
| `awaiting_note_text` | "Please tell me in your own words." | `answer` / `add_note`, `skip`, `remove_note` |
| `awaiting_note_confirmation` | Reads the note back | `confirm` (keep), `add_note` (correct), `answer` "change", `reject` / `remove_note` / "no" (leave out) |
| `awaiting_finish` | Factual summary of confirmed answers | `finish`, `confirm`, `cancel` |

`repeat`, `cancel` and `finish` work in every active state. `finish` before the end saves what
was confirmed and marks the remaining questions `not_reached`. An unconfirmed note or candidate
is dropped. Any other action returns `error.code = "invalid_action"` and leaves the state unchanged.

### Answer parsing (deterministic, no LLM)

* **Exact.** The reply reduces to exactly one option word after filler words are removed
  ("I feel pretty low today" → `low`). It is recorded directly.
* **Candidate.** The reply only *suggests* an option ("great", "sad", "slept well", "good I guess").
  The user is asked to confirm before anything is recorded.
* **Unclear.** The reply contains a negation, several options, or nothing recognisable ("not bad",
  "good and low", "dispense my pills"). The user is asked to clarify. These replies are **never**
  silently mapped to a category.
* **Control phrases.** `skip`, `repeat`, `cancel`/`stop`, and `finish`/`done` count only when
  they are the *entire* utterance.
* In note confirmation, an unclear reply asks again. It never silently replaces the note.

---

## 4. Python usage

```python
from tactidose_wellbeing import ActionRequest, StartSessionRequest, WellbeingService
from tactidose_wellbeing.storage import InMemorySessionStore, SQLiteCheckinRepository

service = WellbeingService(SQLiteCheckinRepository("data/wellbeing.sqlite3"), InMemorySessionStore())
user = "synthetic-user-1"                       # from YOUR authentication, never from speech

r = service.start_session(user, StartSessionRequest(request_id="r1"))
say(r.speech_text)                              # host TTS
r = service.handle_action(user, r.session_id,
        ActionRequest(request_id="r2", action="answer", answer=transcript,
                      question_id=r.next_question.question_id, expected_step=r.step))
service.get_history(user); service.delete_history(user)
service.delete_record(user, record_id); service.delete_note(user, record_id, "mood")
```

See `examples/python_usage.py`. `build_service()` wires everything from environment variables.

---

## 5. REST API

All user-scoped endpoints identify the user through the configured **identity provider**.
A `user_id` in a body is optional and is rejected (403) if it differs.

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | Health and auth status |
| POST | `/v1/sessions` | Start a session → `201 CheckinResponse` |
| POST | `/v1/sessions/{session_id}/actions` | Submit an action → `CheckinResponse` |
| GET | `/v1/sessions/{session_id}` | Read state (no change) |
| GET | `/v1/me/history` | Authenticated user's saved check-ins (with notes) |
| DELETE | `/v1/me/history` | Delete all of the user's saved check-ins and notes |
| DELETE | `/v1/me/history/{record_id}` | Delete one check-in |
| DELETE | `/v1/me/history/{record_id}/notes/{question_id}` | Delete one note, keep the rating |
| PUT | `/v1/me/history/{record_id}/sharing` | Set explicit sharing permission (default off) |
| GET | `/v1/me/history/{record_id}/shareable` | Only the parts the user allowed to share |

**Status codes:**

* `200` / `201`: success.
* `409`: a `CheckinResponse` with `error` set (`invalid_action`, `stale_step`,
  `question_mismatch`, `session_ended`), or an `ErrorResponse` for `idempotency_conflict`.
* `410`: `session_expired`.
* `401`: unauthenticated.
* `403`: `user_mismatch`.
* `404`: `session_not_found`, `record_not_found` or `note_not_found`. A session that belongs to
  another user also returns 404, so ids cannot be probed.
* `422`: `invalid_request`. Submitted values are never echoed back.
* `503`: `auth_not_configured`.

Example:

```bash
curl -s -X POST localhost:8080/v1/sessions -H 'X-Dev-User-Id: synthetic-user-1' \
     -H 'content-type: application/json' -d '{"schema_version":"1.0","request_id":"req-1"}'
curl -s -X POST localhost:8080/v1/sessions/$SID/actions -H 'X-Dev-User-Id: synthetic-user-1' \
     -H 'content-type: application/json' \
     -d '{"schema_version":"1.0","request_id":"req-2","action":"answer","answer":"yes"}'
```

Full request/response samples are in [`docs/samples/`](docs/samples/), including an unconfirmed
note, an ambiguous answer, a skip, a support request, an invalid action, a finish and its replayed
retry, an idempotency conflict, history and deletion. The OpenAPI document is in
[`docs/openapi.json`](docs/openapi.json). Field-by-field contract notes are in
[`docs/integration-contract.md`](docs/integration-contract.md).

### Authentication modes

* `unconfigured` (default). Every user-scoped request gets **503**. A deployment without
  configured auth fails safe.
* `dev`. **LOCAL DEVELOPMENT ONLY.** Trusts the `X-Dev-User-Id` header, and only from loopback
  clients. `tactidose-wellbeing serve --dev-auth` refuses to bind to a non-loopback address.
* `static_tokens`. Prototype bearer tokens mapped to user ids. Replace this with the host's
  real authentication (OIDC/JWT/session) by implementing `IdentityProvider.authenticate(request)`.

---

## 6. Agent integration

`WellbeingAgent` is a bounded component with a typed contract. It needs no LangChain, CrewAI,
AutoGen or LLM.

```python
from tactidose_wellbeing import build_service
from tactidose_wellbeing.agent import WellbeingAgent, AgentContext

agent = WellbeingAgent(build_service())
print(agent.capability.model_dump())       # name, version, allowed_actions, prohibited, events, JSON schemas

ctx = AgentContext(user_id=authenticated_user_id)   # supplied by the orchestrator's auth
out = agent.handle({"request_id": "o-1", "action": "start"}, ctx)
out = agent.handle({"request_id": "o-2", "action": "answer", "session_id": out.session_id,
                    "answer": "yes", "expected_step": out.step}, ctx)
tts(out.speech_text)                       # out.speech_audience == "authenticated_user_only"
```

* **Allowed actions:** `start`, `answer`, `add_note`, `confirm`, `reject`, `remove_note`,
  `repeat`, `skip`, `cancel`, `finish`, `get_state`, `get_history`, `delete_history`,
  `delete_record`, `delete_note`. Anything else returns `error.code = "action_not_allowed"`.
* **Outcomes (bounded):** `awaiting_user_input`, `checkin.completed`, `checkin.cancelled`,
  `checkin.expired`, `state.returned`, `history.returned`, `history.deleted`, `record.deleted`,
  `note.deleted`, `error`.
* **Events:** `checkin.started`, `checkin.completed`, `checkin.cancelled`, `support.requested`,
  `support.urgent_response_shown`. An event is a fact for the orchestrator to act on. It is
  **not** proof that anyone was notified.
* **Errors** are always structured (`ok: false`, `error: {code, message, retryable}`). The agent
  never raises for bad input.
* **Identity.** Only `context.user_id` is used. A `user_id` in the request must match it.
  Text such as "I am user X" is treated as an answer, never as identity.
* **Privacy.** Note text is redacted from structured output (`has_note: true`, `note_text: null`)
  unless `context.include_private_notes=True`. `speech_text` may read the user's own note back
  for confirmation, so deliver it to that user only and never put it into other agents' context.

Run `python examples/mock_orchestrator.py` to see it in action, including medication routing to a
separate stub agent, a spoofed user id being rejected, and a disallowed action.

---

## 7. Data model and privacy

**Saved record** (SQLite, only for consented and finished check-ins):

```
checkin_records:  record_id (PK) · user_id · schema_version · started_at · completed_at
                  · support_requested · share_answers (default 0) · share_notes (default 0)
checkin_answers:  record_id (FK, cascade) · user_id · question_id · answer_value (nullable)
                  · status (answered|skipped|not_reached) · recorded_at
                  · note_text (nullable) · note_recorded_at          PK (record_id, question_id)
```

Each note is stored alongside its answer as `question_id`, `answer_value`, `note_text` and
`recorded_at`.

**In the contract**:

* `pending_input` is unconfirmed input. It is never saved as-is and is always marked
  `confirmed: false`.
* `confirmed_answers` are answers the user confirmed in this session. They are saved only if
  storage consent was given **and** the session is finished.
* History `records` are what was actually saved.

**Privacy rules implemented:**

* **Consent.** Consent is asked first, and it explicitly covers ratings *and* optional
  explanations. If the user declines or skips, the check-in is session-only.
* **Session-only.** Answers and notes live only in the in-memory session store. They are purged
  when the session completes, is cancelled, or expires. Nothing is written to SQLite, and a test
  checks the database file bytes.
* **Cancel.** Nothing from the session is saved. **Expiry** discards unsaved content.
* **Ended sessions** keep no answer content in memory. Terminal responses and their replays
  contain no answers or notes.
* **Never stored:** raw audio, transcripts, unconfirmed input, and the conversation log. Only
  confirmed ratings and confirmed note wording are stored.
* **Sharing** is separate from storage consent and **off by default**, with a separate permission
  for notes. The module never sends anything to anyone. `/shareable` only returns what a host
  *could* share after explicit permission.
* **Ownership.** Every session, history, deletion and sharing operation is scoped to the
  authenticated user.
* **Logs** contain only the action, session id, a pseudonymous user hash, status and step. They
  never contain answers, notes, raw input or speech text (tested). Validation errors never echo
  input.
* **Notes are data.** They are stored verbatim and never parsed for instructions or medical
  conclusions. Only the configured urgent-phrase check reads them.
* **Synthetic data only** appears in examples, samples and tests.

---

## 8. Retry safety (idempotency)

Every request carries a client-generated `request_id`, and a retry **must reuse it**.

| Situation | Behaviour |
|---|---|
| Same `request_id` and payload on a session | The stored response is returned with `idempotent_replay: true`. Nothing is re-applied. |
| Same `request_id`, different payload | `409 idempotency_conflict`; nothing changes. |
| Late retry of an older request whose stored response was discarded when the session ended | Current state is returned with `idempotent_replay: true`. Never re-applied. |
| `POST /v1/sessions` retried with the same (user, `request_id`) | Same session is returned. |
| `finish` retried | One record, keyed by the session (`INSERT OR IGNORE`). |
| Events | `event_id` is deterministic per (session, request, type), so a replay returns the same ids. Deduplicate by `event_id`. |
| Persistence fails during `finish` | The session is not advanced, so retrying the same request is safe. |

Two optional guards stop answers from being attached to the wrong question:

* `expected_step`: the `step` from the last response. A mismatch returns `stale_step`.
* `question_id`: the question the input is meant for. A mismatch returns `question_mismatch`.

The service serialises mutations with a lock, so concurrent duplicates are also safe within one
process.

---

## 9. Support and urgent-support behaviour

* **"Yes" to human support.** The module acknowledges it, emits `support.requested`, sets
  `support_requested: true`, and returns
  `handoff: {type: "human_support", host_action_required: true, contacted_anyone: false}`.
  The speech says the module *cannot* contact anyone and that the TactiDose app can help.
  **No one is contacted automatically.** The host decides what to offer.
* **Urgent support (configurable).** If an input contains a phrase from the configured
  `urgent_phrases` list (explicit statements such as "I am in danger"), the response includes the
  configured `urgent_support_message`, any configured `crisis_resources`, and a disclaimer. It
  also emits `support.urgent_response_shown`. That input is not recorded, and the check-in is
  paused.
* **No contacts are built in.** `crisis_resources` is empty by default. The deploying team must
  add verified, region-appropriate contacts in the config file. Without them, the speech says
  none are configured and suggests local emergency services.
* ⚠️ **This prototype does not reliably detect crises and does not monitor users.** The phrase
  match is simple, misses most real situations, and is not a clinical risk classifier. No
  inference is made from voice tone, facial expression, medication adherence, or answer
  combinations.

---

## 10. Integration guide for the TactiDose team

This module assumes **nothing** about the TactiDose codebase. The contract below is the whole
interface.

1. **Choose a transport.** Use an in-process Python import (`WellbeingService`), REST
   (`/v1/...`), or the agent adapter (`WellbeingAgent`). All three behave identically, and
   `tests/test_equivalence.py` checks this.
2. **Identity.** Pass the user id from *your* authentication.
   * Python: the first argument.
   * Agent: `AgentContext.user_id`.
   * REST: implement an `IdentityProvider` for your auth, or use `static_tokens` for a prototype.
   * Never pass an id taken from speech.
3. **Speech loop.**
   1. Transcribe the user's utterance.
   2. Send `action: "answer"` with the text, plus `question_id` and `expected_step` from the last
      response.
   3. Speak `speech_text`.
   4. Use `next_question.options` for any visual or haptic UI.
4. **Keep medication flows independent.** Route medication intents to your own medication
   features *before* or *instead of* the check-in. The module never blocks, gates or interprets
   medication actions. A medication request sent to it by mistake gets a clarification prompt,
   never an action. Never require a completed check-in for medication access.
5. **Handle events.**
   * Deduplicate by `event_id`.
   * On `support.requested`, *offer* the user your handoff options (call a contact, show
     resources, and so on). Get the user's consent before contacting anyone.
   * On `support.urgent_response_shown`, your UI may surface the configured resources.
6. **Retries.** Reuse the same `request_id` when retrying a network call.
7. **History and deletion.** Wire "read my check-ins", "delete my check-ins" and "delete that
   note" voice commands to the history endpoints. Each response includes `speech_text`.
8. **Sharing.** If caregiver sharing is ever built, ask for explicit permission first.
   * Use `PUT .../sharing` to record the permission.
   * Read only `GET .../shareable`.
   * Notes need their own `share_notes` permission.
9. **Storage backend.** For TiDB, implement `CheckinRepository` and inject it. For a multi-process
   deployment, implement `SessionStore` on a shared non-durable store, such as Redis with TTL.
   Session-only data must never reach durable storage.
10. **Configuration.** Copy `config/wellbeing.example.json`, add verified crisis resources, and
    adjust the wording. Point `TACTIDOSE_WELLBEING_CONFIG_FILE` at it.

---

## 11. Limitations and remaining production work

**Prototype limitations**

* English only. Parsing is a small deterministic rule set, so some valid replies get a
  clarification prompt.
* The urgent-phrase check is a simple list match. It is **not** crisis detection.
* Sessions live in process memory: a restart discards in-progress check-ins, and multi-worker
  deployments need a shared `SessionStore`.
* There is one global lock in the service. That is fine for a prototype, not for high concurrency.
* Auth is development-grade (`dev` header, static tokens).
* SQLite data is not encrypted at rest.
* There is no rate limiting, no retention policy or automatic purge of old records, and no
  data export.
* Events are returned in responses only. There is no outbox or webhook delivery.
* Tested only with Python 3.14 in this environment. Python 3.11 is the declared minimum.

**Before production**

* Real authentication (OIDC/JWT) and TLS. Review CORS if a browser client is added.
* Encryption at rest, a retention policy, audit logging without content, and backup and
  deletion propagation.
* Shared session store and a TiDB `CheckinRepository`, with migrations.
* Accessibility and usability testing of prompts with blind and low-vision users, including
  speech-recognition error patterns.
* Clinical and safety review of all wording, the support handoff, and the urgent-support
  configuration, plus verified region-specific resources.
* Privacy, legal and regulatory review: consent records, data-subject rights, and health-data
  regulations where applicable.
* Localization.
