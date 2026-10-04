"""Report data gathering: one patient, one period, read-only.

:func:`gather_report_data` reads the v2 tables directly (users, devices, compartments,
medications, schedules, dose_events, pill_drops, notifications, conversations,
conversation_messages) inside one read-only session and returns plain frozen
dataclasses, so statistics, narrative and PDF rendering never touch the ORM (and can be
unit-tested with hand-built data).

Scope and privacy: every query is filtered by the patient id. Medication names come
only from the patient's own medications (or the name snapshot on their own drop rows);
anything else is shown as unknown rather than looked up.

Period: ``[period_start, period_end]`` (aware UTC, both inclusive), normally
``[now - days, now]``. Dose events scheduled up to ``dose_early_minutes`` *after* the end
are included when they were already satisfied early (DISPENSED/TAKEN), so a dose dropped
at 07:50 for 08:00 is not lost from a report generated at 07:55.

Also home to the small formatting helpers shared by the narrative, PDF and email
(12-hour local times, "Mon 5 Oct 2026" dates, whole-number percentages).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from sqlalchemy import select

from tactidose.config import Settings
from tactidose.core.clock import Clock
from tactidose.db.models import (
    ALL_DAYS,
    CareLink,
    Compartment,
    Conversation,
    ConversationMessage,
    Device,
    DoseEvent,
    DoseStatus,
    Frequency,
    Medication,
    Notification,
    NotificationKind,
    PillDrop,
    Schedule,
    User,
)
from tactidose.db.session import Database

log = logging.getLogger(__name__)

__all__ = [
    "ALERT_KINDS",
    "MAX_ROWS",
    "AlertRow",
    "ContainerRow",
    "ConversationRow",
    "DeviceRow",
    "DoseRow",
    "DropRow",
    "MedicationRow",
    "MessageRow",
    "PersonRow",
    "ReportData",
    "ScheduleRow",
    "doctor_recipients",
    "fmt_date",
    "fmt_datetime",
    "fmt_day",
    "fmt_pct",
    "fmt_time",
    "gather_report_data",
]

#: Notification kinds summarised in reports (drop failures/uncertain come from pill_drops).
ALERT_KINDS: tuple[str, ...] = (
    NotificationKind.LOW_STOCK.value,
    NotificationKind.EMPTY.value,
    NotificationKind.MISSED_DOSE.value,
    NotificationKind.DEVICE_ALERT.value,
)
#: Safety bound per collection (newest rows kept); reported in ``ReportData.truncated``.
MAX_ROWS = 5000
#: Notifications are stored once per recipient: identical rows this close together are one alert.
_ALERT_DEDUPE_S = 5.0
_SATISFIED = (DoseStatus.DISPENSED.value, DoseStatus.TAKEN.value)


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True)
class PersonRow:
    user_id: int
    display_name: str
    role: str
    email: str | None = None


@dataclass(frozen=True)
class DeviceRow:
    device_id: str
    name: str
    num_slots: int
    manual_cooldown_minutes: int
    auto_drop_enabled: bool


@dataclass(frozen=True)
class ContainerRow:
    device_id: str
    compartment_id: int
    slot: int
    medication_id: int | None
    medication_name: str | None
    strength: str | None
    pill_count: int
    capacity: int
    low_stock_threshold: int
    active: bool = True
    loaded_at: datetime | None = None

    @property
    def container_number(self) -> int:
        return self.slot + 1

    @property
    def empty(self) -> bool:
        return self.pill_count <= 0

    @property
    def low_stock(self) -> bool:
        return 0 < self.pill_count <= self.low_stock_threshold


@dataclass(frozen=True)
class MedicationRow:
    medication_id: int
    name: str
    strength: str | None = None
    active: bool = True
    confirmed: bool = True


@dataclass(frozen=True)
class ScheduleRow:
    schedule_id: int
    medication_id: int
    time_of_day: str
    frequency: str = Frequency.DAILY.value
    days: tuple[str, ...] = ALL_DAYS
    active: bool = True

    @property
    def doses_per_day(self) -> float:
        """Average doses per day (0 for an inactive schedule)."""
        if not self.active:
            return 0.0
        if self.frequency == Frequency.DAILY.value:
            return 1.0
        return len(self.days) / 7.0


@dataclass(frozen=True)
class DoseRow:
    event_id: int
    schedule_id: int
    medication_id: int
    medication_name: str
    scheduled_at: datetime
    status: str
    slot: int | None = None
    drop_id: int | None = None
    dispensed_at: datetime | None = None
    dispense_source: str | None = None
    confirmed_taken_at: datetime | None = None
    missed_at: datetime | None = None
    needs_review: bool = False
    attempts: int = 0
    hardware_result: str | None = None

    @property
    def container_number(self) -> int | None:
        return None if self.slot is None else self.slot + 1


@dataclass(frozen=True)
class DropRow:
    drop_id: int
    requested_at: datetime
    source: str
    status: str
    completed_at: datetime | None = None
    slot: int | None = None
    medication_id: int | None = None
    medication_name: str | None = None
    reason: str | None = None
    hardware_result: str | None = None
    pill_count_before: int | None = None
    pill_count_after: int | None = None
    dose_event_id: int | None = None
    conversation_id: int | None = None
    requested_by_user_id: int | None = None
    needs_review: bool = False
    review_note: str | None = None

    @property
    def container_number(self) -> int | None:
        return None if self.slot is None else self.slot + 1


@dataclass(frozen=True)
class AlertRow:
    notification_id: int
    kind: str
    title: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationRow:
    conversation_id: int
    channel: str
    started_at: datetime
    last_message_at: datetime
    title: str | None = None


@dataclass(frozen=True)
class MessageRow:
    message_id: int
    conversation_id: int
    role: str                      # user | assistant | tool
    content: str
    created_at: datetime
    input_mode: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    model: str | None = None


@dataclass(frozen=True)
class ReportData:
    """Everything a report shows, detached from the database."""

    patient: PersonRow
    days: int
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    timezone: str
    tz: tzinfo | None = None                 # None = system local zone (Clock semantics)
    creator: PersonRow | None = None
    devices: tuple[DeviceRow, ...] = ()
    containers: tuple[ContainerRow, ...] = ()
    medications: tuple[MedicationRow, ...] = ()
    schedules: tuple[ScheduleRow, ...] = ()
    doses: tuple[DoseRow, ...] = ()
    drops: tuple[DropRow, ...] = ()
    alerts: tuple[AlertRow, ...] = ()
    conversations: tuple[ConversationRow, ...] = ()
    messages: tuple[MessageRow, ...] = ()
    #: Collections that hit :data:`MAX_ROWS` (older rows were left out).
    truncated: tuple[str, ...] = ()

    @property
    def device(self) -> DeviceRow | None:
        return self.devices[0] if self.devices else None

    def local(self, dt: datetime) -> datetime:
        """Aware datetime -> the device's local time (same rule as ``Clock.to_local``)."""
        return dt.astimezone(self.tz) if self.tz is not None else dt.astimezone()

    def local_dates(self) -> list[date]:
        """Every local calendar date the period touches, oldest first."""
        first = self.local(self.period_start).date()
        last = self.local(self.period_end).date()
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def medication(self, medication_id: int | None) -> MedicationRow | None:
        if medication_id is None:
            return None
        for med in self.medications:
            if med.medication_id == medication_id:
                return med
        return None


# --------------------------------------------------------------------------- formatting


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def fmt_time(local: datetime | time) -> str:
    """``7:05 AM`` (12-hour clock, no leading zero; portable — no ``%-I``)."""
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def fmt_date(d: date | datetime) -> str:
    """``5 Oct 2026``."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def fmt_day(d: date | datetime) -> str:
    """``Mon 5 Oct``."""
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def fmt_datetime(local: datetime, *, weekday: bool = True, year: bool = False) -> str:
    """``Mon 5 Oct, 7:40 AM`` (optionally with the year)."""
    day = fmt_day(local) if weekday else f"{local.day} {_MONTHS[local.month - 1]}"
    if year:
        day = f"{day} {local.year}"
    return f"{day}, {fmt_time(local)}"


def fmt_pct(rate: float | None) -> str:
    """Whole percent; never shows 100% unless complete or 0% unless nothing. ``None`` -> ``–``."""
    if rate is None:
        return "–"
    pct = int(rate * 100 + 0.5)
    if rate < 1 and pct >= 100:
        pct = 99
    if rate > 0 and pct <= 0:
        pct = 1
    return f"{pct}%"


# --------------------------------------------------------------------------- gathering


def gather_report_data(
    db: Database,
    clock: Clock,
    settings: Settings,
    *,
    patient_id: int,
    days: int,
    period_start: datetime,
    period_end: datetime,
    created_by_user_id: int | None = None,
    generated_at: datetime | None = None,
) -> ReportData:
    """Load the report data for ``patient_id`` (raises ``LookupError`` if there is no such user).

    Database errors propagate: a report must never be built from made-up zeros.
    """
    early = timedelta(minutes=settings.dose_early_minutes)
    truncated: list[str] = []
    with db.session() as s:
        user = s.get(User, patient_id)
        if user is None:
            raise LookupError(f"patient {patient_id} not found")
        patient = _person(user)
        creator = None
        if created_by_user_id is not None:
            cu = s.get(User, created_by_user_id)
            creator = _person(cu) if cu is not None else None

        devices = s.scalars(
            select(Device).where(Device.user_id == patient_id).order_by(Device.created_at, Device.device_id)
        ).all()
        device_ids = [d.device_id for d in devices]

        meds = s.scalars(
            select(Medication).where(Medication.user_id == patient_id).order_by(Medication.medication_id)
        ).all()
        med_by_id = {m.medication_id: m for m in meds}

        comps = []
        if device_ids:
            comps = s.scalars(
                select(Compartment)
                .where(Compartment.device_id.in_(device_ids))
                .order_by(Compartment.device_id, Compartment.slot_number)
            ).all()

        scheds = []
        if med_by_id:
            scheds = s.scalars(
                select(Schedule)
                .where(Schedule.medication_id.in_(list(med_by_id)))
                .order_by(Schedule.time_of_day, Schedule.schedule_id)
            ).all()

        dose_rows = _newest(
            s,
            select(DoseEvent).where(
                DoseEvent.user_id == patient_id,
                DoseEvent.scheduled_at >= period_start,
                DoseEvent.scheduled_at <= period_end + early,
            ),
            (DoseEvent.scheduled_at, DoseEvent.event_id),
            "doses",
            truncated,
        )
        drop_rows = _newest(
            s,
            select(PillDrop).where(
                PillDrop.patient_id == patient_id,
                PillDrop.requested_at >= period_start,
                PillDrop.requested_at <= period_end,
            ),
            (PillDrop.requested_at, PillDrop.drop_id),
            "drops",
            truncated,
        )
        notif_rows = _newest(
            s,
            select(Notification).where(
                Notification.patient_id == patient_id,
                Notification.kind.in_(ALERT_KINDS),
                Notification.created_at >= period_start,
                Notification.created_at <= period_end,
            ),
            (Notification.created_at, Notification.notification_id),
            "alerts",
            truncated,
        )
        msg_rows = _newest(
            s,
            select(ConversationMessage).where(
                ConversationMessage.patient_id == patient_id,
                ConversationMessage.created_at >= period_start,
                ConversationMessage.created_at <= period_end,
            ),
            (ConversationMessage.created_at, ConversationMessage.message_id),
            "messages",
            truncated,
        )
        conv_ids = sorted({m.conversation_id for m in msg_rows})
        convs = []
        if conv_ids:
            convs = s.scalars(
                select(Conversation)
                .where(Conversation.conversation_id.in_(conv_ids), Conversation.patient_id == patient_id)
                .order_by(Conversation.started_at, Conversation.conversation_id)
            ).all()

        data = ReportData(
            patient=patient,
            days=days,
            period_start=period_start,
            period_end=period_end,
            generated_at=generated_at or clock.now(),
            timezone=clock.tz_name,
            tz=clock.tz,
            creator=creator,
            devices=tuple(
                DeviceRow(
                    device_id=d.device_id,
                    name=d.name or d.device_id,
                    num_slots=int(d.num_slots or 0),
                    manual_cooldown_minutes=int(d.manual_cooldown_minutes or 0),
                    auto_drop_enabled=bool(d.auto_drop_enabled),
                )
                for d in devices
            ),
            containers=tuple(_container(c, med_by_id) for c in comps),
            medications=tuple(
                MedicationRow(
                    medication_id=m.medication_id,
                    name=m.name,
                    strength=m.strength,
                    active=bool(m.active),
                    confirmed=bool(m.confirmed_by_user),
                )
                for m in meds
            ),
            schedules=tuple(_schedule(sc) for sc in scheds),
            doses=tuple(
                _dose(ev, med_by_id) for ev in dose_rows
                if ev.scheduled_at <= period_end or ev.status in _SATISFIED
            ),
            drops=tuple(_drop(d, med_by_id) for d in drop_rows),
            alerts=tuple(_dedupe_alerts(notif_rows)),
            conversations=tuple(
                ConversationRow(
                    conversation_id=c.conversation_id,
                    channel=c.channel or "text",
                    title=c.title,
                    started_at=c.started_at,
                    last_message_at=c.last_message_at,
                )
                for c in convs
            ),
            messages=tuple(
                MessageRow(
                    message_id=m.message_id,
                    conversation_id=m.conversation_id,
                    role=m.role,
                    content=m.content or "",
                    created_at=m.created_at,
                    input_mode=m.input_mode,
                    tool_name=m.tool_name,
                    tool_args=m.tool_args if isinstance(m.tool_args, dict) else None,
                    tool_result=m.tool_result if isinstance(m.tool_result, dict) else None,
                    model=m.model,
                )
                for m in msg_rows
            ),
            truncated=tuple(truncated),
        )
    if truncated:
        log.warning("report data for patient %s truncated to the newest %d rows: %s",
                    patient_id, MAX_ROWS, ", ".join(truncated))
    return data


def doctor_recipients(db: Database, patient_id: int) -> list[PersonRow]:
    """Active doctor accounts linked to the patient (``care_links.relationship_kind == 'doctor'``)
    that have an email address, in link order."""
    with db.session() as s:
        rows = s.execute(
            select(User)
            .join(CareLink, CareLink.caregiver_id == User.user_id)
            .where(CareLink.patient_id == patient_id, CareLink.relationship_kind == "doctor")
            .order_by(CareLink.created_at, CareLink.link_id)
        ).scalars().all()
        return [_person(u) for u in rows if u.is_active and (u.email or "").strip()]


# --------------------------------------------------------------------------- helpers


def _newest(s: Any, stmt: Any, order: tuple[Any, Any], name: str, truncated: list[str]) -> list[Any]:
    """Rows of ``stmt`` oldest first, keeping only the newest :data:`MAX_ROWS`."""
    rows = list(s.scalars(stmt.order_by(order[0].desc(), order[1].desc()).limit(MAX_ROWS + 1)).all())
    if len(rows) > MAX_ROWS:
        truncated.append(name)
        rows = rows[:MAX_ROWS]
    rows.reverse()
    return rows


def _person(u: User) -> PersonRow:
    return PersonRow(user_id=u.user_id, display_name=u.display_name or f"User {u.user_id}",
                     role=u.role or "", email=u.email)


def _med_name(med_id: int | None, med_by_id: dict[int, Medication]) -> str | None:
    med = med_by_id.get(med_id) if med_id is not None else None
    return med.name if med is not None else None


def _container(c: Compartment, med_by_id: dict[int, Medication]) -> ContainerRow:
    med = med_by_id.get(c.medication_id) if c.medication_id is not None else None
    return ContainerRow(
        device_id=c.device_id,
        compartment_id=c.compartment_id,
        slot=int(c.slot_number),
        medication_id=c.medication_id if med is not None else None,
        medication_name=med.name if med is not None else None,
        strength=med.strength if med is not None else None,
        pill_count=int(c.pill_count or 0),
        capacity=int(c.capacity or 0),
        low_stock_threshold=int(c.low_stock_threshold or 0),
        active=bool(c.active),
        loaded_at=c.loaded_at,
    )


def _schedule(sc: Schedule) -> ScheduleRow:
    if sc.frequency == Frequency.DAILY.value:
        days = ALL_DAYS
    else:
        stored = {d.strip().upper() for d in (sc.days_of_week or "").split(",") if d.strip()}
        days = tuple(d for d in ALL_DAYS if d in stored)
    return ScheduleRow(
        schedule_id=sc.schedule_id,
        medication_id=sc.medication_id,
        time_of_day=sc.time_of_day,
        frequency=sc.frequency or Frequency.DAILY.value,
        days=days,
        active=bool(sc.active),
    )


def _dose(ev: DoseEvent, med_by_id: dict[int, Medication]) -> DoseRow:
    return DoseRow(
        event_id=ev.event_id,
        schedule_id=ev.schedule_id,
        medication_id=ev.medication_id,
        medication_name=_med_name(ev.medication_id, med_by_id) or f"Medication {ev.medication_id}",
        scheduled_at=ev.scheduled_at,
        status=ev.status,
        slot=ev.slot_number,
        drop_id=ev.drop_id,
        dispensed_at=ev.dispensed_at,
        dispense_source=ev.dispense_source,
        confirmed_taken_at=ev.confirmed_taken_at,
        missed_at=ev.missed_at,
        needs_review=bool(ev.needs_review),
        attempts=int(ev.attempts or 0),
        hardware_result=ev.hardware_result,
    )


def _drop(d: PillDrop, med_by_id: dict[int, Medication]) -> DropRow:
    return DropRow(
        drop_id=d.drop_id,
        requested_at=d.requested_at,
        completed_at=d.completed_at,
        source=d.source,
        status=d.status,
        slot=d.slot_number,
        medication_id=d.medication_id,
        medication_name=d.medication_name or _med_name(d.medication_id, med_by_id),
        reason=d.reason,
        hardware_result=d.hardware_result,
        pill_count_before=d.pill_count_before,
        pill_count_after=d.pill_count_after,
        dose_event_id=d.dose_event_id,
        conversation_id=d.conversation_id,
        requested_by_user_id=d.requested_by_user_id,
        needs_review=bool(d.needs_review),
        review_note=d.review_note,
    )


def _dedupe_alerts(rows: list[Notification]) -> list[AlertRow]:
    """One alert per event: rows with the same kind/title/body/data within a few seconds are copies
    for different recipients."""
    out: list[AlertRow] = []
    last_seen: dict[tuple[str, str, str, str], datetime] = {}
    for n in rows:
        key = (n.kind, n.title or "", n.body or "", repr(sorted((n.data or {}).items())))
        prev = last_seen.get(key)
        last_seen[key] = n.created_at
        if prev is not None and abs((n.created_at - prev).total_seconds()) <= _ALERT_DEDUPE_S:
            continue
        out.append(AlertRow(notification_id=n.notification_id, kind=n.kind, title=n.title or "",
                            body=n.body or "", created_at=n.created_at))
    return out
