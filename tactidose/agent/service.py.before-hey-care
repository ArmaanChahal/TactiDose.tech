"""AgentService: the patient's conversational agent (implements ``AgentServiceAPI``, ARCHITECTURE v2 §7).

``chat(patient_id, text, input_mode, conversation_id)`` runs one turn:

1. **Who** - only an active ``role="patient"`` user may chat (:class:`~tactidose.agent.AgentNotAllowed`
   otherwise); caregivers read conversations but never create them.
2. **Conversation** - continue ``conversation_id`` (or, when omitted, the patient's latest
   conversation) if it belongs to the patient and its last message is at most 30 minutes old;
   otherwise start a new one. The user message (``role="user"``, ``input_mode``) is committed
   before any provider runs. DB failure -> :class:`~tactidose.agent.AgentUnavailable`.
3. **Provider** - deterministic first: emergencies get the 911 sentence and "stop" calls
   ``DropService.interrupt()``, whatever the provider. Then Gemini (``effective_agent_provider``
   = gemini and a key or injected client) or the offline rules agent. Any Gemini failure ->
   the rules agent answers (``model="rules (fallback)"``). Tools are bound to this patient
   and turn (:class:`~tactidose.agent.tools.PatientTools`), with a :class:`TurnGuard` from the
   patient's own words.
   **Circuit breaker** - after a Gemini failure (classified with ``netsafe.classify_exception``;
   one warning ``Gemini unavailable (<code>: <message>); the offline assistant answers for the
   next N s``) the next turns skip Gemini for ``agent_retry_after_s`` seconds of
   ``time.monotonic`` (demo clock travel does not count) and the rules agent answers at once,
   also as ``"rules (fallback)"``; 0 = try Gemini every turn. The first turn after the pause
   tries Gemini again; a success closes the breaker. ``status()`` shows ``gemini_retry_in_s``
   and ``gemini_last_error`` (netsafe code or None).
4. **Reply check** (Gemini replies) - a reply that claims a drop although no request in this
   turn returned DROPPED, that denies a DROPPED drop, or that follows a FAILED/UNCERTAIN drop is
   replaced by the deterministic sentence (``model="rules (safety)"``).
5. **Store + publish** - every tool call (``role="tool"``: tool_name/args/result) and the reply
   (``role="assistant"``, ``model``) are stored; ``Topic.AGENT {patient_id, conversation_id,
   message_id, role}`` is published per stored message. ``Topic.DROP`` is DropService's job.

Speech: ``speak(patient_id, text) -> audio_id | None`` renders WAV (ElevenLabs -> cache ->
offline voice) into an in-memory per-patient store (10-minute TTL) read back with
``audio(audio_id, patient_id)``; ``transcribe(pcm16)`` is full-vocabulary Vosk STT.
All public methods are thread-safe; turns of the same patient are serialised.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import func, select

from tactidose.agent import AgentInputError, AgentNotAllowed, AgentUnavailable
from tactidose.agent.gemini_agent import AgentModelError, GeminiAgent
from tactidose.agent.prompts import system_prompt
from tactidose.agent.rules_agent import (
    RulesAgent,
    TextFlags,
    analyse,
    describe_outcome,
    turn_guard,
)
from tactidose.agent.tools import PatientTools, ToolCall
from tactidose.agent.voice import AudioStore, ReplyTTS, VoskTranscriber
from tactidose.config import Settings
from tactidose.core import phrases
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import AgentReply, DropServiceAPI
from tactidose.db.models import Conversation, ConversationMessage, Role, User
from tactidose.db.session import Database
from tactidose.integrations import netsafe

log = logging.getLogger(__name__)

MODEL_RULES = "rules"
MODEL_FALLBACK = "rules (fallback)"
MODEL_SAFETY = "rules (safety)"
#: A conversation whose last message is older than this is closed; the next message starts a new one.
CONVERSATION_ROLLOVER = timedelta(minutes=30)
MAX_INPUT_CHARS = 2000
MAX_REPLY_CHARS = 600
AUDIO_TTL_S = 600.0

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'"})
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_MARKDOWN = re.compile(r"\*\*|__|`+|^\s{0,3}#{1,6}\s*|^\s*[-*•]\s+", re.MULTILINE)
#: First-person / present claims of a NEW drop ("I've dropped", "here's your pill", ...).
_STRONG_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'ve| have)?(?: just)? (?:dropped|released|dispensed|given you|gave you|sent you)"
    r"|we(?:'ve| have)?(?: just)? (?:dropped|released|dispensed)"
    r"|(?:has|have)(?: just)?(?: been)? (?:dropped|released|dispensed)"
    r"|(?:is|are)(?: now)? (?:dropping|being dropped|on (?:its|the) way|in the (?:tray|cup|chute|dish))"
    r"|dropping (?:it|one|your|a|the)"
    r"|here(?:'s| is| are) your"
    r")\b"
)
_DROP_WORD = re.compile(r"\b(?:dropped|dropping|drop|released|dispensed)\b")
_DROPPED_WORD = re.compile(r"\b(?:dropped|released|dispensed)\b")
_NEGATION = re.compile(
    r"\b(?:not|no|never|nothing|unable|cannot|can't|couldn't|won't|wasn't|didn't|isn't|hasn't|haven't|"
    r"don't|doesn't|weren't|aren't)\b")
_PAST_REFERENCE = re.compile(
    r"\b(?:last|earlier|ago|yesterday|previous|previously|already|this morning|this afternoon|"
    r"this evening|at \d{1,2}(?::\d{2})?|today at|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def message_view(m: ConversationMessage) -> dict[str, Any]:
    """API ``Message`` (+ ``model``)."""
    return {
        "message_id": m.message_id,
        "conversation_id": m.conversation_id,
        "role": m.role,
        "content": m.content,
        "input_mode": m.input_mode,
        "tool_name": m.tool_name,
        "tool_args": m.tool_args,
        "tool_result": m.tool_result,
        "model": m.model,
        "created_at": _iso(m.created_at),
    }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _strip_symbols(text: str) -> str:
    """Remove emoji / pictographs (read aloud as "smiling face") and their joiners."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "So" and ch not in "‍️")


def clean_reply(text: str | None) -> str:
    """Spoken-friendly reply: no markdown or emoji, one line, capped at a sentence boundary."""
    out = " ".join(_strip_symbols(_MARKDOWN.sub("", str(text or ""))).split())
    if len(out) <= MAX_REPLY_CHARS:
        return out
    cut = out[:MAX_REPLY_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= 40:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "."


def claims_new_drop(text: str, *, strict: bool) -> bool:
    """Does ``text`` say a pill dropped (now)? ``strict`` also counts mentions of past drops."""
    for sentence in _SENTENCES.split(text.lower().translate(_APOSTROPHES)):
        if _STRONG_CLAIM.search(sentence):
            return True
        if _DROPPED_WORD.search(sentence) and not _NEGATION.search(sentence) \
                and (strict or not _PAST_REFERENCE.search(sentence)):
            return True
    return False


def confirms_drop(text: str) -> bool:
    """Does ``text`` tell the patient the pill dropped (a positive, non-negated drop sentence)?"""
    return any(_DROP_WORD.search(s) and not _NEGATION.search(s)
               for s in _SENTENCES.split(text.lower().translate(_APOSTROPHES)))


class AgentService:
    """Conversational agent for patients. See the module docstring."""

    def __init__(
        self,
        db: Database,
        drops: DropServiceAPI,
        clock: Clock,
        settings: Settings,
        *,
        bus: EventBus | None = None,
        genai_client: Any | None = None,
        tts: Any | None = None,
    ) -> None:
        self.db = db
        self.drops = drops
        self.clock = clock
        self.settings = settings
        self.bus = bus
        self._genai_client = genai_client
        self._gemini: GeminiAgent | None = None
        self._rules = RulesAgent(settings, clock)
        self._tts = tts
        self._tts_built = tts is not None
        self._owns_tts = False
        self._audio = AudioStore(ttl_s=AUDIO_TTL_S)
        self._stt = VoskTranscriber(settings)
        self._lock = threading.Lock()
        self._patient_locks: dict[int, threading.Lock] = {}
        self._provider_warned = False
        # Gemini circuit breaker (guarded by _lock). Real elapsed time, never the demo clock.
        self._monotonic: Callable[[], float] = time.monotonic
        self._gemini_retry_at: float | None = None
        self._gemini_failure: netsafe.Failure | None = None

    # ================================================================== provider
    @property
    def provider(self) -> str:
        """``"gemini"`` or ``"rules"`` (Gemini needs a key or an injected client)."""
        wanted = self.settings.effective_agent_provider
        if wanted == "gemini" and self._genai_client is None and not self.settings.gemini_configured:
            if not self._provider_warned:
                self._provider_warned = True
                log.warning("agent provider 'gemini' requested but GEMINI_API_KEY is empty; using the rules agent")
            return "rules"
        return "gemini" if wanted == "gemini" else "rules"

    @property
    def model(self) -> str:
        return self._gemini_agent().model if self.provider == "gemini" else MODEL_RULES

    def _gemini_agent(self) -> GeminiAgent:
        with self._lock:
            if self._gemini is None:
                self._gemini = GeminiAgent(self.settings, client=self._genai_client)
            return self._gemini

    def status(self) -> dict[str, Any]:
        """Cheap summary for ``/api/health``: provider, model, speech availability, and the Gemini
        breaker: ``gemini_retry_in_s`` (whole seconds until Gemini is tried again, 0 = not paused)
        and ``gemini_last_error`` (netsafe code of the last failure, None after a success)."""
        with self._lock:
            failure = self._gemini_failure
        return {
            "provider": self.provider,
            "model": self.model,
            "stt": "loaded" if self._stt.loaded else ("available" if self._stt.available() else "unavailable"),
            "tts": bool(self._tts_engine() is not None and getattr(self._tts_engine(), "available", True)),
            "gemini_retry_in_s": math.ceil(self._gemini_pause_s()),
            "gemini_last_error": failure.code if failure is not None else None,
        }

    # ================================================================== Gemini circuit breaker
    def _gemini_pause_s(self) -> float:
        """Seconds until Gemini is tried again (0 = call it now)."""
        with self._lock:
            if self._gemini_retry_at is None:
                return 0.0
            return max(0.0, self._gemini_retry_at - self._monotonic())

    def _gemini_failed(self, exc: BaseException) -> None:
        """Record a failure and open the breaker for ``agent_retry_after_s`` (one warning per opening)."""
        failure = netsafe.classify_exception(exc)
        wait = max(0.0, float(self.settings.agent_retry_after_s))
        with self._lock:
            now = self._monotonic()
            already_open = self._gemini_retry_at is not None and now < self._gemini_retry_at
            self._gemini_failure = failure
            self._gemini_retry_at = now + wait if wait > 0 else None
        if already_open:   # a concurrent turn failed too: the breaker is already open and logged
            return
        what = self._redact_key(str(failure))[:300]
        if wait > 0:
            log.warning("Gemini unavailable (%s); the offline assistant answers for the next %d s",
                        what, math.ceil(wait))
        else:
            log.warning("Gemini unavailable (%s); the offline assistant answers this turn", what)

    def _gemini_succeeded(self) -> None:
        with self._lock:
            was_failing = self._gemini_failure is not None
            self._gemini_retry_at = None
            self._gemini_failure = None
        if was_failing:
            log.info("Gemini is answering again")

    def _redact_key(self, text: str) -> str:
        key = self.settings.gemini_api_key.get_secret_value() if self.settings.gemini_api_key else ""
        return text.replace(key, "***") if key else text

    # ================================================================== chat
    def chat(self, *, patient_id: int, text: str, input_mode: str = "text",
             conversation_id: int | None = None) -> AgentReply:
        message = self._clean_input(text)
        mode = "voice" if str(input_mode or "").strip().lower() == "voice" else "text"
        pid = int(patient_id)
        with self._patient_lock(pid):
            name, conv_id, history, user_msg = self._begin_turn(pid, message, mode, conversation_id)
            self._publish(pid, user_msg)
            flags = analyse(message)
            tools = PatientTools(db=self.db, drops=self.drops, clock=self.clock, settings=self.settings,
                                 patient_id=pid, conversation_id=conv_id, guard=turn_guard(flags), bus=self.bus)
            reply, model = self._respond(message, flags, tools, history, name)
            reply = clean_reply(reply) or phrases.AGENT_ERROR
            stored = self._finish_turn(pid, conv_id, tools.calls, reply, model)
        for msg in stored:
            self._publish(pid, msg)
        log.info("agent turn: patient=%s conversation=%s model=%s tools=%s drops=%s", pid, conv_id, model,
                 [c.name for c in tools.calls], [o.get("status") for o in tools.drop_outcomes])
        return AgentReply(conversation_id=conv_id, text=reply, model=model,
                          actions=list(tools.drop_outcomes), messages=[user_msg, *stored])

    def _respond(self, text: str, flags: TextFlags, tools: PatientTools,
                 history: Sequence[Mapping[str, Any]], patient_name: str | None) -> tuple[str, str]:
        provider = self.provider
        deterministic = MODEL_RULES if provider == "rules" else MODEL_SAFETY
        if flags.emergency:
            return phrases.EMERGENCY, deterministic
        if flags.stop:
            return self._rules.respond(text, tools, history=history), deterministic
        if provider != "gemini":
            return self._rules.respond(text, tools, history=history), MODEL_RULES
        if self._gemini_pause_s() > 0:   # breaker open: answer at once instead of waiting on a timeout
            return self._rules.respond(text, tools, history=history), MODEL_FALLBACK
        try:
            turn = self._gemini_agent().respond(
                system_instruction=system_prompt(patient_name=patient_name, now_local=self.clock.local_now(),
                                                 num_slots=int(self.settings.num_slots)),
                history=history, user_text=text, tools=tools)
        except Exception as exc:  # noqa: BLE001 - never let a provider failure or bug break the turn
            if not isinstance(exc, AgentModelError):
                log.exception("Gemini agent failed unexpectedly; answering with the rules agent")
            self._gemini_failed(exc)
            return self._rules.respond(text, tools, history=history), MODEL_FALLBACK
        self._gemini_succeeded()
        checked = self._check_model_reply(clean_reply(turn.text), tools)
        if checked is None:
            log.warning("Gemini reply did not match the drop outcome; using the deterministic reply")
            if tools.last_outcome is not None:
                return describe_outcome(tools.last_outcome, clock=self.clock), MODEL_SAFETY
            return self._rules.respond(text, tools, history=history), MODEL_SAFETY
        return checked, turn.model

    @staticmethod
    def _check_model_reply(text: str, tools: PatientTools) -> str | None:
        """``text`` when it agrees with this turn's drop outcome, else None (replace it)."""
        if not text:
            return None
        outcome = tools.last_outcome
        if outcome is None:
            if tools.drop_requested:
                return None  # the request raised: its outcome is unknown, say nothing about the pill
            return None if claims_new_drop(text, strict=False) else text
        status = str(outcome.get("status") or "")
        if status in ("FAILED", "UNCERTAIN"):
            return None  # hardware trouble: always the deterministic sentence
        if status == "DROPPED":
            return text if confirms_drop(text) else None
        return None if claims_new_drop(text, strict=True) else text

    # ================================================================== storage
    def _clean_input(self, text: Any) -> str:
        if not isinstance(text, str) or not text.strip():
            raise AgentInputError("text must not be empty")
        cleaned = " ".join(text.split())
        return cleaned[:MAX_INPUT_CHARS]

    def _patient_lock(self, patient_id: int) -> threading.Lock:
        with self._lock:
            lock = self._patient_locks.get(patient_id)
            if lock is None:
                lock = self._patient_locks[patient_id] = threading.Lock()
            return lock

    def _begin_turn(self, pid: int, text: str, mode: str, conversation_id: int | None
                    ) -> tuple[str | None, int, list[dict[str, Any]], dict[str, Any]]:
        now = self.clock.now()
        try:
            with self.db.session() as s:
                user = s.get(User, pid)
                if user is None or user.role != Role.PATIENT.value or not user.is_active:
                    raise AgentNotAllowed("only patients can talk to the agent")
                conv = self._open_conversation(s, pid, conversation_id, now)
                history: list[dict[str, Any]] = []
                if conv is None:
                    conv = Conversation(patient_id=pid, channel=mode, title=_title(text), started_at=now,
                                        last_message_at=now)
                    s.add(conv)
                    s.flush()
                else:
                    if conv.channel != mode:
                        conv.channel = "mixed"
                    rows = s.scalars(
                        select(ConversationMessage)
                        .where(ConversationMessage.conversation_id == conv.conversation_id,
                               ConversationMessage.role.in_(("user", "assistant")))
                        .order_by(ConversationMessage.message_id.desc())
                        .limit(int(self.settings.agent_history_messages))
                    ).all()
                    history = [message_view(m) for m in reversed(rows)]
                msg = ConversationMessage(conversation_id=conv.conversation_id, patient_id=pid, role="user",
                                          content=text, input_mode=mode, created_at=now)
                s.add(msg)
                conv.last_message_at = now
                s.flush()
                return user.display_name, conv.conversation_id, history, message_view(msg)
        except AgentNotAllowed:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed: no stored turn, no agent action
            log.exception("could not open a conversation for patient %s", pid)
            raise AgentUnavailable("the conversation could not be stored") from exc

    @staticmethod
    def _open_conversation(s: Any, pid: int, conversation_id: int | None, now: datetime) -> Conversation | None:
        conv: Conversation | None
        if conversation_id is not None:
            try:
                conv = s.get(Conversation, int(conversation_id))
            except (TypeError, ValueError):
                conv = None
            if conv is not None and conv.patient_id != pid:
                conv = None  # never continue another patient's conversation
        else:
            conv = s.scalars(
                select(Conversation).where(Conversation.patient_id == pid)
                .order_by(Conversation.last_message_at.desc(), Conversation.conversation_id.desc()).limit(1)
            ).first()
        if conv is not None and now - conv.last_message_at > CONVERSATION_ROLLOVER:
            conv = None
        return conv

    def _finish_turn(self, pid: int, conv_id: int, calls: Sequence[ToolCall], reply: str,
                     model: str) -> list[dict[str, Any]]:
        now = self.clock.now()
        try:
            with self.db.session() as s:
                rows = [
                    ConversationMessage(conversation_id=conv_id, patient_id=pid, role="tool", content=call.summary,
                                        tool_name=call.name[:64], tool_args=_json_safe(call.args),
                                        tool_result=_json_safe(call.result), created_at=call.at)
                    for call in calls
                ]
                rows.append(ConversationMessage(conversation_id=conv_id, patient_id=pid, role="assistant",
                                                content=reply, model=model[:64], created_at=now))
                s.add_all(rows)
                conv = s.get(Conversation, conv_id)
                if conv is not None:
                    conv.last_message_at = now
                s.flush()
                return [message_view(m) for m in rows]
        except Exception:  # noqa: BLE001 - the drop (if any) is recorded by DropService anyway
            log.exception("could not store the agent turn for conversation %s", conv_id)
            return []

    def _publish(self, pid: int, msg: Mapping[str, Any]) -> None:
        if self.bus is None:
            return
        self.bus.publish(Topic.AGENT, {"patient_id": pid, "conversation_id": msg.get("conversation_id"),
                                       "message_id": msg.get("message_id"), "role": msg.get("role")})

    # ================================================================== reading
    def conversations(self, patient_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        """``[{conversation_id, started_at, last_message_at, channel, title, message_count}]`` newest first."""
        limit = max(1, min(500, int(limit)))
        with self.db.session() as s:
            counts = (select(ConversationMessage.conversation_id, func.count().label("n"))
                      .group_by(ConversationMessage.conversation_id).subquery())
            rows = s.execute(
                select(Conversation, func.coalesce(counts.c.n, 0))
                .outerjoin(counts, counts.c.conversation_id == Conversation.conversation_id)
                .where(Conversation.patient_id == int(patient_id))
                .order_by(Conversation.last_message_at.desc(), Conversation.conversation_id.desc())
                .limit(limit)
            ).all()
            return [{
                "conversation_id": c.conversation_id,
                "started_at": _iso(c.started_at),
                "last_message_at": _iso(c.last_message_at),
                "channel": c.channel,
                "title": c.title,
                "message_count": int(n or 0),
            } for c, n in rows]

    def get_conversation(self, patient_id: int, conversation_id: int) -> dict[str, Any] | None:
        """The conversation summary if it belongs to ``patient_id`` (else None -> HTTP 404)."""
        with self.db.session() as s:
            conv = s.get(Conversation, int(conversation_id))
            if conv is None or conv.patient_id != int(patient_id):
                return None
            count = s.scalar(select(func.count()).select_from(ConversationMessage)
                             .where(ConversationMessage.conversation_id == conv.conversation_id))
            return {
                "conversation_id": conv.conversation_id,
                "started_at": _iso(conv.started_at),
                "last_message_at": _iso(conv.last_message_at),
                "channel": conv.channel,
                "title": conv.title,
                "message_count": int(count or 0),
            }

    def messages(self, patient_id: int, conversation_id: int) -> list[dict[str, Any]]:
        """Messages of ``conversation_id`` in order; ``[]`` if it is not this patient's."""
        with self.db.session() as s:
            conv = s.get(Conversation, int(conversation_id))
            if conv is None or conv.patient_id != int(patient_id):
                return []
            rows = s.scalars(select(ConversationMessage)
                             .where(ConversationMessage.conversation_id == conv.conversation_id)
                             .order_by(ConversationMessage.message_id)).all()
            return [message_view(m) for m in rows]

    # ================================================================== speech
    def _tts_engine(self) -> Any | None:
        with self._lock:
            if not self._tts_built:
                self._tts_built = True
                if self.settings.tts_provider != "none":
                    self._tts = ReplyTTS(self.settings)
                    self._owns_tts = True
            return self._tts

    def speak(self, patient_id: int, text: str) -> str | None:
        """Render ``text`` to WAV for ``patient_id``; returns an audio id (``None`` = no TTS)."""
        engine = self._tts_engine()
        cleaned = " ".join(str(text or "").split())
        if engine is None or not cleaned:
            return None
        try:
            wav = engine.synthesize(cleaned)
        except Exception:  # noqa: BLE001 - optional: the browser can speak instead
            log.exception("reply speech failed")
            return None
        if not wav:
            return None
        return self._audio.put(int(patient_id), wav)

    def audio(self, audio_id: str, patient_id: int) -> bytes | None:
        """WAV bytes of ``audio_id`` if it was rendered for ``patient_id`` within the last 10 minutes."""
        return self._audio.get(audio_id, int(patient_id))

    @staticmethod
    def audio_url(audio_id: str) -> str:
        return f"/api/agent/audio/{audio_id}.wav"

    def transcribe(self, pcm16: bytes) -> dict[str, Any]:
        """``{text, confidence, engine: "vosk"}`` for 16 kHz mono int16 PCM (<= 30 s).
        Raises AgentUnavailable (no Vosk / model) or AgentInputError (bad / too long audio)."""
        return self._stt.transcribe(pcm16)

    def preload_speech_model(self) -> bool:
        """Load the Vosk model now (call from a background thread at startup). False if unavailable."""
        try:
            self._stt.load()
            return True
        except AgentUnavailable as exc:
            log.info("%s", exc)
            return False

    def close(self) -> None:
        self._stt.close()
        if self._owns_tts and self._tts is not None:
            try:
                self._tts.close()
            except Exception:  # noqa: BLE001
                log.debug("closing reply TTS failed", exc_info=True)


def _title(text: str) -> str:
    words = text.split()
    title = " ".join(words)[:60]
    return title if len(" ".join(words)) <= 60 else title.rsplit(" ", 1)[0] + "..."
