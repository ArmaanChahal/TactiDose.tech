"""ElevenLabs text-to-speech REST client (httpx), optional cloud voice.

``POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={fmt}``
with headers ``xi-api-key``, ``Content-Type: application/json`` and ``Accept: audio/*``,
and a JSON body ``{"text", "model_id"}``. ``pcm_<rate>`` formats return raw 16-bit mono
little-endian PCM at ``<rate>`` Hz, so playback needs no MP3 decoder.

Failures raise :class:`ElevenLabsError`: ``status`` is the HTTP status for non-2xx
replies. Network problems and timeouts raise its subclass :class:`ElevenLabsUnavailable`
(``status == 0``). The speaker treats both as "fall back to cached / offline speech".

Privacy: with ``tts_include_med_names=False`` the phrase layer never puts medication names
into the text sent here. The API key is never logged or included in error messages.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
#: The API rejects longer inputs for most models; our sentences are far shorter.
MAX_TEXT_CHARS = 5000


class ElevenLabsError(Exception):
    """Non-2xx reply (``status`` = HTTP status) or, for the subclass, no reply at all."""

    def __init__(self, status: int, message: str) -> None:
        self.status = int(status)
        self.message = str(message)
        prefix = f"ElevenLabs HTTP {self.status}" if self.status else "ElevenLabs unavailable"
        super().__init__(f"{prefix}: {self.message}")


class ElevenLabsUnavailable(ElevenLabsError):
    """Network error or timeout: the service could not be reached (``status == 0``)."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


def is_pcm_format(fmt: str) -> bool:
    """``pcm_22050`` -> True; ``mp3_44100_128`` -> False."""
    parts = str(fmt).strip().lower().split("_")
    return len(parts) == 2 and parts[0] == "pcm" and parts[1].isdigit()


def sample_rate_for(fmt: str) -> int:
    """Sample rate encoded in an output-format name (``pcm_22050`` -> 22050,
    ``mp3_44100_128`` -> 44100). Raises ``ValueError`` if the name carries no rate."""
    parts = str(fmt).strip().lower().split("_")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    raise ValueError(f"cannot determine the sample rate of output format {fmt!r}")


def _error_message(resp: httpx.Response) -> str:
    """Best-effort human message from an ElevenLabs error body."""
    try:
        data: Any = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return text[:300] or resp.reason_phrase or "request failed"
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if isinstance(detail, list) and detail:
        detail = detail[0]
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("msg") or ""
        code = detail.get("status") or detail.get("code") or detail.get("type") or ""
        text = f"{code}: {message}" if code and message else str(message or code)
        return text[:300] or resp.reason_phrase or "request failed"
    return str(detail)[:300] or resp.reason_phrase or "request failed"


class ElevenLabsClient:
    """Minimal synchronous client. Thread-safe (one shared ``httpx.Client``)."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model_id: str,
        output_format: str = "pcm_22050",
        timeout_s: float = 6.0,
        client: httpx.Client | None = None,
        *,
        base_url: str = API_BASE,
    ) -> None:
        if not api_key:
            raise ValueError("an ElevenLabs API key is required")
        if not voice_id:
            raise ValueError("an ElevenLabs voice id is required")
        self._api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_format = output_format
        self.timeout_s = float(timeout_s)
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never show the key
        return f"ElevenLabsClient(voice_id={self.voice_id!r}, model_id={self.model_id!r}, format={self.output_format!r})"

    @property
    def is_pcm(self) -> bool:
        return is_pcm_format(self.output_format)

    @property
    def sample_rate(self) -> int:
        return sample_rate_for(self.output_format)

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=httpx.Timeout(self.timeout_s))
            return self._client

    def synthesize(self, text: str) -> bytes:
        """Render ``text`` and return the audio bytes (raw PCM for ``pcm_*`` formats)."""
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            raise ValueError("cannot synthesize empty text")
        if len(cleaned) > MAX_TEXT_CHARS:
            raise ValueError(f"text longer than {MAX_TEXT_CHARS} characters")
        url = f"{self.base_url}/v1/text-to-speech/{quote(self.voice_id, safe='')}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/*",
        }
        body = {"text": cleaned, "model_id": self.model_id}
        try:
            resp = self._http().post(
                url,
                params={"output_format": self.output_format},
                headers=headers,
                json=body,
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ElevenLabsUnavailable(f"timed out after {self.timeout_s:g} s") from exc
        except httpx.HTTPError as exc:
            raise ElevenLabsUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            raise ElevenLabsError(resp.status_code, _error_message(resp))
        audio = resp.content
        if not audio:
            raise ElevenLabsError(resp.status_code, "empty audio response")
        if self.is_pcm and len(audio) % 2:
            audio = audio[:-1]  # 16-bit samples: drop a stray trailing byte
        return audio

    def close(self) -> None:
        """Close the HTTP client if this object created it (injected clients are left alone)."""
        if not self._owns_client:
            return
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def __enter__(self) -> "ElevenLabsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
