"""Deterministic dose-eligibility rules (ARCHITECTURE §5, handoff §4.2, §13, §33).

Everything here is a *pure* function of ``(session, now, settings)``: it reads the
database and never writes, sends nothing to hardware and has no clock of its own.
This is the code that "authorizes" (handoff §33: AI interprets, deterministic code
authorizes and actuates) — speech, Gemini or UI input never reach it as anything
but a request to evaluate.

A dose event is **eligible** when all of these hold:

1. ``start <= now <= end`` (``start = scheduled_at - early``, ``end = scheduled_at + late``);
2. status is SCHEDULED/DUE, or HARDWARE_ERROR with ``needs_review`` False and
   ``attempts < max_dispense_attempts``;
3. medication active and confirmed by a human, schedule active;
4. the medication has an active compartment on this device with ``0 <= slot < num_slots``
   (resolved *now*, not when the event was generated);
5. no event on this device is DISPENSING;
6. the same medication was not DISPENSED/TAKEN within ``min_dose_interval_minutes``.

Selection: earliest ``scheduled_at``, then lowest slot. When nothing is eligible the
verdict is, in priority order: IN_PROGRESS -> BLOCKED(NEEDS_REVIEW) -> DUPLICATE
(in-window dose already accessed, or TOO_SOON) -> BLOCKED(NO_COMPARTMENT /
UNCONFIRMED_MEDICATION / INACTIVE) -> NOTHING_DUE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from tactidose.config import Settings
from tactidose.core.clock import Clock
from tactidose.core.interfaces import BlockReason, DoseInfo, DueSummary
from tactidose.db.models import (
    ACCESSED_STATUSES,
    DoseEvent,
    DoseStatus,
    Medication,
    Schedule,
)
from tactidose.medication.compartments import assigned_slots

log = logging.getLogger(__name__)

__all__ = [
    "Assessment",
    "SafetyDecision",
    "Verdict",
    "assess_event",
    "display_slot",
    "dispense_window",
    "evaluate",
    "find_confirmable",
    "in_window",
    "is_dispensable",
    "latest_accessed",
    "recent_accesses",
    "to_dose_info",
]

_ACCESSED = tuple(s.value for s in ACCESSED_STATUSES)
_OPEN_STATUSES = (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value)
#: Blocks that mean "already accessed" for the verdict priority (DUPLICATE tier).
_DUPLICATE_REASONS = frozenset({BlockReason.TOO_SOON})
#: Blocks of the lowest priority tier (configuration problems).
_CONFIG_REASONS = frozenset(
    {BlockReason.NO_COMPARTMENT, BlockReason.UNCONFIRMED_MEDICATION, BlockReason.INACTIVE}
)


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DUPLICATE = "DUPLICATE"
    NOTHING_DUE = "NOTHING_DUE"


@dataclass(frozen=True)
class Assessment:
    """One dose event judged against the rules. ``reason is None`` means eligible."""

    event: DoseEvent
    slot: int | None
    compartment_id: int | None
    reason: BlockReason | None = None

    @property
    def eligible(self) -> bool:
        return self.reason is None


@dataclass
class SafetyDecision:
    """Result of :func:`evaluate` (plus everything a ``DueSummary`` needs)."""

    verdict: Verdict
    now: datetime
    event: DoseEvent | None = None           # selected / most relevant event for the verdict
    slot: int | None = None                  # resolved slot (ALLOW) or display slot
    compartment_id: int | None = None
    reason: BlockReason | None = None        # BLOCKED reason, TOO_SOON, IN_PROGRESS
    due: list[Assessment] = field(default_factory=list)          # eligible under all rules, dispense order
    queued: list[Assessment] = field(default_factory=list)       # pass rules 1-4, 6 but wait for rule 5
    accessed: list[DoseEvent] = field(default_factory=list)      # in window and DISPENSED/TAKEN
    blocked: list[Assessment] = field(default_factory=list)      # in window (or DISPENSING) but blocked
    awaiting: list[DoseEvent] = field(default_factory=list)      # DISPENSED, confirmable, newest first
    in_progress: list[DoseEvent] = field(default_factory=list)   # DISPENSING on this device (rule 5)
    next_upcoming: DoseEvent | None = None
    slots: dict[int, tuple[int, int]] = field(default_factory=dict, repr=False)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def dose_info(self, clock: Clock) -> DoseInfo | None:
        """DoseInfo of :attr:`event` (None when there is no event)."""
        if self.event is None:
            return None
        if self.verdict is Verdict.ALLOW:
            return to_dose_info(self.event, clock, slot=self.slot)
        return to_dose_info(self.event, clock, slot=display_slot(self.event, self.slots))

    def next_upcoming_info(self, clock: Clock) -> DoseInfo | None:
        if self.next_upcoming is None:
            return None
        return to_dose_info(self.next_upcoming, clock, slot=display_slot(self.next_upcoming, self.slots))

    def due_summary(self, clock: Clock) -> DueSummary:
        def info(ev: DoseEvent) -> DoseInfo:
            return to_dose_info(ev, clock, slot=display_slot(ev, self.slots))

        return DueSummary(
            now_local=clock.to_local(self.now),
            due=tuple(to_dose_info(a.event, clock, slot=a.slot) for a in self.due),
            awaiting_confirmation=tuple(info(ev) for ev in self.awaiting),
            accessed=tuple(info(ev) for ev in self.accessed),
            blocked=tuple((to_dose_info(a.event, clock, slot=a.slot), a.reason) for a in self.blocked
                          if a.reason is not None),
            next_upcoming=self.next_upcoming_info(clock),
        )


# --------------------------------------------------------------------------- small rules


def dispense_window(event_or_time: DoseEvent | datetime, settings: Settings) -> tuple[datetime, datetime]:
    """``(start, end)`` of the dispensing window (ARCHITECTURE §5 "Window")."""
    at = event_or_time.scheduled_at if isinstance(event_or_time, DoseEvent) else event_or_time
    return (
        at - timedelta(minutes=settings.dose_early_minutes),
        at + timedelta(minutes=settings.dose_late_minutes),
    )


def in_window(event_or_time: DoseEvent | datetime, now: datetime, settings: Settings) -> bool:
    """Rule 1: ``start <= now <= end`` (both ends inclusive)."""
    start, end = dispense_window(event_or_time, settings)
    return start <= now <= end


def is_dispensable(event: DoseEvent, settings: Settings) -> bool:
    """Rule 2 (status rule). A dose flagged ``needs_review`` is never dispensable: its
    previous outcome was uncertain (gate may have opened) — handoff §30 / SERIAL §9.4."""
    if event.needs_review:
        return False
    if event.status in _OPEN_STATUSES:
        return True
    return (
        event.status == DoseStatus.HARDWARE_ERROR.value
        and int(event.attempts or 0) < settings.max_dispense_attempts
    )


def recent_accesses(session: Session, now: datetime, settings: Settings) -> dict[int, datetime]:
    """Rule 6 input: ``medication_id -> latest access time`` within ``min_dose_interval_minutes``.

    Not limited to this device or schedule: any recent access of the same
    medication blocks another one (prevents double doses from overlapping windows).
    """
    if settings.min_dose_interval_minutes <= 0:
        return {}
    cutoff = now - timedelta(minutes=settings.min_dose_interval_minutes)
    rows = session.execute(
        select(DoseEvent.medication_id, DoseEvent.dispensed_at, DoseEvent.confirmed_taken_at).where(
            DoseEvent.status.in_(_ACCESSED),
            or_(DoseEvent.dispensed_at >= cutoff, DoseEvent.confirmed_taken_at >= cutoff),
        )
    ).all()
    out: dict[int, datetime] = {}
    for med_id, dispensed_at, taken_at in rows:
        at = max(t for t in (dispensed_at, taken_at) if t is not None)
        if med_id not in out or at > out[med_id]:
            out[med_id] = at
    return out


def assess_event(
    event: DoseEvent,
    settings: Settings,
    slots: dict[int, tuple[int, int]],
    recent: dict[int, datetime],
) -> Assessment:
    """Apply rules 1-4 and 6 to one event (rule 5 is device-wide; see :func:`evaluate`).

    The returned assessment carries the first failing rule's reason, checked in the
    order 2 -> 3 -> 4 -> 6 (rule 1 is a precondition: callers only pass in-window events).
    Events in a non-dispensable, non-review status (accessed / final) are not assessed.
    """
    resolved = slots.get(event.medication_id)
    slot, comp_id = resolved if resolved is not None else (None, None)

    def blocked(reason: BlockReason) -> Assessment:
        return Assessment(event, display_slot(event, slots), comp_id, reason)

    # Rule 2 — status; uncertain / exhausted hardware errors are locked for caregiver review.
    if not is_dispensable(event, settings):
        return blocked(BlockReason.NEEDS_REVIEW)
    # Rule 3 — only active, human-confirmed medications on active schedules (handoff §18 activation rule).
    med: Medication | None = event.medication
    sched: Schedule | None = event.schedule
    if med is None or sched is None or not med.active or not sched.active:
        return blocked(BlockReason.INACTIVE)
    if not med.confirmed_by_user:
        return blocked(BlockReason.UNCONFIRMED_MEDICATION)
    # Rule 4 — a physical compartment on this device, resolved now (handoff §13 slot mapping).
    if slot is None or not 0 <= slot < settings.num_slots:
        return blocked(BlockReason.NO_COMPARTMENT)
    # Rule 6 — same medication accessed too recently (handoff §4.2 duplicate prevention).
    if event.medication_id in recent:
        return blocked(BlockReason.TOO_SOON)
    return Assessment(event, slot, comp_id, None)


def display_slot(event: DoseEvent, slots: dict[int, tuple[int, int]]) -> int | None:
    """Slot to show/speak for an event.

    Open doses (SCHEDULED/DUE) show the slot they would be dispensed from *now*;
    doses that already touched hardware show the slot that was used (for a
    HARDWARE_ERROR under review that is the compartment that may be open).
    """
    resolved = slots.get(event.medication_id)
    current = resolved[0] if resolved is not None else None
    if event.status in _OPEN_STATUSES:
        return current if current is not None else event.slot_number
    return event.slot_number if event.slot_number is not None else current


def to_dose_info(event: DoseEvent, clock: Clock, slot: Any = ...) -> DoseInfo:
    """Build the cross-module ``DoseInfo`` (must be called while ``event`` is attached).

    ``slot`` overrides the slot (default: the snapshot stored on the event).
    """
    med = event.medication
    return DoseInfo(
        event_id=event.event_id,
        medication_id=event.medication_id,
        medication_name=med.name if med is not None else "unknown medication",
        strength=med.strength if med is not None else None,
        instructions=med.instructions_text if med is not None else None,
        slot=event.slot_number if slot is ... else slot,
        scheduled_at=event.scheduled_at,
        scheduled_local=clock.to_local(event.scheduled_at),
        status=event.status,
        dispensed_at=event.dispensed_at,
        confirmed_taken_at=event.confirmed_taken_at,
        needs_review=bool(event.needs_review),
        attempts=int(event.attempts or 0),
        hardware_result=event.hardware_result,
    )


# --------------------------------------------------------------------------- confirmation lookups


def find_confirmable(session: Session, now: datetime, settings: Settings) -> DoseEvent | None:
    """Most recent DISPENSED dose on this device with ``dispensed_at >= now - confirm_window``."""
    cutoff = now - timedelta(minutes=settings.confirm_window_minutes)
    return session.scalars(
        select(DoseEvent)
        .options(selectinload(DoseEvent.medication))
        .where(
            DoseEvent.device_id == settings.device_id,
            DoseEvent.status == DoseStatus.DISPENSED.value,
            DoseEvent.dispensed_at >= cutoff,
        )
        .order_by(DoseEvent.dispensed_at.desc(), DoseEvent.event_id.desc())
        .limit(1)
    ).first()


def latest_accessed(session: Session, now: datetime, settings: Settings) -> DoseEvent | None:
    """Latest DISPENSED/TAKEN dose on this device accessed or confirmed within the confirm window."""
    cutoff = now - timedelta(minutes=settings.confirm_window_minutes)
    rows = session.scalars(
        select(DoseEvent)
        .options(selectinload(DoseEvent.medication))
        .where(
            DoseEvent.device_id == settings.device_id,
            DoseEvent.status.in_(_ACCESSED),
            or_(DoseEvent.dispensed_at >= cutoff, DoseEvent.confirmed_taken_at >= cutoff),
        )
    ).all()
    if not rows:
        return None

    def key(ev: DoseEvent) -> tuple[datetime, int]:
        stamps = [t for t in (ev.dispensed_at, ev.confirmed_taken_at) if t is not None]
        return (max(stamps), ev.event_id)

    return max(rows, key=key)


# --------------------------------------------------------------------------- evaluation


def _events_query(settings: Settings):  # noqa: ANN202 - SQLAlchemy Select
    return select(DoseEvent).options(
        selectinload(DoseEvent.medication), selectinload(DoseEvent.schedule)
    ).where(DoseEvent.device_id == settings.device_id)


def _next_upcoming(session: Session, now: datetime, settings: Settings) -> DoseEvent | None:
    """Next not-yet-open dose (window starts after ``now``) of an active, confirmed medication."""
    opens_after = now + timedelta(minutes=settings.dose_early_minutes)
    return session.scalars(
        _events_query(settings)
        .join(Medication, DoseEvent.medication_id == Medication.medication_id)
        .join(Schedule, DoseEvent.schedule_id == Schedule.schedule_id)
        .where(
            DoseEvent.status.in_(_OPEN_STATUSES),
            DoseEvent.scheduled_at > opens_after,
            Medication.active.is_(True),
            Medication.confirmed_by_user.is_(True),
            Schedule.active.is_(True),
        )
        .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
        .limit(1)
    ).first()


def evaluate(session: Session, now: datetime, settings: Settings) -> SafetyDecision:
    """Decide what (if anything) may be dispensed at ``now``. Read-only."""
    slots = assigned_slots(session, settings)
    recent = recent_accesses(session, now, settings)
    late = timedelta(minutes=settings.dose_late_minutes)
    early = timedelta(minutes=settings.dose_early_minutes)

    # Rule 5 — at most one dispense in flight per device (ARCHITECTURE §1.5, handoff §4.2).
    in_progress = list(session.scalars(
        _events_query(settings)
        .where(DoseEvent.status == DoseStatus.DISPENSING.value)
        .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
    ).all())

    # Rule 1 — only events whose window contains now: scheduled_at in [now - late, now + early].
    in_window_events = session.scalars(
        _events_query(settings)
        .where(DoseEvent.scheduled_at >= now - late, DoseEvent.scheduled_at <= now + early)
        .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
    ).all()

    due: list[Assessment] = []
    blocked: list[Assessment] = [
        Assessment(ev, ev.slot_number, ev.compartment_id, BlockReason.IN_PROGRESS) for ev in in_progress
    ]
    accessed: list[DoseEvent] = []
    for ev in in_window_events:
        if ev.status == DoseStatus.DISPENSING.value:
            continue  # already listed above
        if ev.status in _ACCESSED:
            accessed.append(ev)
            continue
        if ev.status not in _OPEN_STATUSES and ev.status != DoseStatus.HARDWARE_ERROR.value:
            continue  # MISSED / CANCELLED are final
        a = assess_event(ev, settings, slots, recent)
        (due if a.eligible else blocked).append(a)

    # Selection — earliest scheduled_at, then lowest slot.
    due.sort(key=lambda a: (a.event.scheduled_at, a.slot if a.slot is not None else 1 << 30, a.event.event_id))

    decision = SafetyDecision(
        verdict=Verdict.NOTHING_DUE,
        now=now,
        due=due,
        accessed=accessed,
        blocked=blocked,
        awaiting=_awaiting(session, now, settings),
        in_progress=in_progress,
        next_upcoming=_next_upcoming(session, now, settings),
        slots=slots,
    )

    def pick(verdict: Verdict, a: Assessment | None, ev: DoseEvent | None = None,
             reason: BlockReason | None = None) -> SafetyDecision:
        decision.verdict = verdict
        if a is not None:
            decision.event, decision.slot, decision.compartment_id = a.event, a.slot, a.compartment_id
            decision.reason = a.reason if reason is None else reason
        elif ev is not None:
            decision.event, decision.slot = ev, display_slot(ev, slots)
            decision.reason = reason
        return decision

    if in_progress:
        # Rule 5 makes nothing eligible while the device is dispensing.
        decision.queued, decision.due = due, []
        return pick(Verdict.IN_PROGRESS, blocked[0], reason=BlockReason.IN_PROGRESS)
    if due:
        return pick(Verdict.ALLOW, due[0])
    review = [a for a in blocked if a.reason is BlockReason.NEEDS_REVIEW]
    if review:
        return pick(Verdict.BLOCKED, review[0])
    too_soon = [a for a in blocked if a.reason in _DUPLICATE_REASONS]
    if accessed:
        latest = max(accessed, key=lambda e: (_access_time(e), e.event_id))
        return pick(Verdict.DUPLICATE, None, latest)
    if too_soon:
        return pick(Verdict.DUPLICATE, too_soon[0])
    config = [a for a in blocked if a.reason in _CONFIG_REASONS]
    if config:
        return pick(Verdict.BLOCKED, config[0])
    log.debug("nothing due at %s", now.isoformat())
    return decision


def _access_time(ev: DoseEvent) -> datetime:
    stamps = [t for t in (ev.dispensed_at, ev.confirmed_taken_at) if t is not None]
    return max(stamps) if stamps else ev.scheduled_at


def _awaiting(session: Session, now: datetime, settings: Settings) -> list[DoseEvent]:
    cutoff = now - timedelta(minutes=settings.confirm_window_minutes)
    return list(session.scalars(
        _events_query(settings)
        .where(DoseEvent.status == DoseStatus.DISPENSED.value, DoseEvent.dispensed_at >= cutoff)
        .order_by(DoseEvent.dispensed_at.desc(), DoseEvent.event_id.desc())
    ).all())
