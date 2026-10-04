"""Both repository implementations satisfy the same contract."""

from datetime import datetime, timezone

import pytest

from tactidose_wellbeing.domain.questions import QuestionId
from tactidose_wellbeing.domain.session import AnswerStatus, CheckinRecord, SavedAnswer, SharingPermission
from tactidose_wellbeing.storage import InMemoryCheckinRepository, SQLiteCheckinRepository

T = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def _record(rid: str, user: str, note: str | None = "synthetic note") -> CheckinRecord:
    return CheckinRecord(
        record_id=rid, user_id=user, schema_version="1.0", started_at=T, completed_at=T,
        support_requested=False,
        answers=[
            SavedAnswer(QuestionId.MOOD, "low", AnswerStatus.ANSWERED, T, note, T if note else None),
            SavedAnswer(QuestionId.STRESS, None, AnswerStatus.SKIPPED, T),
            SavedAnswer(QuestionId.SLEEP, "good", AnswerStatus.ANSWERED, T),
            SavedAnswer(QuestionId.SUPPORT, None, AnswerStatus.NOT_REACHED, None),
        ],
    )


@pytest.fixture(params=["sqlite", "memory"])
def repository(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteCheckinRepository(tmp_path / "t.sqlite3")
    return InMemoryCheckinRepository()


def test_roundtrip_and_idempotent_insert(repository):
    assert repository.save_record(_record("r1", "u1")) is True
    assert repository.save_record(_record("r1", "u1")) is False
    [loaded] = repository.list_records("u1")
    assert loaded == _record("r1", "u1")


def test_scoping_and_deletes(repository):
    repository.save_record(_record("r1", "u1"))
    repository.save_record(_record("r2", "u1", note=None))
    repository.save_record(_record("r3", "u2"))
    assert repository.get_record("u2", "r1") is None
    assert repository.delete_record("u2", "r1") is False
    assert repository.delete_note("u1", "r1", QuestionId.MOOD) is True
    assert repository.delete_note("u1", "r1", QuestionId.MOOD) is False
    assert repository.get_record("u1", "r1").answers[0].note_text is None
    assert repository.set_sharing("u1", "r1", SharingPermission(True, False)) is True
    assert repository.get_record("u1", "r1").sharing == SharingPermission(True, False)
    assert repository.delete_all("u1") == 2
    assert repository.list_records("u1") == []
    assert len(repository.list_records("u2")) == 1


def test_sqlite_persists_across_instances(tmp_path):
    path = tmp_path / "p.sqlite3"
    SQLiteCheckinRepository(path).save_record(_record("r1", "u1"))
    assert SQLiteCheckinRepository(path).get_record("u1", "r1").answers[0].note_text == "synthetic note"
