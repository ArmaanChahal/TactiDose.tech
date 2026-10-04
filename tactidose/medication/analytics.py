"""Local adherence analytics (``GET /api/analytics/summary``), no Snowflake needed.

Computed from the operational database only, so it works offline and on SQLite as well
as MySQL/TiDB: rows are loaded with plain SELECTs and aggregated in Python (volumes are
tiny: a few doses per day).

Window: the last ``days`` *local* calendar days including today, i.e. events whose
``scheduled_at`` falls in ``[local midnight of (today - days + 1), local midnight of
tomorrow)`` in the device time zone (DST-aware via :class:`~tactidose.core.clock.Clock`).
Only events of this device (``settings.device_id``) are counted.

Population: events in the window with ``scheduled_at <= now``, plus later events in the
window that something already happened to (status DISPENSING, DISPENSED, TAKEN, MISSED
or HARDWARE_ERROR, e.g. a dose taken a few minutes before its scheduled time). Future
events that are still SCHEDULED/DUE/CANCELLED are not counted yet.

Definitions (``totals``):

* ``scheduled``            = population excluding CANCELLED
* ``taken``                = TAKEN (user confirmed)
* ``accessed_unconfirmed`` = DISPENSED (compartment opened, no confirmation)
* ``missed``               = MISSED
* ``cancelled``            = CANCELLED (skipped by a caregiver / schedule removed)
* ``hardware_errors``      = HARDWARE_ERROR
* ``pending``              = SCHEDULED, DUE or DISPENSING
* ``adherence_rate``       = taken / (taken + accessed_unconfirmed + missed + hardware_errors),
  ``None`` when that denominator is 0 (the same definition per day in ``by_day.rate``)
* ``avg_confirm_delay_minutes`` = mean of ``confirmed_taken_at - scheduled_at`` over TAKEN
  events (negative = confirmed before the scheduled time), ``None`` if there are none
* ``by_day``         = one entry per local date in the window (oldest first, zero-filled)
* ``by_time_window`` = morning / afternoon / evening / night (``db.outbox.time_window`` of
  the local scheduled hour), zero-filled; ``miss_rate`` = missed / scheduled or ``None``
* ``device_errors``  = ``analytics_outbox`` rows of kind ``device_event`` for this device whose
  ``occurred_at`` is in the window, counted per ``code`` (falling back to ``event_type``),
  most frequent first. The outbox is pruned of *sent* rows older than 7 days when Snowflake
  sync runs, so longer windows may undercount device errors in that setup.

Database errors propagate to the caller (the API turns them into an error response): an
analytics view must never show made-up zeros when the database cannot be read.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select

from tactidose.config import Settings
from tactidose.core.clock import Clock
from tactidose.db.models import AnalyticsOutbox, DoseEvent, DoseStatus
from tactidose.db.outbox import KIND_DEVICE_EVENT, time_window
from tactidose.db.session import Database

log = logging.getLogger(__name__)

__all__ = ["local_summary", "TIME_WINDOWS", "MAX_DAYS"]

TIME_WINDOWS: tuple[str, ...] = ("morning", "afternoon", "evening", "night")
MAX_DAYS = 366
#: Safety bound on how many device-event outbox rows are scanned (newest first).
DEVICE_EVENT_SCAN_LIMIT = 10000

_S = DoseStatus
_ACTED = frozenset({_S.DISPENSING.value, _S.DISPENSED.value, _S.TAKEN.value, _S.MISSED.value,
                    _S.HARDWARE_ERROR.value})
_PENDING = frozenset({_S.SCHEDULED.value, _S.DUE.value, _S.DISPENSING.value})
_RESOLVED = frozenset({_S.TAKEN.value, _S.DISPENSED.value, _S.MISSED.value, _S.HARDWARE_ERROR.value})


@dataclass
class _Tally:
    scheduled: int = 0
    taken: int = 0
    accessed_unconfirmed: int = 0
    missed: int = 0
    cancelled: int = 0
    hardware_errors: int = 0
    pending: int = 0
    confirm_delays: list[float] = field(default_factory=list)

    def add(self, status: str, confirm_delay_min: float | None) -> None:
        if status == _S.CANCELLED.value:
            self.cancelled += 1
            return
        self.scheduled += 1
        if status == _S.TAKEN.value:
            self.taken += 1
            if confirm_delay_min is not None:
                self.confirm_delays.append(confirm_delay_min)
        elif status == _S.DISPENSED.value:
            self.accessed_unconfirmed += 1
        elif status == _S.MISSED.value:
            self.missed += 1
        elif status == _S.HARDWARE_ERROR.value:
            self.hardware_errors += 1
        elif status in _PENDING:
            self.pending += 1

    @property
    def resolved(self) -> int:
        return self.taken + self.accessed_unconfirmed + self.missed + self.hardware_errors

    @property
    def adherence_rate(self) -> float | None:
        return _ratio(self.taken, self.resolved)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _local_midnight_utc(clock: Clock, day: date) -> datetime:
    return clock.localize(datetime.combine(day, time.min)).astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def local_summary(db: Database, clock: Clock, settings: Settings, days: int = 7) -> dict[str, Any]:
    """Adherence summary for the last ``days`` local days (see module docstring).

    Shape (docs/API.md)::

        {window_days, totals: {scheduled, taken, accessed_unconfirmed, missed, cancelled,
         hardware_errors, pending}, adherence_rate, avg_confirm_delay_minutes,
         by_day: [{date, scheduled, taken, missed, rate}],
         by_time_window: [{time_window, scheduled, missed, miss_rate}],
         device_errors: [{code, count}], source: "local",
         start_date, end_date, now_local}

    ``days`` is clamped to ``1..366``.
    """
    days = max(1, min(int(days), MAX_DAYS))
    now = clock.now()
    today = clock.to_local(now).date()
    start_date = today - timedelta(days=days - 1)
    start_utc = _local_midnight_utc(clock, start_date)
    end_utc = _local_midnight_utc(clock, today + timedelta(days=1))
    device_id = settings.device_id

    with db.session() as s:
        events = s.execute(
            select(DoseEvent.scheduled_at, DoseEvent.status, DoseEvent.confirmed_taken_at)
            .where(
                DoseEvent.device_id == device_id,
                DoseEvent.scheduled_at >= start_utc,
                DoseEvent.scheduled_at < end_utc,
            )
        ).all()
        device_rows = s.execute(
            select(AnalyticsOutbox.payload, AnalyticsOutbox.created_at)
            .where(AnalyticsOutbox.kind == KIND_DEVICE_EVENT)
            .order_by(AnalyticsOutbox.outbox_id.desc())
            .limit(DEVICE_EVENT_SCAN_LIMIT)
        ).all()

    totals = _Tally()
    per_day: dict[date, _Tally] = {start_date + timedelta(days=i): _Tally() for i in range(days)}
    per_window: dict[str, _Tally] = {w: _Tally() for w in TIME_WINDOWS}
    for scheduled_at, status, confirmed_at in events:
        if scheduled_at > now and status not in _ACTED:
            continue
        delay = None
        if status == _S.TAKEN.value and confirmed_at is not None:
            delay = (confirmed_at - scheduled_at).total_seconds() / 60.0
        local = clock.to_local(scheduled_at)
        totals.add(status, delay)
        day_tally = per_day.get(local.date())
        if day_tally is not None:
            day_tally.add(status, delay)
        per_window.setdefault(time_window(local.hour), _Tally()).add(status, delay)

    error_counts: Counter[str] = Counter()
    for payload, created_at in device_rows:
        if not isinstance(payload, dict):
            continue
        if payload.get("device_id") not in (None, device_id):
            continue
        at = _parse_ts(payload.get("occurred_at")) or created_at
        if at is None or not (start_utc <= at < end_utc):
            continue
        code = payload.get("code") or payload.get("event_type") or "UNKNOWN"
        error_counts[str(code)] += 1

    delays = totals.confirm_delays
    return {
        "window_days": days,
        "start_date": start_date.isoformat(),
        "end_date": today.isoformat(),
        "now_local": clock.to_local(now).isoformat(),
        "totals": {
            "scheduled": totals.scheduled,
            "taken": totals.taken,
            "accessed_unconfirmed": totals.accessed_unconfirmed,
            "missed": totals.missed,
            "cancelled": totals.cancelled,
            "hardware_errors": totals.hardware_errors,
            "pending": totals.pending,
        },
        "adherence_rate": totals.adherence_rate,
        "avg_confirm_delay_minutes": round(sum(delays) / len(delays), 1) if delays else None,
        "by_day": [
            {"date": d.isoformat(), "scheduled": t.scheduled, "taken": t.taken,
             "missed": t.missed, "rate": t.adherence_rate}
            for d, t in sorted(per_day.items())
        ],
        "by_time_window": [
            {"time_window": w, "scheduled": t.scheduled, "missed": t.missed,
             "miss_rate": _ratio(t.missed, t.scheduled)}
            for w, t in per_window.items()
        ],
        "device_errors": [
            {"code": code, "count": count}
            for code, count in sorted(error_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "source": "local",
    }
