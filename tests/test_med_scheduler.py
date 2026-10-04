"""Scheduler v2: schedule math (DST, WEEKLY, horizon, no backfill), DUE/MISSED (+ MISSED_DOSE
notifications), doctor/family CRUD scoped to one patient, created_by_user_id, edit reconciliation."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from tactidose.core.bus import Topic
from tactidose.core.clock import Clock
from tactidose.db.models import DoseEvent, Medication, Schedule, User
from tactidose.medication.errors import NotFoundError, ValidationError
from tactidose.medication.scheduler import (
    Scheduler,
    occurrence_for,
    parse_days,
    parse_frequency,
    parse_time_of_day,
)
from tests.fakes import FakeDropHardware
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    DUE,
    HARDWARE_ERROR,
    KIND_ADHERENCE,
    MISSED,
    SCHEDULED,
    TAKEN,
    Env,
    build,
    env,
    env_template,
    flaky,
)

UTC = timezone.utc
SCHEDULE_KEYS = {"schedule_id", "medication_id", "medication_name", "patient_id", "time_of_day", "frequency",
                 "days_of_week", "active", "created_by_user_id", "created_at", "updated_at"}


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _add_med(e: Env, name: str = "Demo token", slot: int | None = 0) -> int:
    mid = e.catalog.create({"name": name, "strength": "1 token"}, confirmed=True, confirmed_by="test")["medication_id"]
    if slot is not None:
        e.compartments.assign(slot, mid)
    return mid


def _times(e: Env, schedule_id: int) -> list[datetime]:
    with e.db.session() as s:
        return list(s.scalars(select(DoseEvent.scheduled_at).where(DoseEvent.schedule_id == schedule_id)
                              .order_by(DoseEvent.scheduled_at)).all())


def _env(settings, db, bus, tz: str, local: datetime, **overrides) -> Env:
    clock = Clock(tz, frozen_at=local)
    return build(settings, clock, db, bus, FakeDropHardware(), seed=False, timezone=tz, **overrides)


def _other_patient(e: Env) -> int:
    with e.db.session() as s:
        u = User(display_name="Pat Two", role="patient", email="pat2@test.tactidose")
        s.add(u)
        s.flush()
        return u.user_id


# --------------------------------------------------------------------------- materialize


def test_materialize_dates_horizon_and_initial_transitions(env: Env):
    # Mon 07:55, horizon 36h -> events up to Tue 19:55; the seed is 1h old, so nothing is backfilled.
    statuses = {(e.schedule_id, env.clock.to_local(e.scheduled_at).strftime("%a %H:%M")): e.status
                for e in env.events()}
    assert statuses == {
        (env.sched(0), "Mon 08:00"): DUE, (env.sched(0), "Tue 08:00"): SCHEDULED,
        (env.sched(1), "Mon 13:00"): SCHEDULED, (env.sched(1), "Tue 13:00"): SCHEDULED,
        (env.sched(2), "Mon 20:00"): SCHEDULED,
    }
    assert all(e.device_id == env.settings.device_id and e.user_id == env.patient for e in env.events())


def test_materialize_is_idempotent(env: Env):
    before = [(e.event_id, e.scheduled_at) for e in env.events()]
    assert env.scheduler.materialize() == 0
    assert env.tick() == 0
    assert [(e.event_id, e.scheduled_at) for e in env.events()] == before


def test_horizon_rolls_forward_with_time(env: Env):
    tue_2000 = env.at("20:00", date(2026, 10, 6))
    assert tue_2000 not in _times(env, env.sched(2))           # 07:55 + 36h = Tue 19:55
    env.travel("08:05")
    env.tick()
    assert tue_2000 in _times(env, env.sched(2))


def test_longer_horizon_materializes_more_days(settings_v2, clock, db_v2, bus, fake_drop_hw):
    e = build(settings_v2, clock, db_v2, bus, fake_drop_hw, schedule_horizon_hours=72)
    days = {e.clock.to_local(t).date() for t in _times(e, e.sched(0))}
    assert days == {date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)}   # Thu 08:00 > Thu 07:55


def test_new_schedule_never_backfills_closed_windows(env: Env):
    # 07:55: a 06:00 dose window (05:30-08:00) is still open -> created and DUE now.
    open_sched = env.scheduler.create_schedule(env.med(1), "06:00", patient_id=env.patient)
    assert [env.clock.to_local(t).strftime("%d %H:%M") for t in _times(env, open_sched["schedule_id"])] == \
        ["05 06:00", "06 06:00"]
    assert env.event_at(open_sched["schedule_id"], "06:00").status == DUE
    # 05:00 window closed at 07:00 -> today's occurrence is never fabricated (no MISSED).
    closed = env.scheduler.create_schedule(env.med(1), "05:00", patient_id=env.patient)
    assert [env.clock.to_local(t).date() for t in _times(env, closed["schedule_id"])] == [date(2026, 10, 6)]
    assert all(e.status != MISSED for e in env.events() if e.schedule_id == closed["schedule_id"])


def test_weekly_schedule_only_listed_days(settings_v2, clock, db_v2, bus, fake_drop_hw):
    e = build(settings_v2, clock, db_v2, bus, fake_drop_hw, schedule_horizon_hours=72)
    sc = e.scheduler.create_schedule(e.med(1), "09:00", "WEEKLY", ["wed", "MON"])
    assert sc["frequency"] == "WEEKLY" and sc["days_of_week"] == ["MON", "WED"]
    assert e.schedule(sc["schedule_id"]).days_of_week == "MON,WED"
    days = [e.clock.to_local(t).strftime("%a %d") for t in _times(e, sc["schedule_id"])]
    assert days == ["Mon 05", "Wed 07"]


def test_daily_stores_all_days_even_if_days_given(env: Env):
    sc = env.scheduler.create_schedule(env.med(1), "09:00", "daily", ["MON"])
    assert sc["frequency"] == "DAILY" and sc["days_of_week"] == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def test_only_active_confirmed_medications_of_this_patient_are_materialized(env: Env):
    with env.db.session() as s:
        unconfirmed = Medication(user_id=env.patient, name="Unconfirmed", confirmed_by_user=False)
        archived = Medication(user_id=env.patient, name="Archived", confirmed_by_user=True, active=False)
        s.add_all([unconfirmed, archived])
        s.flush()
        created = env.clock.now() - timedelta(days=3)
        s.add_all([Schedule(medication_id=unconfirmed.medication_id, time_of_day="09:00", created_at=created),
                   Schedule(medication_id=archived.medication_id, time_of_day="09:00", created_at=created)])
        ids = {unconfirmed.medication_id, archived.medication_id}
    assert env.tick() == 0
    assert not [e for e in env.events() if e.medication_id in ids]


def test_other_patients_schedules_never_reach_this_device(env: Env):
    other = _other_patient(env)
    foreign = env.catalog.create({"name": "Pat's token"}, confirmed=True, patient_id=other)["medication_id"]
    sc = env.scheduler.create_schedule(foreign, "09:00", patient_id=other)
    assert _times(env, sc["schedule_id"]) == []                 # stored, but Pat has no dispenser


def test_materialize_without_device_is_a_noop(settings_v2, clock, db_v2):
    assert Scheduler(db_v2, clock, settings_v2).materialize() == 0


def test_concurrent_ticks_never_duplicate(settings_v2, clock, db_v2, bus, fake_drop_hw):
    e = build(settings_v2, clock, db_v2, bus, fake_drop_hw, tick=False)
    schedulers = [Scheduler(db_v2, clock, e.settings, bus=bus) for _ in range(4)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(schedulers))

    def run(sch: Scheduler) -> None:
        try:
            barrier.wait(timeout=5)
            sch.tick()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(sch,)) for sch in schedulers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    keys = [(ev.schedule_id, ev.scheduled_at) for ev in e.events()]
    assert len(keys) == len(set(keys)) == 5


# --------------------------------------------------------------------------- DST


def test_spring_forward_vancouver(settings_v2, db_v2, bus):
    # 2026-03-08 02:00 PST -> 03:00 PDT in America/Vancouver.
    e = _env(settings_v2, db_v2, bus, "America/Vancouver", datetime(2026, 3, 6, 12, 0), schedule_horizon_hours=96)
    mid = _add_med(e)
    gap = e.scheduler.create_schedule(mid, "02:30")["schedule_id"]
    morning = e.scheduler.create_schedule(mid, "08:00")["schedule_id"]
    # 02:30 PST; on the 8th the skipped hour fires at 03:30 PDT
    assert _times(e, gap) == [_utc(2026, 3, 7, 10, 30), _utc(2026, 3, 8, 10, 30),
                              _utc(2026, 3, 9, 9, 30), _utc(2026, 3, 10, 9, 30)]
    assert _times(e, morning) == [_utc(2026, 3, 7, 16, 0), _utc(2026, 3, 8, 15, 0),
                                  _utc(2026, 3, 9, 15, 0), _utc(2026, 3, 10, 15, 0)]
    local = [e.clock.to_local(t) for t in _times(e, gap)]
    assert [t.date() for t in local] == [date(2026, 3, d) for d in (7, 8, 9, 10)]   # one dose per local day
    assert local[1].strftime("%H:%M %z") == "03:30 -0700"
    e.clock.freeze(datetime(2026, 3, 8, 3, 20))
    e.tick()
    assert e.event_at(gap, "02:30", date(2026, 3, 8)).status == DUE


def test_fall_back_los_angeles(settings_v2, db_v2, bus):
    # 2026-11-01 02:00 PDT -> 01:00 PST; 01:30 happens twice.
    e = _env(settings_v2, db_v2, bus, "America/Los_Angeles", datetime(2026, 10, 30, 12, 0), schedule_horizon_hours=96)
    mid = _add_med(e)
    ambiguous = e.scheduler.create_schedule(mid, "01:30")["schedule_id"]
    morning = e.scheduler.create_schedule(mid, "08:00")["schedule_id"]
    assert _times(e, ambiguous) == [_utc(2026, 10, 31, 8, 30), _utc(2026, 11, 1, 8, 30),   # first 01:30 (PDT) only
                                    _utc(2026, 11, 2, 9, 30), _utc(2026, 11, 3, 9, 30)]
    assert _times(e, morning) == [_utc(2026, 10, 31, 15, 0), _utc(2026, 11, 1, 16, 0),
                                  _utc(2026, 11, 2, 16, 0), _utc(2026, 11, 3, 16, 0)]
    assert e.tick() == 0


def test_vancouver_november_follows_tz_database(settings_v2, db_v2, bus):
    """tzdata 2026 encodes B.C.'s permanent daylight time; older tzdata has the fall-back. Either
    way: one event per local day at the local wall time."""
    tz = "America/Vancouver"
    e = _env(settings_v2, db_v2, bus, tz, datetime(2026, 10, 30, 12, 0), schedule_horizon_hours=96)
    mid = _add_med(e)
    for hhmm in ("01:30", "08:00"):
        sid = e.scheduler.create_schedule(mid, hhmm)["schedule_id"]
        times = _times(e, sid)
        h, mi = (int(x) for x in hhmm.split(":"))
        expected = [datetime(2026, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)
                    for mo, d in ((10, 31), (11, 1), (11, 2), (11, 3))]
        assert times == expected
        assert [e.clock.to_local(t).strftime("%H:%M") for t in times] == [hhmm] * 4


# --------------------------------------------------------------------------- transitions


def test_due_starts_exactly_at_window_open(settings_v2, clock, db_v2, bus, fake_drop_hw):
    clock.freeze(datetime(2026, 10, 5, 7, 29, 59))
    e = build(settings_v2, clock, db_v2, bus, fake_drop_hw)
    assert e.dose_0800().status == SCHEDULED
    e.travel("07:30")
    assert e.tick() == 1
    assert e.dose_0800().status == DUE


def test_missed_only_after_window_end(env: Env):
    env.travel("10:00")                       # the end is inclusive
    env.tick()
    assert env.dose_0800().status == DUE
    env.travel("10:00", seconds=1)
    env.tick()
    ev = env.dose_0800()
    assert ev.status == MISSED and ev.missed_at == env.clock.now()


def test_missed_doses_notify_the_patient_and_caregivers(env: Env):
    sub = env.subscribe(Topic.PATIENT_STATUS, Topic.NOTIFICATION)
    env.travel("10:01")
    env.tick()
    notes = env.notes(kind="MISSED_DOSE")
    assert {n.user_id for n in notes} == {env.patient, env.family, env.doctor}
    assert notes[0].title == "Missed dose"
    assert notes[0].body == "The 8:00 AM dose of Vitamin C (demo candy) was not dropped."
    assert notes[0].data == {"dose_event_id": env.dose_0800().event_id, "medication_id": env.med(0),
                             "scheduled_at": env.at("08:00").isoformat(), "slot": 0, "container_number": 1}
    events = sub.drain()
    hints = [e.data for e in events if e.topic == Topic.PATIENT_STATUS]
    assert {"patient_id": env.patient, "reason": "missed"} in hints
    assert len([e for e in events if e.topic == Topic.NOTIFICATION]) == 3
    env.travel("10:05")
    env.tick()
    assert len(env.notes(kind="MISSED_DOSE")) == 3              # once per dose


def test_hardware_error_misses_unless_under_review(env: Env):
    a = env.dose_0800()
    b = env.event_at(env.sched(1), "13:00")
    env.set_event(a.event_id, status=HARDWARE_ERROR, needs_review=False, attempts=1,
                  next_attempt_at=env.at("08:30"))
    env.set_event(b.event_id, status=HARDWARE_ERROR, needs_review=True, attempts=1)
    env.travel("23:59")
    env.tick()
    a2 = env.event(a.event_id)
    assert a2.status == MISSED and a2.next_attempt_at is None
    assert env.event(b.event_id).status == HARDWARE_ERROR     # a caregiver must review uncertain doses


def test_transitions_write_outbox_log_and_publish(env: Env):
    sub = env.subscribe(Topic.DOSE_UPDATED)
    ev = env.dose_0800()
    assert env.adherence_statuses(ev.event_id) == [DUE]
    assert [r.event_id for r in env.devlog("DOSE_DUE")] == [ev.event_id]
    env.travel("10:01")
    env.tick()
    assert env.adherence_statuses(ev.event_id) == [DUE, MISSED]
    assert ev.event_id in [r.event_id for r in env.devlog("DOSE_MISSED")]
    assert env.devlog("DOSE_MISSED")[0].created_at == env.clock.now()     # audit rows follow the demo clock
    payloads = [e.data for e in sub.drain()]
    assert (ev.event_id, "missed") in [(p["event_id"], p["change"]) for p in payloads]
    assert any(p["change"] == "created" for p in payloads)              # Tue 20:00 materialized
    assert all(p["patient_id"] == env.patient for p in payloads)
    payload = next(r for r in env.outbox(KIND_ADHERENCE) if r.payload["final_status"] == MISSED)
    assert "name" not in str(payload.payload) and payload.payload["missed"] is True


def test_creation_alone_writes_no_outbox_row(env: Env):
    ev = env.event_at(env.sched(1), "13:00")
    assert ev.status == SCHEDULED
    assert env.adherence_statuses(ev.event_id) == []
    assert ev.event_id in [r.event_id for r in env.devlog("DOSE_MATERIALIZED")]


def test_tick_never_raises_on_db_error(env: Env, flaky):
    flaky.fail = True
    assert env.tick() == 0


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("bad", ["24:00", "12:60", "8", "ab:cd", "08:00:00", "", " : ", 800, None])
def test_create_schedule_rejects_bad_times(env: Env, bad):
    with pytest.raises(ValidationError):
        env.scheduler.create_schedule(env.med(0), bad)


def test_time_frequency_and_day_parsing():
    assert parse_time_of_day("8:05") == "08:05" and parse_time_of_day(" 23:59 ") == "23:59"
    assert parse_frequency("weekly") == "WEEKLY"
    with pytest.raises(ValidationError):
        parse_frequency("MONTHLY")
    with pytest.raises(ValidationError):
        parse_frequency(7)
    assert parse_days("fri, Monday ,mon", "WEEKLY") == ["MON", "FRI"]
    assert parse_days({"SUN"}, "WEEKLY") == ["SUN"]
    assert parse_days(None, "DAILY") == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    for bad in (None, [], "", ["XYZ"], [1], 5):
        with pytest.raises(ValidationError):
            parse_days(bad, "WEEKLY")


def test_create_schedule_requires_existing_confirmed_active_medication(env: Env):
    with pytest.raises(NotFoundError):
        env.scheduler.create_schedule(9999, "08:00")
    with pytest.raises(NotFoundError):
        env.scheduler.create_schedule(True, "08:00")
    with env.db.session() as s:
        raw = Medication(user_id=env.patient, name="Typed but not confirmed", confirmed_by_user=False)
        s.add(raw)
        s.flush()
        raw_id = raw.medication_id
    with pytest.raises(ValidationError):
        env.scheduler.create_schedule(raw_id, "08:00")
    env.catalog.archive(env.med(1))
    with pytest.raises(ValidationError):
        env.scheduler.create_schedule(env.med(1), "08:00")
    with pytest.raises(ValidationError):
        env.scheduler.create_schedule(env.med(0), "09:00", "WEEKLY", [])
    with pytest.raises(ValidationError):
        env.scheduler.create_schedule(env.med(0), "09:00", created_by_user_id="dr")  # type: ignore[arg-type]


def test_list_schedules_shape_and_filter(env: Env):
    rows = env.scheduler.list_schedules()
    assert [r["time_of_day"] for r in rows] == ["08:00", "13:00", "20:00"]
    assert all(set(r) == SCHEDULE_KEYS for r in rows)
    assert rows[0]["medication_name"] == "Vitamin C (demo candy)" and rows[0]["active"] is True
    assert (rows[0]["patient_id"], rows[0]["created_by_user_id"]) == (env.patient, env.doctor)
    assert env.scheduler.list_schedules(patient_id=env.patient) == rows
    env.scheduler.deactivate_schedule(env.sched(2))
    assert [r["schedule_id"] for r in env.scheduler.list_schedules()] == [env.sched(0), env.sched(1)]
    assert len(env.scheduler.list_schedules(include_inactive=True)) == 3
    assert env.scheduler.get_schedule(env.sched(2))["active"] is False
    with pytest.raises(NotFoundError):
        env.scheduler.get_schedule(424242)


# --------------------------------------------------------------------------- v2: authorship & patient scope


def test_created_by_user_id_is_stored_and_edits_are_audited(env: Env):
    sub = env.subscribe(Topic.PATIENT_STATUS, Topic.DATA_CHANGED)
    sc = env.scheduler.create_schedule(env.med(2), "21:30", created_by_user_id=env.family, patient_id=env.patient)
    assert sc["created_by_user_id"] == env.family and env.schedule(sc["schedule_id"]).created_by_user_id == env.family
    assert env.devlog("SCHEDULE_CREATED")[-1].detail["by_user_id"] == env.family
    env.scheduler.update_schedule(sc["schedule_id"], patient_id=env.patient, by_user_id=env.doctor, time_of_day="21:45")
    assert env.devlog("SCHEDULE_UPDATED")[-1].detail["by_user_id"] == env.doctor
    published = [e.data for e in sub.drain()]
    assert {"patient_id": env.patient, "reason": "schedule"} in published
    assert {"entity": "schedule", "id": sc["schedule_id"], "patient_id": env.patient} in published


def test_crud_is_scoped_to_one_patient(env: Env):
    other = _other_patient(env)
    assert env.scheduler.list_schedules(patient_id=other) == []
    with pytest.raises(NotFoundError):
        env.scheduler.get_schedule(env.sched(0), patient_id=other)
    with pytest.raises(NotFoundError):
        env.scheduler.create_schedule(env.med(0), "09:00", patient_id=other)
    with pytest.raises(NotFoundError):
        env.scheduler.update_schedule(env.sched(0), patient_id=other, time_of_day="09:00")
    with pytest.raises(NotFoundError):
        env.scheduler.delete_schedule(env.sched(0), patient_id=other)
    assert env.schedule(env.sched(0)).time_of_day == "08:00" and env.schedule(env.sched(0)).active
    foreign = env.catalog.create({"name": "Pat's token"}, confirmed=True, patient_id=other)["medication_id"]
    with pytest.raises(ValidationError):            # without a scope: another patient's medication is refused
        env.scheduler.create_schedule(foreign, "09:00")


def test_delete_schedule_is_a_soft_delete(env: Env):
    env.scheduler.delete_schedule(env.sched(1), patient_id=env.patient, by_user_id=env.doctor)
    assert env.schedule(env.sched(1)).active is False
    assert env.event_at(env.sched(1), "13:00") is None              # untouched future event removed
    assert env.devlog("SCHEDULE_DEACTIVATED")[-1].detail["by_user_id"] == env.doctor


# --------------------------------------------------------------------------- update / deactivate


def test_update_time_cancels_stale_in_window_and_deletes_future(env: Env):
    today = env.dose_0800()
    tomorrow = env.event_at(env.sched(0), "08:00", date(2026, 10, 6))
    sub = env.subscribe(Topic.DOSE_UPDATED)
    out = env.scheduler.update_schedule(env.sched(0), time_of_day="8:30")
    assert out["time_of_day"] == "08:30"
    assert env.event(today.event_id).status == CANCELLED                 # in window -> cancelled
    with env.db.session() as s:
        assert s.get(DoseEvent, tomorrow.event_id) is None               # never opened -> deleted
    assert env.adherence_statuses(today.event_id) == [DUE, CANCELLED]
    new_today = env.event_at(env.sched(0), "08:30")
    assert new_today is not None and new_today.status == SCHEDULED       # window opens 08:00
    assert env.event_at(env.sched(0), "08:30", date(2026, 10, 6)) is not None
    changes = {(e.data["event_id"], e.data["change"]) for e in sub.drain()}
    assert {(today.event_id, "cancelled"), (tomorrow.event_id, "deleted")} <= changes


def test_update_days_keeps_events_that_still_match(env: Env):
    today = env.dose_0800()                                # Monday
    tomorrow = env.event_at(env.sched(0), "08:00", date(2026, 10, 6))
    env.scheduler.update_schedule(env.sched(0), frequency="WEEKLY", days_of_week=["TUE"])
    assert env.event(today.event_id).status == CANCELLED
    assert env.event(tomorrow.event_id).status == SCHEDULED   # same row kept (same id)


def test_deactivate_cancels_in_window_and_deletes_future_but_keeps_dropped(env: Env):
    dropped = env.dose_0800()
    env.set_event(dropped.event_id, status=DISPENSED, dispensed_at=env.clock.now())
    tomorrow = env.event_at(env.sched(0), "08:00", date(2026, 10, 6))
    evening = env.event_at(env.sched(2), "20:00")
    env.scheduler.deactivate_schedule(env.sched(0))
    assert env.event(dropped.event_id).status == DISPENSED
    with env.db.session() as s:
        assert s.get(DoseEvent, tomorrow.event_id) is None
    assert env.event(evening.event_id).status == SCHEDULED     # other schedule untouched
    env.tick()
    assert env.event_at(env.sched(0), "08:00", date(2026, 10, 6)) is None


def test_redefining_schedule_does_not_fabricate_missed_dose(env: Env):
    env.travel("09:00")
    env.tick()
    old = env.dose_0800()
    assert old.status == DUE
    env.scheduler.update_schedule(env.sched(0), time_of_day="06:00")   # 06:00 window closed at 08:00
    assert env.event(old.event_id).status == CANCELLED
    assert env.event_at(env.sched(0), "06:00") is None
    assert env.event_at(env.sched(0), "06:00", date(2026, 10, 6)) is not None
    assert env.schedule(env.sched(0)).created_at == env.clock.now()
    assert env.notes(kind="MISSED_DOSE") == []


def test_edited_time_never_gives_second_dose_same_day(env: Env):
    today = env.dose_0800()
    env.set_event(today.event_id, status=TAKEN, dispensed_at=env.clock.now(), confirmed_taken_at=env.clock.now())
    env.scheduler.update_schedule(env.sched(0), time_of_day="09:00")
    assert env.event(today.event_id).status == TAKEN
    assert env.event_at(env.sched(0), "09:00") is None                       # already dropped today
    assert env.event_at(env.sched(0), "09:00", date(2026, 10, 6)) is not None


def test_reactivation_regenerates_without_backfill(env: Env):
    env.scheduler.deactivate_schedule(env.sched(1))
    assert env.event_at(env.sched(1), "13:00") is None
    env.travel("16:00")
    out = env.scheduler.update_schedule(env.sched(1), active=True)
    assert out["active"] is True
    assert env.event_at(env.sched(1), "13:00") is None                       # 13:00 window closed at 15:00
    assert env.event_at(env.sched(1), "13:00", date(2026, 10, 6)) is not None


def test_update_validation(env: Env):
    with pytest.raises(ValidationError):
        env.scheduler.update_schedule(env.sched(0), colour="red")
    with pytest.raises(ValidationError):
        env.scheduler.update_schedule(env.sched(0), active="no")
    with pytest.raises(ValidationError):
        env.scheduler.update_schedule(env.sched(0), frequency="WEEKLY", days_of_week=[])
    with pytest.raises(NotFoundError):
        env.scheduler.update_schedule(31337, time_of_day="08:00")
    unchanged = env.scheduler.update_schedule(env.sched(0), time_of_day="08:00")
    assert unchanged["time_of_day"] == "08:00" and env.dose_0800().status == DUE
    env.catalog.archive(env.med(1))
    with pytest.raises(ValidationError):
        env.scheduler.update_schedule(env.sched(1), active=True)   # medication archived


def test_occurrence_for_respects_days(env: Env):
    sc = env.schedule(env.sched(0))
    sc.frequency, sc.days_of_week = "WEEKLY", "TUE"
    assert occurrence_for(sc, date(2026, 10, 5), env.clock) is None
    assert occurrence_for(sc, date(2026, 10, 6), env.clock) == _utc(2026, 10, 6, 15, 0)
