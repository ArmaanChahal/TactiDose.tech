# CLAUDE.md: tactidose-wellbeing

This file explains how this project was built and how to work in it.

## How it was built

The `BlindAI/` folder was empty. This project was built from scratch in `BlindAI/tactidose-wellbeing/`
from two specifications:

1. An independent, non-clinical **well-being check-in module** for TactiDose, a voice-first
   medication-access prototype for blind and low-vision users. The module asks about mood,
   stress, sleep, and whether the user wants support from a person.
2. An extension for **optional notes**: the user can explain a mood, stress or sleep answer in
   their own words. The note is read back for confirmation, and the user can correct or remove
   it. Notes follow the same storage consent as answers.

The main TactiDose app is built separately. That covers medication schedules, offline speech
recognition, TTS, the ESP32 carousel, Gemini, ElevenLabs, TiDB and Snowflake. None of it was
touched or assumed. This module defines its own contract and ships mock hosts.

### What was delivered

* A pure domain layer in `src/tactidose_wellbeing/domain/`:
  * question catalog
  * deterministic parser
  * server-controlled state machine
  * configurable urgent-support phrases
* One application service, `service.py`. It handles ownership, idempotency, guards, expiry,
  persistence and events.
* Storage interfaces in `storage/base.py`, with SQLite and in-memory implementations.
* A FastAPI REST API (`api/`) with three identity modes: `unconfigured`, which is the safe
  default, `dev`, and `static_tokens`.
* `WellbeingAgent`, a framework-independent adapter (`agent/adapter.py`), with a capability
  description, an allowlist of actions, and structured errors.
* CLI commands (`cli.py`): `demo`, `interactive`, `serve`, `openapi`. Shared synthetic
  scenarios are in `demo.py`.
* Examples: `python_usage.py`, `mock_host.py` (REST), `mock_orchestrator.py` (agent), and
  `generate_samples.py`.
* Docs:
  * `README.md` (the main documentation)
  * `docs/integration-contract.md`
  * `docs/openapi.json`
  * `docs/samples/*.json` (18 request/response pairs, all generated)
* 137 pytest tests.

### Verification at hand-off (2026-10-03)

* `pytest`: 137 passed on Python 3.14.7. Python 3.11 is the declared minimum but was not tested
  here.
* Mutation checks: disabling replay, always persisting, and removing the ownership check each
  made tests fail.
* All examples and every CLI subcommand ran successfully.
* A real `uvicorn` server was exercised with curl. Dev auth worked, and the default
  unconfigured auth returned 503.
* A session-only run left 0 rows in SQLite.

## Key design decisions and assumptions

* **Consent comes first.** One yes/no question covers ratings *and* notes. "No" or "skip" means
  session-only.
* **Persistence happens only on `finish` with consent.** Cancel, expiry and session-only
  sessions never write anything. In-progress sessions live only in `InMemorySessionStore`.
* **Ended sessions are purged.** Answers, notes and cached replays are removed from memory, so
  terminal responses and their replays contain no answer content.
* **Parsing has three outcomes.** Exact answers are recorded. Suggestive ones ("great", "sad")
  need confirmation. Unclear ones (negations, several options) trigger clarification.
  Control words count only when they are the whole utterance.
* **Note handling.** An unclear reply while confirming a note asks again; it never replaces the
  note. Note text is stored verbatim and is never interpreted.
* **Support "yes".** It emits a `support.requested` event and returns a `handoff` with
  `contacted_anyone: false`. Nothing is contacted.
* **Urgent support.** It is a configurable phrase list. The response comes from config, and the
  input is not recorded. No phone numbers are built in, and `crisis_resources` is empty by
  default. It is explicitly not crisis detection.
* **Idempotency.**
  * Each session caches responses by `request_id`, storing a payload fingerprint.
  * A request id seen before the session's cache was cleared is never re-applied.
  * `record_id` is derived from the session, and records are inserted with `INSERT OR IGNORE`.
  * Event ids are deterministic (uuid5).
* **Guards.** The optional `expected_step` and `question_id` fields stop input from being
  attached to the wrong question.
* **Errors inside the response.** State errors (`invalid_action`, `stale_step`, etc.) come back
  as a `CheckinResponse` with `error` set and HTTP 409/410. Request-level errors (auth,
  ownership, not found, conflict) are `ServiceError` → `ErrorResponse`.
* **Agent privacy.** The agent redacts note text from structured output unless
  `include_private_notes=True`. `speech_text` is for the authenticated user only.
* **Sharing.** It is separate from storage consent and off by default (`share_answers`,
  `share_notes`). The module never sends anything.

## Working rules for future changes

* Put **business logic only in `domain/` or `service.py`**. API routes, the agent adapter and the
  CLI must stay thin and call `WellbeingService`.
  `tests/test_equivalence.py` checks that Python, REST and the agent behave the same.
* Never add medication, dispensing or hardware behaviour. Never gate medication on a check-in.
  Never add scoring, diagnosis or inference.
* Never log answers, notes, raw input or speech text. `test_logs_do_not_contain_...` enforces
  this.
* Use synthetic data only.
* **To change the contract,** edit `contract.py` and/or `agent/adapter.py`. Then run
  `python examples/generate_samples.py` and update `docs/integration-contract.md`. Breaking
  changes need a new `schema_version`.
* **To add a storage backend** (e.g. TiDB), implement `CheckinRepository`, keep every query
  scoped by `user_id`, and run `tests/test_storage.py` against it.

## Commands

```bash
cd tactidose-wellbeing && source .venv/bin/activate   # venv already created, package installed -e
pytest
tactidose-wellbeing demo
tactidose-wellbeing serve --dev-auth                  # 127.0.0.1:8080, /docs for Swagger
python examples/mock_host.py && python examples/mock_orchestrator.py
```

## Known limitations

The full list is in README §11. In short:

* English only.
* Sessions live in process memory, behind a single lock.
* Auth is development-grade.
* SQLite is not encrypted, and there is no retention policy.
* Urgent phrase matching is not reliable crisis detection.
* Before production, the wording needs clinical and accessibility review.
