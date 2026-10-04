"""Tests for tactidose.integrations.gemini. No network: every client is a fake object
mimicking ``client.models.generate_content(model=, contents=, config=)``."""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr

from tactidose.config import Settings
from tactidose.core.interfaces import ExtractionResult, LabelExtraction, LabelExtractor
from tactidose.integrations import gemini as g

API_KEY = "AIzaFAKE-test-key-0123456789"
PIXELS = b"SECRETPIXELS" * 8
PNG = b"\x89PNG\r\n\x1a\n" + PIXELS
JPEG = b"\xff\xd8\xff\xe0" + PIXELS
COULD_NOT_READ = ExtractionResult.COULD_NOT_READ

VALID = {
    "medication_name": "Vitamin C (demo candy)",
    "strength": "1 piece",
    "visible_instructions": "Take one piece in the morning.",
    "warnings_visible": ["Demo only"],
    "confidence_notes": "",
    "legible": True,
}
VALID_JSON = json.dumps(VALID)


# --------------------------------------------------------------------------- fakes


class FakeModels:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0) if self.outcomes else resp(VALID_JSON)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.models = FakeModels(list(outcomes))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.models.calls


def resp(text: str | None, *, finish: Any = None, block: Any = None) -> SimpleNamespace:
    candidates = [SimpleNamespace(finish_reason=finish)] if finish is not None else None
    feedback = SimpleNamespace(block_reason=block) if block is not None else None
    return SimpleNamespace(text=text, candidates=candidates, prompt_feedback=feedback)


def sdk_resp(text: str, finish: types.FinishReason = types.FinishReason.STOP) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]), finish_reason=finish)])


def not_found(model: str = "gemini-3.8-flash") -> genai_errors.ClientError:
    return genai_errors.ClientError(404, {"error": {
        "code": 404, "status": "NOT_FOUND",
        "message": f"models/{model} is not found for API version v1beta"}})


@pytest.fixture
def gsettings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, data_dir=tmp_path / "data", label_extractor="gemini",
        gemini_api_key=API_KEY, gemini_model="gemini-3.8-flash",
        gemini_fallback_model="gemini-flash-latest", gemini_timeout_s=45,
    )


def extractor(gsettings: Settings, *outcomes: Any) -> tuple[g.GeminiLabelExtractor, FakeClient]:
    client = FakeClient(*outcomes)
    return g.GeminiLabelExtractor(gsettings, client=client), client


def assert_failed(result: ExtractionResult, error: str) -> None:
    assert result.ok is False
    assert result.error == error
    assert result.user_message == COULD_NOT_READ


# --------------------------------------------------------------------------- success paths


def test_success_and_request_shape(gsettings):
    ex, client = extractor(gsettings, resp(VALID_JSON))
    assert isinstance(ex, LabelExtractor) and ex.name == "gemini"
    result = ex.extract(PNG, "image/png")
    assert result.ok and result.error is None and result.user_message is None
    assert result.model == "gemini-3.8-flash"
    assert result.data == LabelExtraction(**VALID)
    assert result.raw_text == VALID_JSON

    (call,) = client.calls
    assert call["model"] == "gemini-3.8-flash"
    part, prompt = call["contents"]
    assert isinstance(part, types.Part)
    assert part.inline_data.data == PNG and part.inline_data.mime_type == "image/png"
    assert prompt == g.PROMPT
    cfg = call["config"]
    assert isinstance(cfg, types.GenerateContentConfig)
    assert cfg.system_instruction == g.SYSTEM_INSTRUCTION
    assert cfg.response_mime_type == "application/json"
    assert cfg.temperature is None and cfg.top_p is None and cfg.top_k is None   # Gemini 3: default sampling
    assert cfg.response_schema is None
    assert cfg.response_json_schema == g.response_json_schema()
    assert cfg.tools is None and cfg.automatic_function_calling.disable is True


def test_real_sdk_response_object_is_understood(gsettings):
    ex, _ = extractor(gsettings, sdk_resp(VALID_JSON))
    result = ex.extract(JPEG, "image/jpeg")
    assert result.ok and result.data.medication_name == "Vitamin C (demo candy)"


@pytest.mark.parametrize("text", [
    f"```json\n{VALID_JSON}\n```",
    f"Here is the transcription:\n```JSON\n{VALID_JSON}\n```\nLet me know if you need anything else.",
    f"```\n{VALID_JSON}\n```",
    f"Sure! {VALID_JSON} -- end",
    f"[{VALID_JSON}]",
])
def test_fenced_or_wrapped_json_is_tolerated(gsettings, text):
    ex, _ = extractor(gsettings, resp(text))
    result = ex.extract(PNG, "image/png")
    assert result.ok, result
    assert result.data.medication_name == VALID["medication_name"]
    assert result.raw_text == text


def test_null_fields_and_string_warning_are_repaired(gsettings):
    payload = {"medication_name": "Mint (demo)", "strength": None, "visible_instructions": None,
               "warnings_visible": "Contains sugar", "confidence_notes": None, "legible": True}
    ex, _ = extractor(gsettings, resp(json.dumps(payload)))
    result = ex.extract(PNG, "image/png")
    assert result.ok
    assert result.data.strength == "" and result.data.warnings_visible == ["Contains sugar"]


def test_whitespace_is_normalised(gsettings):
    payload = dict(VALID, medication_name="  Vitamin\n C \t(demo)\x00  ", strength=" 1  piece ",
                   visible_instructions="  Line one  \r\n\r\n\r\n\r\nLine two\x07 ",
                   warnings_visible=["  ", " Keep   dry ", ""])
    ex, _ = extractor(gsettings, resp(json.dumps(payload)))
    data = ex.extract(PNG, "image/png").data
    assert data.medication_name == "Vitamin C (demo)"
    assert data.strength == "1 piece"
    assert data.visible_instructions == "Line one\n\nLine two"
    assert data.warnings_visible == ["Keep dry"]


def test_length_caps(gsettings):
    payload = dict(VALID, medication_name="N" * 500, strength="S" * 300,
                   visible_instructions="I" * 5000, warnings_visible=[f"W{i}" + "w" * 1000 for i in range(30)],
                   confidence_notes="C" * 3000)
    ex, _ = extractor(gsettings, resp(json.dumps(payload)))
    result = ex.extract(PNG, "image/png")
    assert result.ok
    data = result.data
    assert len(data.medication_name) == g.MAX_NAME_CHARS == 200
    assert len(data.strength) == g.MAX_STRENGTH_CHARS == 120
    assert len(data.visible_instructions) == g.MAX_INSTRUCTIONS_CHARS == 2000
    assert len(data.warnings_visible) == g.MAX_WARNINGS == 20
    assert all(len(w) == g.MAX_WARNING_CHARS == 300 for w in data.warnings_visible)
    assert data.warnings_visible[0].startswith("W0") and data.warnings_visible[-1].startswith("W19")
    # the human reviewer is told what was cut
    assert "[TactiDose:" in data.confidence_notes
    for word in ("name truncated", "strength truncated", "instructions truncated",
                 "first 20 of 30 warnings", "long warnings truncated"):
        assert word in data.confidence_notes
    assert len(data.confidence_notes) < g.MAX_NOTES_CHARS + 300


# --------------------------------------------------------------------------- unreadable / invalid


@pytest.mark.parametrize("payload", [
    dict(VALID, legible=False),
    dict(VALID, legible=False, medication_name=""),
    dict(VALID, legible=None),
    dict(VALID, legible="false"),
])
def test_not_legible(gsettings, payload):
    ex, _ = extractor(gsettings, resp(json.dumps(payload)))
    result = ex.extract(PNG, "image/png")
    assert_failed(result, g.ERR_UNREADABLE)
    assert result.data is not None and result.data.legible is False   # kept for audit


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
def test_empty_name_is_unreadable(gsettings, name):
    ex, _ = extractor(gsettings, resp(json.dumps(dict(VALID, medication_name=name))))
    result = ex.extract(PNG, "image/png")
    assert_failed(result, g.ERR_UNREADABLE)
    assert result.data.medication_name == ""


@pytest.mark.parametrize("text", [
    "I cannot help with that.",
    "{not json at all",
    '{"medication_name": ["a", "b"], "legible": true}',
    '"just a string"',
    "",
    "   ",
    None,
])
def test_invalid_response(gsettings, text):
    ex, _ = extractor(gsettings, resp(text))
    result = ex.extract(PNG, "image/png")
    assert_failed(result, g.ERR_INVALID_RESPONSE)
    assert result.data is None


def test_truncated_output_is_invalid_even_if_parseable(gsettings):
    ex, _ = extractor(gsettings, sdk_resp(VALID_JSON, finish=types.FinishReason.MAX_TOKENS))
    assert_failed(ex.extract(PNG, "image/png"), g.ERR_INVALID_RESPONSE)


@pytest.mark.parametrize("response", [
    resp(None, block="SAFETY"),
    resp(VALID_JSON, finish="SAFETY"),
    resp(VALID_JSON, finish=types.FinishReason.PROHIBITED_CONTENT),
    resp(None, finish="FinishReason.RECITATION"),
    types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(
        block_reason=types.BlockedReason.PROHIBITED_CONTENT)),
])
def test_blocked(gsettings, response):
    ex, _ = extractor(gsettings, response)
    assert_failed(ex.extract(PNG, "image/png"), g.ERR_BLOCKED)


def test_unspecified_block_reason_is_not_blocked(gsettings):
    ex, _ = extractor(gsettings, resp(VALID_JSON, block="BLOCKED_REASON_UNSPECIFIED"))
    assert ex.extract(PNG, "image/png").ok


# --------------------------------------------------------------------------- exceptions


_REQ = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")


@pytest.mark.parametrize("exc,code", [
    (httpx.ReadTimeout("read timed out", request=_REQ), "timeout"),
    (httpx.ConnectTimeout("connect timed out", request=_REQ), "timeout"),
    (TimeoutError("timed out"), "timeout"),
    (httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=_REQ), "network"),
    (httpx.RemoteProtocolError("Server disconnected", request=_REQ), "network"),
    (ConnectionResetError(104, "reset by peer"), "network"),
    (OSError(101, "Network is unreachable"), "network"),
    (genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad"}}), "api_error:400"),
    (genai_errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "key"}}), "api_error:403"),
    (genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}), "api_error:429"),
    (genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}}), "api_error:503"),
    (genai_errors.APIError(500, {"message": "internal", "status": "INTERNAL"}), "api_error:500"),
    (genai_errors.UnknownApiResponseError("Failed to parse response as JSON"), "invalid_response"),
    (httpx.DecodingError("bad gzip", request=_REQ), "invalid_response"),
    (json.JSONDecodeError("Expecting value", "<html>", 0), "invalid_response"),
    (RuntimeError("something odd"), "api_error:unknown"),
    (ValueError("weird"), "api_error:unknown"),
])
def test_exceptions_map_to_codes_and_never_raise(gsettings, exc, code):
    ex, client = extractor(gsettings, exc)
    result = ex.extract(PNG, "image/png")
    assert_failed(result, code)
    assert result.data is None and result.model == "gemini-3.8-flash"
    assert len(client.calls) == 1        # no retry for anything but "model not found"


def test_wrapped_exception_uses_cause():
    try:
        try:
            raise httpx.ConnectError("refused", request=_REQ)
        except httpx.ConnectError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert g.classify_exception(outer) == "network"


def test_broken_client_never_raises(gsettings):
    ex = g.GeminiLabelExtractor(gsettings, client=object())
    assert_failed(ex.extract(PNG, "image/png"), "api_error:unknown")


# --------------------------------------------------------------------------- fallback model


def test_404_retries_once_with_fallback_and_sticks(gsettings):
    ex, client = extractor(gsettings, not_found(), resp(VALID_JSON), resp(VALID_JSON))
    result = ex.extract(PNG, "image/png")
    assert result.ok and result.model == "gemini-flash-latest"
    assert [c["model"] for c in client.calls] == ["gemini-3.8-flash", "gemini-flash-latest"]
    assert ex.model == "gemini-flash-latest"
    again = ex.extract(PNG, "image/png")
    assert again.ok and [c["model"] for c in client.calls][-1] == "gemini-flash-latest"
    assert len(client.calls) == 3


def test_not_found_status_without_code_also_falls_back(gsettings):
    exc = genai_errors.ClientError(0, {"error": {"status": "NOT_FOUND", "message": "no such model"}})
    ex, client = extractor(gsettings, exc, resp(VALID_JSON))
    assert ex.extract(PNG, "image/png").ok
    assert [c["model"] for c in client.calls] == ["gemini-3.8-flash", "gemini-flash-latest"]


def test_fallback_is_tried_only_once(gsettings):
    ex, client = extractor(gsettings, not_found(), not_found("gemini-flash-latest"), resp(VALID_JSON))
    result = ex.extract(PNG, "image/png")
    assert_failed(result, "api_error:404")
    assert result.model == "gemini-flash-latest"
    assert len(client.calls) == 2


def test_fallback_error_is_reported(gsettings):
    ex, client = extractor(gsettings, not_found(), httpx.ReadTimeout("slow", request=_REQ))
    assert_failed(ex.extract(PNG, "image/png"), "timeout")
    assert len(client.calls) == 2


def test_no_fallback_when_same_or_empty(gsettings):
    for fallback in ("gemini-3.8-flash", ""):
        s = gsettings.model_copy(update={"gemini_fallback_model": fallback})
        ex, client = extractor(s, not_found(), resp(VALID_JSON))
        assert_failed(ex.extract(PNG, "image/png"), "api_error:404")
        assert len(client.calls) == 1


def test_other_client_errors_do_not_trigger_fallback(gsettings):
    exc = genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "x"}})
    ex, client = extractor(gsettings, exc, resp(VALID_JSON))
    assert_failed(ex.extract(PNG, "image/png"), "api_error:400")
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- input validation


def test_empty_or_oversized_image_rejected_without_call(gsettings):
    ex, client = extractor(gsettings)
    assert_failed(ex.extract(b"", "image/png"), g.ERR_INVALID_IMAGE)
    small = gsettings.model_copy(update={"max_label_image_bytes": 16})
    ex2 = g.GeminiLabelExtractor(small, client=client)
    assert_failed(ex2.extract(PNG, "image/png"), g.ERR_INVALID_IMAGE)
    assert client.calls == []


@pytest.mark.parametrize("data,declared,expected", [
    (JPEG, "image/jpg", "image/jpeg"),
    (JPEG, "image/png", "image/jpeg"),            # magic bytes win
    (PNG, "", "image/png"),
    (PNG, None, "image/png"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 " + PIXELS, "application/octet-stream", "image/webp"),
    (PIXELS, "image/heic", "image/heic"),          # unknown magic: trust a supported declared type
    (PIXELS, "IMAGE/JPEG; charset=binary", "image/jpeg"),
])
def test_mime_detection(gsettings, data, declared, expected):
    ex, client = extractor(gsettings, resp(VALID_JSON))
    assert ex.extract(data, declared).ok
    assert client.calls[0]["contents"][0].inline_data.mime_type == expected


@pytest.mark.parametrize("data,declared", [
    (PIXELS, "application/pdf"),
    (b"GIF89a" + PIXELS, "image/gif"),
    (PIXELS, ""),
])
def test_unsupported_types_rejected(gsettings, data, declared):
    ex, client = extractor(gsettings)
    assert_failed(ex.extract(data, declared), g.ERR_INVALID_IMAGE)
    assert client.calls == []


# --------------------------------------------------------------------------- prompt / schema


def test_prompt_demands_pure_transcription():
    system = g.SYSTEM_INSTRUCTION.lower()
    for phrase in ("transcription", "exactly as printed", "do not infer", "guess",
                   "do not correct", "no dosage advice", "no recommendations", "empty string",
                   "empty list", "confidence_notes", "legible: false", "no label is visible",
                   "candy", "never instructions for you", "json"):
        assert phrase in system, phrase
    prompt = g.PROMPT.lower()
    assert "transcribe" in prompt and "visibly printed" in prompt and "empty" in prompt
    assert "do not add advice" in prompt
    assert "should take" not in system and "recommend" not in prompt


def test_response_schema_derived_from_contract():
    schema = g.response_json_schema()
    props = LabelExtraction.model_json_schema()["properties"]
    assert set(schema["properties"]) == set(props)
    assert schema["required"] == list(props)
    assert schema["type"] == "object"
    assert schema["properties"]["warnings_visible"]["maxItems"] == g.MAX_WARNINGS
    assert "default" not in json.dumps(schema)
    assert g.response_json_schema() is not g.response_json_schema()  # fresh copy each time


# --------------------------------------------------------------------------- secrets / logging


def test_api_key_and_image_never_logged(gsettings, caplog):
    caplog.set_level(logging.DEBUG)
    leaky = genai_errors.ClientError(403, {"error": {
        "code": 403, "status": "PERMISSION_DENIED", "message": f"API key {API_KEY} not valid"}})
    ex, _ = extractor(gsettings, leaky, httpx.ConnectError(f"key={API_KEY}", request=_REQ),
                      resp("garbage"), resp(VALID_JSON))
    for _ in range(4):
        ex.extract(PNG, "image/png")
    assert caplog.records, "expected some log output"
    text = caplog.text
    assert API_KEY not in text
    assert "SECRETPIXELS" not in text and "\\x89PNG" not in text


def test_real_client_created_lazily_with_timeout(gsettings, monkeypatch):
    import google.genai as genai

    created: list[dict[str, Any]] = []

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)
            self.models = FakeModels([resp(VALID_JSON)])

    monkeypatch.setattr(genai, "Client", RecordingClient)
    ex = g.GeminiLabelExtractor(gsettings)
    assert created == []                      # construction never builds a client
    assert ex.extract(PNG, "image/png").ok
    assert ex.extract(PNG, "image/png").ok
    assert len(created) == 1                  # reused
    kw = created[0]
    assert kw["api_key"] == API_KEY and kw["vertexai"] is False
    assert isinstance(kw["http_options"], types.HttpOptions)
    assert kw["http_options"].timeout == 45000
    assert kw["http_options"].client_args == {"follow_redirects": False}


def test_failures_are_logged_as_netsafe_code_and_message(gsettings, caplog):
    url = "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?trace=abc"
    ex, _ = extractor(gsettings, httpx.ConnectError(f"cannot reach {url}", request=_REQ),
                      httpx.ReadTimeout(f"timed out {url}", request=_REQ))
    with caplog.at_level(logging.WARNING):
        assert_failed(ex.extract(PNG, "image/png"), "network")
        assert_failed(ex.extract(PNG, "image/png"), "timeout")
    assert "network (NETWORK_ERROR: could not connect" in caplog.text
    assert "timeout (TIMEOUT: the service did not answer in time)" in caplog.text
    assert "googleapis.com" not in caplog.text and "trace=abc" not in caplog.text


def test_temperature_constant_is_gone():
    assert not hasattr(g, "TEMPERATURE") and "TEMPERATURE" not in g.__all__


def route_to_mock(genai_client: Any, handler: Any) -> list[httpx.Request]:
    """Send a real ``genai.Client``'s own httpx client through ``handler`` (no sockets, no proxy
    mounts). Unlike ``MockHttp`` this keeps the client (and its redirect setting) the app built."""
    seen: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = genai_client._api_client._httpx_client
    http._mounts = {}
    http._transport = httpx.MockTransport(dispatch)
    return seen


def redirect_to_sign_in(request: httpx.Request) -> httpx.Response:
    """What a corporate web filter answers: 307 to its sign-in page (which must never get the key)."""
    if request.url.host == "sso.example.com":
        return httpx.Response(200, text="<html>sign in</html>")
    return httpx.Response(307, headers={"location": "https://sso.example.com/login?user=alex"})


def test_real_client_never_follows_redirects(gsettings, caplog):
    ex = g.GeminiLabelExtractor(gsettings)       # builds a real genai.Client (no network at construction)
    client = ex._get_client()
    try:
        assert client._api_client._httpx_client.follow_redirects is False
        seen = route_to_mock(client, redirect_to_sign_in)
        with caplog.at_level(logging.WARNING):
            result = ex.extract(PNG, "image/png")
        assert_failed(result, "api_error:307")
        assert [r.url.host for r in seen] == ["generativelanguage.googleapis.com"]   # 307 not followed
        assert "BLOCKED_BY_NETWORK: the network redirected the request" in caplog.text
        assert API_KEY not in caplog.text and "alex" not in caplog.text and "/login" not in caplog.text
    finally:
        client.close()


# --------------------------------------------------------------------------- real SDK, mock HTTP


class MockHttp:
    """A real ``genai.Client`` whose HTTP goes to an in-process httpx.MockTransport (no sockets).

    Building a client costs ~0.5 s, so one is shared per module and tests swap ``handler``.
    """

    def __init__(self) -> None:
        import google.genai as genai

        self.handler: Any = lambda request: httpx.Response(200, json=ok_body())
        self.seen: list[httpx.Request] = []
        http = httpx.Client(transport=httpx.MockTransport(self._dispatch))
        self.client = genai.Client(api_key=API_KEY, vertexai=False,
                                   http_options=types.HttpOptions(timeout=45000, httpx_client=http))

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.handler(request)


@pytest.fixture(scope="module")
def _shared_mock_http() -> MockHttp:
    return MockHttp()


@pytest.fixture
def mock_http(_shared_mock_http: MockHttp) -> MockHttp:
    _shared_mock_http.seen.clear()
    return _shared_mock_http


def ok_body(text: str = VALID_JSON) -> dict[str, Any]:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]}


def b64(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except ValueError:
        return base64.urlsafe_b64decode(data)


def test_real_sdk_request_and_fallback_over_mock_http(gsettings, mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if "gemini-3.8-flash" in request.url.path:
            return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND",
                                                       "message": "models/gemini-3.8-flash is not found"}})
        return httpx.Response(200, json=ok_body())

    mock_http.handler = handler
    seen = mock_http.seen
    ex = g.GeminiLabelExtractor(gsettings, client=mock_http.client)
    result = ex.extract(PNG, "image/png")
    assert result.ok and result.model == "gemini-flash-latest"
    assert result.data == LabelExtraction(**VALID)
    assert [r.url.path for r in seen] == ["/v1beta/models/gemini-3.8-flash:generateContent",
                                          "/v1beta/models/gemini-flash-latest:generateContent"]
    request = seen[-1]
    assert request.headers["x-goog-api-key"] == API_KEY and API_KEY not in str(request.url)
    body = json.loads(request.content)
    assert "tools" not in body
    gen = body["generationConfig"]
    assert gen["responseMimeType"] == "application/json" and "temperature" not in gen
    assert gen["responseJsonSchema"] == g.response_json_schema()
    assert gen["responseJsonSchema"]["required"] == list(LabelExtraction.model_fields)
    assert body["systemInstruction"]["parts"][0]["text"] == g.SYSTEM_INSTRUCTION
    image_part, text_part = body["contents"][0]["parts"]
    inline = image_part["inlineData"]
    assert (inline.get("mimeType") or inline.get("mime_type")) == "image/png"
    assert b64(inline["data"]) == PNG
    assert text_part["text"] == g.PROMPT


@pytest.mark.parametrize("respond,code", [
    (lambda r: httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}}),
     "api_error:503"),
    (lambda r: httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q"}}),
     "api_error:429"),
    (lambda r: httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "key"}}),
     "api_error:403"),
    (lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "blocked"),
    (lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}), "blocked"),
    (lambda r: httpx.Response(200, text="<html>proxy error</html>"), "invalid_response"),
    (lambda r: httpx.Response(200, json=ok_body("Sorry, I can't read that.")), "invalid_response"),
    (lambda r: httpx.Response(200, json={}), "invalid_response"),
    (lambda r: httpx.Response(200, json=ok_body(json.dumps(dict(VALID, legible=False)))), "unreadable"),
])
def test_real_sdk_failures_over_mock_http(gsettings, mock_http, respond, code):
    mock_http.handler = respond
    ex = g.GeminiLabelExtractor(gsettings, client=mock_http.client)
    assert_failed(ex.extract(PNG, "image/png"), code)
    assert len(mock_http.seen) == 1            # no SDK-level retries either


@pytest.mark.parametrize("exc_type,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")])
def test_real_sdk_transport_errors_over_mock_http(gsettings, mock_http, exc_type, code):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("simulated", request=request)

    mock_http.handler = handler
    ex = g.GeminiLabelExtractor(gsettings, client=mock_http.client)
    assert_failed(ex.extract(PNG, "image/png"), code)
    assert len(mock_http.seen) == 1


def test_missing_key_is_reported(gsettings):
    s = gsettings.model_copy(update={"gemini_api_key": SecretStr("")})
    assert_failed(g.GeminiLabelExtractor(s).extract(PNG, "image/png"), "api_error:no_api_key")


# --------------------------------------------------------------------------- fake + factory


def test_fake_extractor_is_deterministic(settings):
    fake = g.FakeLabelExtractor()
    assert isinstance(fake, LabelExtractor) and fake.name == "fake"
    a1, a2 = fake.extract(PNG, "image/png"), fake.extract(PNG, "image/png")
    assert a1 == a2 and a1.ok and a1.model == "fake-demo-extractor"
    assert a1.data.confidence_notes == g.FAKE_CONFIDENCE_NOTES == "FAKE EXTRACTOR - demo only"
    assert "demo" in a1.data.medication_name.lower() and a1.data.legible
    names = {fake.extract(bytes([i]) * 10, "image/png").data.medication_name for i in range(40)}
    assert len(names) > 1                                  # varies with the image
    assert all(fake.extract(bytes([i]) * 10, "").data.confidence_notes == g.FAKE_CONFIDENCE_NOTES
               for i in range(5))
    assert_failed(fake.extract(b"", "image/png"), g.ERR_INVALID_IMAGE)
    fixed = g.FakeLabelExtractor(LabelExtraction(medication_name="Gummy (demo)", confidence_notes="x"))
    out = fixed.extract(PNG, "image/png").data
    assert out.medication_name == "Gummy (demo)" and out.confidence_notes == g.FAKE_CONFIDENCE_NOTES


def test_factory_modes(settings, gsettings):
    assert isinstance(g.create_label_extractor(settings), g.FakeLabelExtractor)   # conftest: fake
    assert isinstance(g.create_label_extractor(gsettings), g.GeminiLabelExtractor)
    auto_with_key = gsettings.model_copy(update={"label_extractor": "auto"})
    assert isinstance(g.create_label_extractor(auto_with_key), g.GeminiLabelExtractor)
    auto_no_key = settings.model_copy(update={"label_extractor": "auto"})
    assert g.create_label_extractor(auto_no_key) is None
    assert g.create_label_extractor(settings.model_copy(update={"label_extractor": "disabled"})) is None
    gemini_no_key = settings.model_copy(update={"label_extractor": "gemini"})
    assert g.create_label_extractor(gemini_no_key) is None


def test_result_to_dict_round_trip():
    ok = g.FakeLabelExtractor().extract(PNG, "image/png")
    d = g.result_to_dict(ok)
    assert d["ok"] and d["extracted"]["confidence_notes"] == g.FAKE_CONFIDENCE_NOTES
    json.dumps(d)
    bad = g.result_to_dict(g.FakeLabelExtractor().extract(b"", ""))
    assert bad == {"ok": False, "model": "fake-demo-extractor", "error": "invalid_image",
                   "user_message": COULD_NOT_READ, "extracted": None, "raw_text": None}


# --------------------------------------------------------------------------- CLI


def test_cli_fake_prints_json(tmp_path, settings, capsys):
    img = tmp_path / "label.png"
    img.write_bytes(PNG)
    assert g.main([str(img), "--fake"], settings=settings) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["extracted"]["confidence_notes"] == g.FAKE_CONFIDENCE_NOTES


def test_cli_disabled_and_missing_file(tmp_path, settings, capsys):
    img = tmp_path / "label.png"
    img.write_bytes(PNG)
    disabled = settings.model_copy(update={"label_extractor": "disabled"})
    assert g.main([str(img)], settings=disabled) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "disabled"
    assert g.main([str(tmp_path / "missing.png"), "--fake"], settings=settings) == 2
    assert "cannot read image" in capsys.readouterr().out


def test_cli_failure_exit_code(tmp_path, settings, capsys):
    img = tmp_path / "empty.png"
    img.write_bytes(b"")
    assert g.main([str(img), "--fake"], settings=settings) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_image"
