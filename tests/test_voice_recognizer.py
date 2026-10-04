"""VoiceRecognizer + download_model (tactidose/voice/recognizer.py).

Fake ``vosk`` / ``sounddevice`` modules are injected through ``sys.modules``: no microphone,
model files or network are needed. Audio "blocks" are byte strings pushed through the fake
stream's callback; a block starting with ``UTT:`` makes the fake recognizer return the JSON
that follows as a final result.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tactidose.core.bus import EventBus, Topic
from tactidose.voice import recognizer as rec_mod
from tactidose.voice.intents import GRAMMAR_PHRASES
from tactidose.voice.recognizer import (
    DOWNLOAD_HINT,
    VoiceRecognizer,
    download_model,
    looks_like_vosk_model,
    resolve_input_device,
)
from tests.fakes import wait_until

# --------------------------------------------------------------------------- fakes


class FakeModel:
    unknown_words: set[str] = set()

    def __init__(self, path: str) -> None:
        self.path = path

    def vosk_model_find_word(self, word: str) -> int:
        return -1 if word in self.unknown_words else 7


class FakeKaldi:
    instances: list["FakeKaldi"] = []

    def __init__(self, model: FakeModel, rate: float, grammar: str | None = None) -> None:
        self.model = model
        self.rate = rate
        self.grammar = json.loads(grammar) if grammar is not None else None
        self.words = False
        self.fed: list[bytes] = []
        self.resets = 0
        self._result = "{}"
        FakeKaldi.instances.append(self)

    def SetWords(self, enabled: bool) -> None:  # noqa: N802 - Vosk API name
        self.words = enabled

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802
        self.fed.append(data)
        if data.startswith(b"UTT:"):
            self._result = data[4:].decode("utf-8")
            return True
        return False

    def Result(self) -> str:  # noqa: N802
        return self._result

    def Reset(self) -> None:  # noqa: N802
        self.resets += 1


class FakeStream:
    instances: list["FakeStream"] = []
    refuse_rates: set[int] = set()

    def __init__(self, *, samplerate: int, blocksize: int, device: Any, dtype: str, channels: int,
                 callback: Any) -> None:
        if samplerate in self.refuse_rates:
            raise RuntimeError(f"Invalid sample rate {samplerate}")
        self.samplerate, self.blocksize, self.device = samplerate, blocksize, device
        self.dtype, self.channels, self.callback = dtype, channels, callback
        self.started = self.stopped = self.closed = False
        FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def push(self, data: bytes) -> None:
        self.callback(data, len(data) // 2, None, None)


DEVICES = [
    {"index": 0, "name": "Speakers (Realtek)", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 48000.0},
    {"index": 1, "name": "Microphone Array (Realtek)", "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 48000.0},
    {"index": 2, "name": "USB Headset Mic", "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 44100.0},
]


def make_sd(devices: list[dict[str, Any]] | None = None, *, default_input: int | None = 1) -> types.ModuleType:
    devs = DEVICES if devices is None else devices
    sd = types.ModuleType("sounddevice")

    def query_devices(device: Any = None, kind: str | None = None) -> Any:
        if device is None and kind is None:
            return list(devs)
        if device is None:
            if default_input is None:
                raise RuntimeError("Error querying device -1")
            return devs[default_input]
        return devs[int(device)]

    sd.query_devices = query_devices  # type: ignore[attr-defined]
    sd.RawInputStream = FakeStream  # type: ignore[attr-defined]
    return sd


def make_vosk() -> types.ModuleType:
    vosk = types.ModuleType("vosk")
    vosk.Model = FakeModel  # type: ignore[attr-defined]
    vosk.KaldiRecognizer = FakeKaldi  # type: ignore[attr-defined]
    vosk.log_levels = []  # type: ignore[attr-defined]
    vosk.SetLogLevel = lambda level: vosk.log_levels.append(level)  # type: ignore[attr-defined]
    return vosk


def make_model_dir(root: Path, name: str = "vosk-model-small-en-us-0.15") -> Path:
    model = root / name
    (model / "am").mkdir(parents=True)
    (model / "conf").mkdir()
    (model / "am" / "final.mdl").write_bytes(b"mdl")
    (model / "conf" / "mfcc.conf").write_text("--use-energy=false\n")
    return model


def result_json(text: str, confs: list[float] | None = None) -> bytes:
    words = text.split()
    confs = confs if confs is not None else [1.0] * len(words)
    payload = {"text": text, "result": [{"word": w, "conf": c, "start": 0.0, "end": 0.1}
                                        for w, c in zip(words, confs)]}
    return b"UTT:" + json.dumps(payload).encode("utf-8")


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeKaldi.instances.clear()
    FakeStream.instances.clear()
    FakeStream.refuse_rates = set()
    FakeModel.unknown_words = set()
    yield


@pytest.fixture
def fake_modules(monkeypatch):
    sd, vosk = make_sd(), make_vosk()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setitem(sys.modules, "vosk", vosk)
    return sd, vosk


@pytest.fixture
def voice_settings(settings, tmp_path):
    model = make_model_dir(tmp_path / "models")
    return settings.model_copy(update={"voice_enabled": True, "vosk_model_path": model})


class Harness:
    def __init__(self, settings, bus: EventBus) -> None:
        self.heard: list[tuple[str, float]] = []
        self.muted = False
        self.bus = bus
        self.sub = bus.subscribe([Topic.VOICE_HEARD, Topic.VOICE_STATUS])
        self.rec = VoiceRecognizer(settings, on_text=self.on_text, is_muted=lambda: self.muted, bus=bus)
        self.clock = Clock()
        self.rec._now = self.clock

    def on_text(self, text: str, conf: float) -> None:
        self.heard.append((text, conf))

    @property
    def stream(self) -> FakeStream:
        return FakeStream.instances[-1]

    @property
    def kaldi(self) -> FakeKaldi:
        return FakeKaldi.instances[-1]

    def events(self, topic: str) -> list[dict[str, Any]]:
        return [e.data for e in self.sub.drain() if e.topic == topic]


@pytest.fixture
def harness(voice_settings, bus):
    made: list[Harness] = []

    def make(settings=None) -> Harness:
        h = Harness(settings or voice_settings, bus)
        made.append(h)
        return h

    yield make
    for h in made:
        h.rec.close()


# --------------------------------------------------------------------------- start(): unavailable paths


def test_disabled_returns_false_and_publishes_status(settings, bus):
    sub = bus.subscribe([Topic.VOICE_STATUS])
    rec = VoiceRecognizer(settings, on_text=lambda t, c: None, is_muted=lambda: False, bus=bus)
    assert rec.start() is False
    st = rec.status()
    assert st["enabled"] is False and st["listening"] is False and "disabled" in st["error"]
    events = [e.data for e in sub.drain()]
    assert events and events[-1]["listening"] is False and events[-1]["error"]
    rec.close()


def test_missing_model_gives_download_hint(settings, bus, tmp_path, fake_modules):
    s = settings.model_copy(update={"voice_enabled": True, "vosk_model_path": tmp_path / "nope"})
    sub = bus.subscribe([Topic.VOICE_STATUS])
    rec = VoiceRecognizer(s, on_text=lambda t, c: None, is_muted=lambda: False, bus=bus)
    assert rec.start() is False
    status = sub.drain()[-1].data
    assert status["hint"] == DOWNLOAD_HINT == "run: python -m tactidose download-voice-model"
    assert "not found" in status["error"]
    assert FakeStream.instances == []


def test_invalid_model_directory(settings, tmp_path, fake_modules):
    (tmp_path / "empty-model").mkdir()
    s = settings.model_copy(update={"voice_enabled": True, "vosk_model_path": tmp_path / "empty-model"})
    rec = VoiceRecognizer(s, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False and "not a valid Vosk model" in rec.status()["error"]


def test_import_error_returns_false(voice_settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "vosk", None)  # makes `import vosk` raise ImportError
    monkeypatch.setitem(sys.modules, "sounddevice", make_sd())
    rec = VoiceRecognizer(voice_settings, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False
    assert "dependencies unavailable" in rec.status()["error"] and "tactidose[voice]" in rec.status()["hint"]


def test_no_input_device_returns_false(voice_settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "vosk", make_vosk())
    monkeypatch.setitem(sys.modules, "sounddevice", make_sd([DEVICES[0]], default_input=None))
    rec = VoiceRecognizer(voice_settings, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False and "no microphone" in rec.status()["error"]


def test_unknown_mic_name_returns_false(voice_settings, fake_modules):
    s = voice_settings.model_copy(update={"mic_device": "Studio Condenser"})
    rec = VoiceRecognizer(s, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False and "Studio Condenser" in rec.status()["error"]


def test_model_load_failure_returns_false(voice_settings, fake_modules):
    _, vosk = fake_modules

    def boom(path: str) -> None:
        raise Exception("Failed to create a model")

    vosk.Model = boom
    rec = VoiceRecognizer(voice_settings, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False and "could not load" in rec.status()["error"]


def test_start_never_raises_on_unexpected_errors(voice_settings, fake_modules):
    sd, _ = fake_modules
    sd.query_devices = None  # calling it raises TypeError deep inside start()
    rec = VoiceRecognizer(voice_settings, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False and rec.status()["error"]


# --------------------------------------------------------------------------- device selection


def test_resolve_input_device_by_default_index_and_name():
    sd = make_sd()
    assert resolve_input_device(sd, None) == (None, "Microphone Array (Realtek)")
    assert resolve_input_device(sd, "  ") == (None, "Microphone Array (Realtek)")
    assert resolve_input_device(sd, "2") == (2, "USB Headset Mic")
    assert resolve_input_device(sd, "headset") == (2, "USB Headset Mic")
    with pytest.raises(LookupError):
        resolve_input_device(sd, "0")  # output-only device
    with pytest.raises(LookupError):
        resolve_input_device(sd, "nothing like this")


def test_mic_selected_by_name_substring(harness, voice_settings, fake_modules):
    h = harness(voice_settings.model_copy(update={"mic_device": "usb headset"}))
    assert h.rec.start() is True
    assert h.stream.device == 2 and h.rec.status()["device"] == "USB Headset Mic"


# --------------------------------------------------------------------------- start(): success


def test_start_opens_16k_mono_int16_stream_with_grammar(harness, fake_modules):
    _, vosk = fake_modules
    h = harness()
    assert h.rec.start() is True
    assert h.rec.start() is True  # idempotent
    assert len(FakeStream.instances) == 1
    s = h.stream
    assert (s.samplerate, s.dtype, s.channels, s.blocksize) == (16000, "int16", 1, 1600) and s.started
    k = h.kaldi
    assert k.rate == 16000 and k.words is True
    assert k.grammar == GRAMMAR_PHRASES and k.grammar[-1] == "[unk]"
    assert -1 in vosk.log_levels
    st = h.rec.status()
    assert st["listening"] is True and st["error"] is None and st["grammar_phrases"] == len(GRAMMAR_PHRASES)
    assert any(t.name == "voice" for t in threading.enumerate())
    assert h.events(Topic.VOICE_STATUS)[-1]["listening"] is True


def test_grammar_drops_phrases_unknown_to_the_model(harness, fake_modules):
    FakeModel.unknown_words = {"dispense"}
    h = harness()
    assert h.rec.start()
    grammar = h.kaldi.grammar
    assert "dispense" not in " ".join(grammar) and "open it" in grammar and grammar[-1] == "[unk]"


def test_full_vocabulary_mode(harness, voice_settings, fake_modules):
    h = harness(voice_settings.model_copy(update={"voice_use_grammar": False}))
    assert h.rec.start()
    assert h.kaldi.grammar is None and h.kaldi.words is True


def test_falls_back_to_native_sample_rate(harness, fake_modules):
    FakeStream.refuse_rates = {16000}
    h = harness()
    assert h.rec.start() is True
    assert h.stream.samplerate == 48000 and h.kaldi.rate == 48000  # Vosk resamples internally


def test_microphone_open_failure_returns_false(voice_settings, fake_modules):
    FakeStream.refuse_rates = {16000, 48000}
    rec = VoiceRecognizer(voice_settings, on_text=lambda t, c: None, is_muted=lambda: False)
    assert rec.start() is False and "could not open the microphone" in rec.status()["error"]


# --------------------------------------------------------------------------- recognition


def test_final_result_calls_on_text_with_mean_confidence(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    h.stream.push(b"\x00\x00" * 160)
    h.stream.push(result_json("what do i take now", [1.0, 0.9, 0.8, 1.0, 0.8]))
    assert wait_until(lambda: h.heard)
    assert h.heard == [("what do i take now", 0.9)]
    heard = h.events(Topic.VOICE_HEARD)
    assert heard == [{"text": "what do i take now", "confidence": 0.9, "intent": "CHECK_DUE", "accepted": True}]
    assert h.rec.status()["accepted"] == 1 and h.rec.status()["last_heard"]["text"] == "what do i take now"


def test_low_confidence_is_published_but_not_forwarded(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    h.stream.push(result_json("taken", [0.4]))
    assert wait_until(lambda: h.rec.status()["rejected"] == 1)
    assert h.heard == []
    assert h.events(Topic.VOICE_HEARD)[-1]["accepted"] is False


def test_low_confidence_stop_is_still_accepted(harness, fake_modules):
    h = harness()  # min confidence 0.55 -> stop accepted from 0.33
    assert h.rec.start()
    h.stream.push(result_json("stop", [0.4]))
    h.stream.push(result_json("stop", [0.2]))
    assert wait_until(lambda: h.rec.status()["heard"] == 2)
    assert h.heard == [("stop", 0.4)]


def test_unk_only_and_empty_results_are_ignored(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    h.stream.push(result_json("[unk]"))
    h.stream.push(result_json("[unk] [unk]"))
    h.stream.push(b'UTT:{"text": ""}')
    h.stream.push(b"UTT:not json")
    h.stream.push(result_json("help"))
    assert wait_until(lambda: h.heard)
    assert h.heard == [("help", 1.0)]
    assert h.rec.status()["heard"] == 1


def test_text_with_unk_is_forwarded_verbatim(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    h.stream.push(result_json("that [unk]", [0.83, 1.0]))
    assert wait_until(lambda: h.heard)
    assert h.heard == [("that [unk]", 0.915)]  # the assistant decides it is noise


def test_missing_word_confidences_fail_closed(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    h.stream.push(b'UTT:{"text": "taken"}')
    assert wait_until(lambda: h.rec.status()["rejected"] == 1)
    assert h.heard == []


def test_on_text_exception_does_not_stop_listening(harness, fake_modules):
    h = harness()
    calls: list[str] = []

    def flaky(text: str, conf: float) -> None:
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("assistant exploded")

    h.rec._on_text = flaky
    assert h.rec.start()
    h.stream.push(result_json("help"))
    h.stream.push(result_json("repeat"))
    assert wait_until(lambda: len(calls) == 2)


# --------------------------------------------------------------------------- half-duplex


def test_mute_drops_audio_resets_on_unmute_and_honours_tail(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    k = h.kaldi
    h.stream.push(b"A")                                    # t=100, live
    assert wait_until(lambda: k.fed == [b"A"])
    h.muted = True
    h.clock.t = 101.0
    h.stream.push(result_json("taken"))                    # our own TTS: dropped
    assert wait_until(lambda: h.rec.status()["muted_blocks"] >= 1)
    assert wait_until(lambda: h.rec.status()["muted"] is True)
    h.clock.t = 102.0                                      # speaker finished
    h.muted = False
    h.stream.push(b"C")                                    # inside the 400 ms tail: dropped
    assert wait_until(lambda: k.resets == 1)
    assert wait_until(lambda: h.rec.status()["muted"] is False)
    h.clock.t = 102.3
    h.stream.push(b"D")                                    # still inside the tail
    h.clock.t = 102.5
    h.stream.push(b"E")                                    # after the tail: fed
    assert wait_until(lambda: k.fed[-1:] == [b"E"])
    assert k.fed == [b"A", b"E"]
    assert h.heard == []
    statuses = h.events(Topic.VOICE_STATUS)
    assert [s["muted"] for s in statuses if s["listening"]][-2:] == [True, False]


def test_blocks_captured_while_muted_are_dropped_even_if_processed_later(harness, fake_modules):
    h = harness()
    h.rec.mute_tail_s = 0.4
    assert h.rec.start()
    k = h.kaldi
    h.muted = True
    h.clock.t = 200.0
    h.stream.push(b"X")
    assert wait_until(lambda: h.rec.status()["muted"] is True)
    h.muted = False
    h.clock.t = 200.1
    assert wait_until(lambda: k.resets == 1)               # unmute noticed via the idle poll
    h.rec._callback(b"OLD", 1, None, None)                 # captured at 200.1 (< 200.5)
    h.clock.t = 201.0
    h.stream.push(b"NEW")
    assert wait_until(lambda: k.fed == [b"NEW"])


def test_is_muted_errors_fail_closed(harness, fake_modules):
    h = harness()

    def broken() -> bool:
        raise RuntimeError("speaker gone")

    h.rec._is_muted = broken
    assert h.rec.start()
    h.stream.push(result_json("taken"))
    assert wait_until(lambda: h.rec.status()["muted_blocks"] >= 1)
    assert h.kaldi.fed == [] and h.heard == []


def test_bounded_queue_drops_when_full(voice_settings):
    rec = VoiceRecognizer(voice_settings, on_text=lambda t, c: None, is_muted=lambda: False)
    for _ in range(rec.queue_blocks + 5):
        rec._callback(b"\x00\x00", 1, None, None)
    assert rec.status()["dropped_blocks"] == 5


def test_close_is_idempotent_and_releases_the_stream(harness, fake_modules):
    h = harness()
    assert h.rec.start()
    stream = h.stream
    h.rec.close()
    h.rec.close()
    assert stream.stopped and stream.closed
    assert not any(t.name == "voice" and t.is_alive() for t in threading.enumerate())
    assert h.events(Topic.VOICE_STATUS)[-1]["listening"] is False
    assert h.rec.start() is True  # can be restarted
    h.rec.close()


# --------------------------------------------------------------------------- download_model


def build_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


MODEL_FILES = {
    "vosk-model-small-en-us-0.15/am/final.mdl": b"m" * 1000,
    "vosk-model-small-en-us-0.15/conf/mfcc.conf": b"--use-energy=false\n",
    "vosk-model-small-en-us-0.15/conf/model.conf": b"--min-active=200\n",
    "vosk-model-small-en-us-0.15/graph/HCLr.fst": b"f" * 500,
    "vosk-model-small-en-us-0.15/README": b"readme",
}


class FakeResponse:
    def __init__(self, data: bytes, *, status: int = 200, length: int | None = -1) -> None:
        self._buf = io.BytesIO(data)
        self.status = status
        size = len(data) if length == -1 else length
        self.headers = {} if size is None else {"Content-Length": str(size)}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def opener(monkeypatch):
    state: dict[str, Any] = {"calls": [], "response": None}

    def fake_urlopen(request: Any, timeout: float | None = None) -> FakeResponse:
        state["calls"].append((request.full_url, timeout))
        return state["response"]

    monkeypatch.setattr(rec_mod, "urlopen", fake_urlopen)
    return state


def test_download_model_streams_extracts_and_verifies(tmp_path, opener):
    data = build_zip(MODEL_FILES)
    opener["response"] = FakeResponse(data)
    progress: list[tuple[int, int | None]] = []
    path = download_model(tmp_path / "models", progress=lambda done, total: progress.append((done, total)))
    assert path == tmp_path / "models" / "vosk-model-small-en-us-0.15"
    assert looks_like_vosk_model(path) and (path / "graph" / "HCLr.fst").read_bytes() == b"f" * 500
    assert opener["calls"][0][0] == "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
    assert progress and progress[-1] == (len(data), len(data))
    leftovers = sorted(p.name for p in (tmp_path / "models").iterdir())
    assert leftovers == ["vosk-model-small-en-us-0.15"]  # no .part / staging dirs


def test_download_model_is_idempotent(tmp_path, opener):
    make_model_dir(tmp_path)
    assert download_model(tmp_path) == tmp_path / "vosk-model-small-en-us-0.15"
    assert opener["calls"] == []


def test_download_model_custom_name_and_flat_archive(tmp_path, opener):
    flat = {k.split("/", 1)[1]: v for k, v in MODEL_FILES.items()}
    opener["response"] = FakeResponse(build_zip(flat), length=None)
    path = download_model(tmp_path, name="my-model", url="https://example.invalid/m.zip")
    assert path == tmp_path / "my-model" and looks_like_vosk_model(path)
    assert opener["calls"][0][0] == "https://example.invalid/m.zip"


@pytest.mark.parametrize(
    "response,message",
    [
        (FakeResponse(b"this is not a zip file"), "not a valid zip"),
        (FakeResponse(build_zip({"readme.txt": b"hello"})), "does not contain a Vosk model"),
        (FakeResponse(build_zip({"../evil.txt": b"x", **MODEL_FILES})), "unsafe path"),
        (FakeResponse(build_zip(MODEL_FILES), length=10), "incomplete"),
        (FakeResponse(b"", status=404), "HTTP 404"),
    ],
)
def test_download_model_failures_leave_nothing_behind(tmp_path, opener, response, message):
    opener["response"] = response
    with pytest.raises(RuntimeError, match=message):
        download_model(tmp_path / "models")
    left = list((tmp_path / "models").iterdir()) if (tmp_path / "models").exists() else []
    assert left == []
    assert not (tmp_path / "evil.txt").exists()


def test_download_model_refuses_to_overwrite_foreign_directory(tmp_path, opener):
    (tmp_path / "vosk-model-small-en-us-0.15").mkdir()
    (tmp_path / "vosk-model-small-en-us-0.15" / "notes.txt").write_text("mine")
    with pytest.raises(RuntimeError, match="not a valid Vosk model"):
        download_model(tmp_path)
    assert opener["calls"] == []
    assert (tmp_path / "vosk-model-small-en-us-0.15" / "notes.txt").read_text() == "mine"
