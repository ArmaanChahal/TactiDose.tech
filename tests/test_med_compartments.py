"""CompartmentService v2: containers as ContainerInfo, assignment (doctor/family), refills and
thresholds within 0..capacity, loaded_at stamping, device bootstrap and binding to a patient."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from tactidose.core.bus import Topic
from tactidose.core.interfaces import ContainerInfo
from tactidose.db.models import Compartment, Device, Medication, User
from tactidose.medication.compartments import (
    DEFAULT_USER_NAME,
    CompartmentService,
    compartment_to_dict,
    ensure_device_rows,
    is_real_patient,
)
from tactidose.medication.errors import NotFoundError, ValidationError
from tests.test_med_support import Env, env, env_template  # noqa: F401 - fixtures

CONTAINER_KEYS = {"slot", "container_number", "compartment_id", "medication_id", "medication_name", "strength",
                  "pill_count", "capacity", "low_stock_threshold", "low_stock", "empty", "loaded_at"}


def _count(db, model) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def _raw_med(e: Env, **kw: Any) -> int:
    with e.db.session() as s:
        row = Medication(**{"user_id": e.patient, "name": "Raw", "confirmed_by_user": True, **kw})
        s.add(row)
        s.flush()
        return row.medication_id


def _user(e_db, **kw: Any) -> int:
    with e_db.session() as s:
        u = User(**kw)
        s.add(u)
        s.flush()
        return u.user_id


# --------------------------------------------------------------------------- queries


def test_containers_are_container_infos_for_the_patient(env: Env):
    infos = env.compartments.containers(env.patient)
    assert all(isinstance(c, ContainerInfo) for c in infos) and [c.slot for c in infos] == [0, 1, 2]
    assert [(c.medication_name, c.pill_count, c.capacity, c.low_stock_threshold) for c in infos] == [
        ("Vitamin C (demo candy)", 20, 30, 3), ("Calcium (demo token)", 20, 30, 3), ("Omega-3 (demo candy)", 20, 30, 3)]
    rows = env.compartments.list(env.patient)
    assert all(set(r) == CONTAINER_KEYS for r in rows) and rows[1]["container_number"] == 2
    assert env.compartments.list() == rows                                  # default: the device's patient
    assert env.compartments.get(2, patient_id=env.patient).medication_id == env.med(2)
    assert env.compartments.containers(env.family) == []                    # no dispenser
    with pytest.raises(ValidationError):
        env.compartments.get(3)


def test_compartment_to_dict_adds_active(env: Env):
    with env.db.session() as s:
        comp = s.scalars(select(Compartment).where(Compartment.slot_number == 0)).one()
        d = compartment_to_dict(comp)
    assert set(d) == CONTAINER_KEYS | {"active"} and d["active"] is True and d["empty"] is False


# --------------------------------------------------------------------------- assignment


def test_assign_moves_and_replaces_and_resets_counts(env: Env):
    sub = env.subscribe(Topic.DATA_CHANGED, Topic.PATIENT_STATUS)
    info = env.compartments.assign(0, env.med(2), patient_id=env.patient, by_user_id=env.doctor)
    assert (info.slot, info.medication_id, info.pill_count, info.loaded_at) == (0, env.med(2), 0, None)
    assert env.compartment(2).medication_id is None and env.compartment(2).pill_count == 0   # moved away
    rows = env.compartments.list(env.patient)
    assert [r["medication_id"] for r in rows] == [env.med(2), env.med(1), None]
    log = env.devlog("COMPARTMENT_ASSIGNED")[-1].detail
    assert log == {"slot": 0, "by_user_id": env.doctor, "medication_id": env.med(2),
                   "previous_medication_id": env.med(0), "moved_from_slots": [2]}
    published = [e.data for e in sub.drain()]
    assert {"entity": "compartment", "id": 0, "patient_id": env.patient} in published
    assert {"patient_id": env.patient, "reason": "inventory"} in published
    # a count can be loaded in the same update; loaded_at is stamped
    info = env.compartments.assign(2, env.med(0), patient_id=env.patient, pill_count=12)
    assert (info.pill_count, info.loaded_at) == (12, env.clock.now())
    unassigned = env.compartments.assign(2, None, patient_id=env.patient)
    assert (unassigned.medication_id, unassigned.pill_count) == (None, 0)
    same = env.compartments.assign(1, env.med(1), patient_id=env.patient)   # same medication: count kept
    assert same.pill_count == 20


@pytest.mark.parametrize("slot", [-1, 3, True, "1", None])
def test_assign_validates_the_slot(env: Env, slot):
    with pytest.raises(ValidationError):
        env.compartments.assign(slot, env.med(0))  # type: ignore[arg-type]


def test_assign_validates_the_medication(env: Env):
    with pytest.raises(ValidationError):
        env.compartments.assign(1, "3")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        env.compartments.assign(1, 9999)
    with pytest.raises(ValidationError):
        env.compartments.assign(1, _raw_med(env, confirmed_by_user=False))
    with pytest.raises(ValidationError):
        env.compartments.assign(1, _raw_med(env, active=False))
    other = _user(env.db, display_name="Pat Two", role="patient", email="pat2@test.tactidose")
    with pytest.raises(ValidationError):
        env.compartments.assign(1, _raw_med(env, user_id=other))
    assert env.compartment(1).medication_id == env.med(1)                  # unchanged after every refusal


def test_container_operations_need_the_patients_dispenser(env: Env):
    other = _user(env.db, display_name="Pat Two", role="patient", email="pat2@test.tactidose")
    with pytest.raises(NotFoundError):
        env.compartments.assign(0, None, patient_id=other)
    with pytest.raises(NotFoundError):
        env.compartments.refill(0, add=1, patient_id=other)
    with env.db.session() as s:      # a second unit (not this system's) without container rows
        s.add(Device(device_id="far-away-unit", user_id=other, name="Elsewhere", num_slots=3))
    assert env.compartments.containers(other) == []
    with pytest.raises(NotFoundError):
        env.compartments.refill(1, add=1, patient_id=other)


# --------------------------------------------------------------------------- inventory edits


def test_update_capacity_threshold_and_count_together(env: Env):
    info = env.compartments.update(0, patient_id=env.patient, capacity=40, low_stock_threshold=5, pill_count=35,
                                   by_user_id=env.family)
    assert (info.capacity, info.low_stock_threshold, info.pill_count) == (40, 5, 35)
    assert info.loaded_at == env.clock.now()                               # count went up: loaded now
    env.advance(minutes=5)
    lower = env.compartments.update(0, patient_id=env.patient, pill_count=4)
    assert (lower.pill_count, lower.low_stock, lower.loaded_at) == (4, True, env.clock.now() - timedelta(minutes=5))
    assert env.devlog("CONTAINER_UPDATED")[-1].detail == {"slot": 0, "by_user_id": None, "pill_count": 4}


@pytest.mark.parametrize("kw", [
    dict(pill_count=-1), dict(pill_count=31), dict(pill_count="5"), dict(pill_count=True),
    dict(capacity=0), dict(capacity=501), dict(capacity=19), dict(low_stock_threshold=-1),
    dict(low_stock_threshold=101), dict(capacity=10, pill_count=11), dict(medication_id=True),
])
def test_update_validation_is_all_or_nothing(env: Env, kw):
    with pytest.raises(ValidationError):
        env.compartments.update(0, patient_id=env.patient, **kw)
    comp = env.compartment(0)
    assert (comp.pill_count, comp.capacity, comp.low_stock_threshold, comp.medication_id) == (20, 30, 3, env.med(0))


def test_refill_set_and_add(env: Env):
    sub = env.subscribe(Topic.PATIENT_STATUS)
    info = env.compartments.refill(1, add=5, patient_id=env.patient, by_user_id=env.family)
    assert (info.pill_count, info.loaded_at) == (25, env.clock.now())
    env.advance(minutes=1)
    info = env.compartments.refill(1, set=10, patient_id=env.patient)
    assert (info.pill_count, info.loaded_at) == (10, env.clock.now() - timedelta(minutes=1))   # lowered: not a load
    info = env.compartments.refill(1, set=30, patient_id=env.patient)
    assert (info.pill_count, info.loaded_at) == (30, env.clock.now())
    assert env.compartments.refill(1, set=0, patient_id=env.patient).empty
    log = env.devlog("CONTAINER_REFILLED")
    assert [(r.detail["before"], r.detail["after"]) for r in log] == [(20, 25), (25, 10), (10, 30), (30, 0)]
    assert log[0].detail["by_user_id"] == env.family
    assert sub.drain()[0].data == {"patient_id": env.patient, "reason": "inventory"}


@pytest.mark.parametrize("kw", [dict(), dict(set=5, add=5), dict(set=-1), dict(set=31), dict(add=0), dict(add=11),
                                dict(add=-3), dict(set="5"), dict(add=True)])
def test_refill_validation(env: Env, kw):
    with pytest.raises(ValidationError):
        env.compartments.refill(0, patient_id=env.patient, **kw)
    assert env.compartment(0).pill_count == 20


def test_refill_after_drops_uses_the_current_count(env: Env):
    env.set_cooldown(0)
    env.manual(0)
    env.manual(0)
    assert env.compartments.refill(0, add=10, patient_id=env.patient).pill_count == 28


# --------------------------------------------------------------------------- bootstrap & binding


def test_ensure_device_bootstraps_defaults(settings_v2, db_v2, bus):
    svc = CompartmentService(db_v2, settings_v2, bus=bus)
    sub = bus.subscribe([Topic.DATA_CHANGED])
    out = svc.ensure_device()
    with db_v2.session() as s:
        users = s.scalars(select(User)).all()
        assert [(u.display_name, u.role, u.email) for u in users] == [(DEFAULT_USER_NAME, "patient", None)]
        dev = s.get(Device, settings_v2.device_id)
        assert (dev.num_slots, dev.user_id, dev.manual_cooldown_minutes, dev.auto_drop_enabled) == (
            3, users[0].user_id, 60, True)
        comps = s.scalars(select(Compartment).order_by(Compartment.slot_number)).all()
        assert [(c.slot_number, c.active, c.medication_id, c.pill_count, c.capacity, c.low_stock_threshold)
                for c in comps] == [(i, True, None, 0, 30, 3) for i in range(3)]
    assert out["bound"] is True and out["patient_id"] == users[0].user_id and "DEVICE_CREATED" in out["changes"]
    assert sub.drain() and svc.user_id() == users[0].user_id and svc.device_id == settings_v2.device_id
    assert svc.ensure_device()["changes"] == [] and sub.drain() == []            # idempotent
    assert _count(db_v2, Compartment) == 3 and _count(db_v2, User) == 1


def test_bootstrap_prefers_a_registered_patient(settings_v2, db_v2):
    _user(db_v2, display_name="Dr. First", role="doctor", email="dr@test.tactidose")
    alex = _user(db_v2, display_name="Alex", role="patient", email="alex@test.tactidose")
    out = CompartmentService(db_v2, settings_v2).ensure_device()
    assert out["patient_id"] == alex and _count(db_v2, User) == 2


def test_ensure_device_binds_a_placeholder_device_to_a_registering_patient(settings_v2, db_v2):
    svc = CompartmentService(db_v2, settings_v2)
    placeholder = svc.ensure_device()["patient_id"]
    with db_v2.session() as s:
        med = Medication(user_id=placeholder, name="Placeholder's", confirmed_by_user=True)
        s.add(med)
        s.flush()
        s.execute(Compartment.__table__.update().where(Compartment.__table__.c.slot_number == 0)
                  .values(medication_id=med.medication_id, pill_count=7))
    alex = _user(db_v2, display_name="Alex", role="patient", email="alex@test.tactidose")
    out = svc.ensure_device(alex)
    assert out["bound"] is True and out["patient_id"] == alex
    assert out["changes"] == ["DEVICE_BOUND", "COMPARTMENTS_CLEARED"]
    with db_v2.session() as s:
        comp = s.scalars(select(Compartment).where(Compartment.slot_number == 0)).one()
        assert (comp.medication_id, comp.pill_count) == (None, 0)
    sam = _user(db_v2, display_name="Sam", role="patient", email="sam@test.tactidose")
    taken = svc.ensure_device(sam)                         # Alex is a real patient: never stolen
    assert taken["bound"] is False and taken["patient_id"] == alex


def test_ensure_device_creates_the_device_for_a_patient(settings_v2, db_v2):
    alex = _user(db_v2, display_name="Alex", role="patient", email="alex@test.tactidose")
    out = CompartmentService(db_v2, settings_v2).ensure_device(alex)
    assert out["patient_id"] == alex and out["bound"] and _count(db_v2, User) == 1


def test_ensure_device_validates_the_patient(settings_v2, db_v2):
    svc = CompartmentService(db_v2, settings_v2)
    with pytest.raises(NotFoundError):
        svc.ensure_device(4242)
    doctor = _user(db_v2, display_name="Dr. Lee", role="doctor", email="dr@test.tactidose")
    with pytest.raises(ValidationError):
        svc.ensure_device(doctor)
    assert _count(db_v2, Device) == 0


def test_is_real_patient():
    assert is_real_patient(User(display_name="A", role="patient", email="a@x"))
    assert not is_real_patient(User(display_name="A", role="patient", email=None))
    assert not is_real_patient(User(display_name="A", role="family", email="a@x"))
    assert not is_real_patient(None)


def test_slot_count_shrinks_and_grows_without_losing_assignments(env: Env):
    small = CompartmentService(env.db, env.settings.model_copy(update={"num_slots": 2}))
    small.ensure_device()
    assert [c["slot"] for c in small.list()] == [0, 1]
    c2 = env.compartment(2)
    assert c2.active is False and c2.medication_id == env.med(2)            # deactivated, assignment kept
    big = CompartmentService(env.db, env.settings.model_copy(update={"num_slots": 4}))
    big.ensure_device()
    rows = big.list()
    assert [c["slot"] for c in rows] == [0, 1, 2, 3]
    assert rows[2]["medication_id"] == env.med(2) and rows[3]["capacity"] == 30
    with env.db.session() as s:
        assert s.get(Device, env.settings.device_id).num_slots == 4
        dev, changes = ensure_device_rows(s, env.settings.model_copy(update={"num_slots": 4}))
        assert changes == []
