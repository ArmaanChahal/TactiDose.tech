"""ElevenLabs text-to-speech REST client (httpx), optional cloud voice.

``POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={fmt}``
with headers ``xi-api-key``, ``Content-Type: application/json`` and ``Accept: audio/*``,
and a JSON body ``{"text", "model_id"}``. ``pcm_<rate>`` formats return raw 16-bit mono
little-endian PCM at ``<rate>`` Hz, so playback needs no MP3 decoder.

Failures raise :class:`ElevenLabsError`: ``status`` is the HTTP status for non-2xx
replies. Network problems, timeouts and network blocks raise its subclass
:class:`ElevenLabsUnavailable` (``status == 0``). The speaker treats both as "fall back to
cached / offline speech".

Redirects are never followed (:data:`~tactidose.integrations.netsafe.NO_REDIRECTS`, on the
owned client and on every request, so an injected client cannot re-enable them): httpx would
forward the ``xi-api-key`` header (and a 307's body) to the redirect target, so the key only
ever goes to ``base_url``. A 3xx, or a 2xx HTML page, is a web filter's sign-in / block page:
:class:`ElevenLabsUnavailable` "blocked by the network".

Voice auto-pick (``auto_voice``, setting ``elevenlabs_auto_voice``): when a synthesis is refused
because of the voice (a :data:`VOICE_ERROR_STATUSES` reply whose error mentions a voice:
``voice_not_found``, library voices on free plans, or the legacy default "George", which
accounts created after March 2026 cannot use), the client lists the account's voices
(:meth:`ElevenLabsClient.available_voices`, ``GET /v1/voices``), switches to the first premade
one (:func:`pick_voice`), logs one warning naming the ``ELEVENLABS_VOICE_ID`` to set, and
retries once. The switch is kept for later calls. If the list cannot be fetched (networks that
redirect GET requests, keys without voice access) the original error is raised.

Privacy: with ``tts_include_med_names=False`` the phrase layer never puts medication names
into the text sent here. The API key is never logged or included in error messages.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import quote

import httpx

from tactidose.integrations import netsafe

log = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
#: The API rejects longer inputs for most models; our sentences are far shorter.
MAX_TEXT_CHARS = 5000
#: A refusal with one of these statuses whose error mentions a voice means "this account cannot
#: use this voice" (it may still be able to use others).
VOICE_ERROR_STATUSES = frozenset({400, 402, 403, 404, 422})
#: Replacement-voice preference by ``category``; after these, the first voice of any category.
VOICE_CATEGORY_PREFERENCE = ("premade", "default")


class ElevenLabsError(Exception):
    """Non-2xx reply (``status`` = HTTP status) or, for the subclass, no usable reply at all.
    ``voice_error``: the reply says this account cannot use the requested voice."""

    def __init__(self, status: int, message: str, *, voice_error: bool = False) -> None:
        self.status = int(status)
        self.message = str(message)
        self.voice_error = bool(voice_error)
        prefix = f"ElevenLabs HTTP {self.status}" if self.status else "ElevenLabs unavailable"
        super().__init__(f"{prefix}: {self.message}")


class ElevenLabsUnavailable(ElevenLabsError):
    """Network error, timeout or network block (redirect / block page): no usable reply (``status == 0``)."""

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


def _http_error(resp: httpx.Response) -> ElevenLabsError:
    """The exception for a non-2xx reply. netsafe words the cases it knows better than the vendor
    text; a voice refusal is judged on the error's code and message, never on echoed input."""
    status = resp.status_code
    failure = netsafe.classify_response(resp)
    if 300 <= status < 400:  # a web filter's sign-in / block page (never followed): host only
        return ElevenLabsUnavailable(f"blocked by the network ({failure.message})")
    if status == 401 and failure.code == netsafe.PERMISSION:  # detected_unusual_activity
        return ElevenLabsError(status, f"{failure.message}. {failure.hint}")
    message = _error_message(resp)
    return ElevenLabsError(status, message, voice_error=status in VOICE_ERROR_STATUSES and "voice" in message.lower())


def pick_voice(voices: list[dict[str, str]], exclude: str = "") -> dict[str, str] | None:
    """Replacement voice: the first ``premade`` one, else the first ``default`` one, else the first
    of any category, never ``exclude`` (the refused voice). None when nothing is left."""
    candidates = [v for v in voices if v.get("voice_id") and v.get("voice_id") != exclude
                  and not v.get("is_legacy")]
    for category in VOICE_CATEGORY_PREFERENCE:
        for voice in candidates:
            if str(voice.get("category") or "").lower() == category:
                return voice
    return candidates[0] if candidates else None


class ElevenLabsClient:
    """Minimal synchronous client. Thread-safe (one shared ``httpx.Client``; ``voice_id`` only
    changes under the lock, when ``auto_voice`` replaces a refused voice)."""

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
        auto_voice: bool = False,
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
        self.auto_voice = bool(auto_voice)
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._no_switch_logged = False

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
                self._client = httpx.Client(timeout=httpx.Timeout(self.timeout_s), **netsafe.NO_REDIRECTS)
            return self._client

    def synthesize(self, text: str) -> bytes:
        """Render ``text`` and return the audio bytes (raw PCM for ``pcm_*`` formats).

        With ``auto_voice``, a refused voice is replaced by another voice of the account and the
        request retried once (module docstring); a second failure is raised as is."""
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            raise ValueError("cannot synthesize empty text")
        if len(cleaned) > MAX_TEXT_CHARS:
            raise ValueError(f"text longer than {MAX_TEXT_CHARS} characters")
        voice = self.voice_id
        try:
            return self._render(voice, cleaned)
        except ElevenLabsError as exc:
            replacement = self._replacement_voice(voice) if self.auto_voice and exc.voice_error else None
            if replacement is None:
                raise
        return self._render(replacement, cleaned)

    def available_voices(self) -> list[dict[str, str]]:
        """The account's voices as ``[{"voice_id", "name", "category"}]`` in API order
        (``GET /v1/voices``: same key header, timeout and no-redirect rule as :meth:`synthesize`).
        Raises :class:`ElevenLabsError` / :class:`ElevenLabsUnavailable`; on networks that
        redirect GET requests this fails while synthesis (a POST) may still work."""
        resp = self._send("GET", f"{self.base_url}/v1/voices",
                          headers={"xi-api-key": self._api_key, "Accept": "application/json"})
        try:
            data: Any = resp.json()
        except ValueError as exc:
            raise ElevenLabsError(resp.status_code, "the voice list is not JSON") from exc
        items = data.get("voices") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ElevenLabsError(resp.status_code, "unexpected voice list format")
        return [{"voice_id": str(v["voice_id"]), "name": str(v.get("name") or v["voice_id"]),
                 "category": str(v.get("category") or ""), **({"is_legacy": True} if v.get("is_legacy") else {})}
                for v in items if isinstance(v, dict) and v.get("voice_id")]

    # ------------------------------------------------------------------ internals
    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request to ``url`` (always under ``base_url``), redirects off for this request too.
        Non-2xx -> :func:`_http_error`; no reply or a 2xx HTML page -> ElevenLabsUnavailable."""
        try:
            resp = self._http().request(method, url, timeout=self.timeout_s, **netsafe.NO_REDIRECTS, **kwargs)
        except httpx.TimeoutException as exc:
            raise ElevenLabsUnavailable(f"timed out after {self.timeout_s:g} s") from exc
        except httpx.HTTPError as exc:
            raise ElevenLabsUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            raise _http_error(resp)
        if "text/html" in resp.headers.get("content-type", "").lower():  # never audio or API JSON
            raise ElevenLabsUnavailable("blocked by the network (a web page came back instead of an API answer)")
        return resp

    def _render(self, voice_id: str, text: str) -> bytes:
        resp = self._send(
            "POST",
            f"{self.base_url}/v1/text-to-speech/{quote(voice_id, safe='')}",
            params={"output_format": self.output_format},
            headers={"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/*"},
            json={"text": text, "model_id": self.model_id},
        )
        audio = resp.content
        if not audio:
            raise ElevenLabsError(resp.status_code, "empty audio response")
        if self.is_pcm and len(audio) % 2:
            audio = audio[:-1]  # 16-bit samples: drop a stray trailing byte
        return audio

    def _replacement_voice(self, refused: str) -> str | None:
        """Voice to retry with after ``refused``: the one a concurrent call already switched to,
        else :func:`pick_voice` from the account's list, kept for later calls (one warning).
        None when the list cannot be fetched or has no other voice. Never raises."""
        with self._lock:
            if self.voice_id != refused:
                return self.voice_id
        try:
            voices = self.available_voices()
        except Exception as exc:  # noqa: BLE001 - best effort: the caller raises the original error
            self._log_no_switch(refused, f"the voice list could not be fetched ({exc})")
            return None
        pick = pick_voice(voices, exclude=refused)
        if pick is None:
            self._log_no_switch(refused, "the account has no other voice")
            return None
        with self._lock:
            if self.voice_id != refused:  # a concurrent call switched while we were listing
                return self.voice_id
            self.voice_id = pick["voice_id"]
        log.warning("ElevenLabs voice %s is not available on this account; using %s (%s). "
                    "Set ELEVENLABS_VOICE_ID=%s in .env to keep it.",
                    refused, pick["name"], pick["voice_id"], pick["voice_id"])
        return pick["voice_id"]

    def _log_no_switch(self, refused: str, reason: str) -> None:
        level = logging.DEBUG if self._no_switch_logged else logging.WARNING  # once per client
        self._no_switch_logged = True
        log.log(level, "ElevenLabs voice %s is not available on this account and no other voice could be "
                "picked: %s. Set ELEVENLABS_VOICE_ID in .env to a voice id from the Voices page of your "
                "ElevenLabs account.", refused, reason)

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
