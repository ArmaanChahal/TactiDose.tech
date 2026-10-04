"""``/api/agent/*`` — the conversational agent, for the signed-in *patient* only.

The agent may only *request* a pill through its tools; ``DropService`` decides (ARCHITECTURE §2).
The patient id is always the session user's id — never taken from the request body.

* ``POST /api/agent/chat`` → ``AgentServiceAPI.chat``; with ``speak`` the reply is rendered
  to WAV (``AgentService.speak``) and served from ``/api/agent/audio/{id}.wav``.
  When the well-being check-in is on, ``WellbeingBridge.handle_chat`` sees the text first:
  check-in turns are answered there (``model="wellbeing"``) and never reach the agent or the
  stored conversation; a turn that dropped a pill gets the check-in offer appended
  (tactidose/wellbeing.py).
* ``POST /api/agent/transcribe`` — raw 16-bit little-endian mono PCM at 16 kHz
  (``application/octet-stream``, ≤ 30 s = 960 000 bytes) → ``AgentService.transcribe``.
  503 when the offline recognizer (Vosk + model) is unavailable.
* ``GET /api/agent/audio/{audio_id}.wav`` — short-lived, only for the patient it was made for.
* ``POST /api/agent/speak`` ``{text}`` -> ``{audio_url}``: the same voice (ElevenLabs when configured,
  else cached / offline OS voice) for the page's own announcements such as "pill dropped", instead
  of the browser's built-in voice. ``audio_url`` is null when no server voice is available.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from tactidose.api.common import TactiRoute, error_status, require_service, to_dict
from tactidose.auth.deps import PatientUser, ServicesDep

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", route_class=TactiRoute, tags=["agent"])

SAMPLE_RATE = 16_000
MAX_SECONDS = 30
#: 30 s of 16 kHz mono int16.
MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS
MAX_TEXT = 2000
_AUDIO_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
UNAVAILABLE = "Speech recognition is not available right now. Please type your message instead."
AGENT_NAME = "The assistant"


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(min_length=1, max_length=MAX_TEXT)
    conversation_id: StrictInt | None = Field(None, ge=1)
    input_mode: Literal["text", "voice"] = "text"
    speak: StrictBool = False


class SpeakBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(min_length=1, max_length=600)


@router.post("/speak")
def speak(body: SpeakBody, user: PatientUser, services: ServicesDep) -> dict[str, Any]:
    text = " ".join(body.text.split())
    agent = getattr(services, "agent", None)
    return {"audio_url": _speak(agent, user.user_id, text) if agent is not None and text else None}


@router.post("/chat")
def chat(body: ChatBody, user: PatientUser, services: ServicesDep) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Please say or type something.")
    wellbeing = getattr(services, "wellbeing", None)
    if wellbeing is not None:
        routed = wellbeing.handle_chat(user.user_id, text, conversation_id=body.conversation_id)
        if routed is not None:
            if body.speak and wellbeing.server_tts and services.agent is not None:
                routed["audio_url"] = _speak(services.agent, user.user_id, routed.get("text") or "")
            return routed
    agent = require_service(services, "agent", AGENT_NAME)
    reply = agent.chat(
        patient_id=user.user_id, text=text, input_mode=body.input_mode, conversation_id=body.conversation_id,
    )
    out = to_dict(reply)
    if wellbeing is not None and out.get("text"):
        out = dict(out)
        wellbeing.after_agent_turn(user.user_id, text, out)
    if body.speak and not out.get("audio_url"):
        out["audio_url"] = _speak(agent, user.user_id, out.get("text") or "")
    return out


def _speak(agent: Any, patient_id: int, text: str) -> str | None:
    speak = getattr(agent, "speak", None)
    if not text or not callable(speak):
        return None
    try:
        audio_id = speak(patient_id, text)
    except Exception:  # noqa: BLE001 - no audio is fine: the browser falls back to speechSynthesis
        log.warning("reply TTS failed for patient %s", patient_id, exc_info=True)
        return None
    return f"/api/agent/audio/{audio_id}.wav" if audio_id else None


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Audio is too long; send at most {MAX_SECONDS} seconds.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"Audio is too long; send at most {MAX_SECONDS} seconds.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/transcribe")
async def transcribe(request: Request, user: PatientUser, services: ServicesDep) -> dict[str, Any]:
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/octet-stream":
        raise HTTPException(415, "Send raw 16 kHz mono 16-bit PCM as application/octet-stream.")
    pcm = await _read_body(request, MAX_PCM_BYTES)
    if not pcm:
        raise HTTPException(422, "No audio was received.")
    if len(pcm) % 2:
        raise HTTPException(422, "Audio must be 16-bit samples (an even number of bytes).")
    agent = require_service(services, "agent", AGENT_NAME)
    fn = getattr(agent, "transcribe", None)
    if not callable(fn):
        raise HTTPException(503, UNAVAILABLE)
    try:
        result = await run_in_threadpool(fn, pcm)
    except Exception as exc:  # noqa: BLE001 - recognizer missing/broken: offer typing instead
        if error_status(exc) == 422:
            raise HTTPException(422, "That audio could not be used. Please try again or type instead.") from None
        log.warning("transcribe failed (%s): %s", type(exc).__name__, exc)
        raise HTTPException(503, UNAVAILABLE) from None
    result = dict(to_dict(result) or {})
    result.setdefault("engine", "vosk")
    result.setdefault("confidence", None)
    result["text"] = str(result.get("text") or "")
    return result


@router.get("/audio/{audio_id}.wav")
def audio(audio_id: str, user: PatientUser, services: ServicesDep) -> Response:
    if not _AUDIO_ID.match(audio_id):
        raise HTTPException(404, "Audio not found.")
    agent = require_service(services, "agent", AGENT_NAME)
    fn = getattr(agent, "audio", None)
    data = fn(audio_id, user.user_id) if callable(fn) else None
    if not data:
        raise HTTPException(404, "Audio not found (it may have expired).")
    return Response(
        content=bytes(data), media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"},
    )
