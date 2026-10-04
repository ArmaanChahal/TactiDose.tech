"""SpeakerService (tactidose/audio/speaker.py): captions, fallback chain, interrupt, cache warm-up."""

from __future__ import annotations

import threading
from typing import Any, Callable

import httpx
import pytest
from pydantic import SecretStr

from tactidose.agent.voice import ReplyTTS
from tactidose.audio.cache import TTSCache
from tactidose.audio.playback import NullPlayer, pcm_to_wav, read_wav
from tactidose.audio.speaker import DEGRADED_S, MAX_QUEUE, SpeakerService
from tactidose.core import phrases
from tactidose.core.bus import EventBus, Topic
from tactidose.core.interfaces import Speaker
from tactidose.integrations.elevenlabs import ElevenLabsClient, ElevenLabsError, ElevenLabsUnavailable
from tests.fakes import wait_until

CLOUD_PCM = b"\x01\x00" * 160


class FakeTTS:
    voice_id, model_id, output_format, sample_rate = "voice-1", "model-1", "pcm_16000", 16000

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail is not None:
            raise self.fail
        return CLOUD_PCM


class FakeOffline:
    engine, voice = "fake-os", None

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.ok

    def synthesize(self, text: str) -> bytes | None:
        self.calls.append(text)
        return pcm_to_wav(b"\x02\x00" * 80, 22050) if self.ok else None


class GatePlayer:
    """Blocks in play_wav until released (or told to stop) when ``block`` is set."""

    def __init__(self, block: bool = False) -> None:
        self.block = block
        self.played: list[bytes] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.stops = 0
        self.interrupted = 0

    def play_wav(self, wav: bytes, *, should_stop: Callable[[], bool] | None = None) -> bool:
        self.played.append(wav)
        self.started.set()
        while self.block and not self.release.is_set():
            if should_stop is not None and should_stop():
                self.interrupted += 1
                return False
            self.release.wait(0.005)
        return True

    def stop(self) -> None:
        self.stops += 1


class MonoNow:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def make(settings, bus):
    made: list[SpeakerService] = []

    def factory(provider: str = "elevenlabs", *, start: bool = True, **kw: Any) -> SpeakerService:
        s = settings.model_copy(update={"tts_provider": provider})
        kw.setdefault("player", GatePlayer())
        svc = SpeakerService(s, bus, **kw)
        svc._now = MonoNow()
        made.append(svc)
        if start:
            svc.start()
        return svc

    yield factory
    for svc in made:
        svc.close()


def spoken(bus: EventBus) -> list[dict[str, Any]]:
    return [e.data for e in bus.recent(100, [Topic.SPOKEN])]


def test_implements_speaker_protocol(make):
    assert isinstance(make("none", start=False), Speaker)


def test_captions_only_with_provider_none(make, bus):
    tts, offline = FakeTTS(), FakeOffline()
    svc = make("none", tts=tts, offline=offline, player=None)
    svc.say("Cancelled.", kind="info", meta={"intent": "CANCEL", "source": "voice"})
    assert svc.wait_idle(2)
    assert spoken(bus) == [{"intent": "CANCEL", "source": "voice", "text": "Cancelled.", "kind": "info",
                            "audio": "none", "cached": False}]
    assert tts.calls == [] and offline.calls == []
    assert isinstance(svc._player, NullPlayer)


def test_elevenlabs_then_cache(make, bus):
    tts, player = FakeTTS(), GatePlayer()
    svc = make(tts=tts, offline=FakeOffline(), player=player)
    svc.say("Hello there.")
    svc.say("Hello there.")
    assert svc.wait_idle(2)
    assert [e["audio"] for e in spoken(bus)] == ["elevenlabs", "cache"]
    assert tts.calls == ["Hello there."]
    info = read_wav(player.played[0])
    assert info.sample_rate == 16000 and info.frames == CLOUD_PCM
    key = TTSCache.key("elevenlabs", "voice-1", "model-1", "pcm_16000", "Hello there.")
    assert svc._cache.contains(key)
    assert svc.status()["cache_entries"] == 1


@pytest.mark.parametrize("error", [ElevenLabsUnavailable("ConnectError: offline"),
                                   ElevenLabsError(500, "internal"), ElevenLabsError(401, "invalid key"),
                                   RuntimeError("unexpected")])
def test_cloud_failure_degrades_for_60s_and_falls_back_to_offline(make, bus, error):
    tts, offline = FakeTTS(fail=error), FakeOffline()
    svc = make(tts=tts, offline=offline)
    svc.say("First.")
    svc.say("Second.")
    assert svc.wait_idle(2)
    assert [e["audio"] for e in spoken(bus)] == ["offline", "offline"]
    assert tts.calls == ["First."]  # degraded: the cloud is skipped quickly
    status = svc.status()
    assert status["degraded"] is True and status["degraded_until"] and status["last_error"]
    notices = [e.data for e in bus.recent(50, [Topic.NOTICE])]
    assert len(notices) == 1 and notices[0]["code"] == "TTS_DEGRADED"
    svc._now.t += DEGRADED_S + 1
    tts.fail = None
    svc.say("Third.")
    assert svc.wait_idle(2)
    assert spoken(bus)[-1]["audio"] == "elevenlabs" and tts.calls[-1] == "Third."
    assert svc.status()["degraded_until"] is None


def test_offline_renders_are_cached(make, bus):
    offline = FakeOffline()
    svc = make("offline", tts=FakeTTS(), offline=offline)
    svc.say("Repeat me.")
    svc.say("Repeat me.")
    assert svc.wait_idle(2)
    events = spoken(bus)
    assert [(e["audio"], e["cached"]) for e in events] == [("offline", False), ("offline", True)]
    assert offline.calls == ["Repeat me."]


def test_offline_provider_never_uses_the_cloud(make, bus):
    tts = FakeTTS()
    svc = make("offline", tts=tts, offline=FakeOffline())
    svc.say("Hi.")
    assert svc.wait_idle(2)
    assert tts.calls == [] and svc.status()["elevenlabs"] is False


def test_no_engine_means_captions_only(make, bus):
    player = GatePlayer()
    svc = make(tts=None, offline=FakeOffline(ok=False), player=player)
    svc.say("Hello.")
    assert svc.wait_idle(2)
    assert spoken(bus)[-1]["audio"] == "none" and player.played == []


def test_warmed_cache_is_used_offline(make, bus):
    tts = FakeTTS()
    svc = make(tts=tts, offline=FakeOffline(ok=False))
    assert svc.warm_cache([phrases.CANCELLED])["rendered"] == 1
    tts.fail = ElevenLabsUnavailable("no network")
    svc.say(phrases.CANCELLED)
    assert svc.wait_idle(2)
    assert spoken(bus)[-1]["audio"] == "cache" and tts.calls == [phrases.CANCELLED]


def test_interrupt_drops_queue_and_cuts_playback(make, bus):
    player = GatePlayer(block=True)
    svc = make("offline", offline=FakeOffline(), player=player)
    svc.say("A long sentence.")
    assert player.started.wait(2)
    assert svc.is_speaking
    svc.say("B")
    svc.say("C")
    svc.say("Stop now.", interrupt=True)
    assert wait_until(lambda: player.interrupted == 1, timeout=2)  # current playback cut off
    assert player.stops >= 1
    player.release.set()
    assert svc.wait_idle(2)
    assert [e["text"] for e in spoken(bus)] == ["A long sentence.", "Stop now."]
    assert not svc.is_speaking


def test_is_speaking_covers_dequeue_to_end_of_playback(make):
    player = GatePlayer(block=True)
    svc = make("offline", offline=FakeOffline(), player=player)
    assert not svc.is_speaking
    svc.say("Hello.")
    assert svc.is_speaking  # queued counts as speaking (mute the mic early)
    assert player.started.wait(2) and svc.is_speaking
    assert svc.wait_idle(0.05) is False
    player.release.set()
    assert svc.wait_idle(2) and not svc.is_speaking


def test_queue_is_bounded(make, bus):
    player = GatePlayer(block=True)
    svc = make("offline", offline=FakeOffline(), player=player)
    svc.say("first")
    assert player.started.wait(2)
    for i in range(MAX_QUEUE + 3):
        svc.say(f"item {i}")
    assert svc.status()["queued"] == MAX_QUEUE
    player.release.set()
    assert svc.wait_idle(3)
    texts = [e["text"] for e in spoken(bus)]
    assert "item 0" not in texts and texts[-1] == f"item {MAX_QUEUE + 2}"


def test_failures_in_engines_or_player_never_kill_the_thread(make, bus):
    class ExplodingPlayer(GatePlayer):
        def play_wav(self, wav: bytes, *, should_stop=None) -> bool:
            raise RuntimeError("audio device vanished")

    class ExplodingOffline(FakeOffline):
        def synthesize(self, text: str) -> bytes | None:
            raise RuntimeError("engine crashed")

    svc = make("offline", offline=FakeOffline(), player=ExplodingPlayer())
    svc.say("one")
    assert svc.wait_idle(2)
    svc._offline = ExplodingOffline()
    svc.say("two")
    assert svc.wait_idle(2)
    svc._offline = FakeOffline()
    svc._player = GatePlayer()
    svc.say("three")
    assert svc.wait_idle(2)
    assert [e["text"] for e in spoken(bus)] == ["one", "three"]


def test_lifecycle_is_idempotent(make):
    svc = make("none")
    svc.start()
    assert sum(1 for t in threading.enumerate() if t.name == "speaker" and t.is_alive()) >= 1
    svc.close()
    svc.close()
    svc.say("after close")  # ignored, no error
    svc.start()  # closed services stay closed
    assert svc.status()["running"] is False


def test_say_before_start_is_spoken_after_start(make, bus):
    svc = make("none", start=False)
    svc.say("Queued early.")
    assert not svc.is_speaking  # not running: never block the microphone
    svc.start()
    assert svc.wait_idle(2)
    assert spoken(bus)[-1]["text"] == "Queued early."


# --------------------------------------------------------------------------- real client (httpx.MockTransport)


def cloud_client(handler: Callable[[httpx.Request], httpx.Response], **http_kw: Any) -> ElevenLabsClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), **http_kw)
    return ElevenLabsClient("sk-test", "voice-1", "model-1", "pcm_16000", client=http, auto_voice=True)


def test_a_network_redirect_falls_back_to_offline_speech(make, bus):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://sso.corp.example/login"})

    svc = make(tts=cloud_client(handler, follow_redirects=True), offline=FakeOffline())
    svc.say("First.")
    svc.say("Second.")
    assert svc.wait_idle(2)
    assert [e["audio"] for e in spoken(bus)] == ["offline", "offline"]
    assert hosts == ["api.elevenlabs.io"]  # one request, never to the sign-in host; then degraded for 60 s
    status = svc.status()
    assert status["degraded"] is True and "blocked by the network" in status["last_error"]
    assert [e.data["code"] for e in bus.recent(50, [Topic.NOTICE])] == ["TTS_DEGRADED"]


def test_an_auto_voice_switch_is_cached_under_the_voice_actually_used(make, bus):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            voices = [{"voice_id": "voice-2", "name": "Roger", "category": "premade"}]
            return httpx.Response(200, json={"voices": voices})
        if request.url.path.endswith("/voice-1"):
            return httpx.Response(404, json={"detail": {"status": "voice_not_found", "message": "voice not found"}})
        return httpx.Response(200, content=CLOUD_PCM)

    svc = make(tts=cloud_client(handler), offline=FakeOffline())
    svc.say("Hello there.")
    svc.say("Hello there.")
    assert svc.wait_idle(2)
    assert [e["audio"] for e in spoken(bus)] == ["elevenlabs", "cache"]
    assert calls == ["POST /v1/text-to-speech/voice-1", "GET /v1/voices", "POST /v1/text-to-speech/voice-2"]
    assert svc._cache.contains(TTSCache.key("elevenlabs", "voice-2", "model-1", "pcm_16000", "Hello there."))
    assert not svc._cache.contains(TTSCache.key("elevenlabs", "voice-1", "model-1", "pcm_16000", "Hello there."))
    assert svc.status()["degraded"] is False


# --------------------------------------------------------------------------- warm_cache


def test_warm_cache_defaults_to_critical_phrases(make):
    tts = FakeTTS()
    svc = make(tts=tts, offline=FakeOffline(), start=False)
    first = svc.warm_cache()
    total = len(set(phrases.CRITICAL_PHRASES))
    assert first["provider"] == "elevenlabs"
    assert (first["rendered"], first["cached"], first["failed"], first["total"]) == (total, 0, 0, total)
    second = svc.warm_cache()
    assert (second["rendered"], second["cached"]) == (0, total)
    assert len(tts.calls) == total and svc.status()["cache_entries"] == total


def test_warm_cache_stops_calling_cloud_after_network_failure(make):
    tts = FakeTTS(fail=ElevenLabsUnavailable("no network"))
    svc = make(tts=tts, offline=FakeOffline(), start=False)
    result = svc.warm_cache(["a", "b", "c"])
    assert (result["rendered"], result["failed"]) == (0, 3) and len(tts.calls) == 1
    assert result["errors"]


def test_warm_cache_continues_after_a_single_bad_request(make):
    class Picky(FakeTTS):
        def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            if text == "bad":
                raise ElevenLabsError(400, "text rejected")
            return CLOUD_PCM

    svc = make(tts=Picky(), offline=FakeOffline(), start=False)
    result = svc.warm_cache(["good", "bad", "also good", "good"])
    assert (result["rendered"], result["failed"], result["total"]) == (2, 1, 3)


def test_warm_cache_stops_calling_cloud_after_a_voice_refusal(make):
    tts = FakeTTS(fail=ElevenLabsError(404, "voice_not_found: gone", voice_error=True))
    svc = make(tts=tts, offline=FakeOffline(), start=False)
    result = svc.warm_cache(["a", "b", "c"])
    assert (result["rendered"], result["failed"]) == (0, 3) and len(tts.calls) == 1


def test_warm_cache_offline_and_none(make):
    offline = FakeOffline()
    svc = make("offline", offline=offline, start=False)
    result = svc.warm_cache(["x", "y"])
    assert result["provider"] == "offline" and result["rendered"] == 2
    assert svc.warm_cache(["x", "y"])["cached"] == 2
    nothing = make("none", start=False).warm_cache(["x"])
    assert nothing["provider"] == "none" and nothing["failed"] == 1


# --------------------------------------------------------------------------- construction


def test_status_contract(make):
    st = make("none", start=False).status()
    for key in ("provider", "elevenlabs", "offline", "cache_entries", "degraded_until"):
        assert key in st
    assert st["provider"] == "none" and st["elevenlabs"] is False and st["degraded_until"] is None


AUTO_VOICE = pytest.mark.parametrize("update,auto", [({}, True), ({"elevenlabs_auto_voice": False}, False)])


@AUTO_VOICE
def test_configured_key_builds_a_client_without_network(settings, bus, update, auto):
    s = settings.model_copy(update={"tts_provider": "elevenlabs", "elevenlabs_api_key": SecretStr("sk-test"), **update})
    svc = SpeakerService(s, bus, offline=FakeOffline(), player=GatePlayer())
    assert svc.status()["elevenlabs"] is True
    assert svc._tts.voice_id == s.elevenlabs_voice_id and svc._tts._client is None
    assert svc._tts.auto_voice is auto
    svc.close()


@AUTO_VOICE
def test_reply_tts_builds_the_client_with_auto_voice_from_settings(settings, update, auto):
    s = settings.model_copy(update={"tts_provider": "elevenlabs", "elevenlabs_api_key": SecretStr("sk-test"), **update})
    tts = ReplyTTS(s, offline=FakeOffline())
    assert isinstance(tts._client, ElevenLabsClient) and tts._client.auto_voice is auto
    assert tts._client.voice_id == s.elevenlabs_voice_id and tts._client._client is None  # no network yet
    tts.close()


def test_reply_tts_caches_under_the_voice_actually_used(settings):
    class Switching(FakeTTS):
        def synthesize(self, text: str) -> bytes:
            self.voice_id = "voice-2"  # what auto voice does when voice-1 is refused
            return super().synthesize(text)

    cloud = Switching()
    tts = ReplyTTS(settings.model_copy(update={"tts_provider": "elevenlabs"}), client=cloud, offline=FakeOffline())
    first = tts.synthesize("Hi.")
    assert first[:4] == b"RIFF"
    assert tts._cache.contains(TTSCache.key("elevenlabs", "voice-2", "model-1", "pcm_16000", "Hi."))
    assert tts.synthesize("Hi.") == first and cloud.calls == ["Hi."]


def test_non_pcm_format_disables_cloud(settings, bus):
    s = settings.model_copy(update={"tts_provider": "elevenlabs", "elevenlabs_output_format": "mp3_44100_128",
                                    "elevenlabs_api_key": SecretStr("sk-test")})
    svc = SpeakerService(s, bus, offline=FakeOffline(), player=GatePlayer())
    assert svc.status()["elevenlabs"] is False
    svc.close()
