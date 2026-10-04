"""Scheduler: schedule math (DST, WEEKLY, horizon, no backfill), DUE/MISSED, CRUD, reconcile."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from tactidose.core.bus import Topic
from tactidose.core.clock import Clock
from tactidose.db.models import DoseEvent, Medication, Schedule
from tactidose.medication.errors import NotFoundError, ValidationError
from tactidose.medication.scheduler import (
    Scheduler,
    occurrence_for,
    parse_days,
    parse_frequency,
    parse_time_of_day,
)
from tests.fakes import FakeHardware
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    DUE,
    HARDWARE_ERROR,
    KIND_ADHERENCE,
    MISSED,
    SCHEDULED,
    TAKEN,
    Med,
    build,
    flaky,
    med,
    med_template,
)

UTC = timezone.utc


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _add_med(m: Med, name: str = "Demo token", slot: int | None = 0) -> int:
    mid = m.catalog.create({"name": name, "strength": "1 token"}, confirmed=True, confirmed_by="test")["medication_id"]
    if slot is not None:
        m.compartments.assign(slot, mid)
    return mid


def _times(m: Med, schedule_id: int) -> list[datetime]:
    with m.db.session() as s:
        return list(s.scalars(select(DoseEvent.scheduled_at).where(DoseEvent.schedule_id == schedule_id)
                              .order_by(DoseEvent.scheduled_at)).all())


def _env(settings, db, bus, tz: str, local: datetime, **overrides) -> Med:
    clock = Clock(tz, frozen_at=local)
    return build(settings, clock, db, bus, FakeHardware(), seed=False, timezone=tz, **overrides)


# --------------------------------------------------------------------------- materialize


def test_materialize_dates_horizon_and_initial_transitions(med: Med):
    # Mon 07:55, horizon 36h -> local dates Sun..Wed considered, events up to Tue 19:55.
    statuses = {(e.schedule_id, med.clock.to_local(e.scheduled_at).strftime("%a %H:%M")): e.status
                for e in med.events()}
    assert statuses == {
        (med.sched_0800, "Sun 08:00"): MISSED, (med.sched_0800, "Mon 08:00"): DUE,
        (med.sched_0800, "Tue 08:00"): SCHEDULED,
        (med.sched_2000, "Sun 20:00"): MISSED, (med.sched_2000, "Mon 20:00"): SCHEDULED,
        (med.sched_1300, "Sun 13:00"): MISSED, (med.sched_1300, "Mon 13:00"): SCHEDULED,
        (med.sched_1300, "Tue 13:00"): SCHEDULED,
    }
    assert all(e.device_id == med.settings.device_id and e.user_id == med.ids["user_id"] for e in med.events())


def test_materialize_is_idempotent(med: Med):
    before = [(e.event_id, e.scheduled_at) for e in med.events()]
    assert med.scheduler.materialize() == 0
    assert med.tick() == 0
    assert [(e.event_id, e.scheduled_at) for e in med.events()] == before


def test_horizon_rolls_forward_with_time(med: Med):
    tue_2000 = med.at("20:00", date(2026, 10, 6))
    assert tue_2000 not in _times(med, med.sched_2000)       # 07:55 + 36h = Tue 19:55
    med.travel("08:05")
    med.tick()
    assert tue_2000 in _times(med, med.sched_2000)


def test_longer_horizon_materializes_more_days(settings, clock, db, bus, fake_hw):
    m = build(settings, clock, db, bus, fake_hw, schedule_horizon_hours=72)
    days = {m.clock.to_local(t).date() for t in _times(m, m.sched_0800)}
    assert days == {date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)}


def test_new_schedule_never_backfills_closed_windows(med: Med):
    # 07:55: an 06:00 dose window (05:30-08:00) is still open -> created and DUE now.
    open_sched = med.scheduler.create_schedule(med.med2, "06:00")
    assert [med.clock.to_local(t).strftime("%d %H:%M") for t in _times(med, open_sched["schedule_id"])] == \
        ["05 06:00", "06 06:00"]
    assert med.event_at(open_sched["schedule_id"], "06:00").status == DUE
    # 05:00 window closed at 07:00 -> today's occurrence is never fabricated (no MISSED).
    closed = med.scheduler.create_schedule(med.med2, "05:00")
    times = [med.clock.to_local(t).date() for t in _times(med, closed["schedule_id"])]
    assert times == [date(2026, 10, 6)]
    assert all(e.status != MISSED for e in med.events() if e.schedule_id == closed["schedule_id"])


def test_weekly_schedule_only_listed_days(settings, clock, db, bus, fake_hw):
    m = build(settings, clock, db, bus, fake_hw, schedule_horizon_hours=72)
    sc = m.scheduler.create_schedule(m.med2, "09:00", "WEEKLY", ["wed", "MON"])
    assert sc["frequency"] == "WEEKLY" and sc["days_of_week"] == ["MON", "WED"]
    assert m.schedule(sc["schedule_id"]).days_of_week == "MON,WED"
    days = [m.clock.to_local(t).strftime("%a %d") for t in _times(m, sc["schedule_id"])]
    assert days == ["Mon 05", "Wed 07"]


def test_daily_stores_all_days_even_if_days_given(med: Med):
    sc = med.scheduler.create_schedule(med.med2, "09:00", "daily", ["MON"])
    assert sc["frequency"] == "DAILY" and sc["days_of_week"] == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def test_only_active_confirmed_medications_of_this_user_are_materialized(med: Med):
    with med.db.session() as s:
        unconfirmed = Medication(user_id=med.ids["user_id"], name="Unconfirmed", confirmed_by_user=False)
        archived = Medication(user_id=med.ids["user_id"], name="Archived", confirmed_by_user=True, active=False)
        s.add_all([unconfirmed, archived])
        s.flush()
        created = med.clock.now() - timedelta(days=3)
        s.add_all([Schedule(medication_id=unconfirmed.medication_id, time_of_day="09:00", created_at=created),
                   Schedule(medication_id=archived.medication_id, time_of_day="09:00", created_at=created)])
        ids = {unconfirmed.medication_id, archived.medication_id}
    assert med.tick() == 0
    assert not [e for e in med.events() if e.medication_id in ids]


def test_materialize_without_device_is_a_noop(settings, clock, db):
    assert Scheduler(db, clock, settings).materialize() == 0


def test_concurrent_ticks_never_duplicate(settings, clock, db, bus, fake_hw):
    m = build(settings, clock, db, bus, fake_hw, tick=False)
    schedulers = [Scheduler(db, clock, m.settings, bus=bus) for _ in range(4)]
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
        t.join(timeout=20)
    assert not errors
    keys = [(e.schedule_id, e.scheduled_at) for e in m.events()]
    assert len(keys) == len(set(keys)) == 8


# --------------------------------------------------------------------------- DST


def test_spring_forward_vancouver(settings, db, bus):
    # 2026-03-08 02:00 PST -> 03:00 PDT in America/Vancouver.
    m = _env(settings, db, bus, "America/Vancouver", datetime(2026, 3, 6, 12, 0), schedule_horizon_hours=96)
    mid = _add_med(m)
    gap = m.scheduler.create_schedule(mid, "02:30")["schedule_id"]
    morning = m.scheduler.create_schedule(mid, "08:00")["schedule_id"]
    assert _times(m, gap) == [_utc(2026, 3, 7, 10, 30), _utc(2026, 3, 8, 10, 30),   # 02:30 PST; skipped hour -> 03:30 PDT
                              _utc(2026, 3, 9, 9, 30), _utc(2026, 3, 10, 9, 30)]
    assert _times(m, morning) == [_utc(2026, 3, 7, 16, 0), _utc(2026, 3, 8, 15, 0),
                                  _utc(2026, 3, 9, 15, 0), _utc(2026, 3, 10, 15, 0)]
    local = [m.clock.to_local(t) for t in _times(m, gap)]
    assert [t.date() for t in local] == [date(2026, 3, d) for d in (7, 8, 9, 10)]   # one dose per local day
    assert local[1].strftime("%H:%M %z") == "03:30 -0700"
    # Windows are computed on the real instant: at 03:20 PDT the 03:30 dose is due.
    m.clock.freeze(datetime(2026, 3, 8, 3, 20))
    m.tick()
    assert m.event_at(gap, "02:30", date(2026, 3, 8)).status == DUE


def test_fall_back_los_angeles(settings, db, bus):
    # US rules (Vancouver followed these until B.C. adopted permanent daylight time):
    # 2026-11-01 02:00 PDT -> 01:00 PST; 01:30 happens twice.
    m = _env(settings, db, bus, "America/Los_Angeles", datetime(2026, 10, 30, 12, 0), schedule_horizon_hours=96)
    mid = _add_med(m)
    ambiguous = m.scheduler.create_schedule(mid, "01:30")["schedule_id"]
    morning = m.scheduler.create_schedule(mid, "08:00")["schedule_id"]
    assert _times(m, ambiguous) == [_utc(2026, 10, 31, 8, 30), _utc(2026, 11, 1, 8, 30),   # first 01:30 (PDT) only
                                    _utc(2026, 11, 2, 9, 30), _utc(2026, 11, 3, 9, 30)]
    assert _times(m, morning) == [_utc(2026, 10, 31, 15, 0), _utc(2026, 11, 1, 16, 0),
                                  _utc(2026, 11, 2, 16, 0), _utc(2026, 11, 3, 16, 0)]
    assert m.tick() == 0   # re-materializing across the repeated hour creates nothing new


def test_vancouver_november_follows_tz_database(settings, db, bus):
    """tzdata 2026 encodes B.C.'s permanent daylight time (no 2026-11-01 fall-back); older
    tzdata has the fall-back. Either way: one event per local day at the local wall time."""
    tz = "America/Vancouver"
    m = _env(settings, db, bus, tz, datetime(2026, 10, 30, 12, 0), schedule_horizon_hours=96)
    mid = _add_med(m)
    for hhmm in ("01:30", "08:00"):
        sid = m.scheduler.create_schedule(mid, hhmm)["schedule_id"]
        times = _times(m, sid)
        h, mi = (int(x) for x in hhmm.split(":"))
        expected = [datetime(2026, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)
                    for mo, d in ((10, 31), (11, 1), (11, 2), (11, 3))]
        assert times == expected
        assert [m.clock.to_local(t).strftime("%H:%M") for t in times] == [hhmm] * 4


# --------------------------------------------------------------------------- transitions


def test_due_starts_exactly_at_window_open(settings, clock, db, bus, fake_hw):
    clock.freeze(datetime(2026, 10, 5, 7, 29, 59))
    m = build(settings, clock, db, bus, fake_hw)
    assert m.due_0800().status == SCHEDULED
    m.travel("07:30")
    assert m.tick() == 1
    assert m.due_0800().status == DUE


def test_missed_only_after_window_end(med: Med):
    med.travel("10:00")                       # end is inclusive
    med.tick()
    assert med.due_0800().status == DUE
    med.travel("10:00", seconds=1)
    med.tick()
    ev = med.due_0800()
    assert ev.status == MISSED and ev.missed_at == med.clock.now()


def test_hardware_error_misses_unless_under_review(med: Med):
    a = med.due_0800()
    b = med.event_at(med.sched_1300, "13:00")
    med.set_event(a.event_id, status=HARDWARE_ERROR, needs_review=False, attempts=1)
    med.set_event(b.event_id, status=HARDWARE_ERROR, needs_review=True, attempts=1)
    med.travel("23:59")
    med.tick()
    assert med.event(a.event_id).status == MISSED
    assert med.event(b.event_id).status == HARDWARE_ERROR    # caregiver must review uncertain doses


def test_transitions_write_outbox_log_and_publish(med: Med):
    sub = med.subscribe(Topic.DOSE_UPDATED)
    ev = med.due_0800()
    assert med.adherence_statuses(ev.event_id) == [DUE]
    assert [r.event_id for r in med.devlog("DOSE_DUE")] == [ev.event_id]
    med.travel("10:01")
    med.tick()
    assert med.adherence_statuses(ev.event_id) == [DUE, MISSED]
    assert ev.event_id in [r.event_id for r in med.devlog("DOSE_MISSED")]
    changes = [(e.data["event_id"], e.data["change"]) for e in sub.drain()]
    assert (ev.event_id, "missed") in changes
    assert any(change == "created" for _, change in changes)          # Tue 20:00 materialized
    payload = next(e for e in med.outbox(KIND_ADHERENCE) if e.payload["final_status"] == MISSED)
    assert "name" not in str(payload.payload) and payload.payload["missed"] is True


def test_creation_alone_writes_no_outbox_row(med: Med):
    ev = med.event_at(med.sched_1300, "13:00")
    assert ev.status == SCHEDULED
    assert med.adherence_statuses(ev.event_id) == []
    assert ev.event_id in [r.event_id for r in med.devlog("DOSE_MATERIALIZED")]


def test_tick_never_raises_on_db_error(med: Med, flaky):
    flaky.fail = True
    assert med.tick() == 0


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("bad", ["24:00", "12:60", "8", "ab:cd", "08:00:00", "", " : ", 800, None])
def test_create_schedule_rejects_bad_times(med: Med, bad):
    with pytest.raises(ValidationError):
        med.scheduler.create_schedule(med.med1, bad)


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


def test_create_schedule_requires_existing_confirmed_active_medication(med: Med):
    with pytest.raises(NotFoundError):
        med.scheduler.create_schedule(9999, "08:00")
    with pytest.raises(NotFoundError):
        med.scheduler.create_schedule(True, "08:00")
    with med.db.session() as s:
        raw = Medication(user_id=med.ids["user_id"], name="Typed but not confirmed", confirmed_by_user=False)
        s.add(raw)
        s.flush()
        raw_id = raw.medication_id
    with pytest.raises(ValidationError):
        med.scheduler.create_schedule(raw_id, "08:00")
    med.catalog.archive(med.med2)
    with pytest.raises(ValidationError):
        med.scheduler.create_schedule(med.med2, "08:00")
    with pytest.raises(ValidationError):
        med.scheduler.create_schedule(med.med1, "09:00", "WEEKLY", [])


def test_list_schedules_shape_and_filter(med: Med):
    rows = med.scheduler.list_schedules()
    assert [r["time_of_day"] for r in rows] == ["08:00", "13:00", "20:00"]
    assert set(rows[0]) == {"schedule_id", "medication_id", "medication_name", "time_of_day", "frequency",
                            "days_of_week", "active", "created_at"}
    assert rows[0]["medication_name"] == "Vitamin C (demo candy)" and rows[0]["active"] is True
    med.scheduler.deactivate_schedule(med.sched_2000)
    assert [r["schedule_id"] for r in med.scheduler.list_schedules()] == [med.sched_0800, med.sched_1300]
    assert len(med.scheduler.list_schedules(include_inactive=True)) == 3
    assert med.scheduler.get_schedule(med.sched_2000)["active"] is False
    with pytest.raises(NotFoundError):
        med.scheduler.get_schedule(424242)


# --------------------------------------------------------------------------- update / deactivate


def test_update_time_cancels_stale_in_window_and_deletes_future(med: Med):
    today = med.due_0800()
    tomorrow = med.event_at(med.sched_0800, "08:00", date(2026, 10, 6))
    sub = med.subscribe(Topic.DOSE_UPDATED)
    out = med.scheduler.update_schedule(med.sched_0800, time_of_day="8:30")
    assert out["time_of_day"] == "08:30"
    assert med.event(today.event_id).status == CANCELLED                 # in window -> cancelled
    with med.db.session() as s:
        assert s.get(DoseEvent, tomorrow.event_id) is None               # never opened -> deleted
    assert med.adherence_statuses(today.event_id) == [DUE, CANCELLED]
    new_today = med.event_at(med.sched_0800, "08:30")
    assert new_today is not None and new_today.status == SCHEDULED       # window opens 08:00
    assert med.event_at(med.sched_0800, "08:30", date(2026, 10, 6)) is not None
    changes = {(e.data["event_id"], e.data["change"]) for e in sub.drain()}
    assert {(today.event_id, "cancelled"), (tomorrow.event_id, "deleted")} <= changes


def test_update_days_keeps_events_that_still_match(med: Med):
    today = med.due_0800()                                # Monday
    tomorrow = med.event_at(med.sched_0800, "08:00", date(2026, 10, 6))
    med.scheduler.update_schedule(med.sched_0800, frequency="WEEKLY", days_of_week=["TUE"])
    assert med.event(today.event_id).status == CANCELLED
    assert med.event(tomorrow.event_id).status == SCHEDULED   # same row kept (same id)


def test_deactivate_cancels_in_window_and_deletes_future_but_keeps_accessed(med: Med):
    accessed = med.due_0800()
    med.set_event(accessed.event_id, status=DISPENSED, dispensed_at=med.clock.now())
    tomorrow = med.event_at(med.sched_0800, "08:00", date(2026, 10, 6))
    evening = med.event_at(med.sched_2000, "20:00")
    med.scheduler.deactivate_schedule(med.sched_0800)
    assert med.event(accessed.event_id).status == DISPENSED
    with med.db.session() as s:
        assert s.get(DoseEvent, tomorrow.event_id) is None
    assert med.event(evening.event_id).status == SCHEDULED     # other schedule untouched
    med.tick()
    assert med.event_at(med.sched_0800, "08:00", date(2026, 10, 6)) is None


def test_redefining_schedule_does_not_fabricate_missed_dose(med: Med):
    med.travel("09:00")
    med.tick()
    old = med.due_0800()
    assert old.status == DUE
    med.scheduler.update_schedule(med.sched_0800, time_of_day="06:00")   # 06:00 window closed at 08:00
    assert med.event(old.event_id).status == CANCELLED
    assert med.event_at(med.sched_0800, "06:00") is None
    assert med.event_at(med.sched_0800, "06:00", date(2026, 10, 6)) is not None
    assert med.schedule(med.sched_0800).created_at == med.clock.now()


def test_edited_time_never_gives_second_dose_same_day(med: Med):
    today = med.due_0800()
    med.set_event(today.event_id, status=TAKEN, dispensed_at=med.clock.now(), confirmed_taken_at=med.clock.now())
    med.scheduler.update_schedule(med.sched_0800, time_of_day="09:00")
    assert med.event(today.event_id).status == TAKEN
    assert med.event_at(med.sched_0800, "09:00") is None                       # already accessed today
    assert med.event_at(med.sched_0800, "09:00", date(2026, 10, 6)) is not None


def test_reactivation_regenerates_without_backfill(med: Med):
    med.scheduler.deactivate_schedule(med.sched_1300)
    assert med.event_at(med.sched_1300, "13:00") is None
    med.travel("16:00")
    out = med.scheduler.update_schedule(med.sched_1300, active=True)
    assert out["active"] is True
    assert med.event_at(med.sched_1300, "13:00") is None                       # 13:00 window closed at 15:00
    assert med.event_at(med.sched_1300, "13:00", date(2026, 10, 6)) is not None


def test_update_validation(med: Med):
    with pytest.raises(ValidationError):
        med.scheduler.update_schedule(med.sched_0800, colour="red")
    with pytest.raises(ValidationError):
        med.scheduler.update_schedule(med.sched_0800, active="no")
    with pytest.raises(ValidationError):
        med.scheduler.update_schedule(med.sched_0800, frequency="WEEKLY", days_of_week=[])
    with pytest.raises(NotFoundError):
        med.scheduler.update_schedule(31337, time_of_day="08:00")
    unchanged = med.scheduler.update_schedule(med.sched_0800, time_of_day="08:00")
    assert unchanged["time_of_day"] == "08:00" and med.due_0800().status == DUE
    med.catalog.archive(med.med2)
    with pytest.raises(ValidationError):
        med.scheduler.update_schedule(med.sched_1300, active=True)   # medication archived


def test_occurrence_for_respects_days(med: Med):
    sc = med.schedule(med.sched_0800)
    sc.frequency, sc.days_of_week = "WEEKLY", "TUE"
    assert occurrence_for(sc, date(2026, 10, 5), med.clock) is None
    assert occurrence_for(sc, date(2026, 10, 6), med.clock) == _utc(2026, 10, 6, 15, 0)
