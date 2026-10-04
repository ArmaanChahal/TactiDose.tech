"""Agent tools: JSON-schema declarations and an executor bound to ONE patient and ONE turn.

The model (or the offline rules agent) never supplies a patient id: every call runs for the
patient the executor was created for, and unknown arguments are ignored. Tools only *read*
status/history and *request* drops; the deterministic ``DropService`` decides and actuates
(ARCHITECTURE v2 §2, §5, §7).

Tools offered to the model
--------------------------
* ``get_patient_status()`` - containers and pill counts, global cooldown, last drop, today's
  doses, next scheduled dose (times already in spoken local form).
* ``get_recent_drops(days<=14)`` - recent drop requests and their outcomes.
* ``request_pill(container_number? | medication_name?, reason)`` - one drop request with
  source ``agent`` and this conversation's id. Returns ``DropOutcome.to_dict()``.
* ``confirm_pill_taken(medication_name?)`` - marks the most recent DISPENSED dose (within
  ``confirm_window_minutes``) TAKEN, through a conditional UPDATE owned by this module.

Deterministic guards (applied before ``DropService`` sees a request)
--------------------------------------------------------------------
* at most :attr:`PatientTools.max_drop_requests` (1) drop request per patient message;
* the patient's status is always read first in the same turn (inserted automatically, and
  recorded, when the model skipped it);
* no request when the patient's own words block it (:class:`TurnGuard`: emergency, prompt
  injection, "don't drop it", several pills at once, speech with unrecognised words);
* the container / medication must resolve to exactly one of the patient's containers.

A locally refused request returns ``{"status": "NOT_REQUESTED", "reason", "message"}``. It
never reaches the hardware and is not a ``pill_drops`` row; the tool message in the
conversation is its audit record. Every call is recorded in :attr:`PatientTools.calls` so
``AgentService`` can store it as a ``tool`` message. :meth:`PatientTools.execute` never raises.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select, update

from tactidose.config import Settings
from tactidose.core import phrases
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import (
    ContainerInfo,
    DropOutcome,
    DropServiceAPI,
    PatientStatus,
)
from tactidose.db.devlog import log_event
from tactidose.db.models import DoseEvent, DoseStatus, LogCategory, Medication
from tactidose.db.session import Database

log = logging.getLogger(__name__)

GET_PATIENT_STATUS = "get_patient_status"
GET_RECENT_DROPS = "get_recent_drops"
REQUEST_PILL = "request_pill"
CONFIRM_PILL_TAKEN = "confirm_pill_taken"
#: Internal only (deterministic "stop" handling); never offered to a model.
STOP_DEVICE = "stop_device"
MODEL_TOOLS: tuple[str, ...] = (GET_PATIENT_STATUS, GET_RECENT_DROPS, REQUEST_PILL, CONFIRM_PILL_TAKEN)

#: ``status`` of a request refused by this module (it never reached DropService).
NOT_REQUESTED = "NOT_REQUESTED"
MAX_DROP_DAYS = 14
DEFAULT_DROP_DAYS = 7
#: Minimum score for a medication-name match (see :func:`name_score`).
MIN_MATCH_SCORE = 0.6

_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
#: Words in medication names that do not identify the medication.
_NAME_NOISE = frozenset({
    "demo", "candy", "candies", "token", "tokens", "piece", "pieces", "pill", "pills", "tablet",
    "tablets", "capsule", "capsules", "caplet", "caplets", "softgel", "softgels", "mg", "mcg", "ug",
    "g", "ml", "iu", "the", "my", "of", "a", "an", "and", "with", "medication", "medicine", "dose",
    "only", "not", "real",
})
_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# =========================================================================== declarations


def tool_declarations(num_slots: int) -> list[dict[str, Any]]:
    """Provider-neutral function declarations: ``{name, description, parameters}`` where
    ``parameters`` is a JSON schema (``None`` for a tool without arguments)."""
    return [
        {
            "name": GET_PATIENT_STATUS,
            "description": (
                "Read the patient's current situation: each container's medication and pill count, "
                "the global cooldown (whether another pill may be requested now, and when), the last "
                "drop, today's scheduled doses and the next scheduled dose. Call this before answering "
                "anything about pills, doses, times or containers, and before request_pill."
            ),
            "parameters": None,
        },
        {
            "name": GET_RECENT_DROPS,
            "description": "List the patient's recent pill drop requests and their outcomes, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer", "minimum": 1, "maximum": MAX_DROP_DAYS,
                        "description": f"How many days back to look (1 to {MAX_DROP_DAYS}, default 7).",
                    },
                },
            },
        },
        {
            "name": REQUEST_PILL,
            "description": (
                "Ask the dispenser to drop ONE pill for the patient. Use it only when the patient asks "
                "for a pill now, or clearly confirms they want one. Identify the pill by "
                "container_number or medication_name; if the patient did not say which and more than "
                "one could be meant, ask them instead. The dispenser applies the rules (cooldown, empty "
                "container, device ready) and returns the outcome: status DROPPED is the only success, "
                "and message explains any refusal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container_number": {
                        "type": "integer", "minimum": 1, "maximum": int(num_slots),
                        "description": f"Container number as the patient says it (1 to {int(num_slots)}).",
                    },
                    "medication_name": {
                        "type": "string",
                        "description": "Medication name as the patient said it, e.g. 'vitamin c'.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "The patient's request in a few words, e.g. 'patient asked for vitamin c'.",
                    },
                },
                "required": ["reason"],
            },
        },
        {
            "name": CONFIRM_PILL_TAKEN,
            "description": (
                "Record that the patient says they have taken their most recently dropped scheduled "
                "pill. Use only when the patient says they took it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "medication_name": {
                        "type": "string",
                        "description": "Optional: which medication they took, as they said it.",
                    },
                },
            },
        },
    ]


# =========================================================================== records


@dataclass(frozen=True)
class TurnGuard:
    """Deterministic limits for one patient message, derived from the patient's own words.

    ``block_drop`` / ``block_confirm`` are ``(code, message)`` when that action must not happen
    in this turn, whatever the model asks for (see ``rules_agent.turn_guard``)."""

    block_drop: tuple[str, str] | None = None
    block_confirm: tuple[str, str] | None = None
    #: Normalised patient text when it contains "not": a pill named right after it is never
    #: requested ("can i have my pill not calcium"), whichever provider resolved the target.
    negated_text: str = ""


@dataclass
class ToolCall:
    """One executed tool call (stored as a ``role="tool"`` conversation message)."""

    name: str
    args: dict[str, Any]
    result: dict[str, Any]
    at: datetime
    #: True when the executor inserted the call itself (status check before a pill request).
    auto: bool = False

    @property
    def summary(self) -> str:
        """Short human-readable line for the message ``content``."""
        r = self.result
        if self.name == REQUEST_PILL:
            status = r.get("status") or "?"
            reason = r.get("reason")
            return f"request_pill: {status}" + (f" ({reason})" if reason else "")
        if self.name == CONFIRM_PILL_TAKEN:
            return f"confirm_pill_taken: {r.get('status') or '?'}"
        if self.name == STOP_DEVICE:
            return f"stop_device: {'STOP sent' if r.get('stopped') else 'nothing moving'}"
        if r.get("error"):
            return f"{self.name}: error ({r.get('error')})"
        if self.name == GET_RECENT_DROPS:
            return f"get_recent_drops: {r.get('count', 0)} drops in {r.get('days')} days"
        return f"{self.name}: ok" + (" (automatic)" if self.auto else "")


# =========================================================================== helpers


def parse_dt(value: Any) -> datetime | None:
    """Aware datetime from a datetime or an ISO-8601 string (``None`` when absent/naive/bad)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def as_int(value: Any) -> int | None:
    """``2``, ``2.0``, ``"2"``, ``"two"`` -> 2; anything else (incl. bools) -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit():
            return int(text)
        if text in _DIGIT_WORDS:
            return int(_DIGIT_WORDS[text])
    return None


def _tokens(text: str | None) -> list[str]:
    return [_DIGIT_WORDS.get(w, w) for w in _NON_ALNUM.sub(" ", str(text or "").lower()).split()]


def name_tokens(name: str | None) -> list[str]:
    """Identifying words of a medication name: ``"Omega-3 (demo candy)"`` -> ``["omega", "3"]``."""
    core = [w for w in _tokens(_BRACKETS.sub(" ", str(name or ""))) if w not in _NAME_NOISE]
    return core or [w for w in _tokens(name) if w not in _NAME_NOISE]


def _fuzzy_in(token: str, words: Sequence[str]) -> bool:
    if token in words:
        return True
    if len(token) < 4:
        return False  # short tokens ("c", "3", "d") must match exactly
    return any(len(w) >= 4 and SequenceMatcher(None, token, w).ratio() >= 0.8 for w in words)


def _contains_run(words: Sequence[str], run: Sequence[str]) -> bool:
    n = len(run)
    return any(list(words[i:i + n]) == list(run) for i in range(len(words) - n + 1))


def name_score(query: str | Sequence[str], name: str | None) -> float:
    """How well ``query`` (free text) names medication ``name``: 1.0 exact phrase, 0.95 all
    words, 0.85 all words with typos, else 0.

    Every word of the name must be present: a first-word-only match ("vitamin d" vs
    "Vitamin C", "insulin lispro" vs "Insulin glargine") would release the wrong medication."""
    words = _tokens(query) if isinstance(query, str) else list(query)
    core = name_tokens(name)
    if not core or not words:
        return 0.0
    if _contains_run(words, core):
        return 1.0
    if all(t in words for t in core):
        return 0.95
    if all(_fuzzy_in(t, words) for t in core):
        return 0.85
    return 0.0


def match_containers(query: str, containers: Sequence[ContainerInfo]) -> list[ContainerInfo]:
    """Containers whose medication best matches ``query`` (empty = no match, >1 = ambiguous)."""
    words = _tokens(query)
    scored = [(name_score(words, c.medication_name), c) for c in containers
              if c.medication_id is not None and c.medication_name]
    scored = [(s, c) for s, c in scored if s >= MIN_MATCH_SCORE]
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [c for s, c in scored if s == best]


def medication_options(containers: Sequence[ContainerInfo]) -> list[tuple[int, str]]:
    """``[(container_number, medication_name)]`` for containers holding a medication."""
    return [(c.container_number, c.medication_name) for c in containers
            if c.medication_id is not None and c.medication_name]


def due_doses(status: PatientStatus, *, now: datetime, settings: Settings) -> list[dict[str, Any]]:
    """Today's doses that may be taken now (window open, not dropped, no review pending),
    oldest first. Uses the dose window ``[scheduled - early, scheduled + late]``."""
    early = timedelta(minutes=settings.dose_early_minutes)
    late = timedelta(minutes=settings.dose_late_minutes)
    due: list[tuple[datetime, dict[str, Any]]] = []
    for dose in status.today:
        if not isinstance(dose, Mapping) or dose.get("needs_review"):
            continue
        if str(dose.get("status") or "") not in ("SCHEDULED", "DUE", "HARDWARE_ERROR"):
            continue
        at = parse_dt(dose.get("scheduled_at")) or parse_dt(dose.get("scheduled_local"))
        if at is not None and at - early <= now <= at + late:
            due.append((at, dict(dose)))
    due.sort(key=lambda item: item[0])
    return [d for _, d in due]


def _plain_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(args or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
        else:
            out[str(key)] = str(value)[:200]
    return out


def _clip(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


# =========================================================================== executor


class PatientTools:
    """Executes tool calls for exactly one patient and one conversation turn. Not thread-safe
    (one instance per turn); never raises from :meth:`execute`."""

    #: Drop requests allowed per patient message (later ones are refused locally).
    max_drop_requests: int = 1
    #: Newest drops returned by ``get_recent_drops``.
    recent_drops_limit: int = 20

    def __init__(
        self,
        *,
        db: Database,
        drops: DropServiceAPI,
        clock: Clock,
        settings: Settings,
        patient_id: int,
        conversation_id: int | None,
        guard: TurnGuard | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._db = db
        self._drops = drops
        self._clock = clock
        self.settings = settings
        self.patient_id = int(patient_id)
        self.conversation_id = conversation_id
        self.guard = guard or TurnGuard()
        self._bus = bus
        self.calls: list[ToolCall] = []
        #: ``DropOutcome.to_dict()`` of every request that reached DropService (AgentReply.actions).
        self.drop_outcomes: list[dict[str, Any]] = []
        #: The most recent ``PatientStatus`` read in this turn (None until read / on failure).
        self.last_status: PatientStatus | None = None
        #: Raw PillDropView rows from the last ``get_recent_drops`` call in this turn.
        self.last_recent_drops: list[Mapping[str, Any]] = []
        self._status_read = False
        self._drop_requests = 0

    # ------------------------------------------------------------------ dispatch
    def execute(self, name: str, args: Mapping[str, Any] | None = None, *,
                internal: bool = False) -> dict[str, Any]:
        """Run tool ``name`` with ``args`` (unknown keys ignored) and record the call.
        ``internal`` additionally allows :data:`STOP_DEVICE` (never offered to models)."""
        clean = _plain_args(args)
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            GET_PATIENT_STATUS: lambda a: self._get_patient_status(),
            GET_RECENT_DROPS: lambda a: self._get_recent_drops(a.get("days")),
            REQUEST_PILL: lambda a: self._request_pill(a.get("container_number"), a.get("medication_name")),
            CONFIRM_PILL_TAKEN: lambda a: self._confirm_pill_taken(a.get("medication_name")),
        }
        if internal:
            handlers[STOP_DEVICE] = lambda a: self._stop_device()
        handler = handlers.get(str(name))
        if handler is None:
            result: dict[str, Any] = {"error": "unknown_tool", "message": f"There is no tool named {name}."}
        else:
            try:
                result = handler(clean)
            except Exception:  # noqa: BLE001 - a tool failure must not break the turn
                log.exception("agent tool %s failed for patient %s", name, self.patient_id)
                result = {"error": "tool_failed", "message": phrases.AGENT_ERROR}
        self._record(str(name), clean, result)
        return result

    def _record(self, name: str, args: dict[str, Any], result: dict[str, Any], *, auto: bool = False) -> None:
        self.calls.append(ToolCall(name=name, args=args, result=result, at=self._clock.now(), auto=auto))

    # ------------------------------------------------------------------ convenience (rules agent)
    def status(self) -> PatientStatus | None:
        """Read the status through the recorded tool (once per turn) and return the object."""
        if not self._status_read:
            self.execute(GET_PATIENT_STATUS)
        return self.last_status

    @property
    def drop_requested(self) -> bool:
        return self._drop_requests > 0

    @property
    def last_outcome(self) -> dict[str, Any] | None:
        return self.drop_outcomes[-1] if self.drop_outcomes else None

    # ------------------------------------------------------------------ tools
    def _get_patient_status(self) -> dict[str, Any]:
        self._status_read = True
        try:
            status = self._drops.patient_status(self.patient_id)
        except Exception:  # noqa: BLE001 - fail closed: no status, no drop decisions
            log.exception("patient_status failed for patient %s", self.patient_id)
            self.last_status = None
            return {"error": "status_unavailable", "message": phrases.DB_UNAVAILABLE}
        self.last_status = status
        return self._status_view(status)

    def _get_recent_drops(self, days: Any) -> dict[str, Any]:
        n = as_int(days)
        n = DEFAULT_DROP_DAYS if n is None else max(1, min(MAX_DROP_DAYS, n))
        try:
            rows = self._drops.recent_drops(self.patient_id, days=n, limit=self.recent_drops_limit)
        except Exception:  # noqa: BLE001
            log.exception("recent_drops failed for patient %s", self.patient_id)
            self.last_recent_drops = []
            return {"error": "history_unavailable", "message": phrases.DB_UNAVAILABLE}
        self.last_recent_drops = [r for r in list(rows)[: self.recent_drops_limit] if isinstance(r, Mapping)]
        now_local = self._clock.local_now()
        views = [self._drop_view(r, now_local) for r in self.last_recent_drops]
        return {"days": n, "count": len(views), "drops": views}

    def _request_pill(self, container_number: Any, medication_name: Any) -> dict[str, Any]:
        # The model's "reason" argument is kept in the recorded tool_args only (audit).
        if self.guard.block_drop is not None:
            code, message = self.guard.block_drop
            return self._not_requested(code, message)
        if self._drop_requests >= self.max_drop_requests:
            return self._not_requested("ONE_PER_MESSAGE", phrases.ONE_PILL_ONLY)
        if not self._status_read:
            # Status first, always: recorded as its own (automatic) tool call.
            view = self._get_patient_status()
            self._record(GET_PATIENT_STATUS, {}, view, auto=True)
        status = self.last_status
        if status is None:
            return self._not_requested("STATUS_UNAVAILABLE", phrases.DB_UNAVAILABLE)
        target = self._resolve_target(status, container_number, medication_name)
        if isinstance(target, dict):
            return target
        slot, medication_id = target
        if self.guard.negated_text:
            from tactidose.agent.rules_agent import negated_containers  # rules_agent imports this module

            negated = negated_containers(self.guard.negated_text, status.containers)
            if any((slot is not None and c.slot == slot)
                   or (medication_id is not None and c.medication_id == medication_id) for c in negated):
                return self._not_requested("NEGATED", phrases.NEGATED)
        self._drop_requests += 1
        try:
            outcome = self._drops.request_drop(
                patient_id=self.patient_id,
                source="agent",
                slot=slot,
                medication_id=medication_id,
                requested_by_user_id=self.patient_id,
                conversation_id=self.conversation_id,
            )
        except Exception:  # noqa: BLE001 - outcome unknown: say nothing about the pill
            log.exception("request_drop raised for patient %s", self.patient_id)
            return {"status": "ERROR", "reason": "TOOL_ERROR", "message": phrases.AGENT_ERROR}
        view = outcome.to_dict()
        self.drop_outcomes.append(view)
        return {**view, **self._outcome_hints(outcome)}

    def _confirm_pill_taken(self, medication_name: Any) -> dict[str, Any]:
        if self.guard.block_confirm is not None:
            code, message = self.guard.block_confirm
            return {"status": "NOT_CONFIRMED", "reason": code, "message": message}
        now = self._clock.now()
        since = now - timedelta(minutes=self.settings.confirm_window_minutes)
        query_name = _clip(medication_name) if isinstance(medication_name, str) else ""
        confirmed: dict[str, Any] | None = None
        try:
            with self._db.session() as s:
                rows = s.execute(
                    select(DoseEvent, Medication.name)
                    .join(Medication, Medication.medication_id == DoseEvent.medication_id)
                    .where(
                        DoseEvent.user_id == self.patient_id,
                        DoseEvent.status.in_([DoseStatus.DISPENSED.value, DoseStatus.TAKEN.value]),
                        DoseEvent.dispensed_at.is_not(None),
                        DoseEvent.dispensed_at >= since,
                        DoseEvent.dispensed_at <= now,
                    )
                    .order_by(DoseEvent.dispensed_at.desc(), DoseEvent.event_id.desc())
                ).all()
                if query_name:
                    rows = [r for r in rows if name_score(query_name, r[1]) >= MIN_MATCH_SCORE]
                pending = [r for r in rows if r[0].status == DoseStatus.DISPENSED.value]
                if not pending:
                    if rows:
                        return {"status": "ALREADY_CONFIRMED", "message": phrases.ALREADY_TAKEN}
                    return {"status": "NOTHING_TO_CONFIRM", "message": phrases.NOTHING_TO_CONFIRM}
                event, med_name = pending[0]
                changed = s.execute(
                    update(DoseEvent)
                    .where(DoseEvent.event_id == event.event_id,
                           DoseEvent.status == DoseStatus.DISPENSED.value)
                    .values(status=DoseStatus.TAKEN.value, confirmed_taken_at=now,
                            confirm_source="agent", updated_at=now)
                    .execution_options(synchronize_session=False)
                ).rowcount
                if changed != 1:
                    return {"status": "ALREADY_CONFIRMED", "message": phrases.ALREADY_TAKEN}
                log_event(s, event.device_id, LogCategory.DOSE, "DOSE_TAKEN",
                          {"source": "agent", "conversation_id": self.conversation_id},
                          event_id=event.event_id, at=now)
                confirmed = {"status": "CONFIRMED", "event_id": event.event_id,
                             "medication_name": med_name, "message": phrases.taken_noted(med_name)}
        except Exception:  # noqa: BLE001
            log.exception("confirm_pill_taken failed for patient %s", self.patient_id)
            return {"status": "ERROR", "reason": "DB_ERROR", "message": phrases.RECORD_FAILED}
        if self._bus is not None and confirmed is not None:
            self._bus.publish(Topic.DOSE_UPDATED, {"event_id": confirmed["event_id"], "status": "TAKEN",
                                                   "patient_id": self.patient_id})
            self._bus.publish(Topic.PATIENT_STATUS, {"patient_id": self.patient_id, "reason": "dose_taken"})
        return confirmed or {"status": "NOTHING_TO_CONFIRM", "message": phrases.NOTHING_TO_CONFIRM}

    def _stop_device(self) -> dict[str, Any]:
        try:
            return {"stopped": bool(self._drops.interrupt())}
        except Exception:  # noqa: BLE001
            log.exception("drops.interrupt() failed")
            return {"stopped": False, "error": "stop_failed"}

    # ------------------------------------------------------------------ target resolution
    def _resolve_target(self, status: PatientStatus, container_number: Any,
                        medication_name: Any) -> tuple[int | None, int | None] | dict[str, Any]:
        """``(slot, None)`` / ``(None, medication_id)`` or a NOT_REQUESTED result."""
        containers = list(status.containers)
        options = medication_options(containers)
        num_slots = int(self.settings.num_slots)
        name = medication_name.strip() if isinstance(medication_name, str) else ""
        if container_number is not None and container_number != "":
            number = as_int(container_number)
            if number is None:
                return self._not_requested("UNKNOWN_CONTAINER", phrases.which_pill(options))
            if not 1 <= number <= num_slots:
                return self._not_requested("NO_SUCH_CONTAINER", phrases.no_such_container(number, num_slots))
            held = next((c for c in containers if c.container_number == number), None)
            if name and held is not None and held.medication_name and \
                    name_score(name, held.medication_name) < MIN_MATCH_SCORE:
                return self._not_requested("MISMATCH", phrases.join(
                    phrases.container_holds(number, held.medication_name), phrases.WHICH_PILL))
            return number - 1, None
        if name:
            matches = match_containers(name, containers)
            if not matches:
                return self._not_requested("UNKNOWN_MEDICATION", phrases.join(
                    phrases.UNKNOWN_MEDICATION, phrases.medication_list(options)))
            if len(matches) > 1:
                return self._not_requested("AMBIGUOUS", phrases.which_pill(medication_options(matches)))
            return matches[0].slot, None
        if not options:
            return self._not_requested("NO_MEDICATION", phrases.NO_CONTAINERS)
        if len(options) == 1:
            only = next(c for c in containers if c.medication_id is not None and c.medication_name)
            return only.slot, None
        due = due_doses(status, now=self._clock.now(), settings=self.settings)
        if due:
            med_id = as_int(due[0].get("medication_id"))
            if med_id is not None:
                return None, med_id
            slot = as_int(due[0].get("slot"))
            if slot is not None:
                return slot, None
        return self._not_requested("NEED_TARGET", phrases.which_pill(options))

    @staticmethod
    def _not_requested(code: str, message: str) -> dict[str, Any]:
        return {"status": NOT_REQUESTED, "reason": code, "message": message, "source": "agent",
                "drop_id": None}

    # ------------------------------------------------------------------ views (model-facing)
    def _local(self, value: Any) -> datetime | None:
        dt = parse_dt(value)
        return self._clock.to_local(dt) if dt is not None else None

    def _outcome_hints(self, outcome: DropOutcome) -> dict[str, Any]:
        hints: dict[str, Any] = {}
        if outcome.next_allowed_at is not None and outcome.next_allowed_at.tzinfo is not None:
            hints["next_allowed_spoken"] = phrases.when_phrase(
                self._clock.to_local(outcome.next_allowed_at), self._clock.local_now())
        if outcome.cooldown_remaining_s:
            hints["cooldown_remaining_spoken"] = phrases.duration_phrase(outcome.cooldown_remaining_s)
        return hints

    def _status_view(self, st: PatientStatus) -> dict[str, Any]:
        now_local = st.now_local if isinstance(st.now_local, datetime) and st.now_local.tzinfo else \
            self._clock.local_now()
        remaining = max(0, int(st.cooldown_remaining_s or 0))
        next_allowed = self._local(st.next_manual_allowed_at)
        device = st.device if isinstance(st.device, Mapping) else {}
        due = due_doses(st, now=self._clock.now(), settings=self.settings)
        return {
            "patient_name": st.display_name,
            "now": f"{now_local.strftime('%A')} {phrases.spoken_time(now_local)}",
            "containers": [
                {"container_number": c.container_number, "medication_name": c.medication_name,
                 "strength": c.strength, "pill_count": c.pill_count, "low_stock": c.low_stock,
                 "empty": c.empty}
                for c in st.containers
            ],
            "cooldown_minutes": st.cooldown_minutes,
            "cooldown_active": remaining > 0,
            "cooldown_remaining_minutes": math.ceil(remaining / 60) if remaining else 0,
            "next_pill_allowed_at": (phrases.when_phrase(next_allowed, now_local)
                                     if remaining and next_allowed is not None else None),
            "last_drop": self._drop_view(st.last_drop, now_local) if st.last_drop else None,
            "today": [self._dose_view(d, now_local) for d in st.today if isinstance(d, Mapping)],
            "due_now": [self._dose_view(d, now_local) for d in due],
            "next_scheduled": (self._dose_view(st.next_scheduled, now_local)
                               if isinstance(st.next_scheduled, Mapping) else None),
            "auto_drop_enabled": bool(st.auto_drop_enabled),
            "device_connected": bool(device.get("connected")),
            "device_state": device.get("state"),
            "alerts": [str(a.get("message")) for a in st.alerts if isinstance(a, Mapping) and a.get("message")],
            "rule": "The dispenser makes the final decision on every drop.",
        }

    def _drop_view(self, d: Mapping[str, Any], now_local: datetime) -> dict[str, Any]:
        at = self._local(d.get("completed_at")) or self._local(d.get("requested_at")) or \
            self._local(d.get("requested_local"))
        return {
            "when": phrases.when_phrase(at, now_local, say_today=True) if at else None,
            "medication_name": d.get("medication_name"),
            "container_number": d.get("container_number"),
            "source": d.get("source"),
            "status": d.get("status"),
            "reason": d.get("reason"),
        }

    def _dose_view(self, d: Mapping[str, Any], now_local: datetime) -> dict[str, Any]:
        at = self._local(d.get("scheduled_local")) or self._local(d.get("scheduled_at"))
        dropped = self._local(d.get("dispensed_at"))
        return {
            "time": phrases.when_phrase(at, now_local) if at else None,
            "medication_name": d.get("medication_name"),
            "container_number": d.get("container_number"),
            "status": d.get("status"),
            "dropped_at": phrases.when_phrase(dropped, now_local) if dropped else None,
        }
