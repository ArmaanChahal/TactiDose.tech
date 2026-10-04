"""Live API checks behind ``python -m tactidose check-apis``.

One tiny real request per *configured* cloud service shows which keys work on this network before a
demo. Services without settings are never contacted; their ``NOT_CONFIGURED`` row says which ``.env``
keys to set.

=============  =====================================================================================
gemini         REST ``generateContent`` probe (key in ``x-goog-api-key``, never in the URL); on a 404
               the fallback model is probed, and a working fallback is a ``WARN``
gemini-agent   google-genai SDK call shaped like the assistant's (one function declaration, automatic
               function calling off, ``agent_thinking_level``); only when the probe got through
elevenlabs     one short text-to-speech request; when the voice is not available to the account and
               ``elevenlabs_auto_voice`` is on, the account's first premade voice is tried (``WARN``)
snowflake      credential-free pre-flight POST without redirects (the connector follows redirects and
               would re-send its login body), then the sync's own connection settings and
               ``SELECT CURRENT_VERSION(), CURRENT_WAREHOUSE(), ...``
tidb           ``SELECT VERSION()`` over PyMySQL with the app's TLS arguments
smtp           connect, EHLO, STARTTLS, sign in, QUIT - no message is sent
=============  =====================================================================================

Every HTTP request uses :data:`netsafe.NO_REDIRECTS` (a 3xx is ``BLOCKED_BY_NETWORK``: following it
would hand the key, and for a 307 the body, to a web filter's page), gives up after
:data:`TIMEOUT_S` and is never retried. Checks report service problems as rows instead of raising, and
:func:`run_checks` turns any crash into an ``ERROR`` row. Results never contain a key, password or
token - at most the :func:`netsafe.redact` fingerprint (length and last three characters).
"""

from __future__ import annotations

import functools
import logging
import re
import smtplib
import ssl
import textwrap
import time
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from tactidose.config import Settings
from tactidose.integrations import netsafe as ns

log = logging.getLogger(__name__)

__all__ = [
    "PASSING", "SERVICES", "TIMEOUT_S", "WARN", "CheckResult", "check_elevenlabs", "check_gemini",
    "check_smtp", "check_snowflake", "check_tidb", "format_report", "gemini_features", "pick_voice",
    "run_checks",
]

#: Every HTTP request, SDK call, database login and SMTP step gives up after this many seconds.
TIMEOUT_S = 15.0
#: Check order and the names ``--only`` accepts (``gemini`` also yields the ``gemini-agent`` row).
SERVICES = ("gemini", "elevenlabs", "snowflake", "tidb", "smtp")
WARN = "WARN"
#: Statuses that do not fail the command (exit code 0).
PASSING = frozenset({ns.OK, WARN, ns.NOT_CONFIGURED})

GEMINI_API = "https://generativelanguage.googleapis.com"
GEMINI_PROBE_BODY: dict[str, Any] = {
    "contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}]}
GEMINI_AGENT_PROMPT = "Say OK."
ELEVENLABS_API = "https://api.elevenlabs.io"
ELEVENLABS_TEST_TEXT = "TactiDose voice test."
SNOWFLAKE_QUERY = ("SELECT CURRENT_VERSION(), CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA(), "
                   "CURRENT_ROLE()")

#: (feature, the setting that stops it using Gemini), in report order.
_GEMINI_SWITCHES = (
    ("assistant", "TACTIDOSE_AGENT_PROVIDER=rules"),
    ("report summaries", "TACTIDOSE_REPORT_AI_SUMMARY=false"),
    ("label scanning", "TACTIDOSE_LABEL_EXTRACTOR=disabled"),
)
#: ElevenLabs statuses whose body may say "this voice is not available to you".
_VOICE_STATUSES = frozenset({400, 402, 403, 404, 422})
_HINT_BLOCKED = ns.classify_status(307).hint          # netsafe's wording: a web filter answered
_HINT_QUOTA = ns.classify_status(429).hint
_HINT_GMAIL = ("Gmail needs an app password (turn on 2-Step Verification, then "
               "myaccount.google.com/apppasswords); put it in SMTP_PASSWORD and the Gmail address in SMTP_USER.")
_HINT_SMTP_PORTS = ("Outgoing mail ports (587, 465) are often blocked on corporate and venue networks: "
                    "use a network that allows outgoing mail, or leave SMTP empty (reports are then saved as .eml files).")
_HINT_SMTP_TLS = "Gmail: SMTP_PORT=587 with SMTP_STARTTLS=true, or SMTP_PORT=465 with SMTP_SSL=true."
_MAX_TEXT = 200


# =========================================================================== result


@dataclass
class CheckResult:
    """One report row. ``status`` is a :mod:`netsafe` code, ``WARN`` or ``NOT_CONFIGURED``; ``hint``
    says what to do; ``info`` holds non-secret machine-readable facts (``--json``)."""

    service: str
    status: str
    detail: str
    hint: str = ""
    elapsed_s: float | None = None
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status not in PASSING

    def to_dict(self) -> dict[str, Any]:
        return {"service": self.service, "status": self.status, "failed": self.failed, "detail": self.detail,
                "hint": self.hint, "elapsed_s": self.elapsed_s, "info": dict(self.info)}


# =========================================================================== secrets


def _secret_values(settings: Settings) -> list[str]:
    """Every secret the settings hold (longest first, so a secret containing another is fully masked)."""
    values: list[str] = []
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            if raw and len(raw) >= 4:
                values.append(raw)
    if settings.database_url:
        try:
            from sqlalchemy.engine import make_url

            password = make_url(settings.database_url).password
            if password:
                values.append(str(password))
        except Exception:  # noqa: BLE001 - an unparseable URL has nothing to mask
            log.debug("database URL could not be parsed for masking")
    return sorted(set(values), key=len, reverse=True)


def _mask(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, Mapping):
        return {k: _mask(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v, secrets) for v in value]
    return value


def _scrub(result: CheckResult, secrets: Sequence[str]) -> CheckResult:
    return replace(result, detail=_mask(result.detail, secrets), hint=_mask(result.hint, secrets),
                   info=_mask(result.info, secrets))


_R = TypeVar("_R", CheckResult, list[CheckResult])


def _scrubbed(func: Callable[..., _R]) -> Callable[..., _R]:
    """Mask every secret of ``settings`` (the first argument) in the check's result(s)."""

    @functools.wraps(func)
    def wrapper(settings: Settings, *args: Any, **kwargs: Any) -> _R:
        out = func(settings, *args, **kwargs)
        secrets = _secret_values(settings)
        if isinstance(out, list):
            return [_scrub(r, secrets) for r in out]
        return _scrub(out, secrets)

    return wrapper


def _short(exc: BaseException | str) -> str:
    text = " ".join(str(exc).split())
    return text[:_MAX_TEXT] + ("..." if len(text) > _MAX_TEXT else "")


def _since(started: float) -> float:
    return round(time.monotonic() - started, 2)


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _quiet_close(obj: Any) -> None:
    try:
        obj.close()
    except Exception:  # noqa: BLE001 - the result is already decided
        log.debug("close() failed", exc_info=True)


# =========================================================================== HTTP


def _new_client() -> httpx.Client:
    """HTTP client for the probes: never follows redirects; :data:`TIMEOUT_S` for every phase."""
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), **ns.NO_REDIRECTS)


@contextmanager
def _http_client(http: httpx.Client | None) -> Iterator[httpx.Client]:
    if http is not None:            # injected: the caller owns (and closes) it
        yield http
        return
    client = _new_client()
    try:
        yield client
    finally:
        client.close()


@dataclass
class _Answer:
    response: httpx.Response | None
    failure: ns.Failure | None
    elapsed_s: float


def _send(client: httpx.Client, method: str, url: str, *, headers: Mapping[str, str],
          json_body: Any = None, params: Mapping[str, str] | None = None,
          classify: Callable[[httpx.Response], ns.Failure] = ns.classify_response) -> _Answer:
    """One request; non-2xx answers, transport errors and HTML block pages become a ``failure``."""
    started = time.monotonic()
    try:
        # follow_redirects per request too: even an injected client never forwards the key.
        response = client.request(method, url, headers=dict(headers), json=json_body, params=params,
                                  follow_redirects=False, timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - transport, TLS and timeout errors are results here
        return _Answer(None, ns.classify_exception(exc), _since(started))
    elapsed = _since(started)
    if not 200 <= response.status_code < 300:
        failure = classify(response)
        if failure.code == ns.BLOCKED:
            failure = replace(failure, message=failure.message + " (the redirect was not followed)")
        return _Answer(response, failure, elapsed)
    if "text/html" in response.headers.get("content-type", "").lower():
        return _Answer(response, ns.Failure(
            ns.BLOCKED, "the network answered with a web page instead of the service", _HINT_BLOCKED), elapsed)
    return _Answer(response, None, elapsed)


def _json(response: httpx.Response | None) -> Any:
    try:
        return response.json() if response is not None else None
    except ValueError:
        return None


def _body_text(response: httpx.Response | None) -> str:
    try:
        return response.text if response is not None else ""
    except Exception:  # noqa: BLE001 - undecodable body
        return ""


# =========================================================================== Gemini


def gemini_features(settings: Settings) -> dict[str, bool]:
    """Which features use Gemini with a key: the assistant (``effective_agent_provider == "gemini"``),
    report summaries (``report_ai_summary``) and label scanning (``effective_label_extractor ==
    "gemini"``). Without a key: what setting one would turn on (``auto`` counts as on)."""
    if settings.gemini_configured:
        return {"assistant": settings.effective_agent_provider == "gemini",
                "report summaries": bool(settings.report_ai_summary),
                "label scanning": settings.effective_label_extractor == "gemini"}
    return {"assistant": settings.agent_provider in ("auto", "gemini"),
            "report summaries": bool(settings.report_ai_summary),
            "label scanning": settings.label_extractor in ("auto", "gemini")}


def _features_info(features: Mapping[str, bool]) -> dict[str, Any]:
    return {"features_on": [n for n, on in features.items() if on],
            "features_off": [n for n, on in features.items() if not on],
            "turn_off": {n: switch for n, switch in _GEMINI_SWITCHES if features.get(n)}}


def _features_text(features: Mapping[str, bool]) -> tuple[str, str]:
    """(``"Gemini is on for: ..."``, ``"To turn one off: ..."``)."""
    on = [n for n, v in features.items() if v]
    off = [n for n, v in features.items() if not v]
    text = "Gemini is on for: " + (", ".join(on) if on else "nothing (every Gemini feature is switched off)")
    if on and off:
        text += f" (off: {', '.join(off)})"
    switches = [switch for n, switch in _GEMINI_SWITCHES if features.get(n)]
    return text, ("To turn one off: " + ", ".join(switches)) if switches else ""


def _model_id(name: str | None) -> str:
    return (name or "").strip().removeprefix("models/")


def _gemini_probe(client: httpx.Client, key: str, model: str) -> _Answer:
    url = f"{GEMINI_API}/v1beta/models/{quote(model, safe='-._~')}:generateContent"
    answer = _send(client, "POST", url, json_body=GEMINI_PROBE_BODY,
                   headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    if answer.failure is None and not isinstance(_json(answer.response), dict):
        answer.failure = ns.Failure(ns.ERROR, "the answer was not the API's JSON",
                                    "Run the check again; if it repeats, a proxy may be rewriting answers.")
    return answer


def _key_row(service: str, failure: ns.Failure, elapsed: float | None, info: dict[str, Any], *,
             key_name: str, key: Any, prefix: str = "") -> CheckResult:
    detail = prefix + failure.message
    if failure.code in (ns.INVALID_KEY, ns.PERMISSION):
        detail += f"; {key_name} is {ns.redact(key)}"
    return CheckResult(service, failure.code, detail, failure.hint, elapsed, info)


@_scrubbed
def check_gemini(settings: Settings, *, http: httpx.Client | None = None, sdk_client: Any = None) -> list[CheckResult]:
    """Rows ``gemini`` (REST probe) and, when it got through, ``gemini-agent`` (SDK call)."""
    features = gemini_features(settings)
    if not settings.gemini_configured:
        on = [n for n, v in features.items() if v]
        return [CheckResult(
            "gemini", ns.NOT_CONFIGURED,
            "set GEMINI_API_KEY in .env (free key: aistudio.google.com/apikey) to turn on: "
            + (", ".join(on) if on else "nothing (every Gemini feature is switched off)"),
            info={"set": ["GEMINI_API_KEY"], **_features_info(features)})]
    key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
    model = _model_id(settings.gemini_model)
    fallback = _model_id(settings.gemini_fallback_model)
    info: dict[str, Any] = {"model": model, "key": ns.redact(key), **_features_info(features)}
    working: str | None = None
    with _http_client(http) as client:
        first = _gemini_probe(client, key, model)
        if first.failure is None:
            working = model
            row = CheckResult("gemini", ns.OK, f"{model} answered in {first.elapsed_s:.1f} s",
                              elapsed_s=first.elapsed_s, info=info)
        elif first.failure.code == ns.NOT_FOUND and fallback and fallback != model:
            second = _gemini_probe(client, key, fallback)
            elapsed = round(first.elapsed_s + second.elapsed_s, 2)
            if second.failure is None:
                working = fallback
                row = CheckResult(
                    "gemini", WARN, f"GEMINI_MODEL {model} was not found; {fallback} works - set GEMINI_MODEL={fallback}",
                    "Until then the app tries GEMINI_MODEL first and switches to the fallback after the 404.",
                    elapsed, {**info, "fallback_model": fallback})
            else:
                row = _key_row("gemini", second.failure, elapsed, info, key_name="GEMINI_API_KEY", key=key,
                               prefix=f"GEMINI_MODEL {model} was not found, and the fallback {fallback} failed too: ")
        else:
            row = _key_row("gemini", first.failure, first.elapsed_s, info, key_name="GEMINI_API_KEY", key=key)
    rows = [row]
    if working is not None:
        rows.append(_check_gemini_agent(settings, key, working, features, sdk_client,
                                        primary_missing=working != model))
    return rows


def _new_genai_client(genai: Any, types: Any, key: str) -> Any:
    """``genai.Client`` built like the app's: Gemini Developer API, no redirects, 15 s per request."""
    return genai.Client(api_key=key, vertexai=False,
                        http_options=ns.gemini_http_options(types, int(TIMEOUT_S * 1000)))


def _agent_config(types: Any, thinking_level: str) -> Any:
    """The assistant's request shape: one function declaration, automatic calling off, thinking level."""
    kwargs: dict[str, Any] = {
        "tools": [types.Tool(function_declarations=[types.FunctionDeclaration(
            name="get_patient_status", description="Read the patient's containers and cooldown.",
            parameters_json_schema={"type": "object", "properties": {}})])],
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    if thinking_level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level.upper()))
    return types.GenerateContentConfig(**kwargs)


def _reply_kind(response: Any) -> tuple[bool, str]:
    """(usable, description) of an SDK answer: a function call or text is usable."""
    calls = list(getattr(response, "function_calls", None) or [])
    if calls:
        return True, f"asked to call {getattr(calls[0], 'name', None) or 'a tool'}"
    try:
        parts = response.candidates[0].content.parts or []
    except (AttributeError, IndexError, TypeError):
        parts = []
    if any(isinstance(getattr(p, "text", None), str) and p.text.strip() and not getattr(p, "thought", False)
           for p in parts):
        return True, "answered"
    try:
        reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    return False, f"returned no text ({getattr(reason, 'name', None) or reason or 'no candidate'})"


def _check_gemini_agent(settings: Settings, key: str, probe_model: str, features: Mapping[str, bool],
                        sdk_client: Any, *, primary_missing: bool) -> CheckResult:
    agent_model = _model_id(settings.effective_agent_model)
    fallback = _model_id(settings.gemini_fallback_model)
    if primary_missing and agent_model == _model_id(settings.gemini_model):
        agent_model = probe_model            # the probe already found GEMINI_MODEL missing: use the fallback
    level = settings.agent_thinking_level
    shape = "function calling" + (f", thinking {level}" if level else "")
    on_text, off_hint = _features_text(features)
    info: dict[str, Any] = {"model": agent_model, "thinking_level": level or None, **_features_info(features)}
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return CheckResult("gemini-agent", ns.ERROR, "google-genai is not installed, so the app cannot use Gemini",
                           "pip install 'tactidose[gemini]'", info=info)
    owned = sdk_client is None
    client = sdk_client
    used = agent_model
    started = time.monotonic()
    try:
        config = _agent_config(types, level)
        if client is None:
            client = _new_genai_client(genai, types, key)
        try:
            response = client.models.generate_content(model=used, contents=GEMINI_AGENT_PROMPT, config=config)
        except Exception as exc:  # noqa: BLE001 - the assistant also falls back once on a 404
            if not (ns.classify_exception(exc).code == ns.NOT_FOUND and fallback and fallback != used):
                raise
            used = fallback
            response = client.models.generate_content(model=used, contents=GEMINI_AGENT_PROMPT, config=config)
    except Exception as exc:  # noqa: BLE001 - SDK, transport and API errors are results here
        failure = ns.classify_exception(exc)
        hint = failure.hint
        if failure.code == ns.BAD_REQUEST and level:
            hint = (hint + " " if hint else "") + ("If the message is about thinking, clear "
                                                    "TACTIDOSE_AGENT_THINKING_LEVEL (the model may not support it).")
        return _key_row("gemini-agent", replace(failure, hint=hint), _since(started), info, key_name="GEMINI_API_KEY",
                        key=key, prefix=f"assistant request ({used}, {shape}) failed: ")
    finally:
        if owned and client is not None:
            _quiet_close(client)
    elapsed = _since(started)
    info["model"] = used
    usable, how = _reply_kind(response)
    if not usable:
        return CheckResult("gemini-agent", WARN, f"assistant request ({used}, {shape}) {how} in {elapsed:.1f} s",
                           "The assistant answers with the offline rules when Gemini returns no text; retry, "
                           "or try another GEMINI_MODEL.", elapsed, info)
    if used != agent_model:
        return CheckResult(
            "gemini-agent", WARN, f"agent model {agent_model} was not found; {used} {how} in {elapsed:.1f} s",
            f"Set TACTIDOSE_AGENT_MODEL={used} (or leave it empty to use GEMINI_MODEL).", elapsed, info)
    return CheckResult("gemini-agent", ns.OK, f"assistant request ({used}, {shape}) {how} in {elapsed:.1f} s. {on_text}",
                       off_hint, elapsed, info)


# =========================================================================== ElevenLabs


def _elevenlabs_failure(response: httpx.Response) -> ns.Failure:
    """netsafe's classification, plus two ElevenLabs 401 bodies that are not a bad key."""
    text = _body_text(response).lower()
    if response.status_code == 401 and "quota_exceeded" in text:
        return ns.Failure(ns.QUOTA, "the account has no credits left (401 quota_exceeded)", _HINT_QUOTA)
    if response.status_code == 401 and "missing_permissions" in text:
        return ns.Failure(ns.PERMISSION, "the API key lacks a permission (401 missing_permissions)",
                          "Edit the key in ElevenLabs: allow Text to Speech, and Voices (read) for the "
                          "automatic voice choice.")
    return ns.classify_response(response)


def _elevenlabs_reason(response: httpx.Response) -> str:
    """``404 voice_not_found``: the HTTP status and the ``detail.status`` code of an error body."""
    data = _json(response)
    detail = data.get("detail") if isinstance(data, dict) else None
    code = (detail.get("status") or detail.get("code")) if isinstance(detail, dict) else None
    return f"{response.status_code} {code}" if isinstance(code, str) and code else str(response.status_code)


def _tts(client: httpx.Client, key: str, voice_id: str, model_id: str, output_format: str) -> _Answer:
    answer = _send(client, "POST", f"{ELEVENLABS_API}/v1/text-to-speech/{quote(voice_id, safe='')}",
                   headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/*"},
                   params={"output_format": output_format},
                   json_body={"text": ELEVENLABS_TEST_TEXT, "model_id": model_id}, classify=_elevenlabs_failure)
    if answer.failure is None and not answer.response.content:
        answer.failure = ns.Failure(ns.ERROR, "the answer contained no audio")
    return answer


def pick_voice(voices: Iterable[Any], *, exclude: str = "") -> tuple[str, str] | None:
    """``(voice_id, name)`` to use instead of ``exclude`` from ``GET /v1/voices``: the first voice in
    category ``premade``, else ``default``, else the first one; legacy voices are skipped."""
    usable: list[tuple[str, str, str]] = []
    for v in voices:
        if not isinstance(v, Mapping) or v.get("is_legacy"):
            continue
        voice_id = str(v.get("voice_id") or "").strip()
        if voice_id and voice_id != exclude:
            usable.append((voice_id, str(v.get("name") or voice_id), str(v.get("category") or "").lower()))
    for category in ("premade", "default"):
        for voice_id, name, cat in usable:
            if cat == category:
                return voice_id, name
    return (usable[0][0], usable[0][1]) if usable else None


@_scrubbed
def check_elevenlabs(settings: Settings, *, http: httpx.Client | None = None) -> CheckResult:
    """Row ``elevenlabs``: one short text-to-speech request (and the voice fallback, see module doc)."""
    if not settings.elevenlabs_configured:
        return CheckResult("elevenlabs", ns.NOT_CONFIGURED,
                           "set ELEVENLABS_API_KEY in .env for the natural voice; until then the offline voice speaks",
                           info={"set": ["ELEVENLABS_API_KEY"]})
    if settings.tts_provider != "elevenlabs":
        return CheckResult("elevenlabs", ns.NOT_CONFIGURED,
                           f"ELEVENLABS_API_KEY is set but not used: TACTIDOSE_TTS_PROVIDER is "
                           f"{settings.tts_provider}; set TACTIDOSE_TTS_PROVIDER=elevenlabs to use and test it",
                           info={"set": ["TACTIDOSE_TTS_PROVIDER=elevenlabs"]})
    key = settings.elevenlabs_api_key.get_secret_value() if settings.elevenlabs_api_key else ""
    voice = settings.elevenlabs_voice_id.strip()
    model, fmt = settings.elevenlabs_model_id, settings.elevenlabs_output_format
    info: dict[str, Any] = {"voice_id": voice, "model_id": model, "output_format": fmt, "key": ns.redact(key)}
    with _http_client(http) as client:
        first = _tts(client, key, voice, model, fmt)
        if first.failure is None:
            size = len(first.response.content)
            return CheckResult("elevenlabs", ns.OK,
                               f"voice {voice} ({model}) answered in {first.elapsed_s:.1f} s with {size} bytes of audio",
                               elapsed_s=first.elapsed_s, info={**info, "bytes": size})
        f = first.failure
        voice_problem = (first.response is not None and first.response.status_code in _VOICE_STATUSES
                         and "voice" in _body_text(first.response).lower())
        if not voice_problem:
            return _key_row("elevenlabs", f, first.elapsed_s, info, key_name="ELEVENLABS_API_KEY", key=key)
        unavailable = f"voice {voice} is not available on this account ({_elevenlabs_reason(first.response)})"
        if not settings.elevenlabs_auto_voice:
            return CheckResult("elevenlabs", f.code, unavailable,
                               "Set ELEVENLABS_VOICE_ID to a voice ID from your ElevenLabs account (Voices), or "
                               "TACTIDOSE_ELEVENLABS_AUTO_VOICE=true to let the app pick one.", first.elapsed_s, info)
        listing = _send(client, "GET", f"{ELEVENLABS_API}/v1/voices", classify=_elevenlabs_failure,
                        headers={"xi-api-key": key, "Accept": "application/json"})
        elapsed = round(first.elapsed_s + listing.elapsed_s, 2)
        data = _json(listing.response) if listing.failure is None else None
        voices = data.get("voices") if isinstance(data, dict) else None
        if not isinstance(voices, list):
            lf = listing.failure or ns.Failure(ns.ERROR, "the answer was not a voice list")
            return CheckResult(
                "elevenlabs", f.code,
                f"{unavailable}; the voice list could not be read ({lf.code}: {lf.message}), so no other voice was tried",
                "Set ELEVENLABS_VOICE_ID to a voice ID from your ElevenLabs account (Voices); the app cannot look "
                "one up from here either." + (f" {lf.hint}" if lf.hint and lf.code != ns.BLOCKED else ""), elapsed, info)
        picked = pick_voice(voices, exclude=voice)
        if picked is None:
            return CheckResult("elevenlabs", f.code, f"{unavailable}, and the account has no other voice to try",
                               "Add a voice in ElevenLabs (Voices) and set ELEVENLABS_VOICE_ID to its ID.", elapsed, info)
        picked_id, picked_name = picked
        second = _tts(client, key, picked_id, model, fmt)
        elapsed = round(elapsed + second.elapsed_s, 2)
        info.update(fallback_voice_id=picked_id, fallback_voice_name=picked_name)
        if second.failure is not None:
            return _key_row("elevenlabs", second.failure, elapsed, info, key_name="ELEVENLABS_API_KEY", key=key,
                            prefix=f"voice {voice} is not available on this account, and {picked_name} "
                                   f"({picked_id}) failed too: ")
        info["bytes"] = len(second.response.content)
        return CheckResult("elevenlabs", WARN,
                           f"voice {voice} is not available on this account; {picked_name} ({picked_id}) works - the app "
                           f"switches to it automatically; set ELEVENLABS_VOICE_ID={picked_id} to keep it",
                           elapsed_s=elapsed, info=info)


# =========================================================================== Snowflake


def _snowflake_host(account: str) -> str:
    """The login host the connector derives from ``SNOWFLAKE_ACCOUNT``."""
    try:
        from snowflake.connector.util_text import construct_hostname

        return str(construct_hostname(None, account))
    except Exception:  # noqa: BLE001 - connector layout changed: its documented default
        return f"{account}.snowflakecomputing.com"


def _snowflake_failure(exc: BaseException) -> ns.Failure:
    base = ns.classify_exception(exc)
    if base.code in (ns.TLS_ERROR, ns.TIMEOUT):
        return base
    text = " ".join(str(exc).split()).lower()
    detail = _short(exc)
    if "network policy" in text or "is not allowed to access snowflake" in text:
        return ns.Failure(ns.PERMISSION, f"a Snowflake network policy refused this network: {detail}",
                          "Ask the account admin to allow this IP address in the network policy (programmatic "
                          "access tokens need one that allows it), or use another network.")
    if re.search(r"\bmfa\b|multi-factor|totp|passcode", text):
        return ns.Failure(ns.PERMISSION, f"Snowflake asks for multi-factor authentication: {detail}",
                          "Create a programmatic access token in Snowsight and set SNOWFLAKE_TOKEN instead of "
                          "SNOWFLAKE_PASSWORD.")
    if any(s in text for s in ("incorrect username or password", "token is invalid", "invalid token",
                               "token has expired", "invalid oauth access token", "authentication failed")):
        return ns.Failure(ns.INVALID_KEY, f"Snowflake rejected the sign-in: {detail}",
                          "Check SNOWFLAKE_USER and SNOWFLAKE_TOKEN (programmatic access token) or "
                          "SNOWFLAKE_PASSWORD; tokens expire, so create a new one if needed.")
    if "verify the account name" in text:
        return ns.Failure(ns.NOT_FOUND, f"Snowflake does not know this account: {detail}",
                          "SNOWFLAKE_ACCOUNT is the account identifier, e.g. myorg-myaccount (the part before "
                          ".snowflakecomputing.com in your account URL).")
    if "role" in text and "does not exist or not authorized" in text:
        return ns.Failure(ns.PERMISSION, f"the role is not available to this user: {detail}",
                          "Check SNOWFLAKE_ROLE, or leave it empty to use the user's default role.")
    if "could not connect to snowflake backend" in text:
        return ns.Failure(ns.NETWORK_ERROR, f"could not reach Snowflake: {detail}",
                          "Check the internet connection; a firewall may block *.snowflakecomputing.com.")
    return base


@_scrubbed
def check_snowflake(settings: Settings, *, connect: Callable[..., Any] | None = None,
                    http: httpx.Client | None = None) -> CheckResult:
    """Row ``snowflake``: pre-flight, sign-in with the sync's settings, one metadata query.
    ``connect`` replaces ``snowflake.connector.connect`` and ``http`` the pre-flight client (tests)."""
    if not settings.snowflake_configured:
        missing = [name for name, value in (
            ("SNOWFLAKE_ACCOUNT", settings.snowflake_account), ("SNOWFLAKE_USER", settings.snowflake_user),
            ("SNOWFLAKE_TOKEN (or SNOWFLAKE_PASSWORD)", settings.snowflake_password or settings.snowflake_token
             or settings.snowflake_private_key_file)) if not value]
        return CheckResult("snowflake", ns.NOT_CONFIGURED,
                           f"set {_join(missing)} in .env to sync analytics to Snowflake (optional; until then "
                           "the rows wait in the local outbox)", info={"set": missing})
    from tactidose.integrations import snowflake as sf

    account = (settings.snowflake_account or "").strip()
    info: dict[str, Any] = {"account": account, "user": settings.snowflake_user, "auth": sf.auth_mode(settings)}
    if re.search(r"[/\\:]", account) or account.lower().endswith((".snowflakecomputing.com", ".snowflakecomputing.cn")):
        return CheckResult("snowflake", ns.BAD_REQUEST, f"SNOWFLAKE_ACCOUNT {account!r} is not an account identifier",
                           "Use only the identifier, e.g. myorg-myaccount (the part before .snowflakecomputing.com "
                           "in your account URL).", info=info)
    if connect is None:
        try:
            import snowflake.connector  # noqa: F401 - optional dependency: tactidose[snowflake]
        except ImportError:
            return CheckResult("snowflake", ns.ERROR, "the Snowflake connector is not installed",
                               "pip install 'tactidose[snowflake]'", info=info)
        connect = sf._default_connect
    host = _snowflake_host(account)
    info["host"] = host
    started = time.monotonic()
    with _http_client(http) as client:                 # no credentials in this request
        pre = _send(client, "POST", f"https://{host}/session/v1/login-request", json_body={},
                    headers={"Content-Type": "application/json", "Accept": "application/json"})
    if pre.failure is not None and (pre.response is None or pre.failure.code == ns.BLOCKED):
        return CheckResult("snowflake", pre.failure.code, f"{host}: {pre.failure.message}; no credentials were sent",
                           pre.failure.hint, pre.elapsed_s, info)
    kwargs = {**sf.connection_kwargs(settings), "login_timeout": int(TIMEOUT_S), "network_timeout": int(TIMEOUT_S)}
    conn = cur = None
    try:
        conn = connect(**kwargs)
        cur = conn.cursor()
        cur.execute(SNOWFLAKE_QUERY)
        row = tuple(cur.fetchone() or ())
    except Exception as exc:  # noqa: BLE001 - connector errors are results here
        key_name, secret = next(((n, v) for n, v in (
            ("SNOWFLAKE_TOKEN", settings.snowflake_token), ("SNOWFLAKE_PASSWORD", settings.snowflake_password),
            ("SNOWFLAKE_PRIVATE_KEY_FILE", settings.snowflake_private_key_file)) if v), ("SNOWFLAKE_TOKEN", None))
        return _key_row("snowflake", _snowflake_failure(exc), _since(started), info, key_name=key_name, key=secret)
    finally:
        for obj in (cur, conn):
            if obj is not None:
                _quiet_close(obj)
    elapsed = _since(started)
    version, warehouse, database, schema, role = (list(row) + [None] * 5)[:5]
    info.update(version=version, warehouse=warehouse, database=database, schema=schema, role=role)
    detail = (f"signed in to {account} as {settings.snowflake_user} (role {role or 'none'}, warehouse "
              f"{warehouse or 'none'}, Snowflake {version or '?'}) in {elapsed:.1f} s")
    if not database:
        detail += f"; database {settings.snowflake_database} does not exist yet - the first sync creates it"
    hints: list[str] = []
    if not warehouse:
        hints.append(f"Warehouse {settings.snowflake_warehouse} is not available to this role: check "
                     "SNOWFLAKE_WAREHOUSE and that the role may use it - the sync needs one."
                     if settings.snowflake_warehouse else
                     "Set SNOWFLAKE_WAREHOUSE (trial accounts have COMPUTE_WH) - the sync needs one.")
    if "change-me" in settings.analytics_salt.get_secret_value().lower():
        hints.append("Set TACTIDOSE_ANALYTICS_SALT to a long random string before the first sync (it pseudonymises "
                     "user ids; changing it later splits the history).")
    return CheckResult("snowflake", WARN if hints else ns.OK, detail, " ".join(hints), elapsed, info)


# =========================================================================== TiDB


def _mysql_errno(exc: BaseException) -> int | None:
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) and not isinstance(args[0], bool) else None


def _tidb_failure(exc: BaseException, settings: Settings) -> ns.Failure:
    code = _mysql_errno(exc)
    args = getattr(exc, "args", ())
    detail = _short(args[1] if code is not None and len(args) > 1 else exc)    # PyMySQL: (errno, message)
    text = str(exc).lower()
    where = f"{settings.tidb_host}:{settings.tidb_port}"
    if code == 1045:
        return ns.Failure(ns.INVALID_KEY, f"TiDB rejected the user name or password (1045); TIDB_USER is "
                                          f"{settings.tidb_user!r}, TIDB_PASSWORD is {ns.redact(settings.tidb_password)}",
                          "Copy TIDB_USER (it looks like xxxxxxxx.root) and TIDB_PASSWORD from the TiDB Cloud "
                          "Connect dialog.")
    if code == 1049:
        return ns.Failure(ns.NOT_FOUND, f"database {settings.tidb_database!r} does not exist yet (1049)",
                          "Run python -m tactidose init-db (it creates the database and the tables).")
    original = getattr(exc, "original_exception", None)
    if isinstance(original, ssl.SSLError) or any(s in text for s in ("ssl", "certificate", "tls")):
        return ns.Failure(ns.TLS_ERROR, f"the TLS connection to {where} failed: {detail}",
                          "Leave TIDB_SSL_CA empty to use the SSL_CERT_FILE / REQUESTS_CA_BUNDLE file (if set) or the certifi bundle, or set "
                          "TIDB_SSL_CA to the CA file from the TiDB Cloud Connect dialog.")
    if code in (2003, 2006, 2013):
        return ns.Failure(ns.NETWORK_ERROR, f"could not connect to {where} ({code}): {detail}",
                          "Check TIDB_HOST and TIDB_PORT and the internet connection; some networks block port 4000.")
    if "insecure transport" in text:
        return ns.Failure(ns.BAD_REQUEST, f"TiDB requires TLS: {detail}", "Set TIDB_SSL=true.")
    if "prefix" in text:
        return ns.Failure(ns.INVALID_KEY, f"TiDB rejected the user name: {detail}",
                          "TiDB Cloud user names include the cluster prefix, e.g. xxxxxxxx.root (Connect dialog).")
    return ns.classify_exception(exc)


@_scrubbed
def check_tidb(settings: Settings, *, connect: Callable[..., Any] | None = None) -> CheckResult:
    """Row ``tidb``: PyMySQL sign-in with the app's TLS arguments and ``SELECT VERSION()``.
    ``connect`` replaces ``pymysql.connect`` (tests)."""
    if settings.database_url:
        return CheckResult("tidb", ns.NOT_CONFIGURED,
                           "TACTIDOSE_DATABASE_URL is set, so the app uses that database and ignores TIDB_*; "
                           "python -m tactidose doctor checks it")
    if not settings.tidb_host:
        return CheckResult("tidb", ns.NOT_CONFIGURED,
                           "set TIDB_HOST, TIDB_USER and TIDB_PASSWORD in .env (TiDB Cloud: Connect dialog) to use "
                           "TiDB instead of the local SQLite database", info={"set": ["TIDB_HOST", "TIDB_USER", "TIDB_PASSWORD"]})
    from tactidose.db.session import tidb_connect_args
    from tactidose.integrations.tidb import describe, is_tidb_version

    where = f"{settings.tidb_host}:{settings.tidb_port}"
    password = settings.tidb_password.get_secret_value() if settings.tidb_password else ""
    db_info = describe(settings)
    info: dict[str, Any] = {"host": settings.tidb_host, "port": settings.tidb_port, "database": settings.tidb_database,
                            "user": settings.tidb_user, "password": ns.redact(password), "tls": db_info.get("tls"),
                            "ca_source": db_info.get("ca_source")}
    if not settings.tidb_user:
        return CheckResult("tidb", ns.INVALID_KEY, "TIDB_USER is not set",
                           "Copy TIDB_USER (it looks like xxxxxxxx.root) from the TiDB Cloud Connect dialog.", info=info)
    if connect is None:
        try:
            import pymysql
        except ImportError:
            return CheckResult("tidb", ns.ERROR, "PyMySQL is not installed", "pip install 'tactidose[tidb]'", info=info)
        connect = pymysql.connect
    kwargs: dict[str, Any] = {"host": settings.tidb_host, "port": settings.tidb_port, "user": settings.tidb_user,
                              "password": password, "database": settings.tidb_database, "charset": "utf8mb4",
                              **tidb_connect_args(settings)}
    started = time.monotonic()
    conn = cur = None
    try:
        conn = connect(**kwargs)
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - driver errors are results here
        return CheckResult("tidb", **_failure_fields(_tidb_failure(exc, settings)), elapsed_s=_since(started),
                           info=info)
    finally:
        for obj in (cur, conn):
            if obj is not None:
                _quiet_close(obj)
    elapsed = _since(started)
    version = str(row[0]) if row else ""
    info["version"] = version
    kind = "TiDB" if is_tidb_version(version) else "a MySQL server (not TiDB)"
    tls = f"TLS on (CA: {db_info.get('ca_source')})" if settings.tidb_ssl else "TLS off"
    return CheckResult("tidb", ns.OK, f"connected to {where}/{settings.tidb_database}: {kind} {version}, {tls}, "
                                      f"in {elapsed:.1f} s", elapsed_s=elapsed, info=info)


def _failure_fields(failure: ns.Failure) -> dict[str, str]:
    return {"status": failure.code, "detail": failure.message, "hint": failure.hint}


# =========================================================================== SMTP


@_scrubbed
def check_smtp(settings: Settings) -> CheckResult:
    """Row ``smtp``: connect, EHLO, STARTTLS (or implicit TLS), sign in, QUIT. Nothing is sent."""
    if not settings.smtp_configured:
        return CheckResult("smtp", ns.NOT_CONFIGURED,
                           "set SMTP_HOST, SMTP_USER, SMTP_PASSWORD (Gmail: an app password) and SMTP_FROM in .env; "
                           f"until then report emails are saved as .eml files in {settings.outbox_dir}",
                           info={"set": ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"]})
    host, port = str(settings.smtp_host), int(settings.smtp_port)
    timeout = min(TIMEOUT_S, float(settings.smtp_timeout_s))
    user = settings.smtp_user or ""
    password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
    mode = "SSL" if settings.smtp_ssl else ("STARTTLS" if settings.smtp_starttls else "no TLS")
    info: dict[str, Any] = {"host": host, "port": port, "tls": mode, "user": user or None,
                            "password": ns.redact(password)}
    if user and not password:
        return CheckResult("smtp", ns.INVALID_KEY, "SMTP_USER is set but SMTP_PASSWORD is empty", _HINT_GMAIL, info=info)
    sign_in = bool(user) and mode != "no TLS"     # never send a password over a plain connection
    started = time.monotonic()
    smtp: Any = None
    try:
        if settings.smtp_ssl:
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
            smtp.ehlo()
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
            smtp.ehlo()
            if settings.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        if sign_in:
            smtp.login(user, password)
    except smtplib.SMTPAuthenticationError as exc:
        return CheckResult("smtp", ns.INVALID_KEY,
                           f"{host} rejected the sign-in for {user} ({exc.smtp_code}); SMTP_PASSWORD is "
                           f"{ns.redact(password)}", _HINT_GMAIL, _since(started), info)
    except smtplib.SMTPNotSupportedError as exc:     # no STARTTLS, or no AUTH (often: only after STARTTLS)
        return CheckResult("smtp", ns.BAD_REQUEST, f"{host}:{port} ({mode}): {_short(exc)}", _HINT_SMTP_TLS,
                           _since(started), info)
    except Exception as exc:  # noqa: BLE001 - network, TLS and protocol errors are results here
        failure = ns.classify_exception(exc)
        if isinstance(exc, ssl.SSLError) and "wrong_version_number" in str(exc).lower():
            failure = ns.Failure(ns.BAD_REQUEST, f"TLS mode does not match port {port} ({mode})", _HINT_SMTP_TLS)
        elif failure.code in (ns.TIMEOUT, ns.NETWORK_ERROR):
            failure = replace(failure, hint=_HINT_SMTP_PORTS)
        return CheckResult("smtp", failure.code, f"{host}:{port} ({mode}): {failure.message}", failure.hint,
                           _since(started), info)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 - the result is already decided
                _quiet_close(smtp)
    elapsed = _since(started)
    if user and not sign_in:
        return CheckResult("smtp", WARN, f"connected to {host}:{port} but did not sign in: TLS is off, so the password "
                                         "would travel unencrypted; no email was sent",
                           "Set SMTP_STARTTLS=true (port 587) or SMTP_SSL=true (port 465).", elapsed, info)
    detail = (f"signed in to {host}:{port}; no email was sent ({mode}, as {user}, {elapsed:.1f} s)" if user else
              f"connected to {host}:{port}; no email was sent ({mode}; no sign-in: SMTP_USER is not set)")
    return CheckResult("smtp", ns.OK, detail, "python -m tactidose send-test-email --to you@example.com sends a real one.",
                       elapsed, info)


# =========================================================================== runner / report


def _run_one(name: str, settings: Settings, secrets: Sequence[str]) -> list[CheckResult]:
    started = time.monotonic()
    try:
        out = globals()[f"check_{name}"](settings)     # looked up per call: tests may replace a check
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
        log.debug("check %s crashed: %s", name, _mask("".join(traceback.format_exception(exc)), secrets))
        return [CheckResult(name, ns.ERROR, f"the check itself failed: {type(exc).__name__}: {_short(exc)}",
                            "Run with -v to see the details.", _since(started))]
    return list(out) if isinstance(out, list) else [out]


def run_checks(settings: Settings, only: set[str] | None = None) -> list[CheckResult]:
    """Run the selected checks (all of :data:`SERVICES` by default) side by side; rows come back in
    :data:`SERVICES` order. Never raises for a failing or crashing check."""
    unknown = set(only or ()) - set(SERVICES)
    if unknown:
        raise ValueError(f"unknown service(s): {', '.join(sorted(unknown))}; choose from {', '.join(SERVICES)}")
    names = [n for n in SERVICES if only is None or n in only]
    secrets = _secret_values(settings)
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="check-apis") as pool:
        futures = [pool.submit(_run_one, name, settings, secrets) for name in names]
        results = [row for fut in futures for row in fut.result()]
    return [_scrub(r, secrets) for r in results]


def _wrapped(text: str, width: int | None, indent: int, *, hanging: int = 0) -> list[str]:
    if width is None:
        return [text]
    return textwrap.wrap(text, width=max(30, width - indent), subsequent_indent=" " * hanging,
                         break_long_words=False, break_on_hyphens=False) or [""]


def format_report(results: Sequence[CheckResult], env_file: Path, *, width: int | None = None) -> str:
    """The ``check-apis`` table: the settings file, one row per result (``-> hint`` underneath) and a
    summary. ``width`` wraps long lines for a terminal (``None`` = no wrapping)."""
    path = Path(env_file).resolve()
    found = path.is_file()
    lines = [f"Settings file: {path} ({'found' if found else 'not found'})"]
    if not found:
        lines.append("  -> copy .env.example to .env here and add the keys, or run the command from the folder "
                     "that has your .env")
    lines.append("")
    if not results:
        lines.append("No services were selected.")
        return "\n".join(lines)
    svc_w = max(len("SERVICE"), *(len(r.service) for r in results)) + 2
    st_w = max(len("STATUS"), *(len(r.status) for r in results)) + 2
    pad = " " * (svc_w + st_w)
    lines.append(f"{'SERVICE':<{svc_w}}{'STATUS':<{st_w}}DETAIL")
    for r in results:
        detail = _wrapped(r.detail, width, svc_w + st_w)
        lines.append(f"{r.service:<{svc_w}}{r.status:<{st_w}}{detail[0]}".rstrip())
        lines.extend(pad + line for line in detail[1:])
        if r.hint:
            lines.extend(pad + line for line in _wrapped(f"-> {r.hint}", width, svc_w + st_w, hanging=3))
    counts = {"OK": 0, "WARN": 0, "failed": 0, "not configured": 0}
    for r in results:
        key = "not configured" if r.status == ns.NOT_CONFIGURED else (r.status if r.status in (ns.OK, WARN) else "failed")
        counts[key] += 1
    lines += ["", "Summary: " + ", ".join(f"{n} {label}" for label, n in counts.items() if n) + "."]
    if counts["failed"]:
        lines.append("The app still runs: a failed service falls back to its offline behaviour. Fix the hints and "
                     "run the check again.")
    elif counts["not configured"] == len(results):
        lines.append(f"Nothing was contacted. Put the keys in {path} and run the check again.")
    return "\n".join(lines)
