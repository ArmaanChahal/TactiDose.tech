"""Domain models: check-in session state and saved records."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from .questions import QUESTIONS, Question, QuestionId


class SessionStatus(StrEnum):
    AWAITING_CONSENT = "awaiting_consent"
    AWAITING_ANSWER = "awaiting_answer"
    AWAITING_ANSWER_CONFIRMATION = "awaiting_answer_confirmation"
    AWAITING_NOTE_OFFER = "awaiting_note_offer"
    AWAITING_NOTE_TEXT = "awaiting_note_text"
    AWAITING_NOTE_CONFIRMATION = "awaiting_note_confirmation"
    AWAITING_FINISH = "awaiting_finish"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (SessionStatus.COMPLETED, SessionStatus.CANCELLED, SessionStatus.EXPIRED)


class StorageMode(StrEnum):
    UNDECIDED = "undecided"
    SAVE = "save"
    SESSION_ONLY = "session_only"


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    SKIPPED = "skipped"
    NOT_REACHED = "not_reached"


class Command(StrEnum):
    ANSWER = "answer"
    ADD_NOTE = "add_note"
    CONFIRM = "confirm"
    REJECT = "reject"
    REMOVE_NOTE = "remove_note"
    REPEAT = "repeat"
    SKIP = "skip"
    CANCEL = "cancel"
    FINISH = "finish"


# States in which a specific question is "current".
QUESTION_STATES = frozenset(
    {
        SessionStatus.AWAITING_ANSWER,
        SessionStatus.AWAITING_ANSWER_CONFIRMATION,
        SessionStatus.AWAITING_NOTE_OFFER,
        SessionStatus.AWAITING_NOTE_TEXT,
        SessionStatus.AWAITING_NOTE_CONFIRMATION,
    }
)


@dataclass
class AnswerSlot:
    question_id: QuestionId
    status: AnswerStatus = AnswerStatus.NOT_REACHED
    value: str | None = None
    recorded_at: datetime | None = None
    note_text: str | None = None
    note_recorded_at: datetime | None = None


@dataclass
class PendingInput:
    """User input that has NOT been confirmed and is never saved as-is."""

    kind: Literal["answer_candidate", "note_draft"]
    question_id: QuestionId
    candidate_value: str | None = None
    note_text: str | None = None


def _empty_answers() -> dict[QuestionId, AnswerSlot]:
    return {q.id: AnswerSlot(q.id) for q in QUESTIONS}


@dataclass
class CheckinSession:
    session_id: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    status: SessionStatus = SessionStatus.AWAITING_CONSENT
    storage_mode: StorageMode = StorageMode.UNDECIDED
    question_index: int = 0
    answers: dict[QuestionId, AnswerSlot] = field(default_factory=_empty_answers)
    pending: PendingInput | None = None
    step: int = 0
    unclear_attempts: int = 0
    support_requested: bool = False
    record_id: str | None = None
    ended_at: datetime | None = None
    content_purged: bool = False
    # Idempotency bookkeeping (owned by the application service).
    start_request_id: str | None = None
    replay: "OrderedDict[str, tuple[str, dict[str, Any]]]" = field(default_factory=OrderedDict)
    seen_request_ids: "OrderedDict[str, None]" = field(default_factory=OrderedDict)

    @property
    def current_question(self) -> Question | None:
        if self.status in QUESTION_STATES and self.question_index < len(QUESTIONS):
            return QUESTIONS[self.question_index]
        return None

    def purge_content(self) -> None:
        """Drop all answers, notes and pending input from memory."""
        self.answers = _empty_answers()
        self.pending = None
        self.replay.clear()
        self.content_purged = True


@dataclass
class SavedAnswer:
    question_id: QuestionId
    answer_value: str | None
    status: AnswerStatus
    recorded_at: datetime | None
    note_text: str | None = None
    note_recorded_at: datetime | None = None


@dataclass
class SharingPermission:
    """Separate from storage consent. Everything is off by default."""

    share_answers: bool = False
    share_notes: bool = False


@dataclass
class CheckinRecord:
    record_id: str
    user_id: str
    schema_version: str
    started_at: datetime
    completed_at: datetime
    support_requested: bool
    answers: list[SavedAnswer]
    sharing: SharingPermission = field(default_factory=SharingPermission)
