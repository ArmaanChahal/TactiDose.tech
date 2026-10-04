"""Storage interfaces.

The application service depends only on these protocols, so the SQLite
implementation can be swapped (e.g. for TiDB) without touching the workflow.
Every read/write of saved records is scoped by ``user_id``; implementations must
never return or modify another user's data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..domain.questions import QuestionId
from ..domain.session import CheckinRecord, CheckinSession, SharingPermission


class CheckinRepository(Protocol):
    """Durable store for check-ins the user consented to save."""

    def save_record(self, record: CheckinRecord) -> bool:
        """Insert a record. Must be idempotent on ``record_id``: returns False if it already exists."""

    def list_records(self, user_id: str) -> list[CheckinRecord]: ...

    def get_record(self, user_id: str, record_id: str) -> CheckinRecord | None: ...

    def delete_all(self, user_id: str) -> int:
        """Delete every record (and note) of this user. Returns the number of records deleted."""

    def delete_record(self, user_id: str, record_id: str) -> bool: ...

    def delete_note(self, user_id: str, record_id: str, question_id: QuestionId) -> bool:
        """Remove one note, keeping the rating. False if there was no such note."""

    def set_sharing(self, user_id: str, record_id: str, sharing: SharingPermission) -> bool: ...


class SessionStore(Protocol):
    """Holds in-progress session state. Must never write to durable storage,
    because session-only answers may not be persisted."""

    def get(self, session_id: str) -> CheckinSession | None: ...

    def put(self, session: CheckinSession) -> None: ...

    def find_by_start_request(self, user_id: str, request_id: str) -> CheckinSession | None: ...

    def list_expired(self, now: datetime) -> list[CheckinSession]:
        """Sessions whose ``expires_at`` is at or before ``now``."""

    def delete(self, session_id: str) -> None: ...
