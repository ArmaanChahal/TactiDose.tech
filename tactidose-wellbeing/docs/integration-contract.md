# Integration contract: schema_version 1.0

The Pydantic models in `src/tactidose_wellbeing/contract.py` (REST and Python) and
`src/tactidose_wellbeing/agent/adapter.py` (agent) define this contract. `docs/openapi.json` is
generated from the same models. This file explains what the fields mean.

Versioning rule: additive, backwards-compatible changes keep `"1.0"`. Removing or renaming a
field, or changing what it means, requires a new `schema_version`. Requests with an unknown
`schema_version` or extra fields are rejected (`extra="forbid"`).

## ActionRequest (`POST /v1/sessions/{session_id}/actions`)

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `"1.0"` | |
| `request_id` | string `[A-Za-z0-9._:-]{1,128}` | Client-generated. **Reuse it on retry.** |
| `user_id` | string, optional | Must equal the authenticated user, otherwise 403. Never used as identity. |
| `action` | `answer` · `add_note` · `confirm` · `reject` · `remove_note` · `repeat` · `skip` · `cancel` · `finish` | |
| `answer` | string ≤ 500 | Required for `answer`, forbidden otherwise. Free text from typing or transcription. |
| `note_text` | string ≤ 1000 | Required for `add_note`, forbidden otherwise. |
| `expected_step` | int, optional | The `step` from the last response. A mismatch returns `stale_step`. |
| `question_id` | `mood`·`stress`·`sleep`·`support`, optional | A mismatch returns `question_mismatch`. |

## CheckinResponse

| Field | Meaning |
|---|---|
| `request_id`, `session_id`, `user_id`, `action` | Echo and identification. |
| `session_status` | See the state table in the README. |
| `step` | Increments on every state change. Clarifications, repeats and errors do not change it. |
| `storage_mode` | `undecided` · `save` · `session_only`. |
| `next_question` | `{kind, question_id, prompt, options, position, total}`. `kind` is one of `consent`, `question`, `confirm_answer`, `note_offer`, `note_text`, `note_confirmation`, `finish_review`. `null` when the session has ended. |
| `speech_text` | What the host should say. For the authenticated user only. |
| `pending_input` | **Unconfirmed** answer candidate or note draft, with `confirmed: false`. Never saved as-is. |
| `confirmed_answers` | Confirmed in this session. Saved only with consent plus `finish`. Empty once the session has ended. |
| `summary` | Factual summary of confirmed answers. Present in `awaiting_finish`. |
| `support_requested` | The user answered "yes" to human support. |
| `handoff` | `{type: "human_support", host_action_required: true, contacted_anyone: false, message}`. |
| `urgent_support` | `{message, resources[], disclaimer}`. Present only when an urgent phrase matched. |
| `record_id` | Set when a record was saved. |
| `events` | `[{event_id, type, session_id, occurred_at, data}]`. `data` never contains answers or notes. |
| `expires_at` | Inactivity expiry. For an ended session, how long it is kept for retries. |
| `idempotent_replay` | `true` when this is a stored response to a retried `request_id`. |
| `error` | `{code, message, retryable}` for state errors (see below), otherwise `null`. |

## Error codes

| Code | Where | HTTP |
|---|---|---|
| `invalid_action` | CheckinResponse.error | 409 |
| `stale_step`, `question_mismatch`, `session_ended` | CheckinResponse.error | 409 |
| `session_expired` | CheckinResponse.error | 410 |
| `idempotency_conflict` | ErrorResponse | 409 |
| `session_not_found`, `record_not_found`, `note_not_found` | ErrorResponse | 404 |
| `user_mismatch` | ErrorResponse | 403 |
| `unauthenticated` | ErrorResponse | 401 |
| `auth_not_configured` | ErrorResponse | 503 |
| `invalid_request` | ErrorResponse | 422 |
| `action_not_allowed`, `invalid_context`, `internal_error` | Agent only (`AgentResponse.error`) | – |

## Events

| Type | When | `data` |
|---|---|---|
| `checkin.started` | Session created | `{}` |
| `checkin.completed` | `finish` | `{saved, answered_count, skipped_count, support_requested}` |
| `checkin.cancelled` | `cancel` | `{saved: false}` |
| `support.requested` | User confirmed "yes" to human support | `{handoff: "host_controlled"}` |
| `support.urgent_response_shown` | A configured urgent phrase matched | `{handoff: "host_controlled"}` |

Events report what happened in the check-in. They do not mean that a notification was sent or
received. `event_id` is stable across retries, so deduplicate by it.

## Agent adapter

* `AgentContext`: `{user_id, orchestrator_id?, conversation_id?, include_private_notes=false}`.
* `AgentRequest`: `{schema_version, request_id, action, session_id?, answer?, note_text?,
  expected_step?, question_id?, record_id?, user_id?}`.
* `AgentResponse`: `{ok, outcome, session_id, session_status, step, next_question, speech_text,
  speech_audience="authenticated_user_only", pending_input, confirmed_answers[{question_id,
  answer_value, status, has_note, note_text}], support_requested, handoff, urgent_support, events,
  records, deleted_records, deleted_notes, idempotent_replay, error}`.
* JSON Schemas are available at runtime from `WellbeingAgent(...).capability.input_schema`,
  `.output_schema` and `.context_schema`.
