"""Application service: the only entry point to the check-in workflow.

The Python API, REST routes, agent adapter and CLI all call this class. It owns
ownership checks, idempotency, expiry, persistence and event creation, and
delegates conversation logic to the pure state machine.

Idempotency (see README "Retry safety"):
* ``start_session``: the same (user, request_id) returns the same session.
* ``handle_action``: the same request_id on a session returns the stored
  response (``idempotent_replay=true``) without re-applying it. Reusing a
  request_id with a different payload is rejected (``idempotency_conflict``).
* Records are keyed by session, so a session can be saved at most once.
* Event ids are deterministic per (session, request, type) for deduplication.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import contract as c
from .domain.questions import QUESTIONS, QuestionId
from .domain.safety import SafetyConfig
from .domain.session import (
    AnswerStatus,
    CheckinRecord,
    CheckinSession,
    Command,
    SavedAnswer,
    SessionStatus,
    SharingPermission,
)
from .domain.state_machine import CheckinStateMachine, Outcome, current_prompt, summarize
from .storage.base import CheckinRepository, SessionStore

logger = logging.getLogger("tactidose_wellbeing")

_EVENT_NAMESPACE = uuid.UUID("6f1c3c2e-5d0b-4b8e-9a51-0d6c9b7f3a10")
REPLAY_LIMIT = 100
SEEN_LIMIT = 1000


class ServiceError(Exception):
    """A request-level failure (auth, ownership, not found, conflict)."""

    def __init__(self, code: str, message: str, http_status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable

    def to_info(self) -> c.ErrorInfo:
        return c.ErrorInfo(code=self.code, message=self.message, retryable=self.retryable)


# HTTP statuses for errors returned *inside* a CheckinResponse (session state errors).
STATE_ERROR_HTTP_STATUS = {
    "invalid_action": 409,
    "stale_step": 409,
    "question_mismatch": 409,
    "session_ended": 409,
    "session_expired": 410,
}

_TERMINAL_SPEECH = {
    SessionStatus.COMPLETED: "This check-in is finished.",
    SessionStatus.CANCELLED: "This check-in was cancelled.",
    SessionStatus.EXPIRED: (
        "This check-in has expired, and anything that was not saved was discarded. "
        "You can start a new check-in at any time."
    ),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pseudonym(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:10]


def _fingerprint(req: c.ActionRequest) -> str:
    payload = req.model_dump(mode="json", exclude={"request_id", "schema_version"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class WellbeingService:
    def __init__(
        self,
        repository: CheckinRepository,
        sessions: SessionStore,
        *,
        safety: SafetyConfig | None = None,
        session_ttl: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.repository = repository
        self.sessions = sessions
        self.safety = safety or SafetyConfig()
        self.machine = CheckinStateMachine(self.safety)
        self.session_ttl = session_ttl
        self._clock = clock
        self._new_id = id_factory
        self._lock = threading.RLock()

    # ============================================================ sessions
    def start_session(self, user_id: str, request: c.StartSessionRequest) -> c.CheckinResponse:
        with self._lock:
            now = self._clock()
            self.purge_expired(now)
            self._check_claimed_user(user_id, request.user_id)

            existing = self.sessions.find_by_start_request(user_id, request.request_id)
            if existing is not None:
                cached = existing.replay.get(request.request_id)
                if cached:
                    return self._replayed(cached[1])
                return self._view(existing, request.request_id, replay=True)

            s = CheckinSession(
                session_id=f"ses_{self._new_id()}",
                user_id=user_id,
                created_at=now,
                expires_at=now + self.session_ttl,
                start_request_id=request.request_id,
            )
            outcome = Outcome(
                speech=current_prompt(s).prompt, events=[("checkin.started", {})]
            )
            resp = self._build(s, request.request_id, "start", outcome, now)
            self._remember(s, request.request_id, "start", resp)
            self.sessions.put(s)
            self._log("start", s)
            return resp

    def handle_action(
        self, user_id: str, session_id: str, request: c.ActionRequest
    ) -> c.CheckinResponse:
        with self._lock:
            now = self._clock()
            self.purge_expired(now)
            self._check_claimed_user(user_id, request.user_id)
            s = self._load_owned(user_id, session_id)

            fp = _fingerprint(request)
            cached = s.replay.get(request.request_id)
            if cached:
                if cached[0] != fp:
                    raise ServiceError(
                        "idempotency_conflict",
                        "This request_id was already used with a different payload.",
                        409,
                    )
                return self._replayed(cached[1])
            if request.request_id in s.seen_request_ids:
                # Already applied earlier; its stored response was discarded with
                # the session content. Never re-apply.
                return self._view(s, request.request_id, replay=True)

            guard = self._guard(s, request)
            if guard:
                code, message = guard
                prompt = current_prompt(s)
                outcome = Outcome(
                    speech=f"{message} {prompt.prompt if prompt else ''}".strip(), error=guard
                )
                if code == "session_expired":
                    outcome.speech = _TERMINAL_SPEECH[SessionStatus.EXPIRED]
            else:
                work = copy.deepcopy(s)
                text = request.answer if request.action is Command.ANSWER else request.note_text
                outcome = self.machine.apply(work, request.action, text, now)
                if outcome.changed:
                    work.step += 1
                if outcome.commit:
                    record = self._build_record(work, now)
                    self.repository.save_record(record)
                    work.record_id = record.record_id
                if work.status.is_terminal:
                    work.expires_at = now + self.session_ttl  # retention for retries only
                    work.purge_content()
                else:
                    work.expires_at = now + self.session_ttl  # sliding inactivity timeout
                s = work

            resp = self._build(s, request.request_id, request.action.value, outcome, now)
            self._remember(s, request.request_id, fp, resp)
            self.sessions.put(s)
            self._log(request.action.value, s, resp.error.code if resp.error else None)
            return resp

    def get_session(self, user_id: str, session_id: str) -> c.CheckinResponse:
        with self._lock:
            self.purge_expired(self._clock())
            return self._view(self._load_owned(user_id, session_id), None)

    def purge_expired(self, now: datetime | None = None) -> None:
        """Expire idle sessions (discarding unsaved content) and drop old terminal ones.

        Called on every service call; hosts may also call it periodically.
        """
        now = now or self._clock()
        with self._lock:
            for s in self.sessions.list_expired(now):
                if s.status.is_terminal:
                    self.sessions.delete(s.session_id)
                else:
                    s.status = SessionStatus.EXPIRED
                    s.ended_at = now
                    s.purge_content()
                    s.expires_at = now + self.session_ttl
                    self.sessions.put(s)
                    self._log("expired", s)

    # ============================================================= history
    def get_history(self, user_id: str) -> c.HistoryResponse:
        records = self.repository.list_records(user_id)
        return c.HistoryResponse(
            user_id=user_id, records=[_record_out(r) for r in records], speech_text=_history_speech(records)
        )

    def delete_history(self, user_id: str) -> c.DeleteResponse:
        deleted = self.repository.delete_all(user_id)
        self._log_user("delete_history", user_id)
        speech = (
            f"I deleted {deleted} saved check-in{'' if deleted == 1 else 's'}, including any notes."
            if deleted
            else "You have no saved check-ins to delete."
        )
        return c.DeleteResponse(user_id=user_id, deleted_records=deleted, speech_text=speech)

    def delete_record(self, user_id: str, record_id: str) -> c.DeleteResponse:
        if not self.repository.delete_record(user_id, record_id):
            raise ServiceError("record_not_found", "No saved check-in with that id.", 404)
        self._log_user("delete_record", user_id)
        return c.DeleteResponse(
            user_id=user_id, deleted_records=1, speech_text="I deleted that check-in and its notes."
        )

    def delete_note(self, user_id: str, record_id: str, question_id: QuestionId) -> c.DeleteResponse:
        if not self.repository.delete_note(user_id, record_id, QuestionId(question_id)):
            raise ServiceError("note_not_found", "No saved note for that check-in and question.", 404)
        self._log_user("delete_note", user_id)
        return c.DeleteResponse(
            user_id=user_id, deleted_notes=1, speech_text="I deleted that note. The answer itself is kept."
        )

    def set_sharing(self, user_id: str, record_id: str, update: c.SharingUpdate) -> c.RecordOut:
        sharing = SharingPermission(update.share_answers, update.share_notes)
        if not self.repository.set_sharing(user_id, record_id, sharing):
            raise ServiceError("record_not_found", "No saved check-in with that id.", 404)
        return _record_out(self.repository.get_record(user_id, record_id))

    def get_shareable(self, user_id: str, record_id: str) -> c.ShareableRecord:
        """What the owner explicitly allowed to be shared. This module never sends it anywhere."""
        record = self.repository.get_record(user_id, record_id)
        if record is None:
            raise ServiceError("record_not_found", "No saved check-in with that id.", 404)
        answers: list[c.SavedAnswerOut] = []
        if record.sharing.share_answers:
            for a in record.answers:
                out = _saved_answer_out(a)
                if not record.sharing.share_notes:
                    out = out.model_copy(update={"note_text": None, "note_recorded_at": None})
                answers.append(out)
        return c.ShareableRecord(
            record_id=record.record_id, completed_at=record.completed_at, answers=answers
        )

    # ============================================================= helpers
    def _check_claimed_user(self, user_id: str, claimed: str | None) -> None:
        if claimed is not None and claimed != user_id:
            raise ServiceError(
                "user_mismatch", "user_id does not match the authenticated user.", 403
            )

    def _load_owned(self, user_id: str, session_id: str) -> CheckinSession:
        s = self.sessions.get(session_id)
        if s is None or s.user_id != user_id:
            # Same error for "missing" and "not yours", so ids cannot be probed.
            raise ServiceError("session_not_found", "No such check-in session.", 404)
        return s

    def _guard(self, s: CheckinSession, req: c.ActionRequest) -> tuple[str, str] | None:
        if s.status is SessionStatus.EXPIRED:
            return ("session_expired", "This check-in has expired.")
        if s.status.is_terminal:
            return ("session_ended", "This check-in has already ended.")
        if req.expected_step is not None and req.expected_step != s.step:
            return (
                "stale_step",
                f"This input was meant for step {req.expected_step}, but the check-in is at step {s.step}.",
            )
        if req.question_id is not None:
            current = s.current_question
            if current is None or current.id != req.question_id:
                return ("question_mismatch", "That input was meant for a different question.")
        return None

    def _build_record(self, s: CheckinSession, now: datetime) -> CheckinRecord:
        return CheckinRecord(
            record_id=f"rec_{s.session_id.removeprefix('ses_')}",
            user_id=s.user_id,
            schema_version=c.SCHEMA_VERSION,
            started_at=s.created_at,
            completed_at=now,
            support_requested=s.support_requested,
            answers=[
                SavedAnswer(
                    question_id=q.id,
                    answer_value=s.answers[q.id].value,
                    status=s.answers[q.id].status,
                    recorded_at=s.answers[q.id].recorded_at,
                    note_text=s.answers[q.id].note_text,
                    note_recorded_at=s.answers[q.id].note_recorded_at,
                )
                for q in QUESTIONS
            ],
        )

    def _events(
        self, s: CheckinSession, request_id: str | None, outcome: Outcome, now: datetime
    ) -> list[c.EventOut]:
        return [
            c.EventOut(
                event_id="evt_"
                + uuid.uuid5(_EVENT_NAMESPACE, f"{s.session_id}|{request_id}|{etype}").hex,
                type=etype,
                session_id=s.session_id,
                occurred_at=now,
                data=data,
            )
            for etype, data in outcome.events
        ]

    def _build(
        self,
        s: CheckinSession,
        request_id: str | None,
        action: str | None,
        outcome: Outcome,
        now: datetime,
    ) -> c.CheckinResponse:
        view = None if s.status.is_terminal else current_prompt(s)
        pending = None
        if s.pending is not None:
            pending = c.PendingInputOut(
                kind=s.pending.kind,
                question_id=s.pending.question_id,
                candidate_value=s.pending.candidate_value,
                note_text=s.pending.note_text,
            )
        confirmed = [
            c.ConfirmedAnswer(
                question_id=slot.question_id,
                answer_value=slot.value,
                status=slot.status,
                recorded_at=slot.recorded_at,
                note_text=slot.note_text,
                note_recorded_at=slot.note_recorded_at,
            )
            for slot in s.answers.values()
            if slot.status is not AnswerStatus.NOT_REACHED
        ]
        handoff = None
        if outcome.handoff:
            handoff = c.Handoff(message=self.safety.support_handoff_message)
        urgent = None
        if outcome.urgent:
            urgent = c.UrgentSupport(
                message=self.safety.urgent_message,
                resources=[
                    c.CrisisResourceOut(name=r.name, contact=r.contact, notes=r.notes)
                    for r in self.safety.crisis_resources
                ],
            )
        error = None
        if outcome.error:
            error = c.ErrorInfo(code=outcome.error[0], message=outcome.error[1])
        return c.CheckinResponse(
            request_id=request_id,
            session_id=s.session_id,
            user_id=s.user_id,
            action=action,
            session_status=s.status,
            step=s.step,
            storage_mode=s.storage_mode,
            next_question=(
                c.NextQuestion(
                    kind=view.kind,
                    question_id=view.question_id,
                    prompt=view.prompt,
                    options=view.options,
                    position=view.position,
                    total=view.total,
                )
                if view
                else None
            ),
            speech_text=outcome.speech,
            pending_input=pending,
            confirmed_answers=confirmed,
            summary=summarize(s) if s.status is SessionStatus.AWAITING_FINISH else None,
            support_requested=s.support_requested,
            handoff=handoff,
            urgent_support=urgent,
            record_id=s.record_id,
            events=self._events(s, request_id, outcome, now),
            expires_at=s.expires_at,
            error=error,
        )

    def _view(
        self, s: CheckinSession, request_id: str | None, replay: bool = False
    ) -> c.CheckinResponse:
        prompt = current_prompt(s)
        speech = prompt.prompt if prompt else _TERMINAL_SPEECH[s.status]
        resp = self._build(s, request_id, None, Outcome(speech=speech), self._clock())
        return resp.model_copy(update={"idempotent_replay": replay})

    @staticmethod
    def _replayed(payload: dict) -> c.CheckinResponse:
        resp = c.CheckinResponse.model_validate(payload)
        return resp.model_copy(update={"idempotent_replay": True})

    @staticmethod
    def _remember(s: CheckinSession, request_id: str, fingerprint: str, resp: c.CheckinResponse) -> None:
        s.replay[request_id] = (fingerprint, resp.model_dump(mode="json"))
        while len(s.replay) > REPLAY_LIMIT:
            s.replay.popitem(last=False)
        s.seen_request_ids[request_id] = None
        while len(s.seen_request_ids) > SEEN_LIMIT:
            s.seen_request_ids.popitem(last=False)

    @staticmethod
    def _log(action: str, s: CheckinSession, error: str | None = None) -> None:
        # Never log answers, notes, raw input or speech text.
        logger.info(
            "checkin action=%s session=%s user=%s status=%s step=%d error=%s",
            action,
            s.session_id,
            _pseudonym(s.user_id),
            s.status.value,
            s.step,
            error or "-",
        )

    @staticmethod
    def _log_user(action: str, user_id: str) -> None:
        logger.info("history action=%s user=%s", action, _pseudonym(user_id))


def _history_speech(records: list[CheckinRecord]) -> str:
    if not records:
        return "You have no saved check-ins."
    latest = records[-1]
    parts = []
    for a in latest.answers:
        label = next(q.label for q in QUESTIONS if q.id == a.question_id)
        if a.status is AnswerStatus.ANSWERED:
            parts.append(f"{label}: {a.answer_value}" + (", with a note." if a.note_text else "."))
        elif a.status is AnswerStatus.SKIPPED:
            parts.append(f"{label}: skipped.")
        else:
            parts.append(f"{label}: not answered.")
    count = len(records)
    return (
        f"You have {count} saved check-in{'' if count == 1 else 's'}. "
        f"The most recent one, from {latest.completed_at:%B} {latest.completed_at.day}: "
        + " ".join(parts)
    )


def _saved_answer_out(a: SavedAnswer) -> c.SavedAnswerOut:
    return c.SavedAnswerOut(
        question_id=a.question_id,
        answer_value=a.answer_value,
        status=a.status,
        recorded_at=a.recorded_at,
        note_text=a.note_text,
        note_recorded_at=a.note_recorded_at,
    )


def _record_out(r: CheckinRecord) -> c.RecordOut:
    return c.RecordOut(
        record_id=r.record_id,
        started_at=r.started_at,
        completed_at=r.completed_at,
        support_requested=r.support_requested,
        answers=[_saved_answer_out(a) for a in r.answers],
        sharing=c.SharingOut(
            share_answers=r.sharing.share_answers, share_notes=r.sharing.share_notes
        ),
    )
