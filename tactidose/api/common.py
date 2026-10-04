"""Shared plumbing for the v2 routers: error mapping, readable 422s, small view helpers.

Error mapping (docs/API.md: every error is ``{"detail": "<human readable>"}``):

=========================================================  ======
``medication.errors.ValidationError``                      422
``medication.errors.NotFoundError``                        404
``medication.errors.ConflictError``                        409
``auth.errors.AuthError`` / ``TooManyAttempts``            401 / 429 (+ ``Retry-After``)
``auth.errors.PermissionDenied``                           403
any other ``DomainError``                                  its ``status_code``
``agent.AgentInputError`` / ``AgentNotAllowed``             422 / 403
``agent.AgentUnavailable``                                 503
SQLAlchemy connection / DBAPI errors                       503
anything else                                              500 (logged with traceback)
=========================================================  ======

Every router is created with ``route_class=TactiRoute`` so the mapping also covers exceptions
raised inside dependencies.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from tactidose.medication.errors import DomainError

log = logging.getLogger(__name__)

__all__ = [
    "TactiRoute",
    "call_supported",
    "command_result_view",
    "device_view",
    "error_detail",
    "error_status",
    "install_error_handlers",
    "require_service",
    "supported_kwargs",
    "to_dict",
    "trigger_scheduler",
]

SERVER_ERROR = "Something went wrong on the server. Please try again."
DB_UNAVAILABLE = "The database is not available right now. Please try again shortly."

#: Fallbacks by class name, for errors that do not extend ``DomainError`` (e.g. ``tactidose.agent``).
_STATUS_BY_NAME = {
    "AuthError": 401, "AuthenticationError": 401, "PermissionDenied": 403,
    "AgentNotAllowed": 403, "AgentInputError": 422,
    "AgentUnavailable": 503, "ServiceUnavailable": 503, "RecognizerUnavailable": 503,
}


def error_status(exc: BaseException) -> int | None:
    """HTTP status for an expected service exception, or None (= a bug -> 500)."""
    if isinstance(exc, (HTTPException, StarletteHTTPException, RequestValidationError)):
        return None
    if isinstance(exc, DomainError):
        return int(exc.status_code)
    for name in (c.__name__ for c in type(exc).__mro__):
        if name in _STATUS_BY_NAME:
            return _STATUS_BY_NAME[name]
    try:
        from sqlalchemy.exc import DBAPIError, OperationalError

        if isinstance(exc, (OperationalError, DBAPIError)):
            return 503
    except ImportError:  # pragma: no cover - sqlalchemy is a hard dependency
        pass
    # Our own modules may define further errors with a ``status_code`` (third-party ones must not leak).
    code = getattr(exc, "status_code", None)
    if (
        type(exc).__module__.startswith("tactidose.")
        and isinstance(code, int)
        and not isinstance(code, bool)
        and 400 <= code < 600
    ):
        return code
    return None


def error_detail(exc: BaseException, status_code: int) -> str:
    if status_code == 503 and not isinstance(exc, DomainError) and "DBAPIError" in {
        c.__name__ for c in type(exc).__mro__
    }:
        return DB_UNAVAILABLE
    message = getattr(exc, "message", None)
    text = message if isinstance(message, str) and message else str(exc)
    return text or {401: "Please sign in.", 403: "You are not allowed to do this."}.get(status_code, "Request failed.")


class TactiRoute(APIRoute):
    """APIRoute that maps service exceptions to ``{"detail": ...}`` responses (see module docs)."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await original(request)
            except (HTTPException, StarletteHTTPException, RequestValidationError):
                raise
            except Exception as exc:  # noqa: BLE001 - mapped or logged below
                code = error_status(exc)
                if code is None:
                    log.exception("unhandled error in %s %s", request.method, request.url.path)
                    return JSONResponse({"detail": SERVER_ERROR}, status_code=500)
                if code >= 500:
                    log.warning("%s %s -> %d: %s", request.method, request.url.path, code, exc)
                headers: dict[str, str] = {}
                retry = getattr(exc, "retry_after_s", None)
                if isinstance(retry, (int, float)) and not isinstance(retry, bool) and retry > 0:
                    headers["Retry-After"] = str(int(retry))
                if code == 401:
                    headers["WWW-Authenticate"] = "Bearer"
                return JSONResponse({"detail": error_detail(exc, code)}, status_code=code, headers=headers or None)

        return route_handler


def _validation_message(exc: RequestValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path", "header")]
        msg = str(err.get("msg", "invalid value"))
        parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid request."


def install_error_handlers(app: FastAPI) -> None:
    """Readable 422s (``detail`` is a sentence, not FastAPI's list of error objects)."""

    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_message(exc)}, status_code=422)

    app.add_exception_handler(RequestValidationError, on_validation_error)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- views


def command_result_view(result: Any) -> dict[str, Any] | None:
    """API.md ``CommandResultView`` for a ``protocol.CommandResult``."""
    if result is None:
        return None
    try:
        certainty = result.drop_certainty.value
    except Exception:  # noqa: BLE001 - only meaningful for drop commands
        certainty = None
    return {
        "command": result.command.to_line(),
        "ok": bool(result.ok),
        "code": result.code,
        "definitive": bool(result.definitive),
        "gate_may_be_open": bool(result.gate_may_be_open),
        "elapsed_s": round(float(result.elapsed_s or 0.0), 3),
        "messages": [m.raw or m.to_line() for m in result.messages],
        "hardware_result": result.hardware_result,
        "detail": result.detail or None,
        "drop_certainty": certainty,
    }


def device_view(hardware: Any) -> dict[str, Any]:
    return hardware.snapshot().to_dict()


def require_service(services: Any, name: str, what: str) -> Any:
    """The optional service ``name`` or 503 ("<what> is not available right now.")."""
    svc = getattr(services, name, None)
    if svc is None:
        raise HTTPException(503, f"{what} is not available right now.")
    return svc


def trigger_scheduler(services: Any) -> None:
    """Wake the scheduler loop (schedule/settings edits, clock travel)."""
    loop = getattr(services, "scheduler_loop", None)
    if loop is not None:
        loop.trigger()


def to_dict(obj: Any) -> Any:
    """``obj.to_dict()`` for dataclass outcomes, the object itself for dicts/lists."""
    fn = getattr(obj, "to_dict", None)
    return fn() if callable(fn) else obj


def supported_kwargs(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """The subset of ``kwargs`` that ``fn`` (a function or class) accepts by name."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    named = {n for n, p in params.items() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    return {k: v for k, v in kwargs.items() if k in named}


def call_supported(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` passing only the keyword arguments it declares.

    Used for parallel-module entry points whose optional keyword arguments differ slightly
    between the architecture doc and the implementation (e.g. ``auth=`` / ``notifications=``).
    """
    accepted = supported_kwargs(fn, kwargs)
    dropped = sorted(set(kwargs) - set(accepted))
    if dropped:
        log.debug("%s: not passing unsupported argument(s) %s", getattr(fn, "__qualname__", fn), dropped)
    return fn(*args, **accepted)
