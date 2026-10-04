"""Pure domain layer: questions, parsing, session model and state machine (no I/O)."""

from .questions import QUESTIONS, QuestionId
from .safety import CrisisResource, SafetyConfig
from .session import (
    AnswerStatus,
    CheckinRecord,
    CheckinSession,
    Command,
    SavedAnswer,
    SessionStatus,
    SharingPermission,
    StorageMode,
)
from .state_machine import CheckinStateMachine

__all__ = [
    "QUESTIONS",
    "AnswerStatus",
    "CheckinRecord",
    "CheckinSession",
    "CheckinStateMachine",
    "Command",
    "CrisisResource",
    "QuestionId",
    "SafetyConfig",
    "SavedAnswer",
    "SessionStatus",
    "SharingPermission",
    "StorageMode",
]
