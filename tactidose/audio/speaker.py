"""SpeakerService: queued speech output with captions and an offline-first fallback chain.

Implements :class:`tactidose.core.interfaces.Speaker`. ``say()`` never blocks: utterances
are queued and spoken in order by the ``speaker`` thread. For each utterance:

1. ``Topic.SPOKEN`` ``{text, kind, audio, cached, ...meta}`` is published right before
   playback. Captions therefore work even with ``tts_provider="none"``. ``audio`` is
   ``elevenlabs`` | ``cache`` | ``offline`` | ``none``.
2. Audio is chosen by the fallback chain:

   * cache hit for the ElevenLabs rendering -> play (works offline once warmed);
   * else, provider ``elevenlabs`` with a key -> synthesize (``tts_timeout_s``; with
     ``elevenlabs_auto_voice`` the client replaces a voice the account cannot use, and the
     cache key follows the voice actually used) -> cache -> play. On any ElevenLabs error
     (including "blocked by the network") the cloud is marked *degraded* for 60 s and skipped
     quickly; speech falls back to offline TTS;
   * else, offline OS speech (renders are cached too, because PowerShell start-up is slow);
   * else, captions only.

``is_speaking`` is True from the moment an utterance is dequeued (or queued) until its
playback ends. The recognizer uses it to mute the microphone (half-duplex).
``say(..., interrupt=True)`` drops everything queued and cuts off the current playback.

:meth:`SpeakerService.warm_cache` pre-renders :data:`~tactidose.core.phrases.CRITICAL_PHRASES`
(``warm-tts-cache`` CLI). Nothing in this module raises into callers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from tactidose.audio.cache import TTSCache
from tactidose.audio.offline_tts import OfflineTTS
from tactidose.audio.playback import NullPlayer, Player, is_wav, pcm_to_wav
from tactidose.config import Settings
from tactidose.core import phrases
from tactidose.core.bus import EventBus, Topic
from tactidose.integrations.elevenlabs import (
    ElevenLabsClient,
    ElevenLabsError,
    ElevenLabsUnavailable,
    is_pcm_format,
    sample_rate_for,
)

log = logging.getLogger(__name__)

#: After an ElevenLabs failure, skip the cloud for this long.
DEGRADED_S = 60.0
#: Oldest pending utterances are dropped beyond this (a long speech backlog helps no one).
MAX_QUEUE = 20
_FATAL_HTTP = frozenset({401, 402, 403, 429})


@dataclass
class _Utterance:
    text: str
    kind: str
    meta: dict[str, Any] = field(default_factory=dict)
    gen: int = 0


class SpeakerService:
    """Speech output: queue + captions + ElevenLabs -> cache -> offline -> captions only."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus | None,
        *,
        tts: Any | None = None,
        offline: Any | None = None,
        player: Any | None = None,
        cache: TTSCache | None = None,
        cache_offline: bool = True,
    ) -> None:
        self.settings = settings
        self._bus = bus
        self.provider = settings.tts_provider
        self._cache = cache if cache is not None else TTSCache(settings.tts_cache_dir)
        self._cache_offline = cache_offline
        self._owns_tts = False

        if self.provider == "elevenlabs":
            api_key = settings.elevenlabs_api_key.get_secret_value() if settings.elevenlabs_api_key else ""
            if tts is None and api_key:
                if is_pcm_format(settings.elevenlabs_output_format):
                    tts = ElevenLabsClient(
                        api_key=api_key,
                        voice_id=settings.elevenlabs_voice_id,
                        model_id=settings.elevenlabs_model_id,
                        output_format=settings.elevenlabs_output_format,
                        timeout_s=settings.tts_timeout_s,
                        auto_voice=settings.elevenlabs_auto_voice,
                    )
                    self._owns_tts = True
                else:
                    log.warning("ELEVENLABS output format %r is not raw PCM (pcm_16000/22050/24000); "
                                "cloud voice disabled", settings.elevenlabs_output_format)
            self._tts = tts
        else:
            self._tts = None
        if self.provider == "none":
            self._offline = None
            self._player = player if player is not None else NullPlayer()
        else:
            self._offline = offline if offline is not None else OfflineTTS()
            self._player = player if player is not None else Player(settings.audio_output_device)

        self._cond = threading.Condition()
        self._queue: deque[_Utterance] = deque()
        self._gen = 0
        self._speaking = False
        self._running = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._now: Callable[[], float] = time.monotonic
        self._degraded_until = 0.0
        self._last_error: str | None = None
        self._last_audio: str | None = None
        self._spoken = 0

    # ------------------------------------------------------------------ Speaker protocol
    def start(self) -> None:
        with self._cond:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._running = True
            self._thread = threading.Thread(target=self._run, name="speaker", daemon=True)
            self._thread.start()
        log.info("speech output: provider=%s, elevenlabs=%s, offline=%s, player=%s",
                 self.provider, "yes" if self._tts is not None else "no",
                 self._offline_engine() or "none", type(self._player).__name__)

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._running = False
            self._gen += 1
            self._queue.clear()
            self._cond.notify_all()
            thread = self._thread
        self._stop_player()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        if self._owns_tts and self._tts is not None:
            try:
                self._tts.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the ElevenLabs client failed", exc_info=True)

    def say(self, text: str, *, kind: str = "info", interrupt: bool = False,
            meta: dict[str, Any] | None = None) -> None:
        cleaned = " ".join(str(text or "").split())
        with self._cond:
            if self._closed:
                log.debug("speaker closed; dropping %r", cleaned)
                return
            if interrupt:
                self._gen += 1
                if self._queue:
                    log.debug("interrupt: dropping %d queued utterance(s)", len(self._queue))
                self._queue.clear()
            if cleaned:
                if len(self._queue) >= MAX_QUEUE:
                    dropped = self._queue.popleft()
                    log.warning("speech backlog full; dropping %r", dropped.text)
                self._queue.append(_Utterance(cleaned, str(kind or "info"), dict(meta or {}), self._gen))
            self._cond.notify_all()
        if interrupt:
            self._stop_player()

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and not self._speaking, timeout)

    @property
    def is_speaking(self) -> bool:
        with self._cond:
            return self._speaking or (self._running and bool(self._queue))

    # ------------------------------------------------------------------ status / cache
    def status(self) -> dict[str, Any]:
        remaining = self._degraded_until - self._now()
        degraded = remaining > 0
        until = (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat() if degraded else None
        with self._cond:
            queued, speaking = len(self._queue), self._speaking
        return {
            "provider": self.provider,
            "elevenlabs": self._tts is not None,
            "offline": self._offline_available(),
            "cache_entries": self._cache.count(),
            "degraded_until": until,
            "degraded": degraded,
            "offline_engine": self._offline_engine(),
            "player": getattr(self._player, "last_backend", None) or type(self._player).__name__,
            "speaking": speaking,
            "queued": queued,
            "spoken": self._spoken,
            "last_audio": self._last_audio,
            "last_error": self._last_error,
            "running": self._running,
        }

    def warm_cache(self, texts: Iterable[str] | None = None) -> dict[str, Any]:
        """Pre-render ``texts`` (default: ``phrases.CRITICAL_PHRASES``) with the primary engine.

        Returns ``{rendered, cached, failed, total, provider, errors}``. Synchronous; ignores
        the degraded flag, but stops calling the cloud after a network/auth/quota failure.
        """
        source = phrases.CRITICAL_PHRASES if texts is None else texts
        items = list(dict.fromkeys(" ".join(str(t).split()) for t in source if t and str(t).strip()))
        result: dict[str, Any] = {"rendered": 0, "cached": 0, "failed": 0, "total": len(items),
                                  "provider": "none", "errors": []}

        def error(msg: str) -> None:
            result["failed"] += 1
            if len(result["errors"]) < 5:
                result["errors"].append(msg)

        if self._tts is not None:
            result["provider"] = "elevenlabs"
            abort: str | None = None
            for text in items:
                key = self._cloud_key(text)
                if self._cache.contains(key):
                    result["cached"] += 1
                    continue
                if abort is not None:
                    error(abort)
                    continue
                try:
                    audio = self._tts.synthesize(text)
                except ElevenLabsError as exc:
                    error(str(exc))
                    if (isinstance(exc, ElevenLabsUnavailable) or exc.status in _FATAL_HTTP or exc.status >= 500
                            or getattr(exc, "voice_error", False)):  # a refused voice fails every phrase
                        abort = f"skipped after: {exc}"
                        self._mark_degraded(exc)
                    continue
                except Exception as exc:  # noqa: BLE001
                    error(f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    key = self._cloud_key(text)  # re-keyed: the client may have switched voices
                    stored = self._store_cloud(key, audio) is not None and self._cache.contains(key)
                except Exception as exc:  # noqa: BLE001 - e.g. unusable output format
                    error(f"{type(exc).__name__}: {exc}")
                    continue
                if stored:
                    result["rendered"] += 1
                else:
                    error("could not write the cache entry")
        elif self._offline_available():
            result["provider"] = "offline"
            for text in items:
                key = self._offline_key(text)
                if self._cache.contains(key):
                    result["cached"] += 1
                    continue
                wav = self._offline.synthesize(text) if self._offline is not None else None
                if wav is None or self._cache.put(key, wav) is None:
                    error(f"offline render failed: {text[:40]}")
                else:
                    result["rendered"] += 1
        else:
            result["failed"] = len(items)
            result["errors"].append("no speech engine available (ElevenLabs not configured, no offline voice)")
        log.info("TTS cache warm-up (%s): %s", result["provider"],
                 {k: result[k] for k in ("rendered", "cached", "failed", "total")})
        return result

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                utterance = self._queue.popleft()
                self._speaking = True
            try:
                self._speak(utterance)
            except Exception:  # noqa: BLE001 - the speaker thread must survive anything
                log.exception("speaker failed on %r", utterance.text)
            finally:
                with self._cond:
                    self._speaking = False
                    self._cond.notify_all()

    def _stale(self, u: _Utterance) -> bool:
        return u.gen != self._gen or self._closed

    def _speak(self, u: _Utterance) -> None:
        if self._stale(u):
            return
        audio, cached, wav = self._render(u.text)
        if self._stale(u):
            return
        payload = {**u.meta, "text": u.text, "kind": u.kind, "audio": audio, "cached": cached}
        self._last_audio = audio
        self._spoken += 1
        if self._bus is not None:
            self._bus.publish(Topic.SPOKEN, payload)
        if wav is not None:
            try:
                self._player.play_wav(wav, should_stop=lambda: self._stale(u))
            except Exception:  # noqa: BLE001
                log.exception("audio playback failed")

    def _render(self, text: str) -> tuple[str, bool, bytes | None]:
        """Returns ``(audio_source, from_cache, wav_or_None)``."""
        if self.provider == "none":
            return "none", False, None
        if self.provider == "elevenlabs":
            key = self._cloud_key(text)
            wav = self._cache.get(key)
            if wav is not None:
                return "cache", True, wav
            if self._tts is not None and not self._degraded():
                wav = self._synth_cloud(text)
                if wav is not None:
                    return "elevenlabs", False, wav
        if self._offline_available():
            key = self._offline_key(text)
            wav = self._cache.get(key) if self._cache_offline else None
            if wav is not None:
                return "offline", True, wav
            wav = self._offline.synthesize(text) if self._offline is not None else None
            if wav is not None:
                if self._cache_offline:
                    self._cache.put(key, wav)
                return "offline", False, wav
        return "none", False, None

    # ------------------------------------------------------------------ helpers
    def _cloud_params(self) -> tuple[str, str, str]:
        s = self.settings
        voice = str(getattr(self._tts, "voice_id", None) or s.elevenlabs_voice_id)
        model = str(getattr(self._tts, "model_id", None) or s.elevenlabs_model_id)
        fmt = str(getattr(self._tts, "output_format", None) or s.elevenlabs_output_format)
        return voice, model, fmt

    def _cloud_key(self, text: str) -> str:
        voice, model, fmt = self._cloud_params()
        return TTSCache.key("elevenlabs", voice, model, fmt, text)

    def _offline_key(self, text: str) -> str:
        engine = self._offline_engine() or "offline"
        voice = str(getattr(self._offline, "voice", None) or "default")
        return TTSCache.key("offline", engine, voice, "wav", text)

    def _offline_engine(self) -> str | None:
        if self._offline is None:
            return None
        try:
            engine = getattr(self._offline, "engine", None)
            return str(engine) if engine else None
        except Exception:  # noqa: BLE001
            return None

    def _offline_available(self) -> bool:
        if self._offline is None:
            return False
        try:
            return bool(self._offline.available())
        except Exception:  # noqa: BLE001
            return False

    def _store_cloud(self, key: str, audio: bytes) -> bytes | None:
        """Wrap cloud PCM as WAV, cache it, and return the WAV (None if unusable)."""
        if is_wav(audio):
            wav = audio
        else:
            _, _, fmt = self._cloud_params()
            rate = getattr(self._tts, "sample_rate", None) or sample_rate_for(fmt)
            wav = pcm_to_wav(audio, int(rate))
        self._cache.put(key, wav)
        return wav

    def _synth_cloud(self, text: str) -> bytes | None:
        try:
            audio = self._tts.synthesize(text)  # type: ignore[union-attr]
            # keyed after synthesis: with auto voice the client may have switched to another voice
            return self._store_cloud(self._cloud_key(text), audio)
        except Exception as exc:  # noqa: BLE001 - ElevenLabsError or anything unexpected
            self._mark_degraded(exc)
            return None

    def _degraded(self) -> bool:
        return self._now() < self._degraded_until

    def _mark_degraded(self, exc: BaseException) -> None:
        first = not self._degraded()
        self._degraded_until = self._now() + DEGRADED_S
        self._last_error = str(exc)
        if first:
            log.warning("ElevenLabs unavailable (%s); using cached/offline speech for %d s", exc, int(DEGRADED_S))
            if self._bus is not None:
                self._bus.publish(Topic.NOTICE, {
                    "level": "warning",
                    "message": "Cloud voice unavailable; using offline speech.",
                    "code": "TTS_DEGRADED",
                })

    def _stop_player(self) -> None:
        try:
            self._player.stop()
        except Exception:  # noqa: BLE001
            log.debug("player.stop() failed", exc_info=True)
