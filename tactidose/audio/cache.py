"""TTS disk cache: rendered speech stored as WAV files, keyed by what produced it.

The key is ``sha256("provider|voice|model|format|text")``, with whitespace in the text
normalised. A different voice, model or output format never reuses stale audio. Entries
are plain WAV files (PCM wrapped with a WAV header) in ``settings.tts_cache_dir``, so they
can be inspected or played by hand.

Writes are atomic (temporary file + ``os.replace``). A corrupt entry is deleted and
reported as a miss. All methods are thread-safe and never raise for I/O problems: they log
and behave like a miss.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from pathlib import Path

from tactidose.audio.playback import is_wav, pcm_to_wav, read_wav

log = logging.getLogger(__name__)

_SUFFIX = ".wav"


def normalise_text(text: str) -> str:
    return " ".join(str(text or "").split())


class TTSCache:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    # ------------------------------------------------------------------ keys
    @staticmethod
    def key(provider: str, voice: str, model: str, fmt: str, text: str) -> str:
        material = "|".join((provider, voice or "", model or "", fmt or "", normalise_text(text)))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        if not key or any(c not in "0123456789abcdef" for c in key):
            raise ValueError(f"invalid cache key {key!r}")
        return self.directory / f"{key}{_SUFFIX}"

    # ------------------------------------------------------------------ access
    def contains(self, key: str) -> bool:
        try:
            return self.path_for(key).is_file()
        except (OSError, ValueError):
            return False

    def get(self, key: str) -> bytes | None:
        """WAV bytes for ``key`` or ``None`` (missing / unreadable / corrupt)."""
        try:
            path = self.path_for(key)
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("TTS cache read failed for %s: %s", key[:12], exc)
            return None
        try:
            if read_wav(data).frame_count == 0:
                raise ValueError("empty audio")
        except ValueError as exc:
            log.warning("discarding corrupt TTS cache entry %s (%s)", key[:12], exc)
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data

    def put(self, key: str, audio: bytes, *, sample_rate: int | None = None,
            channels: int = 1, sample_width: int = 2) -> Path | None:
        """Store WAV bytes, or raw PCM (then ``sample_rate`` is required). Returns the path."""
        tmp: Path | None = None
        try:
            path = self.path_for(key)
            wav = audio if is_wav(audio) else None
            if wav is None:
                if not sample_rate:
                    raise ValueError("sample_rate is required to cache raw PCM")
                wav = pcm_to_wav(audio, sample_rate, channels, sample_width)
            read_wav(wav)  # refuse to store something we could not play back
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_bytes(wav)
            os.replace(tmp, path)
            return path
        except (OSError, ValueError) as exc:
            log.warning("TTS cache write failed for %s: %s", key[:12], exc)
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return None

    def count(self) -> int:
        try:
            return sum(1 for p in self.directory.glob(f"*{_SUFFIX}") if p.is_file())
        except OSError:
            return 0
