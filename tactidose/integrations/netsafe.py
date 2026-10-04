"""Outbound-call safety shared by every cloud client (Gemini, ElevenLabs, ``check-apis``).

API keys travel in custom headers (``x-goog-api-key``, ``xi-api-key``). When httpx follows a
redirect to another host it drops only ``Authorization``, so a filtering proxy that answers an API
call with "307 -> sign-in or block page" would receive the key (and, for a 307, the request body).
Cloud clients therefore never follow redirects: a 3xx means "blocked by the network".

:func:`classify_status` and :func:`classify_exception` turn an HTTP status or an exception into a
:class:`Failure` - a short code, a plain sentence and what to do - for logs, fallbacks and
``python -m tactidose check-apis``. Nothing here performs I/O or logs secrets.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "BAD_REQUEST", "BLOCKED", "ERROR", "Failure", "INVALID_KEY", "NETWORK_ERROR", "NOT_CONFIGURED",
    "NOT_FOUND", "NO_REDIRECTS", "OK", "PERMISSION", "QUOTA", "SERVER_ERROR", "TIMEOUT", "TLS_ERROR",
    "classify_exception", "classify_response", "classify_status", "gemini_http_options", "redact",
    "redirect_host",
]

#: httpx client keyword arguments shared by every cloud client.
NO_REDIRECTS: dict[str, Any] = {"follow_redirects": False}

OK = "OK"
NOT_CONFIGURED = "NOT_CONFIGURED"
BLOCKED = "BLOCKED_BY_NETWORK"
TLS_ERROR = "TLS_ERROR"
TIMEOUT = "TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
INVALID_KEY = "INVALID_KEY"
PERMISSION = "PERMISSION_DENIED"
QUOTA = "QUOTA_EXCEEDED"
NOT_FOUND = "NOT_FOUND"
BAD_REQUEST = "BAD_REQUEST"
SERVER_ERROR = "SERVER_ERROR"
ERROR = "ERROR"

_MAX_DETAIL = 200

_HINT_BLOCKED = ("A web filter or proxy on this network blocks this service; the app keeps working "
                 "offline. Use a network that allows it, or ask your IT team.")
_HINT_TLS = ("A TLS-inspecting proxy re-signs HTTPS traffic on this network. Point SSL_CERT_FILE and "
             "REQUESTS_CA_BUNDLE at a CA bundle that includes the proxy's root certificate, or use "
             "another network.")
_HINT_KEY = "Check the key in .env: the whole key, no quotes or spaces, and not revoked."
_HINT_QUOTA = ("Rate limit or quota reached. Wait a minute and retry; on the Gemini free tier use a "
               "Flash-Lite model (GEMINI_MODEL) or enable billing; on ElevenLabs check your credits.")


@dataclass(frozen=True)
class Failure:
    """A classified failure: ``code`` (one of the constants above), a plain ``message`` and a ``hint``."""

    code: str
    message: str
    hint: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def gemini_http_options(types: Any, timeout_ms: int | None = None) -> Any:
    """``types.HttpOptions`` for constructing a ``genai.Client``: redirects off (the API key is a
    custom header) and an optional timeout in milliseconds. The app only uses the sync client."""
    kwargs: dict[str, Any] = {"client_args": dict(NO_REDIRECTS)}
    if timeout_ms is not None:
        kwargs["timeout"] = max(1, int(timeout_ms))
    return types.HttpOptions(**kwargs)


def redact(secret: Any) -> str:
    """Safe description of a secret for logs and reports, e.g. ``set (39 chars, ends ...3kQ)``."""
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if not value:
        return "not set"
    text = str(value)
    if len(text) < 16:
        return f"set ({len(text)} chars)"
    return f"set ({len(text)} chars, ends ...{text[-3:]})"


def redirect_host(location: str | None) -> str:
    """Host of a redirect target, never its path or query (block pages can carry the user's identity)."""
    host = urlsplit(location).hostname if location else None
    return host or "another site"


def _detail(body: Any) -> str:
    text = " ".join(str(body or "").split())
    return text[:_MAX_DETAIL] + ("..." if len(text) > _MAX_DETAIL else "")


def classify_status(status: int, body: Any = "", *, location: str | None = None) -> Failure:
    """Classify a non-2xx HTTP answer from a cloud API (``body`` = error text or JSON as text)."""
    detail = _detail(body)
    text = detail.lower()
    if 300 <= status < 400:
        return Failure(BLOCKED, f"the network redirected the request to {redirect_host(location)}", _HINT_BLOCKED)
    if status == 400:
        if "api key not valid" in text or "api_key_invalid" in text:
            return Failure(INVALID_KEY, "the API key was rejected", _HINT_KEY)
        if "location is not supported" in text or "user location" in text:
            return Failure(PERMISSION, "the service is not available in this region", "Use a supported region.")
        return Failure(BAD_REQUEST, f"the request was rejected (400): {detail}" if detail else "the request was rejected (400)")
    if status == 401:
        if "unusual activity" in text or "unusual_activity" in text:
            return Failure(PERMISSION, "ElevenLabs turned off free-tier use from this network",
                           "Free plans are blocked on shared or proxy networks; a paid plan or another network avoids it.")
        if "quota_exceeded" in text or "quota exceeded" in text:
            return Failure(QUOTA, "the account is out of credits (401 quota_exceeded)", _HINT_QUOTA)
        if "missing_permissions" in text or "missing permission" in text:
            return Failure(PERMISSION, "the key is missing a permission (401 missing_permissions)",
                           "Give the key the permissions it needs (ElevenLabs: Text to Speech, and Voices read).")
        return Failure(INVALID_KEY, "the API key was rejected (401)", _HINT_KEY)
    if status == 402:
        return Failure(QUOTA, "the account is out of credits (402)", _HINT_QUOTA)
    if status == 403:
        return Failure(PERMISSION, f"access denied (403): {detail}" if detail else "access denied (403)",
                       "Check that the key is enabled for this API (ElevenLabs: give the key Text to Speech access).")
    if status == 404:
        return Failure(NOT_FOUND, f"not found (404): {detail}" if detail else "not found (404)",
                       "Check the model or voice id in .env.")
    if status == 408:
        return Failure(TIMEOUT, "the service timed out (408)", "Retry; check the network.")
    if status == 422:
        return Failure(BAD_REQUEST, f"the request was rejected (422): {detail}" if detail else "the request was rejected (422)")
    if status == 429:
        return Failure(QUOTA, "too many requests or daily quota reached (429)", _HINT_QUOTA)
    if 500 <= status < 600:
        return Failure(SERVER_ERROR, f"the service had an error ({status})", "Retry in a minute.")
    return Failure(ERROR, f"unexpected answer ({status}): {detail}" if detail else f"unexpected answer ({status})")


def classify_response(response: Any) -> Failure:
    """:func:`classify_status` for an ``httpx.Response``."""
    try:
        body = response.text
    except Exception:  # noqa: BLE001 - streaming or undecodable body
        body = ""
    return classify_status(int(response.status_code), body, location=response.headers.get("location"))


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen and len(seen) < 8:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def classify_exception(exc: BaseException) -> Failure:
    """Classify an exception raised by a cloud client (httpx, google-genai, sockets, SSL)."""
    chain = _chain(exc)
    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            return Failure(TLS_ERROR, "the secure connection could not be verified", _HINT_TLS)
    try:
        from google.genai import errors as genai_errors
    except Exception:  # noqa: BLE001 - google-genai not installed
        genai_errors = None
    for e in chain:
        if genai_errors is not None and isinstance(e, genai_errors.APIError):
            code = getattr(e, "code", None)
            if isinstance(code, int):
                headers = getattr(getattr(e, "response", None), "headers", None)
                location = headers.get("location") if headers is not None else None
                return classify_status(code, getattr(e, "message", None) or str(e), location=location)
        response = getattr(e, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and type(e).__name__ == "HTTPStatusError":
            return classify_response(response)
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a dependency
        httpx = None  # type: ignore[assignment]
    for e in chain:
        if (httpx is not None and isinstance(e, httpx.TimeoutException)) or isinstance(e, TimeoutError):
            return Failure(TIMEOUT, "the service did not answer in time", "Check the network and retry.")
    for e in chain:
        if (httpx is not None and isinstance(e, (httpx.ConnectError, httpx.NetworkError))) or isinstance(e, ConnectionError):
            return Failure(NETWORK_ERROR, "could not connect (DNS, firewall or no internet)",
                           "Check the internet connection; a firewall may block this service.")
    for e in chain:
        if isinstance(e, OSError):
            return Failure(NETWORK_ERROR, f"network error: {_detail(e)}", "Check the internet connection.")
    return Failure(ERROR, f"{type(exc).__name__}: {_detail(exc)}")
