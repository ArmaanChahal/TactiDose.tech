"""Retry safety: repeated requests never duplicate answers, records or events."""

import pytest

from tactidose_wellbeing.contract import ActionRequest, StartSessionRequest
from tactidose_wellbeing.service import ServiceError


def _act(service, sid, rid, action, **kw):
    return service.handle_action("user-a", sid, ActionRequest(request_id=rid, action=action, **kw))


def test_start_is_idempotent(service):
    a = service.start_session("user-a", StartSessionRequest(request_id="s-1"))
    b = service.start_session("user-a", StartSessionRequest(request_id="s-1"))
    assert a.session_id == b.session_id
    assert b.idempotent_replay is True
    assert [e.event_id for e in a.events] == [e.event_id for e in b.events]
    # Same request_id from another user is a different start.
    c = service.start_session("user-b", StartSessionRequest(request_id="s-1"))
    assert c.session_id != a.session_id


def test_retried_answer_is_not_applied_twice(service):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")
    first = _act(service, sid, "r2", "answer", answer="low")
    retry = _act(service, sid, "r2", "answer", answer="low")
    assert retry.idempotent_replay is True and first.idempotent_replay is False
    assert retry.step == first.step
    assert retry.model_dump(exclude={"idempotent_replay"}) == first.model_dump(exclude={"idempotent_replay"})
    # The session did not advance a second time.
    assert service.get_session("user-a", sid).step == first.step


def test_retry_after_later_actions_does_not_rewind_or_reapply(service):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")
    _act(service, sid, "r2", "skip")                # skip mood
    _act(service, sid, "r3", "answer", answer="high")
    late = _act(service, sid, "r2", "skip")          # late duplicate of the skip
    assert late.idempotent_replay is True
    state = service.get_session("user-a", sid)
    assert state.next_question.kind == "note_offer"  # still on stress note offer
    assert [a.question_id.value for a in state.confirmed_answers] == ["mood", "stress"]


def test_request_id_reuse_with_different_payload_is_rejected(service):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")
    _act(service, sid, "r2", "answer", answer="low")
    with pytest.raises(ServiceError) as err:
        _act(service, sid, "r2", "answer", answer="good")
    assert err.value.code == "idempotency_conflict" and err.value.http_status == 409


def test_retried_finish_creates_one_record_and_same_events(service, repo):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")
    _act(service, sid, "r2", "answer", answer="good")
    first = _act(service, sid, "r3", "finish")
    again = _act(service, sid, "r3", "finish")
    assert len(repo.list_records("user-a")) == 1
    assert again.record_id == first.record_id
    assert [e.event_id for e in again.events] == [e.event_id for e in first.events]
    # A *new* finish request after completion is rejected, not re-applied.
    other = _act(service, sid, "r4", "finish")
    assert other.error.code == "session_ended" and other.events == []
    assert len(repo.list_records("user-a")) == 1


def test_late_retry_of_pre_completion_request_after_end_is_not_reapplied(service, repo):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")
    _act(service, sid, "r2", "answer", answer="good")
    _act(service, sid, "r3", "finish")
    late = _act(service, sid, "r2", "answer", answer="good")
    assert late.idempotent_replay is True
    assert late.session_status.value == "completed" and late.confirmed_answers == []
    assert len(repo.list_records("user-a")) == 1


def test_repository_save_is_idempotent(repo, service):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")
    _act(service, sid, "r2", "finish")
    [record] = repo.list_records("user-a")
    assert repo.save_record(record) is False
    assert len(repo.list_records("user-a")) == 1


def test_failed_persistence_does_not_advance_session(service, monkeypatch):
    sid = service.start_session("user-a", StartSessionRequest(request_id="s")).session_id
    _act(service, sid, "r1", "answer", answer="yes")

    def boom(record):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service.repository, "save_record", boom)
    with pytest.raises(RuntimeError):
        _act(service, sid, "r2", "finish")
    assert service.get_session("user-a", sid).session_status.value == "awaiting_answer"
    monkeypatch.undo()
    assert _act(service, sid, "r2", "finish").session_status.value == "completed"
