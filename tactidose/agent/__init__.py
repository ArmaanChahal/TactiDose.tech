"""TactiDose conversational agent (v2, ARCHITECTURE §7).

The patient talks to the agent by voice or text. It reads the patient's status and history
through tools bound to that one patient and may *request* a pill; the deterministic
``DropService`` decides and actuates. Only patient conversations are stored.

Modules:

* :mod:`~tactidose.agent.service` - :class:`AgentService` (conversation store, provider
  routing, reply checks, reply audio, speech-to-text).
* :mod:`~tactidose.agent.tools` - tool schemas + the executor bound to one patient and turn.
* :mod:`~tactidose.agent.gemini_agent` - Gemini function-calling loop (google-genai).
* :mod:`~tactidose.agent.rules_agent` - offline deterministic agent and text safety checks.
* :mod:`~tactidose.agent.prompts` - the Gemini system prompt.
* :mod:`~tactidose.agent.voice` - server-side Vosk STT, reply TTS and the audio store.
* :mod:`~tactidose.agent.voice_loop` - optional device-side voice + button loop.

Importing this package is cheap: submodules (and google-genai / vosk) load on first use.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentError", "AgentInputError", "AgentNotAllowed", "AgentUnavailable",
    "AgentService", "DeviceVoiceLoop",
]


class AgentError(Exception):
    """Base class for agent errors the HTTP layer maps to a status code."""


class AgentInputError(AgentError, ValueError):
    """Bad input (empty text, audio longer than 30 s, ...): HTTP 422."""


class AgentNotAllowed(AgentError, PermissionError):
    """The user is not a patient (only patients chat with the agent): HTTP 403."""


class AgentUnavailable(AgentError, RuntimeError):
    """A required component is unavailable (Vosk / model missing, database down): HTTP 503."""


def __getattr__(name: str) -> Any:
    if name == "AgentService":
        from tactidose.agent.service import AgentService

        return AgentService
    if name == "DeviceVoiceLoop":
        from tactidose.agent.voice_loop import DeviceVoiceLoop

        return DeviceVoiceLoop
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
