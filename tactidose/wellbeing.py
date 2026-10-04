"""Optional well-being check-in, wired into TactiDose (the ``tactidose-wellbeing`` package).

The check-in itself (questions, parsing, consent, retry safety) lives in the standalone package
in ``tactidose-wellbeing/``; this module is the *host* side its README §10 asks for. Nothing
here changes how pills are dropped: the check-in never gates, delays or interprets a
medication request.

* **After a drop** - every ``DROPPED`` pill (schedule, button, agent) *offers* a check-in to
  the patient: ``Topic.WELLBEING_PROMPT`` on the event stream (patient only; the portal and
  the kiosk speak it) and, when the agent dropped the pill, appended to the agent's reply.
  "yes" starts the check-in linked to that drop; "no" or anything else dismisses the offer
  (anything else then goes to the agent as usual). At most one offer per
  ``wellbeing_after_drop_gap_minutes``.
* **Storage** - :class:`DatabaseCheckinRepository` implements the package's
  ``CheckinRepository`` on the main database (``wellbeing_checkins`` / ``wellbeing_answers``,
  next to ``pill_drops``). Only finished check-ins the patient consented to save are written.
  They are visible to the patient and to linked doctor/family (the consent question says so:
  :data:`CONSENT_NOTICE`); only the patient can delete them.
* **Identity** - :class:`TactiDoseIdentity` resolves the TactiDose session and maps a
  *patient* to the wellbeing user id ``tactidose-patient-<id>``; caregivers get 403 on the
  package's REST API (they read check-ins through ``GET /api/patients/{pid}/wellbeing``).
* **REST** - :func:`mount_wellbeing` mounts the package's own FastAPI app at ``/api/wellbeing``.
* **Chat / voice** - :meth:`WellbeingBridge.handle_chat` runs *before* the agent in
  ``POST /api/agent/chat``. Check-in turns are answered here and are **never** passed to
  ``AgentService`` (whose conversation log goes into reports). Emergencies, "stop", pill
  requests and "what do I take now" always go to the agent, even mid check-in.
* **Speech** - check-in replies may read a note back, so they are not rendered by the server
  TTS (ElevenLabs is a cloud service) unless ``wellbeing_server_tts`` is set; the browser
  speaks them locally. The device-side voice loop is not routed here.

Logs carry the patient id, step and status only - never answers, notes or the patient's words.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from tactidose.auth.deps import session_token
from tactidose.config import Settings
from tactidose.core.bus import BusEvent, EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import Intent
from tactidose.db.models import PillDrop, WellbeingAnswer, WellbeingCheckin
from tactidose.db.session import Database

log = logging.getLogger(__name__)

__all__ = [
    "CONSENT_NOTICE",
    "MODEL_NAME",
    "OFFER",
    "DatabaseCheckinRepository",
    "TactiDoseIdentity",
    "WellbeingBridge",
    "build_wellbeing",
    "checkin_views",
    "mount_wellbeing",
    "patient_id_of",
    "wellbeing_available",
    "wellbeing_user_id",
]

MOUNT_PATH = "/api/wellbeing"
#: ``model`` of a chat reply that came from the check-in (not from the agent).
MODEL_NAME = "wellbeing"
_USER_PREFIX = "tactidose-patient-"
_MAX_ANSWER = 500
#: An unanswered after-drop offer lapses after this long.
OFFER_TTL = timedelta(minutes=30)
_SEEN_DROPS = 500

#: Read with the consent question: saved check-ins are shared with the care team.
CONSENT_NOTICE = ("If you save them, your doctor and family members linked to your account "
                  "will also be able to see them next to your pill history.")
OFFER = ("Would you like a quick well-being check-in about your mood, stress and sleep? "
         "It is optional. Please say yes or no.")
OFFER_DECLINED = "Okay, no check-in this time."
STILL_OPEN = "Your well-being check-in is still open. Say repeat to continue it, or cancel to end it."
CANCELLED_ON_STOP = "I also cancelled your well-being check-in, and nothing from it was saved."
NOT_AVAILABLE = "The well-being check-in is not available right now."
#: Question labels for the read model (kept here so reading works without the package).
QUESTION_LABELS = {"mood": "Mood", "stress": "Stress", "sleep": "Sleep", "support": "Wants support"}

#: "start a check-in", "well-being check", "check in with me", "mood check" ...
_START = re.compile(r"\b(?:well ?being|check ?in|mood check)\b")
#: "read my check-ins", "check-in history", "what did I say in my last check in"
_HISTORY = re.compile(r"\b(?:check ?ins|check ?in history|past check ?in|last check ?in|previous check ?in)\b")
_HISTORY_VERB = re.compile(r"\b(?:read|hear|tell|list|show|history|what did|review)\b")
#: The agent always answers these, even in the middle of a check-in.
_AGENT_INTENTS = frozenset({Intent.CHECK_DUE, Intent.DISPENSE, Intent.PRIMARY_ACTION})


def wellbeing_available() -> bool:
    """True when the ``tactidose-wellbeing`` package is installed."""
    try:
        import tactidose_wellbeing  # noqa: F401
    except ImportError:
        return False
    return True


def wellbeing_user_id(patient_id: int) -> str:
    return f"{_USER_PREFIX}{int(patient_id)}"


def patient_id_of(user_id: str) -> int | None:
    """The TactiDose patient id of a wellbeing user id (None for anything else)."""
    if not isinstance(user_id, str) or not user_id.startswith(_USER_PREFIX):
        return None
    rest = user_id[len(_USER_PREFIX):]
    return int(rest) if rest.isdigit() else None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("-", " ")).split())


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# =========================================================================== storage


class DatabaseCheckinRepository:
    """``tactidose_wellbeing.storage.base.CheckinRepository`` on the TactiDose database.

    Every call is scoped by the wellbeing user id (= one patient). ``link_session`` remembers
    which drop a check-in followed; the link is written when the record is saved.
    """

    def __init__(self, db: Database, *, bus: EventBus | None = None) -> None:
        self.db = db
        self.bus = bus
        self._lock = threading.Lock()
        self._drop_for_session: dict[str, int] = {}

    def link_session(self, session_id: str, drop_id: int | None) -> None:
        if drop_id is None:
            return
        with self._lock:
            self._drop_for_session[session_id] = int(drop_id)

    def _take_drop(self, record_id: str) -> int | None:
        # The package derives record ids from session ids (ses_<x> -> rec_<x>).
        session_id = "ses_" + record_id.removeprefix("rec_")
        with self._lock:
            return self._drop_for_session.pop(session_id, None)

    # ------------------------------------------------------------------ writes
    def save_record(self, record: Any) -> bool:
        pid = patient_id_of(record.user_id)
        if pid is None:
            raise ValueError("not a TactiDose patient")
        drop_id = self._take_drop(record.record_id)
        try:
            with self.db.session() as s:
                if s.scalar(select(WellbeingCheckin.checkin_id).where(
                        WellbeingCheckin.record_id == record.record_id)) is not None:
                    return False
                if drop_id is not None:
                    drop = s.get(PillDrop, drop_id)
                    drop_id = drop_id if drop is not None and drop.patient_id == pid else None
                row = WellbeingCheckin(
                    record_id=record.record_id, patient_id=pid, drop_id=drop_id,
                    schema_version=record.schema_version, started_at=record.started_at,
                    completed_at=record.completed_at, support_requested=bool(record.support_requested),
                    share_answers=bool(record.sharing.share_answers), share_notes=bool(record.sharing.share_notes),
                    answers=[
                        WellbeingAnswer(
                            question_id=a.question_id.value, position=i, answer_value=a.answer_value,
                            status=a.status.value, recorded_at=a.recorded_at, note_text=a.note_text,
                            note_recorded_at=a.note_recorded_at)
                        for i, a in enumerate(record.answers)
                    ],
                )
                s.add(row)
                s.flush()
        except IntegrityError:  # a concurrent save of the same record won
            return False
        self._changed(pid)
        return True

    def delete_all(self, user_id: str) -> int:
        pid = patient_id_of(user_id)
        if pid is None:
            return 0
        with self.db.session() as s:
            ids = list(s.scalars(select(WellbeingCheckin.checkin_id).where(WellbeingCheckin.patient_id == pid)))
            if ids:
                s.execute(delete(WellbeingAnswer).where(WellbeingAnswer.checkin_id.in_(ids)))
                s.execute(delete(WellbeingCheckin).where(WellbeingCheckin.checkin_id.in_(ids)))
        if ids:
            self._changed(pid)
        return len(ids)

    def delete_record(self, user_id: str, record_id: str) -> bool:
        pid = patient_id_of(user_id)
        if pid is None:
            return False
        with self.db.session() as s:
            row = self._owned(s, pid, record_id)
            if row is None:
                return False
            s.delete(row)
        self._changed(pid)
        return True

    def delete_note(self, user_id: str, record_id: str, question_id: Any) -> bool:
        pid = patient_id_of(user_id)
        if pid is None:
            return False
        qid = getattr(question_id, "value", question_id)
        with self.db.session() as s:
            row = self._owned(s, pid, record_id)
            answer = next((a for a in row.answers if a.question_id == qid), None) if row is not None else None
            if answer is None or answer.note_text is None:
                return False
            answer.note_text = None
            answer.note_recorded_at = None
        self._changed(pid)
        return True

    def set_sharing(self, user_id: str, record_id: str, sharing: Any) -> bool:
        pid = patient_id_of(user_id)
        if pid is None:
            return False
        with self.db.session() as s:
            row = self._owned(s, pid, record_id)
            if row is None:
                return False
            row.share_answers = bool(sharing.share_answers)
            row.share_notes = bool(sharing.share_notes)
        return True

    # ------------------------------------------------------------------ reads
    def list_records(self, user_id: str) -> list[Any]:
        pid = patient_id_of(user_id)
        if pid is None:
            return []
        with self.db.session() as s:
            rows = s.scalars(
                select(WellbeingCheckin).options(selectinload(WellbeingCheckin.answers))
                .where(WellbeingCheckin.patient_id == pid)
                .order_by(WellbeingCheckin.completed_at, WellbeingCheckin.record_id)
            ).all()
            return [_to_record(r, user_id) for r in rows]

    def get_record(self, user_id: str, record_id: str) -> Any | None:
        pid = patient_id_of(user_id)
        if pid is None:
            return None
        with self.db.session() as s:
            row = self._owned(s, pid, record_id)
            return _to_record(row, user_id) if row is not None else None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _owned(s: Any, pid: int, record_id: str) -> WellbeingCheckin | None:
        return s.scalars(
            select(WellbeingCheckin).options(selectinload(WellbeingCheckin.answers))
            .where(WellbeingCheckin.patient_id == pid, WellbeingCheckin.record_id == str(record_id))
        ).first()

    def _changed(self, pid: int) -> None:
        if self.bus is not None:
            self.bus.publish(Topic.PATIENT_STATUS, {"patient_id": pid, "reason": "wellbeing"})


def _to_record(row: WellbeingCheckin, user_id: str) -> Any:
    from tactidose_wellbeing.domain.questions import QuestionId
    from tactidose_wellbeing.domain.session import AnswerStatus, CheckinRecord, SavedAnswer, SharingPermission

    return CheckinRecord(
        record_id=row.record_id, user_id=user_id, schema_version=row.schema_version,
        started_at=row.started_at, completed_at=row.completed_at, support_requested=bool(row.support_requested),
        answers=[
            SavedAnswer(question_id=QuestionId(a.question_id), answer_value=a.answer_value,
                        status=AnswerStatus(a.status), recorded_at=a.recorded_at, note_text=a.note_text,
                        note_recorded_at=a.note_recorded_at)
            for a in sorted(row.answers, key=lambda a: a.position)
        ],
        sharing=SharingPermission(bool(row.share_answers), bool(row.share_notes)),
    )


def checkin_views(db: Database, clock: Clock, patient_id: int, *, days: int = 30) -> list[dict[str, Any]]:
    """Saved check-ins of the last ``days`` days, newest first, with the drop each one followed
    (``GET /api/patients/{pid}/wellbeing``). Works without the package installed."""
    since = clock.now() - timedelta(days=max(1, int(days)))
    with db.session() as s:
        rows = s.scalars(
            select(WellbeingCheckin).options(selectinload(WellbeingCheckin.answers))
            .where(WellbeingCheckin.patient_id == int(patient_id), WellbeingCheckin.completed_at >= since)
            .order_by(WellbeingCheckin.completed_at.desc(), WellbeingCheckin.checkin_id.desc())
        ).all()
        drop_ids = {r.drop_id for r in rows if r.drop_id is not None}
        drops = {d.drop_id: d for d in s.scalars(select(PillDrop).where(PillDrop.drop_id.in_(drop_ids)))} \
            if drop_ids else {}
        out = []
        for r in rows:
            d = drops.get(r.drop_id) if r.drop_id is not None else None
            out.append({
                "checkin_id": r.checkin_id,
                "record_id": r.record_id,
                "patient_id": r.patient_id,
                "started_at": _iso(r.started_at),
                "completed_at": _iso(r.completed_at),
                "completed_local": clock.to_local(r.completed_at).isoformat(),
                "support_requested": bool(r.support_requested),
                "after_drop": None if d is None else {
                    "drop_id": d.drop_id,
                    "medication_name": d.medication_name,
                    "container_number": None if d.slot_number is None else d.slot_number + 1,
                    "source": d.source,
                    "dropped_at": _iso(d.completed_at or d.requested_at),
                    "dropped_local": clock.to_local(d.completed_at or d.requested_at).isoformat(),
                },
                "answers": [{
                    "question_id": a.question_id,
                    "label": QUESTION_LABELS.get(a.question_id, a.question_id.title()),
                    "answer_value": a.answer_value,
                    "status": a.status,
                    "note_text": a.note_text,
                } for a in sorted(r.answers, key=lambda a: a.position)],
            })
        return out


# =========================================================================== identity


class TactiDoseIdentity:
    """``tactidose_wellbeing.api.auth.IdentityProvider`` backed by TactiDose sessions."""

    mode = "tactidose_session"
    configured = True

    def __init__(self, services: Any) -> None:
        self._services = services

    def authenticate(self, request: Any) -> str:
        from tactidose_wellbeing.service import ServiceError

        services = self._services
        token = session_token(request, services.settings)
        try:
            user = services.auth.resolve(token) if token else None
        except Exception as exc:  # noqa: BLE001 - DB down: fail closed
            log.warning("wellbeing: session lookup failed (%s)", type(exc).__name__)
            raise ServiceError("auth_unavailable", "Sign-in cannot be checked right now.", 503,
                               retryable=True) from None
        if user is None:
            raise ServiceError("unauthenticated", "Please sign in.", 401)
        if not getattr(user, "is_patient", False):
            raise ServiceError("patient_only", "Only the patient can use their well-being check-in.", 403)
        return wellbeing_user_id(user.user_id)


# =========================================================================== bridge


@dataclass
class _Offer:
    offer_id: str
    drop_id: int
    at: datetime
    announced: bool = False


class WellbeingBridge:
    """Owns the ``WellbeingService``, offers check-ins after drops and routes the patient's chat
    turns. Thread-safe."""

    def __init__(self, service: Any, repository: DatabaseCheckinRepository, settings: Settings, *,
                 clock: Clock, bus: EventBus | None = None) -> None:
        self.service = service
        self.repository = repository
        self.settings = settings
        self.clock = clock
        self.bus = bus
        self._lock = threading.Lock()
        #: patient id -> (session_id, step) of the patient's open check-in.
        self._active: dict[int, tuple[str, int]] = {}
        #: patient id -> the after-drop offer waiting for a yes/no.
        self._offers: dict[int, _Offer] = {}
        #: patient id -> when the last after-drop offer was made (demo clock).
        self._last_offer: dict[int, datetime] = {}
        self._seen_drops: OrderedDict[int, None] = OrderedDict()
        #: Patients whose after-drop offers are paused (the guided demo has its own check-in).
        self._suppressed: set[int] = set()
        self._unsubscribe = None
        if bus is not None and settings.wellbeing_after_drop:
            self._unsubscribe = bus.add_listener(self._on_drop, [Topic.DROP])

    @property
    def server_tts(self) -> bool:
        return bool(self.settings.wellbeing_server_tts)

    def status(self) -> dict[str, Any]:
        with self._lock:
            open_sessions, offers = len(self._active), len(self._offers)
        return {"available": True, "mount": MOUNT_PATH, "open_sessions": open_sessions,
                "pending_offers": offers, "after_drop": bool(self.settings.wellbeing_after_drop),
                "server_tts": self.server_tts}

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # ------------------------------------------------------------------ after a drop
    def _on_drop(self, ev: BusEvent) -> None:
        """Bus listener (publisher's thread, after the drop committed): offer a check-in."""
        data = ev.data or {}
        if data.get("status") != "DROPPED" or data.get("in_progress"):
            return
        try:
            drop_id, pid = int(data["drop_id"]), int(data["patient_id"])
        except (KeyError, TypeError, ValueError):
            return
        offer = self.offer_after_drop(pid, drop_id)
        if offer is not None and self.bus is not None:
            self.bus.publish(Topic.WELLBEING_PROMPT, {
                "user_id": pid, "patient_id": pid, "offer_id": offer.offer_id, "drop_id": drop_id,
                "kind": "offer", "text": OFFER,
            })

    def suppress_offers(self, patient_id: int, on: bool) -> None:
        """Pause (``on``) or resume after-drop offers for a patient (guided demo runs)."""
        with self._lock:
            (self._suppressed.add if on else self._suppressed.discard)(int(patient_id))

    def offer_after_drop(self, patient_id: int, drop_id: int) -> _Offer | None:
        """Record an offer for this drop, unless one was made recently, a check-in is open, or
        this drop was already handled. Returns the new offer or None."""
        pid = int(patient_id)
        now = self.clock.now()
        gap = timedelta(minutes=int(self.settings.wellbeing_after_drop_gap_minutes))
        with self._lock:
            if drop_id in self._seen_drops:
                return None
            self._seen_drops[drop_id] = None
            while len(self._seen_drops) > _SEEN_DROPS:
                self._seen_drops.popitem(last=False)
            last = self._last_offer.get(pid)
            if pid in self._suppressed or pid in self._active or (last is not None and now - last < gap):
                return None
            offer = _Offer(offer_id=f"offer-{uuid.uuid4().hex[:12]}", drop_id=int(drop_id), at=now)
            self._offers[pid] = offer
            self._last_offer[pid] = now
        log.info("wellbeing: patient=%s offered a check-in after drop %s", pid, drop_id)
        return offer

    def _pending_offer(self, pid: int) -> _Offer | None:
        with self._lock:
            offer = self._offers.get(pid)
            if offer is not None and self.clock.now() - offer.at > OFFER_TTL:
                self._offers.pop(pid, None)
                return None
            return offer

    def _take_offer(self, pid: int) -> _Offer | None:
        offer = self._pending_offer(pid)
        with self._lock:
            self._offers.pop(pid, None)
        return offer

    # ------------------------------------------------------------------ routing
    def handle_chat(self, patient_id: int, text: str, *, conversation_id: int | None = None) -> dict[str, Any] | None:
        """Answer ``text`` from the check-in, or return None to let the agent answer it.

        * Open check-in: every turn goes to it, except emergencies, "stop", pill requests and
          "what do I take now".
        * Pending after-drop offer: "yes" starts the check-in (linked to the drop), "no" ends
          the offer; anything else ends the offer and goes to the agent.
        * Otherwise only a start phrase or a history request is taken.
        """
        from tactidose.agent.rules_agent import analyse

        pid = int(patient_id)
        flags = analyse(text)
        if self._agent_first(flags):
            self._take_offer(pid)   # the patient moved on: the agent answers, the offer lapses
            return None
        session = self._open_session(pid)
        if session is not None:
            return self._answer(pid, session, text, conversation_id)
        offer = self._take_offer(pid)
        if offer is not None:
            from tactidose_wellbeing.domain.parsing import parse_yes_no

            decision = parse_yes_no(text)
            if decision == "yes":
                return self._start(pid, text, conversation_id, drop_id=offer.drop_id)
            if decision == "no":
                return self._reply(text, OFFER_DECLINED, conversation_id, extra={"offer_declined": True})
        norm = _norm(text)
        if _HISTORY.search(norm) and _HISTORY_VERB.search(norm):
            return self._history(pid, text, conversation_id)
        if _START.search(norm):
            return self._start(pid, text, conversation_id)
        return None

    def after_agent_turn(self, patient_id: int, text: str, out: dict[str, Any]) -> None:
        """The agent answered: add the after-drop offer if this turn dropped a pill, cancel an
        open check-in on "stop", or remind the patient that a check-in is still open."""
        from tactidose.agent.rules_agent import analyse

        pid = int(patient_id)
        reply = str(out.get("text") or "")
        session = self._open_session(pid)
        if session is not None:
            if analyse(text).stop:
                self._act(pid, session, "cancel")
                self._forget(pid)
                out["text"] = f"{reply} {CANCELLED_ON_STOP}".strip()
            else:
                out["text"] = f"{reply} {STILL_OPEN}".strip()
            return
        offer = self._pending_offer(pid)
        if offer is not None and not offer.announced:
            offer.announced = True
            out["text"] = f"{reply} {OFFER}".strip()
            out["wellbeing"] = {"kind": "offer", "offer_id": offer.offer_id, "drop_id": offer.drop_id}

    def has_open_checkin(self, patient_id: int) -> bool:
        return self._open_session(int(patient_id)) is not None

    def has_pending_offer(self, patient_id: int) -> bool:
        return self._pending_offer(int(patient_id)) is not None

    @staticmethod
    def _agent_first(flags: Any) -> bool:
        return bool(flags.emergency or flags.stop or flags.drop_request
                    or flags.parsed.intent in _AGENT_INTENTS)

    # ------------------------------------------------------------------ check-in calls
    def _start(self, pid: int, text: str, conversation_id: int | None, *, drop_id: int | None = None) -> dict[str, Any]:
        from tactidose_wellbeing import StartSessionRequest

        resp = self.service.start_session(wellbeing_user_id(pid), StartSessionRequest(request_id=_request_id()))
        self.repository.link_session(resp.session_id, drop_id)
        self._track(pid, resp)
        log.info("wellbeing: patient=%s started a check-in (after drop: %s)", pid, drop_id)
        return self._reply(text, resp.speech_text, conversation_id, checkin=resp,
                           extra={"drop_id": drop_id} if drop_id is not None else None)

    def _answer(self, pid: int, session: tuple[str, int], text: str,
                conversation_id: int | None) -> dict[str, Any]:
        resp = self._act(pid, session, "answer", text[:_MAX_ANSWER])
        if resp is None:
            return self._reply(text, NOT_AVAILABLE, conversation_id)
        self._track(pid, resp)
        log.info("wellbeing: patient=%s step=%s status=%s error=%s", pid, resp.step,
                 resp.session_status.value, resp.error.code if resp.error else None)
        return self._reply(text, resp.speech_text, conversation_id, checkin=resp)

    def _history(self, pid: int, text: str, conversation_id: int | None) -> dict[str, Any]:
        hist = self.service.get_history(wellbeing_user_id(pid))
        return self._reply(text, hist.speech_text, conversation_id,
                           extra={"history": {"records": len(hist.records)}})

    def _act(self, pid: int, session: tuple[str, int], action: str, answer: str | None = None) -> Any:
        from tactidose_wellbeing import ActionRequest, ServiceError

        session_id, step = session
        req = ActionRequest(request_id=_request_id(), action=action, answer=answer, expected_step=step)
        try:
            return self.service.handle_action(wellbeing_user_id(pid), session_id, req)
        except ServiceError as err:
            log.warning("wellbeing: patient=%s action=%s failed (%s)", pid, action, err.code)
            self._forget(pid)
            return None

    # ------------------------------------------------------------------ session tracking
    def _open_session(self, pid: int) -> tuple[str, int] | None:
        """The patient's open check-in (re-read from the service, so expiry is honoured)."""
        from tactidose_wellbeing import ServiceError

        with self._lock:
            tracked = self._active.get(pid)
        if tracked is None:
            return None
        try:
            resp = self.service.get_session(wellbeing_user_id(pid), tracked[0])
        except ServiceError:
            self._forget(pid)
            return None
        if resp.session_status.is_terminal:
            self._forget(pid)
            return None
        return resp.session_id, resp.step

    def _track(self, pid: int, resp: Any) -> None:
        with self._lock:
            if resp.session_status.is_terminal:
                self._active.pop(pid, None)
            else:
                self._active[pid] = (resp.session_id, resp.step)

    def _forget(self, pid: int) -> None:
        with self._lock:
            self._active.pop(pid, None)

    # ------------------------------------------------------------------ reply shape
    @staticmethod
    def _reply(text: str, speech: str, conversation_id: int | None, *, checkin: Any = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Same shape as an agent reply (docs/API.md). ``messages`` are echoes for the transcript
        only (string ids, nothing is stored); ``wellbeing`` carries the check-in state."""
        turn = uuid.uuid4().hex[:12]
        out: dict[str, Any] = {
            "conversation_id": conversation_id,
            "text": speech,
            "model": MODEL_NAME,
            "actions": [],
            "audio_url": None,
            "messages": [
                {"message_id": f"wb-{turn}-u", "role": "user", "content": text, "model": None},
                {"message_id": f"wb-{turn}-a", "role": "assistant", "content": speech, "model": MODEL_NAME},
            ],
            "wellbeing": {"kind": "checkin"},
        }
        if checkin is not None:
            out["wellbeing"].update(checkin.model_dump(
                mode="json",
                include={"session_id", "session_status", "step", "storage_mode", "next_question",
                         "support_requested", "handoff", "urgent_support", "record_id", "events", "error"},
            ))
        if extra:
            out["wellbeing"].update(extra)
        return out


def _request_id() -> str:
    return f"td-{uuid.uuid4().hex}"


# =========================================================================== wiring


def build_wellbeing(settings: Settings, *, db: Database, clock: Clock, bus: EventBus | None = None
                    ) -> WellbeingBridge | None:
    """The bridge, or None when disabled in settings or the package is not installed."""
    if not settings.wellbeing_enabled:
        return None
    if not wellbeing_available():
        log.info("tactidose-wellbeing is not installed; the well-being check-in is off "
                 "(pip install -e ./tactidose-wellbeing)")
        return None
    from tactidose_wellbeing import WellbeingService
    from tactidose_wellbeing.config import load_safety_config
    from tactidose_wellbeing.storage import InMemorySessionStore

    repository = DatabaseCheckinRepository(db, bus=bus)
    service = WellbeingService(
        repository,
        InMemorySessionStore(),
        safety=load_safety_config(settings.wellbeing_config_file),
        session_ttl=timedelta(seconds=settings.wellbeing_session_ttl_s),
        clock=clock.now,
        consent_notice=CONSENT_NOTICE,
    )
    return WellbeingBridge(service, repository, settings, clock=clock, bus=bus)


def mount_wellbeing(app: Any, services: Any) -> bool:
    """Mount the check-in REST API at ``/api/wellbeing`` (TactiDose sessions as identity)."""
    bridge = getattr(services, "wellbeing", None)
    if bridge is None:
        return False
    from tactidose_wellbeing.api import create_app

    app.mount(MOUNT_PATH, create_app(bridge.service, TactiDoseIdentity(services)))
    return True
