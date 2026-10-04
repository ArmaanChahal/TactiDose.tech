"""Slot <-> medication assignment, plus the device/user bootstrap.

One ``Compartment`` row exists per (device, slot). A medication occupies at most
one slot on the device; assigning it elsewhere moves it, and assigning onto an
occupied slot unassigns the previous medication. Slots are protocol indices
``0..N-1``; people see "compartment ``slot + 1``" (docs/SERIAL_PROTOCOL.md §2).

The slot used for dispensing is always resolved *at dispense time* from these
rows (ARCHITECTURE §5 rule 4), never from data stored when an event was generated.

Module-level helpers (:func:`get_device`, :func:`ensure_device_rows`,
:func:`assigned_slots`, :func:`compartment_to_dict`) are shared by the other
domain modules; they work inside the caller's session and never commit.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.db.devlog import log_event
from tactidose.db.models import Compartment, Device, LogCategory, Medication, User
from tactidose.db.session import Database
from tactidose.hardware.protocol import compartment_number
from tactidose.medication.errors import NotFoundError, ValidationError

log = logging.getLogger(__name__)

DEFAULT_USER_NAME = "TactiDose User"

__all__ = [
    "DEFAULT_USER_NAME",
    "CompartmentService",
    "assigned_slots",
    "compartment_to_dict",
    "ensure_device_rows",
    "get_device",
    "iso",
    "log_device_changes",
]


def iso(dt: Any) -> str | None:
    """ISO-8601 with offset (all stored datetimes are aware UTC)."""
    return dt.isoformat() if dt is not None else None


# --------------------------------------------------------------------------- shared helpers


def get_device(session: Session, settings: Settings) -> Device | None:
    return session.get(Device, settings.device_id)


def ensure_device_rows(session: Session, settings: Settings) -> tuple[Device, list[dict[str, Any]]]:
    """Idempotently create the default user, this device and one compartment per slot.

    When ``num_slots`` grows, missing rows are added (and previously deactivated
    in-range rows re-activated); when it shrinks, out-of-range rows are
    deactivated but keep their assignment. Returns the device and a list of
    change descriptions (empty when nothing changed). Flushes, never commits.
    """
    changes: list[dict[str, Any]] = []
    dev = session.get(Device, settings.device_id)
    if dev is None:
        user = session.scalars(select(User).order_by(User.user_id).limit(1)).first()
        if user is None:
            user = User(display_name=DEFAULT_USER_NAME, accessibility_preferences={}, voice_enabled=True)
            session.add(user)
            session.flush()
            changes.append({"event": "USER_CREATED", "user_id": user.user_id})
        dev = Device(
            device_id=settings.device_id,
            user_id=user.user_id,
            name=settings.device_name,
            num_slots=settings.num_slots,
        )
        session.add(dev)
        session.flush()
        changes.append({"event": "DEVICE_CREATED", "num_slots": settings.num_slots, "user_id": user.user_id})
    elif dev.num_slots != settings.num_slots:
        changes.append({"event": "SLOT_COUNT_CHANGED", "from": dev.num_slots, "to": settings.num_slots})
        dev.num_slots = settings.num_slots

    existing = {
        c.slot_number: c
        for c in session.scalars(select(Compartment).where(Compartment.device_id == dev.device_id))
    }
    added: list[int] = []
    reactivated: list[int] = []
    deactivated: list[int] = []
    for slot in range(settings.num_slots):
        comp = existing.get(slot)
        if comp is None:
            session.add(Compartment(device_id=dev.device_id, slot_number=slot, active=True))
            added.append(slot)
        elif not comp.active:
            comp.active = True
            reactivated.append(slot)
    for slot, comp in sorted(existing.items()):
        if slot >= settings.num_slots and comp.active:
            comp.active = False  # keep medication_id: assignments are never deleted
            deactivated.append(slot)
    if added:
        changes.append({"event": "COMPARTMENTS_ADDED", "slots": added})
    if reactivated:
        changes.append({"event": "COMPARTMENTS_REACTIVATED", "slots": reactivated})
    if deactivated:
        changes.append({"event": "COMPARTMENTS_DEACTIVATED", "slots": deactivated})
    session.flush()
    return dev, changes


def assigned_slots(session: Session, settings: Settings) -> dict[int, tuple[int, int]]:
    """``medication_id -> (slot, compartment_id)`` for active, in-range compartments of this device.

    If (through data corruption) a medication sits in two compartments, the
    lowest slot wins — both would hold the same medication.
    """
    rows = session.execute(
        select(Compartment.medication_id, Compartment.slot_number, Compartment.compartment_id)
        .where(
            Compartment.device_id == settings.device_id,
            Compartment.active.is_(True),
            Compartment.medication_id.is_not(None),
        )
        .order_by(Compartment.slot_number)
    ).all()
    out: dict[int, tuple[int, int]] = {}
    for med_id, slot, comp_id in rows:
        if 0 <= slot < settings.num_slots and med_id not in out:
            out[med_id] = (slot, comp_id)
    return out


def compartment_to_dict(comp: Compartment) -> dict[str, Any]:
    """API.md ``Compartment`` shape."""
    med = comp.medication if comp.medication_id is not None else None
    return {
        "slot": comp.slot_number,
        "compartment_number": compartment_number(comp.slot_number),
        "compartment_id": comp.compartment_id,
        "medication_id": comp.medication_id,
        "medication_name": med.name if med is not None else None,
        "active": bool(comp.active),
        "loaded_at": iso(comp.loaded_at),
    }


def log_device_changes(session: Session, device_id: str, changes: list[dict[str, Any]]) -> None:
    for change in changes:
        detail = {k: v for k, v in change.items() if k != "event"}
        log_event(session, device_id, LogCategory.ADMIN, str(change["event"]), detail)


# --------------------------------------------------------------------------- service


class CompartmentService:
    def __init__(self, db: Database, settings: Settings, bus: EventBus | None = None) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus

    @property
    def device_id(self) -> str:
        return self.settings.device_id

    # ------------------------------------------------------------------ bootstrap
    def ensure_device(self) -> None:
        """Create the default user / device / compartments if missing (idempotent)."""
        changes = self._ensure()
        if changes:
            log.info("device %s bootstrap: %s", self.device_id, [c["event"] for c in changes])

    def user_id(self) -> int:
        """The user this device belongs to (bootstraps the device if needed)."""
        with self.db.session() as s:
            dev, changes = ensure_device_rows(s, self.settings)
            log_device_changes(s, self.device_id, changes)
            uid = dev.user_id
        if changes:
            self._publish({"entity": "compartment", "id": None})
        return uid

    def _ensure(self) -> list[dict[str, Any]]:
        for attempt in range(2):
            try:
                with self.db.session() as s:
                    _dev, changes = ensure_device_rows(s, self.settings)
                    log_device_changes(s, self.device_id, changes)
                break
            except IntegrityError:
                # Another thread/process bootstrapped concurrently; the retry sees its rows.
                if attempt:
                    raise
                log.info("concurrent device bootstrap detected; retrying")
        if changes:
            self._publish({"entity": "compartment", "id": None})
        return changes

    # ------------------------------------------------------------------ queries
    def list(self) -> list[dict[str, Any]]:
        """All ``num_slots`` compartments, ordered by slot (API.md ``Compartment``)."""
        with self.db.session() as s:
            dev, changes = ensure_device_rows(s, self.settings)
            log_device_changes(s, self.device_id, changes)
            rows = s.scalars(
                select(Compartment)
                .where(
                    Compartment.device_id == dev.device_id,
                    Compartment.slot_number < self.settings.num_slots,
                )
                .order_by(Compartment.slot_number)
            ).all()
            out = [compartment_to_dict(c) for c in rows]
        if changes:
            self._publish({"entity": "compartment", "id": None})
        return out

    # ------------------------------------------------------------------ commands
    def assign(self, slot: int, medication_id: int | None) -> list[dict[str, Any]]:
        """Put ``medication_id`` (or nothing) in ``slot``. Returns the full compartment list."""
        n = self.settings.num_slots
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < n:
            raise ValidationError(f"Slot must be a whole number from 0 to {n - 1}.")
        if medication_id is not None and (isinstance(medication_id, bool) or not isinstance(medication_id, int)):
            raise ValidationError("medication_id must be an integer or null.")
        with self.db.session() as s:
            dev, changes = ensure_device_rows(s, self.settings)
            log_device_changes(s, self.device_id, changes)
            comp = s.scalars(
                select(Compartment).where(Compartment.device_id == dev.device_id, Compartment.slot_number == slot)
            ).one()
            previous = comp.medication_id
            moved_from: list[int] = []
            if medication_id is not None:
                med = s.get(Medication, medication_id)
                if med is None:
                    raise NotFoundError(f"Medication {medication_id} not found.")
                if not med.active:
                    raise ValidationError("An archived medication cannot be assigned to a compartment.")
                if not med.confirmed_by_user:
                    raise ValidationError("Only confirmed medications can be assigned to a compartment.")
                if med.user_id != dev.user_id:
                    raise ValidationError("That medication belongs to a different user.")
                others = s.scalars(
                    select(Compartment).where(
                        Compartment.device_id == dev.device_id,
                        Compartment.medication_id == medication_id,
                        Compartment.compartment_id != comp.compartment_id,
                    )
                ).all()
                for other in others:  # a medication occupies at most one slot: moving clears the old one
                    other.medication_id = None
                    other.loaded_at = None
                    moved_from.append(other.slot_number)
            if previous != medication_id:
                comp.medication_id = medication_id
                comp.loaded_at = None  # new contents must be loaded
            log_event(
                s,
                self.device_id,
                LogCategory.ADMIN,
                "COMPARTMENT_ASSIGNED",
                {
                    "slot": slot,
                    "medication_id": medication_id,
                    "previous_medication_id": previous,
                    "moved_from_slots": moved_from,
                },
            )
        log.info("slot %s assigned medication %s (was %s, moved from %s)", slot, medication_id, previous, moved_from)
        self._publish({"entity": "compartment", "id": slot})
        return self.list()

    # ------------------------------------------------------------------ internals
    def _publish(self, data: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(Topic.DATA_CHANGED, data)
