"""tactidose/integrations/live_check.py and ``python -m tactidose check-apis``.

No live calls: HTTP goes through ``httpx.MockTransport``, the Gemini SDK client, PyMySQL, smtplib and
the Snowflake connector are fakes. An autouse fixture turns any accidental real call into a hard
failure (:class:`LiveCall` is a ``BaseException``, so no ``except Exception`` can swallow it).
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pymysql
import pytest
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr

import tactidose.__main__ as cli
from tactidose.config import Settings
from tactidose.integrations import live_check as lc
from tactidose.integrations import netsafe as ns
from tactidose.integrations import snowflake as sf

GEMINI_KEY = "fake-gemini-testkey-0123456789abcdef3kQ"          # 39 characters, ends ...3kQ
ELEVEN_KEY = "sk_fake_eleven_key_0123456789abcdefXYZ"
SF_TOKEN = "snowflake-fake-pat-0123456789abcdef"
TIDB_PASSWORD = "tidb-fake-password-0123"
SMTP_PASSWORD = "fake-gmail-app-pass"
SECRETS = (GEMINI_KEY, ELEVEN_KEY, SF_TOKEN, TIDB_PASSWORD, SMTP_PASSWORD)
OLD_VOICE = "JBFqnCBsd6RMkjVDRZzb"
SF_ROW = ("9.30.1", "COMPUTE_WH", "TACTIDOSE", "ANALYTICS", "ACCOUNTADMIN")


class LiveCall(BaseException):
    """A check tried to reach a real service."""


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise LiveCall(f"unexpected live call: {args!r:.120}")

    monkeypatch.setattr(lc, "_new_client", lambda: httpx.Client(transport=httpx.MockTransport(refuse)))
    monkeypatch.setattr(lc, "_new_genai_client", refuse)
    monkeypatch.setattr(sf, "_default_connect", refuse)
    monkeypatch.setattr(pymysql, "connect", refuse)
    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)


def make(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "data_dir": tmp_path / "data", "hardware_mode": "none",
                              "voice_enabled": False, "tts_provider": "none", "agent_provider": "auto",
                              "label_extractor": "auto", "report_ai_summary": True}
    values.update(overrides)
    return Settings(**values)


def gemini(tmp_path: Path, **overrides: Any) -> Settings:
    return make(tmp_path, gemini_api_key=SecretStr(GEMINI_KEY), **overrides)


def eleven(tmp_path: Path, **overrides: Any) -> Settings:
    return make(tmp_path, tts_provider="elevenlabs", elevenlabs_api_key=SecretStr(ELEVEN_KEY), **overrides)


def snow(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"snowflake_account": "myorg-myacct", "snowflake_user": "TACTI",
                              "snowflake_token": SecretStr(SF_TOKEN), "snowflake_warehouse": "COMPUTE_WH",
                              "analytics_salt": SecretStr("a-long-random-salt-4f9c2b")}
    values.update(overrides)
    return make(tmp_path, **values)


def tidb(tmp_path: Path, **overrides: Any) -> Settings:
    return make(tmp_path, tidb_host="gateway01.us-west-2.prod.aws.tidbcloud.com", tidb_user="2abc3def.root",
                tidb_password=SecretStr(TIDB_PASSWORD), **overrides)


def smtp(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"smtp_host": "smtp.gmail.com", "smtp_user": "tactidose.demo@gmail.com",
                              "smtp_password": SecretStr(SMTP_PASSWORD), "smtp_from": "tactidose.demo@gmail.com"}
    values.update(overrides)
    return make(tmp_path, **values)


def assert_no_secrets(*results: lc.CheckResult) -> None:
    text = json.dumps([r.to_dict() for r in results]) + lc.format_report(list(results), Path("missing.env"))
    for secret in SECRETS:
        assert secret not in text


class Recorder:
    """MockTransport handler that records every request."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def client(self, *, follow_redirects: bool = False) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=follow_redirects)


GEMINI_OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}


def gemini_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=GEMINI_OK)


def text_reply(text: str = "OK") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]), finish_reason="STOP")])


def call_reply() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name="get_patient_status", args={}))]))])


class FakeGenai:
    """Stands in for ``genai.Client``: ``models.generate_content`` replays ``outcomes``."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes) or [text_reply()]
        self.calls: list[dict[str, Any]] = []
        self.models = self
        self.closed = False

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


# =========================================================================== Gemini


def test_gemini_not_configured_lists_what_a_key_turns_on(tmp_path) -> None:
    (row,) = lc.check_gemini(make(tmp_path))
    assert row.status == ns.NOT_CONFIGURED and not row.failed and "GEMINI_API_KEY" in row.detail
    assert "assistant, report summaries, label scanning" in row.detail
    assert row.info["turn_off"]["assistant"] == "TACTIDOSE_AGENT_PROVIDER=rules"
    (row,) = lc.check_gemini(make(tmp_path, agent_provider="rules", label_extractor="disabled"))
    assert row.detail.endswith("turn on: report summaries")


def test_gemini_probe_and_agent_request_have_the_documented_shape(tmp_path) -> None:
    rec, sdk = Recorder(gemini_ok), FakeGenai(text_reply())
    rows = lc.check_gemini(gemini(tmp_path), http=rec.client(), sdk_client=sdk)
    assert [(r.service, r.status) for r in rows] == [("gemini", ns.OK), ("gemini-agent", ns.OK)]
    (req,) = rec.requests
    assert req.method == "POST" and req.url.host == "generativelanguage.googleapis.com"
    assert req.url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert req.headers["x-goog-api-key"] == GEMINI_KEY and GEMINI_KEY not in str(req.url)
    assert json.loads(req.content) == {"contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}]}
    assert "gemini-3.8-flash answered in" in rows[0].detail and rows[0].info["key"].endswith("...3kQ)")
    (call,) = sdk.calls
    assert call["model"] == "gemini-3.8-flash" and call["contents"] == "Say OK."
    config = call["config"]
    (decl,) = config.tools[0].function_declarations
    assert decl.name == "get_patient_status" and decl.description == "Read the patient's containers and cooldown."
    assert decl.parameters_json_schema == {"type": "object", "properties": {}}
    assert config.automatic_function_calling.disable is True and config.thinking_config is None
    agent = rows[1]
    assert "Gemini is on for: assistant, report summaries, label scanning" in agent.detail
    for switch in ("TACTIDOSE_AGENT_PROVIDER=rules", "TACTIDOSE_REPORT_AI_SUMMARY=false",
                   "TACTIDOSE_LABEL_EXTRACTOR=disabled"):
        assert switch in agent.hint
    assert not sdk.closed                         # an injected client belongs to the caller
    assert_no_secrets(*rows)


def test_gemini_agent_uses_the_thinking_level_and_reports_partial_features(tmp_path) -> None:
    sdk = FakeGenai(call_reply())
    rows = lc.check_gemini(gemini(tmp_path, agent_thinking_level="low", label_extractor="disabled"),
                           http=Recorder(gemini_ok).client(), sdk_client=sdk)
    assert rows[1].status == ns.OK and "asked to call get_patient_status" in rows[1].detail
    assert sdk.calls[0]["config"].thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert "thinking low" in rows[1].detail and "(off: label scanning)" in rows[1].detail
    assert "TACTIDOSE_LABEL_EXTRACTOR" not in rows[1].hint


def test_gemini_agent_errors_are_classified(tmp_path) -> None:
    bad = genai_errors.ClientError(400, {"error": {"message": "thinking_level is not supported", "status": "INVALID_ARGUMENT"}})
    rows = lc.check_gemini(gemini(tmp_path, agent_thinking_level="high"), http=Recorder(gemini_ok).client(),
                           sdk_client=FakeGenai(bad))
    assert rows[1].status == ns.BAD_REQUEST and rows[1].failed and "TACTIDOSE_AGENT_THINKING_LEVEL" in rows[1].hint
    blocked = genai_errors.APIError(307, {"message": "", "status": "Temporary Redirect"})
    rows = lc.check_gemini(gemini(tmp_path), http=Recorder(gemini_ok).client(), sdk_client=FakeGenai(blocked))
    assert rows[1].status == ns.BLOCKED
    empty = types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="SAFETY")])
    rows = lc.check_gemini(gemini(tmp_path), http=Recorder(gemini_ok).client(), sdk_client=FakeGenai(empty))
    assert rows[1].status == lc.WARN and "SAFETY" in rows[1].detail


@pytest.mark.parametrize("follow", [False, True])
def test_gemini_redirect_is_blocked_and_never_followed(tmp_path, follow) -> None:
    rec = Recorder(lambda r: httpx.Response(307, headers={"location": "https://login.corp.example/sso?user=alex"}))
    sdk = FakeGenai()
    rows = lc.check_gemini(gemini(tmp_path), http=rec.client(follow_redirects=follow), sdk_client=sdk)
    (row,) = rows                                  # no gemini-agent row after a block
    assert row.status == ns.BLOCKED and row.failed and "login.corp.example" in row.detail and "alex" not in row.detail
    assert "not followed" in row.detail and row.hint
    assert len(rec.requests) == 1 and rec.requests[0].url.host == "generativelanguage.googleapis.com"
    assert sdk.calls == []


@pytest.mark.parametrize("respond,status", [
    (lambda r: httpx.Response(400, json={"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                   "status": "INVALID_ARGUMENT"}}), ns.INVALID_KEY),
    (lambda r: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}), ns.QUOTA),
    (lambda r: httpx.Response(503), ns.SERVER_ERROR),
    (lambda r: httpx.Response(200, text="<html>Blocked by policy</html>", headers={"content-type": "text/html"}), ns.BLOCKED),
])
def test_gemini_failures(tmp_path, respond, status) -> None:
    (row,) = lc.check_gemini(gemini(tmp_path), http=Recorder(respond).client(), sdk_client=FakeGenai())
    assert row.status == status and row.failed
    if status == ns.INVALID_KEY:
        assert "GEMINI_API_KEY is set (39 chars, ends ...3kQ)" in row.detail
    assert_no_secrets(row)


@pytest.mark.parametrize("exc,status", [
    (httpx.ReadTimeout("slow"), ns.TIMEOUT),
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), ns.TLS_ERROR),
    (httpx.ConnectError("getaddrinfo failed"), ns.NETWORK_ERROR),
])
def test_gemini_transport_errors(tmp_path, exc, status) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise exc

    (row,) = lc.check_gemini(gemini(tmp_path), http=Recorder(fail).client(), sdk_client=FakeGenai())
    assert row.status == status and row.hint


def test_gemini_missing_model_with_a_working_fallback_is_a_warning(tmp_path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "gemini-3.8-flash" in request.url.path:
            return httpx.Response(404, json={"error": {"message": "models/gemini-3.8-flash is not found", "status": "NOT_FOUND"}})
        return gemini_ok(request)

    rec, sdk = Recorder(respond), FakeGenai()
    rows = lc.check_gemini(gemini(tmp_path), http=rec.client(), sdk_client=sdk)
    assert rows[0].status == lc.WARN and not rows[0].failed
    assert rows[0].detail == ("GEMINI_MODEL gemini-3.8-flash was not found; gemini-flash-latest works - "
                              "set GEMINI_MODEL=gemini-flash-latest")
    assert [r.url.path.split("/")[-1] for r in rec.requests] == ["gemini-3.8-flash:generateContent",
                                                                 "gemini-flash-latest:generateContent"]
    assert rows[1].status == ns.OK and sdk.calls[0]["model"] == "gemini-flash-latest"


def test_gemini_agent_falls_back_like_the_assistant(tmp_path) -> None:
    missing = genai_errors.ClientError(404, {"error": {"message": "not found", "status": "NOT_FOUND"}})
    sdk = FakeGenai(missing, text_reply())
    rows = lc.check_gemini(gemini(tmp_path, agent_model="gemini-9-pro"), http=Recorder(gemini_ok).client(), sdk_client=sdk)
    assert [c["model"] for c in sdk.calls] == ["gemini-9-pro", "gemini-flash-latest"]
    assert rows[1].status == lc.WARN and "TACTIDOSE_AGENT_MODEL=gemini-flash-latest" in rows[1].hint


# =========================================================================== ElevenLabs


def audio(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"\x00\x01" * 400, headers={"content-type": "audio/pcm"})


VOICES = {"voices": [
    {"voice_id": "cloned123", "name": "My Clone", "category": "cloned"},
    {"voice_id": OLD_VOICE, "name": "George", "category": "premade"},
    {"voice_id": "legacy99", "name": "Old Rachel", "category": "premade", "is_legacy": True},
    {"voice_id": "premade42", "name": "Sarah", "category": "premade"},
]}


def voice_missing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": {"status": "voice_not_found",
                                                "message": f"A voice with voice_id {OLD_VOICE} was not found."}})


def test_elevenlabs_not_configured_or_not_used(tmp_path) -> None:
    row = lc.check_elevenlabs(make(tmp_path, tts_provider="elevenlabs"))
    assert row.status == ns.NOT_CONFIGURED and "ELEVENLABS_API_KEY" in row.detail
    row = lc.check_elevenlabs(make(tmp_path, tts_provider="offline", elevenlabs_api_key=SecretStr(ELEVEN_KEY)))
    assert row.status == ns.NOT_CONFIGURED and "TACTIDOSE_TTS_PROVIDER=elevenlabs" in row.detail
    assert_no_secrets(row)


def test_elevenlabs_ok_request_shape(tmp_path) -> None:
    rec = Recorder(audio)
    row = lc.check_elevenlabs(eleven(tmp_path), http=rec.client())
    assert row.status == ns.OK and row.info["bytes"] == 800 and row.info["voice_id"] == OLD_VOICE
    (req,) = rec.requests
    assert req.method == "POST" and req.url.host == "api.elevenlabs.io"
    assert req.url.path == f"/v1/text-to-speech/{OLD_VOICE}" and req.url.params["output_format"] == "pcm_22050"
    assert req.headers["xi-api-key"] == ELEVEN_KEY and ELEVEN_KEY not in str(req.url)
    assert json.loads(req.content) == {"text": "TactiDose voice test.", "model_id": "eleven_flash_v2_5"}
    assert_no_secrets(row)


def test_elevenlabs_unavailable_voice_switches_to_the_first_premade_voice(tmp_path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=VOICES)
        return voice_missing(request) if OLD_VOICE in request.url.path else audio(request)

    rec = Recorder(respond)
    row = lc.check_elevenlabs(eleven(tmp_path), http=rec.client())
    assert row.status == lc.WARN and not row.failed
    assert row.detail == (f"voice {OLD_VOICE} is not available on this account; Sarah (premade42) works - the app "
                          "switches to it automatically; set ELEVENLABS_VOICE_ID=premade42 to keep it")
    assert [(r.method, r.url.path) for r in rec.requests] == [
        ("POST", f"/v1/text-to-speech/{OLD_VOICE}"), ("GET", "/v1/voices"), ("POST", "/v1/text-to-speech/premade42")]
    assert rec.requests[1].headers["xi-api-key"] == ELEVEN_KEY
    assert_no_secrets(row)


def test_elevenlabs_voice_list_blocked(tmp_path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(307, headers={"location": "https://filter.corp.example/block?id=7"})
        return voice_missing(request)

    rec = Recorder(respond)
    row = lc.check_elevenlabs(eleven(tmp_path), http=rec.client(follow_redirects=True))
    assert row.status == ns.NOT_FOUND and row.failed
    assert "voice list could not be read (BLOCKED_BY_NETWORK" in row.detail and "filter.corp.example" in row.detail
    assert "ELEVENLABS_VOICE_ID" in row.hint
    assert len(rec.requests) == 2 and {r.url.host for r in rec.requests} == {"api.elevenlabs.io"}


def test_elevenlabs_auto_voice_off_does_not_look_for_another_voice(tmp_path) -> None:
    rec = Recorder(voice_missing)
    row = lc.check_elevenlabs(eleven(tmp_path, elevenlabs_auto_voice=False), http=rec.client())
    assert row.status == ns.NOT_FOUND and "TACTIDOSE_ELEVENLABS_AUTO_VOICE=true" in row.hint and len(rec.requests) == 1


@pytest.mark.parametrize("status,body,code", [
    (401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}, ns.INVALID_KEY),
    (401, {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}, ns.QUOTA),
    (401, {"detail": {"status": "detected_unusual_activity", "message": "Unusual activity detected."}}, ns.PERMISSION),
    (429, {"detail": {"status": "too_many_concurrent_requests"}}, ns.QUOTA),
])
def test_elevenlabs_account_problems(tmp_path, status, body, code) -> None:
    rec = Recorder(lambda r: httpx.Response(status, json=body))
    row = lc.check_elevenlabs(eleven(tmp_path), http=rec.client())
    assert row.status == code and len(rec.requests) == 1
    if code == ns.INVALID_KEY:
        assert "ELEVENLABS_API_KEY is set (38 chars, ends ...XYZ)" in row.detail
    assert_no_secrets(row)


def test_pick_voice_order() -> None:
    assert lc.pick_voice(VOICES["voices"], exclude=OLD_VOICE) == ("premade42", "Sarah")
    assert lc.pick_voice([{"voice_id": "a", "category": "cloned"}, {"voice_id": "b", "name": "B", "category": "default"}]) == ("b", "B")
    assert lc.pick_voice([{"voice_id": "a", "name": "A", "category": "cloned"}]) == ("a", "A")
    assert lc.pick_voice([{"voice_id": OLD_VOICE, "category": "premade"}, "junk"], exclude=OLD_VOICE) is None


# =========================================================================== Snowflake


class FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None, log: list[Any]) -> None:
        self.row, self.log = row, log

    def execute(self, sql: str) -> None:
        self.log.append(sql)

    def fetchone(self) -> Any:
        return self.row

    def close(self) -> None:
        self.log.append("cursor closed")


class FakeConnection:
    def __init__(self, row: Any) -> None:
        self.row, self.log = row, []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.row, self.log)

    def close(self) -> None:
        self.log.append("connection closed")


class FakeConnect:
    """``connect(**kwargs)`` replacement: records the kwargs, returns a connection or raises."""

    def __init__(self, result: Any) -> None:
        self.result, self.calls = result, []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def preflight(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": None, "success": False, "message": "Missing user name"})


def test_snowflake_not_configured_names_the_missing_keys(tmp_path) -> None:
    row = lc.check_snowflake(make(tmp_path, snowflake_account="myorg-myacct"))
    assert row.status == ns.NOT_CONFIGURED and "SNOWFLAKE_USER and SNOWFLAKE_TOKEN" in row.detail
    assert "SNOWFLAKE_ACCOUNT" not in row.detail


def test_snowflake_ok_uses_the_sync_settings_and_sends_no_credentials_first(tmp_path) -> None:
    settings = snow(tmp_path)
    rec, conn = Recorder(preflight), FakeConnection(SF_ROW)
    connect = FakeConnect(conn)
    row = lc.check_snowflake(settings, connect=connect, http=rec.client())
    assert row.status == ns.OK and not row.hint, row
    assert "signed in to myorg-myacct as TACTI (role ACCOUNTADMIN, warehouse COMPUTE_WH, Snowflake 9.30.1)" in row.detail
    (req,) = rec.requests
    assert req.method == "POST" and req.url.host == "myorg-myacct.snowflakecomputing.com"
    assert req.url.path == "/session/v1/login-request" and json.loads(req.content) == {}
    assert SF_TOKEN not in req.content.decode() + str(req.headers)
    (kwargs,) = connect.calls
    expected = {**sf.connection_kwargs(settings), "login_timeout": 15, "network_timeout": 15}
    assert kwargs == expected and kwargs["token"] == SF_TOKEN and kwargs["authenticator"] == "PROGRAMMATIC_ACCESS_TOKEN"
    assert conn.log == [lc.SNOWFLAKE_QUERY, "cursor closed", "connection closed"]
    assert_no_secrets(row)


def test_snowflake_warns_without_a_warehouse_and_with_the_default_salt(tmp_path) -> None:
    settings = snow(tmp_path, snowflake_warehouse=None, analytics_salt=SecretStr("change-me-to-a-random-string"))
    row = lc.check_snowflake(settings, connect=FakeConnect(FakeConnection(("9.30.1", None, None, None, "PUBLIC"))),
                             http=Recorder(preflight).client())
    assert row.status == lc.WARN and not row.failed
    assert "SNOWFLAKE_WAREHOUSE" in row.hint and "TACTIDOSE_ANALYTICS_SALT" in row.hint
    assert "database TACTIDOSE does not exist yet" in row.detail and "change-me" not in row.hint


def test_snowflake_preflight_redirect_stops_before_any_credential(tmp_path) -> None:
    rec = Recorder(lambda r: httpx.Response(307, headers={"location": "https://login.corp.example/"}))
    connect = FakeConnect(FakeConnection(SF_ROW))
    row = lc.check_snowflake(snow(tmp_path), connect=connect, http=rec.client(follow_redirects=True))
    assert row.status == ns.BLOCKED and "no credentials were sent" in row.detail
    assert connect.calls == [] and len(rec.requests) == 1


@pytest.mark.parametrize("message,status", [
    (("250001 (08001): Failed to connect to DB: myorg-myacct.snowflakecomputing.com:443. Incorrect username or "
      "password was specified."), ns.INVALID_KEY),
    (("250001 (08001): Failed to connect to DB: myorg-myacct.snowflakecomputing.com:443. Programmatic access token "
      "is invalid."), ns.INVALID_KEY),
    (("250001 (08001): Failed to connect to DB: myorg-myacct.snowflakecomputing.com:443. Incoming request with "
      "IP/Token 203.0.113.7 is not allowed to access Snowflake. Contact your account administrator."), ns.PERMISSION),
    ("250001 (08001): Failed to authenticate: MFA with TOTP is required.", ns.PERMISSION),
    ("250001 (08001): Failed to connect to DB. Verify the account name is correct: wrong.snowflakecomputing.com:443.",
     ns.NOT_FOUND),
])
def test_snowflake_sign_in_errors(tmp_path, message, status) -> None:
    row = lc.check_snowflake(snow(tmp_path), connect=FakeConnect(Exception(message)), http=Recorder(preflight).client())
    assert row.status == status and row.failed and row.hint
    assert_no_secrets(row)


def test_snowflake_rejects_a_url_as_the_account(tmp_path) -> None:
    row = lc.check_snowflake(snow(tmp_path, snowflake_account="https://myorg-myacct.snowflakecomputing.com"))
    assert row.status == ns.BAD_REQUEST and "myorg-myaccount" in row.hint


# =========================================================================== TiDB


def test_tidb_not_configured(tmp_path) -> None:
    row = lc.check_tidb(make(tmp_path))
    assert row.status == ns.NOT_CONFIGURED and "TIDB_HOST" in row.detail
    row = lc.check_tidb(tidb(tmp_path, database_url="sqlite:///x.db"))
    assert row.status == ns.NOT_CONFIGURED and "TACTIDOSE_DATABASE_URL" in row.detail


def test_tidb_ok_uses_the_app_tls_arguments(tmp_path) -> None:
    from tactidose.db.session import tidb_connect_args

    settings = tidb(tmp_path)
    conn = FakeConnection(("8.0.11-TiDB-v7.5.2-serverless",))
    connect = FakeConnect(conn)
    row = lc.check_tidb(settings, connect=connect)
    assert row.status == ns.OK and "TiDB 8.0.11-TiDB-v7.5.2-serverless" in row.detail and "TLS on" in row.detail
    (kwargs,) = connect.calls
    assert kwargs == {"host": settings.tidb_host, "port": 4000, "user": "2abc3def.root", "password": TIDB_PASSWORD,
                      "database": "tactidose", "charset": "utf8mb4", **tidb_connect_args(settings)}
    assert kwargs["ssl_verify_cert"] is True and kwargs["connect_timeout"] <= 15
    assert conn.log == ["SELECT VERSION()", "cursor closed", "connection closed"]
    assert_no_secrets(row)


def test_tidb_uses_pymysql_connect_by_default(tmp_path, monkeypatch) -> None:
    connect = FakeConnect(FakeConnection(("8.0.11-TiDB-v7.5.2",)))
    monkeypatch.setattr(pymysql, "connect", connect)
    assert lc.check_tidb(tidb(tmp_path)).status == ns.OK and len(connect.calls) == 1


@pytest.mark.parametrize("exc,status,hint", [
    (pymysql.err.OperationalError(1045, "Access denied for user '2abc3def.root'@'1.2.3.4' (using password: YES)"),
     ns.INVALID_KEY, "Connect dialog"),
    (pymysql.err.OperationalError(1049, "Unknown database 'tactidose'"), ns.NOT_FOUND, "python -m tactidose init-db"),
    (pymysql.err.OperationalError(2003, "Can't connect to MySQL server on 'gateway01' (timed out)"),
     ns.NETWORK_ERROR, "port 4000"),
    (pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query"), ns.NETWORK_ERROR, "TIDB_HOST"),
    (pymysql.err.OperationalError(2003, "Can't connect to MySQL server on 'gateway01' ([SSL: CERTIFICATE_VERIFY_FAILED] "
                                        "certificate verify failed: self-signed certificate in certificate chain)"),
     ns.TLS_ERROR, "TIDB_SSL_CA"),
])
def test_tidb_errors(tmp_path, exc, status, hint) -> None:
    row = lc.check_tidb(tidb(tmp_path), connect=FakeConnect(exc))
    assert row.status == status and row.failed and hint in row.hint
    assert_no_secrets(row)


# =========================================================================== SMTP


def fake_smtp(monkeypatch: pytest.MonkeyPatch, *, login_error: BaseException | None = None,
              connect_error: BaseException | None = None) -> list[Any]:
    made: list[Any] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float | None = None, context: Any = None) -> None:
            if connect_error is not None:
                raise connect_error
            self.host, self.port, self.timeout, self.context = host, port, timeout, context
            self.calls: list[Any] = []
            self.ssl = False
            made.append(self)

        def ehlo(self) -> None:
            self.calls.append("ehlo")

        def starttls(self, context: Any = None) -> None:
            assert isinstance(context, ssl.SSLContext)
            self.calls.append("starttls")

        def login(self, user: str, password: str) -> None:
            self.calls.append(("login", user, password == SMTP_PASSWORD))
            if login_error is not None:
                raise login_error

        def quit(self) -> None:
            self.calls.append("quit")

        def close(self) -> None:
            self.calls.append("close")

        def sendmail(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("check-apis must not send email")

        send_message = sendmail

    class FakeSMTPSSL(FakeSMTP):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.ssl = True

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return made


def test_smtp_not_configured(tmp_path) -> None:
    row = lc.check_smtp(make(tmp_path))
    assert row.status == ns.NOT_CONFIGURED and "SMTP_HOST" in row.detail and "outbox" in row.detail


def test_smtp_signs_in_and_sends_nothing(tmp_path, monkeypatch) -> None:
    made = fake_smtp(monkeypatch)
    row = lc.check_smtp(smtp(tmp_path))
    assert row.status == ns.OK and row.detail.startswith("signed in to smtp.gmail.com:587; no email was sent")
    assert "send-test-email --to" in row.hint
    (conn,) = made
    assert conn.timeout <= 15 and conn.calls == ["ehlo", "starttls", "ehlo", ("login", "tactidose.demo@gmail.com", True), "quit"]
    assert_no_secrets(row)


def test_smtp_ssl_mode_and_no_tls(tmp_path, monkeypatch) -> None:
    made = fake_smtp(monkeypatch)
    assert lc.check_smtp(smtp(tmp_path, smtp_ssl=True, smtp_port=465)).status == ns.OK
    assert made[-1].ssl and made[-1].calls[0] == "ehlo" and "starttls" not in made[-1].calls
    row = lc.check_smtp(smtp(tmp_path, smtp_starttls=False))
    assert row.status == lc.WARN and "did not sign in" in row.detail
    assert not any(isinstance(c, tuple) for c in made[-1].calls)       # no password over plain text


def test_smtp_authentication_failure(tmp_path, monkeypatch) -> None:
    made = fake_smtp(monkeypatch, login_error=smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Username and Password not accepted."))
    row = lc.check_smtp(smtp(tmp_path))
    assert row.status == ns.INVALID_KEY and row.failed and "535" in row.detail
    assert "app password" in row.hint and "myaccount.google.com/apppasswords" in row.hint
    assert made[0].calls[-1] == "quit"
    assert_no_secrets(row)


def test_smtp_blocked_port_and_missing_password(tmp_path, monkeypatch) -> None:
    fake_smtp(monkeypatch, connect_error=TimeoutError("timed out"))
    row = lc.check_smtp(smtp(tmp_path))
    assert row.status == ns.TIMEOUT and "587" in row.hint
    row = lc.check_smtp(smtp(tmp_path, smtp_password=None))
    assert row.status == ns.INVALID_KEY and "SMTP_PASSWORD is empty" in row.detail


# =========================================================================== runner and report


def test_run_checks_order_only_and_a_crashing_check(tmp_path, monkeypatch) -> None:
    settings = tidb(tmp_path)

    def broken(settings: Settings) -> lc.CheckResult:
        raise RuntimeError(f"driver exploded near {TIDB_PASSWORD}")

    monkeypatch.setattr(lc, "check_tidb", broken)
    results = lc.run_checks(settings)
    assert [r.service for r in results] == ["gemini", "elevenlabs", "snowflake", "tidb", "smtp"]
    crashed = results[3]
    assert crashed.status == ns.ERROR and crashed.failed and "RuntimeError" in crashed.detail and "***" in crashed.detail
    assert_no_secrets(*results)
    assert [r.service for r in lc.run_checks(settings, only={"smtp", "gemini"})] == ["gemini", "smtp"]
    with pytest.raises(ValueError, match="unknown service"):
        lc.run_checks(settings, only={"gemini-agent"})


def test_format_report_layout(tmp_path) -> None:
    env = tmp_path / ".env"
    results = [lc.CheckResult("gemini", ns.BLOCKED, "the network redirected the request", "Use another network."),
               lc.CheckResult("gemini-agent", ns.OK, "fine"),
               lc.CheckResult("smtp", ns.NOT_CONFIGURED, "set SMTP_HOST")]
    text = lc.format_report(results, env)
    lines = text.splitlines()
    assert lines[0] == f"Settings file: {env.resolve()} (not found)" and "copy .env.example" in lines[1]
    header = next(line for line in lines if line.startswith("SERVICE"))
    assert header.split() == ["SERVICE", "STATUS", "DETAIL"]
    gem = lines.index(next(line for line in lines if line.startswith("gemini ")))
    assert lines[gem].split()[:2] == ["gemini", "BLOCKED_BY_NETWORK"]
    assert lines[gem + 1].strip() == "-> Use another network." and lines[gem + 1].index("->") == header.index("DETAIL")
    assert "Summary: 1 OK, 1 failed, 1 not configured." in text
    env.write_text("", encoding="utf-8")
    assert lc.format_report(results, env).startswith(f"Settings file: {env.resolve()} (found)")
    wrapped = lc.format_report([lc.CheckResult("smtp", ns.OK, "word " * 40, "hint " * 30)], env, width=80)
    assert all(len(line) <= 80 for line in wrapped.splitlines() if not line.startswith("Settings file"))
    assert lc.CheckResult("x", lc.WARN, "d").to_dict()["failed"] is False


# =========================================================================== CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for key, value in {"TACTIDOSE_DATA_DIR": str(tmp_path / "data"), "TACTIDOSE_HARDWARE_MODE": "none",
                       "TACTIDOSE_VOICE_ENABLED": "false", "TACTIDOSE_TTS_PROVIDER": "none"}.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_service_names_match_the_checks() -> None:
    assert cli.API_SERVICES == lc.SERVICES


def test_cli_nothing_configured_contacts_nothing_and_exits_0(cli_env, capsys) -> None:
    code, out, err = run(capsys, "check-apis")
    assert code == 0, out + err
    assert out.startswith(f"Settings file: {(cli_env / '.env').resolve()} (not found)")
    for service in lc.SERVICES:
        assert any(line.split()[:2] == [service, ns.NOT_CONFIGURED] for line in out.splitlines()), service
    assert "Nothing was contacted" in out and "Contacting" in err


def test_cli_only_and_json_and_failure_exit_code(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "tactidose.demo@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", SMTP_PASSWORD)
    rec = Recorder(lambda r: httpx.Response(307, headers={"location": "https://login.corp.example/x"}))
    monkeypatch.setattr(lc, "_new_client", rec.client)
    code, out, err = run(capsys, "check-apis", "--only", "gemini", "--json")
    assert code == 1, err
    (row,) = json.loads(out)
    assert row["service"] == "gemini" and row["status"] == ns.BLOCKED and row["failed"] is True
    assert len(rec.requests) == 1                                       # SMTP was not selected: never contacted
    for secret in (GEMINI_KEY, SMTP_PASSWORD):
        assert secret not in out + err
    code, out, err = run(capsys, "check-apis", "--only", "gemini", "--only", "gemini")
    assert code == 1 and "BLOCKED_BY_NETWORK" in out and GEMINI_KEY not in out + err


@pytest.mark.parametrize("value", ["gemini,openai", ",", "gemini-agent"])
def test_cli_only_rejects_unknown_names(cli_env, capsys, value) -> None:
    code, _, err = run(capsys, "check-apis", "--only", value)
    assert code == 2 and "usage:" in err


def test_cli_everything_configured_and_working(cli_env, capsys, monkeypatch) -> None:
    for key, value in {
        "GEMINI_API_KEY": GEMINI_KEY, "TACTIDOSE_TTS_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": ELEVEN_KEY,
        "SNOWFLAKE_ACCOUNT": "myorg-myacct", "SNOWFLAKE_USER": "TACTI", "SNOWFLAKE_TOKEN": SF_TOKEN,
        "SNOWFLAKE_WAREHOUSE": "COMPUTE_WH", "TACTIDOSE_ANALYTICS_SALT": "a-long-random-salt-4f9c2b",
        "TIDB_HOST": "gateway01.us-west-2.prod.aws.tidbcloud.com", "TIDB_USER": "2abc3def.root",
        "TIDB_PASSWORD": TIDB_PASSWORD, "SMTP_HOST": "smtp.gmail.com", "SMTP_USER": "tactidose.demo@gmail.com",
        "SMTP_PASSWORD": SMTP_PASSWORD, "SMTP_FROM": "tactidose.demo@gmail.com",
    }.items():
        monkeypatch.setenv(key, value)

    def route(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "generativelanguage.googleapis.com":
            return gemini_ok(request)
        if host == "api.elevenlabs.io":
            return audio(request)
        if host.endswith(".snowflakecomputing.com"):
            return preflight(request)
        raise LiveCall(host)

    rec, sdks = Recorder(route), []

    def new_sdk(genai: Any, types: Any, key: str) -> FakeGenai:
        assert key == GEMINI_KEY
        sdks.append(FakeGenai(text_reply()))
        return sdks[-1]

    monkeypatch.setattr(lc, "_new_client", rec.client)
    monkeypatch.setattr(lc, "_new_genai_client", new_sdk)
    monkeypatch.setattr(sf, "_default_connect", FakeConnect(FakeConnection(SF_ROW)))
    monkeypatch.setattr(pymysql, "connect", FakeConnect(FakeConnection(("8.0.11-TiDB-v7.5.2-serverless",))))
    fake_smtp(monkeypatch)
    code, out, err = run(capsys, "check-apis")
    assert code == 0, out + err
    statuses = {line.split()[0]: line.split()[1] for line in out.splitlines()
                if line.split() and line.split()[0] in (*lc.SERVICES, "gemini-agent")}
    assert statuses == {name: ns.OK for name in (*lc.SERVICES, "gemini-agent")}, out
    assert "Summary: 6 OK." in out and len(sdks) == 1 and sdks[0].closed      # a client it made is closed
    code, out_json, err_json = run(capsys, "check-apis", "--json")
    assert code == 0 and [r["status"] for r in json.loads(out_json)] == [ns.OK] * 6
    for secret in SECRETS:
        assert secret not in out + err + out_json + err_json
