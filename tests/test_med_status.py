"""DropService read models and doctor/family operations: patient_status, recent_drops, doses,
settings, resolve_drop, skip_dose, confirm_taken, serializers (shapes from docs/API.md v2)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.exc import OperationalError

from tactidose.core.bus import Topic
from tactidose.db.models import DoseEvent, PillDrop, User
from tactidose.hardware.protocol import CommandName, DeviceState, HostCode
from tactidose.medication.compartments import assigned_slots
from tactidose.medication.drops import dose_to_view, drop_to_view
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    DUE,
    HARDWARE_ERROR,
    MISSED,
    SCHEDULED,
    TAKEN,
    Env,
    env,
    env_template,
    flaky,
)

#: API.md v2 shared objects (keys every view must carry; views may add a few more).
CONTAINER_KEYS = {"slot", "container_number", "compartment_id", "medication_id", "medication_name", "strength",
                  "pill_count", "capacity", "low_stock_threshold", "low_stock", "empty", "loaded_at"}
DROP_VIEW_KEYS = {"drop_id", "requested_at", "completed_at", "requested_local", "slot", "container_number",
                  "medication_id", "medication_name", "source", "status", "reason", "hardware_result",
                  "pill_count_before", "pill_count_after", "dose_event_id", "conversation_id",
                  "requested_by_user_id", "needs_review", "review_note"}
DOSE_VIEW_KEYS = {"event_id", "schedule_id", "medication_id", "medication_name", "slot", "container_number",
                  "scheduled_at", "scheduled_local", "status", "drop_id", "dispensed_at", "dispense_source",
                  "confirmed_taken_at", "missed_at", "needs_review", "attempts", "hardware_result"}
STATUS_KEYS = {"patient_id", "display_name", "now_local", "containers", "cooldown_minutes", "cooldown_remaining_s",
               "next_manual_allowed_at", "last_drop", "today", "next_scheduled", "auto_drop_enabled", "device",
               "alerts"}
OUTCOME_KEYS = {"status", "reason", "message", "drop_id", "slot", "container_number", "medication_id",
                "medication_name", "source", "pill_count_after", "cooldown_remaining_s", "next_allowed_at", "hardware"}


def add_patient(e: Env) -> int:
    with e.db.session() as s:
        u = User(display_name="Pat Two", role="patient", email="pat2@test.tactidose")
        s.add(u)
        s.flush()
        return u.user_id


# --------------------------------------------------------------------------- patient_status


def test_patient_status_shape(env: Env):
    env.set_compartment(1, pill_count=2)
    env.set_compartment(2, pill_count=0)
    out = env.manual(0)
    assert set(out.to_dict()) == OUTCOME_KEYS
    status = env.drops.patient_status(env.patient)
    d = status.to_dict()
    assert set(d) == STATUS_KEYS
    assert (d["patient_id"], d["display_name"]) == (env.patient, "Alex Rivera")
    assert d["now_local"] == "2026-10-05T07:55:00-07:00"
    assert [c["pill_count"] for c in d["containers"]] == [19, 2, 0]
    assert all(set(c) == CONTAINER_KEYS for c in d["containers"])
    assert [c["container_number"] for c in d["containers"]] == [1, 2, 3]
    assert (d["containers"][1]["low_stock"], d["containers"][2]["empty"]) == (True, True)
    assert (d["cooldown_minutes"], d["cooldown_remaining_s"]) == (60, 3600)
    assert d["next_manual_allowed_at"] == "2026-10-05T08:55:00-07:00"
    assert set(d["last_drop"]) >= DROP_VIEW_KEYS and d["last_drop"]["drop_id"] == out.drop_id
    assert [x["scheduled_local"][11:16] for x in d["today"]] == ["08:00", "13:00", "20:00"]
    assert all(set(x) >= DOSE_VIEW_KEYS for x in d["today"])
    assert [x["status"] for x in d["today"]] == [DISPENSED, SCHEDULED, SCHEDULED]
    assert d["today"][1]["container_number"] == 2 and d["today"][1]["medication_name"] == "Calcium (demo token)"
    assert d["next_scheduled"]["scheduled_local"] == "2026-10-05T13:00:00-07:00"
    assert d["auto_drop_enabled"] is True
    assert d["device"]["connected"] is True and d["device"]["proto"] == "1.1" and d["device"]["state"] == "READY"
    assert d["alerts"] == [
        {"kind": "LOW_STOCK", "slot": 1, "message": "Container 2 has 2 pills left."},
        {"kind": "EMPTY", "slot": 2, "message": "Container 3 (Omega-3 (demo candy)) is empty."},
    ]


def test_patient_status_alerts_for_device_review_and_missed(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.manual(1)
    env.hw.set_state(connected=False)
    env.travel("10:01")
    env.tick()
    kinds = [a["kind"] for a in env.drops.patient_status(env.patient).alerts]
    assert kinds == ["DEVICE_ALERT", "DROP_UNCERTAIN", "MISSED_DOSE"]
    alerts = env.drops.patient_status(env.patient).alerts
    assert alerts[0]["message"] == "The dispenser is not connected."
    assert alerts[1]["message"] == "The 7:55 AM drop from container 2 needs to be checked by a caregiver."
    assert alerts[2]["message"] == "The 8:00 AM dose of Vitamin C (demo candy) was missed."
    env.hw.set_state(connected=True, state=DeviceState.FAULT)
    assert env.drops.patient_status(env.patient).alerts[0]["message"].startswith("The dispenser needs attention")


def test_patient_status_without_a_dispenser(env: Env):
    other = add_patient(env)
    st = env.drops.patient_status(other)
    assert st.containers == () and st.device == {} and st.cooldown_minutes == 0 and st.auto_drop_enabled is False
    assert st.alerts == ({"kind": "DEVICE_ALERT", "message": "No dispenser is linked to this account."},)
    assert st.last_drop is None and st.today == () and st.next_scheduled is None
    with pytest.raises(NotFoundError):
        env.drops.patient_status(999999)
    with pytest.raises(NotFoundError):
        env.drops.patient_status("1")  # type: ignore[arg-type]


def test_patient_status_last_drop_ignores_denials_and_in_flight_rows(env: Env):
    first = env.manual(0)
    env.agent(1)                                                  # denied by the cooldown
    with env.db.session() as s:
        s.add(PillDrop(patient_id=env.patient, device_id=env.settings.device_id, slot_number=2, source="manual",
                       status="UNCERTAIN", requested_at=env.clock.now(), completed_at=None))
    assert env.drops.patient_status(env.patient).last_drop["drop_id"] == first.drop_id


def test_patient_status_database_errors_propagate(env: Env, flaky):
    flaky.fail = True
    with pytest.raises(OperationalError):                         # no made-up status when the DB is down
        env.drops.patient_status(env.patient)


def test_next_scheduled_dose(env: Env):
    nxt = env.drops.next_scheduled_dose(env.patient)
    assert nxt["scheduled_local"] == "2026-10-05T08:00:00-07:00" and nxt["status"] == DUE
    env.travel("20:30")
    env.tick()
    assert env.drops.next_scheduled_dose(env.patient)["scheduled_local"] == "2026-10-06T08:00:00-07:00"
    assert env.drops.next_scheduled_dose(add_patient(env)) is None


# --------------------------------------------------------------------------- history


def test_recent_drops_newest_first_with_filters(env: Env):
    env.set_cooldown(0)
    a = env.manual(0)
    env.advance(minutes=1)
    env.set_compartment(2, pill_count=0)
    b = env.manual(2)
    env.advance(minutes=1)
    c = env.agent(1)
    rows = env.drops.recent_drops(env.patient)
    assert [r["drop_id"] for r in rows] == [c.drop_id, b.drop_id, a.drop_id]
    assert all(set(r) >= DROP_VIEW_KEYS for r in rows)
    assert [r["drop_id"] for r in env.drops.recent_drops(env.patient, status="denied")] == [b.drop_id]
    assert [r["drop_id"] for r in env.drops.recent_drops(env.patient, limit=1)] == [c.drop_id]
    env.advance(minutes=60 * 24 * 2)
    assert env.drops.recent_drops(env.patient, days=1) == []
    assert len(env.drops.recent_drops(env.patient, days=3)) == 3
    assert env.drops.recent_drops(env.family) == []


@pytest.mark.parametrize("kwargs", [dict(days=0), dict(days=367), dict(days=True), dict(limit=0), dict(limit=1001),
                                    dict(status="LOST")])
def test_recent_drops_validation(env: Env, kwargs):
    with pytest.raises(ValidationError):
        env.drops.recent_drops(env.patient, **kwargs)


def test_get_drop_is_scoped_to_the_patient(env: Env):
    out = env.manual(0)
    assert env.drops.get_drop(out.drop_id)["status"] == "DROPPED"
    assert env.drops.get_drop(out.drop_id, patient_id=env.patient)["drop_id"] == out.drop_id
    with pytest.raises(NotFoundError):
        env.drops.get_drop(out.drop_id, patient_id=env.family)
    with pytest.raises(NotFoundError):
        env.drops.get_drop(4242)


def test_list_doses_by_local_date(env: Env):
    today = env.drops.list_doses(env.patient)
    assert [d["scheduled_local"][11:16] for d in today] == ["08:00", "13:00", "20:00"]
    tomorrow = env.drops.list_doses(env.patient, "2026-10-06")
    assert [d["scheduled_local"][11:16] for d in tomorrow] == ["08:00", "13:00"]
    assert env.drops.list_doses(env.patient, date(2026, 10, 6)) == tomorrow
    assert env.drops.list_doses(env.patient, date(2026, 10, 9)) == []
    for bad in ("06/10/2026", "tomorrow", 20261006):
        with pytest.raises(ValidationError):
            env.drops.list_doses(env.patient, bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- settings


def test_settings_get_and_update(env: Env):
    assert env.drops.get_settings(env.patient) == {
        "manual_cooldown_minutes": 60, "auto_drop_enabled": True, "device_id": env.settings.device_id,
        "num_slots": 3, "patient_id": env.patient}
    sub = env.subscribe(Topic.PATIENT_STATUS)
    out = env.drops.update_settings(env.patient, manual_cooldown_minutes=0, auto_drop_enabled=False,
                                    by_user_id=env.doctor)
    assert (out["manual_cooldown_minutes"], out["auto_drop_enabled"]) == (0, False)
    dev = env.device()
    assert (dev.manual_cooldown_minutes, dev.auto_drop_enabled) == (0, False)
    assert [e.data for e in sub.drain()] == [{"patient_id": env.patient, "reason": "settings"}]
    assert env.devlog("DEVICE_SETTINGS_UPDATED")[-1].detail == {
        "changes": {"manual_cooldown_minutes": {"from": 60, "to": 0}, "auto_drop_enabled": {"from": True, "to": False}},
        "by_user_id": env.doctor}
    assert env.drops.update_settings(env.patient) == out                 # nothing to change: no event
    assert sub.drain() == []
    assert env.drops.update_settings(env.patient, manual_cooldown_minutes=1440)["manual_cooldown_minutes"] == 1440


@pytest.mark.parametrize("kwargs", [
    dict(manual_cooldown_minutes=-1), dict(manual_cooldown_minutes=1441), dict(manual_cooldown_minutes=True),
    dict(manual_cooldown_minutes="60"), dict(manual_cooldown_minutes=1.5), dict(auto_drop_enabled="yes"),
    dict(auto_drop_enabled=1),
])
def test_settings_validation(env: Env, kwargs):
    with pytest.raises(ValidationError):
        env.drops.update_settings(env.patient, **kwargs)
    assert env.drops.get_settings(env.patient)["manual_cooldown_minutes"] == 60


def test_settings_need_a_dispenser(env: Env):
    with pytest.raises(NotFoundError):
        env.drops.get_settings(env.family)
    with pytest.raises(NotFoundError):
        env.drops.update_settings(add_patient(env), manual_cooldown_minutes=5)


def test_new_cooldown_applies_to_the_running_cooldown(env: Env):
    env.manual(0)
    env.advance(minutes=20)
    env.drops.update_settings(env.patient, manual_cooldown_minutes=15)
    assert env.manual(1).status == "DROPPED"
    env.drops.update_settings(env.patient, manual_cooldown_minutes=120)
    blocked = env.manual(2)
    assert blocked.next_allowed_at == env.clock.now() + timedelta(minutes=120)


# --------------------------------------------------------------------------- resolve / skip / confirm


def test_resolve_drop_errors(env: Env):
    ok = env.manual(0)
    with pytest.raises(ConflictError):
        env.drops.resolve_drop(ok.drop_id, dropped=True)                  # nothing to review
    with pytest.raises(NotFoundError):
        env.drops.resolve_drop(424242, dropped=True)
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.set_cooldown(0)
    unsure = env.manual(1)
    with pytest.raises(NotFoundError):
        env.drops.resolve_drop(unsure.drop_id, dropped=True, patient_id=env.family)
    with pytest.raises(ValidationError):
        env.drops.resolve_drop(unsure.drop_id, dropped="yes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        env.drops.resolve_drop(unsure.drop_id, dropped=True, note=5)  # type: ignore[arg-type]
    env.drops.resolve_drop(unsure.drop_id, dropped=False)
    with pytest.raises(ConflictError):
        env.drops.resolve_drop(unsure.drop_id, dropped=True)              # already resolved


def test_resolve_refuses_a_drop_still_in_flight_but_accepts_a_stuck_one(env: Env):
    with env.db.session() as s:
        row = PillDrop(patient_id=env.patient, device_id=env.settings.device_id, slot_number=0,
                       compartment_id=env.ids["compartment_ids"][0], medication_id=env.med(0),
                       medication_name="Vitamin C (demo candy)", source="manual", status="UNCERTAIN",
                       pill_count_before=20, requested_at=env.clock.now(), completed_at=None)
        s.add(row)
        s.flush()
        drop_id = row.drop_id
    with pytest.raises(ConflictError):
        env.drops.resolve_drop(drop_id, dropped=True)
    env.advance(minutes=10)
    view = env.drops.resolve_drop(drop_id, dropped=True, by_user_id=env.doctor)
    assert view["status"] == "DROPPED" and view["completed_at"] == env.clock.now().isoformat()
    assert env.compartment(0).pill_count == 19


def test_skip_dose(env: Env):
    sub = env.subscribe(Topic.DOSE_UPDATED, Topic.PATIENT_STATUS)
    ev = env.event_at(env.sched(1), "13:00")
    view = env.drops.skip_dose(ev.event_id, note="  Lunch out  ", by_user_id=env.family, patient_id=env.patient)
    assert set(view) >= DOSE_VIEW_KEYS
    assert (view["status"], view["review_note"]) == (CANCELLED, "Lunch out")
    assert view["cancelled_at"] == env.clock.now().isoformat()
    assert env.adherence_statuses(ev.event_id) == [CANCELLED]
    assert {e.topic for e in sub.drain()} == {Topic.DOSE_UPDATED, Topic.PATIENT_STATUS}
    with pytest.raises(ConflictError):
        env.drops.skip_dose(ev.event_id)
    with pytest.raises(NotFoundError):
        env.drops.skip_dose(env.dose_0800().event_id, patient_id=env.family)
    with pytest.raises(NotFoundError):
        env.drops.skip_dose(31337)
    env.set_event(env.dose_0800().event_id, status=HARDWARE_ERROR, needs_review=False)
    assert env.drops.skip_dose(env.dose_0800().event_id)["status"] == CANCELLED


def test_confirm_taken(env: Env):
    assert env.drops.confirm_taken(env.patient) is None                  # nothing dropped yet
    env.manual(0)
    view = env.drops.confirm_taken(env.patient, medication_id=env.med(0))
    assert (view["status"], view["confirmed_taken_at"]) == (TAKEN, env.clock.now().isoformat())
    assert env.dose_0800().confirm_source == "agent"
    assert env.drops.confirm_taken(env.patient) is None                  # already confirmed
    assert env.drops.confirm_taken(env.family) is None


# --------------------------------------------------------------------------- serializers


def test_drop_and_dose_serializers(env: Env):
    out = env.manual(0, conversation_id=9)
    with env.db.session() as s:
        row = s.get(PillDrop, out.drop_id)
        view = drop_to_view(row, env.clock)
        ev = s.get(DoseEvent, env.dose_0800().event_id)
        dose = dose_to_view(ev, env.clock, assigned_slots(s, env.settings))
        raw = dose_to_view(ev, env.clock)
    assert set(view) >= DROP_VIEW_KEYS and view["requested_local"] == "2026-10-05T07:55:00-07:00"
    assert (view["container_number"], view["conversation_id"], view["in_progress"]) == (1, 9, False)
    assert set(dose) >= DOSE_VIEW_KEYS and dose["scheduled_local"] == "2026-10-05T08:00:00-07:00"
    assert (dose["status"], dose["drop_id"], dose["slot"]) == (DISPENSED, out.drop_id, 0)
    assert dose["dispense_source"] == "manual"
    assert raw["slot"] == 0 and dose["patient_id"] == env.patient
    assert dose["label"] == f"dose_{ev.event_id}"


def test_resolving_after_the_container_was_reloaded_keeps_the_new_count(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    unsure = env.manual(0)
    env.compartments.assign(0, env.med(2), patient_id=env.patient, pill_count=10)   # re-assigned and re-counted
    view = env.drops.resolve_drop(unsure.drop_id, dropped=True, by_user_id=env.doctor)
    assert view["status"] == "DROPPED" and env.compartment(0).pill_count == 10
