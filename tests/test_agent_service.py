"""AgentService (tactidose/agent/service.py): conversation storage, isolation, events, speech, STT."""

from __future__ import annotations

import io
import struct
import sys
import wave
from datetime import timedelta

import pytest
from google.genai import errors
from pydantic import SecretStr
from sqlalchemy import select

from tactidose.agent import AgentInputError, AgentNotAllowed, AgentUnavailable
from tactidose.agent.service import CONVERSATION_ROLLOVER, MODEL_FALLBACK, MODEL_SAFETY, clean_reply
from tactidose.agent.voice import MAX_PCM_BYTES, AudioStore, ReplyTTS, VoskTranscriber
from tactidose.core import phrases
from tactidose.core.bus import Topic
from tactidose.core.interfaces import AgentServiceAPI
from tactidose.db.models import Conversation, ConversationMessage, Role, User
from tests.test_agent_support import (
    FakeGenai,
    fake_vosk_module,
    make_model_dir,
    make_service,
    response,
    roles,
    text,
)


def wav_bytes(frames: int = 1600, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


class FakeTTS:
    def __init__(self, result: bytes | None = None) -> None:
        self.result = wav_bytes() if result is None else result
        self.texts: list[str] = []

    def synthesize(self, text: str) -> bytes | None:
        self.texts.append(text)
        return self.result or None


@pytest.fixture
def env(db_v2, settings_v2, clock, bus):
    return make_service(db_v2, settings_v2, clock, bus, tts=FakeTTS())


def chat(svc, pid, text, **kw):
    return svc.chat(patient_id=pid, text=text, **kw)


def add_patient(db, name="Bea Chen", email="bea@test.tactidose") -> int:
    with db.session() as s:
        user = User(display_name=name, role=Role.PATIENT.value, email=email, link_code="BEA2026")
        s.add(user)
        s.flush()
        return user.user_id


# --------------------------------------------------------------------------- contract & storage


def test_implements_the_protocol(env):
    svc, _, _ = env
    assert isinstance(svc, AgentServiceAPI)


def test_turn_is_persisted_in_order_and_published(env, db_v2, bus):
    svc, drops, ids = env
    pid = ids["patient_id"]
    sub = bus.subscribe([Topic.AGENT])
    reply = chat(svc, pid, "drop my vitamin c", input_mode="voice")
    stored = svc.messages(pid, reply.conversation_id)
    assert [m["role"] for m in stored] == ["user", "tool", "tool", "assistant"] == roles(reply)
    assert [m["message_id"] for m in stored] == [m["message_id"] for m in reply.messages]
    user, status, request, answer = stored
    assert user["content"] == "drop my vitamin c" and user["input_mode"] == "voice"
    assert status["tool_name"] == "get_patient_status" and status["tool_result"]["containers"]
    assert request["tool_name"] == "request_pill" and request["tool_result"]["status"] == "DROPPED"
    assert request["content"] == "request_pill: DROPPED" and request["input_mode"] is None
    assert answer["model"] == "rules" and answer["content"] == reply.text
    events = [e.data for e in sub.drain()]
    assert events == [{"patient_id": pid, "conversation_id": reply.conversation_id, "message_id": m["message_id"],
                       "role": m["role"]} for m in stored]
    with db_v2.session() as s:
        conv = s.get(Conversation, reply.conversation_id)
        assert conv.channel == "voice" and conv.title == "drop my vitamin c"
        assert all(m.patient_id == pid for m in s.scalars(select(ConversationMessage)))


def test_conversation_continues_then_rolls_over_after_30_minutes(env, clock):
    svc, drops, ids = env
    pid = ids["patient_id"]
    first = chat(svc, pid, "hello")
    clock.advance(timedelta(minutes=20))
    second = chat(svc, pid, "help")                      # no id: continue the latest one
    assert second.conversation_id == first.conversation_id
    clock.advance(timedelta(minutes=20))
    third = chat(svc, pid, "help", conversation_id=first.conversation_id, input_mode="voice")
    assert third.conversation_id == first.conversation_id
    clock.advance(CONVERSATION_ROLLOVER + timedelta(seconds=1))
    fourth = chat(svc, pid, "help", conversation_id=first.conversation_id)
    assert fourth.conversation_id != first.conversation_id
    listed = svc.conversations(pid)
    assert [c["conversation_id"] for c in listed] == [fourth.conversation_id, first.conversation_id]
    assert listed[1]["message_count"] == 6 and listed[1]["channel"] == "mixed"
    assert svc.get_conversation(pid, first.conversation_id)["message_count"] == 6


def test_patients_are_isolated(env, db_v2):
    svc, drops, ids = env
    alex = ids["patient_id"]
    bea = add_patient(db_v2)
    mine = chat(svc, alex, "hello")
    theirs = chat(svc, bea, "hello", conversation_id=mine.conversation_id)  # someone else's id
    assert theirs.conversation_id != mine.conversation_id
    assert svc.messages(bea, mine.conversation_id) == []
    assert svc.get_conversation(bea, mine.conversation_id) is None
    assert [c["conversation_id"] for c in svc.conversations(bea)] == [theirs.conversation_id]
    chat(svc, bea, "drop my vitamin c")
    assert set(drops.status_calls) == {bea} and all(r["patient_id"] == bea for r in drops.requests)
    assert all(r["conversation_id"] == theirs.conversation_id for r in drops.requests)
    assert [m["role"] for m in svc.messages(alex, mine.conversation_id)] == ["user", "assistant"]


@pytest.mark.parametrize("who", ["doctor_id", "family_id"])
def test_only_patients_can_chat(env, db_v2, who):
    svc, drops, ids = env
    with pytest.raises(AgentNotAllowed):
        chat(svc, ids[who], "drop a pill for Alex")
    with pytest.raises(AgentNotAllowed):
        chat(svc, 12345, "hello")
    with db_v2.session() as s:
        assert s.scalars(select(Conversation)).all() == []
    assert drops.requests == []


def test_empty_text_is_rejected(env):
    svc, drops, ids = env
    for bad in ("", "   ", None):
        with pytest.raises(AgentInputError):
            chat(svc, ids["patient_id"], bad)


def test_long_text_is_capped(env):
    svc, drops, ids = env
    reply = chat(svc, ids["patient_id"], "word " * 1000)
    assert len(reply.messages[0]["content"]) == 2000


def test_database_failure_fails_closed(env, monkeypatch):
    svc, drops, ids = env

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(svc.db, "session", broken)
    with pytest.raises(AgentUnavailable):
        chat(svc, ids["patient_id"], "drop my vitamin c")
    assert drops.requests == []


def test_history_window_is_bounded(db_v2, settings_v2, clock, bus):
    svc, drops, ids = make_service(db_v2, settings_v2, clock, bus, agent_history_messages=2)
    pid = ids["patient_id"]
    first = chat(svc, pid, "help")
    for _ in range(3):
        chat(svc, pid, "hello", conversation_id=first.conversation_id)
    seen = []
    original = svc._rules.respond

    def spy(text, tools, *, history=()):
        seen.append(list(history))
        return original(text, tools, history=history)

    svc._rules.respond = spy
    chat(svc, pid, "repeat", conversation_id=first.conversation_id)
    assert [m["role"] for m in seen[0]] == ["user", "assistant"]


def test_clean_reply_strips_markdown_and_caps_length():
    assert clean_reply("**Done.**\n- Your pill   dropped.") == "Done. Your pill dropped."
    assert clean_reply("All set \U0001F44D✅ \U0001F468‍⚕️") == "All set"
    long = clean_reply("This is a sentence. " * 60)
    assert len(long) <= 600 and long.endswith(".")


def test_status_summary_is_cheap(env, tmp_path):
    svc, _, _ = env
    st = svc.status()
    assert st["provider"] == "rules" and st["model"] == "rules" and st["stt"] in ("available", "unavailable")
    assert st["gemini_retry_in_s"] == 0 and st["gemini_last_error"] is None


# --------------------------------------------------------------------------- Gemini circuit breaker

GEMINI = "gemini-3.8-flash"
BREAKER_LOG = "tactidose.agent.service"


def blocked() -> errors.APIError:
    """What google-genai raises when a web filter answers 307 (the client does not follow it)."""
    return errors.APIError(307, {"message": "", "status": "Temporary Redirect"})


@pytest.fixture
def gemini_env(db_v2, settings_v2, clock, bus):
    """AgentService on a scripted Gemini fake with an injected monotonic time (``t[0]``)."""
    settings = settings_v2.model_copy(update={"agent_provider": "gemini", "gemini_model": GEMINI})

    def build(*script, **overrides):
        client = FakeGenai(*script)
        svc, drops, ids = make_service(db_v2, settings, clock, bus, genai=client, **overrides)
        t = [1000.0]
        svc._monotonic = lambda: t[0]
        return svc, drops, ids["patient_id"], client, t

    return build


def breaker_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == BREAKER_LOG and r.levelname == "WARNING" and "Gemini unavailable" in r.getMessage()]


def test_a_gemini_failure_pauses_gemini_then_it_is_retried(gemini_env, clock, caplog):
    svc, drops, pid, client, t = gemini_env(blocked(), response(text("Hello again.")))
    with caplog.at_level("INFO", logger=BREAKER_LOG):
        first = chat(svc, pid, "hello")
        assert first.model == MODEL_FALLBACK and len(client.calls) == 1
        st = svc.status()
        assert st["gemini_retry_in_s"] == 60 and st["gemini_last_error"] == "BLOCKED_BY_NETWORK"
        assert st["provider"] == "gemini" and st["model"] == GEMINI          # existing keys unchanged

        t[0] += 30                        # paused: the rules agent answers at once, tools still work
        dropped = chat(svc, pid, "drop my vitamin c")
        assert dropped.model == MODEL_FALLBACK and dropped.text == "Vitamin C dropped from container 1."
        assert len(drops.requests) == 1 and len(client.calls) == 1
        assert svc.status()["gemini_retry_in_s"] == 30

        clock.advance(timedelta(hours=5))  # demo clock travel does not shorten (or extend) the pause
        assert chat(svc, pid, "hello").model == MODEL_FALLBACK and len(client.calls) == 1
        assert svc.status()["gemini_retry_in_s"] == 30

        t[0] += 30                        # pause over: Gemini is tried again; a success closes the breaker
        again = chat(svc, pid, "hello")
        assert again.model == GEMINI and again.text == "Hello again." and len(client.calls) == 2
        assert svc.status()["gemini_retry_in_s"] == 0 and svc.status()["gemini_last_error"] is None
    assert breaker_warnings(caplog) == [
        "Gemini unavailable (BLOCKED_BY_NETWORK: the network redirected the request to another site); "
        "the offline assistant answers for the next 60 s"]
    assert "Gemini is answering again" in caplog.text


def test_a_failed_retry_reopens_the_breaker(gemini_env, caplog):
    quota = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}})
    svc, drops, pid, client, t = gemini_env(blocked(), quota, agent_retry_after_s=10)
    with caplog.at_level("WARNING", logger=BREAKER_LOG):
        assert chat(svc, pid, "hello").model == MODEL_FALLBACK
        t[0] += 5
        assert chat(svc, pid, "hello").model == MODEL_FALLBACK and len(client.calls) == 1
        t[0] += 5.5
        assert chat(svc, pid, "hello").model == MODEL_FALLBACK and len(client.calls) == 2
    assert svc.status()["gemini_retry_in_s"] == 10 and svc.status()["gemini_last_error"] == "QUOTA_EXCEEDED"
    assert len(breaker_warnings(caplog)) == 2 and "QUOTA_EXCEEDED: too many requests" in breaker_warnings(caplog)[1]


def test_retry_after_zero_tries_gemini_every_turn(gemini_env, caplog):
    timeout = TimeoutError("read timed out")
    svc, drops, pid, client, t = gemini_env(timeout, ConnectionError("down"), response(text("Hi.")),
                                            agent_retry_after_s=0)
    with caplog.at_level("WARNING", logger=BREAKER_LOG):
        models = [chat(svc, pid, "hello").model for _ in range(3)]
    assert models == [MODEL_FALLBACK, MODEL_FALLBACK, GEMINI] and len(client.calls) == 3
    assert svc.status()["gemini_retry_in_s"] == 0 and svc.status()["gemini_last_error"] is None
    assert [w.split(";")[1].strip() for w in breaker_warnings(caplog)] == [
        "the offline assistant answers this turn"] * 2


def test_emergency_and_stop_stay_deterministic_while_paused(gemini_env):
    svc, drops, pid, client, t = gemini_env(blocked())
    assert chat(svc, pid, "hello").model == MODEL_FALLBACK
    emergency = chat(svc, pid, "I have chest pain and can't breathe")
    assert emergency.text == phrases.EMERGENCY and emergency.model == MODEL_SAFETY
    drops.moving = True
    stop = chat(svc, pid, "stop")
    assert stop.text == phrases.STOPPED and stop.model == MODEL_SAFETY and drops.interrupts == 1
    assert len(client.calls) == 1 and drops.requests == []


def test_an_unexpected_provider_error_opens_the_breaker_too(gemini_env, monkeypatch, caplog):
    svc, drops, pid, client, t = gemini_env()

    def broken(**kw):
        raise KeyError("provider bug")

    monkeypatch.setattr(svc._gemini_agent(), "respond", broken)
    with caplog.at_level("WARNING", logger=BREAKER_LOG):
        assert chat(svc, pid, "help").model == MODEL_FALLBACK
        assert chat(svc, pid, "help").model == MODEL_FALLBACK
    assert svc.status()["gemini_last_error"] == "ERROR" and svc.status()["gemini_retry_in_s"] == 60
    assert len(breaker_warnings(caplog)) == 1
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)   # the bug keeps its traceback


def test_breaker_warning_never_contains_the_key(gemini_env, caplog):
    key = "AIzaFAKE-service-key-0123456789"
    leaky = errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                               "message": f"API key {key} has no access"}})
    svc, drops, pid, client, t = gemini_env(leaky, gemini_api_key=SecretStr(key))
    with caplog.at_level("DEBUG"):
        assert chat(svc, pid, "hello").model == MODEL_FALLBACK
    assert "PERMISSION_DENIED" in caplog.text and key not in caplog.text


# --------------------------------------------------------------------------- reply audio


def test_speak_and_audio_are_owned_by_the_patient(env, db_v2):
    svc, drops, ids = env
    pid = ids["patient_id"]
    audio_id = svc.speak(pid, "Vitamin C dropped from container 1.")
    assert audio_id and len(audio_id) == 32 and svc.audio_url(audio_id) == f"/api/agent/audio/{audio_id}.wav"
    wav = svc.audio(audio_id, pid)
    assert wav is not None and wav[:4] == b"RIFF"
    assert svc.audio(f"{audio_id}.wav", pid) == wav
    assert svc.audio(audio_id, add_patient(db_v2)) is None
    assert svc.audio("not-an-id", pid) is None and svc.audio("0" * 32, pid) is None


def test_speak_without_tts(db_v2, settings_v2, clock, bus):
    svc, drops, ids = make_service(db_v2, settings_v2, clock, bus)   # tts_provider="none"
    assert svc.speak(ids["patient_id"], "Hello.") is None
    assert svc.status()["tts"] is False


def test_speak_returns_none_when_rendering_fails(db_v2, settings_v2, clock, bus):
    svc, drops, ids = make_service(db_v2, settings_v2, clock, bus, tts=FakeTTS(result=b""))
    assert svc.speak(ids["patient_id"], "Hello.") is None
    assert svc.speak(ids["patient_id"], "   ") is None


def test_audio_store_ttl_and_lru():
    now = [100.0]
    store = AudioStore(ttl_s=600, max_items=2, now=lambda: now[0])
    a = store.put(1, b"a")
    b = store.put(1, b"b")
    assert store.get(a, 1) == b"a"            # touch a: b is now the least recently used
    c = store.put(2, b"c")
    assert store.get(b, 1) is None and store.get(a, 1) == b"a" and store.get(c, 2) == b"c"
    assert store.get(c, 1) is None            # wrong patient
    now[0] += 601
    assert store.get(a, 1) is None and len(store) == 0


def test_audio_store_bounds_total_bytes():
    store = AudioStore(max_items=10, max_bytes=5)
    first = store.put(1, b"abc")
    second = store.put(1, b"def")
    assert store.get(first, 1) is None and store.get(second, 1) == b"def"


class FakeCloud:
    voice_id, model_id, output_format, sample_rate = "v", "m", "pcm_16000", 16000

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("network down")
        return b"\x00\x01" * 800

    def close(self) -> None:
        pass


class FakeOffline:
    engine, voice = "fake-sapi", None

    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return wav_bytes(800)


def test_reply_tts_chain_and_cache(settings_v2):
    settings = settings_v2.model_copy(update={"tts_provider": "elevenlabs"})
    cloud, offline = FakeCloud(), FakeOffline()
    tts = ReplyTTS(settings, client=cloud, offline=offline)
    first = tts.synthesize("Vitamin C dropped.")
    assert first[:4] == b"RIFF" and cloud.calls == ["Vitamin C dropped."]
    assert tts.synthesize("Vitamin C dropped.") == first and len(cloud.calls) == 1   # cached
    cloud.fail = True
    assert tts.synthesize("Something new.")[:4] == b"RIFF" and offline.calls == ["Something new."]
    tts.synthesize("Another one.")
    assert len(cloud.calls) == 2                                                     # degraded: cloud skipped


def test_reply_tts_keeps_names_off_the_cloud_when_configured(settings_v2):
    settings = settings_v2.model_copy(update={"tts_provider": "elevenlabs", "tts_include_med_names": False})
    cloud, offline = FakeCloud(), FakeOffline()
    tts = ReplyTTS(settings, client=cloud, offline=offline)
    assert tts.synthesize("Calcium dropped.")[:4] == b"RIFF"
    assert cloud.calls == [] and offline.calls == ["Calcium dropped."]


# --------------------------------------------------------------------------- transcribe (fake vosk)


def pcm(seconds: float) -> bytes:
    return struct.pack("<h", 0) * int(16000 * seconds)


@pytest.fixture
def stt_env(db_v2, settings_v2, clock, bus, tmp_path, monkeypatch):
    model_dir = make_model_dir(tmp_path)
    svc, drops, ids = make_service(db_v2, settings_v2, clock, bus, vosk_model_path=model_dir)
    return svc, model_dir, monkeypatch


def test_transcribe_full_vocabulary(stt_env):
    svc, model_dir, monkeypatch = stt_env
    fake = fake_vosk_module(
        [{"text": "drop my", "result": [{"word": "drop", "conf": 0.9}, {"word": "my", "conf": 0.8}]}],
        final={"text": "vitamin c", "result": [{"word": "vitamin", "conf": 1.0}, {"word": "c", "conf": 0.5}]},
    )
    monkeypatch.setitem(sys.modules, "vosk", fake)
    out = svc.transcribe(pcm(1.0) + b"\x00")          # odd byte is dropped
    assert out == {"text": "drop my vitamin c", "confidence": 0.8, "engine": "vosk"}
    rec = fake.recognizers[0]
    assert rec.rate == 16000 and rec.grammar == () and rec.words is True
    assert sum(rec.fed) == 32000 and max(rec.fed) <= 8000
    svc.transcribe(pcm(0.5))
    assert fake.loaded == [str(model_dir)]          # the model is loaded once and cached
    assert svc.status()["stt"] == "loaded"


def test_transcribe_edge_cases(stt_env):
    svc, model_dir, monkeypatch = stt_env
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk_module([]))
    assert svc.transcribe(b"") == {"text": "", "confidence": 0.0, "engine": "vosk"}
    assert svc.transcribe(pcm(0.2)) == {"text": "", "confidence": 0.0, "engine": "vosk"}
    with pytest.raises(AgentInputError):
        svc.transcribe(b"\x00" * (MAX_PCM_BYTES + 2))
    with pytest.raises(AgentInputError):
        svc.transcribe("not bytes")  # type: ignore[arg-type]


def test_transcribe_without_vosk_or_model(db_v2, settings_v2, clock, bus, tmp_path, monkeypatch):
    missing, _, _ = make_service(db_v2, settings_v2, clock, bus, vosk_model_path=tmp_path / "nope")
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk_module([]))
    with pytest.raises(AgentUnavailable, match="no Vosk model"):
        missing.transcribe(pcm(0.1))
    assert missing.preload_speech_model() is False
    stt = VoskTranscriber(settings_v2.model_copy(update={"vosk_model_path": make_model_dir(tmp_path / "m")}))
    monkeypatch.setitem(sys.modules, "vosk", None)    # import vosk -> ImportError
    with pytest.raises(AgentUnavailable, match="vosk cannot be imported"):
        stt.transcribe(pcm(0.1))


def test_preload_speech_model(stt_env):
    svc, model_dir, monkeypatch = stt_env
    fake = fake_vosk_module([])
    monkeypatch.setitem(sys.modules, "vosk", fake)
    assert svc.preload_speech_model() is True and fake.loaded == [str(model_dir)]
    svc.close()
    assert svc.status()["stt"] == "available"
