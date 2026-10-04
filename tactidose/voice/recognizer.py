"""Offline voice input: microphone -> Vosk -> text -> callback (half-duplex).

``VoiceRecognizer`` listens on a 16 kHz mono int16 ``sounddevice.RawInputStream``. The
PortAudio callback only copies each block into a bounded queue; when the queue is full the
block is dropped. The ``voice`` thread feeds the blocks to a Vosk ``KaldiRecognizer``.

* **Grammar mode** (``voice_use_grammar``, the default) restricts Vosk to
  :data:`~tactidose.voice.intents.GRAMMAR_PHRASES`. This is much more robust for a small
  command set. Phrases containing words that the loaded model does not know are dropped at
  start-up.
* **Half-duplex.** Audio is discarded while ``is_muted()`` is true (the speaker is talking)
  and for :attr:`VoiceRecognizer.mute_tail_s` (400 ms) after it clears. The recognizer is
  ``Reset()`` on unmute, so we never react to our own TTS.
* Final results become text plus the mean word confidence. Empty / ``[unk]``-only results
  are ignored. Every other result is published as ``Topic.VOICE_HEARD`` and, when accepted
  (confidence >= ``voice_min_confidence``), passed to ``on_text(text, confidence)``.
  Safety: a "stop"/"cancel" is accepted at a lower threshold (:attr:`cancel_confidence_factor`),
  because stopping is the safe direction.

``start()`` never raises. It returns ``False`` and publishes ``Topic.VOICE_STATUS`` with
the reason when voice is disabled, the model is missing (hint: ``run: python -m tactidose
download-voice-model``), there is no microphone, or vosk/sounddevice cannot be imported.
``vosk`` and ``sounddevice`` are imported lazily, so the rest of the app works without them.

:func:`download_model` fetches and unpacks the Vosk model. It is used by the
``download-voice-model`` CLI command.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.interfaces import Intent
from tactidose.voice.intents import GRAMMAR_PHRASES, parse_intent

log = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "vosk-model-small-en-us-0.15"
DEFAULT_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
DOWNLOAD_HINT = "run: python -m tactidose download-voice-model"
INSTALL_HINT = "install the voice extras: pip install 'tactidose[voice]'"
_CHUNK_BYTES = 256 * 1024


def looks_like_vosk_model(path: Path | str) -> bool:
    """True if ``path`` is a directory with the files Vosk needs (``final.mdl`` + conf)."""
    p = Path(path)
    if not p.is_dir():
        return False
    has_am = (p / "am" / "final.mdl").is_file() or (p / "final.mdl").is_file()
    has_conf = (p / "conf").is_dir() or (p / "mfcc.conf").is_file()
    return has_am and has_conf


def resolve_input_device(sd: Any, spec: str | None) -> tuple[int | None, str]:
    """Pick the microphone: ``None``/blank = system default, digits = device index,
    otherwise a case-insensitive name substring. Returns ``(index or None, name)``.

    Raises :class:`LookupError` with a human-readable reason when nothing suitable exists.
    """
    try:
        devices = list(sd.query_devices())
    except Exception as exc:  # noqa: BLE001 - PortAudio errors vary by platform
        raise LookupError(f"cannot list audio devices: {exc}") from exc
    inputs: list[tuple[int, dict[str, Any]]] = []
    for pos, dev in enumerate(devices):
        try:
            if int(dev.get("max_input_channels", 0) or 0) > 0:
                inputs.append((int(dev.get("index", pos)), dev))
        except (AttributeError, TypeError, ValueError):
            continue
    if not inputs:
        raise LookupError("no microphone (audio input device) found")
    text = (spec or "").strip()
    if not text:
        try:
            info = sd.query_devices(kind="input")
        except Exception as exc:  # noqa: BLE001
            raise LookupError(f"no default microphone: {exc}") from exc
        return None, str(info.get("name", "default input"))
    if text.isdigit():
        wanted = int(text)
        for index, dev in inputs:
            if index == wanted:
                return index, str(dev.get("name", index))
        raise LookupError(f"audio device {wanted} is not an input device")
    needle = text.lower()
    for index, dev in inputs:
        if needle in str(dev.get("name", "")).lower():
            return index, str(dev.get("name"))
    raise LookupError(f"no microphone matching {text!r}")


class VoiceRecognizer:
    """Vosk + sounddevice listener. ``start()`` returns False when voice is unavailable."""

    #: Audio is ignored for this long after the speaker stops (room echo, output latency).
    mute_tail_s: float = 0.4
    #: Microphone block length.
    block_s: float = 0.1
    #: Bounded audio queue (blocks); the callback drops audio when it is full.
    queue_blocks: int = 50
    #: CANCEL is accepted down to ``voice_min_confidence * cancel_confidence_factor``.
    cancel_confidence_factor: float = 0.6

    def __init__(
        self,
        settings: Settings,
        *,
        on_text: Callable[[str, float], None],
        is_muted: Callable[[], bool],
        bus: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self._on_text = on_text
        self._is_muted = is_muted
        self._bus = bus
        self._now: Callable[[], float] = time.monotonic
        self._lock = threading.RLock()
        self._queue: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=self.queue_blocks)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: Any = None
        self._model: Any = None
        self._rec: Any = None
        self._rate = int(settings.voice_sample_rate)
        self._device_name: str | None = None
        self._error: str | None = None
        self._hint: str | None = None
        self._listening = False
        self._muted = False
        self._grammar_size: int | None = None
        self._mute_error_logged = False
        self._stats = {"heard": 0, "accepted": 0, "rejected": 0, "dropped_blocks": 0,
                       "muted_blocks": 0, "stream_warnings": 0}
        self._last_heard: dict[str, Any] | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Open the microphone and start the ``voice`` thread. Idempotent; never raises."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            try:
                ok = self._start_locked()
            except Exception as exc:  # noqa: BLE001 - start() must never raise
                log.exception("voice recognizer failed to start")
                ok = self._fail(f"voice start failed: {type(exc).__name__}: {exc}")
        self._publish_status()
        return ok

    def close(self) -> None:
        """Stop listening and join the thread. Idempotent; never raises."""
        with self._lock:
            thread, stream = self._thread, self._stream
            was_listening = self._listening
            self._stop.set()
            self._thread = None
            self._stream = None
            self._listening = False
        try:
            self._queue.put_nowait((0.0, b""))  # wake the voice thread now
        except queue.Full:
            pass
        if stream is not None:
            for action in ("stop", "close"):
                try:
                    getattr(stream, action)()
                except Exception:  # noqa: BLE001
                    log.debug("microphone stream %s failed", action, exc_info=True)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._rec = None
        self._model = None
        if was_listening:
            self._publish_status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.settings.voice_enabled,
                "listening": self._listening,
                "muted": self._muted,
                "error": self._error,
                "hint": self._hint,
                "model_path": str(self.settings.vosk_model_path),
                "device": self._device_name,
                "sample_rate": self._rate,
                "grammar": self.settings.voice_use_grammar,
                "grammar_phrases": self._grammar_size,
                "min_confidence": self.settings.voice_min_confidence,
                "last_heard": dict(self._last_heard) if self._last_heard else None,
                **self._stats,
            }

    # ------------------------------------------------------------------ start helpers
    def _fail(self, error: str, hint: str | None = None) -> bool:
        self._error = error
        self._hint = hint
        self._listening = False
        level = logging.INFO if not self.settings.voice_enabled else logging.WARNING
        log.log(level, "voice input unavailable: %s%s", error, f" ({hint})" if hint else "")
        return False

    def _start_locked(self) -> bool:
        self._error = self._hint = None
        s = self.settings
        if not s.voice_enabled:
            return self._fail("voice input is disabled (TACTIDOSE_VOICE_ENABLED=false)")
        model_path = Path(s.vosk_model_path)
        if not model_path.exists():
            return self._fail(f"Vosk model not found at {model_path}", DOWNLOAD_HINT)
        if not looks_like_vosk_model(model_path):
            return self._fail(f"{model_path} is not a valid Vosk model", DOWNLOAD_HINT)
        try:
            import sounddevice as sd  # noqa: PLC0415 - optional dependency
            import vosk  # noqa: PLC0415 - optional dependency
        except Exception as exc:  # noqa: BLE001 - ImportError or a PortAudio/DLL OSError
            return self._fail(f"voice dependencies unavailable ({type(exc).__name__}: {exc})", INSTALL_HINT)
        try:
            device, device_name = resolve_input_device(sd, s.mic_device)
        except LookupError as exc:
            return self._fail(str(exc))
        try:
            if hasattr(vosk, "SetLogLevel"):
                vosk.SetLogLevel(-1)
            model = vosk.Model(str(model_path))
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not load the Vosk model: {exc}", DOWNLOAD_HINT)
        try:
            stream, rate = self._open_stream(sd, device)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not open the microphone: {exc}")
        try:
            rec = self._make_recognizer(vosk, model, rate)
            self._drain_queue()
            self._stop.clear()
            stream.start()
        except Exception as exc:  # noqa: BLE001
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            return self._fail(f"could not start speech recognition: {exc}")
        self._model, self._rec, self._stream, self._rate = model, rec, stream, rate
        self._device_name = device_name
        self._listening = True
        self._muted = False
        self._thread = threading.Thread(target=self._run, args=(rec,), name="voice", daemon=True)
        self._thread.start()
        log.info("voice listening on %r at %d Hz (%s)", device_name, rate,
                 f"grammar, {self._grammar_size} phrases" if s.voice_use_grammar else "full vocabulary")
        return True

    def _open_stream(self, sd: Any, device: int | None) -> tuple[Any, int]:
        rates = [int(self.settings.voice_sample_rate)]
        try:
            info = sd.query_devices(device, kind="input")
            native = int(float(info.get("default_samplerate") or 0))
            if native and native not in rates:
                rates.append(native)  # Vosk resamples internally
        except Exception:  # noqa: BLE001
            pass
        last: Exception | None = None
        for rate in rates:
            try:
                stream = sd.RawInputStream(
                    samplerate=rate,
                    blocksize=max(160, int(rate * self.block_s)),
                    device=device,
                    dtype="int16",
                    channels=1,
                    callback=self._callback,
                )
                return stream, rate
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.debug("microphone refused %d Hz: %s", rate, exc)
        raise RuntimeError(str(last) if last else "no usable sample rate")

    def _make_recognizer(self, vosk: Any, model: Any, rate: int) -> Any:
        if self.settings.voice_use_grammar:
            phrases = self._grammar_for(model)
            self._grammar_size = len(phrases)
            rec = vosk.KaldiRecognizer(model, rate, json.dumps(phrases))
        else:
            self._grammar_size = None
            rec = vosk.KaldiRecognizer(model, rate)
        rec.SetWords(True)
        return rec

    @staticmethod
    def _grammar_for(model: Any) -> list[str]:
        """GRAMMAR_PHRASES minus phrases with words this model does not know."""
        find = getattr(model, "vosk_model_find_word", None)
        if find is None:
            return list(GRAMMAR_PHRASES)
        kept: list[str] = []
        dropped: list[str] = []
        for phrase in GRAMMAR_PHRASES:
            try:
                ok = all(w == "[unk]" or find(w) >= 0 for w in phrase.split())
            except Exception:  # noqa: BLE001
                ok = True
            (kept if ok else dropped).append(phrase)
        if dropped:
            log.warning("grammar phrases not in the model vocabulary (ignored): %s", dropped)
        if "[unk]" not in kept:
            kept.append("[unk]")
        return kept

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    # ------------------------------------------------------------------ audio path
    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """PortAudio thread: copy the block and return immediately."""
        if status:
            self._stats["stream_warnings"] += 1
        try:
            self._queue.put_nowait((self._now(), bytes(indata)))
        except queue.Full:
            self._stats["dropped_blocks"] += 1

    def _muted_now(self) -> bool:
        try:
            return bool(self._is_muted())
        except Exception:  # noqa: BLE001 - fail closed: never listen to ourselves
            if not self._mute_error_logged:
                self._mute_error_logged = True
                log.exception("is_muted() failed; treating the microphone as muted")
            return True

    def _set_muted(self, muted: bool) -> None:
        with self._lock:
            changed = self._muted != muted
            self._muted = muted
        if changed:
            self._publish_status()

    def _run(self, rec: Any) -> None:
        was_muted = False
        drop_until = 0.0
        while not self._stop.is_set():
            try:
                captured, data = self._queue.get(timeout=0.2)
            except queue.Empty:
                captured, data = None, b""
            if self._stop.is_set():
                break
            now = self._now()
            if self._muted_now():
                if not was_muted:
                    was_muted = True
                    self._set_muted(True)
                drop_until = now + self.mute_tail_s
                if data:
                    self._stats["muted_blocks"] += 1
                continue
            if was_muted:
                was_muted = False
                drop_until = now + self.mute_tail_s
                try:
                    rec.Reset()
                except Exception:  # noqa: BLE001
                    log.debug("recognizer Reset() failed", exc_info=True)
                self._set_muted(False)
            if captured is None or not data:
                continue
            if captured < drop_until:
                self._stats["muted_blocks"] += 1
                continue
            try:
                final = rec.AcceptWaveform(data)
                result = rec.Result() if final else None
            except Exception:  # noqa: BLE001 - keep listening
                log.exception("Vosk failed to process audio")
                continue
            if result:
                self._handle_result(result)

    def _handle_result(self, raw: str) -> None:
        try:
            res = json.loads(raw)
        except (TypeError, ValueError):
            return
        text = " ".join(str(res.get("text", "")).split())
        tokens = text.split()
        if not tokens or all(t == "[unk]" for t in tokens):
            return
        confs = [float(w.get("conf", 0.0)) for w in (res.get("result") or []) if isinstance(w, dict)]
        confidence = round(sum(confs) / len(confs), 3) if confs else 0.0  # unknown -> fail closed
        parsed = parse_intent(text, confidence=confidence)
        threshold = float(self.settings.voice_min_confidence)
        accepted = confidence >= threshold or (
            parsed.intent is Intent.CANCEL and confidence >= threshold * self.cancel_confidence_factor
        )
        heard = {"text": text, "confidence": confidence, "intent": parsed.intent.value, "accepted": accepted}
        with self._lock:
            self._stats["heard"] += 1
            self._stats["accepted" if accepted else "rejected"] += 1
            self._last_heard = {**heard, "at": datetime.now(timezone.utc).isoformat()}
        log.info("heard %r (confidence %.2f, %s)%s", text, confidence, parsed.intent.value,
                 "" if accepted else " - below threshold, ignored")
        if self._bus is not None:
            self._bus.publish(Topic.VOICE_HEARD, heard)
        if accepted:
            try:
                self._on_text(text, confidence)
            except Exception:  # noqa: BLE001 - the listener must survive callback errors
                log.exception("voice on_text callback failed")

    def _publish_status(self) -> None:
        if self._bus is None:
            return
        st = self.status()
        self._bus.publish(Topic.VOICE_STATUS, {
            "enabled": st["enabled"], "listening": st["listening"], "muted": st["muted"],
            "error": st["error"], "hint": st["hint"],
        })


# --------------------------------------------------------------------------- model download


def _content_length(resp: Any) -> int | None:
    headers = getattr(resp, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_model(archive: Path, dest_dir: Path, name: str) -> Path:
    """Verify + unpack ``archive`` and move the model directory to ``dest_dir / name``."""
    final = dest_dir / name
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"downloaded file is not a valid zip archive: {exc}") from exc
    with zf:
        broken = zf.testzip()
        if broken is not None:
            raise RuntimeError(f"archive is corrupt (bad CRC in {broken})")
        staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=dest_dir))
        try:
            root = staging.resolve()
            for member in zf.infolist():
                target = (staging / member.filename).resolve()
                if target != root and not target.is_relative_to(root):
                    raise RuntimeError(f"unsafe path in archive: {member.filename!r}")
            zf.extractall(staging)
            # Usual layout: one top-level folder; tolerate a renamed folder or a flat archive.
            candidates = [staging / name, *sorted(p for p in staging.iterdir() if p.is_dir()), staging]
            model_root = next((p for p in candidates if looks_like_vosk_model(p)), None)
            if model_root is None:
                raise RuntimeError("archive does not contain a Vosk model (am/final.mdl + conf/)")
            shutil.move(str(model_root), str(final))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return final


def download_model(
    dest_dir: Path | str,
    name: str = DEFAULT_MODEL_NAME,
    url: str = DEFAULT_MODEL_URL,
    progress: Callable[[int, int | None], None] | None = None,
    *,
    timeout_s: float = 60.0,
) -> Path:
    """Download (streamed), verify and unpack a Vosk model into ``dest_dir / name``.

    Idempotent: returns immediately if a valid model is already there. ``progress`` is
    called as ``progress(bytes_done, total_or_None)``. Raises ``RuntimeError`` (with a
    readable message) or ``OSError`` on failure; partial files are always removed.
    """
    dest = Path(dest_dir)
    final = dest / name
    if looks_like_vosk_model(final):
        log.info("Vosk model already present at %s", final)
        return final
    if final.exists():
        raise RuntimeError(f"{final} exists but is not a valid Vosk model; remove it and retry")
    dest.mkdir(parents=True, exist_ok=True)
    part = dest / f"{name}.zip.part"
    try:
        request = Request(url, headers={"User-Agent": "tactidose-voice-setup"})
        with urlopen(request, timeout=timeout_s) as resp:
            status = getattr(resp, "status", None)
            if status is not None and int(status) >= 400:
                raise RuntimeError(f"download failed: HTTP {status}")
            total = _content_length(resp)
            done = 0
            with open(part, "wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        if total is not None and done != total:
            raise RuntimeError(f"download incomplete: {done} of {total} bytes")
        _extract_model(part, dest, name)
    finally:
        part.unlink(missing_ok=True)
    if not looks_like_vosk_model(final):
        raise RuntimeError(f"model verification failed for {final}")
    log.info("Vosk model installed at %s", final)
    return final
