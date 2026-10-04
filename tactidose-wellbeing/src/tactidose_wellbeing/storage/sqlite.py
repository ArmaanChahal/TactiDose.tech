"""SQLite implementation of :class:`CheckinRepository`.

Only consented, finished check-ins are written here. Raw audio, transcripts,
unconfirmed input and session-only answers never reach this module.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..domain.questions import QUESTIONS, QuestionId
from ..domain.session import AnswerStatus, CheckinRecord, SavedAnswer, SharingPermission

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin_records (
    record_id         TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    schema_version    TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    completed_at      TEXT NOT NULL,
    support_requested INTEGER NOT NULL,
    share_answers     INTEGER NOT NULL DEFAULT 0,
    share_notes       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_user ON checkin_records(user_id, completed_at);

CREATE TABLE IF NOT EXISTS checkin_answers (
    record_id        TEXT NOT NULL REFERENCES checkin_records(record_id) ON DELETE CASCADE,
    user_id          TEXT NOT NULL,
    question_id      TEXT NOT NULL,
    answer_value     TEXT,
    status           TEXT NOT NULL,
    recorded_at      TEXT,
    note_text        TEXT,
    note_recorded_at TEXT,
    PRIMARY KEY (record_id, question_id)
);
CREATE INDEX IF NOT EXISTS idx_answers_user ON checkin_answers(user_id);
"""

_ORDER = {q.id.value: i for i, q in enumerate(QUESTIONS)}


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteCheckinRepository:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------------- writes
    def save_record(self, record: CheckinRecord) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO checkin_records (record_id, user_id, schema_version, "
                    "started_at, completed_at, support_requested, share_answers, share_notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.record_id,
                        record.user_id,
                        record.schema_version,
                        _ts(record.started_at),
                        _ts(record.completed_at),
                        int(record.support_requested),
                        int(record.sharing.share_answers),
                        int(record.sharing.share_notes),
                    ),
                )
                created = cur.rowcount == 1
                if created:
                    cur.executemany(
                        "INSERT INTO checkin_answers (record_id, user_id, question_id, answer_value, "
                        "status, recorded_at, note_text, note_recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                record.record_id,
                                record.user_id,
                                a.question_id.value,
                                a.answer_value,
                                a.status.value,
                                _ts(a.recorded_at),
                                a.note_text,
                                _ts(a.note_recorded_at),
                            )
                            for a in record.answers
                        ],
                    )
                cur.execute("COMMIT")
                return created
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def delete_all(self, user_id: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM checkin_records WHERE user_id = ?", (user_id,))
            # Defensive: also remove any orphaned answer rows for this user.
            self._conn.execute("DELETE FROM checkin_answers WHERE user_id = ?", (user_id,))
            return cur.rowcount

    def delete_record(self, user_id: str, record_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM checkin_records WHERE user_id = ? AND record_id = ?", (user_id, record_id)
            )
            return cur.rowcount == 1

    def delete_note(self, user_id: str, record_id: str, question_id: QuestionId) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE checkin_answers SET note_text = NULL, note_recorded_at = NULL "
                "WHERE user_id = ? AND record_id = ? AND question_id = ? AND note_text IS NOT NULL",
                (user_id, record_id, QuestionId(question_id).value),
            )
            return cur.rowcount == 1

    def set_sharing(self, user_id: str, record_id: str, sharing: SharingPermission) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE checkin_records SET share_answers = ?, share_notes = ? "
                "WHERE user_id = ? AND record_id = ?",
                (int(sharing.share_answers), int(sharing.share_notes), user_id, record_id),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ reads
    def list_records(self, user_id: str) -> list[CheckinRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM checkin_records WHERE user_id = ? ORDER BY completed_at, record_id",
                (user_id,),
            ).fetchall()
            return [self._load(row) for row in rows]

    def get_record(self, user_id: str, record_id: str) -> CheckinRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkin_records WHERE user_id = ? AND record_id = ?",
                (user_id, record_id),
            ).fetchone()
            return self._load(row) if row else None

    def _load(self, row: sqlite3.Row) -> CheckinRecord:
        answer_rows = self._conn.execute(
            "SELECT * FROM checkin_answers WHERE record_id = ? AND user_id = ?",
            (row["record_id"], row["user_id"]),
        ).fetchall()
        answers = sorted(
            (
                SavedAnswer(
                    question_id=QuestionId(a["question_id"]),
                    answer_value=a["answer_value"],
                    status=AnswerStatus(a["status"]),
                    recorded_at=_dt(a["recorded_at"]),
                    note_text=a["note_text"],
                    note_recorded_at=_dt(a["note_recorded_at"]),
                )
                for a in answer_rows
            ),
            key=lambda a: _ORDER[a.question_id.value],
        )
        return CheckinRecord(
            record_id=row["record_id"],
            user_id=row["user_id"],
            schema_version=row["schema_version"],
            started_at=_dt(row["started_at"]),
            completed_at=_dt(row["completed_at"]),
            support_requested=bool(row["support_requested"]),
            answers=answers,
            sharing=SharingPermission(bool(row["share_answers"]), bool(row["share_notes"])),
        )
