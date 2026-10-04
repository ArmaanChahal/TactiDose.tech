"""PCM / WAV playback for speech output.

:class:`Player` plays a WAV (or raw 16-bit PCM) blocking the calling thread (the
``speaker`` thread), but stays interruptible. :meth:`Player.stop` or the per-call
``should_stop`` callback ends playback within about one chunk (50 ms).

Backends, in order:

1. ``sounddevice`` + ``numpy`` on the configured output device
   (``settings.audio_output_device``: index or name substring; default device otherwise).
   If the device refuses the sample rate, the audio is resampled to the device's native
   rate.
2. Windows fallback: ``winsound.PlaySound`` (asynchronous, from a temporary file, so it can
   be stopped). This always uses the default output device.

Playback never raises: failures are logged and reported as ``False``.
:class:`NullPlayer` plays nothing. Use it for tests and ``tts_provider="none"``.

Also home of the tiny WAV helpers shared with :mod:`tactidose.audio.cache`.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- WAV helpers


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int      # bytes per sample
    frames: bytes          # raw interleaved sample data

    @property
    def frame_count(self) -> int:
        size = self.channels * self.sample_width
        return len(self.frames) // size if size else 0

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.sample_rate if self.sample_rate else 0.0


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a WAV header."""
    if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
        raise ValueError("invalid PCM parameters")
    frame = channels * sample_width
    usable = len(pcm) - (len(pcm) % frame)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm[:usable])
    return buf.getvalue()


def read_wav(data: bytes) -> WavInfo:
    """Parse a PCM WAV. Raises ``ValueError`` if it is not a readable WAV file."""
    if not is_wav(data):
        raise ValueError("not a WAV file")
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            info = WavInfo(
                sample_rate=wf.getframerate(),
                channels=wf.getnchannels(),
                sample_width=wf.getsampwidth(),
                frames=wf.readframes(wf.getnframes()),
            )
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"unreadable WAV: {exc}") from exc
    if info.sample_rate <= 0 or info.channels <= 0 or info.sample_width not in (1, 2, 3, 4):
        raise ValueError("unsupported WAV format")
    return info


def resolve_output_device(sd: Any, spec: str | None) -> int | None:
    """Output device index for ``spec`` (index or name substring); ``None`` = default.

    Raises ``LookupError`` when ``spec`` is set but matches no output device.
    """
    text = (spec or "").strip()
    if not text:
        return None
    outputs: list[tuple[int, dict[str, Any]]] = []
    for pos, dev in enumerate(list(sd.query_devices())):
        try:
            if int(dev.get("max_output_channels", 0) or 0) > 0:
                outputs.append((int(dev.get("index", pos)), dev))
        except (AttributeError, TypeError, ValueError):
            continue
    if text.isdigit():
        wanted = int(text)
        if any(index == wanted for index, _ in outputs):
            return wanted
        raise LookupError(f"audio device {wanted} is not an output device")
    needle = text.lower()
    for index, dev in outputs:
        if needle in str(dev.get("name", "")).lower():
            return index
    raise LookupError(f"no audio output device matching {text!r}")


def _to_int16(np: Any, info: WavInfo) -> Any:
    """WAV frames -> int16 array shaped (frames, channels)."""
    raw = info.frames[: info.frame_count * info.channels * info.sample_width]
    if info.sample_width == 2:
        samples = np.frombuffer(raw, dtype="<i2")
    elif info.sample_width == 1:
        samples = ((np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8).astype(np.int16)
    elif info.sample_width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        val = np.where(val & 0x800000, val - 0x1000000, val)
        samples = (val >> 8).astype(np.int16)
    else:
        samples = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
    return samples.reshape(-1, info.channels)


def _resample(np: Any, samples: Any, src: int, dst: int) -> Any:
    """Linear-interpolation resampling (speech-quality is plenty here)."""
    n = samples.shape[0]
    if n == 0 or src == dst:
        return samples
    m = max(1, int(round(n * dst / src)))
    x_old = np.arange(n, dtype=np.float64)
    x_new = np.linspace(0.0, n - 1, m)
    cols = [np.interp(x_new, x_old, samples[:, c].astype(np.float64)) for c in range(samples.shape[1])]
    return np.clip(np.stack(cols, axis=1), -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------- players


class Player:
    """Interruptible blocking playback; one sound at a time. Never raises."""

    def __init__(self, device: str | None = None, *, chunk_ms: int = 50,
                 allow_winsound: bool = True) -> None:
        self.device_spec = device
        self.chunk_ms = max(10, int(chunk_ms))
        self.allow_winsound = allow_winsound
        self._stop = threading.Event()
        self._play_lock = threading.Lock()
        self._playing = False
        self._device: int | None = None
        self._device_resolved = False
        self.last_backend: str | None = None
        self.last_error: str | None = None
        self._leftover_files: list[str] = []

    @property
    def is_playing(self) -> bool:
        return self._playing

    def available(self) -> bool:
        try:
            import sounddevice  # noqa: F401, PLC0415 - optional dependency
            return True
        except Exception:  # noqa: BLE001 - ImportError or PortAudio OSError
            return self.allow_winsound and sys.platform == "win32"

    def status(self) -> dict[str, Any]:
        return {"device": self.device_spec, "backend": self.last_backend,
                "playing": self._playing, "error": self.last_error}

    def stop(self) -> None:
        """Interrupt the current playback (safe from any thread)."""
        self._stop.set()

    def play_pcm(self, pcm: bytes, sample_rate: int, channels: int = 1, *,
                 should_stop: Callable[[], bool] | None = None) -> bool:
        try:
            wav = pcm_to_wav(pcm, sample_rate, channels)
        except ValueError as exc:
            log.warning("cannot play PCM: %s", exc)
            return False
        return self.play_wav(wav, should_stop=should_stop)

    def play_wav(self, wav: bytes, *, should_stop: Callable[[], bool] | None = None) -> bool:
        """Play to completion (True) or until stopped / failed (False)."""
        try:
            info = read_wav(wav)
        except ValueError as exc:
            log.warning("cannot play audio: %s", exc)
            return False
        if info.frame_count == 0:
            return True
        with self._play_lock:
            self._stop.clear()
            self._playing = True
            self._cleanup_leftovers()

            def stopped() -> bool:
                if self._stop.is_set():
                    return True
                try:
                    return bool(should_stop and should_stop())
                except Exception:  # noqa: BLE001
                    return False

            try:
                if stopped():
                    return False
                handled, completed = self._play_sounddevice(info, stopped)
                if handled:
                    return completed
                if self.allow_winsound and sys.platform == "win32" and not stopped():
                    return self._play_winsound(wav, info, stopped)
                self.last_error = self.last_error or "no audio backend available"
                return False
            except Exception as exc:  # noqa: BLE001 - playback must never raise
                log.warning("audio playback failed: %s", exc)
                self.last_error = str(exc)
                return False
            finally:
                self._playing = False

    # ------------------------------------------------------------------ backends
    def _output_device(self, sd: Any) -> int | None:
        if not self._device_resolved:
            self._device_resolved = True
            try:
                self._device = resolve_output_device(sd, self.device_spec)
            except Exception as exc:  # noqa: BLE001
                log.warning("audio output device %r not usable (%s); using the default", self.device_spec, exc)
                self._device = None
        return self._device

    def _play_sounddevice(self, info: WavInfo, stopped: Callable[[], bool]) -> tuple[bool, bool]:
        """Returns (handled, completed). handled=False means: try the next backend."""
        try:
            import numpy as np  # noqa: PLC0415 - optional dependency
            import sounddevice as sd  # noqa: PLC0415 - optional dependency
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"sounddevice unavailable: {exc}"
            return False, False
        try:
            samples = _to_int16(np, info)
            device = self._output_device(sd)
            rate = info.sample_rate
            try:
                stream = sd.OutputStream(samplerate=rate, channels=info.channels, dtype="int16", device=device)
            except Exception:  # noqa: BLE001 - e.g. WASAPI refuses non-native rates
                native = int(float(sd.query_devices(device, kind="output")["default_samplerate"]))
                if not native or native == rate:
                    raise
                samples = _resample(np, samples, rate, native)
                rate = native
                stream = sd.OutputStream(samplerate=rate, channels=info.channels, dtype="int16", device=device)
        except Exception as exc:  # noqa: BLE001
            log.warning("sounddevice output unavailable (%s)", exc)
            self.last_error = str(exc)
            return False, False
        self.last_backend = "sounddevice"
        block = max(256, rate * self.chunk_ms // 1000)
        completed = True
        try:
            stream.start()
            for start in range(0, samples.shape[0], block):
                if stopped():
                    completed = False
                    stream.abort()
                    break
                stream.write(samples[start:start + block])
            if completed:
                stream.stop()  # waits until the buffered audio has played
        except Exception as exc:  # noqa: BLE001
            log.warning("sounddevice playback error: %s", exc)
            self.last_error = str(exc)
            completed = False
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        return True, completed

    def _play_winsound(self, wav: bytes, info: WavInfo, stopped: Callable[[], bool]) -> bool:
        try:
            import winsound  # noqa: PLC0415 - Windows only
        except ImportError:
            return False
        self.last_backend = "winsound"
        fd, path = tempfile.mkstemp(prefix="tactidose-tts-", suffix=".wav")
        completed = True
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(wav)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            deadline = time.monotonic() + info.duration_s + 0.25
            while time.monotonic() < deadline:
                if stopped():
                    completed = False
                    break
                time.sleep(0.02)
            winsound.PlaySound(None, 0)  # stop (no-op if already finished)
            return completed
        except Exception as exc:  # noqa: BLE001
            log.warning("winsound playback failed: %s", exc)
            self.last_error = str(exc)
            return False
        finally:
            try:
                os.unlink(path)
            except OSError:
                self._leftover_files.append(path)  # still locked; retried next time

    def _cleanup_leftovers(self) -> None:
        for path in list(self._leftover_files):
            try:
                os.unlink(path)
                self._leftover_files.remove(path)
            except FileNotFoundError:
                self._leftover_files.remove(path)
            except OSError:
                pass


class NullPlayer:
    """Plays nothing (records what it was asked to play). Used for tests and tts 'none'."""

    last_backend = "none"

    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.stops = 0

    @property
    def is_playing(self) -> bool:
        return False

    def available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"device": None, "backend": "none", "playing": False, "error": None}

    def stop(self) -> None:
        self.stops += 1

    def play_wav(self, wav: bytes, *, should_stop: Callable[[], bool] | None = None) -> bool:
        self.played.append(wav)
        return True

    def play_pcm(self, pcm: bytes, sample_rate: int, channels: int = 1, *,
                 should_stop: Callable[[], bool] | None = None) -> bool:
        self.played.append(pcm)
        return True
