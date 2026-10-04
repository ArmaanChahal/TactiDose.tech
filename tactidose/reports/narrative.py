"""Report narrative: a factual summary of the period's patient <-> assistant conversations.

Two sources (``Narrative.source``):

* ``"gemini"`` — when ``settings.report_ai_summary`` is on, Gemini is configured (or a client
  was injected) and the period has patient messages. One ``client.models.generate_content``
  call (google-genai, default temperature, no tools) with a strict system instruction: report
  only what the patient asked for and mentioned, quoting their words; mention refused
  requests; no diagnosis, advice or recommendations; no names; at most 200 words. The model
  output is untrusted: it is cleaned (plain text, bullets), capped at 200 words and rejected
  (-> rules) when it is empty, blocked, truncated or contains prescriptive/diagnostic phrasing
  outside quotes.
* ``"rules"`` — deterministic bullets from the statistics plus the patient's notable messages
  (possible symptoms/concerns and pill requests), quoted verbatim and timestamped. Used
  whenever Gemini is off, unavailable or fails (``Narrative.fallback_reason`` says why).

The same turn analysis picks the timestamped conversation excerpts printed in the PDF
(:func:`select_excerpts`). Nothing here can request or authorise a drop. The Gemini client never
follows redirects (``netsafe.gemini_http_options``: the key is a custom header) and failures are
logged as a netsafe code + plain message, never raw exception text.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tactidose.config import Settings
from tactidose.integrations import netsafe
from tactidose.reports.data import (
    MessageRow,
    ReportData,
    fmt_date,
    fmt_datetime,
    fmt_pct,
    fmt_time,
)

log = logging.getLogger(__name__)

__all__ = [
    "MAX_AI_WORDS",
    "SYSTEM_INSTRUCTION",
    "Excerpt",
    "Narrative",
    "ReportNarrator",
    "Turn",
    "build_prompt",
    "clean_ai_text",
    "conversation_turns",
    "reason_label",
    "rules_narrative",
    "select_excerpts",
    "source_label",
]

MAX_AI_WORDS = 200
#: Conversation turns printed as excerpts in the PDF (notable turns first, then the most recent).
MAX_EXCERPT_TURNS = 24
MAX_EXCERPT_CHARS = 400
#: Transcript sent to Gemini: newest messages kept within these bounds.
MAX_PROMPT_MESSAGES = 400
MAX_PROMPT_CHARS = 24000
MAX_PROMPT_MESSAGE_CHARS = 600

SYSTEM_INSTRUCTION = """\
You write a short factual summary of a patient's conversations with the assistant built into \
their TactiDose pill dispenser. The summary goes into a report for the patient's doctor and \
family. TactiDose is a prototype, not a medical device.

Rules:
- Use only the transcript and the dispenser log you are given. Never add facts.
- Say what the patient asked for, and quote any symptoms, concerns or side effects the patient \
mentioned in their own words, in double quotes, with the day and time. Do not interpret, \
rename or explain symptoms, and do not guess causes.
- Mention requests that were refused and the stated reason (for example the cooldown after a \
recent drop, an empty container, or a dose that was already dropped).
- Do not diagnose. Do not give medical advice or recommendations. Do not suggest dose or \
schedule changes. Do not judge the patient.
- Do not include names, email addresses or phone numbers; write "the patient".
- Plain text only, no headings and no markdown. Use short sentences or lines starting with \
"- ". At most 200 words.
- If nothing notable was said, say so in one sentence."""

_CONCERN_RE = re.compile(
    r"\b(pain\w*|hurt\w*|ache\w*|headache\w*|migraine\w*|dizz\w*|nause\w*|sick|ill|unwell|vomit\w*|"
    r"throw(?:ing)? up|tired|fatigue\w*|exhaust\w*|sleep\w*|insomnia|rash\w*|itch\w*|chest|breath\w*|"
    r"faint\w*|fever\w*|cough\w*|bleed\w*|blood|swell\w*|side[- ]?effects?|worse|anxi\w*|depress\w*|"
    r"sad|confus\w*|fell|fall(?:en|ing)?|forg[eo]t\w*|missed|overdos\w*|emergenc\w*|911|allerg\w*|"
    r"stomach\w*|diarrh\w*|constipat\w*|heart\w*|palpitat\w*|blurr\w*|vision|numb\w*|weak\w*|"
    r"shak\w*|tremor\w*|cramp\w*|feel(?:ing|s)?\s+(?:bad|awful|terrible|strange|weird|off|funny))\b",
    re.IGNORECASE,
)
_PILL_ASK_RE = re.compile(
    r"\b(pills?|drop|dose|doses|medicine|medication|meds|tablets?|candy|vitamins?|another one)\b",
    re.IGNORECASE,
)
#: Prescriptive / diagnostic phrasing that must not appear outside quotes in an AI summary.
_UNSAFE_RE = re.compile(
    r"\b(diagnos\w*|prescrib\w*|i (?:would )?recommend|we (?:would )?recommend|it is recommended|"
    r"(?:should|must|needs? to) (?:take|increase|decrease|reduce|lower|raise|stop|start|skip|double|change)|"
    r"(?:increase|decrease|reduce|lower|raise|double|halve) (?:the|their|his|her|your) (?:dose|dosage)|"
    r"probably has|likely (?:has|suffers?)|suffers? from)\b",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”|«[^»]*»")
_BLOCKED_FINISH = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
                             "IMAGE_SAFETY", "MALFORMED_FUNCTION_CALL", "MAX_TOKENS", "LANGUAGE"})

_REASON_LABELS = {
    "COOLDOWN": "cooldown after a recent drop",
    "EMPTY": "container empty",
    "NO_MEDICATION": "no medication in that container",
    "UNKNOWN_MEDICATION": "medication not recognised",
    "ALREADY_SATISFIED": "dose already dropped",
    "IN_PROGRESS": "another drop was in progress",
    "DEVICE_UNAVAILABLE": "device not available",
    "NEEDS_REVIEW": "an uncertain drop is waiting for review",
    "NOT_ALLOWED": "not allowed for this account",
    "DB_ERROR": "records could not be read, so it failed safe",
    "NO_PILL": "the device found no pill",
}
_SOURCE_LABELS = {
    "schedule": "automatic (schedule)",
    "manual": "app button",
    "agent": "assistant",
    "button": "device button",
    "demo": "demo panel",
}
_STATUS_WORDS = {
    "DROPPED": "dropped",
    "DENIED": "refused",
    "FAILED": "failed",
    "UNCERTAIN": "uncertain, needs review",
}


def reason_label(code: str | None) -> str:
    """Plain-language label for a DenyReason / hardware code."""
    if not code:
        return "unknown reason"
    return _REASON_LABELS.get(code, code.replace("_", " ").lower())


def source_label(source: str | None) -> str:
    return _SOURCE_LABELS.get(source or "", source or "unknown")


# --------------------------------------------------------------------------- turns & excerpts


@dataclass(frozen=True)
class Turn:
    """One patient message and what followed it (assistant replies, tool calls) in a conversation."""

    conversation_id: int
    user: MessageRow | None
    replies: tuple[MessageRow, ...] = ()
    tools: tuple[MessageRow, ...] = ()

    @property
    def at(self) -> datetime:
        first = self.user or (self.replies[0] if self.replies else self.tools[0])
        return first.created_at

    @property
    def first_id(self) -> int:
        first = self.user or (self.replies[0] if self.replies else self.tools[0])
        return first.message_id

    @property
    def pill_requests(self) -> tuple[MessageRow, ...]:
        return tuple(t for t in self.tools if t.tool_name == "request_pill")

    @property
    def concern(self) -> bool:
        return bool(self.user and _CONCERN_RE.search(self.user.content or ""))

    @property
    def asks_for_pill(self) -> bool:
        return bool(self.pill_requests) or bool(self.user and _PILL_ASK_RE.search(self.user.content or ""))

    @property
    def refused(self) -> bool:
        return any(str((t.tool_result or {}).get("status")) in ("DENIED", "FAILED", "UNCERTAIN")
                   for t in self.pill_requests)

    @property
    def notable(self) -> bool:
        return self.concern or self.asks_for_pill or self.refused


@dataclass(frozen=True)
class Excerpt:
    at: datetime
    speaker: str              # "patient" | "assistant" | "dispenser"
    text: str
    conversation_id: int
    input_mode: str | None = None
    notable: bool = False


def conversation_turns(data: ReportData) -> list[Turn]:
    """Messages grouped into turns, chronological (messages are already ordered by time)."""
    by_conv: dict[int, list[MessageRow]] = {}
    for m in data.messages:
        by_conv.setdefault(m.conversation_id, []).append(m)
    turns: list[Turn] = []
    for cid, msgs in by_conv.items():
        user: MessageRow | None = None
        replies: list[MessageRow] = []
        tools: list[MessageRow] = []
        started = False
        for m in msgs:
            if m.role == "user":
                if started:
                    turns.append(Turn(cid, user, tuple(replies), tuple(tools)))
                user, replies, tools, started = m, [], [], True
            elif m.role == "tool":
                tools.append(m)
                started = True
            else:
                replies.append(m)
                started = True
        if started:
            turns.append(Turn(cid, user, tuple(replies), tuple(tools)))
    turns.sort(key=lambda t: (t.at, t.first_id))
    return turns


def select_excerpts(data: ReportData, *, max_turns: int = MAX_EXCERPT_TURNS,
                    turns: list[Turn] | None = None) -> list[Excerpt]:
    """Timestamped excerpts for the PDF: notable turns first (newest kept), then the most recent
    other turns, printed oldest first. Tool calls appear only as pill-request outcomes."""
    all_turns = [t for t in (turns if turns is not None else conversation_turns(data))
                 if t.user is not None or t.replies]
    notable = [t for t in all_turns if t.notable]
    chosen = notable[-max_turns:] if max_turns > 0 else []
    room = max_turns - len(chosen)
    if room > 0:
        others = [t for t in all_turns if not t.notable]
        chosen += others[-room:]
    chosen.sort(key=lambda t: (t.at, t.first_id))
    out: list[Excerpt] = []
    for t in chosen:
        if t.user is not None:
            out.append(Excerpt(t.user.created_at, "patient", _clip(t.user.content, MAX_EXCERPT_CHARS),
                               t.conversation_id, t.user.input_mode, t.notable))
        for tool in t.pill_requests:
            out.append(Excerpt(tool.created_at, "dispenser", describe_pill_request(tool),
                               t.conversation_id, None, t.notable))
        if t.replies:
            last = t.replies[-1]
            out.append(Excerpt(last.created_at, "assistant", _clip(last.content, MAX_EXCERPT_CHARS),
                               t.conversation_id, None, t.notable))
    return out


def describe_pill_request(tool: MessageRow) -> str:
    """``Pill request refused (cooldown after a recent drop): You can have another pill at 8:40 AM.``"""
    result = tool.tool_result or {}
    status = str(result.get("status") or "UNKNOWN")
    word = _STATUS_WORDS.get(status, status.lower())
    text = f"Pill request {word}"
    reason = result.get("reason")
    if reason and status != "DROPPED":
        text += f" ({reason_label(str(reason))})"
    message = str(result.get("message") or "").strip()
    if message:
        text += f": {_clip(message, 200)}"
    return text if text.endswith((".", "!", "?", "…")) else text + "."


# --------------------------------------------------------------------------- rules narrative


def rules_narrative(data: ReportData, stats: dict[str, Any], turns: list[Turn] | None = None) -> str:
    """Deterministic bullet summary from the statistics and the patient's notable messages."""
    turns = turns if turns is not None else conversation_turns(data)
    d, dr, cv, al = stats["doses"], stats["drops"], stats["conversations"], stats["alerts"]
    lines: list[str] = []
    if d["scheduled"]:
        line = (f"- Scheduled doses: {d['scheduled']}. Dropped: {d['dispensed']} "
                f"({d['on_time']} on time, {d['late']} late). Missed: {d['missed']}.")
        still_open = d["pending"] + d["hardware_errors"]
        if still_open:
            line += f" Still open: {still_open}."
        lines.append(line)
        if d["adherence_rate"] is not None:
            decided = d["dispensed"] + d["missed"]
            lines.append(f"- Adherence for scheduled doses: {fmt_pct(d['adherence_rate'])} "
                         f"({d['dispensed']} of {decided}).")
    else:
        lines.append("- No scheduled doses in this period.")
    if dr["on_request_drops"]:
        parts = [f"{v['dropped']} via the {source_label(src)}" for src, v in dr["by_source"].items()
                 if src != "schedule" and v["dropped"]]
        lines.append(f"- Pills dropped on request: {dr['on_request_drops']} ({', '.join(parts)}).")
    if dr["refused_requests"]:
        parts = [f"{reason_label(r)}: {n}" for r, n in dr["refused_by_reason"].items()]
        lines.append(f"- Refused requests: {dr['refused_requests']} ({'; '.join(parts)}).")
    if dr["failed"] or dr["uncertain"]:
        line = f"- Drop problems: {dr['failed']} failed, {dr['uncertain']} uncertain"
        if dr["needs_review"]:
            line += f" ({dr['needs_review']} still to be reviewed)"
        lines.append(line + ".")
    alert_parts = [f"{n} {label}" for kind, label in (("LOW_STOCK", "low-stock"), ("EMPTY", "empty-container"),
                                                     ("MISSED_DOSE", "missed-dose"), ("DEVICE_ALERT", "device"))
                   if (n := al.get(kind, 0))]
    if alert_parts:
        lines.append(f"- Alerts sent: {', '.join(alert_parts)}.")
    if cv["patient_messages"]:
        convs = cv["conversations"]
        lines.append(f"- Conversations with the assistant: {convs}, with {cv['patient_messages']} "
                     f"message{'s' if cv['patient_messages'] != 1 else ''} from the patient "
                     f"({cv['voice_messages']} by voice).")
        if cv["agent_pill_requests"]:
            parts = [f"{n} {_STATUS_WORDS.get(s, s.lower())}" for s, n in cv["agent_pill_requests_by_status"].items()]
            lines.append(f"- Pill requests through the assistant: {cv['agent_pill_requests']} ({', '.join(parts)}).")
        concerns = [t for t in turns if t.user is not None and t.concern][-4:]
        if concerns:
            lines.append("- Messages that mention symptoms or concerns (the patient's own words):")
            lines += [f"  - {_quote_line(data, t.user)}" for t in concerns if t.user is not None]
        asks = [t for t in turns if t.user is not None and t.asks_for_pill and not t.concern][-2:]
        if asks:
            lines.append("- Recent requests (the patient's own words):")
            lines += [f"  - {_quote_line(data, t.user)}" for t in asks if t.user is not None]
    else:
        lines.append("- No conversations with the assistant in this period.")
    return "\n".join(lines)


def _quote_line(data: ReportData, msg: MessageRow) -> str:
    return f'{fmt_datetime(data.local(msg.created_at))}: "{_clip(_one_line(msg.content), 120)}"'


# --------------------------------------------------------------------------- Gemini


@dataclass(frozen=True)
class Narrative:
    text: str
    source: str                         # "gemini" | "rules"
    model: str | None = None
    #: Why the rules summary was used: disabled | not_configured | no_conversations | timeout |
    #: network | blocked | empty | unsafe_output | sdk_missing | no_api_key | api_error:<code> | error:<Type>
    fallback_reason: str | None = None

    def meta(self) -> dict[str, Any]:
        return {"source": self.source, "model": self.model, "fallback_reason": self.fallback_reason}


class NarrativeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_prompt(data: ReportData, stats: dict[str, Any], turns: list[Turn] | None = None) -> str:
    """User content for Gemini: period, a factual dispenser log of refused requests, and the transcript
    (newest messages kept within :data:`MAX_PROMPT_MESSAGES` / :data:`MAX_PROMPT_CHARS`)."""
    start, end = data.local(data.period_start), data.local(data.period_end)
    head = [
        (f"Report period: {fmt_date(start)}, {fmt_time(start)} to {fmt_date(end)}, {fmt_time(end)} "
         f"({data.timezone})."),
    ]
    dr = stats.get("drops", {})
    refused = [d for d in data.drops if d.status == "DENIED" and d.source in ("manual", "agent", "button")]
    if refused:
        head.append(f"Refused pill requests in the dispenser log: {len(refused)}.")
        for d in refused[-20:]:
            head.append(f"- {fmt_datetime(data.local(d.requested_at))}: via {source_label(d.source)}, "
                        f"refused ({reason_label(d.reason)}).")
    else:
        head.append("Refused pill requests in the dispenser log: none.")
    if dr.get("on_request_drops"):
        head.append(f"Pills dropped on request in the period: {dr['on_request_drops']}.")

    lines: list[str] = []
    for m in data.messages:
        stamp = fmt_datetime(data.local(m.created_at))
        if m.role == "user":
            mode = " (voice)" if m.input_mode == "voice" else ""
            lines.append(f"[{stamp}] Patient{mode}: {_clip(_one_line(m.content), MAX_PROMPT_MESSAGE_CHARS)}")
        elif m.role == "assistant":
            lines.append(f"[{stamp}] Assistant: {_clip(_one_line(m.content), MAX_PROMPT_MESSAGE_CHARS)}")
        elif m.tool_name == "request_pill":
            lines.append(f"[{stamp}] Dispenser: {describe_pill_request(m)}")
        elif m.tool_name == "confirm_pill_taken":
            status = (m.tool_result or {}).get("status")
            lines.append(f"[{stamp}] Dispenser: patient confirmed taking a pill ({status or 'recorded'}).")
    kept: list[str] = []
    size = 0
    for line in reversed(lines[-MAX_PROMPT_MESSAGES:]):
        if size + len(line) + 1 > MAX_PROMPT_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    omitted = len(lines) - len(kept)
    body = ["Transcript (oldest first):"]
    if omitted:
        body.append(f"({omitted} older lines omitted.)")
    body += kept or ["(no messages)"]
    return "\n".join(head + [""] + body)


def clean_ai_text(text: str, *, max_words: int = MAX_AI_WORDS) -> str:
    """Plain-text, bullet-normalised, word-capped version of the model output."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    out_lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[*•·–—]|\d+[.)])\s+", "- ", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = re.sub(r"\s+", " ", line)
        out_lines.append(line)
    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    cleaned = "\n".join(out_lines)
    words = list(re.finditer(r"\S+", cleaned))
    if len(words) > max_words:
        cleaned = cleaned[: words[max_words - 1].end()].rstrip(" ,;:-") + "…"
    return cleaned


def _unsafe(text: str) -> bool:
    return bool(_UNSAFE_RE.search(_QUOTED_RE.sub(" ", text)))


class ReportNarrator:
    """Builds the narrative; thread-safe. ``client`` may be any object exposing
    ``client.models.generate_content(model=, contents=, config=)`` (tests pass fakes); without
    one a ``google.genai.Client`` is created lazily on first use (never at construction)."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self._injected = client is not None
        self._lock = threading.Lock()
        self._model = settings.gemini_model
        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        self._secrets = tuple(s for s in (key,) if s)

    @property
    def model(self) -> str:
        return self._model

    def build(self, data: ReportData, stats: dict[str, Any]) -> Narrative:
        turns = conversation_turns(data)
        rules_text = rules_narrative(data, stats, turns)
        if not self._settings.report_ai_summary:
            return Narrative(rules_text, "rules", fallback_reason="disabled")
        if not (self._settings.gemini_configured or self._injected):
            return Narrative(rules_text, "rules", fallback_reason="not_configured")
        if not any(t.user is not None for t in turns):
            return Narrative(rules_text, "rules", fallback_reason="no_conversations")
        try:
            text, model = self._generate(build_prompt(data, stats, turns))
        except Exception as exc:  # noqa: BLE001 - any AI failure falls back to the rules summary
            if isinstance(exc, NarrativeError):   # local check (blocked, empty, unsafe_output, no_api_key...)
                code = exc.code
                log.warning("report narrative: Gemini not used (%s)", code)
            else:   # netsafe code + plain message, never raw exception text (it can carry URLs)
                code = _failure_code(exc)
                log.warning("report narrative: Gemini not used (%s; %s)", code,
                            self._redact(str(netsafe.classify_exception(exc)))[:200])
            return Narrative(rules_text, "rules", fallback_reason=code)
        log.info("report narrative written by %s (%d words)", model, len(text.split()))
        return Narrative(text, "gemini", model=model)

    # ------------------------------------------------------------------ internals
    def _generate(self, prompt: str) -> tuple[str, str]:
        client = self._get_client()
        model = self._model
        try:
            response = self._call(client, model, prompt)
        except Exception as exc:
            fallback = (self._settings.gemini_fallback_model or "").strip()
            if not (_is_not_found(exc) and fallback and fallback != model):
                raise
            log.warning("Gemini model %r not found; retrying the report summary with %r", model, fallback)
            model = fallback
            response = self._call(client, model, prompt)
            self._model = model
        return _interpret(response), model

    def _call(self, client: Any, model: str, prompt: str) -> Any:
        config = {
            "system_instruction": SYSTEM_INSTRUCTION,
            "automatic_function_calling": {"disable": True},
        }
        return client.models.generate_content(model=model, contents=prompt, config=config)

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                if not self._secrets:
                    raise NarrativeError("no_api_key")
                try:
                    from google import genai
                    from google.genai import types
                except ImportError as exc:
                    raise NarrativeError("sdk_missing") from exc
                timeout_ms = max(1000, round(self._settings.gemini_timeout_s * 1000))
                self._client = genai.Client(api_key=self._secrets[0], vertexai=False,   # never follow redirects
                                            http_options=netsafe.gemini_http_options(types, timeout_ms))
            return self._client

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def _interpret(response: Any) -> str:
    feedback = getattr(response, "prompt_feedback", None)
    block = _enum_name(getattr(feedback, "block_reason", None)) if feedback is not None else None
    if block and block != "BLOCKED_REASON_UNSPECIFIED":
        raise NarrativeError("blocked")
    candidates = getattr(response, "candidates", None) or []
    finish = _enum_name(getattr(candidates[0], "finish_reason", None)) if candidates else None
    if finish in _BLOCKED_FINISH:
        raise NarrativeError("blocked" if finish != "MAX_TOKENS" else "truncated")
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - some SDK versions raise when there is no text part
        text = None
    if not isinstance(text, str) or not text.strip():
        raise NarrativeError("empty")
    cleaned = clean_ai_text(text)
    if not cleaned:
        raise NarrativeError("empty")
    if _unsafe(cleaned):
        raise NarrativeError("unsafe_output")
    return cleaned


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    text = name if isinstance(name, str) else str(value)
    return text.strip().upper().split(".")[-1] or None


def _is_not_found(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    return code == 404 or str(getattr(exc, "status", "") or "").upper() == "NOT_FOUND"


def _failure_code(exc: BaseException) -> str:
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in chain and len(chain) < 5:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    for e in chain:
        code = getattr(e, "code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return f"api_error:{code}"
        name = type(e).__name__.lower()
        if isinstance(e, TimeoutError) or "timeout" in name:
            return "timeout"
        if isinstance(e, (ConnectionError, OSError)) or "connect" in name or "network" in name \
                or "transport" in name:
            return "network"
    return f"error:{type(exc).__name__}"


# --------------------------------------------------------------------------- text helpers


def _one_line(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"
