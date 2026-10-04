"""Ownership isolation for sessions, history, notes, sharing and deletion."""

import pytest

from tactidose_wellbeing.contract import ActionRequest, SharingUpdate, StartSessionRequest
from tactidose_wellbeing.service import ServiceError

from .conftest import Driver


@pytest.fixture
def two_users(service):
    a, b = Driver(service, "user-a"), Driver(service, "user-b")
    a.start(); b.start()
    a.say("yes", "low", "A's private note", "yes")
    a.act("finish")
    return a, b


def test_other_user_cannot_act_on_or_read_session(service):
    a = Driver(service, "user-a")
    a.start()
    for call in (
        lambda: service.handle_action("user-b", a.session_id, ActionRequest(request_id="x", action="answer", answer="yes")),
        lambda: service.get_session("user-b", a.session_id),
    ):
        with pytest.raises(ServiceError) as err:
            call()
        assert err.value.code == "session_not_found" and err.value.http_status == 404
    # Session is untouched.
    assert service.get_session("user-a", a.session_id).step == 0


def test_claimed_user_id_must_match_authenticated_user(service):
    with pytest.raises(ServiceError) as err:
        service.start_session("user-a", StartSessionRequest(request_id="s", user_id="user-b"))
    assert err.value.code == "user_mismatch"
    a = Driver(service, "user-a")
    a.start()
    with pytest.raises(ServiceError):
        service.handle_action(
            "user-a", a.session_id,
            ActionRequest(request_id="x", action="answer", answer="yes", user_id="user-b"),
        )


def test_history_is_isolated(service, two_users):
    assert len(service.get_history("user-a").records) == 1
    assert service.get_history("user-b").records == []


def test_other_user_cannot_delete_or_share(service, two_users):
    rid = service.get_history("user-a").records[0].record_id
    for call, code in (
        (lambda: service.delete_record("user-b", rid), "record_not_found"),
        (lambda: service.delete_note("user-b", rid, "mood"), "note_not_found"),
        (lambda: service.set_sharing("user-b", rid, SharingUpdate(share_answers=True)), "record_not_found"),
        (lambda: service.get_shareable("user-b", rid), "record_not_found"),
    ):
        with pytest.raises(ServiceError) as err:
            call()
        assert err.value.code == code
    assert service.delete_history("user-b").deleted_records == 0
    rec = service.get_history("user-a").records[0]
    assert rec.answers[0].note_text == "A's private note"
    assert rec.sharing.share_answers is False
