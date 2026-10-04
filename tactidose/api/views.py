"""Read-only lookups the HTTP layer needs for shapes and least-privilege checks.

Everything here reads the frozen v2 tables (``db/models.py``) directly and never writes:

* views: API.md ``User``, the patient profile in ``/api/auth/me``, ``CarePatient`` extras
  (unread alerts, 7-day adherence);
* ownership: which patient a drop / dose event / schedule / medication / scan /
  conversation belongs to, and which patient the configured device dispenses for. Routes
  under ``/api/patients/{pid}/…`` use these so an id from another patient is a 404 even when
  the caller may access ``pid``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tactidose.db.models import (
    CareLink,
    Conversation,
    Device,
    DoseEvent,
    DoseStatus,
    LabelScan,
    Medication,
    Notification,
    NotificationKind,
    PillDrop,
    Schedule,
    User,
)

log = logging.getLogger(__name__)

__all__ = [
    "ALERT_KINDS",
    "adherence",
    "care_links",
    "device_owner_id",
    "owner_of",
    "patient_profile",
    "unread_alerts",
    "user_view",
]

#: Notification kinds counted as "alerts" for ``CarePatient.unread_alerts``.
ALERT_KINDS = frozenset({
    NotificationKind.DROP_FAILED.value,
    NotificationKind.DROP_UNCERTAIN.value,
    NotificationKind.DEVICE_ALERT.value,
    NotificationKind.LOW_STOCK.value,
    NotificationKind.EMPTY.value,
    NotificationKind.MISSED_DOSE.value,
})

_DISPENSED = (DoseStatus.DISPENSED.value, DoseStatus.TAKEN.value)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def user_view(db: Any, user_id: int) -> dict[str, Any] | None:
    """API.md ``User``: ``{user_id, email, display_name, role, phone, created_at}``."""
    with db.session() as s:
        u = s.get(User, user_id)
        if u is None:
            return None
        return {
            "user_id": u.user_id,
            "email": u.email,
            "display_name": u.display_name,
            "role": u.role,
            "phone": u.phone,
            "created_at": _iso(u.created_at),
        }


def device_owner_id(db: Any, settings: Any) -> int | None:
    """The patient the configured device dispenses for (``devices.user_id``), if bound."""
    with db.session() as s:
        dev = s.get(Device, settings.device_id)
        return int(dev.user_id) if dev is not None and dev.user_id is not None else None


def patient_profile(db: Any, patient_id: int) -> dict[str, Any]:
    """``{patient_id, link_code, device_id}`` (``/api/auth/me`` for patients)."""
    with db.session() as s:
        u = s.get(User, patient_id)
        link_code = u.link_code if u is not None else None
        device_id = s.scalars(
            select(Device.device_id).where(Device.user_id == patient_id).order_by(Device.device_id).limit(1)
        ).first()
    return {"patient_id": patient_id, "link_code": link_code, "device_id": device_id}


def care_links(db: Any, caregiver_id: int) -> list[dict[str, Any]]:
    """``[{patient_id, display_name, relationship}]`` for one caregiver, by name."""
    with db.session() as s:
        rows = s.execute(
            select(CareLink.patient_id, CareLink.relationship_kind, User.display_name)
            .join(User, User.user_id == CareLink.patient_id)
            .where(CareLink.caregiver_id == caregiver_id)
            .order_by(func.lower(User.display_name), CareLink.patient_id)
        ).all()
    return [{"patient_id": pid, "display_name": name, "relationship": rel} for pid, rel, name in rows]


def unread_alerts(db: Any, user_id: int, patient_id: int) -> int:
    with db.session() as s:
        return int(s.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user_id,
                Notification.patient_id == patient_id,
                Notification.read_at.is_(None),
                Notification.kind.in_(sorted(ALERT_KINDS)),
            )
        ) or 0)


def adherence(db: Any, now: datetime, patient_id: int, *, days: int = 7) -> float | None:
    """dispensed ÷ (dispensed + missed) for scheduled doses in ``[now - days, now]`` (None = no data)."""
    since = now - timedelta(days=days)
    with db.session() as s:
        rows = s.execute(
            select(DoseEvent.status, func.count())
            .where(
                DoseEvent.user_id == patient_id,
                DoseEvent.scheduled_at >= since,
                DoseEvent.scheduled_at <= now,
            )
            .group_by(DoseEvent.status)
        ).all()
    counts = {status: int(n) for status, n in rows}
    dispensed = sum(counts.get(st, 0) for st in _DISPENSED)
    missed = counts.get(DoseStatus.MISSED.value, 0)
    if dispensed + missed == 0:
        return None
    return round(dispensed / (dispensed + missed), 4)


def owner_of(db: Any, kind: str, record_id: int) -> int | None:
    """Patient id owning ``record_id`` of ``kind`` (drop | dose | schedule | medication | scan |
    conversation), or None when the record does not exist."""
    with db.session() as s:
        if kind == "drop":
            return s.scalar(select(PillDrop.patient_id).where(PillDrop.drop_id == record_id))
        if kind == "dose":
            return s.scalar(select(DoseEvent.user_id).where(DoseEvent.event_id == record_id))
        if kind == "schedule":
            return s.scalar(
                select(Medication.user_id)
                .join(Schedule, Schedule.medication_id == Medication.medication_id)
                .where(Schedule.schedule_id == record_id)
            )
        if kind == "medication":
            return s.scalar(select(Medication.user_id).where(Medication.medication_id == record_id))
        if kind == "scan":
            return s.scalar(select(LabelScan.user_id).where(LabelScan.scan_id == record_id))
        if kind == "conversation":
            return s.scalar(select(Conversation.patient_id).where(Conversation.conversation_id == record_id))
    raise ValueError(f"unknown record kind {kind!r}")
