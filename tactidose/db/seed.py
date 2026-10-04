"""Demo data - candy and tokens only, never real medication (ARCHITECTURE v2 §9).

:func:`seed_demo` makes sure the demo exists:

* accounts ``alex@demo.tactidose`` (patient, Alex Rivera), ``sam@demo.tactidose`` (family,
  Sam Rivera) and ``dr.lee@demo.tactidose`` (doctor, Dr. Lee), password
  ``settings.demo_password``, Sam and Dr. Lee linked to Alex;
* device ``settings.device_id`` bound to Alex with ``settings.manual_cooldown_minutes``
  (taken over only if it has no real patient yet - see ``auth.service.bind_device_in_session``);
* containers 1-3 loaded with confirmed demo medications and 20 / 12 / 3 pills (container 3 is low
  on stock, to show alerts), and daily schedules 08:00 / 13:00 / 20:00 created by Dr. Lee.

It is idempotent and safe to run on every start: rows that exist are left as they are, so pill
counts, a changed cooldown, edited or deleted schedules and archived medications survive a
restart. Missing links are re-created and the demo password is re-applied if it changed.
Schedules are created with ``created_at = clock.now()``, so no past doses are invented.

:func:`reset_demo` wipes the dynamic data (pill drops, dose events, conversations and messages,
reports and deliveries, notifications, login sessions) and restores the initial demo state: the
device goes back to Alex (even from another patient), containers and counts are reloaded, the
cooldown and auto-drop flag return to the settings, the three demo schedules are the only active
ones, and other medications of the demo patient are archived. ``reseed=False`` only wipes.

Both return a summary dict (ids, containers, schedules, what was created). It never contains
passwords or the link code (summaries are logged and returned by the demo API); the patient
portal shows the link code to the patient. Raises ``ValidationError`` when the demo password
violates the password policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from tactidose.auth import passwords
from tactidose.auth.service import AuthService, bind_device_in_session
from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    ALL_DAYS,
    AuthSession,
    CareLink,
    Compartment,
    Conversation,
    ConversationMessage,
    Device,
    DoseEvent,
    Frequency,
    LogCategory,
    Medication,
    MedicationSource,
    Notification,
    PillDrop,
    Report,
    ReportDelivery,
    Role,
    Schedule,
    User,
)
from tactidose.db.session import Database
from tactidose.medication.errors import ConflictError, ValidationError

log = logging.getLogger(__name__)

__all__ = [
    "DEMO_ACCOUNTS",
    "DEMO_MEDICATIONS",
    "DYNAMIC_MODELS",
    "DemoAccount",
    "DemoMedication",
    "reset_demo",
    "seed_demo",
    "wipe_dynamic_data",
]


@dataclass(frozen=True)
class DemoAccount:
    key: str            # "patient" | "family" | "doctor"
    email: str
    display_name: str
    role: Role


@dataclass(frozen=True)
class DemoMedication:
    name: str
    strength: str
    instructions: str
    slot: int
    pill_count: int
    time_of_day: str


DEMO_ACCOUNTS: tuple[DemoAccount, ...] = (
    DemoAccount("patient", "alex@demo.tactidose", "Alex Rivera", Role.PATIENT),
    DemoAccount("family", "sam@demo.tactidose", "Sam Rivera", Role.FAMILY),
    DemoAccount("doctor", "dr.lee@demo.tactidose", "Dr. Lee", Role.DOCTOR),
)

DEMO_MEDICATIONS: tuple[DemoMedication, ...] = (
    DemoMedication("Vitamin C (demo candy)", "1 piece",
                   "Demo candy, not a medicine. One piece in the morning.", 0, 20, "08:00"),
    DemoMedication("Calcium (demo token)", "1 token",
                   "Demo token, not a medicine. One token at lunch.", 1, 12, "13:00"),
    DemoMedication("Omega-3 (demo candy)", "1 piece",
                   "Demo candy, not a medicine. One piece with dinner.", 2, 3, "20:00"),
)

DEMO_WARNINGS = ("Demo only - not a real medication.",)

#: Wiped by :func:`reset_demo`, children before parents (foreign keys are enforced on SQLite).
DYNAMIC_MODELS: tuple[type, ...] = (
    ReportDelivery, Report, ConversationMessage, Conversation, Notification, PillDrop, DoseEvent, AuthSession,
)


# =========================================================================== public API


def seed_demo(db: Database, settings: Settings, clock: Clock, *, auth: AuthService | None = None,
              bus: EventBus | None = None) -> dict[str, Any]:
    """Create whatever part of the demo is missing (idempotent). See the module docstring."""
    return _seed(db, settings, clock, auth=auth, bus=bus, restore=False)


def reset_demo(db: Database, settings: Settings, clock: Clock, *, auth: AuthService | None = None,
               bus: EventBus | None = None, keep_sessions: bool = False, reseed: bool = True) -> dict[str, Any]:
    """Wipe the dynamic data and restore the initial demo state.

    Everyone is signed out (sessions are dynamic data) unless ``keep_sessions``. ``reseed=False``
    only wipes (``POST /api/demo/reset {"reseed": false}``): accounts, containers and schedules stay
    as they are. The summary has an extra ``wiped`` entry (rows deleted per table) and ``reseeded``.
    Push the container counts to the simulator if it keeps its own physical pill counts
    (``summary["containers"]``)."""
    wiped = wipe_dynamic_data(db, keep_sessions=keep_sessions)
    if auth is not None:
        auth.clear_lockouts()
    log.info("demo reset: wiped %s", {k: v for k, v in wiped.items() if v})
    if not reseed:
        return {"reset": True, "reseeded": False, "wiped": wiped}
    summary = _seed(db, settings, clock, auth=auth, bus=bus, restore=True)
    summary.update(reseeded=True, wiped=wiped)
    return summary


def wipe_dynamic_data(db: Database, *, keep_sessions: bool = False) -> dict[str, int]:
    """Delete every row of :data:`DYNAMIC_MODELS` (one transaction); returns counts per table."""
    counts: dict[str, int] = {}
    with db.session() as s:
        for model in DYNAMIC_MODELS:
            if keep_sessions and model is AuthSession:
                continue
            counts[model.__tablename__] = int(s.execute(delete(model)).rowcount or 0)
    return counts


# =========================================================================== implementation


def _seed(db: Database, settings: Settings, clock: Clock, *, auth: AuthService | None,
          bus: EventBus | None, restore: bool) -> dict[str, Any]:
    auth = auth or AuthService(db, settings, clock, bus=bus)
    password = settings.demo_password.get_secret_value()
    problem = passwords.password_problem(password)
    if problem:
        raise ValidationError(f"The demo password (TACTIDOSE_DEMO_PASSWORD) cannot be used: {problem}")
    now = clock.now()
    accounts = [_ensure_account(db, auth, spec, password, restore=restore) for spec in DEMO_ACCOUNTS]
    ids = {a["key"]: a["user_id"] for a in accounts}
    specs = DEMO_MEDICATIONS[: settings.num_slots]

    with db.session() as s:
        patient = s.get(User, ids["patient"])
        family = s.get(User, ids["family"])
        doctor = s.get(User, ids["doctor"])
        links_created = _ensure_links(s, patient, (family, doctor), now)
        binding = bind_device_in_session(s, settings, patient, now=now, force=restore, tz=clock.tz)
        dev = s.get(Device, settings.device_id)
        owned = dev is not None and dev.user_id == patient.user_id

        meds: list[Medication] = []
        schedules: list[Schedule] = []
        comps: dict[int, Compartment] = {}
        meds_created = schedules_created = 0
        if owned:
            if restore or binding.changed:
                dev.manual_cooldown_minutes = settings.manual_cooldown_minutes
                dev.auto_drop_enabled = settings.auto_drop_enabled
                dev.updated_at = now
            comps = {c.slot_number: c for c in
                     s.scalars(select(Compartment).where(Compartment.device_id == dev.device_id))}
            for spec in specs:
                med, created = _ensure_medication(s, patient, doctor, spec, now, restore=restore)
                meds_created += created
                meds.append(med)
                _ensure_container(s, settings, dev, comps, med, spec, now, restore=restore)
                sched, created = _ensure_schedule(s, med, doctor, spec, now, restore=restore)
                schedules_created += created
                if sched is not None:
                    schedules.append(sched)
            if restore:
                _retire_other_setup(s, patient, comps, meds, now)
            s.flush()
        else:
            log.warning("demo device %s belongs to patient #%s, not the demo patient: containers and "
                        "schedules were not seeded (run reset-demo to take it back)",
                        settings.device_id, dev.user_id if dev else None)

        log_event(s, settings.device_id, LogCategory.ADMIN, "DEMO_RESET" if restore else "DEMO_SEEDED", {
            "patient_id": patient.user_id, "users_created": sum(a["created"] for a in accounts),
            "links_created": links_created, "medications_created": meds_created,
            "schedules_created": schedules_created, "device_owned": owned,
        }, at=now)
        in_range = [comps[k] for k in sorted(comps) if k < settings.num_slots]
        loaded = {c.medication_id for c in in_range if c.medication_id is not None}
        names = dict(s.execute(select(Medication.medication_id, Medication.name)
                               .where(Medication.medication_id.in_(loaded))).all()) if loaded else {}
        summary: dict[str, Any] = {
            "reset": restore,
            "device_id": settings.device_id,
            "device_owner_id": dev.user_id if dev is not None else None,
            "device_bound_to_patient": owned,
            "patient_id": patient.user_id,
            "family_id": family.user_id,
            "doctor_id": doctor.user_id,
            "accounts": accounts,
            "medication_ids": [m.medication_id for m in meds],
            "med_ids": [m.medication_id for m in meds],
            "compartment_ids": [c.compartment_id for c in in_range],
            "schedule_ids": [sc.schedule_id for sc in schedules],
            "containers": [
                {"slot": c.slot_number, "container_number": c.slot_number + 1,
                 "medication_id": c.medication_id, "medication_name": names.get(c.medication_id),
                 "pill_count": c.pill_count, "low_stock_threshold": c.low_stock_threshold}
                for c in in_range
            ],
            "schedules": [{"schedule_id": sc.schedule_id, "medication_id": sc.medication_id,
                           "time_of_day": sc.time_of_day} for sc in schedules],
            "cooldown_minutes": dev.manual_cooldown_minutes if dev is not None else None,
            "created": {
                "users": sum(a["created"] for a in accounts),
                "links": links_created,
                "device": binding.created,
                "medications": meds_created,
                "schedules": schedules_created,
            },
        }
    if bus is not None:
        bus.publish(Topic.PATIENT_STATUS, {"patient_id": summary["patient_id"],
                                           "reason": "demo_reset" if restore else "demo_seeded"})
    log.info("demo %s: %s", "reset" if restore else "seed", summary["created"])
    return summary


def _ensure_account(db: Database, auth: AuthService, spec: DemoAccount, password: str, *,
                    restore: bool) -> dict[str, Any]:
    def find() -> tuple[int, str | None] | None:
        with db.session() as s:
            row = s.scalars(select(User).where(func.lower(User.email) == spec.email)).first()
            return None if row is None else (row.user_id, row.password_hash)

    found = find()
    if found is None:
        try:
            user = auth.create_user(email=spec.email, password=password, display_name=spec.display_name,
                                    role=spec.role, bind_device=False)
            with db.session() as s:
                row = s.get(User, user.user_id)
                if spec.role is Role.PATIENT:
                    row.accessibility_preferences = {"voice": True, "large_text": True}
            return _account_view(spec, user.user_id, created=True)
        except ConflictError:        # created concurrently: continue with the existing row
            found = find()
            if found is None:
                raise
    user_id, stored = found
    new_hash = None if stored and passwords.verify_password(password, stored) else passwords.hash_password(password)
    with db.session() as s:
        row = s.get(User, user_id)
        row.email = spec.email
        row.role = spec.role.value
        row.is_active = True
        if restore or not row.display_name:
            row.display_name = spec.display_name
        if new_hash:
            row.password_hash = new_hash
        if spec.role is Role.PATIENT and not row.link_code:
            row.link_code = auth.new_link_code(s)
    return _account_view(spec, user_id, created=False, password_reset=new_hash is not None)


def _account_view(spec: DemoAccount, user_id: int, *, created: bool, password_reset: bool = False) -> dict[str, Any]:
    return {"key": spec.key, "user_id": user_id, "email": spec.email, "display_name": spec.display_name,
            "role": spec.role.value, "created": created, "password_reset": password_reset}


def _ensure_links(s: Session, patient: User, caregivers: tuple[User, ...], now: datetime) -> int:
    created = 0
    for carer in caregivers:
        link = s.scalars(select(CareLink).where(CareLink.caregiver_id == carer.user_id,
                                                CareLink.patient_id == patient.user_id)).first()
        if link is None:
            s.add(CareLink(caregiver_id=carer.user_id, patient_id=patient.user_id,
                           relationship_kind=carer.role, created_at=now))
            created += 1
        elif link.relationship_kind != carer.role:
            link.relationship_kind = carer.role
    s.flush()
    return created


def _ensure_medication(s: Session, patient: User, doctor: User, spec: DemoMedication, now: datetime, *,
                       restore: bool) -> tuple[Medication, bool]:
    med = s.scalars(
        select(Medication)
        .where(Medication.user_id == patient.user_id, Medication.name == spec.name)
        .order_by(Medication.active.desc(), Medication.medication_id)
    ).first()
    if med is None:
        med = Medication(
            user_id=patient.user_id, name=spec.name, strength=spec.strength,
            instructions_text=spec.instructions, warnings=list(DEMO_WARNINGS),
            source=MedicationSource.DEMO_SEED.value, confirmed_by_user=True,
            confirmed_by=f"{doctor.display_name} (demo seed)", confirmed_at=now, active=True,
            created_at=now, updated_at=now,
        )
        s.add(med)
        s.flush()
        return med, True
    if restore:
        med.strength = spec.strength
        med.instructions_text = spec.instructions
        med.warnings = list(DEMO_WARNINGS)
        med.active = True
        if not med.confirmed_by_user or med.confirmed_at is None:
            med.confirmed_by_user = True
            med.confirmed_by = f"{doctor.display_name} (demo seed)"
            med.confirmed_at = now
        med.updated_at = now
    return med, False


def _ensure_container(s: Session, settings: Settings, dev: Device, comps: dict[int, Compartment],
                      med: Medication, spec: DemoMedication, now: datetime, *, restore: bool) -> None:
    comp = comps.get(spec.slot)
    if comp is None:
        comp = Compartment(device_id=dev.device_id, slot_number=spec.slot, active=True, pill_count=0,
                           capacity=settings.default_container_capacity,
                           low_stock_threshold=settings.default_low_stock_threshold, updated_at=now)
        s.add(comp)
        s.flush()
        comps[spec.slot] = comp
    holders = [c for c in comps.values() if c.medication_id == med.medication_id]
    if restore:
        for other in holders:
            if other is not comp:
                other.medication_id = None
                other.pill_count = 0
                other.updated_at = now
        _load(comp, med, spec, settings, now)
    elif comp.medication_id is None and not holders and med.active:
        _load(comp, med, spec, settings, now)


def _load(comp: Compartment, med: Medication, spec: DemoMedication, settings: Settings, now: datetime) -> None:
    comp.medication_id = med.medication_id
    comp.pill_count = spec.pill_count
    comp.capacity = max(settings.default_container_capacity, spec.pill_count)
    comp.low_stock_threshold = settings.default_low_stock_threshold
    comp.loaded_at = now
    comp.active = True
    comp.notes = None
    comp.updated_at = now


def _ensure_schedule(s: Session, med: Medication, doctor: User, spec: DemoMedication, now: datetime, *,
                     restore: bool) -> tuple[Schedule | None, bool]:
    existing = s.scalars(select(Schedule).where(Schedule.medication_id == med.medication_id)
                         .order_by(Schedule.schedule_id)).all()
    if restore:
        keep: Schedule | None = None
        for sched in existing:
            if keep is None and sched.time_of_day == spec.time_of_day and sched.frequency == Frequency.DAILY.value:
                keep = sched
                sched.active = True
                sched.days_of_week = ",".join(ALL_DAYS)
                sched.created_at = now      # effective from now: no invented past doses
                sched.updated_at = now
                sched.created_by_user_id = sched.created_by_user_id or doctor.user_id
            elif sched.active:
                sched.active = False
                sched.updated_at = now
        if keep is not None:
            return keep, False
        return _new_schedule(s, med, doctor, spec, now), True
    if not existing:
        return _new_schedule(s, med, doctor, spec, now), True
    # Keep whatever the doctor/family changed (an edited time must not become a second dose).
    active = [sched for sched in existing if sched.active]
    return (active[0] if active else None), False


def _new_schedule(s: Session, med: Medication, doctor: User, spec: DemoMedication, now: datetime) -> Schedule:
    sched = Schedule(medication_id=med.medication_id, time_of_day=spec.time_of_day,
                     frequency=Frequency.DAILY.value, days_of_week=",".join(ALL_DAYS), active=True,
                     created_by_user_id=doctor.user_id, created_at=now, updated_at=now)
    s.add(sched)
    s.flush()
    return sched


def _retire_other_setup(s: Session, patient: User, comps: dict[int, Compartment],
                        demo_meds: list[Medication], now: datetime) -> None:
    """Reset only: archive the demo patient's other medications, deactivate their schedules and
    empty the containers that held them."""
    demo_ids = {m.medication_id for m in demo_meds}
    query = select(Medication).where(Medication.user_id == patient.user_id)
    if demo_ids:
        query = query.where(Medication.medication_id.not_in(demo_ids))
    others = s.scalars(query).all()
    other_ids = [m.medication_id for m in others]
    for med in others:
        if med.active:
            med.active = False
            med.updated_at = now
    if other_ids:
        for sched in s.scalars(select(Schedule).where(Schedule.medication_id.in_(other_ids),
                                                      Schedule.active.is_(True))):
            sched.active = False
            sched.updated_at = now
    for comp in comps.values():
        if comp.medication_id is not None and comp.medication_id not in demo_ids:
            comp.medication_id = None
            comp.pill_count = 0
            comp.updated_at = now
