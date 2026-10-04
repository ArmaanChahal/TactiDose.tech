"""MedicationCatalog (confirmed records only, validation, archive cascade) and CompartmentService."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from tactidose.core.bus import Topic
from tactidose.core.interfaces import IntentSource
from tactidose.db.models import Compartment, Device, Medication, Schedule, User
from tactidose.medication.catalog import MedicationCatalog
from tactidose.medication.compartments import DEFAULT_USER_NAME, CompartmentService
from tactidose.medication.errors import NotFoundError, ValidationError
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    MISSED,
    Med,
    med,
    med_template,
)

MEDICATION_KEYS = {"medication_id", "name", "strength", "instructions_text", "warnings", "source",
                   "confirmed_by_user", "confirmed_by", "confirmed_at", "active", "slot",
                   "compartment_number", "schedules"}
COMPARTMENT_KEYS = {"slot", "compartment_number", "compartment_id", "medication_id", "medication_name",
                    "active", "loaded_at"}


def _count(m_db, model) -> int:
    with m_db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def _raw_med(m: Med, **kw: Any) -> int:
    with m.db.session() as s:
        row = Medication(**{"user_id": m.ids["user_id"], "name": "Raw", "confirmed_by_user": True, **kw})
        s.add(row)
        s.flush()
        return row.medication_id


# --------------------------------------------------------------------------- catalog: create


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
def test_create_requires_explicit_confirmation(med: Med, confirmed):
    before = _count(med.db, Medication)
    with pytest.raises(ValidationError):
        med.catalog.create({"name": "Zinc (demo)"}, confirmed=confirmed)
    assert _count(med.db, Medication) == before


def test_create_normalises_and_returns_api_shape(med: Med):
    sub = med.subscribe(Topic.DATA_CHANGED)
    out = med.catalog.create(
        {"name": "  Zinc (demo token) ", "strength": " 2 tokens ", "instructions": " Chew slowly. ",
         "warnings": [" Demo only ", "", "Not a medication"], "confirmed": True, "confirmed_by": "x"},
        confirmed=True, confirmed_by=" Sam ",
    )
    assert set(out) == MEDICATION_KEYS
    assert out["name"] == "Zinc (demo token)" and out["strength"] == "2 tokens"
    assert out["instructions_text"] == "Chew slowly." and out["warnings"] == ["Demo only", "Not a medication"]
    assert out["source"] == "manual" and out["confirmed_by_user"] is True and out["confirmed_by"] == "Sam"
    assert out["confirmed_at"] == med.clock.now().isoformat() and out["active"] is True
    assert out["slot"] is None and out["compartment_number"] is None and out["schedules"] == []
    assert {"entity": "medication", "id": out["medication_id"]} in [e.data for e in sub.drain()]
    assert med.devlog("MEDICATION_CREATED")[-1].detail["medication_id"] == out["medication_id"]


@pytest.mark.parametrize("fields", [
    {}, {"name": ""}, {"name": "   "}, {"name": None}, {"name": 5}, {"name": "x" * 201},
    {"name": "ok", "strength": "x" * 121}, {"name": "ok", "strength": 2},
    {"name": "ok", "instructions_text": "x" * 2001},
    {"name": "ok", "warnings": "Demo only"}, {"name": "ok", "warnings": ["w"] * 21},
    {"name": "ok", "warnings": [3]}, {"name": "ok", "warnings": ["x" * 501]},
    {"name": "ok", "dosage": "take two"},
])
def test_create_rejects_invalid_fields(med: Med, fields):
    with pytest.raises(ValidationError):
        med.catalog.create(fields, confirmed=True)


def test_create_accepts_boundaries(med: Med):
    out = med.catalog.create({"name": "n" * 200, "strength": "s" * 120, "instructions_text": "i" * 2000,
                              "warnings": [f"w{i}" for i in range(20)]}, confirmed=True)
    assert len(out["name"]) == 200 and len(out["warnings"]) == 20


def test_create_rejects_non_mapping_bad_source_and_unknown_scan(med: Med):
    with pytest.raises(ValidationError):
        med.catalog.create("Zinc", confirmed=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        med.catalog.create({"name": "Zinc"}, confirmed=True, source="gemini_autosave")
    with pytest.raises(NotFoundError):
        med.catalog.create({"name": "Zinc"}, confirmed=True, source="label_scan", scan_id=999)


def test_create_bootstraps_device_on_empty_database(settings, clock, db):
    catalog = MedicationCatalog(db, settings, clock)
    assert catalog.list() == []                      # no device yet
    out = catalog.create({"name": "First token"}, confirmed=True)
    assert [m["medication_id"] for m in catalog.list()] == [out["medication_id"]]
    with db.session() as s:
        assert s.scalars(select(User.display_name)).all() == [DEFAULT_USER_NAME]
        assert s.get(Device, settings.device_id) is not None


# --------------------------------------------------------------------------- catalog: update / archive / list


def test_update_requires_confirmation_and_restamps(med: Med):
    original = med.catalog.get(med.med1)
    med.clock.advance(timedelta(minutes=10))
    with pytest.raises(ValidationError):
        med.catalog.update(med.med1, {"strength": "2 pieces"}, confirmed=False)
    assert med.catalog.get(med.med1)["strength"] == "1 piece"
    out = med.catalog.update(med.med1, {"strength": "2 pieces", "warnings": ["Demo"]},
                             confirmed=True, confirmed_by="nurse")
    assert out["strength"] == "2 pieces" and out["warnings"] == ["Demo"] and out["confirmed_by"] == "nurse"
    assert out["confirmed_at"] == med.clock.now().isoformat() != original["confirmed_at"]
    assert out["name"] == original["name"]           # untouched fields stay
    assert med.catalog.update(med.med1, {}, confirmed=False) == out          # nothing to change
    with pytest.raises(ValidationError):
        med.catalog.update(med.med1, {"name": ""}, confirmed=True)
    with pytest.raises(NotFoundError):
        med.catalog.update(99999, {"name": "x"}, confirmed=True)


def test_list_and_get_shape(med: Med):
    rows = med.catalog.list()
    assert [r["name"] for r in rows] == ["Calcium (demo token)", "Vitamin C (demo candy)"]
    vit = next(r for r in rows if r["medication_id"] == med.med1)
    assert set(vit) == MEDICATION_KEYS and vit["slot"] == 2 and vit["compartment_number"] == 3
    assert [s["time_of_day"] for s in vit["schedules"]] == ["08:00", "20:00"]
    assert med.catalog.get(med.med2)["slot"] == 4
    with pytest.raises(NotFoundError):
        med.catalog.get(12345)


def test_archive_cascade(med: Med):
    accessed = med.dose.dispense_next(IntentSource.VOICE).dose.event_id      # today 08:00 -> DISPENSED
    evening = med.event_at(med.sched_2000, "20:00").event_id
    tomorrow = med.event_at(med.sched_0800, "08:00", date(2026, 10, 6)).event_id
    yesterday = med.event_at(med.sched_0800, "08:00", date(2026, 10, 4)).event_id
    sub = med.subscribe(Topic.DATA_CHANGED, Topic.DOSE_UPDATED)

    med.catalog.archive(med.med1)

    assert med.medication(med.med1).active is False
    assert med.compartment(2).medication_id is None
    assert not med.schedule(med.sched_0800).active and not med.schedule(med.sched_2000).active
    assert med.event(accessed).status == DISPENSED          # accessed doses are history, not cancelled
    assert med.event(evening).status == CANCELLED and med.event(tomorrow).status == CANCELLED
    assert med.event(yesterday).status == MISSED
    assert med.adherence_statuses(evening)[-1] == CANCELLED
    topics = {(e.topic, e.data.get("entity")) for e in sub.drain()}
    assert {(Topic.DATA_CHANGED, "medication"), (Topic.DATA_CHANGED, "compartment"),
            (Topic.DATA_CHANGED, "schedule"), (Topic.DOSE_UPDATED, None)} <= topics
    assert [r["medication_id"] for r in med.catalog.list()] == [med.med2]
    archived = next(r for r in med.catalog.list(include_inactive=True) if r["medication_id"] == med.med1)
    assert archived["active"] is False and archived["slot"] is None and archived["schedules"] == []
    med.catalog.archive(med.med1)                           # idempotent
    with pytest.raises(NotFoundError):
        med.catalog.archive(4242)
    with pytest.raises(ValidationError):
        med.compartments.assign(0, med.med1)                # archived medication cannot be assigned


# --------------------------------------------------------------------------- compartments


def test_ensure_device_bootstraps_defaults(settings, db, bus):
    svc = CompartmentService(db, settings, bus=bus)
    sub = bus.subscribe([Topic.DATA_CHANGED])
    svc.ensure_device()
    with db.session() as s:
        users = s.scalars(select(User)).all()
        assert [u.display_name for u in users] == [DEFAULT_USER_NAME]
        dev = s.get(Device, settings.device_id)
        assert dev.num_slots == 6 and dev.user_id == users[0].user_id
        comps = s.scalars(select(Compartment).order_by(Compartment.slot_number)).all()
        assert [(c.slot_number, c.active, c.medication_id) for c in comps] == [(i, True, None) for i in range(6)]
    assert sub.drain() and svc.user_id() == users[0].user_id and svc.device_id == settings.device_id
    svc.ensure_device()                                      # idempotent: nothing changes, nothing published
    assert _count(db, Compartment) == 6 and _count(db, User) == 1 and sub.drain() == []


def test_ensure_device_uses_existing_user(settings, db):
    with db.session() as s:
        s.add(User(display_name="Alex"))
    CompartmentService(db, settings).ensure_device()
    with db.session() as s:
        assert s.scalars(select(User.display_name)).all() == ["Alex"]
        assert s.get(Device, settings.device_id).user.display_name == "Alex"


def test_slot_count_shrinks_and_grows_without_losing_assignments(med: Med):
    med.compartments.assign(5, med.med2)
    small = CompartmentService(med.db, med.settings.model_copy(update={"num_slots": 4}))
    small.ensure_device()
    assert [c["slot"] for c in small.list()] == [0, 1, 2, 3]
    c5 = med.compartment(5)
    assert c5.active is False and c5.medication_id == med.med2          # deactivated, assignment kept
    big = CompartmentService(med.db, med.settings.model_copy(update={"num_slots": 8}))
    big.ensure_device()
    rows = big.list()
    assert [c["slot"] for c in rows] == list(range(8)) and all(c["active"] for c in rows)
    assert rows[5]["medication_id"] == med.med2
    with med.db.session() as s:
        assert s.get(Device, med.settings.device_id).num_slots == 8


def test_compartment_list_shape(med: Med):
    rows = med.compartments.list()
    assert [r["slot"] for r in rows] == list(range(6)) and all(set(r) == COMPARTMENT_KEYS for r in rows)
    assert rows[2]["medication_name"] == "Vitamin C (demo candy)" and rows[2]["compartment_number"] == 3
    assert rows[0]["medication_id"] is None and rows[0]["loaded_at"] is None


def test_assign_moves_and_replaces(med: Med):
    sub = med.subscribe(Topic.DATA_CHANGED)
    rows = med.compartments.assign(0, med.med1)                         # move med1: slot 2 -> 0
    assert rows[0]["medication_id"] == med.med1 and rows[2]["medication_id"] is None
    rows = med.compartments.assign(0, med.med2)                         # replace: med1 unassigned, med2 moved
    assert rows[0]["medication_id"] == med.med2 and rows[4]["medication_id"] is None
    assert all(r["medication_id"] != med.med1 for r in rows)
    assert med.catalog.get(med.med1)["slot"] is None
    rows = med.compartments.assign(0, None)
    assert all(r["medication_id"] is None for r in rows)
    log = med.devlog("COMPARTMENT_ASSIGNED")
    assert log[1].detail == {"slot": 0, "medication_id": med.med2, "previous_medication_id": med.med1,
                             "moved_from_slots": [4]}
    assert {"entity": "compartment", "id": 0} in [e.data for e in sub.drain()]


def test_assign_validation(med: Med):
    for slot in (-1, 6, True, "1"):
        with pytest.raises(ValidationError):
            med.compartments.assign(slot, med.med1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        med.compartments.assign(1, "3")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        med.compartments.assign(1, 9999)
    with pytest.raises(ValidationError):
        med.compartments.assign(1, _raw_med(med, confirmed_by_user=False))
    with med.db.session() as s:
        other = User(display_name="Someone else")
        s.add(other)
        s.flush()
        other_id = other.user_id
    with pytest.raises(ValidationError):
        med.compartments.assign(1, _raw_med(med, user_id=other_id))


def test_assign_new_medication_resets_loaded_at(med: Med):
    med.dose.finish_loading(2)
    assert med.compartment(2).loaded_at is not None
    med.compartments.assign(2, med.med1)                                 # same medication: kept
    assert med.compartment(2).loaded_at is not None
    med.compartments.assign(2, med.med2)
    assert med.compartment(2).loaded_at is None


def test_schedule_listing_inside_medication_ignores_inactive(med: Med):
    med.scheduler.deactivate_schedule(med.sched_2000)
    assert [s["time_of_day"] for s in med.catalog.get(med.med1)["schedules"]] == ["08:00"]
    with med.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Schedule).where(Schedule.medication_id == med.med1)) == 2
