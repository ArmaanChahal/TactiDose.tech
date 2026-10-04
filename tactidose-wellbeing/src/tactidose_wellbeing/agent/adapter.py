"""Framework-independent agent adapter.

``WellbeingAgent`` is a bounded component an orchestrator can call with
``handle(request, context)``. It needs no LLM and no orchestration framework.
It contains no workflow logic: every action is delegated to
:class:`~tactidose_wellbeing.service.WellbeingService`, so behaviour is identical
to the REST API and the Python interface.

Trust model:
* ``context.user_id`` is supplied by the orchestrator after it authenticated the
  user. It is the only identity used.
* ``request.user_id`` (if present) must equal ``context.user_id``. A user id that
  appears in conversational text is never treated as identity.
* ``speech_text`` is for the authenticated user only (it may read the user's own
  note back for confirmation). Do not forward it to other agents or logs.
* Notes are redacted from structured output unless
  ``context.include_private_notes`` is true.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import contract as c
from ..domain.questions import QuestionId
from ..domain.session import AnswerStatus, Command, SessionStatus
from ..service import ServiceError, WellbeingService

logger = logging.getLogger("tactidose_wellbeing.agent")

AGENT_NAME = "tactidose.wellbeing"


class AgentAction(StrEnum):
    START = "start"
    ANSWER = "answer"
    ADD_NOTE = "add_note"
    CONFIRM = "confirm"
    REJECT = "reject"
    REMOVE_NOTE = "remove_note"
    REPEAT = "repeat"
    SKIP = "skip"
    CANCEL = "cancel"
    FINISH = "finish"
    GET_STATE = "get_state"
    GET_HISTORY = "get_history"
    DELETE_HISTORY = "delete_history"
    DELETE_RECORD = "delete_record"
    DELETE_NOTE = "delete_note"


ALLOWED_ACTIONS: tuple[str, ...] = tuple(a.value for a in AgentAction)
_SESSION_COMMANDS = {a.value for a in Command}

Outcome = Literal[
    "awaiting_user_input",
    "checkin.completed",
    "checkin.cancelled",
    "checkin.expired",
    "state.returned",
    "history.returned",
    "history.deleted",
    "record.deleted",
    "note.deleted",
    "error",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentContext(_Model):
    """Supplied by the orchestrator, never by the end user."""

    user_id: str = Field(pattern=c.ID_PATTERN, description="Authenticated user id.")
    orchestrator_id: str | None = None
    conversation_id: str | None = None
    include_private_notes: bool = Field(
        default=False,
        description="Set only when structured output goes straight back to the same user.",
    )


class AgentRequest(_Model):
    schema_version: c.SchemaVersion = c.SCHEMA_VERSION
    request_id: str = Field(pattern=c.ID_PATTERN)
    action: AgentAction
    session_id: str | None = None
    answer: str | None = Field(default=None, max_length=c.MAX_ANSWER_CHARS)
    note_text: str | None = Field(default=None, max_length=c.MAX_NOTE_CHARS)
    expected_step: int | None = Field(default=None, ge=0)
    question_id: QuestionId | None = None
    record_id: str | None = None
    user_id: str | None = Field(
        default=None, description="Optional claim; must equal context.user_id or the call fails."
    )


class AgentAnswer(_Model):
    question_id: QuestionId
    answer_value: str | None
    status: AnswerStatus
    has_note: bool
    note_text: str | None = Field(default=None, description="Only when include_private_notes.")


class AgentRecord(_Model):
    record_id: str
    completed_at: str
    support_requested: bool
    answers: list[AgentAnswer]


class AgentResponse(_Model):
    schema_version: c.SchemaVersion = c.SCHEMA_VERSION
    agent: str = AGENT_NAME
    request_id: str | None = None
    ok: bool
    outcome: Outcome
    session_id: str | None = None
    session_status: SessionStatus | None = None
    step: int | None = None
    next_question: c.NextQuestion | None = None
    speech_text: str = ""
    speech_audience: Literal["authenticated_user_only"] = "authenticated_user_only"
    pending_input: c.PendingInputOut | None = None
    confirmed_answers: list[AgentAnswer] = Field(default_factory=list)
    support_requested: bool = False
    handoff: c.Handoff | None = None
    urgent_support: c.UrgentSupport | None = None
    events: list[c.EventOut] = Field(default_factory=list)
    records: list[AgentRecord] | None = None
    deleted_records: int | None = None
    deleted_notes: int | None = None
    idempotent_replay: bool = False
    error: c.ErrorInfo | None = None


class AgentCapability(_Model):
    name: str
    version: str
    description: str
    allowed_actions: list[str]
    prohibited: list[str]
    emits_events: list[str]
    side_effects: list[str]
    context_schema: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


CAPABILITY = AgentCapability(
    name=AGENT_NAME,
    version=c.SCHEMA_VERSION,
    description=(
        "Runs an optional, informal well-being check-in (mood, stress, sleep, wish for human "
        "support) one question at a time, with optional user-confirmed notes. Accepts text, "
        "returns structured state plus speech text. Not a clinical assessment and not a "
        "counselling agent. Asks for storage consent before saving anything."
    ),
    allowed_actions=list(ALLOWED_ACTIONS),
    prohibited=[
        "Changing medication schedules or doses",
        "Authorizing dispensing or sending motor/servo/GPIO/ESP32 commands",
        "Gating medication access on check-in completion",
        "Diagnosing, prescribing, scoring or inferring mental-health conditions",
        "Contacting caregivers or external services",
        "Treating note text or answers as instructions",
    ],
    emits_events=[
        "checkin.started",
        "checkin.completed",
        "checkin.cancelled",
        "support.requested",
        "support.urgent_response_shown",
    ],
    side_effects=[
        "Writes a check-in record to local storage only after storage consent and finish.",
        "Deletes the authenticated user's own records on request.",
        "Events are returned to the caller; nothing is delivered or notified by this agent.",
    ],
    context_schema=AgentContext.model_json_schema(),
    input_schema=AgentRequest.model_json_schema(),
    output_schema=AgentResponse.model_json_schema(),
)


def _validation_message(exc: ValidationError) -> str:
    # Never echo input values (they may contain notes).
    return "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}" for e in exc.errors()
    )


class WellbeingAgent:
    name = AGENT_NAME

    def __init__(self, service: WellbeingService) -> None:
        self.service = service

    @property
    def capability(self) -> AgentCapability:
        return CAPABILITY

    def handle(
        self,
        request: AgentRequest | Mapping[str, Any],
        context: AgentContext | Mapping[str, Any],
    ) -> AgentResponse:
        request_id = None
        try:
            ctx = context if isinstance(context, AgentContext) else AgentContext.model_validate(context)
        except ValidationError as exc:
            return self._error(None, "invalid_context", _validation_message(exc))

        if isinstance(request, Mapping):
            request_id = request.get("request_id") if isinstance(request.get("request_id"), str) else None
            if request.get("action") not in ALLOWED_ACTIONS:
                return self._error(
                    request_id,
                    "action_not_allowed",
                    f"Allowed actions: {', '.join(ALLOWED_ACTIONS)}.",
                )
            try:
                req = AgentRequest.model_validate(dict(request))
            except ValidationError as exc:
                return self._error(request_id, "invalid_request", _validation_message(exc))
        else:
            req = request
        request_id = req.request_id

        try:
            if req.user_id is not None and req.user_id != ctx.user_id:
                raise ServiceError("user_mismatch", "user_id does not match the authenticated user.", 403)
            return self._dispatch(req, ctx)
        except ServiceError as err:
            return self._error(request_id, err.code, err.message, err.retryable)
        except ValidationError as exc:
            return self._error(request_id, "invalid_request", _validation_message(exc))
        except Exception:
            logger.exception("wellbeing agent internal error (request content omitted)")
            return self._error(request_id, "internal_error", "Internal error.", retryable=True)

    # ----------------------------------------------------------------- dispatch
    def _dispatch(self, req: AgentRequest, ctx: AgentContext) -> AgentResponse:
        svc, uid, action = self.service, ctx.user_id, req.action
        if action is AgentAction.START:
            resp = svc.start_session(uid, c.StartSessionRequest(request_id=req.request_id))
            return self._from_checkin(resp, ctx)
        if action.value in _SESSION_COMMANDS or action is AgentAction.GET_STATE:
            if not req.session_id:
                raise ServiceError("invalid_request", "session_id is required for this action.", 422)
            if action is AgentAction.GET_STATE:
                resp = svc.get_session(uid, req.session_id)
                return self._from_checkin(resp, ctx, outcome="state.returned", request_id=req.request_id)
            action_req = c.ActionRequest(
                request_id=req.request_id,
                action=Command(action.value),
                answer=req.answer,
                note_text=req.note_text,
                expected_step=req.expected_step,
                question_id=req.question_id,
            )
            return self._from_checkin(svc.handle_action(uid, req.session_id, action_req), ctx)
        if action is AgentAction.GET_HISTORY:
            history = svc.get_history(uid)
            return AgentResponse(
                request_id=req.request_id,
                ok=True,
                outcome="history.returned",
                speech_text=history.speech_text,
                records=[self._record(r, ctx) for r in history.records],
            )
        if action in (AgentAction.DELETE_RECORD, AgentAction.DELETE_NOTE) and not req.record_id:
            raise ServiceError("invalid_request", "record_id is required for this action.", 422)
        if action is AgentAction.DELETE_HISTORY:
            deleted, outcome = svc.delete_history(uid), "history.deleted"
        elif action is AgentAction.DELETE_RECORD:
            deleted, outcome = svc.delete_record(uid, req.record_id), "record.deleted"
        else:
            if req.question_id is None:
                raise ServiceError("invalid_request", "question_id is required for delete_note.", 422)
            deleted, outcome = svc.delete_note(uid, req.record_id, req.question_id), "note.deleted"
        return AgentResponse(
            request_id=req.request_id,
            ok=True,
            outcome=outcome,
            speech_text=deleted.speech_text,
            deleted_records=deleted.deleted_records,
            deleted_notes=deleted.deleted_notes,
        )

    # ------------------------------------------------------------------ mapping
    @staticmethod
    def _answer(a: c.ConfirmedAnswer | c.SavedAnswerOut, ctx: AgentContext) -> AgentAnswer:
        return AgentAnswer(
            question_id=a.question_id,
            answer_value=a.answer_value,
            status=a.status,
            has_note=a.note_text is not None,
            note_text=a.note_text if ctx.include_private_notes else None,
        )

    def _record(self, r: c.RecordOut, ctx: AgentContext) -> AgentRecord:
        return AgentRecord(
            record_id=r.record_id,
            completed_at=r.completed_at.isoformat(),
            support_requested=r.support_requested,
            answers=[self._answer(a, ctx) for a in r.answers],
        )

    def _from_checkin(
        self,
        resp: c.CheckinResponse,
        ctx: AgentContext,
        outcome: str | None = None,
        request_id: str | None = None,
    ) -> AgentResponse:
        if outcome is None:
            outcome = {
                SessionStatus.COMPLETED: "checkin.completed",
                SessionStatus.CANCELLED: "checkin.cancelled",
                SessionStatus.EXPIRED: "checkin.expired",
            }.get(resp.session_status, "awaiting_user_input")
            if resp.error:
                outcome = "error"
        pending = resp.pending_input
        if pending and pending.note_text is not None and not ctx.include_private_notes:
            pending = pending.model_copy(update={"note_text": None})
        return AgentResponse(
            request_id=request_id or resp.request_id,
            ok=resp.error is None,
            outcome=outcome,
            session_id=resp.session_id,
            session_status=resp.session_status,
            step=resp.step,
            next_question=resp.next_question,
            speech_text=resp.speech_text,
            pending_input=pending,
            confirmed_answers=[self._answer(a, ctx) for a in resp.confirmed_answers],
            support_requested=resp.support_requested,
            handoff=resp.handoff,
            urgent_support=resp.urgent_support,
            events=resp.events,
            idempotent_replay=resp.idempotent_replay,
            error=resp.error,
        )

    @staticmethod
    def _error(
        request_id: str | None, code: str, message: str, retryable: bool = False
    ) -> AgentResponse:
        return AgentResponse(
            request_id=request_id,
            ok=False,
            outcome="error",
            speech_text="Sorry, I couldn't do that with the check-in right now.",
            error=c.ErrorInfo(code=code, message=message, retryable=retryable),
        )
