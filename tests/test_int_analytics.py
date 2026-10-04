"""Tests for tactidose.medication.analytics.local_summary (seeded events, frozen clock).

The frozen test clock reads Monday 2026-10-05 07:55 America/Vancouver (PDT, UTC-7).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tactidose.core.clock import Clock
from tactidose.db.models import AnalyticsOutbox, Device, DoseEvent, DoseStatus
from tactidose.db.outbox import KIND_DEVICE_EVENT, enqueue_device_event
from tactidose.medication.analytics import MAX_DAYS, TIME_WINDOWS, local_summary
from tests.conftest import TEST_TZ
from tests.fakes import seed_minimal

S = DoseStatus
L = datetime   # naive local wall-clock times in TEST_TZ

API_KEYS = {"window_days", "totals", "adherence_rate", "avg_confirm_delay_minutes", "by_day",
            "by_time_window", "device_errors", "source"}
TOTAL_KEYS = {"scheduled", "taken", "accessed_unconfirmed", "missed", "cancelled",
              "hardware_errors", "pending"}

#: (schedule index, local scheduled time, status, local confirmation time, device or None)
EVENTS = [
    (0, L(2026, 10, 5, 6, 0), S.TAKEN, L(2026, 10, 5, 6, 10), None),          # a  morning, +10
    (1, L(2026, 10, 5, 7, 50), S.DUE, None, None),                             # b  pending
    (0, L(2026, 10, 5, 8, 0), S.DUE, None, None),                              # c  future: not counted
    (2, L(2026, 10, 5, 8, 0), S.TAKEN, L(2026, 10, 5, 7, 50), None),           # d  future but taken, -10
    (1, L(2026, 10, 5, 20, 0), S.CANCELLED, None, None),                       # e  future cancel: not counted
    (0, L(2026, 10, 4, 8, 0), S.MISSED, None, None),                           # f  morning missed
    (2, L(2026, 10, 4, 13, 0), S.DISPENSED, None, None),                       # g  afternoon accessed
    (1, L(2026, 10, 4, 20, 0), S.TAKEN, L(2026, 10, 4, 20, 30), None),         # h  evening, +30
    (0, L(2026, 10, 3, 8, 0), S.HARDWARE_ERROR, None, None),                   # i  morning hw error
    (2, L(2026, 10, 3, 13, 0), S.CANCELLED, None, None),                       # j  cancelled (past)
    (1, L(2026, 10, 3, 23, 0), S.MISSED, None, None),                          # k  night missed
    (0, L(2026, 9, 28, 8, 0), S.TAKEN, L(2026, 9, 28, 8, 5), None),            # l  8 days ago, +5
    (1, L(2026, 9, 29, 0, 30), S.TAKEN, L(2026, 9, 29, 0, 40), None),          # m  first day, night, +10
    (0, L(2026, 10, 4, 9, 0), S.TAKEN, L(2026, 10, 4, 9, 1), "other-device"),  # n  other device
]

#: (local time, code, event_type, device or None)
DEVICE_EVENTS = [
    (L(2026, 10, 4, 10, 0), "MOTOR_FAULT", "FAULT", None),
    (L(2026, 10, 4, 10, 5), "MOTOR_FAULT", "FAULT", None),
    (L(2026, 10, 3, 12, 0), None, "DEVICE_RESET", None),
    (L(2026, 10, 5, 7, 0), "HOME_TIMEOUT", "FAULT", None),
    (L(2026, 9, 20, 9, 0), "MOTOR_FAULT", "FAULT", None),              # outside the window
    (L(2026, 10, 4, 11, 0), "HOME_TIMEOUT", "FAULT", "other-device"),  # other device
]


def add_events(db, ids, clock: Clock, events) -> None:
    with db.session() as s:
        for sched, local, status, confirmed, device in events:
            s.add(DoseEvent(
                schedule_id=ids["schedule_ids"][sched],
                medication_id=ids["med_ids"][0 if sched in (0, 1) else 1],
                user_id=ids["user_id"],
                device_id=device or ids["device_id"],
                scheduled_at=clock.local_to_utc(local),
                status=status.value,
                confirmed_taken_at=clock.local_to_utc(confirmed) if confirmed else None,
            ))


@pytest.fixture
def scenario(db, settings, clock):
    ids = seed_minimal(db, settings)
    with db.session() as s:
        s.add(Device(device_id="other-device", user_id=ids["user_id"], name="Other", num_slots=6))
    add_events(db, ids, clock, EVENTS)
    with db.session() as s:
        for local, code, event_type, device in DEVICE_EVENTS:
            enqueue_device_event(s, device_id=device or ids["device_id"], event_type=event_type,
                                 code=code, at=clock.local_to_utc(local))
    return ids


def test_seven_day_summary(db, settings, clock, scenario):
    out = local_summary(db, clock, settings)
    assert out["window_days"] == 7 and out["source"] == "local"
    assert out["start_date"] == "2026-09-29" and out["end_date"] == "2026-10-05"
    assert out["totals"] == {"scheduled": 9, "taken": 4, "accessed_unconfirmed": 1, "missed": 2,
                             "cancelled": 1, "hardware_errors": 1, "pending": 1}
    assert out["adherence_rate"] == 0.5                       # 4 / (4 + 1 + 2 + 1)
    assert out["avg_confirm_delay_minutes"] == 10.0           # (10 - 10 + 30 + 10) / 4
    assert out["by_day"] == [
        {"date": "2026-09-29", "scheduled": 1, "taken": 1, "missed": 0, "rate": 1.0},
        {"date": "2026-09-30", "scheduled": 0, "taken": 0, "missed": 0, "rate": None},
        {"date": "2026-10-01", "scheduled": 0, "taken": 0, "missed": 0, "rate": None},
        {"date": "2026-10-02", "scheduled": 0, "taken": 0, "missed": 0, "rate": None},
        {"date": "2026-10-03", "scheduled": 2, "taken": 0, "missed": 1, "rate": 0.0},
        {"date": "2026-10-04", "scheduled": 3, "taken": 1, "missed": 1, "rate": 0.3333},
        {"date": "2026-10-05", "scheduled": 3, "taken": 2, "missed": 0, "rate": 1.0},
    ]
    assert out["by_time_window"] == [
        {"time_window": "morning", "scheduled": 5, "missed": 1, "miss_rate": 0.2},
        {"time_window": "afternoon", "scheduled": 1, "missed": 0, "miss_rate": 0.0},
        {"time_window": "evening", "scheduled": 1, "missed": 0, "miss_rate": 0.0},
        {"time_window": "night", "scheduled": 2, "missed": 1, "miss_rate": 0.5},
    ]
    assert out["device_errors"] == [
        {"code": "MOTOR_FAULT", "count": 2},
        {"code": "DEVICE_RESET", "count": 1},     # no code -> event type
        {"code": "HOME_TIMEOUT", "count": 1},
    ]


def test_shape_matches_api_contract(db, settings, clock, scenario):
    out = local_summary(db, clock, settings, days=7)
    assert API_KEYS <= set(out)
    assert set(out["totals"]) == TOTAL_KEYS
    assert all(set(d) == {"date", "scheduled", "taken", "missed", "rate"} for d in out["by_day"])
    assert [w["time_window"] for w in out["by_time_window"]] == list(TIME_WINDOWS)
    assert all(set(w) == {"time_window", "scheduled", "missed", "miss_rate"} for w in out["by_time_window"])
    assert all(set(e) == {"code", "count"} for e in out["device_errors"])
    json.dumps(out)
    assert out["now_local"].startswith("2026-10-05T07:55:00")


def test_other_window_lengths(db, settings, clock, scenario):
    today = local_summary(db, clock, settings, days=1)
    assert today["start_date"] == today["end_date"] == "2026-10-05"
    assert today["totals"] == {"scheduled": 3, "taken": 2, "accessed_unconfirmed": 0, "missed": 0,
                               "cancelled": 0, "hardware_errors": 0, "pending": 1}
    assert today["adherence_rate"] == 1.0 and len(today["by_day"]) == 1
    assert today["device_errors"] == [{"code": "HOME_TIMEOUT", "count": 1}]

    eight = local_summary(db, clock, settings, days=8)
    assert eight["start_date"] == "2026-09-28" and len(eight["by_day"]) == 8
    assert eight["totals"]["taken"] == 5 and eight["totals"]["scheduled"] == 10
    assert eight["adherence_rate"] == round(5 / 9, 4)
    assert eight["avg_confirm_delay_minutes"] == 9.0          # (10 - 10 + 30 + 5 + 10) / 5


def test_empty_database(db, settings, clock):
    out = local_summary(db, clock, settings)
    assert out["totals"] == dict.fromkeys(TOTAL_KEYS, 0)
    assert out["adherence_rate"] is None and out["avg_confirm_delay_minutes"] is None
    assert len(out["by_day"]) == 7 and all(d["rate"] is None and d["scheduled"] == 0 for d in out["by_day"])
    assert [w["miss_rate"] for w in out["by_time_window"]] == [None] * 4
    assert out["device_errors"] == []


@pytest.mark.parametrize("days,expected", [(0, 1), (-5, 1), (1, 1), (30, 30), (10_000, MAX_DAYS)])
def test_days_are_clamped(db, settings, clock, days, expected):
    out = local_summary(db, clock, settings, days=days)
    assert out["window_days"] == expected and len(out["by_day"]) == expected


def test_local_day_boundaries_across_dst(db, settings):
    clock = Clock(TEST_TZ, frozen_at=L(2026, 11, 2, 12, 0))     # DST ended Sun 2026-11-01 02:00
    ids = seed_minimal(db, settings)
    add_events(db, ids, clock, [
        (0, L(2026, 10, 31, 23, 30), S.TAKEN, L(2026, 10, 31, 23, 35), None),   # day before the window
        (1, L(2026, 11, 1, 0, 30), S.TAKEN, L(2026, 11, 1, 0, 35), None),       # PDT
        (2, L(2026, 11, 1, 23, 30), S.TAKEN, L(2026, 11, 1, 23, 35), None),     # PST (UTC: Nov 2)
        (0, L(2026, 11, 2, 11, 0), S.MISSED, None, None),
    ])
    out = local_summary(db, clock, settings, days=2)
    assert out["by_day"] == [
        {"date": "2026-11-01", "scheduled": 2, "taken": 2, "missed": 0, "rate": 1.0},
        {"date": "2026-11-02", "scheduled": 1, "taken": 0, "missed": 1, "rate": 0.0},
    ]
    assert out["avg_confirm_delay_minutes"] == 5.0
    night = next(w for w in out["by_time_window"] if w["time_window"] == "night")
    assert night["scheduled"] == 2                              # 00:30 and 23:30 local


def test_device_event_edge_cases(db, settings, clock):
    now = clock.now()
    with db.session() as s:
        s.add_all([
            AnalyticsOutbox(kind=KIND_DEVICE_EVENT, dedupe_key="device_event:a",
                            payload={"device_id": settings.device_id, "event_type": "DISCONNECTED",
                                     "code": None, "occurred_at": "garbage"},
                            created_at=now - timedelta(hours=1)),                 # falls back to created_at
            AnalyticsOutbox(kind=KIND_DEVICE_EVENT, dedupe_key="device_event:b",
                            payload={"device_id": settings.device_id, "code": "OLD"},
                            created_at=now - timedelta(days=30)),
            AnalyticsOutbox(kind=KIND_DEVICE_EVENT, dedupe_key="device_event:c",
                            payload=["not", "a", "dict"], created_at=now),
            AnalyticsOutbox(kind=KIND_DEVICE_EVENT, dedupe_key="device_event:d",
                            payload={"occurred_at": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")},
                            created_at=now),                                      # no device id/code
            AnalyticsOutbox(kind="adherence", dedupe_key="adherence:x",
                            payload={"code": "NOT_A_DEVICE_EVENT"}, created_at=now),
        ])
    errors = local_summary(db, clock, settings)["device_errors"]
    assert errors == [{"code": "DISCONNECTED", "count": 1}, {"code": "UNKNOWN", "count": 1}]


def test_read_only_and_repeatable(db, settings, clock, scenario):
    with db.session() as s:
        before = s.query(AnalyticsOutbox).count(), s.query(DoseEvent).count()
    assert local_summary(db, clock, settings) == local_summary(db, clock, settings)
    with db.session() as s:
        assert (s.query(AnalyticsOutbox).count(), s.query(DoseEvent).count()) == before


def test_database_errors_propagate(settings, clock):
    class BrokenDb:
        def session(self):
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        local_summary(BrokenDb(), clock, settings)   # type: ignore[arg-type]
