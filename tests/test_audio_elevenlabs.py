"""ElevenLabs REST client (tactidose/integrations/elevenlabs.py) via httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tactidose.integrations.elevenlabs import (
    ElevenLabsClient,
    ElevenLabsError,
    ElevenLabsUnavailable,
    is_pcm_format,
    sample_rate_for,
)

KEY = "sk_test_secret_key_123"


def make_client(handler, **kw) -> tuple[ElevenLabsClient, httpx.Client]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ElevenLabsClient(KEY, kw.pop("voice_id", "JBFqnCBsd6RMkjVDRZzb"), kw.pop("model_id", "eleven_flash_v2_5"),
                              kw.pop("output_format", "pcm_22050"), kw.pop("timeout_s", 6.0), client=http, **kw)
    return client, http


def test_synthesize_request_shape_and_pcm_response():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\x01\x02" * 100, headers={"content-type": "audio/pcm"})

    client, _ = make_client(handler)
    audio = client.synthesize("  Cancelled.  ")
    assert audio == b"\x01\x02" * 100
    req = seen[0]
    assert req.method == "POST"
    assert req.url.scheme == "https" and req.url.host == "api.elevenlabs.io"
    assert req.url.path == "/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb"
    assert req.url.params["output_format"] == "pcm_22050"
    assert req.headers["xi-api-key"] == KEY
    assert req.headers["content-type"] == "application/json"
    assert req.headers["accept"] == "audio/*"
    assert json.loads(req.content) == {"text": "Cancelled.", "model_id": "eleven_flash_v2_5"}
    assert client.sample_rate == 22050 and client.is_pcm


def test_odd_length_pcm_is_trimmed_to_whole_samples():
    client, _ = make_client(lambda r: httpx.Response(200, content=b"\x01\x02\x03"))
    assert client.synthesize("hi") == b"\x01\x02"


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}, "invalid_api_key: Invalid API key"),
        (429, {"detail": {"status": "quota_exceeded", "message": "Quota exceeded"}}, "quota_exceeded"),
        (422, {"detail": [{"loc": ["body", "text"], "msg": "field required", "type": "missing"}]}, "field required"),
        (404, {"detail": "Not Found"}, "Not Found"),
        (500, "upstream exploded", "upstream exploded"),
    ],
)
def test_http_errors_raise_elevenlabs_error(status, body, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    client, _ = make_client(handler)
    with pytest.raises(ElevenLabsError) as info:
        client.synthesize("hello")
    err = info.value
    assert not isinstance(err, ElevenLabsUnavailable)
    assert err.status == status and expected in err.message
    assert KEY not in str(err)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ConnectError("getaddrinfo failed"), "ConnectError"),
        (httpx.ReadTimeout("read timed out"), "timed out after 6 s"),
        (httpx.ConnectTimeout("connect timed out"), "timed out"),
        (httpx.RemoteProtocolError("peer closed connection"), "RemoteProtocolError"),
    ],
)
def test_network_problems_raise_unavailable(exc, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    client, _ = make_client(handler)
    with pytest.raises(ElevenLabsUnavailable) as info:
        client.synthesize("hello")
    assert info.value.status == 0 and expected in str(info.value)
    assert isinstance(info.value, ElevenLabsError)  # callers can catch the base class


def test_empty_audio_is_an_error():
    client, _ = make_client(lambda r: httpx.Response(200, content=b""))
    with pytest.raises(ElevenLabsError, match="empty audio"):
        client.synthesize("hello")


def test_invalid_input_is_rejected_before_any_request():
    calls: list[httpx.Request] = []
    client, _ = make_client(lambda r: calls.append(r) or httpx.Response(200, content=b"\x00\x00"))
    with pytest.raises(ValueError):
        client.synthesize("   ")
    with pytest.raises(ValueError):
        client.synthesize("x" * 5001)
    assert calls == []
    with pytest.raises(ValueError):
        ElevenLabsClient("", "voice", "model")


def test_voice_id_is_url_encoded():
    seen: list[httpx.Request] = []
    client, _ = make_client(lambda r: seen.append(r) or httpx.Response(200, content=b"\x00\x00"),
                            voice_id="a/b c")
    client.synthesize("hi")
    assert seen[0].url.raw_path.startswith(b"/v1/text-to-speech/a%2Fb%20c")


def test_repr_hides_the_key_and_injected_client_is_not_closed():
    client, http = make_client(lambda r: httpx.Response(200, content=b"\x00\x00"))
    assert KEY not in repr(client)
    client.close()
    assert not http.is_closed
    assert client.synthesize("still works") == b"\x00\x00"


def test_owned_client_is_created_lazily_and_closed(monkeypatch):
    created: list[Any] = []

    class LightClient:  # avoids loading the system certificate store in a unit test
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            self.timeout = timeout
            self.is_closed = False
            created.append(self)

        def close(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(httpx, "Client", LightClient)
    client = ElevenLabsClient(KEY, "voice", "model", "pcm_16000", 2.0)
    assert created == []  # nothing is created until the first request
    http = client._http()
    assert client._http() is http and http.timeout.read == 2.0
    with client:
        pass
    assert http.is_closed and client._client is None


@pytest.mark.parametrize(
    "fmt,rate,pcm",
    [("pcm_16000", 16000, True), ("pcm_22050", 22050, True), ("pcm_24000", 24000, True),
     ("mp3_44100_128", 44100, False), ("ulaw_8000", 8000, False)],
)
def test_format_helpers(fmt, rate, pcm):
    assert sample_rate_for(fmt) == rate
    assert is_pcm_format(fmt) is pcm


def test_sample_rate_for_rejects_formats_without_a_rate():
    with pytest.raises(ValueError):
        sample_rate_for("wav")
