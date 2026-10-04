"""PCM/WAV playback (tactidose/audio/playback.py) with fake sounddevice / winsound modules."""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any

import numpy as np
import pytest

from tactidose.audio import playback
from tactidose.audio.playback import (
    NullPlayer,
    Player,
    WavInfo,
    _to_int16,
    is_wav,
    pcm_to_wav,
    read_wav,
    resolve_output_device,
)

OUT_DEVICES = [
    {"index": 0, "name": "Microphone Array", "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 48000.0},
    {"index": 1, "name": "Speakers (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 48000.0},
    {"index": 2, "name": "Kiosk Speaker USB", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 44100.0},
]


class FakeOutputStream:
    instances: list["FakeOutputStream"] = []
    refuse_rates: set[int] = set()
    write_delay_s = 0.0

    def __init__(self, *, samplerate: int, channels: int, dtype: str, device: Any) -> None:
        if samplerate in self.refuse_rates:
            raise RuntimeError(f"Invalid sample rate {samplerate}")
        self.samplerate, self.channels, self.dtype, self.device = samplerate, channels, dtype, device
        self.frames_written = 0
        self.events: list[str] = []
        FakeOutputStream.instances.append(self)

    def start(self) -> None:
        self.events.append("start")

    def write(self, data: Any) -> None:
        assert data.dtype == np.int16 and data.ndim == 2
        self.frames_written += data.shape[0]
        if self.write_delay_s:
            time.sleep(self.write_delay_s)

    def stop(self) -> None:
        self.events.append("stop")

    def abort(self) -> None:
        self.events.append("abort")

    def close(self) -> None:
        self.events.append("close")


def make_sd() -> types.ModuleType:
    sd = types.ModuleType("sounddevice")

    def query_devices(device: Any = None, kind: str | None = None) -> Any:
        if device is None and kind is None:
            return list(OUT_DEVICES)
        if device is None:
            return OUT_DEVICES[1]
        return OUT_DEVICES[int(device)]

    sd.query_devices = query_devices  # type: ignore[attr-defined]
    sd.OutputStream = FakeOutputStream  # type: ignore[attr-defined]
    return sd


@pytest.fixture(autouse=True)
def _reset():
    FakeOutputStream.instances.clear()
    FakeOutputStream.refuse_rates = set()
    FakeOutputStream.write_delay_s = 0.0
    yield


@pytest.fixture
def fake_sd(monkeypatch):
    sd = make_sd()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def tone(n: int = 2205, rate: int = 22050) -> bytes:
    samples = (np.sin(np.arange(n) / 5.0) * 8000).astype("<i2")
    return pcm_to_wav(samples.tobytes(), rate)


# --------------------------------------------------------------------------- WAV helpers


def test_pcm_wav_round_trip_and_duration():
    pcm = b"\x01\x00\x02\x00" * 1000
    wav = pcm_to_wav(pcm, 16000)
    assert is_wav(wav) and not is_wav(pcm)
    info = read_wav(wav)
    assert (info.sample_rate, info.channels, info.sample_width, info.frames) == (16000, 1, 2, pcm)
    assert info.frame_count == 2000 and info.duration_s == pytest.approx(0.125)
    assert read_wav(pcm_to_wav(pcm + b"\x05", 16000)).frames == pcm  # partial frame dropped


@pytest.mark.parametrize("bad", [b"", b"RIFF", b"not a wav at all", b"RIFF\x00\x00\x00\x00WAVEjunk"])
def test_read_wav_rejects_garbage(bad):
    with pytest.raises(ValueError):
        read_wav(bad)


def test_pcm_to_wav_validates_parameters():
    with pytest.raises(ValueError):
        pcm_to_wav(b"\x00\x00", 0)


def test_sample_width_conversions():
    u8 = WavInfo(8000, 1, 1, bytes([0, 128, 255]))
    assert _to_int16(np, u8)[:, 0].tolist() == [-32768, 0, 32512]
    s24 = WavInfo(8000, 1, 3, b"\x00\x00\x80" + b"\x00\x00\x00" + b"\xff\xff\x7f")
    assert _to_int16(np, s24)[:, 0].tolist() == [-32768, 0, 32767]
    s32 = WavInfo(8000, 2, 4, np.array([65536 * 100, -65536 * 100], dtype="<i4").tobytes())
    assert _to_int16(np, s32).tolist() == [[100, -100]]


def test_resolve_output_device():
    sd = make_sd()
    assert resolve_output_device(sd, None) is None
    assert resolve_output_device(sd, "") is None
    assert resolve_output_device(sd, "2") == 2
    assert resolve_output_device(sd, "kiosk") == 2
    with pytest.raises(LookupError):
        resolve_output_device(sd, "0")  # input-only
    with pytest.raises(LookupError):
        resolve_output_device(sd, "hdmi")


# --------------------------------------------------------------------------- Player


def test_player_plays_all_frames_on_selected_device(fake_sd):
    player = Player("Kiosk Speaker")
    assert player.play_wav(tone(2205)) is True
    stream = FakeOutputStream.instances[-1]
    assert (stream.samplerate, stream.channels, stream.dtype, stream.device) == (22050, 1, "int16", 2)
    assert stream.frames_written == 2205
    assert stream.events == ["start", "stop", "close"]
    assert player.last_backend == "sounddevice" and not player.is_playing


def test_unknown_output_device_falls_back_to_default(fake_sd):
    player = Player("HDMI 7")
    assert player.play_wav(tone()) is True
    assert FakeOutputStream.instances[-1].device is None


def test_refused_rate_is_resampled_to_native(fake_sd):
    FakeOutputStream.refuse_rates = {22050}
    player = Player()
    assert player.play_wav(tone(2205, 22050)) is True
    stream = FakeOutputStream.instances[-1]
    assert stream.samplerate == 48000 and stream.frames_written == 4800


def test_stop_interrupts_playback_quickly(fake_sd):
    FakeOutputStream.write_delay_s = 0.02
    player = Player()
    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(player.play_wav(tone(22050 * 5))))
    worker.start()
    time.sleep(0.1)
    started = time.monotonic()
    player.stop()
    worker.join(timeout=2)
    assert result == [False] and time.monotonic() - started < 0.5
    assert "abort" in FakeOutputStream.instances[-1].events


def test_should_stop_callback_interrupts(fake_sd):
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    assert Player().play_wav(tone(22050), should_stop=should_stop) is False
    assert FakeOutputStream.instances[-1].frames_written < 22050


def test_play_pcm_wraps_and_plays(fake_sd):
    assert Player().play_pcm(b"\x00\x01" * 800, 16000) is True
    assert FakeOutputStream.instances[-1].frames_written == 800


def test_garbage_and_empty_audio_never_raise(fake_sd):
    player = Player()
    assert player.play_wav(b"garbage") is False
    assert player.play_wav(pcm_to_wav(b"", 16000)) is True
    assert player.play_pcm(b"\x00\x00", 0) is False


def test_no_backend_returns_false(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    player = Player(allow_winsound=False)
    assert player.play_wav(tone()) is False
    assert player.last_error


class FakeWinsound(types.ModuleType):
    SND_FILENAME, SND_ASYNC, SND_NODEFAULT = 0x20000, 0x0001, 0x0002

    def __init__(self) -> None:
        super().__init__("winsound")
        self.calls: list[tuple[Any, int]] = []

    def PlaySound(self, sound: Any, flags: int) -> None:  # noqa: N802 - winsound API name
        self.calls.append((sound, flags))


def test_winsound_fallback_is_async_and_stoppable(monkeypatch):
    fake = FakeWinsound()
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(playback, "sys", types.SimpleNamespace(platform="win32", modules=sys.modules))
    player = Player()
    assert player.play_wav(tone(441, 22050)) is True  # 20 ms of audio
    (path, flags), (stop_sound, stop_flags) = fake.calls
    assert flags == fake.SND_FILENAME | fake.SND_ASYNC | fake.SND_NODEFAULT
    assert stop_sound is None and stop_flags == 0
    assert player.last_backend == "winsound"
    import os
    assert not os.path.exists(path)  # temp file removed

    fake.calls.clear()
    assert player.play_wav(tone(), should_stop=lambda: True) is False
    assert fake.calls == []  # an already-stale utterance never starts

    checks = {"n": 0}

    def stop_after_start() -> bool:
        checks["n"] += 1
        return checks["n"] > 3

    started = time.monotonic()
    assert player.play_wav(tone(22050 * 10), should_stop=stop_after_start) is False  # 10 s of audio
    assert time.monotonic() - started < 0.5
    assert fake.calls[0][1] & fake.SND_ASYNC and fake.calls[-1] == (None, 0)


def test_null_player_records():
    player = NullPlayer()
    assert player.play_wav(b"RIFF") is True and player.play_pcm(b"\x00\x00", 16000) is True
    player.stop()
    assert len(player.played) == 2 and player.stops == 1 and player.available()
