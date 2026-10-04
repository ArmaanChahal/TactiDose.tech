"""AuthService: care links, the §9 permission matrix, care lists and the device binding rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tactidose.auth import passwords
from tactidose.auth.errors import PermissionDenied, TooManyAttempts
from tactidose.auth.service import (
    AuthService,
    bind_device_in_session,
    is_unclaimed_owner,
)
from tactidose.core.bus import Topic
from tactidose.core.interfaces import AuthUser
from tactidose.db.models import (
    AnalyticsOutbox,
    CareLink,
    Compartment,
    Device,
    DeviceLog,
    DoseEvent,
    DoseStatus,
    LabelScan,
    Medication,
    Schedule,
    User,
)
from tactidose.medication.errors import NotFoundError, ValidationError
from tests.fakes import seed_v2

PASSWORD = "correct horse"


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(passwords, "SCRYPT_N", 2 ** 10)


@pytest.fixture
def auth(db_v2, settings_v2, clock, bus) -> AuthService:
    return AuthService(db_v2, settings_v2, clock, bus=bus)


@pytest.fixture
def world(auth, db_v2, settings_v2):
    """seed_v2 (Alex + linked Sam and Dr. Lee) plus an unrelated patient, doctor and family member."""
    ids = seed_v2(db_v2, settings_v2)
    users = {}
    with db_v2.session() as s:
        for key in ("patient", "family", "doctor"):
            row = s.get(User, ids[f"{key}_id"])
            users[key] = AuthUser(row.user_id, row.display_name, row.role, row.email)
    users["other_patient"] = auth.create_user(email="other@example.com", password=PASSWORD,
                                              display_name="Other Patient", role="patient")
    users["other_doctor"] = auth.create_user(email="otherdoc@example.com", password=PASSWORD,
                                             display_name="Other Doctor", role="doctor")
    users["other_family"] = auth.create_user(email="otherfam@example.com", password=PASSWORD,
                                             display_name="Other Family", role="family")
    return ids, users


def _code(auth: AuthService, patient_id: int) -> str:
    return auth.patient_profile(patient_id)["link_code"]


# --------------------------------------------------------------------------- permission matrix (§9)

# actor -> (view, edit, drop/chat) on Alex's data
MATRIX = {
    "patient": (True, False, True),
    "family": (True, True, False),
    "doctor": (True, True, False),
    "other_patient": (False, False, False),
    "other_doctor": (False, False, False),
    "other_family": (False, False, False),
}


@pytest.mark.parametrize("actor", sorted(MATRIX))
def test_permission_matrix(auth, world, actor) -> None:
    ids, users = world
    pid = ids["patient_id"]
    view, edit, drop = MATRIX[actor]
    user = users[actor]
    assert auth.can_view(user, pid) is view
    assert auth.can_edit(user, pid) is edit
    assert auth.can_drop(user, pid) is drop
    perms = auth.permissions(user, pid)
    assert perms == {
        "view": view, "drop": drop, "chat": drop, "edit": edit, "report": view,
        "device_home": edit, "device_reconnect": edit, "device_stop": view,
    }


def test_patients_only_see_themselves(auth, world) -> None:
    _, users = world
    other = users["other_patient"]
    assert auth.can_view(other, other.user_id) and not auth.can_edit(other, other.user_id)
    assert auth.can_drop(other, other.user_id)
    assert not auth.can_view(users["patient"], other.user_id)


def test_access_checks_reject_odd_input(auth, world) -> None:
    ids, users = world
    pid = ids["patient_id"]
    for bad in (None, str(pid), float(pid), True):
        assert not auth.can_view(users["doctor"], bad)  # type: ignore[arg-type]
        assert not auth.can_edit(users["doctor"], bad)  # type: ignore[arg-type]
        assert not auth.can_drop(users["patient"], bad)  # type: ignore[arg-type]
    assert not auth.can_view(None, pid) and not auth.can_edit(None, pid) and not auth.can_drop(None, pid)
    stranger = AuthUser(999, "Admin?", "admin", "x@example.com")
    assert not auth.can_view(stranger, pid) and auth.linked_patient_ids(stranger) == []
    assert auth.permissions(None, pid) == dict.fromkeys(auth.permissions(None, pid), False)


def test_disabled_caregivers_lose_access(auth, world, db_v2) -> None:
    ids, users = world
    with db_v2.session() as s:
        s.get(User, users["doctor"].user_id).is_active = False
    assert not auth.can_view(users["doctor"], ids["patient_id"])
    assert users["doctor"].user_id not in auth.caregiver_ids(ids["patient_id"])


def test_links_to_non_patients_grant_nothing(auth, world, db_v2) -> None:
    _, users = world
    with db_v2.session() as s:
        s.add(CareLink(caregiver_id=users["other_family"].user_id, patient_id=users["other_doctor"].user_id,
                       relationship_kind="family"))
    assert not auth.can_view(users["other_family"], users["other_doctor"].user_id)
    assert auth.linked_patient_ids(users["other_family"]) == []


def test_care_lists(auth, world) -> None:
    ids, users = world
    pid = ids["patient_id"]
    assert auth.linked_patient_ids(users["patient"]) == [pid]
    assert auth.linked_patient_ids(users["doctor"]) == [pid]
    assert auth.linked_patient_ids(users["other_doctor"]) == []
    assert auth.caregiver_ids(pid) == sorted([ids["family_id"], ids["doctor_id"]])
    assert auth.caregiver_ids(users["other_patient"].user_id) == []
    doctors = auth.caregivers(pid, relationship="doctor")
    assert doctors == [{"user_id": ids["doctor_id"], "display_name": "Dr. Lee", "email": "dr.lee@test.tactidose",
                        "role": "doctor", "relationship": "doctor"}]
    assert auth.care_patients(users["family"]) == [
        {"patient_id": pid, "display_name": "Alex Rivera", "relationship": "family"}]
    assert auth.care_patients(users["patient"]) == [] and auth.care_patients(None) == []


# --------------------------------------------------------------------------- linking


def test_link_with_patient_id_and_code(auth, world, db_v2, bus) -> None:
    ids, users = world
    pid = ids["patient_id"]
    sub = bus.subscribe([Topic.PATIENT_STATUS])
    view = auth.link_patient(caregiver=users["other_doctor"], patient_id=pid, link_code="ALEX2026")
    assert view == {"patient_id": pid, "display_name": "Alex Rivera", "relationship": "doctor"}
    assert auth.can_edit(users["other_doctor"], pid)
    fam = auth.link_patient(caregiver=users["other_family"], patient_id=pid, link_code=" alex-2026 ")
    assert fam["relationship"] == "family"
    assert auth.link_patient(caregiver=users["other_doctor"], patient_id=pid, link_code="ALEX2026") == view
    with db_v2.session() as s:
        links = s.scalars(select(CareLink).where(CareLink.patient_id == pid)).all()
        assert len(links) == 4
        assert [r.event for r in s.scalars(select(DeviceLog))].count("CARE_LINK_ADDED") == 2
    assert [e.data["reason"] for e in sub.drain()] == ["care_link_added", "care_link_added"]
    assert sorted(p["patient_id"] for p in auth.care_patients(users["other_doctor"])) == [pid]


def test_link_with_a_generated_code(auth, world) -> None:
    _, users = world
    target = users["other_patient"]
    code = _code(auth, target.user_id)
    spaced = " ".join(code[i:i + 4] for i in (0, 4)).lower()
    assert auth.link_patient(caregiver=users["doctor"], patient_id=target.user_id, link_code=spaced)["patient_id"] \
        == target.user_id


@pytest.mark.parametrize("code", ["WRONG123", "", "ALEX202", "ALEX20266", "ÄLEX2026", None, 2026])
def test_wrong_codes_are_refused(auth, world, code) -> None:
    ids, users = world
    with pytest.raises(PermissionDenied) as info:
        auth.link_patient(caregiver=users["other_doctor"], patient_id=ids["patient_id"], link_code=code)
    assert info.value.status_code == 403
    assert not auth.can_view(users["other_doctor"], ids["patient_id"])


@pytest.mark.parametrize("target", ["missing", "doctor", "bool", "str"])
def test_unknown_patients_are_not_found(auth, world, target) -> None:
    ids, users = world
    patient_id = {"missing": 9999, "doctor": ids["doctor_id"], "bool": True, "str": str(ids["patient_id"])}[target]
    with pytest.raises(NotFoundError) as info:
        auth.link_patient(caregiver=users["other_doctor"], patient_id=patient_id, link_code="ALEX2026")
    assert info.value.status_code == 404


def test_patients_cannot_link(auth, world) -> None:
    ids, users = world
    for caller in (users["other_patient"], users["patient"], None):
        with pytest.raises(PermissionDenied):
            auth.link_patient(caregiver=caller, patient_id=ids["patient_id"], link_code="ALEX2026")  # type: ignore[arg-type]
    assert not auth.can_view(users["other_patient"], ids["patient_id"])


def test_stale_caregiver_identity_is_rechecked(auth, world, db_v2) -> None:
    ids, users = world
    with db_v2.session() as s:
        s.get(User, users["other_doctor"].user_id).role = "patient"
    with pytest.raises(PermissionDenied):
        auth.link_patient(caregiver=users["other_doctor"], patient_id=ids["patient_id"], link_code="ALEX2026")


def test_patients_without_code_cannot_be_linked(auth, world, db_v2) -> None:
    _, users = world
    with db_v2.session() as s:
        s.get(User, users["other_patient"].user_id).link_code = None
    with pytest.raises(PermissionDenied):
        auth.link_patient(caregiver=users["doctor"], patient_id=users["other_patient"].user_id, link_code="")


def test_wrong_codes_are_rate_limited(auth, world, clock) -> None:
    ids, users = world
    pid = ids["patient_id"]
    for _ in range(4):
        with pytest.raises(PermissionDenied):
            auth.link_patient(caregiver=users["other_doctor"], patient_id=pid, link_code="GUESS234")
    with pytest.raises(TooManyAttempts) as info:
        auth.link_patient(caregiver=users["other_doctor"], patient_id=pid, link_code="GUESS234")
    assert info.value.retry_after_s == 30 and "link codes" in info.value.message
    with pytest.raises(TooManyAttempts):
        auth.link_patient(caregiver=users["other_doctor"], patient_id=pid, link_code="ALEX2026")
    # other caregivers are not affected
    auth.link_patient(caregiver=users["other_family"], patient_id=pid, link_code="ALEX2026")
    clock.advance(timedelta(seconds=31))
    assert auth.link_patient(caregiver=users["other_doctor"], patient_id=pid, link_code="ALEX2026")


def test_unlink(auth, world, db_v2, bus) -> None:
    ids, users = world
    pid = ids["patient_id"]
    sub = bus.subscribe([Topic.PATIENT_STATUS])
    auth.unlink_patient(caregiver=users["family"], patient_id=pid)
    assert not auth.can_view(users["family"], pid) and auth.caregiver_ids(pid) == [ids["doctor_id"]]
    assert auth.can_view(users["doctor"], pid)
    with pytest.raises(NotFoundError):
        auth.unlink_patient(caregiver=users["family"], patient_id=pid)
    with pytest.raises(PermissionDenied):
        auth.unlink_patient(caregiver=users["patient"], patient_id=pid)
    assert [e.data for e in sub.drain()] == [{"patient_id": pid, "reason": "care_link_removed"}]
    with db_v2.session() as s:
        assert "CARE_LINK_REMOVED" in [r.event for r in s.scalars(select(DeviceLog))]


def test_admin_link(auth, world) -> None:
    ids, users = world
    pid = ids["patient_id"]
    view = auth.admin_link(caregiver_id=users["other_family"].user_id, patient_id=pid)
    assert view["relationship"] == "family" and auth.can_edit(users["other_family"], pid)
    assert auth.admin_link(caregiver_id=users["other_family"].user_id, patient_id=pid) == view
    with pytest.raises(ValidationError):
        auth.admin_link(caregiver_id=users["other_patient"].user_id, patient_id=pid)
    with pytest.raises(NotFoundError):
        auth.admin_link(caregiver_id=9999, patient_id=pid)
    with pytest.raises(NotFoundError):
        auth.admin_link(caregiver_id=users["other_family"].user_id, patient_id=ids["doctor_id"])


# --------------------------------------------------------------------------- device binding rule


def _placeholder_device(db, settings, *, with_setup: bool = True) -> dict[str, int]:
    """The wave-1 bootstrap: a default user without email owns the device (+ a medication setup)."""
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    with db.session() as s:
        legacy = User(display_name="TactiDose User", accessibility_preferences={})
        s.add(legacy)
        s.flush()
        s.add(Device(device_id=settings.device_id, user_id=legacy.user_id, num_slots=settings.num_slots))
        s.flush()
        comps = [Compartment(device_id=settings.device_id, slot_number=i, active=True) for i in range(2)]
        s.add_all(comps)
        out = {"legacy_id": legacy.user_id}
        if with_setup:
            med = Medication(user_id=legacy.user_id, name="Vitamin C (demo candy)", confirmed_by_user=True)
            s.add(med)
            s.flush()
            comps[0].medication_id = med.medication_id
            comps[0].pill_count = 7
            sched = Schedule(medication_id=med.medication_id, time_of_day="08:00")
            s.add(sched)
            s.flush()
            s.add(LabelScan(user_id=legacy.user_id))
            ev = DoseEvent(schedule_id=sched.schedule_id, medication_id=med.medication_id, user_id=legacy.user_id,
                           device_id=settings.device_id, scheduled_at=now)
            s.add(ev)
            s.flush()
            out.update(med_id=med.medication_id, event_id=ev.event_id)
        return out


def test_first_patient_adopts_a_placeholder_device(auth, db_v2, settings_v2) -> None:
    legacy = _placeholder_device(db_v2, settings_v2)
    assert auth.create_user(email="doc@example.com", password=PASSWORD, display_name="Doc", role="doctor")
    patient = auth.register(email="alex@example.com", password=PASSWORD, display_name="Alex", role="patient")
    with db_v2.session() as s:
        assert s.get(Device, settings_v2.device_id).user_id == patient.user_id
        assert s.get(Medication, legacy["med_id"]).user_id == patient.user_id
        assert s.get(DoseEvent, legacy["event_id"]).user_id == patient.user_id
        assert s.scalars(select(LabelScan)).one().user_id == patient.user_id
        comps = {c.slot_number: c for c in s.scalars(select(Compartment))}
        assert sorted(comps) == [0, 1, 2]                    # missing container added
        assert comps[0].medication_id == legacy["med_id"] and comps[0].pill_count == 7
        bound = [r.detail for r in s.scalars(select(DeviceLog).where(DeviceLog.event == "DEVICE_BOUND"))]
        assert bound[-1]["adopted"] is True and bound[-1]["previous_owner_id"] == legacy["legacy_id"]


def test_a_device_owned_by_a_caregiver_goes_to_the_first_patient(auth, db_v2, settings_v2) -> None:
    doctor = auth.create_user(email="doc@example.com", password=PASSWORD, display_name="Doc", role="doctor")
    with db_v2.session() as s:
        s.add(Device(device_id=settings_v2.device_id, user_id=doctor.user_id))
    patient = auth.register(email="alex@example.com", password=PASSWORD, display_name="Alex", role="patient")
    with db_v2.session() as s:
        assert s.get(Device, settings_v2.device_id).user_id == patient.user_id


def test_forced_binding_releases_the_previous_patients_setup(auth, db_v2, settings_v2, clock) -> None:
    ids = seed_v2(db_v2, settings_v2)
    now = clock.now()
    with db_v2.session() as s:
        s.add_all([
            DoseEvent(schedule_id=ids["schedule_ids"][0], medication_id=ids["med_ids"][0], user_id=ids["patient_id"],
                      device_id=settings_v2.device_id, scheduled_at=now + timedelta(hours=1)),
            DoseEvent(schedule_id=ids["schedule_ids"][1], medication_id=ids["med_ids"][1], user_id=ids["patient_id"],
                      device_id=settings_v2.device_id, scheduled_at=now - timedelta(hours=20),
                      status=DoseStatus.DISPENSED.value),
        ])
    newcomer = auth.register(email="new@example.com", password=PASSWORD, display_name="New", role="patient")
    assert auth.patient_profile(newcomer.user_id)["device_id"] is None   # Alex is a real patient
    binding = auth.bind_device(newcomer.user_id, force=True)
    assert binding["changed"] and not binding["adopted"] and binding["previous_owner_id"] == ids["patient_id"]
    assert binding["released_containers"] == 3 and binding["cancelled_doses"] == 1
    with db_v2.session() as s:
        assert s.get(Device, settings_v2.device_id).user_id == newcomer.user_id
        comps = s.scalars(select(Compartment)).all()
        assert all(c.medication_id is None and c.pill_count == 0 for c in comps)
        statuses = sorted(e.status for e in s.scalars(select(DoseEvent)))
        assert statuses == ["CANCELLED", "DISPENSED"]
        assert s.scalars(select(AnalyticsOutbox)).all()      # the cancellation is in the outbox
    assert auth.patient_profile(ids["patient_id"])["device_id"] is None
    again = auth.bind_device(newcomer.user_id, force=True)
    assert not again["changed"] and again["patient_id"] == newcomer.user_id


def test_bind_device_validation(auth, world) -> None:
    ids, users = world
    with pytest.raises(NotFoundError):
        auth.bind_device(9999)
    with pytest.raises(ValidationError):
        auth.bind_device(ids["doctor_id"])
    assert not auth.bind_device(users["other_patient"].user_id)["changed"]   # Alex keeps it


def test_bind_device_publishes_for_both_patients(auth, world, bus) -> None:
    ids, users = world
    sub = bus.subscribe([Topic.PATIENT_STATUS])
    auth.bind_device(users["other_patient"].user_id, force=True)
    assert [e.data for e in sub.drain()] == [
        {"patient_id": users["other_patient"].user_id, "reason": "device_bound"},
        {"patient_id": ids["patient_id"], "reason": "device_unbound"},
    ]


def test_unclaimed_owner_rule(db_v2, settings_v2, clock) -> None:
    assert is_unclaimed_owner(None)
    assert is_unclaimed_owner(User(display_name="x", role="patient", email=None))
    assert is_unclaimed_owner(User(display_name="x", role="doctor", email="d@example.com"))
    assert not is_unclaimed_owner(User(display_name="x", role="patient", email="p@example.com"))
    ids = seed_v2(db_v2, settings_v2)
    with db_v2.session() as s:
        other = User(display_name="Other", role="patient", email="o@example.com")
        s.add(other)
        s.flush()
        result = bind_device_in_session(s, settings_v2, other, now=clock.now())
        assert not result.changed and result.patient_id == ids["patient_id"]
