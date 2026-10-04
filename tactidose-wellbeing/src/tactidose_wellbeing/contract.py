"""Versioned integration contract (schema_version "1.0").

These Pydantic models are the single source of truth for the Python interface,
the REST API (and its OpenAPI document) and the agent adapter.

Terminology used throughout the contract:

* ``pending_input``     - what the user said that is NOT yet confirmed. Never saved.
* ``confirmed_answers`` - answers/notes the user confirmed in this session. They
  are saved only if storage consent was given AND the session is finished.
* history ``records``   - check-ins that were actually saved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain.questions import QuestionId
from .domain.session import AnswerStatus, Command, SessionStatus, StorageMode

SCHEMA_VERSION = "1.0"

ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
MAX_ANSWER_CHARS = 500
MAX_NOTE_CHARS = 1000

SchemaVersion = Literal["1.0"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# ---------------------------------------------------------------- requests
class StartSessionRequest(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    request_id: str = Field(pattern=ID_PATTERN, description="Client-generated id; retries reuse it.")
    user_id: str | None = Field(
        default=None,
        pattern=ID_PATTERN,
        description="Optional. If present it must equal the authenticated user, otherwise the "
        "request is rejected. It is never used as proof of identity.",
    )


class ActionRequest(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    request_id: str = Field(pattern=ID_PATTERN, description="Client-generated id; retries reuse it.")
    user_id: str | None = Field(default=None, pattern=ID_PATTERN)
    action: Command
    answer: str | None = Field(
        default=None,
        max_length=MAX_ANSWER_CHARS,
        description="Free text (typed or transcribed). Required for action=answer.",
    )
    note_text: str | None = Field(
        default=None,
        max_length=MAX_NOTE_CHARS,
        description="Optional explanation in the user's own words. Required for action=add_note.",
    )
    expected_step: int | None = Field(
        default=None,
        ge=0,
        description="Optional optimistic-concurrency guard: the `step` from the last response.",
    )
    question_id: QuestionId | None = Field(
        default=None,
        description="Optional guard: the question this input is meant for.",
    )

    @model_validator(mode="after")
    def _check_payload(self) -> "ActionRequest":
        if self.action is Command.ANSWER:
            if not (self.answer and self.answer.strip()):
                raise ValueError("action 'answer' requires a non-empty 'answer'")
            if self.note_text is not None:
                raise ValueError("action 'answer' does not accept 'note_text'")
        elif self.action is Command.ADD_NOTE:
            if not (self.note_text and self.note_text.strip()):
                raise ValueError("action 'add_note' requires a non-empty 'note_text'")
            if self.answer is not None:
                raise ValueError("action 'add_note' does not accept 'answer'")
        elif self.answer is not None or self.note_text is not None:
            raise ValueError(f"action '{self.action.value}' does not accept text")
        return self


class SharingUpdate(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    share_answers: bool = False
    share_notes: bool = False


# --------------------------------------------------------------- responses
class ErrorInfo(_Model):
    code: str
    message: str
    retryable: bool = False


class NextQuestion(_Model):
    kind: Literal[
        "consent",
        "question",
        "confirm_answer",
        "note_offer",
        "note_text",
        "note_confirmation",
        "finish_review",
    ]
    question_id: QuestionId | None = None
    prompt: str
    options: list[str]
    position: int | None = Field(default=None, description="1-based question number.")
    total: int


class PendingInputOut(_Model):
    """Unconfirmed input. Shown for confirmation; never saved as-is."""

    confirmed: Literal[False] = False
    kind: Literal["answer_candidate", "note_draft"]
    question_id: QuestionId
    candidate_value: str | None = None
    note_text: str | None = None


class ConfirmedAnswer(_Model):
    question_id: QuestionId
    answer_value: str | None
    status: AnswerStatus
    recorded_at: datetime | None = None
    note_text: str | None = None
    note_recorded_at: datetime | None = None


class EventOut(_Model):
    """A fact the host may act on. NOT proof that anyone was notified."""

    event_id: str = Field(description="Deterministic; identical on retries. Deduplicate by this.")
    type: Literal[
        "checkin.started",
        "checkin.completed",
        "checkin.cancelled",
        "support.requested",
        "support.urgent_response_shown",
    ]
    session_id: str
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class CrisisResourceOut(_Model):
    name: str
    contact: str
    notes: str | None = None


class UrgentSupport(_Model):
    message: str
    resources: list[CrisisResourceOut]
    disclaimer: str = (
        "This prototype does not reliably detect crises and does not monitor users."
    )


class Handoff(_Model):
    type: Literal["human_support"] = "human_support"
    host_action_required: Literal[True] = True
    contacted_anyone: Literal[False] = False
    message: str


class CheckinResponse(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    request_id: str | None
    session_id: str
    user_id: str
    action: str | None = None
    session_status: SessionStatus
    step: int = Field(description="Increments on every state change.")
    storage_mode: StorageMode
    next_question: NextQuestion | None
    speech_text: str = Field(description="Text for the host's text-to-speech, for this user only.")
    pending_input: PendingInputOut | None = None
    confirmed_answers: list[ConfirmedAnswer] = Field(default_factory=list)
    summary: str | None = Field(default=None, description="Factual summary of confirmed answers.")
    support_requested: bool = False
    handoff: Handoff | None = None
    urgent_support: UrgentSupport | None = None
    record_id: str | None = Field(default=None, description="Set once a record was saved.")
    events: list[EventOut] = Field(default_factory=list)
    expires_at: datetime | None = None
    idempotent_replay: bool = False
    error: ErrorInfo | None = None


class SavedAnswerOut(_Model):
    question_id: QuestionId
    answer_value: str | None
    status: AnswerStatus
    recorded_at: datetime | None
    note_text: str | None = None
    note_recorded_at: datetime | None = None


class SharingOut(_Model):
    share_answers: bool
    share_notes: bool


class RecordOut(_Model):
    record_id: str
    started_at: datetime
    completed_at: datetime
    support_requested: bool
    answers: list[SavedAnswerOut]
    sharing: SharingOut


class HistoryResponse(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    user_id: str
    records: list[RecordOut]
    speech_text: str


class DeleteResponse(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    user_id: str
    deleted_records: int = 0
    deleted_notes: int = 0
    speech_text: str


class ShareableRecord(_Model):
    """Only what the user explicitly allowed to be shared. Empty by default."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    record_id: str
    completed_at: datetime
    answers: list[SavedAnswerOut]


class ErrorResponse(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    request_id: str | None = None
    error: ErrorInfo


class HealthResponse(_Model):
    schema_version: SchemaVersion = SCHEMA_VERSION
    status: Literal["ok"] = "ok"
    service: str = "tactidose-wellbeing"
    auth_configured: bool
    auth_mode: str
