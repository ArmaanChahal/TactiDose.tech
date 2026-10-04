"""Scheduled auto-drops (ARCHITECTURE v2 §6): due doses drop at their time, oldest first, retry
after failures until the window closes, then become MISSED with a MISSED_DOSE notification;
uncertain outcomes are never retried; startup recovery; STOP pauses a round."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tactidose.core.bus import Topic
from tactidose.db.models import PillDrop, Schedule
from tactidose.hardware.protocol import CommandName, DeviceState, Err, HostCode
from tactidose.medication.drops import DropService
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tests.fakes import FakeDropHardware
from tests.test_med_support import (  # noqa: F401 - fixtures
    DISPENSED,
    DISPENSING,
    DUE,
    HARDWARE_ERROR,
    MISSED,
    SCHEDULED,
    Env,
    background,
    env,
    env_template,
    flaky,
)


def add_schedule(e: Env, medication_id: int, hhmm: str) -> int:
    with e.db.session() as s:
        sc = Schedule(medication_id=medication_id, time_of_day=hhmm, created_at=e.clock.now() - timedelta(hours=2),
                      created_by_user_id=e.doctor)
        s.add(sc)
        s.flush()
        return sc.schedule_id


# --------------------------------------------------------------------------- happy path


def test_due_dose_drops_automatically_at_its_time(env: Env):
    assert env.drops.run_scheduled_drops() == 0            # 07:55: the 08:00 dose is not due yet
    env.travel("08:00")
    sub = env.subscribe(Topic.DOSE_UPDATED, Topic.DROP, Topic.NOTIFICATION)
    assert env.drops.run_scheduled_drops() == 1
    ev = env.dose_0800()
    assert (ev.status, ev.dispense_source, ev.attempts, ev.dispensed_at) == (DISPENSED, "schedule", 1, env.clock.now())
    row = env.drop(ev.drop_id)
    assert (row.source, row.status, row.dose_event_id) == ("schedule", "DROPPED", ev.event_id)
    assert row.requested_by_user_id is None
    assert env.hw.sent == ["DROP_SLOT 0"] and env.compartment(0).pill_count == 19
    changes = [e.data["change"] for e in sub.drain() if e.topic == Topic.DOSE_UPDATED]
    assert changes == ["dispensing", "dispensed"]
    assert env.adherence_statuses(ev.event_id)[-2:] == [DISPENSING, DISPENSED]
    assert {n.kind for n in env.notes(env.patient)} == {"PILL_DROPPED"}
    assert env.drops.run_scheduled_drops() == 0            # never twice


def test_auto_drop_disabled_drops_nothing_and_the_dose_is_missed(env: Env):
    env.drops.update_settings(env.patient, auto_drop_enabled=False)
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 0 and env.hw.sent == []
    env.travel("10:01")
    env.tick()
    assert env.dose_0800().status == MISSED
    assert {n.user_id for n in env.notes(kind="MISSED_DOSE")} == {env.patient, env.family, env.doctor}
    env.drops.update_settings(env.patient, auto_drop_enabled=True)
    assert env.drops.run_scheduled_drops() == 0            # a closed window is never dropped late


def test_due_doses_drop_oldest_first_one_at_a_time(env: Env):
    early = add_schedule(env, env.med(2), "07:45")
    same = add_schedule(env, env.med(1), "08:00")
    env.tick()
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 3
    assert env.hw.sent == ["DROP_SLOT 2", "DROP_SLOT 0", "DROP_SLOT 1"]   # 07:45, then 08:00 by event id
    assert env.event_at(early, "07:45").status == DISPENSED
    assert env.event_at(same, "08:00").status == DISPENSED and env.dose_0800().status == DISPENSED


def test_scheduled_doses_are_dropped_late_within_the_window(env: Env):
    env.travel("09:59")
    assert env.drops.run_scheduled_drops() == 1 and env.dose_0800().status == DISPENSED


def test_doses_of_a_previous_owner_are_never_dropped(env: Env):
    env.set_event(env.dose_0800().event_id, user_id=env.family)   # e.g. the device was re-bound since
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 0 and env.hw.sent == []


# --------------------------------------------------------------------------- failures and retries


def test_failed_auto_drop_retries_until_the_window_closes_then_missed(env: Env):
    env.settings.auto_drop_retry_minutes = 30              # shared settings object: fewer retries to walk through
    env.hw.script(CommandName.DROP_SLOT, Err.INVALID_STATE, times=100)
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    ev = env.dose_0800()
    assert (ev.status, ev.needs_review, ev.attempts) == (HARDWARE_ERROR, False, 1)
    assert ev.next_attempt_at == env.at("08:30") and ev.hardware_result == "ERR INVALID_STATE"
    env.travel("08:29")
    assert env.drops.run_scheduled_drops() == 0            # waits for its retry time
    attempts = 1
    for hhmm in ("08:30", "09:00", "09:30", "10:00"):      # the window ends at 10:00 (inclusive)
        env.travel(hhmm)
        env.tick()
        attempts += env.drops.run_scheduled_drops()
    assert attempts == 5 and env.dose_0800().attempts == 5
    assert len(env.drop_commands()) == 5
    assert len(env.notes(env.patient, "DROP_FAILED")) == 1     # one failure notice per dose, not per retry
    sub = env.subscribe(Topic.PATIENT_STATUS)
    env.travel("10:00", seconds=1)
    env.tick()
    ev = env.dose_0800()
    assert ev.status == MISSED and ev.next_attempt_at is None and ev.missed_at == env.clock.now()
    missed = env.notes(kind="MISSED_DOSE")
    assert {n.user_id for n in missed} == {env.patient, env.family, env.doctor}
    assert missed[0].body == "The 8:00 AM dose of Vitamin C (demo candy) was not dropped."
    assert missed[0].data["dose_event_id"] == ev.event_id and missed[0].data["container_number"] == 1
    assert {"patient_id": env.patient, "reason": "missed"} in [e.data for e in sub.drain()]
    assert env.drops.run_scheduled_drops() == 0


def test_retry_succeeds_once_the_device_works_again(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.NOT_CONNECTED)
    env.travel("08:00")
    env.drops.run_scheduled_drops()
    env.travel("08:05")
    assert env.drops.run_scheduled_drops() == 1
    ev = env.dose_0800()
    assert (ev.status, ev.attempts) == (DISPENSED, 2)
    assert [r.status for r in env.drop_rows()] == ["FAILED", "DROPPED"]


def test_device_unavailable_is_retried_and_alerted_once(env: Env):
    env.hw.set_state(connected=False)
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    ev = env.dose_0800()
    assert ev.status == DUE and ev.next_attempt_at == env.at("08:05") and ev.attempts == 0
    alert = env.notes(env.patient, "DEVICE_ALERT")
    assert len(alert) == 1 and alert[0].body == (
        "The 8:00 AM dose of Vitamin C (demo candy) could not drop. The dispenser is not connected or needs "
        "attention. It will be tried again until 10:00 AM.")
    assert {n.user_id for n in env.notes(kind="DEVICE_ALERT")} == {env.patient, env.family, env.doctor}
    env.travel("08:05")
    assert env.drops.run_scheduled_drops() == 1
    assert len(env.notes(env.patient, "DEVICE_ALERT")) == 1    # alerted once per dose
    env.hw.set_state(connected=True)
    env.travel("08:10")
    assert env.drops.run_scheduled_drops() == 1 and env.dose_0800().status == DISPENSED
    assert [r.reason for r in env.drop_rows()] == ["DEVICE_UNAVAILABLE", "DEVICE_UNAVAILABLE", None]


def test_fault_after_a_motor_error_is_retried_as_unavailable(env: Env):
    env.hw.script(CommandName.DROP_SLOT, Err.MOTOR_FAULT)    # the fake enters FAULT
    env.travel("08:00")
    env.drops.run_scheduled_drops()
    env.travel("08:05")
    env.drops.run_scheduled_drops()
    assert [(r.status, r.reason) for r in env.drop_rows()] == [("FAILED", "MOTOR_FAULT"),
                                                               ("DENIED", "DEVICE_UNAVAILABLE")]
    assert env.hw.snapshot().state is DeviceState.FAULT and len(env.drop_commands()) == 1


def test_empty_container_for_a_scheduled_dose(env: Env):
    env.set_compartment(0, pill_count=0)
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    ev = env.dose_0800()
    assert ev.status == DUE and ev.next_attempt_at == env.at("08:05")
    assert len(env.notes(env.patient, "EMPTY")) == 1 and env.hw.sent == []
    env.travel("08:05")
    env.drops.run_scheduled_drops()
    assert len(env.notes(env.patient, "EMPTY")) == 1           # same empty episode
    env.compartments.refill(0, set=10, patient_id=env.patient)
    env.travel("08:10")
    assert env.drops.run_scheduled_drops() == 1 and env.dose_0800().status == DISPENSED


def test_unconfigured_container_for_a_scheduled_dose_alerts(env: Env):
    env.compartments.assign(0, None, patient_id=env.patient)
    env.travel("08:00")
    env.drops.run_scheduled_drops()
    row = env.drop_rows()[-1]
    assert (row.status, row.reason) == ("DENIED", "NO_MEDICATION")
    alert = env.notes(env.patient, "DEVICE_ALERT")
    assert len(alert) == 1 and "Its medication is not set up in a container." in alert[0].body


def test_uncertain_auto_drop_is_never_retried(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    ev = env.dose_0800()
    assert (ev.status, ev.needs_review, ev.next_attempt_at) == (HARDWARE_ERROR, True, None)
    assert ev.hardware_result == "UNCERTAIN TIMEOUT" and ev.drop_id == env.drop_rows()[0].drop_id
    env.travel("09:00")
    assert env.drops.run_scheduled_drops() == 0
    env.travel("12:00")
    env.tick()
    assert env.dose_0800().status == HARDWARE_ERROR          # stays locked for review, never MISSED silently
    assert len(env.drop_commands()) == 1


def test_resolving_an_uncertain_auto_drop_as_not_dropped_lets_it_drop(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.travel("08:00")
    env.drops.run_scheduled_drops()
    unsure = env.drop_rows()[0]
    env.travel("08:20")
    view = env.drops.resolve_drop(unsure.drop_id, dropped=False, by_user_id=env.doctor,
                                  patient_id=env.patient)
    assert view["status"] == "FAILED"
    ev = env.dose_0800()
    assert (ev.status, ev.needs_review, ev.drop_id) == (DUE, False, None)
    assert env.drops.run_scheduled_drops() == 1 and env.dose_0800().status == DISPENSED


def test_resolving_an_uncertain_auto_drop_as_dropped(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.travel("08:00")
    env.drops.run_scheduled_drops()
    unsure = env.drop_rows()[0]
    env.drops.resolve_drop(unsure.drop_id, dropped=True, by_user_id=env.doctor)
    ev = env.dose_0800()
    assert (ev.status, ev.needs_review, ev.drop_id) == (DISPENSED, False, unsure.drop_id)
    assert ev.dispensed_at == env.at("08:00")
    assert env.compartment(0).pill_count == 19
    assert env.drops.run_scheduled_drops() == 0


def test_resolving_after_the_window_closed_marks_the_dose_missed(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.travel("08:00")
    env.drops.run_scheduled_drops()
    env.travel("11:00")
    env.drops.resolve_drop(env.drop_rows()[0].drop_id, dropped=False)
    assert env.dose_0800().status == MISSED
    assert len(env.notes(env.patient, "MISSED_DOSE")) == 1


def test_resolving_a_review_releases_doses_waiting_for_a_retry(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    env.set_cooldown(0)
    env.manual(1)                                             # uncertain manual drop of Calcium
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1               # 08:00 dose blocked: NEEDS_REVIEW + retry wait
    assert env.drop_rows()[-1].reason == "NEEDS_REVIEW" and env.dose_0800().next_attempt_at == env.at("08:05")
    env.travel("08:01")
    env.drops.resolve_drop(env.drop_rows()[0].drop_id, dropped=False)
    assert env.dose_0800().next_attempt_at is None
    assert env.drops.run_scheduled_drops() == 1 and env.dose_0800().status == DISPENSED


def test_skipped_doses_are_not_dropped(env: Env):
    view = env.drops.skip_dose(env.dose_0800().event_id, note="Doctor paused it", by_user_id=env.doctor)
    assert view["status"] == "CANCELLED" and view["review_note"] == "Doctor paused it"
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 0 and env.hw.sent == []


# --------------------------------------------------------------------------- STOP during a round


def test_stop_during_a_scheduled_drop_pauses_the_round(env: Env):
    add_schedule(env, env.med(1), "08:00")
    env.tick()
    env.travel("08:00")
    env.hw.hold(CommandName.DROP_SLOT)
    t, out = background(env.drops.run_scheduled_drops)
    assert env.hw.holding.wait(5)
    assert env.drops.interrupt() is True
    t.join(5)
    assert out["value"] == 1
    ev = env.dose_0800()
    assert (ev.status, ev.hardware_result, ev.next_attempt_at) == (HARDWARE_ERROR, "ERR STOPPED", env.at("08:05"))
    deferred = [e for e in env.events() if e.medication_id == env.med(1) and e.scheduled_at == env.at("08:00")]
    assert deferred[0].status == DUE and deferred[0].next_attempt_at == env.at("08:05")
    assert env.hw.sent == ["DROP_SLOT 0", "STOP"]
    assert [(r.status, r.reason) for r in env.drop_rows()] == [("FAILED", "STOPPED")]
    env.travel("08:05")
    assert env.drops.run_scheduled_drops() == 2               # re-homes, then both drop
    assert env.hw.sent[2:] == ["HOME", "DROP_SLOT 0", "DROP_SLOT 1"]


def test_stop_between_two_drops_defers_the_rest(env: Env, monkeypatch: pytest.MonkeyPatch):
    add_schedule(env, env.med(1), "08:00")
    env.tick()
    env.travel("08:00")
    real = env.drops._request_safe

    def first_then_stop(req, **kw):
        out = real(req, **kw)
        env.drops._interrupt.set()                            # STOP pressed while idle between drops
        return out

    monkeypatch.setattr(env.drops, "_request_safe", first_then_stop)
    assert env.drops.run_scheduled_drops() == 1
    assert env.drop_commands() == ["DROP_SLOT 0"]
    second = [e for e in env.events() if e.medication_id == env.med(1) and e.scheduled_at == env.at("08:00")][0]
    assert second.status == DUE and second.next_attempt_at == env.at("08:05")


# --------------------------------------------------------------------------- startup recovery


def test_recover_on_startup_marks_in_flight_drops_and_doses_for_review(env: Env):
    ev = env.dose_0800()
    env.set_event(ev.event_id, status=DISPENSING, attempts=1, slot_number=0)
    with env.db.session() as s:
        row = PillDrop(patient_id=env.patient, device_id=env.settings.device_id, slot_number=0,
                       compartment_id=env.ids["compartment_ids"][0],
                       medication_id=env.med(0), medication_name="Vitamin C (demo candy)", source="schedule",
                       dose_event_id=ev.event_id, status="UNCERTAIN", pill_count_before=20,
                       requested_at=env.clock.now() - timedelta(minutes=1), completed_at=None)
        s.add(row)
        s.flush()
        drop_id = row.drop_id
    sub = env.subscribe(Topic.DROP, Topic.DOSE_UPDATED, Topic.NOTIFICATION, Topic.PATIENT_STATUS)
    fresh = DropService(env.db, FakeDropHardware(), env.clock, env.settings,
                        notifications=env.notifications, bus=env.bus)
    assert fresh.recover_on_startup() == 2
    row = env.drop(drop_id)
    assert (row.status, row.needs_review, row.reason) == ("UNCERTAIN", True, "RESTART")
    assert row.completed_at == env.clock.now()
    assert row.hardware_result == "UNCERTAIN RESTART"
    ev = env.event(ev.event_id)
    assert (ev.status, ev.needs_review, ev.hardware_result) == (HARDWARE_ERROR, True, "UNCERTAIN RESTART")
    assert {n.user_id for n in env.notes(kind="DROP_UNCERTAIN")} == {env.patient, env.family, env.doctor}
    assert {e.topic for e in sub.drain()} == {Topic.DROP, Topic.DOSE_UPDATED, Topic.NOTIFICATION, Topic.PATIENT_STATUS}
    assert fresh.recover_on_startup() == 0                    # idempotent
    blocked = fresh.request_drop(patient_id=env.patient, source="manual", slot=1)
    assert blocked.reason == "NEEDS_REVIEW"
    assert env.devlog("DROP_RECOVERED")[0].detail["drop_id"] == drop_id


def test_recover_on_startup_survives_a_database_error(env: Env, flaky):
    flaky.fail = True
    assert env.drops.recover_on_startup() == 0


def test_run_scheduled_drops_never_raises_on_db_error(env: Env, flaky):
    env.travel("08:00")
    flaky.fail = True
    assert env.drops.run_scheduled_drops() == 0
    flaky.fail = False
    assert env.drops.run_scheduled_drops() == 1


def test_demo_dose_now_is_dropped_by_the_next_round(env: Env):
    env.travel("10:30")
    out = env.drops.create_demo_dose_now(env.patient, env.med(2), by_user_id=env.doctor)
    assert out["event"]["status"] == DUE and out["event"]["scheduled_local"].endswith("10:30:00-07:00")
    assert out["schedule"]["created_by_user_id"] == env.doctor and out["schedule"]["time_of_day"] == "10:30"
    assert env.drops.run_scheduled_drops() == 1
    assert env.event(out["event"]["event_id"]).status == DISPENSED and env.hw.sent == ["DROP_SLOT 2"]
    with pytest.raises(ConflictError):                     # same minute, already dropped: no second dose
        env.drops.create_demo_dose_now(env.patient, env.med(2))
    with pytest.raises(NotFoundError):
        env.drops.create_demo_dose_now(env.patient, 9999)
    with pytest.raises(ValidationError):
        env.drops.create_demo_dose_now(env.family)         # no dispenser


def test_demo_dose_defaults_to_the_lowest_container(env: Env):
    env.travel("11:00")
    out = env.drops.create_demo_dose_now(env.patient)
    assert out["event"]["medication_id"] == env.med(0) and out["event"]["slot"] == 0
    assert out["event"]["status"] == DUE


def test_scheduled_dose_drops_from_the_container_its_medication_is_in_now(env: Env):
    env.compartments.assign(2, env.med(0), patient_id=env.patient, pill_count=8)   # Vitamin C moved after generation
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    assert env.hw.sent == ["DROP_SLOT 2"]
    ev = env.dose_0800()
    assert (ev.status, ev.slot_number) == (DISPENSED, 2) and env.compartment(2).pill_count == 7
