"""safety.py: every eligibility rule in isolation, selection order, refusal priority, helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from tactidose.core.interfaces import BlockReason
from tactidose.db.models import (
    AnalyticsOutbox,
    Compartment,
    DeviceLog,
    DoseEvent,
    Medication,
    Schedule,
)
from tactidose.medication import safety
from tactidose.medication.compartments import assigned_slots
from tactidose.medication.safety import Verdict
from tests.test_med_support import (
    CANCELLED,
    DISPENSED,
    DISPENSING,
    DUE,
    HARDWARE_ERROR,
    MISSED,
    SCHEDULED,
    TAKEN,
    Med,
    build,
)

R = BlockReason


@pytest.fixture
def m(settings, clock, db, bus, fake_hw) -> Med:
    """Seeded (med1 slot 2: 08:00/20:00, med2 slot 4: 13:00) but no events: tests add them."""
    return build(settings, clock, db, bus, fake_hw, tick=False)


def add(m: Med, schedule_id: int, hhmm: str, status: str = DUE, day: date | None = None, **kw: Any) -> int:
    with m.db.session() as s:
        sc = s.get(Schedule, schedule_id)
        ev = DoseEvent(schedule_id=schedule_id, medication_id=sc.medication_id, user_id=m.ids["user_id"],
                       device_id=m.settings.device_id, scheduled_at=m.at(hhmm, day), status=status, **kw)
        s.add(ev)
        s.flush()
        return ev.event_id


def extra_schedule(m: Med, medication_id: int, hhmm: str) -> int:
    with m.db.session() as s:
        sc = Schedule(medication_id=medication_id, time_of_day=hhmm, created_at=m.clock.now() - timedelta(days=5))
        s.add(sc)
        s.flush()
        return sc.schedule_id


@dataclass
class D:
    verdict: Verdict
    reason: BlockReason | None
    event_id: int | None
    slot: int | None
    due: list[int] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)
    accessed: list[int] = field(default_factory=list)
    awaiting: list[int] = field(default_factory=list)
    blocked: list[tuple[int, BlockReason]] = field(default_factory=list)
    next_upcoming: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def decide(m: Med, now: datetime | None = None, **overrides: Any) -> D:
    st = m.settings.model_copy(update=overrides) if overrides else m.settings
    with m.db.session() as s:
        d = safety.evaluate(s, now or m.clock.now(), st)
        return D(
            d.verdict, d.reason, d.event.event_id if d.event is not None else None, d.slot,
            [a.event.event_id for a in d.due], [a.event.event_id for a in d.queued],
            [e.event_id for e in d.accessed], [e.event_id for e in d.awaiting],
            [(a.event.event_id, a.reason) for a in d.blocked],
            d.next_upcoming.event_id if d.next_upcoming is not None else None,
            d.due_summary(m.clock).to_dict(),
        )


def set_med(m: Med, medication_id: int, **values: Any) -> None:
    with m.db.session() as s:
        s.execute(update(Medication).where(Medication.medication_id == medication_id).values(**values))


def set_schedule(m: Med, schedule_id: int, **values: Any) -> None:
    with m.db.session() as s:
        s.execute(update(Schedule).where(Schedule.schedule_id == schedule_id).values(**values))


def set_slot(m: Med, slot: int, **values: Any) -> None:
    with m.db.session() as s:
        s.execute(update(Compartment).where(Compartment.slot_number == slot).values(**values))


# --------------------------------------------------------------------------- rule 1: window


def test_window_helpers(m: Med):
    at = m.at("08:00")
    start, end = safety.dispense_window(at, m.settings)
    assert (start, end) == (at - timedelta(minutes=30), at + timedelta(minutes=120))
    assert safety.in_window(at, start, m.settings) and safety.in_window(at, end, m.settings)
    assert not safety.in_window(at, start - timedelta(seconds=1), m.settings)
    assert not safety.in_window(at, end + timedelta(seconds=1), m.settings)


@pytest.mark.parametrize("now,expected", [
    ("07:29:59", Verdict.NOTHING_DUE), ("07:30:00", Verdict.ALLOW),
    ("10:00:00", Verdict.ALLOW), ("10:00:01", Verdict.NOTHING_DUE),
])
def test_rule1_window_bounds_inclusive(m: Med, now: str, expected: Verdict):
    eid = add(m, m.sched_0800, "08:00", DUE)
    h, mi, se = (int(x) for x in now.split(":"))
    d = decide(m, m.at(f"{h:02d}:{mi:02d}") + timedelta(seconds=se))
    assert d.verdict is expected
    if expected is Verdict.ALLOW:
        assert d.event_id == eid and d.slot == 2 and d.due == [eid]


def test_nothing_due_reports_next_upcoming(m: Med):
    add(m, m.sched_0800, "08:00", MISSED, day=date(2026, 10, 4))
    nxt = add(m, m.sched_1300, "13:00", SCHEDULED)
    add(m, m.sched_2000, "20:00", SCHEDULED)
    d = decide(m, m.at("10:30"))
    assert d.verdict is Verdict.NOTHING_DUE and d.event_id is None and d.next_upcoming == nxt
    assert d.summary["next_upcoming"]["event_id"] == nxt and d.summary["next_upcoming"]["slot"] == 4


# --------------------------------------------------------------------------- rule 2: status


@pytest.mark.parametrize("status,kw,verdict,reason", [
    (SCHEDULED, {}, Verdict.ALLOW, None),
    (DUE, {}, Verdict.ALLOW, None),
    (HARDWARE_ERROR, {"attempts": 2}, Verdict.ALLOW, None),
    (HARDWARE_ERROR, {"attempts": 3}, Verdict.BLOCKED, R.NEEDS_REVIEW),            # attempts >= max
    (HARDWARE_ERROR, {"attempts": 1, "needs_review": True}, Verdict.BLOCKED, R.NEEDS_REVIEW),
    (DUE, {"needs_review": True}, Verdict.BLOCKED, R.NEEDS_REVIEW),                 # defensive
    (MISSED, {}, Verdict.NOTHING_DUE, None),
    (CANCELLED, {}, Verdict.NOTHING_DUE, None),
    (DISPENSED, {"dispensed_at_offset": -5}, Verdict.DUPLICATE, None),
    (TAKEN, {"dispensed_at_offset": -5}, Verdict.DUPLICATE, None),
    (DISPENSING, {}, Verdict.IN_PROGRESS, R.IN_PROGRESS),
])
def test_rule2_status(m: Med, status, kw, verdict, reason):
    kw = dict(kw)
    if "dispensed_at_offset" in kw:
        kw["dispensed_at"] = m.clock.now() + timedelta(minutes=kw.pop("dispensed_at_offset"))
    eid = add(m, m.sched_0800, "08:00", status, **kw)
    d = decide(m)
    assert d.verdict is verdict and d.reason is reason
    if verdict is not Verdict.NOTHING_DUE:
        assert d.event_id == eid


def test_is_dispensable(m: Med):
    def ev(**kw: Any) -> DoseEvent:
        return DoseEvent(**{"status": DUE, "needs_review": False, "attempts": 0, **kw})

    assert safety.is_dispensable(ev(), m.settings)
    assert safety.is_dispensable(ev(status=SCHEDULED), m.settings)
    assert safety.is_dispensable(ev(status=HARDWARE_ERROR, attempts=2), m.settings)
    assert not safety.is_dispensable(ev(status=HARDWARE_ERROR, attempts=3), m.settings)
    assert not safety.is_dispensable(ev(status=HARDWARE_ERROR, needs_review=True), m.settings)
    for status in (DISPENSING, DISPENSED, TAKEN, MISSED, CANCELLED):
        assert not safety.is_dispensable(ev(status=status), m.settings)


# --------------------------------------------------------------------------- rule 3: active + confirmed


def test_rule3_medication_inactive(m: Med):
    add(m, m.sched_0800, "08:00")
    set_med(m, m.med1, active=False)
    d = decide(m)
    assert (d.verdict, d.reason) == (Verdict.BLOCKED, R.INACTIVE)


def test_rule3_schedule_inactive(m: Med):
    add(m, m.sched_0800, "08:00")
    set_schedule(m, m.sched_0800, active=False)
    assert (decide(m).verdict, decide(m).reason) == (Verdict.BLOCKED, R.INACTIVE)


def test_rule3_medication_unconfirmed(m: Med):
    add(m, m.sched_0800, "08:00")
    set_med(m, m.med1, confirmed_by_user=False)
    d = decide(m)
    assert (d.verdict, d.reason) == (Verdict.BLOCKED, R.UNCONFIRMED_MEDICATION)
    assert d.summary["blocked"][0]["reason"] == "UNCONFIRMED_MEDICATION"


# --------------------------------------------------------------------------- rule 4: compartment


def test_rule4_no_compartment(m: Med):
    add(m, m.sched_0800, "08:00")
    m.compartments.assign(2, None)
    d = decide(m)
    assert (d.verdict, d.reason, d.slot) == (Verdict.BLOCKED, R.NO_COMPARTMENT, None)


def test_rule4_inactive_compartment(m: Med):
    add(m, m.sched_0800, "08:00")
    set_slot(m, 2, active=False)
    assert decide(m).reason is R.NO_COMPARTMENT


def test_rule4_slot_out_of_configured_range(m: Med):
    add(m, m.sched_1300, "13:00")
    d = decide(m, m.at("13:00"), num_slots=4)          # med2 sits in slot 4
    assert (d.verdict, d.reason) == (Verdict.BLOCKED, R.NO_COMPARTMENT)
    assert decide(m, m.at("13:00")).verdict is Verdict.ALLOW


def test_rule4_slot_resolved_at_evaluation_time(m: Med):
    add(m, m.sched_0800, "08:00")
    assert decide(m).slot == 2
    m.compartments.assign(5, m.med1)                    # moved after the event was generated
    d = decide(m)
    assert d.verdict is Verdict.ALLOW and d.slot == 5
    assert d.summary["due"][0]["compartment_number"] == 6


# --------------------------------------------------------------------------- rule 5: one at a time


def test_rule5_any_dispensing_blocks_everything(m: Med):
    eligible = add(m, m.sched_0800, "08:00", DUE)
    busy = add(m, m.sched_1300, "13:00", DISPENSING, slot_number=4)      # outside the window, still blocks
    d = decide(m)
    assert d.verdict is Verdict.IN_PROGRESS and d.reason is R.IN_PROGRESS and d.event_id == busy
    assert d.due == [] and d.queued == [eligible]
    assert (busy, R.IN_PROGRESS) in d.blocked
    assert d.summary["due"] == [] and d.summary["blocked"][0]["reason"] == "IN_PROGRESS"


# --------------------------------------------------------------------------- rule 6: min interval


def test_rule6_too_soon_after_access_of_same_medication(m: Med):
    add(m, m.sched_2000, "20:00", TAKEN, day=date(2026, 10, 4),
        dispensed_at=m.clock.now() - timedelta(minutes=30), confirmed_taken_at=m.clock.now() - timedelta(minutes=29))
    eid = add(m, m.sched_0800, "08:00")
    d = decide(m)
    assert (d.verdict, d.reason, d.event_id) == (Verdict.DUPLICATE, R.TOO_SOON, eid)
    assert decide(m, min_dose_interval_minutes=0).verdict is Verdict.ALLOW       # 0 disables the rule
    # The latest access stamp (confirmation, 29 min ago) counts; the cutoff is inclusive.
    assert decide(m, min_dose_interval_minutes=29).reason is R.TOO_SOON
    assert decide(m, min_dose_interval_minutes=28).verdict is Verdict.ALLOW


def test_rule6_counts_confirmation_time_and_ignores_other_medications(m: Med):
    add(m, m.sched_2000, "20:00", TAKEN, day=date(2026, 10, 4),
        confirmed_taken_at=m.clock.now() - timedelta(minutes=10))
    assert decide(m, m.at("13:00")).verdict is Verdict.NOTHING_DUE
    add(m, m.sched_1300, "13:00")                        # med2: a recent med1 access does not block it
    assert decide(m, m.at("13:00")).verdict is Verdict.ALLOW
    eid = add(m, m.sched_0800, "08:00")
    assert decide(m).event_id == eid and decide(m).reason is R.TOO_SOON


def test_recent_accesses_map(m: Med):
    now = m.clock.now()
    add(m, m.sched_2000, "20:00", DISPENSED, day=date(2026, 10, 4), dispensed_at=now - timedelta(minutes=50))
    add(m, m.sched_1300, "13:00", TAKEN, day=date(2026, 10, 4), dispensed_at=now - timedelta(minutes=90))
    with m.db.session() as s:
        assert safety.recent_accesses(s, now, m.settings) == {m.med1: now - timedelta(minutes=50)}


# --------------------------------------------------------------------------- selection


def test_selection_earliest_scheduled_first(m: Med):
    early = add(m, extra_schedule(m, m.med2, "07:45"), "07:45")
    add(m, m.sched_0800, "08:00")
    d = decide(m)
    assert d.event_id == early and d.slot == 4 and d.due[0] == early and len(d.due) == 2


def test_selection_lowest_slot_breaks_ties(m: Med):
    a = add(m, m.sched_0800, "08:00")                              # med1, slot 2
    b = add(m, extra_schedule(m, m.med2, "08:00"), "08:00")         # med2, slot 4
    assert decide(m).due == [a, b]
    m.compartments.assign(0, m.med2)
    assert decide(m).due == [b, a]


# --------------------------------------------------------------------------- refusal priority


def _config_blocked(m: Med) -> int:
    sid = extra_schedule(m, m.med2, "08:10")
    eid = add(m, sid, "08:10")
    m.compartments.assign(4, None)                                   # med2 -> NO_COMPARTMENT
    return eid


def test_priority_in_progress_first(m: Med):
    _config_blocked(m)
    add(m, m.sched_0800, "08:00", HARDWARE_ERROR, needs_review=True, attempts=1)
    busy = add(m, m.sched_1300, "13:00", DISPENSING)
    assert (decide(m).verdict, decide(m).event_id) == (Verdict.IN_PROGRESS, busy)


def test_priority_needs_review_before_duplicate(m: Med):
    _config_blocked(m)
    review = add(m, m.sched_0800, "08:00", HARDWARE_ERROR, needs_review=True, attempts=1)
    add(m, extra_schedule(m, m.med1, "08:20"), "08:20", DISPENSED, dispensed_at=m.clock.now())
    d = decide(m)
    assert (d.verdict, d.reason, d.event_id) == (Verdict.BLOCKED, R.NEEDS_REVIEW, review)


def test_priority_duplicate_before_configuration_blocks(m: Med):
    _config_blocked(m)
    older = add(m, m.sched_0800, "08:00", TAKEN, dispensed_at=m.clock.now() - timedelta(minutes=20))
    newer = add(m, extra_schedule(m, m.med1, "08:20"), "08:20", DISPENSED,
                dispensed_at=m.clock.now() - timedelta(minutes=2))
    d = decide(m)
    assert (d.verdict, d.reason) == (Verdict.DUPLICATE, None)
    assert d.event_id == newer and set(d.accessed) == {older, newer}          # most recently accessed


def test_priority_too_soon_is_duplicate_tier(m: Med):
    _config_blocked(m)
    add(m, m.sched_2000, "20:00", TAKEN, day=date(2026, 10, 4), dispensed_at=m.clock.now() - timedelta(minutes=5))
    eid = add(m, m.sched_0800, "08:00")
    d = decide(m)
    assert (d.verdict, d.reason, d.event_id) == (Verdict.DUPLICATE, R.TOO_SOON, eid)


def test_priority_configuration_block_before_nothing_due(m: Med):
    eid = _config_blocked(m)
    d = decide(m)
    assert (d.verdict, d.reason, d.event_id) == (Verdict.BLOCKED, R.NO_COMPARTMENT, eid)


def test_eligible_dose_wins_over_blocked_ones(m: Med):
    _config_blocked(m)
    add(m, extra_schedule(m, m.med1, "07:40"), "07:40", HARDWARE_ERROR, needs_review=True, attempts=1)
    ok = add(m, extra_schedule(m, m.med2, "08:05"), "08:05")
    m.compartments.assign(1, m.med2)
    d = decide(m)
    assert d.verdict is Verdict.ALLOW and d.event_id == ok and d.slot == 1


# --------------------------------------------------------------------------- confirmation lookups


def test_find_confirmable_window_and_recency(m: Med):
    now = m.clock.now()
    old = add(m, m.sched_2000, "20:00", DISPENSED, day=date(2026, 10, 4), dispensed_at=now - timedelta(minutes=181))
    with m.db.session() as s:
        assert safety.find_confirmable(s, now, m.settings) is None
    m.set_event(old, dispensed_at=now - timedelta(minutes=180))
    a = add(m, m.sched_0800, "08:00", DISPENSED, dispensed_at=now - timedelta(minutes=5))
    with m.db.session() as s:
        assert safety.find_confirmable(s, now, m.settings).event_id == a
    assert decide(m).awaiting == [a, old]


def test_latest_accessed_uses_latest_stamp(m: Med):
    now = m.clock.now()
    with m.db.session() as s:
        assert safety.latest_accessed(s, now, m.settings) is None
    taken = add(m, m.sched_2000, "20:00", TAKEN, day=date(2026, 10, 4),
                dispensed_at=now - timedelta(minutes=170), confirmed_taken_at=now - timedelta(minutes=1))
    add(m, m.sched_0800, "08:00", DISPENSED, dispensed_at=now - timedelta(minutes=10))
    with m.db.session() as s:
        assert safety.latest_accessed(s, now, m.settings).event_id == taken


# --------------------------------------------------------------------------- DoseInfo / display / purity


def test_display_slot_and_dose_info(m: Med):
    due = add(m, m.sched_0800, "08:00", DUE, slot_number=0)
    done = add(m, m.sched_2000, "20:00", DISPENSED, day=date(2026, 10, 4), slot_number=1,
               dispensed_at=m.clock.now() - timedelta(hours=2))
    err = add(m, m.sched_1300, "13:00", HARDWARE_ERROR, day=date(2026, 10, 4), slot_number=3, needs_review=True)
    with m.db.session() as s:
        slots = assigned_slots(s, m.settings)
        evs = {e.event_id: e for e in s.scalars(select(DoseEvent)).all()}
        assert safety.display_slot(evs[due], slots) == 2           # open dose: where it would come from now
        assert safety.display_slot(evs[done], slots) == 1          # accessed: where it actually came from
        assert safety.display_slot(evs[err], slots) == 3           # review: the compartment that may be open
        info = safety.to_dose_info(evs[due], m.clock)
        override = safety.to_dose_info(evs[due], m.clock, slot=None)
    assert info.slot == 0 and override.slot is None
    d = info.to_dict()
    assert d["label"] == f"dose_{due}" and d["scheduled_local"] == "2026-10-05T08:00:00-07:00"
    assert d["medication_name"] == "Vitamin C (demo candy)" and d["instructions"] == "Take one piece."
    assert d["compartment"] == "compartment 1" and d["compartment_number"] == 1


def test_due_summary_shape(m: Med):
    add(m, m.sched_0800, "08:00")
    add(m, m.sched_1300, "13:00", SCHEDULED)
    summary = decide(m).summary
    assert set(summary) == {"now_local", "due", "awaiting_confirmation", "accessed", "blocked", "next_upcoming"}
    assert summary["now_local"] == "2026-10-05T07:55:00-07:00"
    assert summary["due"][0]["slot"] == 2 and summary["next_upcoming"]["status"] == SCHEDULED


def test_evaluate_is_read_only(m: Med):
    add(m, m.sched_0800, "08:00")
    add(m, m.sched_1300, "13:00", DISPENSING)

    def counts() -> tuple[int, int, list[str]]:
        with m.db.session() as s:
            return (s.scalar(select(func.count()).select_from(AnalyticsOutbox)),
                    s.scalar(select(func.count()).select_from(DeviceLog)),
                    list(s.scalars(select(DoseEvent.status).order_by(DoseEvent.event_id))))

    before = counts()
    with m.db.session() as s:
        safety.evaluate(s, m.clock.now(), m.settings)
        assert not s.new and not s.dirty and not s.deleted
    assert counts() == before
