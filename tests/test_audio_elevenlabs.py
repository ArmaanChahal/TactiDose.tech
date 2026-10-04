"""ElevenLabs REST client (tactidose/integrations/elevenlabs.py) via httpx.MockTransport."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import httpx
import pytest

from tactidose.integrations.elevenlabs import (
    ElevenLabsClient,
    ElevenLabsError,
    ElevenLabsUnavailable,
    is_pcm_format,
    pick_voice,
    sample_rate_for,
)

KEY = "sk_test_secret_key_123"
OLD = "JBFqnCBsd6RMkjVDRZzb"  # the legacy default voice, refused for accounts created after March 2026
AUDIO = b"\x01\x02" * 50
VOICE_GONE = {"detail": {"status": "voice_not_found", "message": f"A voice with voice_id '{OLD}' was not found."}}
VOICES = {"voices": [
    {"voice_id": "clone1", "name": "My clone", "category": "cloned"},
    {"voice_id": "lib1", "name": "Library voice", "category": "professional"},
    {"voice_id": "roger1", "name": "Roger", "category": "premade", "labels": {"accent": "american"}},
    {"voice_id": "sarah1", "name": "Sarah", "category": "premade"},
]}
LOGGER = "tactidose.integrations.elevenlabs"


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
        def __init__(self, *, timeout: httpx.Timeout, follow_redirects: bool = True) -> None:
            self.timeout = timeout
            self.follow_redirects = follow_redirects  # httpx.Client's own default is False: be explicit
            self.is_closed = False
            created.append(self)

        def close(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(httpx, "Client", LightClient)
    client = ElevenLabsClient(KEY, "voice", "model", "pcm_16000", 2.0)
    assert created == []  # nothing is created until the first request
    http = client._http()
    assert client._http() is http and http.timeout.read == 2.0 and http.follow_redirects is False
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


# --------------------------------------------------------------------------- network blocks


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed_so_the_key_stays_on_the_api_host(status):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host != "api.elevenlabs.io":
            return httpx.Response(200, content=b"\x00\x00")  # the sign-in page would have received the key
        return httpx.Response(status, headers={"location": "https://sso.corp.example/login?user=alex"})

    # worst case: an injected client that follows redirects; every request turns them off again
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client = ElevenLabsClient(KEY, "voice", "model", client=http, auto_voice=True)
    with pytest.raises(ElevenLabsUnavailable) as info:
        client.synthesize("hello")
    message = str(info.value)
    assert "blocked by the network" in message and "redirected the request to sso.corp.example" in message
    assert "alex" not in message and "/login" not in message and KEY not in message
    assert not info.value.voice_error  # a block is not a voice refusal: nothing is listed
    with pytest.raises(ElevenLabsUnavailable, match="blocked by the network"):
        client.available_voices()
    assert [(r.method, r.url.host) for r in seen] == [("POST", "api.elevenlabs.io"), ("GET", "api.elevenlabs.io")]


def test_a_block_page_is_unavailable_and_never_returned_as_audio():
    client, _ = make_client(lambda r: httpx.Response(200, html="<html>Access to this site is blocked</html>"))
    with pytest.raises(ElevenLabsUnavailable, match="blocked by the network"):
        client.synthesize("hello")


def test_unusual_activity_401_explains_that_free_tier_use_is_off_on_this_network():
    body = {"detail": {"status": "detected_unusual_activity",
                       "message": "Unusual activity detected. Free Tier usage disabled. If you are using a proxy/VPN "
                                  "you might need to purchase a Paid Plan to not trigger our abuse detectors."}}
    calls: list[httpx.Request] = []
    client, _ = make_client(lambda r: calls.append(r) or httpx.Response(401, json=body), auto_voice=True)
    with pytest.raises(ElevenLabsError) as info:
        client.synthesize("hello")
    err = info.value
    assert err.status == 401 and not isinstance(err, ElevenLabsUnavailable) and not err.voice_error
    assert "turned off free-tier use from this network" in err.message and "a paid plan" in err.message
    assert len(calls) == 1 and KEY not in str(err)


# --------------------------------------------------------------------------- voice auto-pick


class FakeAPI:
    """MockTransport handler: refuses the voices in ``refused``; ``listing()`` answers GET /v1/voices."""

    def __init__(self, refused: tuple[str, ...] = (OLD,),
                 listing: Callable[[], httpx.Response] = lambda: httpx.Response(200, json=VOICES)) -> None:
        self.refused = refused
        self.listing = listing
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/voices":
            return self.listing()
        if request.url.path.rsplit("/", 1)[-1] in self.refused:
            return httpx.Response(404, json=VOICE_GONE)
        return httpx.Response(200, content=AUDIO)

    @property
    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def post(voice: str) -> str:
    return f"POST /v1/text-to-speech/{voice}"


def raising(exc: BaseException) -> Callable[[], httpx.Response]:
    def listing() -> httpx.Response:
        raise exc

    return listing


def test_a_refused_voice_is_replaced_by_the_first_premade_voice_and_remembered(caplog):
    api = FakeAPI()
    client, _ = make_client(api, auto_voice=True)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert client.synthesize("Hello.") == AUDIO
        assert client.synthesize("Again.") == AUDIO  # later calls use the new voice without listing
    assert api.calls == [post(OLD), "GET /v1/voices", post("roger1"), post("roger1")]
    listing = api.requests[1]
    assert listing.url == "https://api.elevenlabs.io/v1/voices" and listing.headers["xi-api-key"] == KEY
    assert client.voice_id == "roger1" and "roger1" in repr(client)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [f"ElevenLabs voice {OLD} is not available on this account; using Roger (roger1). "
                        "Set ELEVENLABS_VOICE_ID=roger1 in .env to keep it."]
    assert KEY not in caplog.text


@pytest.mark.parametrize("listing", [
    lambda: httpx.Response(307, headers={"location": "https://sso.corp.example/login"}),  # GETs are filtered here
    lambda: httpx.Response(401, json={"detail": {"status": "missing_permissions", "message": "needs voices_read"}}),
    lambda: httpx.Response(200, json={"voices": []}),
    lambda: httpx.Response(200, json={"voices": [{"voice_id": OLD, "name": "George", "category": "premade"}]}),
    lambda: httpx.Response(200, json={"unexpected": True}),
    lambda: httpx.Response(200, html="<html>Sign in to continue</html>"),
    raising(httpx.ConnectError("connection reset")),
], ids=["redirected", "no-permission", "empty", "only-the-refused-voice", "bad-shape", "block-page", "network"])
def test_a_failed_voice_list_raises_the_original_error_without_a_retry(listing, caplog):
    api = FakeAPI(listing=listing)
    client, _ = make_client(api, auto_voice=True)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with pytest.raises(ElevenLabsError) as info:
            client.synthesize("Hello.")
        with pytest.raises(ElevenLabsError):
            client.synthesize("Again.")  # listed again (the network may have changed) but warned once
    err = info.value
    assert type(err) is ElevenLabsError and err.status == 404 and err.voice_error
    assert "voice_not_found" in err.message
    assert api.calls == [post(OLD), "GET /v1/voices"] * 2 and client.voice_id == OLD
    [warning] = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "no other voice could be picked" in warning and "ELEVENLABS_VOICE_ID" in warning
    assert KEY not in caplog.text and "/login" not in warning


def test_without_auto_voice_a_refused_voice_is_raised_and_nothing_is_listed():
    api = FakeAPI()
    client, _ = make_client(api)
    with pytest.raises(ElevenLabsError) as info:
        client.synthesize("Hello.")
    assert info.value.voice_error and api.calls == [post(OLD)] and client.voice_id == OLD


@pytest.mark.parametrize("status,body", [
    (404, {"detail": "Not Found"}),  # a wrong URL, not a voice problem
    (401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}),
    (429, {"detail": {"status": "too_many_concurrent_requests", "message": "Too many concurrent requests"}}),
    (500, {"detail": "voice service crashed"}),  # mentions a voice, but a server error is not a refusal
    # validation errors echo the input: our own text saying "voice" is not a voice refusal
    (422, {"detail": [{"type": "string_too_long", "loc": ["body", "text"], "msg": "String too long",
                       "input": "Voice control is on."}]}),
])
def test_other_errors_never_trigger_a_voice_switch(status, body):
    calls: list[httpx.Request] = []
    client, _ = make_client(lambda r: calls.append(r) or httpx.Response(status, json=body), auto_voice=True)
    with pytest.raises(ElevenLabsError) as info:
        client.synthesize("Voice control is on.")
    assert info.value.status == status and not info.value.voice_error and len(calls) == 1


def test_the_retry_happens_once_per_call_and_the_switch_is_kept():
    api = FakeAPI(refused=(OLD, "roger1"))
    client, _ = make_client(api, auto_voice=True)
    with pytest.raises(ElevenLabsError) as info:
        client.synthesize("Hello.")
    assert info.value.voice_error and client.voice_id == "roger1"
    assert api.calls == [post(OLD), "GET /v1/voices", post("roger1")]  # no second listing in the same call
    assert client.synthesize("Later.") == AUDIO and client.voice_id == "sarah1"  # a later call may switch again


def test_a_switch_made_by_a_concurrent_call_is_reused_without_listing():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith(OLD):
            client.voice_id = "other1"  # another thread switched while this request was in flight
            return httpx.Response(404, json=VOICE_GONE)
        return httpx.Response(200, content=AUDIO)

    client, _ = make_client(handler, auto_voice=True)
    assert client.synthesize("Hello.") == AUDIO
    assert seen == [post(OLD), post("other1")]


def test_available_voices_request_and_shape():
    seen: list[httpx.Request] = []
    body = {"voices": [{"voice_id": "v1", "name": "Roger", "category": "premade", "labels": {"age": "middle"}},
                       {"voice_id": "v2", "category": "cloned"}, {"name": "no id"}, "junk"]}
    client, _ = make_client(lambda r: seen.append(r) or httpx.Response(200, json=body))
    assert client.available_voices() == [{"voice_id": "v1", "name": "Roger", "category": "premade"},
                                         {"voice_id": "v2", "name": "v2", "category": "cloned"}]
    [req] = seen
    assert req.method == "GET" and req.url == "https://api.elevenlabs.io/v1/voices"
    assert req.headers["xi-api-key"] == KEY and req.headers["accept"] == "application/json"


@pytest.mark.parametrize("voices,expected", [
    (VOICES["voices"], "roger1"),
    ([{"voice_id": "c1", "category": "cloned"}, {"voice_id": "d1", "category": "default"}], "d1"),
    ([{"voice_id": "c1", "category": "cloned"}, {"voice_id": "g1", "category": "generated"}], "c1"),
    ([{"voice_id": OLD, "category": "premade"}, {"voice_id": "c1", "category": "cloned"}], "c1"),
    ([{"voice_id": OLD, "category": "premade"}], None),
    ([], None),
])
def test_pick_voice_prefers_premade_then_default_and_never_the_refused_voice(voices, expected):
    pick = pick_voice(voices, exclude=OLD)
    assert (pick["voice_id"] if pick else None) == expected


def test_pick_voice_skips_legacy_voices():
    voices = [{"voice_id": "legacy1", "name": "Old", "category": "premade", "is_legacy": True},
              {"voice_id": "new1", "name": "New", "category": "premade"}]
    assert pick_voice(voices, exclude="")["voice_id"] == "new1"
    assert pick_voice(voices[:1], exclude="") is None
