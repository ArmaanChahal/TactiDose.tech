"""Deterministic dose helpers shared by ``drops`` and ``scheduler`` (v2).

The v2 drop rules themselves (device, target, pending review, global cooldown, scheduled
satisfaction, inventory, one drop at a time, hardware readiness) live in
:mod:`tactidose.medication.drops`. This module keeps the small, pure pieces both modules need:

* the dose **window** ``[scheduled_at - dose_early_minutes, scheduled_at + dose_late_minutes]``
  (both ends inclusive): a scheduled dose may be dropped inside it, an earlier drop of the same
  medication from its start onward satisfies it, and after its end an undropped dose is MISSED;
* which dose statuses are still **open** (may still be dropped);
* the slot to **display** for a dose (resolved from the current container assignment for open
  doses, the slot actually used for doses that touched the hardware);
* the cross-module ``DoseInfo`` builder and the "most recent dropped dose" lookup used to
  confirm a dose as taken.

Everything here is read-only: no writes, no hardware, no clock of its own (v1's consent/gate
eligibility engine was retired together with ``dispense.py``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tactidose.config import Settings
from tactidose.core.clock import Clock
from tactidose.core.interfaces import DoseInfo
from tactidose.db.models import DoseEvent, DoseStatus, PillDrop

log = logging.getLogger(__name__)

__all__ = [
    "OPEN_STATUSES",
    "SCHEDULED_OR_DUE",
    "dispense_window",
    "display_slot",
    "drop_time",
    "find_confirmable",
    "in_window",
    "is_open",
    "to_dose_info",
]

SCHEDULED_OR_DUE = (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value)
#: Statuses a dose may still be dropped from (HARDWARE_ERROR only without ``needs_review``).
OPEN_STATUSES = (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value, DoseStatus.HARDWARE_ERROR.value)


def dispense_window(event_or_time: DoseEvent | datetime, settings: Settings) -> tuple[datetime, datetime]:
    """``(start, end)`` of the dose window."""
    at = event_or_time.scheduled_at if isinstance(event_or_time, DoseEvent) else event_or_time
    return (
        at - timedelta(minutes=settings.dose_early_minutes),
        at + timedelta(minutes=settings.dose_late_minutes),
    )


def in_window(event_or_time: DoseEvent | datetime, now: datetime, settings: Settings) -> bool:
    """``start <= now <= end`` (both ends inclusive)."""
    start, end = dispense_window(event_or_time, settings)
    return start <= now <= end


def is_open(event: DoseEvent) -> bool:
    """SCHEDULED / DUE, or HARDWARE_ERROR without ``needs_review`` (a retry is still allowed).

    A dose flagged ``needs_review`` is never open: its last drop outcome was uncertain, so a
    doctor/family member must resolve it first (fail closed)."""
    if event.needs_review:
        return False
    return event.status in OPEN_STATUSES


def drop_time(drop: PillDrop) -> datetime:
    """When a drop happened: completion time, or the request time while still in flight."""
    return drop.completed_at or drop.requested_at


def display_slot(event: DoseEvent, slots: dict[int, tuple[int, int]]) -> int | None:
    """Slot to show/speak for a dose.

    Open doses (SCHEDULED/DUE) show the container they would drop from *now*; doses that
    already touched the hardware show the slot that was used.
    """
    resolved = slots.get(event.medication_id)
    current = resolved[0] if resolved is not None else None
    if event.status in SCHEDULED_OR_DUE:
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


def find_confirmable(
    session: Session,
    now: datetime,
    settings: Settings,
    *,
    patient_id: int | None = None,
    medication_id: int | None = None,
) -> DoseEvent | None:
    """Most recent DISPENSED dose on the configured device dropped within
    ``confirm_window_minutes`` (optionally of one patient / one medication)."""
    cutoff = now - timedelta(minutes=settings.confirm_window_minutes)
    q = (
        select(DoseEvent)
        .options(selectinload(DoseEvent.medication))
        .where(
            DoseEvent.device_id == settings.device_id,
            DoseEvent.status == DoseStatus.DISPENSED.value,
            DoseEvent.dispensed_at >= cutoff,
        )
    )
    if patient_id is not None:
        q = q.where(DoseEvent.user_id == patient_id)
    if medication_id is not None:
        q = q.where(DoseEvent.medication_id == medication_id)
    return session.scalars(
        q.order_by(DoseEvent.dispensed_at.desc(), DoseEvent.event_id.desc()).limit(1)
    ).first()
