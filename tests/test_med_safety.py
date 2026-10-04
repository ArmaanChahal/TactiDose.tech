"""safety.py v2 helpers: dose window, open statuses, drop time, display slot, DoseInfo, the
"most recent dropped dose" lookup used to confirm a dose as taken."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from tactidose.db.models import DoseEvent, PillDrop, Schedule
from tactidose.medication import safety
from tactidose.medication.compartments import assigned_slots
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    DISPENSING,
    DUE,
    HARDWARE_ERROR,
    MISSED,
    SCHEDULED,
    TAKEN,
    Env,
    env_template_unticked,
    env_unticked,
)


@pytest.fixture
def m(env_unticked: Env) -> Env:
    """Seeded (med 0/1/2 in containers 1/2/3 at 08:00/13:00/20:00) but no events: tests add them."""
    return env_unticked


def add(e: Env, schedule_id: int, hhmm: str, status: str = DUE, day: date | None = None, **kw: Any) -> int:
    with e.db.session() as s:
        sc = s.get(Schedule, schedule_id)
        ev = DoseEvent(schedule_id=schedule_id, medication_id=sc.medication_id, user_id=e.patient,
                       device_id=e.settings.device_id, scheduled_at=e.at(hhmm, day), status=status, **kw)
        s.add(ev)
        s.flush()
        return ev.event_id


def test_window_helpers(m: Env):
    at = m.at("08:00")
    start, end = safety.dispense_window(at, m.settings)
    assert (start, end) == (at - timedelta(minutes=30), at + timedelta(minutes=120))
    assert safety.in_window(at, start, m.settings) and safety.in_window(at, end, m.settings)
    assert not safety.in_window(at, start - timedelta(seconds=1), m.settings)
    assert not safety.in_window(at, end + timedelta(seconds=1), m.settings)
    eid = add(m, m.sched(0), "08:00")
    with m.db.session() as s:
        assert safety.dispense_window(s.get(DoseEvent, eid), m.settings) == (start, end)


@pytest.mark.parametrize("status,needs_review,expected", [
    (SCHEDULED, False, True), (DUE, False, True), (HARDWARE_ERROR, False, True),
    (HARDWARE_ERROR, True, False), (DUE, True, False),
    (DISPENSING, False, False), (DISPENSED, False, False), (TAKEN, False, False),
    (MISSED, False, False), (CANCELLED, False, False),
])
def test_is_open(status, needs_review, expected):
    assert safety.is_open(DoseEvent(status=status, needs_review=needs_review)) is expected


def test_drop_time_prefers_completion():
    now = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    done = PillDrop(requested_at=now, completed_at=now + timedelta(seconds=4))
    in_flight = PillDrop(requested_at=now, completed_at=None)
    assert safety.drop_time(done) == now + timedelta(seconds=4) and safety.drop_time(in_flight) == now


def test_display_slot_and_dose_info(m: Env):
    due = add(m, m.sched(0), "08:00", DUE, slot_number=2)
    done = add(m, m.sched(2), "20:00", DISPENSED, day=date(2026, 10, 4), slot_number=1,
               dispensed_at=m.clock.now() - timedelta(hours=2))
    err = add(m, m.sched(1), "13:00", HARDWARE_ERROR, day=date(2026, 10, 4), slot_number=2, needs_review=True)
    with m.db.session() as s:
        slots = assigned_slots(s, m.settings)
        evs = {e.event_id: e for e in s.scalars(select(DoseEvent)).all()}
        assert safety.display_slot(evs[due], slots) == 0           # open dose: where it would come from now
        assert safety.display_slot(evs[done], slots) == 1          # dropped: where it actually came from
        assert safety.display_slot(evs[err], slots) == 2           # review: the container that was used
        info = safety.to_dose_info(evs[due], m.clock)
        override = safety.to_dose_info(evs[due], m.clock, slot=None)
    assert info.slot == 2 and override.slot is None
    d = info.to_dict()
    assert d["label"] == f"dose_{due}" and d["scheduled_local"] == "2026-10-05T08:00:00-07:00"
    assert d["medication_name"] == "Vitamin C (demo candy)" and d["instructions"] == "Take one piece."
    assert d["compartment"] == "compartment 3" and d["compartment_number"] == 3


def test_display_slot_falls_back_when_not_assigned(m: Env):
    m.compartments.assign(0, None, patient_id=m.patient)
    eid = add(m, m.sched(0), "08:00", DUE, slot_number=1)
    with m.db.session() as s:
        ev = s.get(DoseEvent, eid)
        assert safety.display_slot(ev, assigned_slots(s, m.settings)) == 1
        ev.slot_number = None
        assert safety.display_slot(ev, assigned_slots(s, m.settings)) is None


def test_find_confirmable_window_recency_and_filters(m: Env):
    now = m.clock.now()
    old = add(m, m.sched(2), "20:00", DISPENSED, day=date(2026, 10, 4), dispensed_at=now - timedelta(minutes=181))
    with m.db.session() as s:
        assert safety.find_confirmable(s, now, m.settings) is None
    m.set_event(old, dispensed_at=now - timedelta(minutes=180))
    a = add(m, m.sched(0), "08:00", DISPENSED, dispensed_at=now - timedelta(minutes=5))
    with m.db.session() as s:
        assert safety.find_confirmable(s, now, m.settings).event_id == a
        assert safety.find_confirmable(s, now, m.settings, medication_id=m.med(2)).event_id == old
        assert safety.find_confirmable(s, now, m.settings, patient_id=m.patient).event_id == a
        assert safety.find_confirmable(s, now, m.settings, patient_id=m.family) is None
