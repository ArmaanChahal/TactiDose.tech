"""MedicationCatalog v2: confirmed records only, validation, patient scoping, archive cascade."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from tactidose.core.bus import Topic
from tactidose.db.models import Device, Medication, Schedule, User
from tactidose.medication.catalog import MedicationCatalog
from tactidose.medication.compartments import DEFAULT_USER_NAME
from tactidose.medication.errors import NotFoundError, ValidationError
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    HARDWARE_ERROR,
    Env,
    env,
    env_template,
)

MEDICATION_KEYS = {"medication_id", "patient_id", "name", "strength", "instructions_text", "warnings", "source",
                   "confirmed_by_user", "confirmed_by", "confirmed_at", "active", "slot", "compartment_number",
                   "container_number", "schedules"}


def _count(e_db, model) -> int:
    with e_db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def _other_patient(e: Env, role: str = "patient") -> int:
    with e.db.session() as s:
        u = User(display_name="Pat Two", role=role, email=f"pat2-{role}@test.tactidose")
        s.add(u)
        s.flush()
        return u.user_id


# --------------------------------------------------------------------------- create


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
def test_create_requires_explicit_confirmation(env: Env, confirmed):
    before = _count(env.db, Medication)
    with pytest.raises(ValidationError):
        env.catalog.create({"name": "Zinc (demo)"}, confirmed=confirmed, patient_id=env.patient)
    assert _count(env.db, Medication) == before


def test_create_normalises_and_returns_api_shape(env: Env):
    sub = env.subscribe(Topic.DATA_CHANGED, Topic.PATIENT_STATUS)
    out = env.catalog.create(
        {"name": "  Zinc (demo token) ", "strength": " 2 tokens ", "instructions": " Chew slowly. ",
         "warnings": [" Demo only ", "", "Not a medication"], "confirmed": True, "confirmed_by": "x"},
        confirmed=True, confirmed_by=" Dr. Lee ", patient_id=env.patient,
    )
    assert set(out) == MEDICATION_KEYS and out["patient_id"] == env.patient
    assert out["name"] == "Zinc (demo token)" and out["strength"] == "2 tokens"
    assert out["instructions_text"] == "Chew slowly." and out["warnings"] == ["Demo only", "Not a medication"]
    assert out["source"] == "manual" and out["confirmed_by_user"] is True and out["confirmed_by"] == "Dr. Lee"
    assert out["confirmed_at"] == env.clock.now().isoformat() and out["active"] is True
    assert out["slot"] is None and out["container_number"] is None and out["schedules"] == []
    published = [e.data for e in sub.drain()]
    assert {"entity": "medication", "id": out["medication_id"], "patient_id": env.patient} in published
    assert {"patient_id": env.patient, "reason": "medication"} in published
    assert env.devlog("MEDICATION_CREATED")[-1].detail["medication_id"] == out["medication_id"]


@pytest.mark.parametrize("fields", [
    {}, {"name": ""}, {"name": "   "}, {"name": None}, {"name": 5}, {"name": "x" * 201},
    {"name": "ok", "strength": "x" * 121}, {"name": "ok", "strength": 2},
    {"name": "ok", "instructions_text": "x" * 2001},
    {"name": "ok", "warnings": "Demo only"}, {"name": "ok", "warnings": ["w"] * 21},
    {"name": "ok", "warnings": [3]}, {"name": "ok", "warnings": ["x" * 501]},
    {"name": "ok", "dosage": "take two"},
])
def test_create_rejects_invalid_fields(env: Env, fields):
    with pytest.raises(ValidationError):
        env.catalog.create(fields, confirmed=True, patient_id=env.patient)


def test_create_accepts_boundaries(env: Env):
    out = env.catalog.create({"name": "n" * 200, "strength": "s" * 120, "instructions_text": "i" * 2000,
                              "warnings": [f"w{i}" for i in range(20)]}, confirmed=True)
    assert len(out["name"]) == 200 and len(out["warnings"]) == 20 and out["patient_id"] == env.patient


def test_create_rejects_non_mapping_bad_source_and_unknown_scan(env: Env):
    with pytest.raises(ValidationError):
        env.catalog.create("Zinc", confirmed=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        env.catalog.create({"name": "Zinc"}, confirmed=True, source="gemini_autosave")
    with pytest.raises(NotFoundError):
        env.catalog.create({"name": "Zinc"}, confirmed=True, source="label_scan", scan_id=999)


def test_create_for_a_given_patient_only(env: Env):
    other = _other_patient(env)
    out = env.catalog.create({"name": "Pat's token"}, confirmed=True, patient_id=other)
    assert out["patient_id"] == other and env.medication(out["medication_id"]).user_id == other
    with pytest.raises(NotFoundError):
        env.catalog.create({"name": "Nobody's"}, confirmed=True, patient_id=4242)
    with pytest.raises(ValidationError):
        env.catalog.create({"name": "Caregiver's"}, confirmed=True, patient_id=env.doctor)


def test_create_bootstraps_device_on_empty_database(settings_v2, clock, db_v2):
    catalog = MedicationCatalog(db_v2, settings_v2, clock)
    assert catalog.list() == []                      # no device yet
    out = catalog.create({"name": "First token"}, confirmed=True)
    assert [m["medication_id"] for m in catalog.list()] == [out["medication_id"]]
    with db_v2.session() as s:
        assert s.scalars(select(User.display_name)).all() == [DEFAULT_USER_NAME]
        assert s.get(Device, settings_v2.device_id) is not None


# --------------------------------------------------------------------------- update / list / get


def test_update_requires_confirmation_and_restamps(env: Env):
    original = env.catalog.get(env.med(0))
    env.advance(minutes=10)
    with pytest.raises(ValidationError):
        env.catalog.update(env.med(0), {"strength": "2 pieces"}, confirmed=False)
    assert env.catalog.get(env.med(0))["strength"] == "1 piece"
    out = env.catalog.update(env.med(0), {"strength": "2 pieces", "warnings": ["Demo"]},
                             confirmed=True, confirmed_by="nurse", patient_id=env.patient)
    assert out["strength"] == "2 pieces" and out["warnings"] == ["Demo"] and out["confirmed_by"] == "nurse"
    assert out["confirmed_at"] == env.clock.now().isoformat() != original["confirmed_at"]
    assert out["name"] == original["name"]           # untouched fields stay
    assert env.catalog.update(env.med(0), {}, confirmed=False) == out          # nothing to change
    with pytest.raises(ValidationError):
        env.catalog.update(env.med(0), {"name": ""}, confirmed=True)
    with pytest.raises(NotFoundError):
        env.catalog.update(99999, {"name": "x"}, confirmed=True)
    with pytest.raises(NotFoundError):
        env.catalog.update(env.med(0), {"name": "x"}, confirmed=True, patient_id=_other_patient(env))


def test_list_and_get_shape(env: Env):
    rows = env.catalog.list()
    assert [r["name"] for r in rows] == ["Calcium (demo token)", "Omega-3 (demo candy)", "Vitamin C (demo candy)"]
    assert env.catalog.list(patient_id=env.patient) == rows
    vit = next(r for r in rows if r["medication_id"] == env.med(0))
    assert set(vit) == MEDICATION_KEYS and vit["slot"] == 0 and vit["container_number"] == 1
    assert [s["time_of_day"] for s in vit["schedules"]] == ["08:00"]
    assert env.catalog.get(env.med(1))["slot"] == 1
    other = _other_patient(env)
    assert env.catalog.list(patient_id=other) == []
    with pytest.raises(NotFoundError):
        env.catalog.get(env.med(0), patient_id=other)          # another patient's record is "not found"
    with pytest.raises(NotFoundError):
        env.catalog.get(12345)


# --------------------------------------------------------------------------- archive


def test_archive_cascade(env: Env):
    dropped = env.manual(0)                                    # today 08:00 -> DISPENSED
    assert env.dose_0800().status == DISPENSED and dropped.dropped
    tomorrow = env.event_at(env.sched(0), "08:00", date(2026, 10, 6)).event_id
    sub = env.subscribe(Topic.DATA_CHANGED, Topic.DOSE_UPDATED, Topic.PATIENT_STATUS)

    env.catalog.archive(env.med(0), patient_id=env.patient)

    assert env.medication(env.med(0)).active is False
    comp = env.compartment(0)
    assert (comp.medication_id, comp.pill_count, comp.loaded_at) == (None, 0, None)
    assert not env.schedule(env.sched(0)).active
    assert env.dose_0800().status == DISPENSED                 # dropped doses are history, not cancelled
    assert env.event(tomorrow).status == CANCELLED
    assert env.adherence_statuses(tomorrow)[-1] == CANCELLED
    topics = {(e.topic, e.data.get("entity")) for e in sub.drain()}
    assert {(Topic.DATA_CHANGED, "medication"), (Topic.DATA_CHANGED, "compartment"),
            (Topic.DATA_CHANGED, "schedule"), (Topic.DOSE_UPDATED, None), (Topic.PATIENT_STATUS, None)} <= topics
    assert [r["medication_id"] for r in env.catalog.list()] == [env.med(1), env.med(2)]
    archived = next(r for r in env.catalog.list(include_inactive=True) if r["medication_id"] == env.med(0))
    assert archived["active"] is False and archived["slot"] is None and archived["schedules"] == []
    env.catalog.archive(env.med(0))                            # idempotent
    with pytest.raises(NotFoundError):
        env.catalog.archive(4242)
    with pytest.raises(NotFoundError):
        env.catalog.archive(env.med(1), patient_id=_other_patient(env))
    with pytest.raises(ValidationError):
        env.compartments.assign(0, env.med(0))                 # archived medication cannot be assigned
    assert env.manual(0).reason == "NO_MEDICATION"


def test_archive_cancels_retrying_doses_but_not_ones_under_review(env: Env):
    retrying = env.dose_0800()
    env.set_event(retrying.event_id, status=HARDWARE_ERROR, needs_review=False, next_attempt_at=env.at("08:30"))
    review = env.event_at(env.sched(0), "08:00", date(2026, 10, 6))
    env.set_event(review.event_id, status=HARDWARE_ERROR, needs_review=True)
    env.catalog.archive(env.med(0))
    gone = env.event(retrying.event_id)
    assert (gone.status, gone.next_attempt_at, gone.review_note) == (CANCELLED, None, "medication archived")
    assert env.event(review.event_id).status == HARDWARE_ERROR     # a caregiver resolves it


def test_schedule_listing_inside_medication_ignores_inactive(env: Env):
    extra = env.scheduler.create_schedule(env.med(0), "21:00", patient_id=env.patient)
    env.scheduler.deactivate_schedule(extra["schedule_id"])
    assert [s["time_of_day"] for s in env.catalog.get(env.med(0))["schedules"]] == ["08:00"]
    with env.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Schedule).where(Schedule.medication_id == env.med(0))) == 2


def test_create_record_inside_a_callers_transaction(env: Env):
    with env.db.session() as s:
        med = env.catalog.create_record(s, {"name": "Staged"}, confirmed=True, patient_id=env.patient)
        assert med.medication_id is not None
    assert env.medication(med.medication_id).name == "Staged"
