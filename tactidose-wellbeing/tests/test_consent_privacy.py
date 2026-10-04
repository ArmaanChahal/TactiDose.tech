"""Consent enforcement, session-only behaviour, sharing defaults and log hygiene."""

import logging
import sqlite3

import pytest

from tactidose_wellbeing.contract import SharingUpdate
from tactidose_wellbeing.domain.session import SessionStatus as S, StorageMode
from tactidose_wellbeing.storage import SQLiteCheckinRepository

from .conftest import Driver, make_service


def _complete_session_only(driver):
    driver.say("no")                       # decline storage
    driver.say("low", "Rough day at the office", "yes")
    driver.say("high", "no", "poor", "no", "yes")
    return driver.act("finish")


def test_declining_consent_means_session_only(driver, repo):
    r = driver.say("no")
    assert r.storage_mode is StorageMode.SESSION_ONLY
    assert "Nothing from this check-in will be saved" in r.speech_text


def test_skipping_consent_defaults_to_not_saving(driver):
    assert driver.act("skip").storage_mode is StorageMode.SESSION_ONLY


def test_unclear_consent_is_asked_again(driver):
    r = driver.say("maybe")
    assert r.session_status is S.AWAITING_CONSENT and r.storage_mode is StorageMode.UNDECIDED


def test_session_only_answers_and_notes_are_never_persisted(tmp_path, clock):
    db = tmp_path / "w.sqlite3"
    service = make_service(SQLiteCheckinRepository(db), clock)
    d = Driver(service)
    d.start()
    _complete_session_only(d)
    assert d.last.session_status is S.COMPLETED
    assert d.last.record_id is None
    assert [e.data["saved"] for e in d.last.events] == [False]
    # Session-only data are kept in memory during the session (shown for confirmation) ...
    # ... and are purged when the session ends:
    state = service.get_session("user-a", d.session_id)
    assert state.confirmed_answers == [] and state.pending_input is None
    stored = service.sessions.get(d.session_id)
    assert stored.content_purged and stored.replay and all(
        not payload["confirmed_answers"] for _, payload in stored.replay.values()
    )
    # Nothing at all in the database file.
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM checkin_records").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checkin_answers").fetchone()[0] == 0
    raw = db.read_bytes()
    assert b"Rough day" not in raw


def test_session_only_notes_visible_during_session(driver):
    driver.say("no", "low", "Rough day")
    r = driver.say("yes")
    assert r.confirmed_answers[0].note_text == "Rough day"  # in memory, for this session only


def test_consented_save_includes_notes(driver, repo):
    driver.say("yes", "low", "Rough day", "yes")
    driver.act("finish")
    [record] = repo.list_records("user-a")
    assert record.answers[0].note_text == "Rough day"


def test_terminal_responses_and_replays_contain_no_answer_content(driver):
    driver.say("yes", "low", "Private words", "yes")
    r = driver.act("finish")
    assert r.confirmed_answers == [] and r.pending_input is None
    assert "Private words" not in r.model_dump_json()
    assert "low" not in r.speech_text


def test_sharing_is_off_by_default_and_separate_from_storage(service, driver):
    driver.say("yes", "low", "Private words", "yes")
    driver.act("finish")
    rec = service.get_history("user-a").records[0]
    assert rec.sharing.share_answers is False and rec.sharing.share_notes is False
    assert service.get_shareable("user-a", rec.record_id).answers == []

    service.set_sharing("user-a", rec.record_id, SharingUpdate(share_answers=True))
    shared = service.get_shareable("user-a", rec.record_id)
    assert shared.answers[0].answer_value == "low"
    assert shared.answers[0].note_text is None  # notes need their own permission

    service.set_sharing("user-a", rec.record_id, SharingUpdate(share_answers=True, share_notes=True))
    assert service.get_shareable("user-a", rec.record_id).answers[0].note_text == "Private words"


def test_logs_do_not_contain_answers_notes_or_user_ids(driver, caplog):
    caplog.set_level(logging.DEBUG)
    driver.say("yes", "low", "Secret synthetic note", "yes", "high")
    driver.act("finish")
    text = caplog.text
    assert "checkin action=" in text  # logging does happen
    for sensitive in ("Secret synthetic note", "user-a", "answer=low", "high"):
        assert sensitive not in text


@pytest.mark.parametrize("deleted_via", ["all", "record"])
def test_deletion_removes_answers_and_notes_from_database(tmp_path, clock, deleted_via):
    db = tmp_path / "w.sqlite3"
    service = make_service(SQLiteCheckinRepository(db), clock)
    d = Driver(service)
    d.start()
    d.say("yes", "low", "Deletable synthetic note", "yes")
    rid = d.act("finish").record_id
    if deleted_via == "all":
        assert service.delete_history("user-a").deleted_records == 1
    else:
        assert service.delete_record("user-a", rid).deleted_records == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM checkin_answers").fetchone()[0] == 0
    assert service.get_history("user-a").records == []
