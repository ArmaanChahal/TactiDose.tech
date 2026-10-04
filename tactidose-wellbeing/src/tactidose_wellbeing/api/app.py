"""FastAPI application. Routes are thin: authenticate, call the service, map errors."""

from __future__ import annotations

from typing import Annotated, Any, Callable, TypeVar

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import contract as c
from ..domain.questions import QuestionId
from ..service import STATE_ERROR_HTTP_STATUS, ServiceError, WellbeingService
from .auth import IdentityProvider

T = TypeVar("T")

API_DESCRIPTION = """
Optional, informal well-being check-in for the TactiDose prototype.

* Not a clinical assessment. No scores, diagnoses or inferences are produced.
* Never affects medication schedules, dispensing or hardware.
* The host owns microphones, speech recognition, text-to-speech and UI. Send text, play `speech_text`.
* The authenticated user comes from the configured identity provider, never from the request body.
"""

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": c.ErrorResponse, "description": "Not authenticated"},
    403: {"model": c.ErrorResponse, "description": "user_id does not match the authenticated user"},
    404: {"model": c.ErrorResponse, "description": "Not found (or not owned by this user)"},
    422: {"model": c.ErrorResponse, "description": "Invalid request"},
    503: {"model": c.ErrorResponse, "description": "Authentication not configured"},
}

_START_EXAMPLES = {
    "start": {
        "summary": "Start a check-in",
        "value": {"schema_version": "1.0", "request_id": "req-0001"},
    }
}
_ACTION_EXAMPLES = {
    "consent_yes": {
        "summary": "Consent to saving",
        "value": {"schema_version": "1.0", "request_id": "req-0002", "action": "answer", "answer": "yes"},
    },
    "answer": {
        "summary": "Answer the current question (guarded)",
        "value": {
            "schema_version": "1.0",
            "request_id": "req-0003",
            "action": "answer",
            "answer": "I feel pretty low",
            "question_id": "mood",
            "expected_step": 1,
        },
    },
    "add_note": {
        "summary": "Add an optional note",
        "value": {
            "schema_version": "1.0",
            "request_id": "req-0004",
            "action": "add_note",
            "note_text": "A busy week at work.",
        },
    },
    "confirm": {"summary": "Confirm", "value": {"schema_version": "1.0", "request_id": "req-0005", "action": "confirm"}},
    "skip": {"summary": "Skip", "value": {"schema_version": "1.0", "request_id": "req-0006", "action": "skip"}},
    "cancel": {"summary": "Cancel", "value": {"schema_version": "1.0", "request_id": "req-0007", "action": "cancel"}},
}


def _error_response(err: ServiceError, request_id: str | None = None) -> JSONResponse:
    body = c.ErrorResponse(request_id=request_id, error=err.to_info())
    return JSONResponse(status_code=err.http_status, content=body.model_dump(mode="json"))


class _ServiceErrorWithRequest(Exception):
    def __init__(self, err: ServiceError, request_id: str | None) -> None:
        self.err, self.request_id = err, request_id


def _call(request_id: str | None, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except ServiceError as err:
        raise _ServiceErrorWithRequest(err, request_id) from err


def _checkin_json(resp: c.CheckinResponse, ok_status: int = 200) -> JSONResponse:
    status = STATE_ERROR_HTTP_STATUS.get(resp.error.code, 409) if resp.error else ok_status
    return JSONResponse(status_code=status, content=resp.model_dump(mode="json"))


def current_user(request: Request) -> str:
    """Authenticated user id from the app's identity provider (never from the body)."""
    return request.app.state.identity.authenticate(request)


User = Annotated[str, Depends(current_user)]


def create_app(service: WellbeingService, identity: IdentityProvider) -> FastAPI:
    app = FastAPI(
        title="TactiDose Well-being Check-in API",
        version=c.SCHEMA_VERSION,
        description=API_DESCRIPTION,
    )
    app.state.service = service
    app.state.identity = identity

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, err: ServiceError) -> JSONResponse:
        return _error_response(err)

    @app.exception_handler(_ServiceErrorWithRequest)
    async def _service_error_req(request: Request, exc: _ServiceErrorWithRequest) -> JSONResponse:
        return _error_response(exc.err, exc.request_id)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo submitted values: they may contain answers or notes.
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
            for e in exc.errors()
        )
        err = ServiceError("invalid_request", details or "Invalid request.", 422)
        return _error_response(err)

    @app.get("/v1/health", response_model=c.HealthResponse, tags=["system"])
    def health() -> c.HealthResponse:
        return c.HealthResponse(auth_configured=identity.configured, auth_mode=identity.mode)

    @app.post(
        "/v1/sessions",
        response_model=c.CheckinResponse,
        status_code=201,
        responses=_ERRORS,
        tags=["sessions"],
        summary="Start a check-in session",
    )
    def start_session(
        user_id: User,
        body: Annotated[c.StartSessionRequest, Body(openapi_examples=_START_EXAMPLES)],
    ) -> JSONResponse:
        resp = _call(body.request_id, lambda: service.start_session(user_id, body))
        return _checkin_json(resp, 201)

    @app.post(
        "/v1/sessions/{session_id}/actions",
        response_model=c.CheckinResponse,
        responses={
            **_ERRORS,
            409: {"model": c.CheckinResponse, "description": "Action not valid in the current state, "
                  "stale step, question mismatch, ended session, or idempotency conflict"},
            410: {"model": c.CheckinResponse, "description": "Session expired"},
        },
        tags=["sessions"],
        summary="Submit an action (answer, add_note, confirm, reject, remove_note, repeat, skip, cancel, finish)",
    )
    def submit_action(
        session_id: str,
        user_id: User,
        body: Annotated[c.ActionRequest, Body(openapi_examples=_ACTION_EXAMPLES)],
    ) -> JSONResponse:
        resp = _call(body.request_id, lambda: service.handle_action(user_id, session_id, body))
        return _checkin_json(resp)

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=c.CheckinResponse,
        responses=_ERRORS,
        tags=["sessions"],
        summary="Read the current session state (no state change)",
    )
    def get_session(session_id: str, user_id: User) -> c.CheckinResponse:
        return service.get_session(user_id, session_id)

    @app.get("/v1/me/history", response_model=c.HistoryResponse, responses=_ERRORS, tags=["history"])
    def get_history(user_id: User) -> c.HistoryResponse:
        return service.get_history(user_id)

    @app.delete("/v1/me/history", response_model=c.DeleteResponse, responses=_ERRORS, tags=["history"])
    def delete_history(user_id: User) -> c.DeleteResponse:
        return service.delete_history(user_id)

    @app.delete(
        "/v1/me/history/{record_id}", response_model=c.DeleteResponse, responses=_ERRORS, tags=["history"]
    )
    def delete_record(record_id: str, user_id: User) -> c.DeleteResponse:
        return service.delete_record(user_id, record_id)

    @app.delete(
        "/v1/me/history/{record_id}/notes/{question_id}",
        response_model=c.DeleteResponse,
        responses=_ERRORS,
        tags=["history"],
        summary="Delete one saved note, keeping its rating",
    )
    def delete_note(record_id: str, question_id: QuestionId, user_id: User) -> c.DeleteResponse:
        return service.delete_note(user_id, record_id, question_id)

    @app.put(
        "/v1/me/history/{record_id}/sharing",
        response_model=c.RecordOut,
        responses=_ERRORS,
        tags=["history"],
        summary="Set explicit sharing permission (off by default, separate from storage consent)",
    )
    def set_sharing(record_id: str, body: c.SharingUpdate, user_id: User) -> c.RecordOut:
        return service.set_sharing(user_id, record_id, body)

    @app.get(
        "/v1/me/history/{record_id}/shareable",
        response_model=c.ShareableRecord,
        responses=_ERRORS,
        tags=["history"],
        summary="Only the parts the user allowed to be shared. This service never sends them anywhere.",
    )
    def get_shareable(record_id: str, user_id: User) -> c.ShareableRecord:
        return service.get_shareable(user_id, record_id)

    return app
