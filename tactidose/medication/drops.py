"""DropService — deterministic pill-drop rules, inventory and scheduled auto-drops (v2).

Implements ``core.interfaces.DropServiceAPI`` (ARCHITECTURE v2 §5, §6, §12). It is the only
code in the v2 app that sends a pill-releasing command (``DROP_SLOT``) to the hardware; the
agent, the portals and the device button only ever *request* a drop through
:meth:`DropService.request_drop`, and these rules have the final say.

Checks, in order — the first failing one stores a DENIED ``pill_drops`` row with that reason and
nothing is sent to the hardware:

1. device          the patient owns the configured device             DEVICE_UNAVAILABLE
   (permission)    known source; manual/agent/button only for the
                   patient themself; schedule never on behalf of a
                   user; demo only in demo mode                       NOT_ALLOWED
2. target          slot 0..N-1, or medication -> its container;       UNKNOWN_MEDICATION
                   the container holds an active, confirmed med        NO_MEDICATION
3. pending review  an UNCERTAIN drop awaits doctor/family review      NEEDS_REVIEW
                   (another drop is still in flight on the device     IN_PROGRESS)
4. cooldown        manual/agent/button: now < latest DROPPED or
                   UNCERTAIN drop of ANY pill + manual_cooldown       COOLDOWN
5. satisfaction    schedule: dose already DISPENSED/TAKEN, or the
                   same medication dropped since its window opened    ALREADY_SATISFIED
                   (the dose is then linked to that drop, DISPENSED)
6. inventory       pill_count <= 0 (+ EMPTY notification)             EMPTY
7. concurrency     the drop lock is held (never waited for)           IN_PROGRESS
8. hardware        not connected / FAULT / busy; SAFE_STOP or
                   unhomed -> HOME first; gate open -> CLOSE_GATE     DEVICE_UNAVAILABLE

Checks 1-6 always run inside the *claim*: the transaction that inserts the row *in flight*
(status UNCERTAIN, ``completed_at`` NULL: a crash mid-drop leaves an uncertain record, never a
silent one) and moves a scheduled dose to DISPENSING. When the device first needs HOME /
CLOSE_GATE they also run before that preparation, so a refused request never moves anything.
``hardware.drop_slot(slot)`` is then sent and the row is finalised from
``protocol.drop_certainty(result)``:

==============  ===================================================================================
DROPPED         DROPPED; pill_count - 1; the scheduled dose (or, for manual/agent drops, today's
                matching open dose) DISPENSED; PILL_DROPPED (+ LOW_STOCK / EMPTY on crossing)
NOT_DROPPED     FAILED, reason = code; count unchanged (``ERR NO_PILL``: count -> 0 + EMPTY);
                scheduled dose HARDWARE_ERROR, retried at now + auto_drop_retry_minutes; DROP_FAILED
UNCERTAIN       UNCERTAIN + needs_review; count unchanged until reviewed; scheduled dose
                HARDWARE_ERROR + needs_review, never retried; DROP_UNCERTAIN
==============  ===================================================================================

Fail closed: a database error before the claim -> DENIED/DB_ERROR and nothing is sent; an
outcome that cannot be written is kept in memory and every further drop is refused (DB_ERROR)
until it is; :meth:`DropService.recover_on_startup` turns rows left in flight into UNCERTAIN +
needs_review. Every state change commits in one transaction with its notifications, audit rows
and analytics outbox rows; bus events follow the commit (``Topic.DROP`` with a PillDropView,
``Topic.DOSE_UPDATED``, ``Topic.PATIENT_STATUS``, ``Topic.NOTIFICATION``).

Threading: one drop at a time (``threading.Lock``, non-blocking); caregiver operations use
compare-and-set updates instead of the lock; :meth:`DropService.interrupt` never takes it. The
service starts no threads. Hackathon prototype (candy/tokens only) — not a medical device.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from enum import Enum
from typing import Any, Callable, Iterable

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import ContainerInfo, DeviceSnapshot, DropOutcome, HardwareController, PatientStatus
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    COOLDOWN_SOURCES,
    Compartment,
    DenyReason,
    Device,
    DoseEvent,
    DoseStatus,
    DropSource,
    DropStatus,
    Frequency,
    LogCategory,
    Medication,
    Notification,
    NotificationKind,
    PillDrop,
    Schedule,
    User,
)
from tactidose.db.outbox import enqueue_device_event
from tactidose.db.session import Database
from tactidose.hardware.protocol import (
    BUSY_STATES,
    DROP_COMMANDS,
    LONG_RUNNING_COMMANDS,
    Command,
    CommandName,
    CommandResult,
    DeviceState,
    DropCertainty,
    Err,
    GateState,
    HostCode,
    drop_certainty,
)
from tactidose.medication import safety
from tactidose.medication.compartments import (
    assigned_slots,
    container_info,
    device_compartments,
    iso,
    patient_device,
)
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tactidose.medication.notifications import PendingNotifications, clock_label, duration_label, plural
from tactidose.medication.scheduler import (
    Scheduler,
    cas_transition,
    dose_update_payload,
    is_id,
    missed_dose_notice,
    publish_all,
    publish_patient_status,
    record_dose_change,
)

log = logging.getLogger(__name__)

__all__ = ["RESTART", "UNRECORDED", "DropService", "dose_to_view", "drop_to_view"]

#: ``DropOutcome.reason`` of a DROPPED drop whose record could not be written yet.
UNRECORDED = "UNRECORDED"
#: ``pill_drops.reason`` of a drop found in flight at startup.
RESTART = "RESTART"
INTERNAL_ERROR = "INTERNAL_ERROR"
MAX_RECENT_DAYS = 366
MAX_RECENT_LIMIT = 1000
MAX_COOLDOWN_MINUTES = 1440
_MAX_NOTE = 255

_SOURCES = frozenset(s.value for s in DropSource)
_COOLDOWN_SOURCES = frozenset(s.value for s in COOLDOWN_SOURCES)
_SCHEDULE = DropSource.SCHEDULE.value
_DEMO = DropSource.DEMO.value

_DROPPED = DropStatus.DROPPED.value
_DENIED = DropStatus.DENIED.value
_FAILED = DropStatus.FAILED.value
_UNCERTAIN = DropStatus.UNCERTAIN.value
_STATUSES = frozenset(s.value for s in DropStatus)
#: Drops that start the cooldown and satisfy a scheduled dose.
_COUNTED = (_DROPPED, _UNCERTAIN)

_SCHEDULED = DoseStatus.SCHEDULED.value
_DUE = DoseStatus.DUE.value
_DISPENSING = DoseStatus.DISPENSING.value
_DISPENSED = DoseStatus.DISPENSED.value
_TAKEN = DoseStatus.TAKEN.value
_MISSED = DoseStatus.MISSED.value
_CANCELLED = DoseStatus.CANCELLED.value
_HARDWARE_ERROR = DoseStatus.HARDWARE_ERROR.value

_R = DenyReason
#: Scheduled-dose denials after which the next automatic attempt waits auto_drop_retry_minutes.
_THROTTLED = frozenset(r.value for r in (_R.EMPTY, _R.NO_MEDICATION, _R.UNKNOWN_MEDICATION,
                                         _R.DEVICE_UNAVAILABLE, _R.NEEDS_REVIEW))
#: Scheduled-dose denials that alert the patient and caregivers (once per dose).
_ALERTED = frozenset(r.value for r in (_R.NO_MEDICATION, _R.UNKNOWN_MEDICATION, _R.DEVICE_UNAVAILABLE))
_MOTION_COMMANDS = frozenset(c.value for c in LONG_RUNNING_COMMANDS)
_STOPPED = Err.STOPPED.value
_NO_PILL = Err.NO_PILL.value


# --------------------------------------------------------------------------- serializers


def drop_to_view(drop: PillDrop, clock: Clock) -> dict[str, Any]:
    """API.md ``PillDropView`` (+ ``patient_id``, ``device_id``, ``in_progress``)."""
    slot = drop.slot_number
    return {
        "drop_id": drop.drop_id,
        "patient_id": drop.patient_id,
        "device_id": drop.device_id,
        "requested_at": iso(drop.requested_at),
        "completed_at": iso(drop.completed_at),
        "requested_local": clock.to_local(drop.requested_at).isoformat() if drop.requested_at else None,
        "slot": slot,
        "container_number": None if slot is None else slot + 1,
        "medication_id": drop.medication_id,
        "medication_name": drop.medication_name,
        "source": drop.source,
        "status": drop.status,
        "reason": drop.reason,
        "hardware_result": drop.hardware_result,
        "pill_count_before": drop.pill_count_before,
        "pill_count_after": drop.pill_count_after,
        "dose_event_id": drop.dose_event_id,
        "conversation_id": drop.conversation_id,
        "requested_by_user_id": drop.requested_by_user_id,
        "needs_review": bool(drop.needs_review),
        "review_note": drop.review_note,
        "in_progress": drop.completed_at is None,
    }


def dose_to_view(event: DoseEvent, clock: Clock, slots: dict[int, tuple[int, int]] | None = None) -> dict[str, Any]:
    """API.md ``DoseView`` (+ ``patient_id``, ``strength``, ``cancelled_at``, ``next_attempt_at``,
    ``review_note``, ``label``). ``slots`` (``compartments.assigned_slots``) resolves the
    container an open dose would drop from now; without it the stored slot is shown."""
    slot = safety.display_slot(event, slots) if slots is not None else event.slot_number
    med = event.medication
    return {
        "event_id": event.event_id,
        "schedule_id": event.schedule_id,
        "patient_id": event.user_id,
        "medication_id": event.medication_id,
        "medication_name": med.name if med is not None else None,
        "strength": med.strength if med is not None else None,
        "slot": slot,
        "container_number": None if slot is None else slot + 1,
        "scheduled_at": iso(event.scheduled_at),
        "scheduled_local": clock.to_local(event.scheduled_at).isoformat(),
        "status": event.status,
        "drop_id": event.drop_id,
        "dispensed_at": iso(event.dispensed_at),
        "dispense_source": event.dispense_source,
        "confirmed_taken_at": iso(event.confirmed_taken_at),
        "missed_at": iso(event.missed_at),
        "cancelled_at": iso(event.cancelled_at),
        "needs_review": bool(event.needs_review),
        "attempts": int(event.attempts or 0),
        "hardware_result": event.hardware_result,
        "next_attempt_at": iso(event.next_attempt_at),
        "review_note": event.review_note,
        "label": event.label,
    }


# --------------------------------------------------------------------------- internal records


@dataclass(frozen=True)
class _Request:
    patient_id: Any
    source: str
    slot: Any = None
    medication_id: Any = None
    requested_by_user_id: Any = None
    conversation_id: Any = None
    dose_event_id: Any = None


@dataclass(frozen=True)
class _Target:
    """What a request resolved to (as far as the checks got)."""

    device_id: str
    patient_id: int
    slot: int | None = None
    compartment_id: int | None = None
    medication_id: int | None = None
    medication_name: str | None = None
    pill_count: int | None = None
    event_id: int | None = None           # schedule source: the dose being dropped
    event_status: str | None = None
    scheduled_at: datetime | None = None


@dataclass(frozen=True)
class _Check:
    """Result of checks 1-6. ``reason is None`` = allowed so far."""

    target: _Target | None
    reason: str | None = None
    message: str = ""
    remaining_s: int = 0
    next_allowed_at: datetime | None = None
    satisfied_by: int | None = None       # ALREADY_SATISFIED: the earlier drop

    @property
    def allowed(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class _Prep:
    ok: bool
    reason: str = ""
    result: CommandResult | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class _Ctx:
    """A claimed drop: the in-flight row exists."""

    drop_id: int
    req: _Request
    target: _Target


@dataclass(frozen=True)
class _Final:
    """A drop outcome to write (kept in memory while it cannot be written)."""

    ctx: _Ctx
    status: str
    reason: str | None
    hardware_text: str | None
    result: CommandResult | None
    at: datetime


class _Effects:
    """Post-commit work collected inside a transaction."""

    def __init__(self, notifications: Any | None) -> None:
        self.notes = PendingNotifications(notifications)
        self.drops: list[dict[str, Any]] = []
        self.doses: list[dict[str, Any]] = []
        self.patients: dict[int, str] = {}


def _int_or_none(value: Any) -> int | None:
    return value if is_id(value) else None


def _is_low(count: int | None, threshold: int) -> bool:
    return count is not None and 0 < count <= threshold


def _clean_note(value: object, field: str = "note") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.")
    text = value.strip()
    return text[:_MAX_NOTE] if text else None


def _container(slot: int | None) -> str:
    return "the container" if slot is None else f"container {slot + 1}"


# --------------------------------------------------------------------------- service


class DropService:
    """Deterministic drop logic (implements ``DropServiceAPI``). Thread-safe."""

    #: Pauses between the attempts to write a drop outcome (tests may shorten them).
    db_retry_delays_s: tuple[float, ...] = (0.05, 0.15)
    #: Poll interval while waiting for HOMING to finish.
    poll_interval_s: float = 0.05
    #: How long to retry while another host command (e.g. a heartbeat PING) holds the link.
    busy_wait_s: float = 3.0

    def __init__(
        self,
        db: Database,
        hardware: HardwareController,
        clock: Clock,
        settings: Settings,
        *,
        notifications: Any | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.hardware = hardware
        self.clock = clock
        self.settings = settings
        self.notifications = notifications
        self.bus = bus
        self._lock = threading.Lock()            # one drop at a time (never waited for)
        self._guard_lock = threading.RLock()     # protects _pending
        self._pending: _Final | None = None      # outcome not yet written: drops stay blocked
        self._interrupt = threading.Event()      # set by interrupt(): abort before/while dropping
        self._active: _Ctx | None = None         # claimed drop of the request holding the lock
        self._scheduler = Scheduler(db, clock, settings, bus=bus, notifications=notifications)

    # ================================================================== DropServiceAPI
    def request_drop(
        self,
        *,
        patient_id: int,
        source: str,
        slot: int | None = None,
        medication_id: int | None = None,
        requested_by_user_id: int | None = None,
        conversation_id: int | None = None,
        dose_event_id: int | None = None,
    ) -> DropOutcome:
        """Apply the rules and, if they all pass, drop one pill. Never raises."""
        src = source.value if isinstance(source, Enum) else source
        req = _Request(patient_id, src if isinstance(src, str) else repr(src), slot, medication_id,
                       requested_by_user_id, conversation_id, dose_event_id)
        acquired = self._lock.acquire(blocking=False)
        try:
            return self._request_safe(req, locked=acquired)
        finally:
            if acquired:
                self._lock.release()

    def patient_status(self, patient_id: int) -> PatientStatus:
        """Containers, cooldown, last drop, today's doses, next dose, alerts and the device.
        ``NotFoundError`` for an unknown patient; database errors propagate (no made-up state)."""
        now = self.clock.now()
        snap = self._snapshot()
        with self.db.session() as s:
            user = s.get(User, patient_id) if is_id(patient_id) else None
            if user is None:
                raise NotFoundError(f"Patient {patient_id} not found.")
            dev = patient_device(s, self.settings, patient_id)
            containers: tuple[ContainerInfo, ...] = ()
            remaining, next_at = 0, None
            if dev is not None:
                containers = tuple(container_info(c) for c in device_compartments(s, dev, self.settings))
                remaining, next_at, _last = self._cooldown(s, dev, now)
            last = self._last_drop_row(s, patient_id)
            slots = assigned_slots(s, self.settings)
            today_events = self._day_events(s, patient_id, self.clock.to_local(now).date())
            today = tuple(dose_to_view(ev, self.clock, slots) for ev in today_events)
            upcoming = self._next_event(s, patient_id, now)
            alerts = self._alerts(s, dev, containers, today_events, snap)
            configured = dev is not None and dev.device_id == self.settings.device_id
            status = PatientStatus(
                patient_id=user.user_id,
                display_name=user.display_name,
                now_local=self.clock.to_local(now),
                containers=containers,
                cooldown_minutes=int(dev.manual_cooldown_minutes or 0) if dev is not None else 0,
                cooldown_remaining_s=remaining,
                next_manual_allowed_at=self.clock.to_local(next_at) if next_at is not None else None,
                last_drop=drop_to_view(last, self.clock) if last is not None else None,
                today=today,
                next_scheduled=dose_to_view(upcoming, self.clock, slots) if upcoming is not None else None,
                auto_drop_enabled=bool(dev.auto_drop_enabled) if dev is not None else False,
                device=snap.to_dict() if (configured and snap is not None) else {},
                alerts=tuple(alerts),
            )
        return status

    def recent_drops(self, patient_id: int, *, days: int = 7, limit: int = 200,
                     status: str | None = None) -> list[dict[str, Any]]:
        """PillDropViews of the last ``days`` days (1..366), newest first (``status`` filter optional)."""
        if not is_id(days) or not 1 <= days <= MAX_RECENT_DAYS:
            raise ValidationError(f"days must be a whole number from 1 to {MAX_RECENT_DAYS}.")
        if not is_id(limit) or not 1 <= limit <= MAX_RECENT_LIMIT:
            raise ValidationError(f"limit must be a whole number from 1 to {MAX_RECENT_LIMIT}.")
        wanted = status.strip().upper() if isinstance(status, str) and status.strip() else None
        if wanted is not None and wanted not in _STATUSES:
            raise ValidationError(f"status must be one of {', '.join(sorted(_STATUSES))}.")
        since = self.clock.now() - timedelta(days=days)
        with self.db.session() as s:
            q = select(PillDrop).where(PillDrop.patient_id == patient_id, PillDrop.requested_at >= since)
            if wanted is not None:
                q = q.where(PillDrop.status == wanted)
            rows = s.scalars(q.order_by(PillDrop.requested_at.desc(), PillDrop.drop_id.desc()).limit(limit)).all()
            return [drop_to_view(r, self.clock) for r in rows]

    def run_scheduled_drops(self) -> int:
        """ARCHITECTURE §6: drop every due scheduled dose (oldest first, one at a time) whose time
        has come, that is not satisfied and whose retry time (if any) has passed, when the
        device's auto-drop is enabled. Returns the number of drop requests made; never raises."""
        if not self._lock.acquire(blocking=False):
            log.debug("run_scheduled_drops: a drop is in progress; trying again next tick")
            return 0
        made = 0
        try:
            self._interrupt.clear()
            if not self._flush_pending():
                return 0
            try:
                due = self._auto_drop_candidates()
            except Exception:  # noqa: BLE001 - DB down: retry next tick
                log.exception("run_scheduled_drops: cannot read the due doses")
                return 0
            for i, (event_id, patient_id) in enumerate(due):
                if self._interrupt.is_set():
                    # Someone pressed STOP between two drops: pause the rest of this round.
                    self._defer(due[i:])
                    break
                out = self._request_safe(_Request(patient_id, _SCHEDULE, dose_event_id=event_id),
                                         locked=True, fresh=False)
                made += 1
                if out.status == _FAILED and out.reason == _STOPPED:
                    # Stopped mid-drop: pause the remaining automatic drops of this round too.
                    self._defer(due[i + 1:])
                    break
            return made
        except Exception:  # noqa: BLE001 - scheduler thread must survive
            log.exception("run_scheduled_drops failed")
            return made
        finally:
            self._lock.release()

    def interrupt(self) -> bool:
        """Send STOP now if motion/homing/a drop is in flight (never takes the lock; safe while
        another thread is blocked inside ``DROP_SLOT``). Returns True if STOP was sent."""
        if self._lock.locked():
            self._interrupt.set()   # a drop between its checks and DROP_SLOT aborts before sending
        snap = self._snapshot()
        in_flight = (snap.in_flight or "").split(" ")[0].upper() if snap is not None else ""
        moving = snap is not None and (in_flight in _MOTION_COMMANDS or snap.state in BUSY_STATES)
        if not moving:
            return False
        self._interrupt.set()
        try:
            result: CommandResult | None = self.hardware.stop()
        except Exception:  # noqa: BLE001
            log.exception("STOP failed")
            result = None
        log.warning("STOP sent while %s: %s", in_flight or snap.state.value, result.summary if result else "exception")
        return result is not None

    # ================================================================== startup / extra queries
    @property
    def has_unrecorded_outcome(self) -> bool:
        with self._guard_lock:
            return self._pending is not None

    def recover_on_startup(self) -> int:
        """Rows left in flight -> UNCERTAIN + needs_review (+ DROP_UNCERTAIN); DISPENSING doses ->
        HARDWARE_ERROR + needs_review. Returns the number of records changed; never raises."""
        now = self.clock.now()
        fx = self._effects()
        changed_count = 0
        try:
            with self.db.session() as s:
                rows = s.scalars(select(PillDrop).where(
                    PillDrop.device_id == self.settings.device_id, PillDrop.completed_at.is_(None))).all()
                for row in rows:
                    res = s.execute(
                        update(PillDrop)
                        .where(PillDrop.drop_id == row.drop_id, PillDrop.completed_at.is_(None))
                        .values(status=_UNCERTAIN, needs_review=True, completed_at=now, reason=RESTART,
                                hardware_result=row.hardware_result or "UNCERTAIN RESTART")
                        .execution_options(synchronize_session=False)
                    )
                    if res.rowcount != 1:
                        continue
                    row = s.get(PillDrop, row.drop_id, populate_existing=True)
                    changed_count += 1
                    log_event(s, row.device_id, LogCategory.SAFETY, "DROP_RECOVERED",
                              {"drop_id": row.drop_id, "why": "restart while dropping"},
                              event_id=row.dose_event_id, at=now)
                    enqueue_device_event(s, device_id=row.device_id, event_type="uncertain_restart", code=RESTART,
                                         detail={"command": CommandName.DROP_SLOT.value})
                    self._notify_uncertain(s, row, fx)
                    fx.drops.append(drop_to_view(row, self.clock))
                    fx.patients[row.patient_id] = "recovery"
                events = s.scalars(
                    select(DoseEvent).options(selectinload(DoseEvent.medication)).where(
                        DoseEvent.device_id == self.settings.device_id, DoseEvent.status == _DISPENSING)
                ).all()
                for ev in events:
                    changed = cas_transition(s, ev.event_id, _DISPENSING, {
                        "status": _HARDWARE_ERROR, "needs_review": True,
                        "hardware_result": "UNCERTAIN RESTART", "next_attempt_at": None,
                    })
                    if changed is None:
                        continue
                    changed_count += 1
                    record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                                       action="DOSE_RECOVERED", detail={"why": "restart while dropping"})
                    fx.doses.append(dose_update_payload(changed, self.clock, previous=_DISPENSING, action="recovered"))
                    fx.patients[changed.user_id] = "recovery"
        except Exception:  # noqa: BLE001 - leaving in-flight rows blocks drops (fail closed)
            log.exception("startup recovery failed; in-flight drops stay locked")
            return 0
        self._deliver(fx)
        if changed_count:
            log.warning("startup recovery: %d record(s) had an uncertain outcome and need review", changed_count)
        return changed_count

    def get_drop(self, drop_id: int, *, patient_id: int | None = None) -> dict[str, Any]:
        with self.db.session() as s:
            return drop_to_view(self._get_drop(s, drop_id, patient_id), self.clock)

    def list_doses(self, patient_id: int, local_date: date | str | None = None) -> list[dict[str, Any]]:
        """DoseViews for one local day (default today), ordered by time."""
        day = self._parse_day(local_date)
        with self.db.session() as s:
            slots = assigned_slots(s, self.settings)
            return [dose_to_view(ev, self.clock, slots) for ev in self._day_events(s, patient_id, day)]

    def next_scheduled_dose(self, patient_id: int) -> dict[str, Any] | None:
        """The next open dose scheduled after now (DoseView) — used by "jump to next dose"."""
        with self.db.session() as s:
            ev = self._next_event(s, patient_id, self.clock.now())
            return dose_to_view(ev, self.clock, assigned_slots(s, self.settings)) if ev is not None else None

    # ================================================================== doctor/family operations
    def get_settings(self, patient_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            return self._settings_view(self._require_device(s, patient_id))

    def update_settings(
        self,
        patient_id: int,
        *,
        manual_cooldown_minutes: int | None = None,
        auto_drop_enabled: bool | None = None,
        by_user_id: int | None = None,
    ) -> dict[str, Any]:
        """``PATCH …/settings``: cooldown 0..1440 minutes (0 disables it) and/or auto-drop."""
        if manual_cooldown_minutes is not None and (
                not is_id(manual_cooldown_minutes) or not 0 <= manual_cooldown_minutes <= MAX_COOLDOWN_MINUTES):
            raise ValidationError(f"manual_cooldown_minutes must be a whole number from 0 to {MAX_COOLDOWN_MINUTES}.")
        if auto_drop_enabled is not None and not isinstance(auto_drop_enabled, bool):
            raise ValidationError("auto_drop_enabled must be true or false.")
        now = self.clock.now()
        changes: dict[str, dict[str, Any]] = {}
        with self.db.session() as s:
            dev = self._require_device(s, patient_id)
            if manual_cooldown_minutes is not None and manual_cooldown_minutes != dev.manual_cooldown_minutes:
                changes["manual_cooldown_minutes"] = {"from": dev.manual_cooldown_minutes,
                                                      "to": manual_cooldown_minutes}
                dev.manual_cooldown_minutes = manual_cooldown_minutes
            if auto_drop_enabled is not None and auto_drop_enabled != bool(dev.auto_drop_enabled):
                changes["auto_drop_enabled"] = {"from": bool(dev.auto_drop_enabled), "to": auto_drop_enabled}
                dev.auto_drop_enabled = auto_drop_enabled
            if changes:
                dev.updated_at = now
                s.flush()
                log_event(s, dev.device_id, LogCategory.ADMIN, "DEVICE_SETTINGS_UPDATED",
                          {"changes": changes, "by_user_id": by_user_id}, at=now)
            view = self._settings_view(dev)
            owner = dev.user_id
        if changes:
            log.info("device settings for patient %s changed: %s", owner, changes)
            publish_patient_status(self.bus, {owner: "settings"})
        return view

    def resolve_drop(
        self,
        drop_id: int,
        *,
        dropped: bool,
        note: str | None = None,
        by_user_id: int | None = None,
        patient_id: int | None = None,
    ) -> dict[str, Any]:
        """Resolve an UNCERTAIN drop: ``dropped=True`` -> DROPPED (count - 1, its dose DISPENSED);
        ``False`` -> FAILED (count unchanged, its dose due again or MISSED). Clears the review
        block. Returns the PillDropView."""
        if not isinstance(dropped, bool):
            raise ValidationError("dropped must be true or false.")
        note_text = _clean_note(note)
        now = self.clock.now()
        fx = self._effects()
        with self.db.session() as s:
            row = self._get_drop(s, drop_id, patient_id)
            if row.status != _UNCERTAIN or not (row.needs_review or row.completed_at is None):
                raise ConflictError(f"Drop {row.drop_id} is {row.status}; it does not need review.")
            if row.completed_at is None and (now - row.requested_at).total_seconds() <= self._in_flight_grace_s():
                raise ConflictError("That drop is still in progress; try again in a moment.")
            values: dict[str, Any] = {
                "status": _DROPPED if dropped else _FAILED,
                "needs_review": False,
                "completed_at": row.completed_at or now,
            }
            if note_text is not None:
                values["review_note"] = note_text
            res = s.execute(
                update(PillDrop)
                .where(PillDrop.drop_id == row.drop_id, PillDrop.status == _UNCERTAIN)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:
                raise ConflictError(f"Drop {row.drop_id} changed at the same time; reload and try again.")
            row = s.get(PillDrop, row.drop_id, populate_existing=True)
            comp = s.get(Compartment, row.compartment_id) if row.compartment_id is not None else None
            if comp is not None and comp.medication_id != row.medication_id:
                comp = None   # re-assigned (and re-counted) since: its count no longer includes that pill
            if dropped:
                before, after = self._decrement(s, comp, now)
                row.pill_count_after = after
                self._stock_notices(s, row, comp, before, after, fx)
                self._resolve_dose_dropped(s, row, fx)
            else:
                row.pill_count_after = int(comp.pill_count) if comp is not None else row.pill_count_before
                self._resolve_dose_not_dropped(s, row, now, fx)
            self._clear_retry_waits(s, row.device_id, now)
            log_event(s, row.device_id, LogCategory.ADMIN, "DROP_REVIEW_RESOLVED",
                      {"drop_id": row.drop_id, "dropped": dropped, "by_user_id": by_user_id, "note": note_text},
                      event_id=row.dose_event_id, at=now)
            view = drop_to_view(row, self.clock)
            fx.drops.append(view)
            fx.patients[row.patient_id] = "review"
        self._deliver(fx)
        log.info("drop %s resolved by user %s: dropped=%s", drop_id, by_user_id, dropped)
        return view

    def skip_dose(self, event_id: int, *, note: str | None = None, by_user_id: int | None = None,
                  patient_id: int | None = None) -> dict[str, Any]:
        """SCHEDULED / DUE / HARDWARE_ERROR -> CANCELLED (the automatic drop will not happen)."""
        note_text = _clean_note(note)
        now = self.clock.now()
        fx = self._effects()
        with self.db.session() as s:
            ev = self._get_event(s, event_id, patient_id)
            if ev.status not in safety.OPEN_STATUSES:
                raise ConflictError(f"The dose is {ev.status}; it cannot be skipped.")
            previous = ev.status
            values: dict[str, Any] = {"status": _CANCELLED, "cancelled_at": now, "next_attempt_at": None}
            if note_text is not None:
                values["review_note"] = note_text
            changed = cas_transition(s, ev.event_id, previous, values)
            if changed is None:
                raise ConflictError("The dose changed at the same time; reload and try again.")
            record_dose_change(s, changed, settings=self.settings, clock=self.clock, action="DOSE_SKIPPED",
                               detail={"previous": previous, "by_user_id": by_user_id}, category=LogCategory.ADMIN)
            fx.doses.append(dose_update_payload(changed, self.clock, previous=previous, action="skipped"))
            fx.patients[changed.user_id] = "dose"
            view = dose_to_view(changed, self.clock, assigned_slots(s, self.settings))
        self._deliver(fx)
        return view

    def confirm_taken(self, patient_id: int, *, medication_id: int | None = None,
                      source: str = "agent") -> dict[str, Any] | None:
        """Mark the most recent DISPENSED dose (optionally of one medication) TAKEN — an
        optional extra signal. Returns its DoseView, or None when nothing can be confirmed."""
        now = self.clock.now()
        fx = self._effects()
        with self.db.session() as s:
            ev = safety.find_confirmable(s, now, self.settings, patient_id=patient_id, medication_id=medication_id)
            if ev is None:
                return None
            changed = cas_transition(s, ev.event_id, _DISPENSED, {
                "status": _TAKEN, "confirmed_taken_at": now, "confirm_source": str(source)[:32],
            })
            if changed is None:
                return None
            record_dose_change(s, changed, settings=self.settings, clock=self.clock, action="DOSE_TAKEN",
                               detail={"source": str(source)[:32]})
            fx.doses.append(dose_update_payload(changed, self.clock, previous=_DISPENSED, action="taken"))
            fx.patients[changed.user_id] = "dose"
            view = dose_to_view(changed, self.clock, assigned_slots(s, self.settings))
        self._deliver(fx)
        return view

    # ================================================================== demo helpers
    def create_demo_dose_now(self, patient_id: int, medication_id: int | None = None, *,
                             by_user_id: int | None = None) -> dict[str, Any]:
        """Demo: schedule ``medication_id`` (default: the lowest container's active, confirmed
        medication) daily at the current local minute, so the next ``run_scheduled_drops`` drops
        it. Returns ``{schedule, event}``."""
        local_now = self.clock.local_now()
        hhmm = f"{local_now.hour:02d}:{local_now.minute:02d}"
        with self.db.session() as s:
            dev = patient_device(s, self.settings, patient_id)
            if dev is None or dev.device_id != self.settings.device_id:
                raise ValidationError("This patient has no dispenser connected.")
            if medication_id is not None:
                med = s.get(Medication, medication_id) if is_id(medication_id) else None
                if med is None or med.user_id != patient_id:
                    raise NotFoundError(f"Medication {medication_id} not found.")
                if not med.active or not med.confirmed_by_user:
                    raise ValidationError("Only an active, confirmed medication can be scheduled.")
            else:
                med = None
                for mid, _slot in sorted(assigned_slots(s, self.settings).items(), key=lambda kv: kv[1][0]):
                    candidate = s.get(Medication, mid)
                    if candidate is not None and candidate.user_id == patient_id and candidate.active \
                            and candidate.confirmed_by_user:
                        med = candidate
                        break
                if med is None:
                    raise ValidationError("No confirmed medication is in a container.")
            med_id = med.medication_id
            existing = s.scalars(select(Schedule).where(
                Schedule.medication_id == med_id, Schedule.time_of_day == hhmm,
                Schedule.frequency == Frequency.DAILY.value, Schedule.active.is_(True),
            ).order_by(Schedule.schedule_id).limit(1)).first()
            schedule_id = existing.schedule_id if existing is not None else None
        if schedule_id is None:
            schedule = self._scheduler.create_schedule(med_id, hhmm, Frequency.DAILY.value,
                                                       created_by_user_id=by_user_id, patient_id=patient_id)
            schedule_id = schedule["schedule_id"]
        else:
            self._scheduler.tick()
            schedule = self._scheduler.get_schedule(schedule_id)
        at = self.clock.local_to_utc(datetime.combine(local_now.date(), dtime(local_now.hour, local_now.minute)))
        with self.db.session() as s:
            ev = s.scalars(select(DoseEvent).options(selectinload(DoseEvent.medication)).where(
                DoseEvent.schedule_id == schedule_id, DoseEvent.scheduled_at == at)).first()
            if ev is None or not safety.is_open(ev):
                raise ConflictError("A dose of this medication was already dropped at this time today.")
            view = dose_to_view(ev, self.clock, assigned_slots(s, self.settings))
        log.info("demo dose %s created for medication %s at %s", view["label"], med_id, hhmm)
        return {"schedule": schedule, "event": view}

    # ================================================================== request pipeline
    def _request_safe(self, req: _Request, *, locked: bool, fresh: bool = True) -> DropOutcome:
        try:
            return self._request(req, locked=locked, fresh=fresh)
        except Exception:  # noqa: BLE001 - last line of defence: never raise to the caller
            log.exception("drop request failed unexpectedly")
            active = self._active if locked else None   # only the lock holder can have claimed a drop
            if active is not None:
                return self._outcome_internal_after_claim(active)
            return DropOutcome(status=_DENIED, source=req.source, reason=_R.DB_ERROR.value,
                               message="Something went wrong, so no pill was dropped. Please try again.")
        finally:
            if locked:
                self._active = None

    def _request(self, req: _Request, *, locked: bool, fresh: bool = True) -> DropOutcome:
        """``locked`` = the caller holds the drop lock; ``fresh`` = a new user request (a STOP
        aimed at an earlier drop no longer applies) rather than the next drop of a scheduled round."""
        if locked:
            if fresh:
                self._interrupt.clear()
            if not self._flush_pending():
                return self._db_error(req, unrecorded=True)
        if not (locked and self._ready_now()):
            # Checks 1-6 first, so that a refused request never makes the device HOME or close its gate.
            now = self.clock.now()
            fx = self._effects()
            out: DropOutcome | None = None
            try:
                with self.db.session() as s:
                    check = self._check(s, req, now)
                    if check.allowed and not locked:
                        check = replace(check, reason=_R.IN_PROGRESS.value,
                                        message="Another pill is dropping right now. Please wait a moment.")
                    if not check.allowed:
                        out = self._close(s, req, check, now, fx)
            except Exception:  # noqa: BLE001 - state unknown: fail closed, nothing is sent
                log.exception("drop request: cannot read the records; nothing sent")
                return self._db_error(req)
            self._deliver(fx)
            if out is not None:
                self._log_outcome(req, out)
                return out
            prep = self._prepare_hardware()
            if not prep.ok:
                out = self._prep_failed(req, check, prep)
                self._log_outcome(req, out)
                return out
        # The claim re-runs checks 1-6 in the transaction that inserts the in-flight row.
        claimed = self._claim(req)
        if isinstance(claimed, DropOutcome):
            self._log_outcome(req, claimed)
            return claimed
        self._active = claimed
        try:
            if self._interrupt.is_set():
                result = CommandResult(Command(CommandName.DROP_SLOT, claimed.target.slot), False, _STOPPED,
                                       definitive=True, detail="stopped before sending")
            else:
                result = self._send_drop(claimed.target.slot)
            out = self._finalise(claimed, result)
        except Exception:  # noqa: BLE001 - a bug after the claim: the outcome is unknown
            log.exception("drop %s: unexpected error after the claim; locking it for review", claimed.drop_id)
            out = self._finalise(claimed, None)
        self._log_outcome(req, out)
        return out

    # ------------------------------------------------------------------ checks 1-6
    def _check(self, s: Session, req: _Request, now: datetime) -> _Check:
        # 1. device
        if not is_id(req.patient_id):
            return _Check(None, _R.NOT_ALLOWED.value, "That request is not allowed.")
        patient = s.get(User, req.patient_id)
        dev = patient_device(s, self.settings, req.patient_id) if patient is not None else None
        if dev is None:
            return _Check(None, _R.DEVICE_UNAVAILABLE.value, "No dispenser is linked to this account.")
        t = _Target(device_id=dev.device_id, patient_id=dev.user_id)
        if dev.device_id != self.settings.device_id:
            return _Check(t, _R.DEVICE_UNAVAILABLE.value, "This dispenser is not connected right now.")
        refusal = self._permission(req)
        if refusal is not None:
            return _Check(t, _R.NOT_ALLOWED.value, refusal)
        # 2. target
        t, denial = self._resolve_target(s, req, dev, t)
        if denial is not None:
            return denial
        # 3. pending review / a drop still in flight
        blocker = self._review_blocker(s, dev.device_id, now, t)
        if blocker is not None:
            return blocker
        # 4. global cooldown (manual / agent / button)
        if req.source in _COOLDOWN_SOURCES:
            remaining, next_at, last_at = self._cooldown(s, dev, now)
            if remaining > 0 and next_at is not None and last_at is not None:
                # The drop behind the cooldown may still be in flight: its row can commit between
                # check 3 and this query. Say "dropping right now", never "was dropped at …".
                in_flight = self._review_blocker(s, dev.device_id, now, t)
                if in_flight is not None and in_flight.reason == _R.IN_PROGRESS.value:
                    return in_flight
                return _Check(t, _R.COOLDOWN.value, self._cooldown_message(last_at, next_at, remaining, now),
                              remaining_s=remaining, next_allowed_at=self.clock.to_local(next_at))
        # 5. scheduled dose already satisfied
        if req.source == _SCHEDULE:
            satisfied = self._satisfaction(s, t, now)
            if satisfied is not None:
                return satisfied
        # 6. inventory
        if (t.pill_count or 0) <= 0:
            return _Check(t, _R.EMPTY.value,
                          f"{_container(t.slot).capitalize()} is empty. Please ask your caregiver to refill it.")
        # 8 (static part). The connected hardware must have this container.
        hw_slots = getattr(self.hardware, "num_slots", self.settings.num_slots)
        if is_id(hw_slots) and t.slot is not None and t.slot >= hw_slots:
            return _Check(t, _R.DEVICE_UNAVAILABLE.value,
                          f"{_container(t.slot).capitalize()} is not available on this dispenser right now.")
        return _Check(t)

    def _permission(self, req: _Request) -> str | None:
        if req.source not in _SOURCES:
            return "That kind of drop request is not allowed."
        for value in (req.requested_by_user_id, req.conversation_id, req.dose_event_id):
            if value is not None and not is_id(value):
                return "That request is not allowed."
        if req.source in _COOLDOWN_SOURCES and req.requested_by_user_id is not None \
                and req.requested_by_user_id != req.patient_id:
            return "Only the patient can ask for their pill."
        if req.source == _SCHEDULE and req.requested_by_user_id is not None:
            return "Scheduled pills drop automatically."
        if req.source == _DEMO and not self.settings.demo_mode:
            return "Demo drops are turned off."
        return None

    def _resolve_target(self, s: Session, req: _Request, dev: Device, t: _Target) -> tuple[_Target, _Check | None]:
        n = self.settings.num_slots
        event: DoseEvent | None = None
        if req.source == _SCHEDULE:
            event = s.get(DoseEvent, req.dose_event_id) if is_id(req.dose_event_id) else None
            if event is None or event.user_id != t.patient_id or event.device_id != dev.device_id:
                return t, _Check(t, _R.UNKNOWN_MEDICATION.value, "That scheduled dose was not found.")
            t = replace(t, event_id=event.event_id, event_status=event.status, scheduled_at=event.scheduled_at)
            if req.medication_id is not None and req.medication_id != event.medication_id:
                return t, _Check(t, _R.UNKNOWN_MEDICATION.value, "That medication does not match the scheduled dose.")
        elif req.slot is None and req.medication_id is None:
            return t, _Check(t, _R.UNKNOWN_MEDICATION.value, "Please say which container or medication you want.")
        if req.slot is not None:
            if not is_id(req.slot) or not 0 <= req.slot < n:
                label = f"container {req.slot + 1}" if is_id(req.slot) else "such container"
                return t, _Check(t, _R.UNKNOWN_MEDICATION.value,
                                 f"There is no {label}. The containers are numbered 1 to {n}.")
            comp = s.scalars(select(Compartment).options(joinedload(Compartment.medication)).where(
                Compartment.device_id == dev.device_id, Compartment.slot_number == req.slot)).first()
            t = replace(t, slot=req.slot)
            if comp is None or not comp.active or comp.medication_id is None:
                return t, _Check(t, _R.NO_MEDICATION.value, f"Container {req.slot + 1} has no medication set up.")
            med = comp.medication
        else:
            mid = event.medication_id if event is not None else req.medication_id
            med = s.get(Medication, mid) if is_id(mid) else None
            if med is None or med.user_id != t.patient_id:
                return t, _Check(t, _R.UNKNOWN_MEDICATION.value, "I could not find that medication.")
            t = replace(t, medication_id=med.medication_id, medication_name=med.name)
            if not med.active or not med.confirmed_by_user:
                return t, _Check(t, _R.NO_MEDICATION.value, f"{med.name} is not set up for dropping.")
            slots = assigned_slots(s, self.settings)
            if med.medication_id not in slots:
                return t, _Check(t, _R.NO_MEDICATION.value, f"{med.name} is not in a container right now.")
            comp = s.get(Compartment, slots[med.medication_id][1])
        assert comp is not None
        t = replace(t, slot=comp.slot_number, compartment_id=comp.compartment_id,
                    medication_id=med.medication_id if med is not None else None,
                    medication_name=med.name if med is not None else None,
                    pill_count=int(comp.pill_count or 0))
        if med is None or med.user_id != t.patient_id or not med.active or not med.confirmed_by_user:
            return t, _Check(t, _R.NO_MEDICATION.value, f"Container {comp.slot_number + 1} has no medication set up.")
        if req.medication_id is not None and req.medication_id != med.medication_id:
            return t, _Check(t, _R.UNKNOWN_MEDICATION.value,
                             f"Container {comp.slot_number + 1} holds {med.name}, not that medication.")
        if event is not None and event.medication_id != med.medication_id:
            return t, _Check(t, _R.UNKNOWN_MEDICATION.value,
                             f"Container {comp.slot_number + 1} does not hold the scheduled medication.")
        return t, None

    def _review_blocker(self, s: Session, device_id: str, now: datetime, t: _Target) -> _Check | None:
        rows = s.scalars(
            select(PillDrop)
            .where(
                PillDrop.device_id == device_id,
                PillDrop.status == _UNCERTAIN,
                or_(PillDrop.needs_review.is_(True), PillDrop.completed_at.is_(None)),
            )
            .order_by(PillDrop.requested_at.desc(), PillDrop.drop_id.desc())
        ).all()
        if not rows:
            return None
        grace = self._in_flight_grace_s()
        if any(r.completed_at is None and (now - r.requested_at).total_seconds() <= grace for r in rows):
            return _Check(t, _R.IN_PROGRESS.value, "Another pill is dropping right now. Please wait a moment.")
        first = rows[0]
        return _Check(t, _R.NEEDS_REVIEW.value,
                      f"An earlier drop from {_container(first.slot_number)} could not be confirmed. "
                      "A caregiver must check it before another pill can drop.")

    def _cooldown(self, s: Session, dev: Device, now: datetime) -> tuple[int, datetime | None, datetime | None]:
        """``(remaining_s, next_allowed_at, last_drop_at)`` of the global cooldown."""
        last = self._last_drop_at(s, dev.device_id)
        minutes = int(dev.manual_cooldown_minutes or 0)
        if minutes <= 0 or last is None:
            return 0, None, last
        next_at = last + timedelta(minutes=minutes)
        if now >= next_at:
            return 0, None, last
        return int(math.ceil((next_at - now).total_seconds())), next_at, last

    def _satisfaction(self, s: Session, t: _Target, now: datetime) -> _Check | None:
        ev = s.get(DoseEvent, t.event_id)
        assert ev is not None
        when = clock_label(self.clock.to_local(ev.scheduled_at))
        dose = f"The {when} dose of {t.medication_name}"
        if ev.status in (_DISPENSED, _TAKEN):
            at = ev.dispensed_at or ev.confirmed_taken_at
            tail = f" at {clock_label(self.clock.to_local(at))}." if at is not None else "."
            return _Check(t, _R.ALREADY_SATISFIED.value, f"{dose} was already dropped{tail}", satisfied_by=ev.drop_id)
        if ev.status == _DISPENSING:
            return _Check(t, _R.IN_PROGRESS.value, f"{dose} is dropping right now.")
        if ev.status in (_MISSED, _CANCELLED):
            return _Check(t, _R.NOT_ALLOWED.value, f"{dose} was {'missed' if ev.status == _MISSED else 'skipped'}.")
        if ev.needs_review:
            return _Check(t, _R.NEEDS_REVIEW.value, f"{dose} must be checked by a caregiver first.")
        start, end = safety.dispense_window(ev, self.settings)
        if not start <= now <= end:
            return _Check(t, _R.NOT_ALLOWED.value, f"{dose} is not due now.")
        # Double-dose guard: a drop of the same medication counts for this dose if it happened
        # inside the dose window OR within min_dose_interval_minutes before the scheduled time
        # (a manual drop at 07:25 satisfies an 08:00 dose even though the window opens at 07:30).
        lookback = min(start, ev.scheduled_at - timedelta(minutes=int(self.settings.min_dose_interval_minutes or 0)))
        happened = func.coalesce(PillDrop.completed_at, PillDrop.requested_at)
        prior = s.scalars(
            select(PillDrop)
            .where(
                PillDrop.patient_id == t.patient_id,
                PillDrop.medication_id == ev.medication_id,
                PillDrop.status.in_(_COUNTED),
                happened >= lookback,
            )
            .order_by(happened.desc(), PillDrop.drop_id.desc())
            .limit(1)
        ).first()
        if prior is not None:
            at_local = self.clock.to_local(safety.drop_time(prior))
            return _Check(t, _R.ALREADY_SATISFIED.value, f"{dose} was already dropped at {clock_label(at_local)}.",
                          satisfied_by=prior.drop_id)
        return None

    # ------------------------------------------------------------------ terminal rows without a drop
    def _close(self, s: Session, req: _Request, check: _Check, now: datetime, fx: _Effects, *,
               status: str = _DENIED, hardware: CommandResult | None = None) -> DropOutcome:
        """Store a completed DENIED (or prepare-time FAILED) row and its side effects."""
        t = check.target
        if t is None:
            # No device row to attach the request to (unknown patient / no dispenser): audit only.
            log_event(s, self.settings.device_id, LogCategory.SAFETY, "DROP_DENIED",
                      {"patient_id": _int_or_none(req.patient_id), "source": req.source[:16], "reason": check.reason},
                      at=now)
            return self._outcome(status, req, check, None, hardware)
        hw_text = hardware.hardware_result[:255] if hardware is not None else None
        row = PillDrop(
            patient_id=t.patient_id, device_id=t.device_id, slot_number=t.slot, compartment_id=t.compartment_id,
            medication_id=t.medication_id, medication_name=t.medication_name, source=req.source[:16],
            requested_by_user_id=_int_or_none(req.requested_by_user_id),
            dose_event_id=t.event_id if t.event_id is not None else _int_or_none(req.dose_event_id),
            conversation_id=_int_or_none(req.conversation_id), status=status, reason=check.reason,
            hardware_result=hw_text, pill_count_before=t.pill_count, pill_count_after=t.pill_count,
            needs_review=False, requested_at=now, completed_at=now,
        )
        s.add(row)
        s.flush()
        if req.source == _SCHEDULE and t.event_id is not None:
            self._scheduled_close_effects(s, row, check, status, now, fx)
        if check.reason == _R.EMPTY.value and t.compartment_id is not None:
            self._empty_notice(s, row, s.get(Compartment, t.compartment_id), fx)
        if status == _FAILED:
            self._notify_failed(s, row, fx)
        log_event(s, t.device_id, LogCategory.SAFETY if status == _DENIED else LogCategory.HARDWARE,
                  "DROP_DENIED" if status == _DENIED else "DROP_FAILED",
                  {"drop_id": row.drop_id, "source": row.source, "reason": check.reason, "slot": t.slot,
                   "result": hw_text}, event_id=row.dose_event_id, at=now)
        fx.drops.append(drop_to_view(row, self.clock))
        fx.patients[t.patient_id] = "drop"
        return self._outcome(status, req, check, row.drop_id, hardware)

    def _scheduled_close_effects(self, s: Session, row: PillDrop, check: _Check, status: str, now: datetime,
                                 fx: _Effects) -> None:
        ev = s.get(DoseEvent, row.dose_event_id)
        if ev is None:
            return
        if check.reason == _R.ALREADY_SATISFIED.value:
            prior = s.get(PillDrop, check.satisfied_by) if check.satisfied_by is not None else None
            if prior is not None and safety.is_open(ev):
                self._link_dose(s, ev, prior, safety.drop_time(prior), fx, action="satisfied")
            return
        if not safety.is_open(ev):
            return
        retry_at = now + timedelta(minutes=self.settings.auto_drop_retry_minutes)
        if status == _FAILED:   # stopped while preparing: nothing dropped, try again later
            previous = ev.status
            changed = cas_transition(s, ev.event_id, previous, {
                "status": _HARDWARE_ERROR, "hardware_result": row.hardware_result or f"ERR {row.reason}",
                "next_attempt_at": retry_at,
            }, require_no_review=True)
            if changed is not None:
                record_dose_change(s, changed, settings=self.settings, clock=self.clock, action="DOSE_HARDWARE_ERROR",
                                   detail={"previous": previous, "drop_id": row.drop_id, "reason": row.reason})
                fx.doses.append(dose_update_payload(changed, self.clock, previous=previous, action="hardware_error"))
            return
        if check.reason in _THROTTLED:
            s.execute(update(DoseEvent).where(DoseEvent.event_id == ev.event_id)
                      .values(next_attempt_at=retry_at).execution_options(synchronize_session=False))
        if check.reason in _ALERTED and not self._notified(s, row.patient_id, NotificationKind.DEVICE_ALERT,
                                                           dose_event_id=ev.event_id):
            when = clock_label(self.clock.to_local(ev.scheduled_at))
            cause = ("The dispenser is not connected or needs attention."
                     if check.reason == _R.DEVICE_UNAVAILABLE.value else "Its medication is not set up in a container.")
            end = clock_label(self.clock.to_local(safety.dispense_window(ev, self.settings)[1]))
            fx.notes.add(
                s, patient_id=row.patient_id, kind=NotificationKind.DEVICE_ALERT.value,
                title="Dispenser needs attention",
                body=f"The {when} dose of {row.medication_name or 'medication'} could not drop. {cause} "
                     f"It will be tried again until {end}.",
                data={"dose_event_id": ev.event_id, "drop_id": row.drop_id, "reason": check.reason,
                      "slot": row.slot_number,
                      "container_number": None if row.slot_number is None else row.slot_number + 1},
                to_patient=True, to_caregivers=True,
            )

    def _prep_failed(self, req: _Request, check: _Check, prep: _Prep) -> DropOutcome:
        now = self.clock.now()
        if prep.cancelled:
            status = _FAILED
            closed = replace(check, reason=_STOPPED, message="Stopped. No pill was dropped.")
        else:
            status = _DENIED
            closed = replace(check, reason=_R.DEVICE_UNAVAILABLE.value, message=self._device_message(prep))
        fx = self._effects()
        try:
            with self.db.session() as s:
                out = self._close(s, req, closed, now, fx, status=status, hardware=prep.result)
        except Exception:  # noqa: BLE001 - nothing was dropped; the record is best effort
            log.exception("could not record a refused drop")
            return replace(self._db_error(req), message=closed.message)
        self._deliver(fx)
        return out

    # ------------------------------------------------------------------ claim, send, finalise
    def _claim(self, req: _Request) -> _Ctx | DropOutcome:
        """Re-run checks 1-6 and insert the in-flight row (+ DISPENSING for a scheduled dose)."""
        now = self.clock.now()
        fx = self._effects()
        ctx: _Ctx | None = None
        out: DropOutcome | None = None
        try:
            with self.db.session() as s:
                check = self._check(s, req, now)
                if not check.allowed:
                    out = self._close(s, req, check, now, fx)
                else:
                    t = check.target
                    assert t is not None
                    claimed = True
                    if req.source == _SCHEDULE:
                        changed = cas_transition(s, t.event_id, t.event_status, {
                            "status": _DISPENSING, "attempts": DoseEvent.attempts + 1, "slot_number": t.slot,
                            "compartment_id": t.compartment_id, "dispense_source": _SCHEDULE, "next_attempt_at": None,
                        }, require_no_review=True)
                        if changed is None:
                            claimed = False
                            out = self._close(s, req, replace(check, reason=_R.IN_PROGRESS.value,
                                                              message="That dose changed just now. Please try again."),
                                              now, fx)
                        else:
                            record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                                               action="DOSE_DISPENSING",
                                               detail={"previous": t.event_status, "slot": t.slot,
                                                       "attempt": changed.attempts})
                            fx.doses.append(dose_update_payload(changed, self.clock, previous=t.event_status,
                                                                action="dispensing"))
                    if claimed:
                        row = PillDrop(
                            patient_id=t.patient_id, device_id=t.device_id, slot_number=t.slot,
                            compartment_id=t.compartment_id, medication_id=t.medication_id,
                            medication_name=t.medication_name, source=req.source,
                            requested_by_user_id=_int_or_none(req.requested_by_user_id),
                            dose_event_id=t.event_id if t.event_id is not None else _int_or_none(req.dose_event_id),
                            conversation_id=_int_or_none(req.conversation_id), status=_UNCERTAIN, reason=None,
                            pill_count_before=t.pill_count, needs_review=False, requested_at=now, completed_at=None,
                        )
                        s.add(row)
                        s.flush()
                        log_event(s, t.device_id, LogCategory.HARDWARE, "DROP_STARTED",
                                  {"drop_id": row.drop_id, "slot": t.slot, "source": req.source,
                                   "medication_id": t.medication_id}, event_id=row.dose_event_id, at=now)
                        fx.drops.append(drop_to_view(row, self.clock))
                        fx.patients[t.patient_id] = "drop"
                        ctx = _Ctx(row.drop_id, req, t)
        except Exception:  # noqa: BLE001 - nothing has been sent yet
            log.exception("drop claim failed; nothing was sent")
            return self._db_error(req)
        self._deliver(fx)
        if ctx is not None:
            return ctx
        assert out is not None
        return out

    def _send_drop(self, slot: int) -> CommandResult:
        drop = self.hardware.drop_slot
        return self._send(Command(CommandName.DROP_SLOT, slot), lambda: drop(slot))

    def _finalise(self, ctx: _Ctx, result: CommandResult | None) -> DropOutcome:
        """Map the hardware result (``None`` = unknown) to the §5 outcome table and record it."""
        if result is None:
            status, reason, hw_text = _UNCERTAIN, INTERNAL_ERROR, f"UNCERTAIN {INTERNAL_ERROR}"
        else:
            certainty = self._certainty(result)
            hw_text = result.hardware_result
            if certainty is DropCertainty.DROPPED:
                status, reason = _DROPPED, None
            elif certainty is DropCertainty.NOT_DROPPED:
                status, reason = _FAILED, result.code
            else:
                status, reason = _UNCERTAIN, result.code
        return self._record_final(_Final(ctx, status, reason, hw_text, result, self.clock.now()))

    @staticmethod
    def _certainty(result: CommandResult) -> DropCertainty:
        try:
            certainty = drop_certainty(result)
        except Exception:  # noqa: BLE001 - cannot interpret it: assume the worst
            return DropCertainty.UNCERTAIN
        if result.command.name not in DROP_COMMANDS and not result.definitive:
            return DropCertainty.UNCERTAIN
        return certainty

    def _record_final(self, fin: _Final) -> DropOutcome:
        delays = tuple(self.db_retry_delays_s)
        tries = len(delays) + 1
        for i in range(tries):
            try:
                return self._write_final(fin)
            except Exception:  # noqa: BLE001
                log.warning("could not record the outcome of drop %s (try %d/%d)", fin.ctx.drop_id, i + 1, tries,
                            exc_info=True)
                if i < len(delays) and delays[i] > 0:
                    time.sleep(delays[i])
        with self._guard_lock:
            self._pending = fin
        log.error("outcome %s of drop %s NOT recorded; drops are blocked until it is", fin.status, fin.ctx.drop_id)
        if self.bus is not None:
            self.bus.publish(Topic.NOTICE, {
                "level": "error",
                "message": "A pill drop could not be saved. Drops are paused until it is saved; please ask for help.",
            })
        return self._unrecorded_outcome(fin)

    def _write_final(self, fin: _Final) -> DropOutcome:
        ctx, t, now = fin.ctx, fin.ctx.target, fin.at
        fx = self._effects()
        with self.db.session() as s:
            res = s.execute(
                update(PillDrop)
                .where(PillDrop.drop_id == ctx.drop_id, PillDrop.completed_at.is_(None))
                .values(status=fin.status, reason=fin.reason,
                        hardware_result=fin.hardware_text[:255] if fin.hardware_text else None,
                        completed_at=now, needs_review=fin.status == _UNCERTAIN)
                .execution_options(synchronize_session=False)
            )
            row = s.get(PillDrop, ctx.drop_id, populate_existing=True)
            if row is None:
                raise RuntimeError(f"drop {ctx.drop_id} vanished")
            comp = s.get(Compartment, t.compartment_id) if t.compartment_id is not None else None
            if res.rowcount != 1:
                # Someone else (startup recovery of another process) already closed it: keep that.
                log.error("drop %s was closed elsewhere (%s); outcome %s not applied",
                          row.drop_id, row.status, fin.status)
                log_event(s, row.device_id, LogCategory.SAFETY, "DROP_OUTCOME_NOT_APPLIED",
                          {"drop_id": row.drop_id, "outcome": fin.status, "recorded": row.status},
                          event_id=row.dose_event_id, at=now)
                after = int(comp.pill_count) if comp is not None else None
            else:
                if fin.status == _DROPPED:
                    before, after = self._decrement(s, comp, now)
                elif fin.status == _FAILED and fin.reason == _NO_PILL and comp is not None:
                    before = int(comp.pill_count or 0)
                    s.execute(update(Compartment).where(Compartment.compartment_id == comp.compartment_id)
                              .values(pill_count=0, updated_at=now).execution_options(synchronize_session=False))
                    comp = s.get(Compartment, comp.compartment_id, populate_existing=True)
                    after = 0
                else:
                    before = after = int(comp.pill_count) if comp is not None else None
                row.pill_count_after = after
                self._dose_outcome(s, fin, row, fx)
                self._outcome_notices(s, fin, row, comp, before, after, fx)
                self._audit_outcome(s, fin, row)
            fx.drops.append(drop_to_view(row, self.clock))
            fx.patients[t.patient_id] = "drop"
            dev = s.get(Device, t.device_id)
            low = comp is not None and _is_low(after, int(comp.low_stock_threshold or 0))
            outcome = self._final_outcome(fin, row, after, int(dev.manual_cooldown_minutes or 0) if dev else 0, low)
        self._deliver(fx)
        return outcome

    def _dose_outcome(self, s: Session, fin: _Final, row: PillDrop, fx: _Effects) -> None:
        ctx, t, now = fin.ctx, fin.ctx.target, fin.at
        if ctx.req.source == _SCHEDULE and t.event_id is not None:
            if fin.status == _DROPPED:
                values: dict[str, Any] = {"status": _DISPENSED, "drop_id": row.drop_id, "dispensed_at": now,
                                          "hardware_result": row.hardware_result, "needs_review": False,
                                          "next_attempt_at": None}
            elif fin.status == _FAILED:
                values = {"status": _HARDWARE_ERROR, "hardware_result": row.hardware_result, "needs_review": False,
                          "drop_id": None,
                          "next_attempt_at": now + timedelta(minutes=self.settings.auto_drop_retry_minutes)}
            else:
                values = {"status": _HARDWARE_ERROR, "hardware_result": row.hardware_result, "needs_review": True,
                          "drop_id": row.drop_id, "next_attempt_at": None}
            changed = cas_transition(s, t.event_id, _DISPENSING, values)
            if changed is None:
                current = s.get(DoseEvent, t.event_id)
                log.error("dose %s is %s (not DISPENSING); drop outcome not applied to it", t.event_id,
                          current.status if current else "missing")
                log_event(s, row.device_id, LogCategory.SAFETY, "DOSE_OUTCOME_NOT_APPLIED",
                          {"drop_id": row.drop_id, "status": current.status if current else None},
                          event_id=t.event_id, at=now)
                return
            record_dose_change(s, changed, settings=self.settings, clock=self.clock, action=f"DOSE_{changed.status}",
                               detail={"drop_id": row.drop_id, "result": row.hardware_result,
                                       "attempt": changed.attempts, "needs_review": bool(changed.needs_review)})
            fx.doses.append(dose_update_payload(changed, self.clock, previous=_DISPENSING,
                                                action=changed.status.lower()))
        elif fin.status == _DROPPED:
            self._satisfy_open_dose(s, row, now, fx, preferred=_int_or_none(ctx.req.dose_event_id))

    def _satisfy_open_dose(self, s: Session, row: PillDrop, at: datetime, fx: _Effects, *,
                           preferred: int | None = None) -> DoseEvent | None:
        """A manual/agent drop also satisfies the matching open dose whose window contains ``at``
        (so the automatic drop will not repeat it)."""
        if row.medication_id is None:
            return None
        early = timedelta(minutes=self.settings.dose_early_minutes)
        late = timedelta(minutes=self.settings.dose_late_minutes)
        candidates = list(s.scalars(
            select(DoseEvent)
            .options(selectinload(DoseEvent.medication))
            .where(
                DoseEvent.device_id == row.device_id,
                DoseEvent.user_id == row.patient_id,
                DoseEvent.medication_id == row.medication_id,
                DoseEvent.status.in_(safety.OPEN_STATUSES),
                DoseEvent.needs_review.is_(False),
                DoseEvent.scheduled_at >= at - late,
                DoseEvent.scheduled_at <= at + early,
            )
            .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
        ).all())
        candidates.sort(key=lambda ev: ev.event_id != preferred)
        for ev in candidates:
            changed = self._link_dose(s, ev, row, at, fx, action="dispensed")
            if changed is not None:
                return changed
        return None

    def _link_dose(self, s: Session, ev: DoseEvent, drop: PillDrop, at: datetime, fx: _Effects, *,
                   action: str) -> DoseEvent | None:
        """Mark an open dose DISPENSED by ``drop`` (and link the drop back to it)."""
        previous = ev.status
        changed = cas_transition(s, ev.event_id, previous, {
            "status": _DISPENSED, "drop_id": drop.drop_id, "dispensed_at": at, "dispense_source": drop.source,
            "hardware_result": drop.hardware_result, "next_attempt_at": None, "slot_number": drop.slot_number,
            "compartment_id": drop.compartment_id,
        }, require_no_review=True)
        if changed is None:
            return None
        if drop.dose_event_id is None:
            drop.dose_event_id = changed.event_id
        record_dose_change(s, changed, settings=self.settings, clock=self.clock, action="DOSE_DISPENSED",
                           detail={"previous": previous, "drop_id": drop.drop_id, "source": drop.source,
                                   "how": action})
        fx.doses.append(dose_update_payload(changed, self.clock, previous=previous, action=action))
        fx.patients[changed.user_id] = "dose"
        return changed

    def _decrement(self, s: Session, comp: Compartment | None, now: datetime) -> tuple[int | None, int | None]:
        """Atomic ``pill_count - 1`` (never below 0). Returns (before, after)."""
        if comp is None:
            return None, None
        before = int(comp.pill_count or 0)
        s.execute(
            update(Compartment)
            .where(Compartment.compartment_id == comp.compartment_id)
            .values(pill_count=case((Compartment.pill_count > 0, Compartment.pill_count - 1), else_=0),
                    updated_at=now)
            .execution_options(synchronize_session=False)
        )
        comp = s.get(Compartment, comp.compartment_id, populate_existing=True)
        return before, int(comp.pill_count or 0)

    # ------------------------------------------------------------------ notifications
    def _outcome_notices(self, s: Session, fin: _Final, row: PillDrop, comp: Compartment | None,
                         before: int | None, after: int | None, fx: _Effects) -> None:
        if fin.status == _DROPPED:
            when = clock_label(self.clock.to_local(fin.at))
            fx.notes.add(
                s, patient_id=row.patient_id, kind=NotificationKind.PILL_DROPPED.value, title="Pill dropped",
                body=f"{row.medication_name or 'A pill'} dropped from {_container(row.slot_number)} at {when}.",
                data=self._drop_data(row), to_patient=True, to_caregivers=self.settings.notify_caregivers_on_drop,
            )
            self._stock_notices(s, row, comp, before, after, fx)
        elif fin.status == _FAILED:
            if fin.reason == _NO_PILL and comp is not None:
                self._empty_notice(s, row, comp, fx)
            else:
                self._notify_failed(s, row, fx)
        else:
            self._notify_uncertain(s, row, fx)

    def _stock_notices(self, s: Session, row: PillDrop, comp: Compartment | None, before: int | None,
                       after: int | None, fx: _Effects) -> None:
        """LOW_STOCK once per crossing into the low band; EMPTY when the count reaches 0."""
        if comp is None or after is None or before is None:
            return
        threshold = int(comp.low_stock_threshold or 0)
        if _is_low(after, threshold) and not _is_low(before, threshold):
            fx.notes.add(
                s, patient_id=row.patient_id, kind=NotificationKind.LOW_STOCK.value, title="Low stock",
                body=f"{_container(comp.slot_number).capitalize()} ({row.medication_name or 'medication'}) has "
                     f"{plural(after, 'pill')} left. Please refill it soon.",
                data={**self._container_data(comp), "pill_count": after, "medication_id": row.medication_id},
                to_patient=True, to_caregivers=True,
            )
        if after == 0 and before > 0:
            self._empty_notice(s, row, comp, fx)

    def _empty_notice(self, s: Session, row: PillDrop, comp: Compartment | None, fx: _Effects) -> None:
        """EMPTY at most once per "empty episode" (until the next refill stamps ``loaded_at``)."""
        if comp is None:
            return
        marker = iso(comp.loaded_at)
        if self._notified(s, row.patient_id, NotificationKind.EMPTY, compartment_id=comp.compartment_id,
                          loaded_at=marker):
            return
        fx.notes.add(
            s, patient_id=row.patient_id, kind=NotificationKind.EMPTY.value, title="Container empty",
            body=f"{_container(comp.slot_number).capitalize()} ({row.medication_name or 'medication'}) is empty. "
                 "Please refill it.",
            data={**self._container_data(comp), "medication_id": row.medication_id, "drop_id": row.drop_id,
                  "loaded_at": marker},
            to_patient=True, to_caregivers=True,
        )

    def _notify_failed(self, s: Session, row: PillDrop, fx: _Effects) -> None:
        """DROP_FAILED (for scheduled retries only on the dose's first failure)."""
        if row.source == _SCHEDULE and row.dose_event_id is not None and self._notified(
                s, row.patient_id, NotificationKind.DROP_FAILED, dose_event_id=row.dose_event_id):
            return
        if row.reason == _STOPPED:
            body = f"The drop from {_container(row.slot_number)} was stopped. No pill was dropped."
        else:
            body = (f"{row.medication_name or 'The pill'} did not drop from {_container(row.slot_number)} "
                    f"({row.reason}). No pill was dropped.")
        fx.notes.add(s, patient_id=row.patient_id, kind=NotificationKind.DROP_FAILED.value, title="Pill did not drop",
                     body=body, data=self._drop_data(row), to_patient=True, to_caregivers=True)

    def _notify_uncertain(self, s: Session, row: PillDrop, fx: _Effects) -> None:
        fx.notes.add(
            s, patient_id=row.patient_id, kind=NotificationKind.DROP_UNCERTAIN.value,
            title="Please check the dispenser",
            body=f"A drop from {_container(row.slot_number)} ({row.medication_name or 'medication'}) could not be "
                 "confirmed. Check whether a pill came out, then mark it as dropped or not dropped.",
            data=self._drop_data(row), to_patient=True, to_caregivers=True,
        )

    @staticmethod
    def _notified(s: Session, patient_id: int, kind: NotificationKind, **match: Any) -> bool:
        """True if a ``kind`` notification about ``patient_id`` already carries ``match`` in its data."""
        rows = s.scalars(
            select(Notification.data)
            .where(Notification.patient_id == patient_id, Notification.kind == kind.value)
            .order_by(Notification.notification_id.desc())
            .limit(200)
        ).all()
        return any(all((data or {}).get(k) == v for k, v in match.items()) for data in rows)

    @staticmethod
    def _drop_data(row: PillDrop) -> dict[str, Any]:
        return {
            "drop_id": row.drop_id, "slot": row.slot_number,
            "container_number": None if row.slot_number is None else row.slot_number + 1,
            "medication_id": row.medication_id, "source": row.source, "status": row.status,
            "reason": row.reason, "pill_count_after": row.pill_count_after, "dose_event_id": row.dose_event_id,
        }

    @staticmethod
    def _container_data(comp: Compartment) -> dict[str, Any]:
        return {"slot": comp.slot_number, "container_number": comp.slot_number + 1,
                "compartment_id": comp.compartment_id}

    def _audit_outcome(self, s: Session, fin: _Final, row: PillDrop) -> None:
        log_event(s, row.device_id, LogCategory.HARDWARE, f"DROP_{fin.status}",
                  {"drop_id": row.drop_id, "slot": row.slot_number, "source": row.source, "reason": fin.reason,
                   "result": row.hardware_result, "pill_count_after": row.pill_count_after},
                  event_id=row.dose_event_id, at=fin.at)
        if fin.status == _UNCERTAIN:
            enqueue_device_event(s, device_id=row.device_id, event_type="drop_uncertain", code=fin.reason,
                                 detail={"command": CommandName.DROP_SLOT.value})
        elif fin.status == _FAILED and fin.reason != _STOPPED:
            enqueue_device_event(s, device_id=row.device_id, event_type="drop_failed", code=fin.reason,
                                 detail={"command": CommandName.DROP_SLOT.value})

    # ------------------------------------------------------------------ review resolution
    def _resolve_dose_dropped(self, s: Session, row: PillDrop, fx: _Effects) -> None:
        at = safety.drop_time(row)
        ev = s.get(DoseEvent, row.dose_event_id) if row.dose_event_id is not None else None
        if ev is not None and ev.status in (_HARDWARE_ERROR, _DISPENSING, _SCHEDULED, _DUE, _CANCELLED, _MISSED) \
                and ev.drop_id in (None, row.drop_id):
            previous = ev.status
            changed = cas_transition(s, ev.event_id, previous, {
                "status": _DISPENSED, "drop_id": row.drop_id, "dispensed_at": at, "needs_review": False,
                "next_attempt_at": None, "dispense_source": ev.dispense_source or row.source,
            })
            if changed is not None:
                record_dose_change(s, changed, settings=self.settings, clock=self.clock, action="DOSE_DISPENSED",
                                   detail={"previous": previous, "drop_id": row.drop_id, "how": "review"},
                                   category=LogCategory.ADMIN)
                fx.doses.append(dose_update_payload(changed, self.clock, previous=previous, action="resolved"))
            return
        if ev is None:
            self._satisfy_open_dose(s, row, at, fx)

    def _resolve_dose_not_dropped(self, s: Session, row: PillDrop, now: datetime, fx: _Effects) -> None:
        ev = s.get(DoseEvent, row.dose_event_id) if row.dose_event_id is not None else None
        if ev is None or ev.status != _HARDWARE_ERROR or ev.drop_id not in (None, row.drop_id):
            return
        start, end = safety.dispense_window(ev, self.settings)
        values: dict[str, Any] = {"needs_review": False, "drop_id": None, "next_attempt_at": None}
        if now > end:
            values.update(status=_MISSED, missed_at=now)
        else:
            values.update(status=_DUE if now >= start else _SCHEDULED)
        changed = cas_transition(s, ev.event_id, _HARDWARE_ERROR, values)
        if changed is None:
            return
        record_dose_change(s, changed, settings=self.settings, clock=self.clock, action=f"DOSE_{changed.status}",
                           detail={"previous": _HARDWARE_ERROR, "drop_id": row.drop_id, "how": "review"},
                           category=LogCategory.ADMIN)
        fx.doses.append(dose_update_payload(changed, self.clock, previous=_HARDWARE_ERROR, action="resolved"))
        if changed.status == _MISSED:
            fx.notes.add(s, **missed_dose_notice(changed, self.clock, changed.slot_number))

    def _clear_retry_waits(self, s: Session, device_id: str, now: datetime) -> None:
        """A review was resolved: doses waiting for their next automatic attempt may go now."""
        s.execute(
            update(DoseEvent)
            .where(DoseEvent.device_id == device_id, DoseEvent.status.in_(safety.OPEN_STATUSES),
                   DoseEvent.needs_review.is_(False), DoseEvent.next_attempt_at > now)
            .values(next_attempt_at=None)
            .execution_options(synchronize_session=False)
        )

    # ------------------------------------------------------------------ hardware
    def _ready_now(self) -> bool:
        """The device can take ``DROP_SLOT`` right now without any preparatory command."""
        if not callable(getattr(self.hardware, "drop_slot", None)) or self._interrupt.is_set():
            return False
        snap = self._snapshot()
        if snap is None or not snap.responsive or snap.gate is not GateState.CLOSED:
            return False
        in_flight = (snap.in_flight or "").split(" ")[0].upper()
        return snap.ready_for_motion and in_flight not in _MOTION_COMMANDS

    def _prepare_hardware(self) -> _Prep:
        """Check 8: device ready for a drop (HOME / CLOSE_GATE first when needed). No DB writes."""
        hw = self.hardware
        if not callable(getattr(hw, "drop_slot", None)):
            return _Prep(False, "NO_DROP_SUPPORT")
        snap = self._snapshot()
        if snap is None:
            return _Prep(False, "NO_STATUS")
        if not snap.connected:
            return _Prep(False, HostCode.NOT_CONNECTED.value)
        if snap.state is DeviceState.UNKNOWN or snap.homed is None or not snap.responsive:
            r = self._send(Command.status(), hw.status)      # resynchronise the host mirror
            if not r.ok:
                return _Prep(False, r.code, r)
            snap = self._snapshot() or snap
            if not snap.connected:
                return _Prep(False, HostCode.NOT_CONNECTED.value)
            if snap.state is DeviceState.UNKNOWN:
                return _Prep(False, f"NOT_READY_{DeviceState.UNKNOWN.value}")   # never move blind
        if snap.state is DeviceState.FAULT:
            return _Prep(False, DeviceState.FAULT.value)      # a caregiver must re-home it
        if snap.state is DeviceState.HOMING:
            snap = self._wait_while_homing()
            if self._interrupt.is_set():
                return _Prep(False, _STOPPED, cancelled=True)
            if snap is None:
                return _Prep(False, "NO_STATUS")
            if not snap.connected:
                return _Prep(False, HostCode.NOT_CONNECTED.value)
            if snap.state is DeviceState.HOMING:
                return _Prep(False, "HOMING_TIMEOUT")
            if snap.state is DeviceState.FAULT:
                return _Prep(False, DeviceState.FAULT.value)
        in_flight = (snap.in_flight or "").split(" ")[0].upper()
        if snap.state in (DeviceState.MOVING, DeviceState.AT_TARGET) or in_flight in _MOTION_COMMANDS:
            return _Prep(False, Err.BUSY.value)
        if snap.gate is GateState.OPEN or snap.state is DeviceState.GATE_OPEN:
            r = self._send(Command.close_gate(), hw.close_gate)
            if not r.ok:
                return _Prep(False, r.code, r)
            snap = self._snapshot() or snap
        if snap.state in (DeviceState.SAFE_STOP, DeviceState.BOOT) or snap.homed is not True:
            if not self.settings.hw_auto_home:
                return _Prep(False, Err.NOT_HOMED.value)
            if self._interrupt.is_set():
                return _Prep(False, _STOPPED, cancelled=True)
            r = self._send(Command.home(), hw.home)
            if self._interrupt.is_set() or r.code == _STOPPED:
                return _Prep(False, _STOPPED, r, cancelled=True)
            if not r.ok:
                return _Prep(False, r.code, r)
            snap = self._snapshot() or snap
        if self._interrupt.is_set():
            return _Prep(False, _STOPPED, cancelled=True)
        if not snap.ready_for_motion:
            return _Prep(False, f"NOT_READY_{snap.state.value}")
        return _Prep(True)

    def _wait_while_homing(self) -> DeviceSnapshot | None:
        deadline = time.monotonic() + self.settings.timeout_home_s
        while True:
            snap = self._snapshot()
            if snap is None or snap.state is not DeviceState.HOMING or not snap.connected:
                return snap
            if self._interrupt.is_set() or time.monotonic() >= deadline:
                return snap
            time.sleep(self.poll_interval_s)

    def _send(self, command: Command, fn: Callable[[], CommandResult]) -> CommandResult:
        """Run one hardware command; retry (bounded) while another host command holds the link."""
        deadline = time.monotonic() + self.busy_wait_s
        while True:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - contract says never raises; if it does, assume written
                log.exception("hardware %s raised", command.to_line())
                return CommandResult.host_failure(command, HostCode.DISCONNECTED,
                                                  detail=f"exception: {type(exc).__name__}")
            if result.code != HostCode.BUSY_LOCAL.value or time.monotonic() >= deadline:
                return result
            # BUSY_LOCAL = never written (e.g. a heartbeat PING in flight): safe to retry.
            time.sleep(min(self.poll_interval_s, 0.02))

    def _snapshot(self) -> DeviceSnapshot | None:
        try:
            return self.hardware.snapshot()
        except Exception:  # noqa: BLE001
            log.exception("hardware snapshot failed")
            return None

    def _in_flight_grace_s(self) -> float:
        """How long an in-flight row counts as "a drop is running" before it is treated as stuck."""
        st = self.settings
        return st.timeout_drop_s + st.timeout_dispense_s + st.timeout_gate_s + st.drop_close_delay_ms / 1000 + 30

    def _device_message(self, prep: _Prep) -> str:
        if prep.reason in (HostCode.NOT_CONNECTED.value, "NO_STATUS", "NO_DROP_SUPPORT"):
            return "The dispenser is not connected, so no pill was dropped. Please ask your caregiver for help."
        if prep.reason == Err.BUSY.value:
            return "The dispenser is busy, so no pill was dropped. Please try again in a moment."
        return "The dispenser needs attention, so no pill was dropped. Please ask your caregiver for help."

    # ------------------------------------------------------------------ queries / helpers
    @staticmethod
    def _last_drop_at(s: Session, device_id: str) -> datetime | None:
        happened = func.coalesce(PillDrop.completed_at, PillDrop.requested_at)
        row = s.execute(
            select(PillDrop.completed_at, PillDrop.requested_at)
            .where(PillDrop.device_id == device_id, PillDrop.status.in_(_COUNTED))
            .order_by(happened.desc(), PillDrop.drop_id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return row.completed_at or row.requested_at

    @staticmethod
    def _last_drop_row(s: Session, patient_id: int) -> PillDrop | None:
        """Latest completed DROPPED/UNCERTAIN drop of the patient."""
        return s.scalars(
            select(PillDrop)
            .where(PillDrop.patient_id == patient_id, PillDrop.status.in_(_COUNTED),
                   PillDrop.completed_at.is_not(None))
            .order_by(PillDrop.completed_at.desc(), PillDrop.drop_id.desc())
            .limit(1)
        ).first()

    def _day_events(self, s: Session, patient_id: int, day: date) -> list[DoseEvent]:
        start = self.clock.local_to_utc(datetime.combine(day, dtime(0, 0)))
        end = self.clock.local_to_utc(datetime.combine(day + timedelta(days=1), dtime(0, 0)))
        return list(s.scalars(
            select(DoseEvent)
            .options(selectinload(DoseEvent.medication))
            .where(DoseEvent.user_id == patient_id, DoseEvent.scheduled_at >= start, DoseEvent.scheduled_at < end)
            .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
        ).all())

    @staticmethod
    def _next_event(s: Session, patient_id: int, now: datetime) -> DoseEvent | None:
        return s.scalars(
            select(DoseEvent)
            .options(selectinload(DoseEvent.medication))
            .where(DoseEvent.user_id == patient_id, DoseEvent.status.in_(safety.OPEN_STATUSES),
                   DoseEvent.needs_review.is_(False), DoseEvent.scheduled_at > now)
            .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
            .limit(1)
        ).first()

    def _alerts(self, s: Session, dev: Device | None, containers: Iterable[ContainerInfo],
                today: Iterable[DoseEvent], snap: DeviceSnapshot | None) -> list[dict[str, Any]]:
        if dev is None:
            return [{"kind": NotificationKind.DEVICE_ALERT.value, "message": "No dispenser is linked to this account."}]
        alerts: list[dict[str, Any]] = []
        if dev.device_id != self.settings.device_id or snap is None or not snap.connected:
            alerts.append({"kind": NotificationKind.DEVICE_ALERT.value, "message": "The dispenser is not connected."})
        elif snap.state is DeviceState.FAULT:
            alerts.append({"kind": NotificationKind.DEVICE_ALERT.value,
                           "message": "The dispenser needs attention. A caregiver should restart it."})
        for row in s.scalars(
            select(PillDrop).where(PillDrop.device_id == dev.device_id, PillDrop.status == _UNCERTAIN,
                                   PillDrop.needs_review.is_(True)).order_by(PillDrop.drop_id)
        ).all():
            when = clock_label(self.clock.to_local(safety.drop_time(row)))
            alerts.append({"kind": NotificationKind.DROP_UNCERTAIN.value, "drop_id": row.drop_id,
                           "message": f"The {when} drop from {_container(row.slot_number)} needs to be checked by a "
                                      "caregiver."})
        for c in containers:
            if c.medication_id is None:
                continue
            if c.empty:
                alerts.append({"kind": NotificationKind.EMPTY.value, "slot": c.slot,
                               "message": f"Container {c.container_number} ({c.medication_name}) is empty."})
            elif c.low_stock:
                alerts.append({"kind": NotificationKind.LOW_STOCK.value, "slot": c.slot,
                               "message": f"Container {c.container_number} has {plural(c.pill_count, 'pill')} left."})
        for ev in today:
            if ev.status == _MISSED:
                name = ev.medication.name if ev.medication is not None else "medication"
                alerts.append({"kind": NotificationKind.MISSED_DOSE.value, "event_id": ev.event_id,
                               "message": f"The {clock_label(self.clock.to_local(ev.scheduled_at))} dose of {name} "
                                          "was missed."})
        return alerts

    def _auto_drop_candidates(self) -> list[tuple[int, int]]:
        now = self.clock.now()
        late = timedelta(minutes=self.settings.dose_late_minutes)
        with self.db.session() as s:
            dev = s.get(Device, self.settings.device_id)
            if dev is None or not dev.auto_drop_enabled:
                return []
            rows = s.execute(
                select(DoseEvent.event_id, DoseEvent.user_id)
                .where(
                    DoseEvent.device_id == dev.device_id,
                    DoseEvent.user_id == dev.user_id,
                    DoseEvent.status.in_(safety.OPEN_STATUSES),
                    DoseEvent.needs_review.is_(False),
                    DoseEvent.scheduled_at <= now,
                    DoseEvent.scheduled_at >= now - late,
                    or_(DoseEvent.next_attempt_at.is_(None), DoseEvent.next_attempt_at <= now),
                )
                .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
            ).all()
            return [(int(r.event_id), int(r.user_id)) for r in rows]

    def _defer(self, items: list[tuple[int, int]]) -> None:
        if not items:
            return
        retry_at = self.clock.now() + timedelta(minutes=self.settings.auto_drop_retry_minutes)
        try:
            with self.db.session() as s:
                s.execute(update(DoseEvent).where(DoseEvent.event_id.in_([eid for eid, _ in items]))
                          .values(next_attempt_at=retry_at).execution_options(synchronize_session=False))
        except Exception:  # noqa: BLE001 - worst case they are tried at the next tick
            log.warning("could not defer %d automatic drop(s)", len(items), exc_info=True)

    def _flush_pending(self) -> bool:
        """Retry writing an unrecorded outcome. True when nothing is pending any more."""
        with self._guard_lock:
            fin = self._pending
            if fin is None:
                return True
            try:
                self._write_final(fin)
            except Exception:  # noqa: BLE001
                log.warning("unrecorded drop outcome still cannot be written", exc_info=True)
                return False
            self._pending = None
        log.info("previously unrecorded outcome of drop %s is now recorded", fin.ctx.drop_id)
        return True

    def _require_device(self, s: Session, patient_id: int) -> Device:
        dev = patient_device(s, self.settings, patient_id) if is_id(patient_id) else None
        if dev is None:
            raise NotFoundError("No dispenser is linked to this patient.")
        return dev

    @staticmethod
    def _settings_view(dev: Device) -> dict[str, Any]:
        return {
            "manual_cooldown_minutes": int(dev.manual_cooldown_minutes or 0),
            "auto_drop_enabled": bool(dev.auto_drop_enabled),
            "device_id": dev.device_id,
            "num_slots": int(dev.num_slots),
            "patient_id": dev.user_id,
        }

    @staticmethod
    def _get_drop(s: Session, drop_id: int, patient_id: int | None) -> PillDrop:
        row = s.get(PillDrop, drop_id) if is_id(drop_id) else None
        if row is None or (patient_id is not None and row.patient_id != patient_id):
            raise NotFoundError(f"Drop {drop_id} not found.")
        return row

    @staticmethod
    def _get_event(s: Session, event_id: int, patient_id: int | None) -> DoseEvent:
        ev = s.get(DoseEvent, event_id) if is_id(event_id) else None
        if ev is None or (patient_id is not None and ev.user_id != patient_id):
            raise NotFoundError(f"Dose {event_id} not found.")
        return ev

    def _parse_day(self, value: date | str | None) -> date:
        if value is None:
            return self.clock.today_local()
        if isinstance(value, datetime):
            return self.clock.to_local(value).date() if value.tzinfo else value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError:
                pass
        raise ValidationError("date must look like 2026-10-05.")

    def _effects(self) -> _Effects:
        return _Effects(self.notifications)

    def _deliver(self, fx: _Effects) -> None:
        publish_all(self.bus, Topic.DROP, fx.drops)
        publish_all(self.bus, Topic.DOSE_UPDATED, fx.doses)
        publish_patient_status(self.bus, fx.patients)
        fx.notes.deliver()

    # ------------------------------------------------------------------ outcomes & wording
    def _outcome(self, status: str, req: _Request, check: _Check, drop_id: int | None,
                 hardware: CommandResult | None) -> DropOutcome:
        t = check.target
        return DropOutcome(
            status=status, source=req.source, message=check.message, reason=check.reason, drop_id=drop_id,
            slot=t.slot if t is not None else _int_or_none(req.slot),
            medication_id=t.medication_id if t is not None else None,
            medication_name=t.medication_name if t is not None else None,
            pill_count_after=t.pill_count if t is not None else None,
            cooldown_remaining_s=check.remaining_s, next_allowed_at=check.next_allowed_at, hardware=hardware,
        )

    def _final_outcome(self, fin: _Final, row: PillDrop, after: int | None, cooldown_minutes: int,
                       low_stock: bool = False) -> DropOutcome:
        status = row.status
        remaining, next_at = 0, None
        if status in _COUNTED and cooldown_minutes > 0:
            next_at = safety.drop_time(row) + timedelta(minutes=cooldown_minutes)
            remaining = max(0, int(math.ceil((next_at - fin.at).total_seconds())))
            if remaining == 0:
                next_at = None
        return DropOutcome(
            status=status, source=row.source,
            message=self._final_message(status, row.reason, row.slot_number, row.medication_name, after, low_stock),
            reason=row.reason, drop_id=row.drop_id, slot=row.slot_number, medication_id=row.medication_id,
            medication_name=row.medication_name, pill_count_after=after, cooldown_remaining_s=remaining,
            next_allowed_at=self.clock.to_local(next_at) if next_at is not None else None, hardware=fin.result,
        )

    @staticmethod
    def _final_message(status: str, reason: str | None, slot: int | None, medication_name: str | None,
                       after: int | None, low_stock: bool = False) -> str:
        where = _container(slot)
        if status == _DROPPED:
            text = f"{medication_name or 'Your pill'} dropped from {where}."
            if after == 0:
                text += f" {where.capitalize()} is now empty."
            elif low_stock and after is not None:
                text += f" Only {plural(after, 'pill')} left."
            return text
        if status == _FAILED:
            if reason == _STOPPED:
                return "Stopped. No pill was dropped."
            if reason == _NO_PILL:
                return f"No pill came out of {where}. It may be empty. Please ask your caregiver to refill it."
            return f"The pill did not drop from {where}. Please try again, or ask your caregiver for help."
        return (f"I could not confirm whether a pill dropped from {where}. Please check. "
                "A caregiver must confirm it before the next pill can drop.")

    def _unrecorded_outcome(self, fin: _Final) -> DropOutcome:
        t = fin.ctx.target
        message = self._final_message(fin.status, fin.reason, t.slot, t.medication_name, None) + \
            " The record could not be saved yet, so no more pills will drop until it is."
        return DropOutcome(
            status=fin.status, source=fin.ctx.req.source, message=message,
            reason=UNRECORDED if fin.status == _DROPPED else fin.reason, drop_id=fin.ctx.drop_id, slot=t.slot,
            medication_id=t.medication_id, medication_name=t.medication_name, hardware=fin.result,
        )

    def _outcome_internal_after_claim(self, ctx: _Ctx) -> DropOutcome:
        t = ctx.target
        return DropOutcome(
            status=_UNCERTAIN, source=ctx.req.source, reason=INTERNAL_ERROR, drop_id=ctx.drop_id, slot=t.slot,
            medication_id=t.medication_id, medication_name=t.medication_name,
            message=self._final_message(_UNCERTAIN, INTERNAL_ERROR, t.slot, t.medication_name, None),
        )

    def _db_error(self, req: _Request, *, unrecorded: bool = False) -> DropOutcome:
        message = ("An earlier drop has not been saved yet, so no pill was dropped. Please ask your caregiver for help."
                   if unrecorded else
                   "I can't check the records right now, so no pill was dropped. Please try again in a moment.")
        return DropOutcome(status=_DENIED, source=req.source, reason=_R.DB_ERROR.value, message=message,
                           slot=_int_or_none(req.slot), medication_id=_int_or_none(req.medication_id))

    def _cooldown_message(self, last_at: datetime, next_at: datetime, remaining: int, now: datetime) -> str:
        last_local, next_local = self.clock.to_local(last_at), self.clock.to_local(next_at)
        day = " tomorrow" if next_local.date() > self.clock.to_local(now).date() else ""
        return (f"A pill was dropped at {clock_label(last_local)}. The next pill can drop{day} at "
                f"{clock_label(next_local)}, in {duration_label(remaining)}.")

    @staticmethod
    def _log_outcome(req: _Request, out: DropOutcome) -> None:
        log.info("drop request (%s, patient %s, slot %s, medication %s) -> %s %s", req.source, req.patient_id,
                 out.slot, out.medication_id, out.status, out.reason or "")
