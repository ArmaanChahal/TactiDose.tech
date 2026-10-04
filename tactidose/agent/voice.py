"""Server-side speech for the patient portal: Vosk STT, reply TTS to WAV, short-lived audio store.

* :class:`VoskTranscriber` - ``transcribe(pcm16)`` for ``POST /api/agent/transcribe``: raw
  16-bit little-endian mono PCM at 16 kHz (at most 30 s) -> ``{text, confidence, engine:
  "vosk"}``. Full vocabulary (no grammar: the agent understands free text). The model is loaded
  lazily from ``settings.vosk_model_path`` once and shared; each call uses its own recognizer.
  Missing ``vosk`` / model -> :class:`~tactidose.agent.AgentUnavailable` (HTTP 503). Not gated
  by ``voice_enabled`` (that switch is for the device microphone loop).
* :class:`ReplyTTS` - reply text -> WAV bytes with the same chain and cache keys as
  ``SpeakerService``: cached ElevenLabs render -> ElevenLabs (when configured, and only with
  ``tts_include_med_names`` because free-text replies may name medications) -> cached / fresh
  offline OS voice -> ``None`` (the browser then uses ``speechSynthesis``).
* :class:`AudioStore` - in-memory LRU of rendered replies: per patient, 10-minute TTL, bounded
  count and bytes. Ids are random 32-hex-character tokens.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import secrets
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tactidose.agent import AgentInputError, AgentUnavailable
from tactidose.audio.cache import TTSCache
from tactidose.audio.offline_tts import OfflineTTS
from tactidose.audio.playback import is_wav, pcm_to_wav
from tactidose.config import Settings
from tactidose.integrations.elevenlabs import (
    ElevenLabsClient,
    is_pcm_format,
    sample_rate_for,
)
from tactidose.voice.recognizer import (
    DOWNLOAD_HINT,
    INSTALL_HINT,
    looks_like_vosk_model,
)

log = logging.getLogger(__name__)

PCM_SAMPLE_RATE = 16000
MAX_AUDIO_S = 30
MAX_PCM_BYTES = PCM_SAMPLE_RATE * 2 * MAX_AUDIO_S
ENGINE = "vosk"


# =========================================================================== speech-to-text


def _vosk_importable() -> bool:
    if "vosk" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("vosk") is not None
    except (ImportError, ValueError):
        return False


class VoskTranscriber:
    """Full-vocabulary Vosk recognition of uploaded PCM16. Thread-safe."""

    #: Bytes fed to Vosk per call (0.25 s of 16 kHz int16 audio).
    chunk_bytes: int = 8000

    def __init__(self, settings: Settings, *, max_concurrent: int = 2) -> None:
        self.settings = settings
        self._load_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self._vosk: Any = None
        self._model: Any = None

    @property
    def model_path(self) -> Path:
        return Path(self.settings.vosk_model_path)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def available(self) -> bool:
        """Cheap check (no model load): vosk importable and a model directory present."""
        if self._model is not None:
            return True
        return _vosk_importable() and looks_like_vosk_model(self.model_path)

    def load(self) -> None:
        """Load the model now (1-16 s). Raises AgentUnavailable when Vosk or the model is missing."""
        self._ensure_model()

    def close(self) -> None:
        with self._load_lock:
            self._model = None

    def _ensure_model(self) -> tuple[Any, Any]:
        with self._load_lock:
            if self._model is not None:
                return self._vosk, self._model
            path = self.model_path
            if not looks_like_vosk_model(path):
                raise AgentUnavailable(f"speech recognition unavailable: no Vosk model at {path} ({DOWNLOAD_HINT})")
            try:
                import vosk  # noqa: PLC0415 - optional dependency
            except Exception as exc:  # noqa: BLE001 - ImportError or a native-library OSError
                raise AgentUnavailable(
                    f"speech recognition unavailable: vosk cannot be imported ({INSTALL_HINT})") from exc
            try:
                if hasattr(vosk, "SetLogLevel"):
                    vosk.SetLogLevel(-1)
                started = time.monotonic()
                model = vosk.Model(str(path))
            except Exception as exc:  # noqa: BLE001
                raise AgentUnavailable(f"speech recognition unavailable: the Vosk model failed to load ({exc})") from exc
            log.info("Vosk model loaded from %s in %.1f s", path, time.monotonic() - started)
            self._vosk, self._model = vosk, model
            return vosk, model

    def transcribe(self, pcm16: bytes | bytearray | memoryview) -> dict[str, Any]:
        """``{"text", "confidence", "engine": "vosk"}`` for 16 kHz mono int16 PCM.

        ``confidence`` is the mean word confidence (0.0 when nothing was recognised). Raises
        AgentInputError for non-bytes input or more than 30 s of audio, AgentUnavailable when
        the recognizer cannot run."""
        if not isinstance(pcm16, (bytes, bytearray, memoryview)):
            raise AgentInputError("audio must be raw 16-bit PCM bytes")
        data = bytes(pcm16)
        if len(data) > MAX_PCM_BYTES:
            raise AgentInputError(f"audio longer than {MAX_AUDIO_S} s")
        vosk, model = self._ensure_model()
        data = data[: len(data) - (len(data) % 2)]
        if not data:
            return {"text": "", "confidence": 0.0, "engine": ENGINE}
        with self._slots:
            try:
                rec = vosk.KaldiRecognizer(model, PCM_SAMPLE_RATE)
                if hasattr(rec, "SetWords"):
                    rec.SetWords(True)
                results: list[str] = []
                for start in range(0, len(data), self.chunk_bytes):
                    if rec.AcceptWaveform(data[start:start + self.chunk_bytes]):
                        results.append(rec.Result())
                results.append(rec.FinalResult())
            except Exception as exc:  # noqa: BLE001
                log.exception("Vosk recognition failed")
                raise AgentUnavailable(f"speech recognition failed ({type(exc).__name__})") from exc
        texts: list[str] = []
        confs: list[float] = []
        for raw in results:
            try:
                res = json.loads(raw)
            except (TypeError, ValueError):
                continue
            text = " ".join(str(res.get("text", "")).split())
            if text:
                texts.append(text)
            for word in res.get("result") or []:
                if isinstance(word, dict) and isinstance(word.get("conf"), (int, float)):
                    confs.append(float(word["conf"]))
        confidence = round(sum(confs) / len(confs), 3) if confs and texts else 0.0
        return {"text": " ".join(texts), "confidence": confidence, "engine": ENGINE}


# =========================================================================== text-to-speech


class ReplyTTS:
    """Reply text -> WAV bytes (ElevenLabs -> cache -> offline OS voice). Never raises."""

    #: After a cloud failure, skip ElevenLabs for this long.
    degraded_s: float = 60.0

    def __init__(self, settings: Settings, *, client: Any | None = None, offline: Any | None = None,
                 cache: TTSCache | None = None) -> None:
        self.settings = settings
        self._cache = cache if cache is not None else TTSCache(settings.tts_cache_dir)
        self._owns_client = False
        self._client = None
        if settings.tts_provider == "elevenlabs" and settings.tts_include_med_names:
            if client is not None:
                self._client = client
            elif settings.elevenlabs_configured and is_pcm_format(settings.elevenlabs_output_format):
                key = settings.elevenlabs_api_key.get_secret_value() if settings.elevenlabs_api_key else ""
                self._client = ElevenLabsClient(
                    api_key=key, voice_id=settings.elevenlabs_voice_id, model_id=settings.elevenlabs_model_id,
                    output_format=settings.elevenlabs_output_format, timeout_s=settings.tts_timeout_s)
                self._owns_client = True
        if settings.tts_provider == "none":
            self._offline = None
        else:
            self._offline = offline if offline is not None else OfflineTTS()
        self._degraded_until = 0.0
        self._now: Callable[[], float] = time.monotonic

    @property
    def available(self) -> bool:
        return self._client is not None or self._offline_available()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the ElevenLabs client failed", exc_info=True)

    def synthesize(self, text: str) -> bytes | None:
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            return None
        try:
            if self._client is not None:
                key = self._cloud_key(cleaned)
                wav = self._cache.get(key)
                if wav is not None:
                    return wav
                if self._now() >= self._degraded_until:
                    wav = self._synth_cloud(cleaned, key)
                    if wav is not None:
                        return wav
            if self._offline_available():
                key = self._offline_key(cleaned)
                wav = self._cache.get(key)
                if wav is not None:
                    return wav
                wav = self._offline.synthesize(cleaned) if self._offline is not None else None
                if wav is not None:
                    self._cache.put(key, wav)
                    return wav
        except Exception:  # noqa: BLE001 - speech is optional; the browser can speak instead
            log.exception("reply TTS failed")
        return None

    # ------------------------------------------------------------------ helpers (same keys as SpeakerService)
    def _cloud_params(self) -> tuple[str, str, str]:
        s = self.settings
        voice = str(getattr(self._client, "voice_id", None) or s.elevenlabs_voice_id)
        model = str(getattr(self._client, "model_id", None) or s.elevenlabs_model_id)
        fmt = str(getattr(self._client, "output_format", None) or s.elevenlabs_output_format)
        return voice, model, fmt

    def _cloud_key(self, text: str) -> str:
        voice, model, fmt = self._cloud_params()
        return TTSCache.key("elevenlabs", voice, model, fmt, text)

    def _offline_key(self, text: str) -> str:
        engine = str(getattr(self._offline, "engine", None) or "offline")
        voice = str(getattr(self._offline, "voice", None) or "default")
        return TTSCache.key("offline", engine, voice, "wav", text)

    def _offline_available(self) -> bool:
        if self._offline is None:
            return False
        try:
            return bool(self._offline.available())
        except Exception:  # noqa: BLE001
            return False

    def _synth_cloud(self, text: str, key: str) -> bytes | None:
        try:
            audio = self._client.synthesize(text)  # type: ignore[union-attr]
            if is_wav(audio):
                wav = audio
            else:
                _, _, fmt = self._cloud_params()
                rate = getattr(self._client, "sample_rate", None) or sample_rate_for(fmt)
                wav = pcm_to_wav(audio, int(rate))
        except Exception as exc:  # noqa: BLE001 - ElevenLabsError or anything unexpected
            if self._now() >= self._degraded_until:
                log.warning("ElevenLabs unavailable for replies (%s); using offline speech for %d s",
                            exc, int(self.degraded_s))
            self._degraded_until = self._now() + self.degraded_s
            return None
        self._cache.put(key, wav)
        return wav


# =========================================================================== audio store


@dataclass
class _Clip:
    patient_id: int
    wav: bytes
    expires_at: float


class AudioStore:
    """Short-lived rendered replies, readable only by the patient they were made for."""

    def __init__(self, *, ttl_s: float = 600.0, max_items: int = 64, max_bytes: int = 48 * 1024 * 1024,
                 now: Callable[[], float] | None = None) -> None:
        self.ttl_s = float(ttl_s)
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1, int(max_bytes))
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._clips: OrderedDict[str, _Clip] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._clips)

    def put(self, patient_id: int, wav: bytes) -> str:
        audio_id = secrets.token_hex(16)
        clip = _Clip(int(patient_id), bytes(wav), self._now() + self.ttl_s)
        with self._lock:
            self._purge_locked()
            self._clips[audio_id] = clip
            self._bytes += len(clip.wav)
            while self._clips and (len(self._clips) > self.max_items or self._bytes > self.max_bytes):
                _, old = self._clips.popitem(last=False)
                self._bytes -= len(old.wav)
        return audio_id

    def get(self, audio_id: str, patient_id: int) -> bytes | None:
        """The WAV for ``audio_id`` if it belongs to ``patient_id`` and has not expired."""
        key = str(audio_id or "").strip().lower()
        key = key.removesuffix(".wav")
        if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
            return None
        with self._lock:
            self._purge_locked()
            clip = self._clips.get(key)
            if clip is None or clip.patient_id != int(patient_id):
                return None
            self._clips.move_to_end(key)
            return clip.wav

    def _purge_locked(self) -> None:
        now = self._now()
        for key in [k for k, c in self._clips.items() if c.expires_at <= now]:
            self._bytes -= len(self._clips.pop(key).wav)
