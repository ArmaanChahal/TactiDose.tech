"""Fakes and fixtures for the API tests (``tests/test_api_*.py``). Contains no tests itself.

The fakes implement the Protocols of ``core/interfaces.py`` (+ the few extra methods the HTTP
layer calls, see ``tactidose/api/domain.py``) on top of the *real* v2 tables, seeded with
``tests.fakes.seed_v2``, so the API's own ownership checks run against real rows. They never
touch hardware and never import the parallel v2 modules.

Actors (``Harness.token(actor)``):

* ``patient``  — Alex (seed_v2 patient, owns the device)
* ``family`` / ``doctor`` — linked caregivers of Alex
* ``other``    — another patient (no device, no links)
* ``stranger`` — a doctor linked to nobody
"""

from __future__ import annotations

import itertools
import secrets
import threading
import warnings
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import anyio.from_thread
import pytest
from sqlalchemy import func, select

from tactidose.core.bus import Topic
from tactidose.core.interfaces import AgentReply, AuthUser, ContainerInfo, DropOutcome, PatientStatus
from tactidose.db.models import (
    CareLink,
    Compartment,
    Conversation,
    ConversationMessage,
    Device,
    DoseEvent,
    LabelScan,
    Medication,
    Notification,
    PillDrop,
    Report,
    ReportDelivery,
    Schedule,
    User,
)
from tactidose.medication.errors import ConflictError, DomainError, NotFoundError, ValidationError
from tests.fakes import FakeDropHardware, seed_v2

with warnings.catch_warnings():
    # Starlette 1.x nudges towards httpx2; the httpx 0.28 transport works fine for these tests.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

PASSWORD = "password123"
ACTORS = ("patient", "family", "doctor", "other", "stranger")


class AuthError(DomainError):
    """Stand-in with the same name/status as ``tactidose.auth.errors.AuthError``."""

    status_code = 401


class PermissionDenied(DomainError):
    status_code = 403


class AgentUnavailable(RuntimeError):
    """Same class name the agent module uses for "Vosk/model missing" (mapped to 503)."""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


_UNSET: Any = object()


# =========================================================================== auth


class FakeAuth:
    """AuthServiceAPI over the users / care_links tables; tokens and passwords in memory."""

    def __init__(self, db: Any, settings: Any) -> None:
        self.db = db
        self.settings = settings
        self.passwords: dict[str, str] = {}
        self.tokens: dict[str, int] = {}
        self.resolve_calls = 0
        self.lock = threading.Lock()

    # -- helpers for tests
    def set_password(self, email: str, password: str = PASSWORD) -> None:
        self.passwords[email.lower()] = password

    def issue(self, user_id: int) -> str:
        token = secrets.token_urlsafe(16)
        self.tokens[token] = user_id
        return token

    def _user(self, user_id: int) -> AuthUser | None:
        with self.db.session() as s:
            u = s.get(User, user_id)
            if u is None or not u.is_active:
                return None
            return AuthUser(user_id=u.user_id, display_name=u.display_name, role=u.role, email=u.email)

    # -- AuthServiceAPI
    def register(self, *, email: str, password: str, display_name: str, role: str,
                 phone: str | None = None) -> AuthUser:
        if not self.settings.allow_registration:
            raise PermissionDenied("Registration is disabled.")
        if len(password) < 8:
            raise ValidationError("The password must be at least 8 characters.")
        key = email.strip().lower()
        with self.db.session() as s:
            if s.scalar(select(User.user_id).where(func.lower(User.email) == key)) is not None:
                raise ConflictError("An account with this email already exists.")
            u = User(display_name=display_name, role=role, email=key, phone=phone,
                     link_code="NEWCODE1" if role == "patient" else None)
            s.add(u)
            s.flush()
            out = AuthUser(user_id=u.user_id, display_name=u.display_name, role=u.role, email=u.email)
        self.passwords[key] = password
        return out

    def login(self, email: str, password: str, *, user_agent: str | None = None) -> tuple[AuthUser, str]:
        key = email.strip().lower()
        if self.passwords.get(key) != password:
            raise AuthError("Email or password is not correct.")
        with self.db.session() as s:
            uid = s.scalar(select(User.user_id).where(func.lower(User.email) == key))
        user = self._user(uid) if uid is not None else None
        if user is None:
            raise AuthError("Email or password is not correct.")
        return user, self.issue(user.user_id)

    def resolve(self, token: str) -> AuthUser | None:
        with self.lock:
            self.resolve_calls += 1
        uid = self.tokens.get(token)
        return self._user(uid) if uid is not None else None

    def logout(self, token: str) -> None:
        self.tokens.pop(token, None)

    def link_patient(self, *, caregiver: AuthUser, patient_id: int, link_code: str) -> dict[str, Any]:
        if not caregiver.is_caregiver:
            raise PermissionDenied("Only doctor and family accounts can link to a patient.")
        with self.db.session() as s:
            p = s.get(User, patient_id)
            if p is None or p.role != "patient":
                raise NotFoundError("No patient has that ID.")
            if (p.link_code or "") != link_code.strip().upper():
                raise PermissionDenied("That link code is not correct.")
            exists = s.scalar(select(CareLink.link_id).where(
                CareLink.caregiver_id == caregiver.user_id, CareLink.patient_id == patient_id))
            if exists is None:
                s.add(CareLink(caregiver_id=caregiver.user_id, patient_id=patient_id,
                               relationship_kind=caregiver.role))
            return {"patient_id": p.user_id, "display_name": p.display_name, "relationship": caregiver.role}

    def unlink_patient(self, *, caregiver: AuthUser, patient_id: int) -> None:
        with self.db.session() as s:
            link = s.scalars(select(CareLink).where(
                CareLink.caregiver_id == caregiver.user_id, CareLink.patient_id == patient_id)).first()
            if link is None:
                raise NotFoundError("You are not linked to that patient.")
            s.delete(link)

    def _linked(self, caregiver_id: int, patient_id: int) -> bool:
        with self.db.session() as s:
            return s.scalar(select(CareLink.link_id).where(
                CareLink.caregiver_id == caregiver_id, CareLink.patient_id == patient_id)) is not None

    def can_view(self, user: AuthUser, patient_id: int) -> bool:
        if user.is_patient:
            return user.user_id == patient_id
        return user.is_caregiver and self._linked(user.user_id, patient_id)

    def can_edit(self, user: AuthUser, patient_id: int) -> bool:
        return user.is_caregiver and self._linked(user.user_id, patient_id)

    def linked_patient_ids(self, user: AuthUser) -> list[int]:
        if user.is_patient:
            return [user.user_id]
        with self.db.session() as s:
            return list(s.scalars(select(CareLink.patient_id).where(CareLink.caregiver_id == user.user_id)))

    def caregiver_ids(self, patient_id: int) -> list[int]:
        with self.db.session() as s:
            return list(s.scalars(select(CareLink.caregiver_id).where(CareLink.patient_id == patient_id)))


# =========================================================================== notifications


class FakeNotifications:
    def __init__(self, db: Any, bus: Any) -> None:
        self.db = db
        self.bus = bus

    def notify(self, *, patient_id: int, kind: str, title: str, body: str = "", data: dict[str, Any] | None = None,
               to_patient: bool = True, to_caregivers: bool = True) -> list[int]:
        with self.db.session() as s:
            recipients: list[int] = [patient_id] if to_patient else []
            if to_caregivers:
                recipients += list(s.scalars(select(CareLink.caregiver_id).where(CareLink.patient_id == patient_id)))
            rows = [Notification(user_id=uid, patient_id=patient_id, kind=kind, title=title, body=body,
                                 data=data or {}) for uid in recipients]
            s.add_all(rows)
            s.flush()
            views = [self._view(r) for r in rows]
        for v in views:
            self.bus.publish(Topic.NOTIFICATION, v)
        return [v["notification_id"] for v in views]

    @staticmethod
    def _view(n: Notification) -> dict[str, Any]:
        return {"notification_id": n.notification_id, "user_id": n.user_id, "patient_id": n.patient_id,
                "kind": n.kind, "title": n.title, "body": n.body, "data": dict(n.data or {}),
                "created_at": _iso(n.created_at), "read_at": _iso(n.read_at)}

    def list_for_user(self, user_id: int, *, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(Notification).where(Notification.user_id == user_id)
            if unread_only:
                q = q.where(Notification.read_at.is_(None))
            rows = s.scalars(q.order_by(Notification.notification_id.desc()).limit(limit)).all()
            return [self._view(r) for r in rows]

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int:
        with self.db.session() as s:
            q = select(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
            if ids is not None:
                q = q.where(Notification.notification_id.in_(ids))
            rows = s.scalars(q).all()
            for r in rows:
                r.read_at = datetime.now(timezone.utc)
            return len(rows)


# =========================================================================== containers / catalog / schedules


def container_info(c: Compartment) -> ContainerInfo:
    med = c.medication
    return ContainerInfo(slot=c.slot_number, compartment_id=c.compartment_id, medication_id=c.medication_id,
                         medication_name=med.name if med else None, strength=med.strength if med else None,
                         pill_count=int(c.pill_count or 0), capacity=int(c.capacity or 0),
                         low_stock_threshold=int(c.low_stock_threshold or 0), loaded_at=c.loaded_at)


def _patient_device(s: Any, patient_id: int) -> Device | None:
    return s.scalars(select(Device).where(Device.user_id == patient_id)).first()


class FakeCompartments:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def containers(self, patient_id: int | None = None) -> list[ContainerInfo]:
        with self.db.session() as s:
            dev = _patient_device(s, patient_id) if patient_id is not None else None
            if dev is None:
                return []
            rows = s.scalars(select(Compartment).where(Compartment.device_id == dev.device_id)
                             .order_by(Compartment.slot_number)).all()
            return [container_info(c) for c in rows]

    def list(self, patient_id: int | None = None) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.containers(patient_id)]

    def _comp(self, s: Any, slot: int, patient_id: int) -> Compartment:
        dev = _patient_device(s, patient_id)
        if dev is None:
            raise NotFoundError("No dispenser is linked to this patient.")
        comp = s.scalars(select(Compartment).where(Compartment.device_id == dev.device_id,
                                                   Compartment.slot_number == slot)).first()
        if comp is None:
            raise NotFoundError(f"Container {slot + 1} not found.")
        return comp

    def update(self, slot: int, *, patient_id: int | None = None, medication_id: Any = _UNSET,
               pill_count: int | None = None, capacity: int | None = None, low_stock_threshold: int | None = None,
               by_user_id: int | None = None) -> ContainerInfo:
        self.calls.append(("update", {"slot": slot, "patient_id": patient_id, "medication_id": medication_id,
                                      "pill_count": pill_count, "capacity": capacity,
                                      "low_stock_threshold": low_stock_threshold, "by_user_id": by_user_id}))
        with self.db.session() as s:
            comp = self._comp(s, slot, patient_id)
            if medication_id is not _UNSET:
                comp.medication_id = medication_id
            cap = capacity if capacity is not None else comp.capacity
            count = pill_count if pill_count is not None else comp.pill_count
            if count > cap:
                raise ValidationError(f"Container {slot + 1} holds at most {cap} pills.")
            comp.capacity, comp.pill_count = cap, count
            if low_stock_threshold is not None:
                comp.low_stock_threshold = low_stock_threshold
            s.flush()
            return container_info(comp)

    def refill(self, slot: int, *, set: int | None = None, add: int | None = None,  # noqa: A002
               patient_id: int | None = None, by_user_id: int | None = None) -> ContainerInfo:
        self.calls.append(("refill", {"slot": slot, "set": set, "add": add, "patient_id": patient_id,
                                      "by_user_id": by_user_id}))
        with self.db.session() as s:
            comp = self._comp(s, slot, patient_id)
            new = set if set is not None else comp.pill_count + int(add or 0)
            if new > comp.capacity:
                raise ValidationError(f"Container {slot + 1} holds at most {comp.capacity} pills.")
            comp.pill_count = new
            s.flush()
            return container_info(comp)


def medication_view(m: Medication) -> dict[str, Any]:
    return {"medication_id": m.medication_id, "patient_id": m.user_id, "name": m.name, "strength": m.strength,
            "instructions_text": m.instructions_text, "warnings": list(m.warnings or []), "source": m.source,
            "confirmed_by_user": bool(m.confirmed_by_user), "confirmed_by": m.confirmed_by,
            "confirmed_at": _iso(m.confirmed_at), "active": bool(m.active), "slot": None,
            "compartment_number": None, "schedules": []}


class FakeCatalog:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.calls: list[tuple[str, Any]] = []

    def list(self, include_inactive: bool = False, *, patient_id: int | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(Medication).where(Medication.user_id == patient_id).order_by(Medication.medication_id)
            if not include_inactive:
                q = q.where(Medication.active.is_(True))
            return [medication_view(m) for m in s.scalars(q).all()]

    def create(self, fields: dict[str, Any], *, confirmed: bool, confirmed_by: str | None = None,
               source: str = "manual", scan_id: int | None = None, patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("create", {"fields": fields, "confirmed": confirmed, "confirmed_by": confirmed_by,
                                      "patient_id": patient_id}))
        if confirmed is not True:
            raise ValidationError("Medication information must be confirmed.")
        if not (fields.get("name") or "").strip():
            raise ValidationError("name is required.")
        with self.db.session() as s:
            m = Medication(user_id=patient_id, name=fields["name"], strength=fields.get("strength"),
                           instructions_text=fields.get("instructions_text"), warnings=fields.get("warnings") or [],
                           source=source, confirmed_by_user=True, confirmed_by=confirmed_by,
                           confirmed_at=datetime.now(timezone.utc))
            s.add(m)
            s.flush()
            return medication_view(m)

    def update(self, medication_id: int, fields: dict[str, Any], *, confirmed: bool,
               confirmed_by: str | None = None, patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("update", {"medication_id": medication_id, "fields": fields, "confirmed": confirmed,
                                      "patient_id": patient_id}))
        with self.db.session() as s:
            m = s.get(Medication, medication_id)
            if m is None or m.user_id != patient_id:
                raise NotFoundError(f"Medication {medication_id} not found.")
            if fields and confirmed is not True:
                raise ValidationError("Changes must be confirmed.")
            for k, v in fields.items():
                setattr(m, "instructions_text" if k == "instructions" else k, v)
            s.flush()
            return medication_view(m)

    def archive(self, medication_id: int, *, patient_id: int | None = None) -> None:
        self.calls.append(("archive", {"medication_id": medication_id, "patient_id": patient_id}))
        with self.db.session() as s:
            m = s.get(Medication, medication_id)
            if m is None or m.user_id != patient_id:
                raise NotFoundError(f"Medication {medication_id} not found.")
            m.active = False


def schedule_view(sc: Schedule) -> dict[str, Any]:
    return {"schedule_id": sc.schedule_id, "medication_id": sc.medication_id,
            "medication_name": sc.medication.name if sc.medication else None, "time_of_day": sc.time_of_day,
            "frequency": sc.frequency, "days_of_week": (sc.days_of_week or "").split(","), "active": bool(sc.active),
            "created_at": _iso(sc.created_at), "patient_id": sc.medication.user_id if sc.medication else None}


class FakeScheduler:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.ticks = 0
        self.calls: list[tuple[str, Any]] = []
        self.ticked = threading.Event()

    def tick(self) -> int:
        self.ticks += 1
        self.ticked.set()
        return 0

    def list_schedules(self, include_inactive: bool = False, *, patient_id: int | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = (select(Schedule).join(Medication, Medication.medication_id == Schedule.medication_id)
                 .where(Medication.user_id == patient_id).order_by(Schedule.time_of_day))
            if not include_inactive:
                q = q.where(Schedule.active.is_(True))
            return [schedule_view(sc) for sc in s.scalars(q).all()]

    def create_schedule(self, medication_id: int, time_of_day: str, frequency: str = "DAILY",
                        days_of_week: Any = None, *, created_by_user_id: int | None = None,
                        patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("create", {"medication_id": medication_id, "time_of_day": time_of_day,
                                      "frequency": frequency, "days_of_week": days_of_week,
                                      "created_by_user_id": created_by_user_id, "patient_id": patient_id}))
        hh, _, mm = time_of_day.partition(":")
        if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
            raise ValidationError("time_of_day must be HH:MM (24-hour), e.g. '08:00'.")
        with self.db.session() as s:
            sc = Schedule(medication_id=medication_id, time_of_day=time_of_day, frequency=frequency,
                          created_by_user_id=created_by_user_id)
            s.add(sc)
            s.flush()
            s.refresh(sc)
            return schedule_view(sc)

    def update_schedule(self, schedule_id: int, *, patient_id: int | None = None, by_user_id: int | None = None,
                        **fields: Any) -> dict[str, Any]:
        self.calls.append(("update", {"schedule_id": schedule_id, "patient_id": patient_id, **fields}))
        with self.db.session() as s:
            sc = s.get(Schedule, schedule_id)
            if sc is None:
                raise NotFoundError(f"Schedule {schedule_id} not found.")
            for k, v in fields.items():
                setattr(sc, k, ",".join(v) if k == "days_of_week" else v)
            s.flush()
            return schedule_view(sc)

    def delete_schedule(self, schedule_id: int, *, patient_id: int | None = None, by_user_id: int | None = None) -> None:
        self.update_schedule(schedule_id, patient_id=patient_id, by_user_id=by_user_id, active=False)


# =========================================================================== drops


def drop_view(d: PillDrop) -> dict[str, Any]:
    return {"drop_id": d.drop_id, "patient_id": d.patient_id, "requested_at": _iso(d.requested_at),
            "completed_at": _iso(d.completed_at), "requested_local": _iso(d.requested_at),
            "slot": d.slot_number, "container_number": None if d.slot_number is None else d.slot_number + 1,
            "medication_id": d.medication_id, "medication_name": d.medication_name, "source": d.source,
            "status": d.status, "reason": d.reason, "hardware_result": d.hardware_result,
            "pill_count_before": d.pill_count_before, "pill_count_after": d.pill_count_after,
            "dose_event_id": d.dose_event_id, "conversation_id": d.conversation_id,
            "requested_by_user_id": d.requested_by_user_id, "needs_review": bool(d.needs_review),
            "review_note": d.review_note}


def dose_view(e: DoseEvent) -> dict[str, Any]:
    return {"event_id": e.event_id, "schedule_id": e.schedule_id, "medication_id": e.medication_id,
            "medication_name": e.medication.name if e.medication else None, "slot": e.slot_number,
            "container_number": None if e.slot_number is None else e.slot_number + 1,
            "scheduled_at": _iso(e.scheduled_at), "scheduled_local": _iso(e.scheduled_at), "status": e.status,
            "drop_id": e.drop_id, "dispensed_at": _iso(e.dispensed_at), "dispense_source": e.dispense_source,
            "confirmed_taken_at": _iso(e.confirmed_taken_at), "missed_at": _iso(e.missed_at),
            "needs_review": bool(e.needs_review), "attempts": e.attempts, "hardware_result": e.hardware_result}


class FakeDrops:
    """DropServiceAPI fake: records every request; outcomes are scripted (default DROPPED)."""

    def __init__(self, db: Any, clock: Any, compartments: FakeCompartments, bus: Any = None) -> None:
        self.db = db
        self.clock = clock
        self.compartments = compartments
        self.bus = bus
        self.requests: list[dict[str, Any]] = []
        self.outcomes: deque[DropOutcome] = deque()
        self.run_calls = 0
        self.interrupts = 0
        self.recovered = 0
        self.next_scheduled: dict[str, Any] | None = None
        self.calls: list[tuple[str, Any]] = []
        self.ran = threading.Event()

    def request_drop(self, *, patient_id: int, source: str, slot: int | None = None,
                     medication_id: int | None = None, requested_by_user_id: int | None = None,
                     conversation_id: int | None = None, dose_event_id: int | None = None) -> DropOutcome:
        self.requests.append(dict(patient_id=patient_id, source=source, slot=slot, medication_id=medication_id,
                                  requested_by_user_id=requested_by_user_id, conversation_id=conversation_id,
                                  dose_event_id=dose_event_id))
        if self.outcomes:
            return self.outcomes.popleft()
        return DropOutcome(status="DROPPED", source=source, message="Vitamin C dropped from container 1.",
                           drop_id=99, slot=0 if slot is None else slot, medication_id=medication_id,
                           medication_name="Vitamin C (demo candy)", pill_count_after=19)

    def patient_status(self, patient_id: int) -> PatientStatus:
        with self.db.session() as s:
            u = s.get(User, patient_id)
            name = u.display_name if u else "?"
            last = s.scalars(select(PillDrop).where(PillDrop.patient_id == patient_id,
                                                    PillDrop.status.in_(("DROPPED", "UNCERTAIN")))
                             .order_by(PillDrop.drop_id.desc())).first()
            last_view = drop_view(last) if last else None
        return PatientStatus(patient_id=patient_id, display_name=name, now_local=self.clock.local_now(),
                             containers=tuple(self.compartments.containers(patient_id)), cooldown_minutes=60,
                             last_drop=last_view, next_scheduled=self.next_scheduled, device={"connected": True})

    def recent_drops(self, patient_id: int, *, days: int = 7, limit: int = 200,
                     status: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("recent_drops", {"patient_id": patient_id, "days": days, "limit": limit,
                                            "status": status}))
        with self.db.session() as s:
            q = select(PillDrop).where(PillDrop.patient_id == patient_id)
            if status is not None:
                q = q.where(PillDrop.status == status)
            rows = s.scalars(q.order_by(PillDrop.drop_id.desc()).limit(limit)).all()
            return [drop_view(d) for d in rows]

    def next_scheduled_dose(self, patient_id: int) -> dict[str, Any] | None:
        return self.next_scheduled

    def run_scheduled_drops(self) -> int:
        self.run_calls += 1
        self.ran.set()
        return 0

    def interrupt(self) -> bool:
        self.interrupts += 1
        return False

    def recover_on_startup(self) -> int:
        self.recovered += 1
        return 0

    def resolve_drop(self, drop_id: int, *, dropped: bool, note: str | None = None,
                     by_user_id: int | None = None, patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("resolve_drop", {"drop_id": drop_id, "dropped": dropped, "note": note,
                                            "by_user_id": by_user_id, "patient_id": patient_id}))
        with self.db.session() as s:
            d = s.get(PillDrop, drop_id)
            if d is None:
                raise NotFoundError(f"Drop {drop_id} not found.")
            if d.status != "UNCERTAIN" or not d.needs_review:
                raise ConflictError("This drop does not need a review.")
            d.needs_review, d.review_note = False, note
            d.status = "DROPPED" if dropped else "FAILED"
            s.flush()
            return drop_view(d)

    def skip_dose(self, event_id: int, *, note: str | None = None, by_user_id: int | None = None,
                  patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("skip_dose", {"event_id": event_id, "note": note, "by_user_id": by_user_id,
                                         "patient_id": patient_id}))
        with self.db.session() as s:
            e = s.get(DoseEvent, event_id)
            if e is None:
                raise NotFoundError(f"Dose {event_id} not found.")
            e.status, e.review_note = "CANCELLED", note
            s.flush()
            return dose_view(e)

    def list_doses(self, patient_id: int, local_date: date) -> list[dict[str, Any]]:
        self.calls.append(("list_doses", {"patient_id": patient_id, "local_date": local_date}))
        with self.db.session() as s:
            rows = s.scalars(select(DoseEvent).where(DoseEvent.user_id == patient_id)
                             .order_by(DoseEvent.scheduled_at)).all()
            return [dose_view(e) for e in rows if self.clock.to_local(e.scheduled_at).date() == local_date]

    def get_settings(self, patient_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            dev = _patient_device(s, patient_id)
            if dev is None:
                raise NotFoundError("No dispenser is linked to this patient.")
            return {"manual_cooldown_minutes": dev.manual_cooldown_minutes,
                    "auto_drop_enabled": bool(dev.auto_drop_enabled), "device_id": dev.device_id,
                    "num_slots": dev.num_slots}

    def update_settings(self, patient_id: int, *, manual_cooldown_minutes: int | None = None,
                        auto_drop_enabled: bool | None = None, by_user_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("update_settings", {"patient_id": patient_id, "manual_cooldown_minutes":
                                               manual_cooldown_minutes, "auto_drop_enabled": auto_drop_enabled,
                                               "by_user_id": by_user_id}))
        with self.db.session() as s:
            dev = _patient_device(s, patient_id)
            if dev is None:
                raise NotFoundError("No dispenser is linked to this patient.")
            if manual_cooldown_minutes is not None:
                dev.manual_cooldown_minutes = manual_cooldown_minutes
            if auto_drop_enabled is not None:
                dev.auto_drop_enabled = auto_drop_enabled
        return self.get_settings(patient_id)


# =========================================================================== agent / reports / onboarding


class FakeAgent:
    """AgentServiceAPI fake: 'vitamin c' requests a drop through the drops fake (source agent)."""

    def __init__(self, db: Any, drops: FakeDrops, bus: Any) -> None:
        self.db = db
        self.drops = drops
        self.bus = bus
        self.chats: list[dict[str, Any]] = []
        self.audio_store: dict[str, tuple[int, bytes]] = {}
        self.transcribe_result: dict[str, Any] | Exception = {"text": "drop my vitamin c", "confidence": 0.91,
                                                             "engine": "vosk"}
        self.transcribed: list[bytes] = []
        self._ids = itertools.count(1)

    def chat(self, *, patient_id: int, text: str, input_mode: str = "text",
             conversation_id: int | None = None) -> AgentReply:
        self.chats.append({"patient_id": patient_id, "text": text, "input_mode": input_mode,
                           "conversation_id": conversation_id})
        actions: list[dict[str, Any]] = []
        with self.db.session() as s:
            conv = s.get(Conversation, conversation_id) if conversation_id else None
            if conv is None or conv.patient_id != patient_id:
                conv = Conversation(patient_id=patient_id, channel=input_mode)
                s.add(conv)
                s.flush()
            cid = conv.conversation_id
            s.add(ConversationMessage(conversation_id=cid, patient_id=patient_id, role="user", content=text,
                                      input_mode=input_mode))
        reply = "How can I help?"
        if "vitamin c" in text.lower():
            outcome = self.drops.request_drop(patient_id=patient_id, source="agent", medication_id=None,
                                              conversation_id=cid)
            actions.append(outcome.to_dict())
            reply = outcome.message
        with self.db.session() as s:
            msg = ConversationMessage(conversation_id=cid, patient_id=patient_id, role="assistant", content=reply,
                                      model="rules")
            s.add(msg)
            s.flush()
            mid = msg.message_id
        self.bus.publish(Topic.AGENT, {"patient_id": patient_id, "conversation_id": cid, "message_id": mid,
                                       "role": "assistant"})
        return AgentReply(conversation_id=cid, text=reply, model="rules", actions=actions)

    def conversations(self, patient_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(Conversation).where(Conversation.patient_id == patient_id)
                             .order_by(Conversation.conversation_id.desc()).limit(limit)).all()
            return [{"conversation_id": c.conversation_id, "started_at": _iso(c.started_at),
                     "last_message_at": _iso(c.last_message_at), "channel": c.channel, "title": c.title,
                     "message_count": len(c.messages)} for c in rows]

    def messages(self, patient_id: int, conversation_id: int) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.patient_id == patient_id).order_by(ConversationMessage.message_id)).all()
            return [{"message_id": m.message_id, "conversation_id": m.conversation_id, "role": m.role,
                     "content": m.content, "input_mode": m.input_mode, "tool_name": m.tool_name,
                     "tool_args": m.tool_args, "tool_result": m.tool_result, "created_at": _iso(m.created_at)}
                    for m in rows]

    def speak(self, patient_id: int, text: str) -> str | None:
        audio_id = f"a{next(self._ids)}"
        self.audio_store[audio_id] = (patient_id, b"RIFF....WAVEfake")
        return audio_id

    def audio(self, audio_id: str, patient_id: int) -> bytes | None:
        item = self.audio_store.get(audio_id)
        return item[1] if item and item[0] == patient_id else None

    def transcribe(self, pcm16: bytes) -> dict[str, Any]:
        self.transcribed.append(pcm16)
        if isinstance(self.transcribe_result, Exception):
            raise self.transcribe_result
        return dict(self.transcribe_result)


class FakeReports:
    def __init__(self, db: Any, bus: Any) -> None:
        self.db = db
        self.bus = bus
        self.sent: list[dict[str, Any]] = []

    def _meta(self, r: Report, deliveries: list[ReportDelivery] | None = None) -> dict[str, Any]:
        return {"report_id": r.report_id, "patient_id": r.patient_id, "title": r.title, "days": r.days,
                "period_start": _iso(r.period_start), "period_end": _iso(r.period_end), "status": r.status,
                "pdf_size": r.pdf_size, "created_at": _iso(r.created_at), "created_by_user_id": r.created_by_user_id,
                "stats": dict(r.stats or {}), "narrative": r.narrative, "narrative_source": r.narrative_source,
                "pdf_url": f"/api/reports/{r.report_id}/pdf",
                "deliveries": [{"delivery_id": d.delivery_id, "to_email": d.to_email, "status": d.status,
                                "error": d.error, "created_at": _iso(d.created_at)} for d in (deliveries or [])]}

    def generate(self, *, patient_id: int, days: int, created_by_user_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        pdf = b"%PDF-1.4 fake report"
        with self.db.session() as s:
            r = Report(patient_id=patient_id, created_by_user_id=created_by_user_id, days=days,
                       period_start=now - timedelta(days=days), period_end=now, title=f"Report {days} days",
                       stats={"adherence_rate": 0.9}, narrative="All good.", narrative_source="rules",
                       pdf=pdf, pdf_size=len(pdf))
            s.add(r)
            s.flush()
            meta = self._meta(r)
        self.bus.publish(Topic.REPORT, {"patient_id": patient_id, "report_id": meta["report_id"], "status": "READY"})
        return meta

    def list(self, patient_id: int) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(Report).where(Report.patient_id == patient_id)
                             .order_by(Report.report_id.desc())).all()
            return [self._meta(r) for r in rows]

    def get(self, report_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            r = s.get(Report, report_id)
            if r is None:
                raise NotFoundError(f"Report {report_id} not found.")
            ds = list(s.scalars(select(ReportDelivery).where(ReportDelivery.report_id == report_id)))
            return self._meta(r, ds)

    def pdf_bytes(self, report_id: int) -> bytes:
        with self.db.session() as s:
            r = s.get(Report, report_id)
            if r is None:
                raise NotFoundError(f"Report {report_id} not found.")
            return bytes(r.pdf or b"")

    def send(self, report_id: int, *, sent_by_user_id: int, to_email: str | None = None) -> dict[str, Any]:
        self.sent.append({"report_id": report_id, "sent_by_user_id": sent_by_user_id, "to_email": to_email})
        with self.db.session() as s:
            d = ReportDelivery(report_id=report_id, to_email=to_email or "dr.lee@test.tactidose",
                               sent_by_user_id=sent_by_user_id, status="SAVED")
            s.add(d)
            s.flush()
            return {"deliveries": [{"delivery_id": d.delivery_id, "to_email": d.to_email, "status": d.status,
                                    "error": None, "created_at": _iso(d.created_at)}]}


class FakeOnboarding:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.calls: list[tuple[str, Any]] = []

    def scan(self, image: bytes, mime_type: str, *, patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("scan", {"bytes": len(image), "mime": mime_type, "patient_id": patient_id}))
        with self.db.session() as s:
            row = LabelScan(user_id=patient_id, status="PENDING_REVIEW", model="fake",
                            extracted={"medication_name": "Vitamin C (demo candy)"})
            s.add(row)
            s.flush()
            return {"scan_id": row.scan_id, "status": row.status, "extracted": row.extracted, "model": row.model,
                    "error": None, "user_message": None, "created_at": _iso(row.created_at), "reviewed_at": None,
                    "medication_id": None}

    def confirm_scan(self, scan_id: int, fields: dict[str, Any], *, confirmed: bool,
                     confirmed_by: str | None = None, patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("confirm", {"scan_id": scan_id, "fields": fields, "patient_id": patient_id}))
        return {"medication_id": 1000 + scan_id, "name": fields.get("name"), "confirmed_by": confirmed_by}

    def reject_scan(self, scan_id: int, *, by: str | None = None, patient_id: int | None = None) -> dict[str, Any]:
        self.calls.append(("reject", {"scan_id": scan_id, "by": by, "patient_id": patient_id}))
        return {"scan_id": scan_id, "status": "REJECTED"}


class FakeSim:
    def __init__(self) -> None:
        self.faults_on: dict[str, bool] = {"motor_jam": False, "disconnect": False}
        self.pressed: list[str] = []
        self.reboots = 0
        self.pills: dict[int, int] = {0: 20, 1: 20, 2: 20}

    def physical(self) -> dict[str, Any]:
        return {"slot": 0, "gate_open": False, "state": "READY", "pills": dict(self.pills)}

    def faults(self) -> dict[str, bool]:
        return dict(self.faults_on)

    def set_fault(self, name: str, enabled: bool) -> None:
        if name not in self.faults_on:
            raise ValueError(f"fault must be one of {tuple(self.faults_on)}, got {name!r}")
        self.faults_on[name] = enabled

    def press(self, name: str, duration_ms: int = 100) -> None:
        if name.upper() not in ("CONFIRM", "CANCEL"):
            raise ValueError(f"button must be CONFIRM or CANCEL, got {name!r}")
        self.pressed.append(name.upper())

    def reboot(self) -> None:
        self.reboots += 1

    def set_pills(self, slot: int, count: int) -> None:
        self.pills[slot] = count


# =========================================================================== harness


@dataclass
class Harness:
    services: Any
    app: Any
    client: Any
    ids: dict[str, Any]
    tokens: dict[str, str] = field(default_factory=dict)
    _portal_cm: Any = None

    def close(self) -> None:
        """Stop the client's event-loop thread (see :func:`make_harness`)."""
        cm, self._portal_cm = self._portal_cm, None
        self.client.portal = None
        if cm is not None:
            cm.__exit__(None, None, None)
        self.client.close()

    def h(self, actor: str | None) -> dict[str, str]:
        """``Authorization`` header for an actor (None = anonymous)."""
        return {} if actor is None else {"Authorization": f"Bearer {self.tokens[actor]}"}

    def uid(self, actor: str) -> int:
        return int(self.ids[f"{actor}_id"])

    @property
    def pid(self) -> int:
        return int(self.ids["patient_id"])

    def _headers(self, actor: str | None, kw: dict[str, Any]) -> dict[str, str]:
        return {**self.h(actor), **(kw.pop("headers", None) or {})}

    def get(self, path: str, actor: str | None = "patient", **kw: Any) -> Any:
        return self.client.get(path, headers=self._headers(actor, kw), **kw)

    def post(self, path: str, actor: str | None = "patient", **kw: Any) -> Any:
        return self.client.post(path, headers=self._headers(actor, kw), **kw)

    def request(self, method: str, path: str, actor: str | None = "patient", **kw: Any) -> Any:
        return self.client.request(method, path, headers=self._headers(actor, kw), **kw)

    @property
    def fakes(self) -> Any:
        return self.services


def seed_api(db: Any, settings: Any) -> dict[str, Any]:
    """seed_v2 + another patient, an unlinked doctor, one UNCERTAIN drop, one dose event, one
    conversation, one label scan and a few notifications."""
    ids = seed_v2(db, settings)
    now = datetime.now(timezone.utc)
    with db.session() as s:
        other = User(display_name="Pat Other", role="patient", email="pat@test.tactidose", link_code="PATCODE1")
        stranger = User(display_name="Dr. Stranger", role="doctor", email="stranger@test.tactidose")
        s.add_all([other, stranger])
        s.flush()
        ids["other_id"], ids["stranger_id"] = other.user_id, stranger.user_id
        drop = PillDrop(patient_id=ids["patient_id"], device_id=ids["device_id"], slot_number=0,
                        medication_id=ids["med_ids"][0], medication_name="Vitamin C (demo candy)", source="schedule",
                        status="UNCERTAIN", needs_review=True, requested_at=now - timedelta(hours=1))
        s.add(drop)
        s.flush()
        ids["drop_id"] = drop.drop_id
        ev = DoseEvent(schedule_id=ids["schedule_ids"][1], medication_id=ids["med_ids"][1],
                       user_id=ids["patient_id"], device_id=ids["device_id"],
                       scheduled_at=datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc), status="SCHEDULED")
        s.add(ev)
        s.flush()
        ids["event_id"] = ev.event_id
        conv = Conversation(patient_id=ids["patient_id"], channel="text", title="Morning")
        s.add(conv)
        s.flush()
        s.add(ConversationMessage(conversation_id=conv.conversation_id, patient_id=ids["patient_id"], role="user",
                                  content="Can I have my pill?", input_mode="text"))
        ids["conversation_id"] = conv.conversation_id
        other_conv = Conversation(patient_id=other.user_id, channel="text")
        s.add(other_conv)
        s.flush()
        ids["other_conversation_id"] = other_conv.conversation_id
        scan = LabelScan(user_id=ids["patient_id"], status="PENDING_REVIEW", model="fake")
        s.add(scan)
        s.flush()
        ids["scan_id"] = scan.scan_id
        s.add_all([
            Notification(user_id=ids["patient_id"], patient_id=ids["patient_id"], kind="PILL_DROPPED",
                         title="Pill dropped"),
            Notification(user_id=ids["doctor_id"], patient_id=ids["patient_id"], kind="LOW_STOCK",
                         title="Low stock"),
            Notification(user_id=ids["doctor_id"], patient_id=ids["patient_id"], kind="PILL_DROPPED",
                         title="Pill dropped"),
        ])
    return ids


_SHARED_APP: Any = None


def shared_app(services: Any) -> Any:
    """One FastAPI app per test process (building one costs ~75 ms); routes and pages read
    ``request.app.state.services``, so each test just swaps in its own services. Tests of the
    lifespan build their own app with ``create_app``."""
    global _SHARED_APP
    from tactidose.app import create_app

    if _SHARED_APP is None:
        _SHARED_APP = create_app(services=services)
    _SHARED_APP.state.services = services
    return _SHARED_APP


def make_harness(settings: Any, db: Any, clock: Any, bus: Any, *, hardware: Any = None, sim: Any = None,
                 **overrides: Any) -> Harness:
    from tactidose.app import build_services

    ids = seed_api(db, settings)
    auth = FakeAuth(db, settings)
    comps = FakeCompartments(db)
    drops = FakeDrops(db, clock, comps, bus)
    kwargs: dict[str, Any] = dict(
        clock=clock, bus=bus, db=db, hardware=hardware or FakeDropHardware(), sim=sim if sim is not None else FakeSim(),
        notifications=FakeNotifications(db, bus), compartments=comps, catalog=FakeCatalog(db),
        scheduler=FakeScheduler(db), drops=drops, auth=auth, agent=FakeAgent(db, drops, bus),
        reports=FakeReports(db, bus), onboarding=FakeOnboarding(db),
    )
    kwargs.update(overrides)
    services = build_services(settings, **kwargs)
    services.sse_max_stream_s = 0.15  # TestClient buffers whole responses: keep streams short
    app = shared_app(services)
    client = TestClient(app)
    # Without a portal the TestClient starts a new event-loop thread for every request (~30 ms on
    # Windows). Keep one per harness — without running the lifespan (tests drive the loop themselves).
    portal_cm = anyio.from_thread.start_blocking_portal(**client.async_backend)
    client.portal = portal_cm.__enter__()
    tokens: dict[str, str] = {}
    with db.session() as s:
        emails = {u.user_id: u.email for u in s.scalars(select(User))}
    for actor in ACTORS:
        uid = ids[f"{actor}_id"]
        auth.set_password(emails[uid])
        tokens[actor] = auth.issue(uid)
    return Harness(services=services, app=app, client=client, ids=ids, tokens=tokens, _portal_cm=portal_cm)


@pytest.fixture
def api_settings(settings_v2: Any) -> Any:
    """settings_v2 without the demo seed (the tests seed with seed_v2) and a slow scheduler loop."""
    return settings_v2.model_copy(update={"seed_demo_accounts": False, "scheduler_tick_s": 600.0})


@pytest.fixture
def api(api_settings: Any, db_v2: Any, clock: Any, bus: Any) -> Iterator[Harness]:
    h = make_harness(api_settings, db_v2, clock, bus)
    try:
        yield h
    finally:
        h.close()


@pytest.fixture
def make_api(api_settings: Any, db_v2: Any, clock: Any, bus: Any) -> Iterator[Any]:
    """Factory ``make_api(settings=None, **overrides) -> Harness``; every harness is closed afterwards."""
    made: list[Harness] = []

    def factory(settings: Any = None, **overrides: Any) -> Harness:
        h = make_harness(settings if settings is not None else api_settings, db_v2, clock, bus, **overrides)
        made.append(h)
        return h

    try:
        yield factory
    finally:
        for h in made:
            h.close()
