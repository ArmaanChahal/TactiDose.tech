"""In-memory implementations (tests, demos, and the default session store)."""

from __future__ import annotations

import copy
import threading
from datetime import datetime

from ..domain.questions import QuestionId
from ..domain.session import CheckinRecord, CheckinSession, SharingPermission


class InMemorySessionStore:
    """Process-local session store. Sessions vanish on restart by design.

    A multi-process deployment needs a shared store implementing the same
    ``SessionStore`` protocol; it must keep session-only data out of durable storage.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, CheckinSession] = {}
        self._starts: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> CheckinSession | None:
        with self._lock:
            s = self._sessions.get(session_id)
            return copy.deepcopy(s) if s else None

    def put(self, session: CheckinSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = copy.deepcopy(session)
            if session.start_request_id:
                self._starts[(session.user_id, session.start_request_id)] = session.session_id

    def find_by_start_request(self, user_id: str, request_id: str) -> CheckinSession | None:
        with self._lock:
            sid = self._starts.get((user_id, request_id))
            s = self._sessions.get(sid) if sid else None
            return copy.deepcopy(s) if s else None

    def list_expired(self, now: datetime) -> list[CheckinSession]:
        with self._lock:
            return [copy.deepcopy(s) for s in self._sessions.values() if s.expires_at <= now]

    def delete(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
            if s is not None:
                self._starts.pop((s.user_id, s.start_request_id), None)

    def __len__(self) -> int:
        return len(self._sessions)


class InMemoryCheckinRepository:
    """Non-durable repository, useful for tests and the CLI demo."""

    def __init__(self) -> None:
        self._records: dict[str, CheckinRecord] = {}
        self._lock = threading.Lock()

    def save_record(self, record: CheckinRecord) -> bool:
        with self._lock:
            if record.record_id in self._records:
                return False
            self._records[record.record_id] = copy.deepcopy(record)
            return True

    def list_records(self, user_id: str) -> list[CheckinRecord]:
        with self._lock:
            mine = [copy.deepcopy(r) for r in self._records.values() if r.user_id == user_id]
        return sorted(mine, key=lambda r: r.completed_at)

    def get_record(self, user_id: str, record_id: str) -> CheckinRecord | None:
        with self._lock:
            r = self._records.get(record_id)
            return copy.deepcopy(r) if r and r.user_id == user_id else None

    def delete_all(self, user_id: str) -> int:
        with self._lock:
            ids = [rid for rid, r in self._records.items() if r.user_id == user_id]
            for rid in ids:
                del self._records[rid]
            return len(ids)

    def delete_record(self, user_id: str, record_id: str) -> bool:
        with self._lock:
            r = self._records.get(record_id)
            if not r or r.user_id != user_id:
                return False
            del self._records[record_id]
            return True

    def delete_note(self, user_id: str, record_id: str, question_id: QuestionId) -> bool:
        with self._lock:
            r = self._records.get(record_id)
            if not r or r.user_id != user_id:
                return False
            for a in r.answers:
                if a.question_id == question_id and a.note_text is not None:
                    a.note_text = None
                    a.note_recorded_at = None
                    return True
            return False

    def set_sharing(self, user_id: str, record_id: str, sharing: SharingPermission) -> bool:
        with self._lock:
            r = self._records.get(record_id)
            if not r or r.user_id != user_id:
                return False
            r.sharing = copy.deepcopy(sharing)
            return True
