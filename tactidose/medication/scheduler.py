"""Schedules -> dose events (materialize) and time-driven transitions (DUE / MISSED).

A ``Schedule`` is a local wall-clock time ("HH:MM", device timezone) on every day
(DAILY) or on listed weekdays (WEEKLY). :meth:`Scheduler.materialize` turns it into
one ``DoseEvent`` per local date, converting local time to UTC with the clock's
zone (DST-correct: a time skipped by spring-forward fires one hour later, an
ambiguous fall-back time fires once, at its first occurrence).

Rules that keep the event log truthful:

* no backfill — an occurrence whose window closed before the schedule was
  created (or last redefined) is never generated, so new schedules do not
  fabricate missed doses;
* one dose per schedule per local day — if the schedule already has an
  accessed (or possibly accessed) event on that day, an edited time does not
  produce a second one;
* idempotent inserts (unique ``(schedule_id, scheduled_at)``; concurrent
  inserts are tolerated).

This module also owns the shared dose-transition helpers (compare-and-set
update, outbox + audit log in the same transaction, bus payloads) used by
``catalog`` and ``dispense``.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Collection, Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    ALL_DAYS,
    DoseEvent,
    DoseStatus,
    Frequency,
    LogCategory,
    Medication,
    Schedule,
)
from tactidose.db.outbox import enqueue_adherence
from tactidose.db.session import Database
from tactidose.medication.compartments import get_device, iso
from tactidose.medication.errors import NotFoundError, ValidationError
from tactidose.medication.safety import dispense_window, to_dose_info

log = logging.getLogger(__name__)

__all__ = [
    "Scheduler",
    "cas_transition",
    "dose_update_payload",
    "is_id",
    "occurrence_for",
    "parse_days",
    "parse_frequency",
    "parse_time_of_day",
    "publish_all",
    "record_dose_change",
    "schedule_days",
    "schedule_to_dict",
]

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_DAY_NAMES = {
    "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED", "THURSDAY": "THU",
    "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN",
}
#: Statuses that mean the compartment was (or may have been) opened for an event.
_DAY_CONSUMED = (DoseStatus.DISPENSING.value, DoseStatus.DISPENSED.value, DoseStatus.TAKEN.value)
_RECONCILE_STATUSES = (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value, DoseStatus.HARDWARE_ERROR.value)
_UPDATABLE_FIELDS = frozenset({"time_of_day", "frequency", "days_of_week", "active"})


# --------------------------------------------------------------------------- parsing


def is_id(value: object) -> bool:
    """True for a plain int primary key (``bool`` is rejected even though it is an int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_time_of_day(value: object) -> str:
    """Validate a 24h local time and normalise it to zero-padded ``HH:MM``."""
    if not isinstance(value, str):
        raise ValidationError("time_of_day must be a string like '08:00'.")
    m = _TIME_RE.match(value)
    if not m:
        raise ValidationError("time_of_day must be HH:MM (24-hour), e.g. '08:00'.")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValidationError("time_of_day must be a valid 24-hour time between 00:00 and 23:59.")
    return f"{hour:02d}:{minute:02d}"


def parse_frequency(value: object) -> str:
    if isinstance(value, Frequency):
        return value.value
    if not isinstance(value, str) or value.strip().upper() not in {f.value for f in Frequency}:
        raise ValidationError("frequency must be DAILY or WEEKLY.")
    return value.strip().upper()


def parse_days(value: object, frequency: str) -> list[str]:
    """Weekday codes in canonical MON..SUN order. DAILY always stores every day."""
    if frequency == Frequency.DAILY.value:
        return list(ALL_DAYS)
    if value is None:
        items: list[object] = []
    elif isinstance(value, str):
        items = [p for p in value.split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        raise ValidationError("days_of_week must be a list like ['MON', 'WED'].")
    days: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValidationError("days_of_week entries must be strings like 'MON'.")
        code = item.strip().upper()
        if not code:
            continue
        code = _DAY_NAMES.get(code, code)
        if code not in ALL_DAYS:
            raise ValidationError(f"Unknown day {item!r}; use MON, TUE, WED, THU, FRI, SAT or SUN.")
        days.add(code)
    if not days:
        raise ValidationError("A WEEKLY schedule needs at least one day of the week.")
    return [d for d in ALL_DAYS if d in days]


def schedule_days(sched: Schedule) -> list[str]:
    """Days a stored schedule fires on (DAILY = every day, whatever is stored)."""
    if sched.frequency == Frequency.DAILY.value:
        return list(ALL_DAYS)
    stored = {d.strip().upper() for d in (sched.days_of_week or "").split(",") if d.strip()}
    return [d for d in ALL_DAYS if d in stored]


def occurrence_for(sched: Schedule, local_date: date, clock: Clock) -> datetime | None:
    """Aware-UTC dose time of ``sched`` on ``local_date``, or None if it does not fire that day."""
    if ALL_DAYS[local_date.weekday()] not in schedule_days(sched):
        return None
    hh, mm = (int(x) for x in sched.time_of_day.split(":"))
    return clock.local_to_utc(datetime.combine(local_date, time(hh, mm)))


def schedule_to_dict(sched: Schedule) -> dict[str, Any]:
    """API.md ``Schedule`` shape (must be called while ``sched`` is attached)."""
    med = sched.medication
    return {
        "schedule_id": sched.schedule_id,
        "medication_id": sched.medication_id,
        "medication_name": med.name if med is not None else None,
        "time_of_day": sched.time_of_day,
        "frequency": sched.frequency,
        "days_of_week": schedule_days(sched),
        "active": bool(sched.active),
        "created_at": iso(sched.created_at),
    }


# --------------------------------------------------------------------------- dose transitions (shared)


def cas_transition(
    session: Session,
    event_id: int,
    expected: str | Collection[str],
    values: dict[str, Any],
    *,
    require_no_review: bool = False,
) -> DoseEvent | None:
    """Compare-and-set: ``UPDATE dose_events SET ... WHERE event_id=:id AND status IN :expected``.

    Returns the reloaded event if exactly one row changed, else None (someone
    else changed the event first — the caller must not proceed).
    """
    expected_values = [expected] if isinstance(expected, str) else list(expected)
    stmt = update(DoseEvent).where(DoseEvent.event_id == event_id, DoseEvent.status.in_(expected_values))
    if require_no_review:
        stmt = stmt.where(DoseEvent.needs_review.is_(False))
    result = session.execute(stmt.values(**values).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        return None
    return session.get(DoseEvent, event_id, populate_existing=True)


def record_dose_change(
    session: Session,
    event: DoseEvent,
    *,
    settings: Settings,
    clock: Clock,
    action: str,
    detail: dict[str, Any] | None = None,
    category: LogCategory = LogCategory.DOSE,
) -> None:
    """Outbox row + audit log for one status change, in the caller's transaction."""
    enqueue_adherence(session, event, salt=settings.analytics_salt.get_secret_value(), tz=clock.tz)
    log_event(
        session,
        event.device_id,
        category,
        action,
        {"status": event.status, **(detail or {})},
        event_id=event.event_id,
    )


def dose_update_payload(
    event: DoseEvent, clock: Clock, *, previous: str | None, action: str, slot: Any = ...
) -> dict[str, Any]:
    """``Topic.DOSE_UPDATED`` payload (built inside the session, published after commit)."""
    return {
        "event_id": event.event_id,
        "label": event.label,
        "status": event.status,
        "previous": previous,
        "change": action,
        "dose": to_dose_info(event, clock, slot=slot).to_dict(),
    }


def publish_all(bus: EventBus | None, topic: str, payloads: Iterable[dict[str, Any]]) -> None:
    if bus is None:
        return
    for payload in payloads:
        bus.publish(topic, payload)


# --------------------------------------------------------------------------- service


class Scheduler:
    def __init__(self, db: Database, clock: Clock, settings: Settings, bus: EventBus | None = None) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.bus = bus
        self._tick_lock = threading.Lock()

    # ------------------------------------------------------------------ periodic work
    def tick(self) -> int:
        """materialize + refresh. Returns the number of changes; never raises."""
        with self._tick_lock:
            total = 0
            try:
                total += self.materialize()
            except Exception:  # noqa: BLE001 - DB down etc.: log and retry next tick
                log.exception("scheduler: materialize failed")
            try:
                total += self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("scheduler: refresh failed")
            return total

    def materialize(self) -> int:
        """Create missing dose events for local dates (today-1) .. (today + ceil(horizon/24))."""
        now = self.clock.now()
        today = self.clock.to_local(now).date()
        horizon_end = now + timedelta(hours=self.settings.schedule_horizon_hours)
        late = timedelta(minutes=self.settings.dose_late_minutes)
        days_ahead = math.ceil(self.settings.schedule_horizon_hours / 24)
        dates = [today + timedelta(days=i) for i in range(-1, days_ahead + 1)]
        payloads: list[dict[str, Any]] = []
        with self.db.session() as s:
            dev = get_device(s, self.settings)
            if dev is None:
                log.debug("materialize: device %s not bootstrapped yet", self.settings.device_id)
                return 0
            schedules = s.scalars(
                select(Schedule)
                .join(Medication, Schedule.medication_id == Medication.medication_id)
                .options(selectinload(Schedule.medication))
                .where(
                    Schedule.active.is_(True),
                    Medication.active.is_(True),
                    Medication.confirmed_by_user.is_(True),
                    Medication.user_id == dev.user_id,
                )
                .order_by(Schedule.schedule_id)
            ).all()
            for sched in schedules:
                effective_from = sched.created_at or now
                wanted: list[tuple[date, datetime]] = []
                for d in dates:
                    at = occurrence_for(sched, d, self.clock)
                    if at is None:
                        continue  # day not listed (WEEKLY)
                    if at + late < effective_from:
                        continue  # never fabricate past doses for new/redefined schedules
                    if at > horizon_end:
                        continue
                    wanted.append((d, at))
                if wanted:
                    payloads.extend(self._insert_missing(s, sched, wanted, now))
        publish_all(self.bus, Topic.DOSE_UPDATED, payloads)
        if payloads:
            log.info("materialized %d dose event(s)", len(payloads))
        return len(payloads)

    def _insert_missing(
        self, s: Session, sched: Schedule, wanted: list[tuple[date, datetime]], now: datetime
    ) -> list[dict[str, Any]]:
        lo = wanted[0][1] - timedelta(days=1)
        hi = wanted[-1][1] + timedelta(days=1)
        existing = s.scalars(
            select(DoseEvent).where(
                DoseEvent.schedule_id == sched.schedule_id,
                DoseEvent.scheduled_at >= lo,
                DoseEvent.scheduled_at <= hi,
            )
        ).all()
        existing_times = {ev.scheduled_at for ev in existing}
        consumed_days = {
            self.clock.to_local(ev.scheduled_at).date()
            for ev in existing
            if ev.status in _DAY_CONSUMED
            or (ev.status == DoseStatus.HARDWARE_ERROR.value and ev.needs_review)
        }
        out: list[dict[str, Any]] = []
        for d, at in wanted:
            if at in existing_times:
                continue
            if d in consumed_days:
                # The schedule's time was edited after today's dose was accessed: one per day.
                continue
            ev = DoseEvent(
                schedule_id=sched.schedule_id,
                medication_id=sched.medication_id,
                user_id=sched.medication.user_id,
                device_id=self.settings.device_id,
                scheduled_at=at,
                status=DoseStatus.SCHEDULED.value,
            )
            try:
                with s.begin_nested():
                    s.add(ev)
                    s.flush()
            except IntegrityError:
                log.debug("dose event for schedule %s at %s created concurrently", sched.schedule_id, at)
                continue
            # Creation is not a status change: no outbox row until the event leaves SCHEDULED.
            log_event(
                s, self.settings.device_id, LogCategory.DOSE, "DOSE_MATERIALIZED",
                {"schedule_id": sched.schedule_id, "scheduled_at": at.isoformat()}, event_id=ev.event_id,
            )
            out.append(dose_update_payload(ev, self.clock, previous=None, action="created"))
        return out

    def refresh(self) -> int:
        """SCHEDULED -> DUE when the window opens; open/failed doses -> MISSED when it closes."""
        now = self.clock.now()
        opens_by = now + timedelta(minutes=self.settings.dose_early_minutes)
        payloads: list[dict[str, Any]] = []
        with self.db.session() as s:
            rows = s.scalars(
                select(DoseEvent)
                .options(selectinload(DoseEvent.medication))
                .where(
                    DoseEvent.device_id == self.settings.device_id,
                    DoseEvent.status.in_(_RECONCILE_STATUSES),
                    DoseEvent.scheduled_at <= opens_by,
                )
                .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
            ).all()
            for ev in rows:
                start, end = dispense_window(ev, self.settings)
                previous = ev.status
                if now > end:
                    if previous == DoseStatus.HARDWARE_ERROR.value and ev.needs_review:
                        continue  # uncertain outcome: stays locked for caregiver review
                    changed = cas_transition(
                        s, ev.event_id, previous,
                        {"status": DoseStatus.MISSED.value, "missed_at": now},
                        require_no_review=previous == DoseStatus.HARDWARE_ERROR.value,
                    )
                    action = "MISSED"
                elif previous == DoseStatus.SCHEDULED.value and now >= start:
                    changed = cas_transition(s, ev.event_id, previous, {"status": DoseStatus.DUE.value})
                    action = "DUE"
                else:
                    continue
                if changed is None:
                    continue  # changed concurrently (e.g. claimed for dispensing)
                record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                                   action=f"DOSE_{action}", detail={"previous": previous})
                payloads.append(dose_update_payload(changed, self.clock, previous=previous, action=action.lower()))
        publish_all(self.bus, Topic.DOSE_UPDATED, payloads)
        return len(payloads)

    # ------------------------------------------------------------------ schedule CRUD
    def list_schedules(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        """API.md ``Schedule`` dicts for this device's user (active only by default)."""
        with self.db.session() as s:
            q = (
                select(Schedule)
                .join(Medication, Schedule.medication_id == Medication.medication_id)
                .options(selectinload(Schedule.medication))
                .order_by(Schedule.time_of_day, Schedule.schedule_id)
            )
            dev = get_device(s, self.settings)
            if dev is not None:
                q = q.where(Medication.user_id == dev.user_id)
            if not include_inactive:
                q = q.where(Schedule.active.is_(True))
            return [schedule_to_dict(sc) for sc in s.scalars(q).all()]

    def get_schedule(self, schedule_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            sched = s.get(Schedule, schedule_id)
            if sched is None:
                raise NotFoundError(f"Schedule {schedule_id} not found.")
            return schedule_to_dict(sched)

    def create_schedule(
        self,
        medication_id: int,
        time_of_day: str,
        frequency: str = "DAILY",
        days_of_week: Iterable[str] | str | None = None,
    ) -> dict[str, Any]:
        tod = parse_time_of_day(time_of_day)
        freq = parse_frequency(frequency)
        days = parse_days(days_of_week, freq)
        now = self.clock.now()
        with self.db.session() as s:
            med = s.get(Medication, medication_id) if is_id(medication_id) else None
            if med is None:
                raise NotFoundError(f"Medication {medication_id} not found.")
            self._require_schedulable(s, med)
            sched = Schedule(
                medication_id=med.medication_id,
                time_of_day=tod,
                frequency=freq,
                days_of_week=",".join(days),
                active=True,
                created_at=now,
                updated_at=now,
            )
            s.add(sched)
            s.flush()
            log_event(s, self.settings.device_id, LogCategory.ADMIN, "SCHEDULE_CREATED",
                      {"schedule_id": sched.schedule_id, "medication_id": med.medication_id,
                       "time_of_day": tod, "frequency": freq, "days_of_week": days})
            out = schedule_to_dict(sched)
        self._publish_data(out["schedule_id"])
        self.tick()
        return out

    def update_schedule(self, schedule_id: int, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValidationError(f"Unknown schedule field(s): {', '.join(sorted(unknown))}.")
        if "active" in fields and not isinstance(fields["active"], bool):
            raise ValidationError("active must be true or false.")
        now = self.clock.now()
        dose_payloads: list[dict[str, Any]] = []
        with self.db.session() as s:
            sched = s.get(Schedule, schedule_id) if is_id(schedule_id) else None
            if sched is None:
                raise NotFoundError(f"Schedule {schedule_id} not found.")
            tod = parse_time_of_day(fields["time_of_day"]) if "time_of_day" in fields else sched.time_of_day
            freq = parse_frequency(fields["frequency"]) if "frequency" in fields else sched.frequency
            days = parse_days(fields["days_of_week"] if "days_of_week" in fields else schedule_days(sched), freq)
            active = fields.get("active", bool(sched.active))
            if active:
                # Deactivating is always allowed (fail safe); anything that keeps it active is not.
                self._require_schedulable(s, sched.medication)
            redefined = (tod, freq, days) != (sched.time_of_day, sched.frequency, schedule_days(sched))
            reactivated = active and not sched.active
            if not (redefined or reactivated or active != bool(sched.active)):
                return schedule_to_dict(sched)
            sched.time_of_day, sched.frequency, sched.days_of_week = tod, freq, ",".join(days)
            sched.active = active
            sched.updated_at = now
            if active and (redefined or reactivated):
                # The no-backfill rule counts from the moment the current definition took effect.
                sched.created_at = now
            s.flush()
            dose_payloads = self._reconcile_events(s, sched, now)
            log_event(s, self.settings.device_id, LogCategory.ADMIN,
                      "SCHEDULE_UPDATED" if active else "SCHEDULE_DEACTIVATED",
                      {"schedule_id": sched.schedule_id, "time_of_day": tod, "frequency": freq,
                       "days_of_week": days, "active": active, "events_changed": len(dose_payloads)})
            out = schedule_to_dict(sched)
        publish_all(self.bus, Topic.DOSE_UPDATED, dose_payloads)
        self._publish_data(schedule_id)
        self.tick()
        return out

    def deactivate_schedule(self, schedule_id: int) -> None:
        self.update_schedule(schedule_id, active=False)

    # ------------------------------------------------------------------ internals
    def _require_schedulable(self, s: Session, med: Medication | None) -> None:
        if med is None:
            raise NotFoundError("Medication not found.")
        if not med.active:
            raise ValidationError("This medication is archived; it cannot be scheduled.")
        if not med.confirmed_by_user:
            raise ValidationError("Only medications confirmed by a person can be scheduled.")
        dev = get_device(s, self.settings)
        if dev is not None and med.user_id != dev.user_id:
            raise ValidationError("That medication belongs to a different user.")

    def _is_valid_occurrence(self, sched: Schedule, at: datetime) -> bool:
        local_date = self.clock.to_local(at).date()
        return any(occurrence_for(sched, d, self.clock) == at for d in (local_date - timedelta(days=1), local_date))

    def _reconcile_events(self, s: Session, sched: Schedule, now: datetime) -> list[dict[str, Any]]:
        """After a schedule edit: drop/cancel open events that no longer match the schedule.

        Events still matching the (active) schedule are kept. Stale events whose window
        has not opened are deleted (they never left SCHEDULED, so nothing was reported);
        stale in-window events are CANCELLED. Accessed, in-flight, closed-window and
        review-locked events are never touched.
        """
        payloads: list[dict[str, Any]] = []
        rows = s.scalars(
            select(DoseEvent)
            .options(selectinload(DoseEvent.medication))
            .where(DoseEvent.schedule_id == sched.schedule_id, DoseEvent.status.in_(_RECONCILE_STATUSES))
            .order_by(DoseEvent.scheduled_at)
        ).all()
        for ev in rows:
            start, end = dispense_window(ev, self.settings)
            if end < now:
                continue  # refresh() turns it into MISSED
            if ev.status == DoseStatus.HARDWARE_ERROR.value and ev.needs_review:
                continue
            if sched.active and self._is_valid_occurrence(sched, ev.scheduled_at):
                continue
            if ev.status == DoseStatus.SCHEDULED.value and start > now:
                payload = dose_update_payload(ev, self.clock, previous=ev.status, action="deleted")
                payload["status"] = None
                log_event(s, self.settings.device_id, LogCategory.DOSE, "DOSE_UNSCHEDULED",
                          {"schedule_id": sched.schedule_id}, event_id=ev.event_id)
                s.delete(ev)
                payloads.append(payload)
                continue
            previous = ev.status
            changed = cas_transition(
                s, ev.event_id, previous,
                {"status": DoseStatus.CANCELLED.value, "cancelled_at": now,
                 "review_note": "schedule changed"},
            )
            if changed is None:
                continue
            record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                               action="DOSE_CANCELLED", detail={"previous": previous, "why": "schedule changed"})
            payloads.append(dose_update_payload(changed, self.clock, previous=previous, action="cancelled"))
        s.flush()
        return payloads

    def _publish_data(self, schedule_id: int | None) -> None:
        if self.bus is not None:
            self.bus.publish(Topic.DATA_CHANGED, {"entity": "schedule", "id": schedule_id})
