"""Containers (compartments) of the dispenser: medication assignment + pill inventory (v2).

One ``Compartment`` row exists per (device, slot). Slots are protocol indices ``0..N-1``;
people see "container ``slot + 1``" (docs/SERIAL_PROTOCOL.md §2). A medication occupies at
most one container on the device: assigning it elsewhere moves it.

Inventory rules (ARCHITECTURE v2 §5):

* ``pill_count`` is decremented by ``DropService`` on every DROPPED drop, set to 0 on
  ``ERR NO_PILL``, and set by doctor/family here (refill / correction) within ``0..capacity``.
* Changing a container's medication resets its count to 0 unless a count is given in the same
  update: pills of the previous medication are never counted as the new one.
* ``loaded_at`` is stamped whenever the count goes up (a refill). It also marks the start of a
  new "empty episode" for the EMPTY-notification de-duplication in ``DropService``.

The configured device (``settings.device_id``) is bound to one patient (``devices.user_id``).
:meth:`CompartmentService.ensure_device` creates it when missing and, when asked, binds it to a
patient if it has no real patient yet (the wave-1 placeholder user has no email).

The slot used for a drop is always resolved *at drop time* from these rows, never from data
stored when a dose event was generated. Module-level helpers (:func:`get_device`,
:func:`patient_device`, :func:`ensure_device_rows`, :func:`assigned_slots`,
:func:`container_info`, :func:`compartment_to_dict`) work inside the caller's session and never
commit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import ContainerInfo
from tactidose.db.devlog import log_event
from tactidose.db.models import Compartment, Device, LogCategory, Medication, Role, User
from tactidose.db.session import Database
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError

log = logging.getLogger(__name__)

DEFAULT_USER_NAME = "TactiDose User"
MAX_CAPACITY = 500
MAX_LOW_STOCK_THRESHOLD = 100
#: Sentinel for "medication_id not given" (``None`` means "unassign").
UNSET: Any = object()

__all__ = [
    "DEFAULT_USER_NAME",
    "MAX_CAPACITY",
    "MAX_LOW_STOCK_THRESHOLD",
    "UNSET",
    "CompartmentService",
    "assigned_slots",
    "compartment_to_dict",
    "container_info",
    "device_compartments",
    "ensure_device_rows",
    "get_device",
    "is_real_patient",
    "iso",
    "log_device_changes",
    "patient_device",
]


def iso(dt: Any) -> str | None:
    """ISO-8601 with offset (all stored datetimes are aware UTC)."""
    return dt.isoformat() if dt is not None else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- shared helpers


def get_device(session: Session, settings: Settings) -> Device | None:
    """The configured device row (``settings.device_id``), or None before bootstrap."""
    return session.get(Device, settings.device_id)


def patient_device(session: Session, settings: Settings, patient_id: int) -> Device | None:
    """The device bound to ``patient_id``: the configured one if it is theirs, else their
    first other device row (which this process cannot actuate), else None."""
    if not _is_int(patient_id):
        return None
    dev = session.get(Device, settings.device_id)
    if dev is not None and dev.user_id == patient_id:
        return dev
    return session.scalars(
        select(Device).where(Device.user_id == patient_id).order_by(Device.device_id).limit(1)
    ).first()


def is_real_patient(user: User | None) -> bool:
    """A registered patient account (the bootstrap placeholder user has no email)."""
    return user is not None and user.role == Role.PATIENT.value and bool(user.email)


def _default_owner(session: Session) -> tuple[User, bool]:
    """Owner for a newly created device: first registered patient, else first patient-role
    user, else a new placeholder. Returns (user, created)."""
    patients = session.scalars(
        select(User).where(User.role == Role.PATIENT.value).order_by(User.user_id)
    ).all()
    for user in patients:
        if is_real_patient(user):
            return user, False
    if patients:
        return patients[0], False
    user = User(display_name=DEFAULT_USER_NAME, role=Role.PATIENT.value, accessibility_preferences={},
                voice_enabled=True)
    session.add(user)
    session.flush()
    return user, True


def ensure_device_rows(
    session: Session, settings: Settings, *, patient_id: int | None = None
) -> tuple[Device, list[dict[str, Any]]]:
    """Idempotently create the configured device and one compartment per slot.

    ``patient_id``: create the device for that patient, or re-bind an existing device whose
    owner is not a real patient (assignments of other users' medications are cleared). A device
    that already belongs to another real patient is left alone.

    When ``num_slots`` grows, missing rows are added (and previously deactivated in-range rows
    re-activated); when it shrinks, out-of-range rows are deactivated but keep their assignment.
    Returns the device and a list of change descriptions. Flushes, never commits.
    """
    changes: list[dict[str, Any]] = []
    owner: User | None = None
    if patient_id is not None:
        owner = session.get(User, patient_id) if _is_int(patient_id) else None
        if owner is None:
            raise NotFoundError(f"Patient {patient_id} not found.")
        if owner.role != Role.PATIENT.value:
            raise ValidationError("Only a patient account can own a dispenser.")
    dev = session.get(Device, settings.device_id)
    if dev is None:
        if owner is None:
            owner, created = _default_owner(session)
            if created:
                changes.append({"event": "USER_CREATED", "user_id": owner.user_id})
        dev = Device(
            device_id=settings.device_id,
            user_id=owner.user_id,
            name=settings.device_name,
            num_slots=settings.num_slots,
            manual_cooldown_minutes=settings.manual_cooldown_minutes,
            auto_drop_enabled=settings.auto_drop_enabled,
        )
        session.add(dev)
        session.flush()
        changes.append({"event": "DEVICE_CREATED", "num_slots": settings.num_slots, "user_id": owner.user_id})
    elif owner is not None and dev.user_id != owner.user_id:
        current = session.get(User, dev.user_id)
        if not is_real_patient(current):
            previous = dev.user_id
            dev.user_id = owner.user_id
            changes.append({"event": "DEVICE_BOUND", "from_user_id": previous, "user_id": owner.user_id})
            cleared: list[int] = []
            for comp in session.scalars(
                select(Compartment)
                .options(selectinload(Compartment.medication))
                .where(Compartment.device_id == dev.device_id, Compartment.medication_id.is_not(None))
            ).all():
                if comp.medication is None or comp.medication.user_id != owner.user_id:
                    comp.medication_id = None
                    comp.pill_count = 0
                    comp.loaded_at = None
                    cleared.append(comp.slot_number)
            if cleared:
                changes.append({"event": "COMPARTMENTS_CLEARED", "slots": sorted(cleared)})
    if dev.num_slots != settings.num_slots:
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
            session.add(Compartment(
                device_id=dev.device_id, slot_number=slot, active=True, pill_count=0,
                capacity=settings.default_container_capacity,
                low_stock_threshold=settings.default_low_stock_threshold,
            ))
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
    """``medication_id -> (slot, compartment_id)`` for active, in-range compartments of the
    configured device. If (through data corruption) a medication sits in two compartments, the
    lowest slot wins."""
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


def device_compartments(session: Session, device: Device, settings: Settings) -> list[Compartment]:
    """Active compartments of ``device`` inside the configured slot range, ordered by slot."""
    limit = min(device.num_slots or settings.num_slots, settings.num_slots)
    return list(session.scalars(
        select(Compartment)
        .options(selectinload(Compartment.medication))
        .where(
            Compartment.device_id == device.device_id,
            Compartment.active.is_(True),
            Compartment.slot_number < limit,
        )
        .order_by(Compartment.slot_number)
    ).all())


def container_info(comp: Compartment) -> ContainerInfo:
    """``ContainerInfo`` for one compartment row (must be attached or fully loaded)."""
    med = comp.medication if comp.medication_id is not None else None
    return ContainerInfo(
        slot=comp.slot_number,
        compartment_id=comp.compartment_id,
        medication_id=comp.medication_id,
        medication_name=med.name if med is not None else None,
        strength=med.strength if med is not None else None,
        pill_count=int(comp.pill_count or 0),
        capacity=int(comp.capacity or 0),
        low_stock_threshold=int(comp.low_stock_threshold or 0),
        loaded_at=comp.loaded_at,
    )


def compartment_to_dict(comp: Compartment) -> dict[str, Any]:
    """API.md v2 ``ContainerInfo`` shape plus ``active``."""
    d = container_info(comp).to_dict()
    d["active"] = bool(comp.active)
    return d


def log_device_changes(session: Session, device_id: str, changes: list[dict[str, Any]],
                       *, at: datetime | None = None) -> None:
    for change in changes:
        detail = {k: v for k, v in change.items() if k != "event"}
        log_event(session, device_id, LogCategory.ADMIN, str(change["event"]), detail, at=at)


# --------------------------------------------------------------------------- service


class CompartmentService:
    """Container assignment + inventory (doctor/family operations). Thread-safe.

    ``clock`` is optional (ARCHITECTURE §11 wires it without one); without it ``loaded_at`` and
    audit rows use wall-clock UTC instead of the demo clock.
    """

    def __init__(self, db: Database, settings: Settings, bus: EventBus | None = None,
                 *, clock: Clock | None = None) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self.clock = clock

    @property
    def device_id(self) -> str:
        return self.settings.device_id

    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(timezone.utc)

    # ------------------------------------------------------------------ bootstrap
    def ensure_device(self, patient_id: int | None = None) -> dict[str, Any]:
        """Create the configured device / compartments if missing (idempotent) and, with
        ``patient_id``, bind the device to that patient if it has no real patient yet.

        Returns ``{device_id, patient_id (the owner), bound (owner == patient_id), num_slots,
        changes}``.
        """
        changes: list[dict[str, Any]] = []
        out: dict[str, Any] = {}
        for attempt in range(2):
            try:
                with self.db.session() as s:
                    dev, changes = ensure_device_rows(s, self.settings, patient_id=patient_id)
                    log_device_changes(s, self.device_id, changes, at=self._now())
                    out = {
                        "device_id": dev.device_id,
                        "patient_id": dev.user_id,
                        "bound": patient_id is None or dev.user_id == patient_id,
                        "num_slots": dev.num_slots,
                        "changes": [c["event"] for c in changes],
                    }
                break
            except IntegrityError:
                # Another thread/process bootstrapped concurrently; the retry sees its rows.
                if attempt:
                    raise
                log.info("concurrent device bootstrap detected; retrying")
        if changes:
            log.info("device %s bootstrap: %s", self.device_id, out["changes"])
            self._publish(None, out["patient_id"])
        return out

    def user_id(self) -> int:
        """The patient this device belongs to (bootstraps the device if needed)."""
        return int(self.ensure_device()["patient_id"])

    # ------------------------------------------------------------------ queries
    def containers(self, patient_id: int | None = None) -> list[ContainerInfo]:
        """All containers of the patient's device (default: the configured device, bootstrapped
        if needed), ordered by slot. A patient without a device has none."""
        with self.db.session() as s:
            if patient_id is None:
                dev, changes = ensure_device_rows(s, self.settings)
                log_device_changes(s, self.device_id, changes, at=self._now())
            else:
                dev, changes = patient_device(s, self.settings, patient_id), []
                if dev is None:
                    return []
            out = [container_info(c) for c in device_compartments(s, dev, self.settings)]
            owner = dev.user_id
        if changes:
            self._publish(None, owner)
        return out

    def list(self, patient_id: int | None = None) -> list[dict[str, Any]]:
        """API.md ``[ContainerInfo]`` dicts (all slots, ordered)."""
        return [c.to_dict() for c in self.containers(patient_id)]

    def get(self, slot: int, *, patient_id: int | None = None) -> ContainerInfo:
        self._check_slot(slot)
        for info in self.containers(patient_id):
            if info.slot == slot:
                return info
        raise NotFoundError(f"Container {slot + 1} not found.")

    # ------------------------------------------------------------------ commands (doctor/family)
    def assign(self, slot: int, medication_id: int | None, *, patient_id: int | None = None,
               pill_count: int | None = None, by_user_id: int | None = None) -> ContainerInfo:
        """Put ``medication_id`` (or nothing) in ``slot``; the medication must belong to the
        device's patient and be active and confirmed. Returns the updated container."""
        return self.update(slot, patient_id=patient_id, medication_id=medication_id,
                           pill_count=pill_count, by_user_id=by_user_id)

    def update(
        self,
        slot: int,
        *,
        patient_id: int | None = None,
        medication_id: Any = UNSET,
        pill_count: int | None = None,
        capacity: int | None = None,
        low_stock_threshold: int | None = None,
        by_user_id: int | None = None,
    ) -> ContainerInfo:
        """``PUT …/containers/{slot}``: any of medication (``None`` = unassign), pill count,
        capacity and low-stock threshold, applied atomically."""
        self._check_slot(slot)
        if medication_id is not UNSET and medication_id is not None and not _is_int(medication_id):
            raise ValidationError("medication_id must be an integer or null.")
        if pill_count is not None:
            self._check_int(pill_count, "pill_count", 0, MAX_CAPACITY)
        if capacity is not None:
            self._check_int(capacity, "capacity", 1, MAX_CAPACITY)
        if low_stock_threshold is not None:
            self._check_int(low_stock_threshold, "low_stock_threshold", 0, MAX_LOW_STOCK_THRESHOLD)
        now = self._now()
        with self.db.session() as s:
            dev = self._device(s, patient_id)
            comp = self._compartment(s, dev, slot)
            previous_med = comp.medication_id
            old_count = int(comp.pill_count or 0)
            moved_from: list[int] = []
            med_changed = medication_id is not UNSET and medication_id != previous_med
            if med_changed and medication_id is not None:
                med = s.get(Medication, medication_id)
                if med is None:
                    raise NotFoundError(f"Medication {medication_id} not found.")
                if med.user_id != dev.user_id:
                    raise ValidationError("That medication belongs to a different patient.")
                if not med.active:
                    raise ValidationError("An archived medication cannot be put in a container.")
                if not med.confirmed_by_user:
                    raise ValidationError("Only confirmed medications can be put in a container.")
                others = s.scalars(select(Compartment).where(
                    Compartment.device_id == dev.device_id,
                    Compartment.medication_id == medication_id,
                    Compartment.compartment_id != comp.compartment_id,
                )).all()
                for other in others:  # a medication occupies at most one container: moving clears the old one
                    other.medication_id = None
                    other.pill_count = 0
                    other.loaded_at = None
                    moved_from.append(other.slot_number)
            new_capacity = capacity if capacity is not None else int(comp.capacity or 0)
            new_count = pill_count if pill_count is not None else (0 if med_changed else old_count)
            if new_count > new_capacity:
                raise ValidationError(
                    f"Container {slot + 1} holds at most {plural_pills(new_capacity)}; "
                    f"{new_count} is too many."
                )
            if med_changed:
                comp.medication_id = medication_id
                comp.loaded_at = None
            if capacity is not None:
                comp.capacity = capacity
            if low_stock_threshold is not None:
                comp.low_stock_threshold = low_stock_threshold
            if new_count != old_count or med_changed:
                comp.pill_count = new_count
            if pill_count is not None and pill_count > (0 if med_changed else old_count):
                comp.loaded_at = now
            comp.updated_at = now
            s.flush()
            detail: dict[str, Any] = {"slot": slot, "by_user_id": by_user_id}
            if med_changed:
                detail.update(medication_id=medication_id, previous_medication_id=previous_med,
                              moved_from_slots=moved_from)
            for key, value in (("pill_count", pill_count), ("capacity", capacity),
                               ("low_stock_threshold", low_stock_threshold)):
                if value is not None:
                    detail[key] = value
            log_event(s, dev.device_id, LogCategory.ADMIN,
                      "COMPARTMENT_ASSIGNED" if med_changed else "CONTAINER_UPDATED", detail, at=now)
            info = container_info(comp)
            owner = dev.user_id
        log.info("container %s updated: %s", slot + 1, detail)
        self._publish(slot, owner)
        return info

    def refill(self, slot: int, *, set: int | None = None, add: int | None = None,  # noqa: A002
               patient_id: int | None = None, by_user_id: int | None = None) -> ContainerInfo:
        """``POST …/containers/{slot}/refill``: ``set`` the count or ``add`` pills (result within
        ``0..capacity``). A count that goes up stamps ``loaded_at``."""
        self._check_slot(slot)
        if (set is None) == (add is None):
            raise ValidationError("Give either set (the new pill count) or add (pills added).")
        if set is not None:
            self._check_int(set, "set", 0, MAX_CAPACITY)
        else:
            self._check_int(add, "add", 1, MAX_CAPACITY)
        now = self._now()
        with self.db.session() as s:
            dev = self._device(s, patient_id)
            comp = self._compartment(s, dev, slot)
            for _attempt in range(5):
                before = int(comp.pill_count or 0)
                new = set if set is not None else before + int(add or 0)
                if new > comp.capacity:
                    raise ValidationError(
                        f"Container {slot + 1} holds at most {plural_pills(comp.capacity)}; "
                        f"{new} is too many."
                    )
                values: dict[str, Any] = {"pill_count": new, "updated_at": now}
                if new > before:
                    values["loaded_at"] = now
                # Compare-and-set on the count: a drop may decrement it concurrently.
                res = s.execute(
                    update(Compartment)
                    .where(Compartment.compartment_id == comp.compartment_id, Compartment.pill_count == before)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                comp = s.get(Compartment, comp.compartment_id, populate_existing=True)
                if res.rowcount == 1:
                    break
            else:
                raise ConflictError("The pill count changed at the same time; please try again.")
            log_event(s, dev.device_id, LogCategory.ADMIN, "CONTAINER_REFILLED",
                      {"slot": slot, "before": before, "after": new, "set": set, "add": add,
                       "by_user_id": by_user_id}, at=now)
            info = container_info(comp)
            owner = dev.user_id
        log.info("container %s refilled: %s -> %s", slot + 1, before, new)
        self._publish(slot, owner)
        return info

    # ------------------------------------------------------------------ internals
    def _device(self, s: Session, patient_id: int | None) -> Device:
        if patient_id is None:
            dev, changes = ensure_device_rows(s, self.settings)
            log_device_changes(s, self.device_id, changes, at=self._now())
            return dev
        dev = patient_device(s, self.settings, patient_id)
        if dev is None:
            raise NotFoundError("No dispenser is linked to this patient.")
        if dev.device_id == self.device_id:
            ensure_device_rows(s, self.settings)  # make sure every slot row exists
        return dev

    def _compartment(self, s: Session, dev: Device, slot: int) -> Compartment:
        comp = s.scalars(
            select(Compartment)
            .options(selectinload(Compartment.medication))
            .where(Compartment.device_id == dev.device_id, Compartment.slot_number == slot)
        ).first()
        if comp is None or not comp.active:
            raise NotFoundError(f"Container {slot + 1} is not in use on this dispenser.")
        return comp

    def _check_slot(self, slot: object) -> None:
        n = self.settings.num_slots
        if not _is_int(slot) or not 0 <= int(slot) < n:  # type: ignore[arg-type]
            raise ValidationError(f"Slot must be a whole number from 0 to {n - 1}.")

    @staticmethod
    def _check_int(value: object, field: str, lo: int, hi: int) -> None:
        if not _is_int(value) or not lo <= int(value) <= hi:  # type: ignore[arg-type]
            raise ValidationError(f"{field} must be a whole number from {lo} to {hi}.")

    def _publish(self, slot: int | None, patient_id: int | None) -> None:
        if self.bus is None:
            return
        self.bus.publish(Topic.DATA_CHANGED, {"entity": "compartment", "id": slot, "patient_id": patient_id})
        if patient_id is not None:
            self.bus.publish(Topic.PATIENT_STATUS, {"patient_id": patient_id, "reason": "inventory"})


def plural_pills(count: int) -> str:
    return f"{count} pill" if count == 1 else f"{count} pills"
