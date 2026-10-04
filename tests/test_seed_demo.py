"""db/seed.py: demo accounts/device/containers/schedules, idempotency and reset_demo."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from tactidose.auth import passwords
from tactidose.auth.errors import AuthError
from tactidose.auth.service import AuthService
from tactidose.core.bus import Topic
from tactidose.db.models import (
    AuthSession,
    CareLink,
    Compartment,
    Conversation,
    ConversationMessage,
    Device,
    DoseEvent,
    Medication,
    Notification,
    PillDrop,
    Report,
    ReportDelivery,
    Schedule,
    User,
)
from tactidose.db.seed import (
    DEMO_ACCOUNTS,
    DEMO_MEDICATIONS,
    DYNAMIC_MODELS,
    reset_demo,
    seed_demo,
)
from tactidose.medication.errors import ValidationError

DEMO_PASSWORD = "demo1234"


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(passwords, "SCRYPT_N", 2 ** 10)


@pytest.fixture
def auth(db_v2, settings_v2, clock, bus) -> AuthService:
    return AuthService(db_v2, settings_v2, clock, bus=bus)


def _count(db, model) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def _comps(db) -> dict[int, Compartment]:
    with db.session() as s:
        return {c.slot_number: c for c in s.scalars(select(Compartment))}


def test_seed_creates_the_demo(db_v2, settings_v2, clock, auth, bus) -> None:
    sub = bus.subscribe([Topic.PATIENT_STATUS])
    summary = seed_demo(db_v2, settings_v2, clock, auth=auth, bus=bus)
    assert [a["email"] for a in summary["accounts"]] == [
        "alex@demo.tactidose", "sam@demo.tactidose", "dr.lee@demo.tactidose"]
    for spec in DEMO_ACCOUNTS:
        user, token = auth.login(spec.email, DEMO_PASSWORD)
        assert (user.role, user.display_name) == (spec.role.value, spec.display_name)
        assert auth.resolve(token) == user
    pid, fid, did = summary["patient_id"], summary["family_id"], summary["doctor_id"]
    with db_v2.session() as s:
        links = {(l.caregiver_id, l.relationship_kind) for l in s.scalars(select(CareLink).where(
            CareLink.patient_id == pid))}
        assert links == {(fid, "family"), (did, "doctor")}
        dev = s.get(Device, settings_v2.device_id)
        assert dev.user_id == pid and dev.manual_cooldown_minutes == 60 and dev.auto_drop_enabled
        meds = {m.medication_id: m for m in s.scalars(select(Medication))}
        assert sorted(m.name for m in meds.values()) == sorted(spec.name for spec in DEMO_MEDICATIONS)
        assert all(m.user_id == pid and m.confirmed_by_user and m.active and m.source == "demo_seed"
                   and m.confirmed_at == clock.now() for m in meds.values())
        assert all("demo" in m.name.lower() for m in meds.values())
        scheds = s.scalars(select(Schedule).order_by(Schedule.time_of_day)).all()
        assert [(meds[sc.medication_id].name, sc.time_of_day, sc.frequency) for sc in scheds] == [
            ("Vitamin C (demo candy)", "08:00", "DAILY"),
            ("Calcium (demo token)", "13:00", "DAILY"),
            ("Omega-3 (demo candy)", "20:00", "DAILY"),
        ]
        assert all(sc.active and sc.created_by_user_id == did and sc.created_at == clock.now() for sc in scheds)
    comps = _comps(db_v2)
    assert [comps[i].pill_count for i in range(3)] == [20, 12, 3]
    low = [i for i, c in comps.items() if 0 < c.pill_count <= c.low_stock_threshold]
    assert low == [2]
    assert [c["medication_name"] for c in summary["containers"]] == [m.name for m in DEMO_MEDICATIONS]
    assert summary["created"] == {"users": 3, "links": 2, "device": True, "medications": 3, "schedules": 3}
    assert summary["device_bound_to_patient"] and summary["cooldown_minutes"] == 60
    assert "link_code" not in summary and auth.patient_profile(pid)["link_code"] not in json.dumps(summary)
    assert summary["med_ids"] == summary["medication_ids"] and len(summary["schedule_ids"]) == 3
    text = json.dumps(summary)
    assert DEMO_PASSWORD not in text and "scrypt" not in text
    assert [e.data for e in sub.drain()] == [{"patient_id": pid, "reason": "demo_seeded"}]


def test_seed_is_idempotent(db_v2, settings_v2, clock) -> None:
    first = seed_demo(db_v2, settings_v2, clock)
    counts = {m.__tablename__: _count(db_v2, m) for m in (User, CareLink, Medication, Schedule, Compartment, Device)}
    second = seed_demo(db_v2, settings_v2, clock)
    assert {m.__tablename__: _count(db_v2, m) for m in (User, CareLink, Medication, Schedule, Compartment,
                                                       Device)} == counts
    assert counts == {"users": 3, "care_links": 2, "medications": 3, "schedules": 3, "compartments": 3,
                      "devices": 1}
    for key in ("patient_id", "family_id", "doctor_id", "medication_ids", "compartment_ids", "schedule_ids"):
        assert first[key] == second[key], key
    assert second["created"] == {"users": 0, "links": 0, "device": False, "medications": 0, "schedules": 0}
    assert not any(a["password_reset"] for a in second["accounts"])


def test_reseeding_keeps_changes_made_during_the_demo(db_v2, settings_v2, clock) -> None:
    summary = seed_demo(db_v2, settings_v2, clock)
    with db_v2.session() as s:
        comp = s.get(Compartment, summary["compartment_ids"][0])
        comp.pill_count = 19
        s.get(Device, settings_v2.device_id).manual_cooldown_minutes = 5
        morning, lunch, _ = (s.get(Schedule, i) for i in summary["schedule_ids"])
        morning.time_of_day = "09:00"           # edited by the doctor
        lunch.active = False                    # deleted by the doctor
        s.execute(CareLink.__table__.delete().where(CareLink.caregiver_id == summary["family_id"]))
    again = seed_demo(db_v2, settings_v2, clock)
    with db_v2.session() as s:
        assert s.get(Compartment, summary["compartment_ids"][0]).pill_count == 19
        assert s.get(Device, settings_v2.device_id).manual_cooldown_minutes == 5
        times = sorted((sc.time_of_day, sc.active) for sc in s.scalars(select(Schedule)))
        assert times == [("09:00", True), ("13:00", False), ("20:00", True)]    # no second morning dose
    assert again["created"]["links"] == 1 and again["created"]["schedules"] == 0


def test_seed_reapplies_a_changed_demo_password(db_v2, settings_v2, clock, auth) -> None:
    seed_demo(db_v2, settings_v2, clock)
    changed = settings_v2.model_copy(update={"demo_password": SecretStr("another-demo-pass")})
    summary = seed_demo(db_v2, changed, clock)
    assert all(a["password_reset"] for a in summary["accounts"])
    assert auth.login("alex@demo.tactidose", "another-demo-pass")
    with pytest.raises(AuthError):
        auth.login("sam@demo.tactidose", DEMO_PASSWORD)


def test_seed_refuses_an_unusable_demo_password(db_v2, settings_v2, clock) -> None:
    short = settings_v2.model_copy(update={"demo_password": SecretStr("demo")})
    with pytest.raises(ValidationError) as info:
        seed_demo(db_v2, short, clock)
    assert "TACTIDOSE_DEMO_PASSWORD" in info.value.message and "8 characters" in info.value.message
    assert _count(db_v2, User) == 0


def test_seed_takes_over_a_placeholder_device(db_v2, settings_v2, clock) -> None:
    with db_v2.session() as s:
        legacy = User(display_name="TactiDose User")
        s.add(legacy)
        s.flush()
        s.add(Device(device_id=settings_v2.device_id, user_id=legacy.user_id, manual_cooldown_minutes=15))
    summary = seed_demo(db_v2, settings_v2.model_copy(update={"manual_cooldown_minutes": 45}), clock)
    with db_v2.session() as s:
        dev = s.get(Device, settings_v2.device_id)
        assert dev.user_id == summary["patient_id"] and dev.manual_cooldown_minutes == 45
    assert [c["pill_count"] for c in summary["containers"]] == [20, 12, 3]


def test_seed_does_not_take_the_device_from_a_real_patient(db_v2, settings_v2, clock, auth) -> None:
    other = auth.register(email="real@example.com", password="real-password", display_name="Real", role="patient")
    summary = seed_demo(db_v2, settings_v2, clock, auth=auth)
    assert not summary["device_bound_to_patient"] and summary["device_owner_id"] == other.user_id
    assert summary["containers"] == [] and summary["medication_ids"] == []
    assert _count(db_v2, Medication) == 0 and _count(db_v2, Schedule) == 0
    restored = reset_demo(db_v2, settings_v2, clock, auth=auth)
    assert restored["device_bound_to_patient"] and restored["device_owner_id"] == restored["patient_id"]
    assert [c["pill_count"] for c in restored["containers"]] == [20, 12, 3]


@pytest.mark.parametrize("slots,expected", [(2, [20, 12]), (4, [20, 12, 3, 0])])
def test_seed_follows_the_number_of_containers(db_v2, settings_v2, clock, slots, expected) -> None:
    summary = seed_demo(db_v2, settings_v2.model_copy(update={"num_slots": slots}), clock)
    assert [c["pill_count"] for c in summary["containers"]] == expected
    assert len(summary["medication_ids"]) == len(summary["schedule_ids"]) == min(slots, 3)
    last = summary["containers"][-1]["medication_id"]
    assert last == (None if slots == 4 else summary["medication_ids"][-1])


def _add_dynamic_rows(db, summary, clock, auth) -> str:
    _, token = auth.login("alex@demo.tactidose", DEMO_PASSWORD)
    pid, did = summary["patient_id"], summary["doctor_id"]
    now = clock.now()
    with db.session() as s:
        s.add(PillDrop(patient_id=pid, device_id=summary["device_id"], slot_number=0, source="manual",
                       status="DROPPED", requested_at=now))
        s.add(DoseEvent(schedule_id=summary["schedule_ids"][0], medication_id=summary["medication_ids"][0],
                        user_id=pid, device_id=summary["device_id"], scheduled_at=now + timedelta(minutes=5)))
        conv = Conversation(patient_id=pid)
        s.add(conv)
        s.flush()
        s.add(ConversationMessage(conversation_id=conv.conversation_id, patient_id=pid, role="user", content="hi"))
        report = Report(patient_id=pid, created_by_user_id=did, days=7, period_start=now - timedelta(days=7),
                        period_end=now, title="t", pdf=b"%PDF-1.4", pdf_size=8)
        s.add(report)
        s.flush()
        s.add(ReportDelivery(report_id=report.report_id, to_email="dr.lee@demo.tactidose", sent_by_user_id=pid,
                             status="SAVED"))
        s.add(Notification(user_id=pid, patient_id=pid, kind="PILL_DROPPED", title="Pill dropped"))
        comp = s.get(Compartment, summary["compartment_ids"][0])
        comp.pill_count = 4
        s.get(Device, summary["device_id"]).manual_cooldown_minutes = 1
        morning = s.get(Schedule, summary["schedule_ids"][0])
        morning.time_of_day = "09:30"
        extra = Medication(user_id=pid, name="Zinc (demo token)", confirmed_by_user=True)
        s.add(extra)
        s.flush()
        s.add(Schedule(medication_id=extra.medication_id, time_of_day="10:00"))
        s.get(Compartment, summary["compartment_ids"][1]).medication_id = extra.medication_id
    return token


def test_reset_wipes_dynamic_data_and_restores_the_demo(db_v2, settings_v2, clock, auth, bus) -> None:
    summary = seed_demo(db_v2, settings_v2, clock, auth=auth)
    code_before = auth.patient_profile(summary["patient_id"])["link_code"]
    token = _add_dynamic_rows(db_v2, summary, clock, auth)
    clock.advance(timedelta(hours=2))
    sub = bus.subscribe([Topic.PATIENT_STATUS])
    restored = reset_demo(db_v2, settings_v2, clock, auth=auth, bus=bus)
    for model in DYNAMIC_MODELS:
        assert _count(db_v2, model) == 0, model.__tablename__
    assert restored["wiped"] == {"report_deliveries": 1, "reports": 1, "conversation_messages": 1,
                                 "conversations": 1, "notifications": 1, "pill_drops": 1, "dose_events": 1,
                                 "auth_sessions": 1}
    assert auth.resolve(token) is None                       # everyone is signed out
    assert restored["patient_id"] == summary["patient_id"]
    assert auth.patient_profile(summary["patient_id"])["link_code"] == code_before   # links stay valid
    assert [c["pill_count"] for c in restored["containers"]] == [20, 12, 3]
    assert [c["medication_id"] for c in restored["containers"]] == summary["medication_ids"]
    assert restored["cooldown_minutes"] == 60
    with db_v2.session() as s:
        active = sorted((sc.time_of_day, sc.created_at) for sc in s.scalars(select(Schedule).where(
            Schedule.active.is_(True))))
        assert active == [("08:00", clock.now()), ("13:00", clock.now()), ("20:00", clock.now())]
        zinc = s.scalars(select(Medication).where(Medication.name == "Zinc (demo token)")).one()
        assert not zinc.active
    assert auth.login("alex@demo.tactidose", DEMO_PASSWORD)
    assert [e.data["reason"] for e in sub.drain()] == ["demo_reset"]


def test_reset_can_keep_sessions(db_v2, settings_v2, clock, auth) -> None:
    seed_demo(db_v2, settings_v2, clock, auth=auth)
    _, token = auth.login("dr.lee@demo.tactidose", DEMO_PASSWORD)
    summary = reset_demo(db_v2, settings_v2, clock, keep_sessions=True)
    assert "auth_sessions" not in summary["wiped"] and auth.resolve(token) is not None
    assert _count(db_v2, AuthSession) == 1


def test_reset_clears_login_lockouts(db_v2, settings_v2, clock, auth) -> None:
    seed_demo(db_v2, settings_v2, clock, auth=auth)
    for _ in range(5):
        with pytest.raises(AuthError):
            auth.login("alex@demo.tactidose", "wrong-password")
    reset_demo(db_v2, settings_v2, clock, auth=auth)
    assert auth.login("alex@demo.tactidose", DEMO_PASSWORD)


def test_reset_on_an_empty_database_seeds(db_v2, settings_v2, clock) -> None:
    summary = reset_demo(db_v2, settings_v2, clock)
    assert summary["reset"] and summary["reseeded"] and summary["created"]["users"] == 3
    assert set(summary["wiped"].values()) == {0}


def test_reset_without_reseed_only_wipes(db_v2, settings_v2, clock, auth) -> None:
    summary = seed_demo(db_v2, settings_v2, clock, auth=auth)
    _add_dynamic_rows(db_v2, summary, clock, auth)
    result = reset_demo(db_v2, settings_v2, clock, auth=auth, reseed=False)
    assert result["reseeded"] is False and result["wiped"]["pill_drops"] == 1
    for model in DYNAMIC_MODELS:
        assert _count(db_v2, model) == 0, model.__tablename__
    comps = _comps(db_v2)
    assert comps[0].pill_count == 4                          # the demo state itself is untouched
    with db_v2.session() as s:
        assert s.get(Device, summary["device_id"]).manual_cooldown_minutes == 1
