"""Report statistics (stored in ``reports.stats`` as JSON).

Pure function of :class:`~tactidose.reports.data.ReportData`; deterministic; no I/O.

Definitions (scheduled doses = dose events in the period, CANCELLED excluded):

* ``dispensed`` — DISPENSED or TAKEN (the pill dropped: an automatic drop, or an earlier
  manual/agent drop that satisfied the dose). Split into ``on_time`` (dropped no later than
  ``on_time_minutes`` = 15 after ``scheduled_at``; early drops count as on time), ``late``
  and ``timing_unknown`` (no ``dispensed_at`` recorded).
* ``missed`` — MISSED. ``pending`` — SCHEDULED / DUE / DISPENSING (window still open).
  ``hardware_errors`` — HARDWARE_ERROR (last attempt failed; retrying or awaiting review).
* ``adherence_rate`` = dispensed / (dispensed + missed), a fraction 0..1, ``None`` when nothing
  is decided yet (same definition per day and per medication).
* Drops: every ``pill_drops`` row in the period by status and source; ``manual_drops`` /
  ``agent_drops`` / ``button_drops`` / ``demo_drops`` / ``scheduled_drops`` count DROPPED rows;
  ``on_request_drops`` = DROPPED rows that were not automatic; ``refused_requests`` = DENIED
  rows from the patient-initiated sources (manual, agent, button); ``denied_by_reason`` covers
  every DENIED row (scheduled ALREADY_SATISFIED skips included); ``uncertain`` = UNCERTAIN rows
  (``needs_review`` = still unresolved).
* Inventory (current pill counts): ``doses_per_day`` from the medication's active schedules
  (DAILY = 1, WEEKLY = listed days / 7); ``days_of_supply`` = pill_count / doses_per_day
  (``None`` without an active schedule).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from tactidose.db.models import (
    COOLDOWN_SOURCES,
    DoseStatus,
    DropSource,
    DropStatus,
    Frequency,
)
from tactidose.reports.data import (
    ALERT_KINDS,
    DoseRow,
    DropRow,
    ReportData,
    ScheduleRow,
    fmt_time,
)

log = logging.getLogger(__name__)

__all__ = ["MAX_ISSUES", "ON_TIME_MINUTES", "STATS_VERSION", "compute_stats", "schedule_label"]

STATS_VERSION = 1
ON_TIME_MINUTES = 15
#: The issues list (missed / failed / uncertain) keeps the newest entries.
MAX_ISSUES = 100

_S = DoseStatus
_DISPENSED = frozenset({_S.DISPENSED.value, _S.TAKEN.value})
_PENDING = frozenset({_S.SCHEDULED.value, _S.DUE.value, _S.DISPENSING.value})
_SOURCES = tuple(s.value for s in DropSource)
_PATIENT_SOURCES = frozenset(s.value for s in COOLDOWN_SOURCES)
_DROP_STATUSES = tuple(s.value for s in DropStatus)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")   # locale-independent


@dataclass
class _DoseTally:
    scheduled: int = 0
    dispensed: int = 0
    on_time: int = 0
    late: int = 0
    timing_unknown: int = 0
    missed: int = 0
    pending: int = 0
    hardware_errors: int = 0
    cancelled: int = 0
    taken_confirmed: int = 0
    needs_review: int = 0

    def add(self, dose: DoseRow, on_time_limit: timedelta) -> None:
        status = dose.status
        if status == _S.CANCELLED.value:
            self.cancelled += 1
            return
        self.scheduled += 1
        if dose.needs_review:
            self.needs_review += 1
        if status in _DISPENSED:
            self.dispensed += 1
            if status == _S.TAKEN.value:
                self.taken_confirmed += 1
            if dose.dispensed_at is None:
                self.timing_unknown += 1
            elif dose.dispensed_at - dose.scheduled_at <= on_time_limit:
                self.on_time += 1
            else:
                self.late += 1
        elif status == _S.MISSED.value:
            self.missed += 1
        elif status == _S.HARDWARE_ERROR.value:
            self.hardware_errors += 1
        elif status in _PENDING:
            self.pending += 1
        else:  # unknown future status: count it as still open rather than guess
            self.pending += 1

    @property
    def adherence_rate(self) -> float | None:
        return _ratio(self.dispensed, self.dispensed + self.missed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduled": self.scheduled,
            "dispensed": self.dispensed,
            "on_time": self.on_time,
            "late": self.late,
            "timing_unknown": self.timing_unknown,
            "missed": self.missed,
            "pending": self.pending,
            "hardware_errors": self.hardware_errors,
            "cancelled": self.cancelled,
            "taken_confirmed": self.taken_confirmed,
            "needs_review": self.needs_review,
            "adherence_rate": self.adherence_rate,
            "on_time_rate": _ratio(self.on_time, self.dispensed),
        }


@dataclass
class _DropTally:
    by_status: Counter[str] = field(default_factory=Counter)
    by_source: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    denied_by_reason: Counter[str] = field(default_factory=Counter)
    refused_by_reason: Counter[str] = field(default_factory=Counter)
    failed_by_reason: Counter[str] = field(default_factory=Counter)
    needs_review: int = 0

    def add(self, drop: DropRow) -> None:
        self.by_status[drop.status] += 1
        self.by_source[drop.source][drop.status] += 1
        if drop.status == DropStatus.DENIED.value:
            reason = drop.reason or "UNKNOWN"
            self.denied_by_reason[reason] += 1
            if drop.source in _PATIENT_SOURCES:
                self.refused_by_reason[reason] += 1
        elif drop.status == DropStatus.FAILED.value:
            self.failed_by_reason[drop.reason or drop.hardware_result or "UNKNOWN"] += 1
        elif drop.status == DropStatus.UNCERTAIN.value and drop.needs_review:
            self.needs_review += 1

    def dropped(self, source: str | None = None) -> int:
        if source is None:
            return self.by_status[DropStatus.DROPPED.value]
        return self.by_source.get(source, Counter())[DropStatus.DROPPED.value]

    def to_dict(self) -> dict[str, Any]:
        sources = list(_SOURCES) + sorted(set(self.by_source) - set(_SOURCES))
        by_source = {}
        for src in sources:
            c = self.by_source.get(src, Counter())
            by_source[src] = {
                "requests": sum(c.values()),
                "dropped": c[DropStatus.DROPPED.value],
                "denied": c[DropStatus.DENIED.value],
                "failed": c[DropStatus.FAILED.value],
                "uncertain": c[DropStatus.UNCERTAIN.value],
            }
        on_request = sum(self.dropped(s) for s in sources if s != DropSource.SCHEDULE.value)
        return {
            "requests": sum(self.by_status.values()),
            "dropped": self.dropped(),
            "denied": self.by_status[DropStatus.DENIED.value],
            "failed": self.by_status[DropStatus.FAILED.value],
            "uncertain": self.by_status[DropStatus.UNCERTAIN.value],
            "needs_review": self.needs_review,
            "by_status": {s: self.by_status[s] for s in _DROP_STATUSES},
            "by_source": by_source,
            "scheduled_drops": self.dropped(DropSource.SCHEDULE.value),
            "manual_drops": self.dropped(DropSource.MANUAL.value),
            "agent_drops": self.dropped(DropSource.AGENT.value),
            "button_drops": self.dropped(DropSource.BUTTON.value),
            "demo_drops": self.dropped(DropSource.DEMO.value),
            "on_request_drops": on_request,
            "refused_requests": sum(self.refused_by_reason.values()),
            "refused_by_reason": _sorted_counts(self.refused_by_reason),
            "denied_by_reason": _sorted_counts(self.denied_by_reason),
            "failed_by_reason": _sorted_counts(self.failed_by_reason),
        }


def compute_stats(data: ReportData, *, on_time_minutes: int = ON_TIME_MINUTES) -> dict[str, Any]:
    """JSON-serialisable statistics for ``data`` (definitions in the module docstring).

    Top-level keys: version, period, generated_at, on_time_minutes, adherence_rate, doses, drops,
    conversations, alerts, per_day, per_medication, inventory, issues, issues_total, device, truncated.
    """
    limit = timedelta(minutes=on_time_minutes)
    local_dates = data.local_dates()

    doses = _DoseTally()
    drops = _DropTally()
    day_doses: dict[date, _DoseTally] = {d: _DoseTally() for d in local_dates}
    day_drops: dict[date, _DropTally] = {d: _DropTally() for d in local_dates}
    day_msgs: Counter[date] = Counter()
    med_doses: dict[int, _DoseTally] = defaultdict(_DoseTally)
    med_drops: dict[int, _DropTally] = defaultdict(_DropTally)

    for dose in data.doses:
        doses.add(dose, limit)
        day = data.local(dose.scheduled_at).date()
        day_doses.setdefault(day, _DoseTally()).add(dose, limit)
        med_doses[dose.medication_id].add(dose, limit)
    for drop in data.drops:
        drops.add(drop)
        day_drops.setdefault(data.local(drop.requested_at).date(), _DropTally()).add(drop)
        if drop.medication_id is not None:
            med_drops[drop.medication_id].add(drop)
    for msg in data.messages:
        if msg.role == "user":
            day_msgs[data.local(msg.created_at).date()] += 1

    dpd = _doses_per_day(data)
    dose_summary = doses.to_dict()
    device = data.device
    return {
        "version": STATS_VERSION,
        "period": {
            "days": data.days,
            "start": data.period_start.isoformat(),
            "end": data.period_end.isoformat(),
            "start_local": data.local(data.period_start).isoformat(),
            "end_local": data.local(data.period_end).isoformat(),
            "start_date": local_dates[0].isoformat() if local_dates else None,
            "end_date": local_dates[-1].isoformat() if local_dates else None,
            "timezone": data.timezone,
        },
        "generated_at": data.generated_at.isoformat(),
        "on_time_minutes": on_time_minutes,
        "adherence_rate": dose_summary["adherence_rate"],
        "doses": dose_summary,
        "drops": drops.to_dict(),
        "conversations": _conversation_stats(data),
        "alerts": _alert_counts(data),
        "per_day": _per_day(local_dates, day_doses, day_drops, day_msgs),
        "per_medication": _per_medication(data, med_doses, med_drops, dpd),
        "inventory": _inventory(data, dpd),
        **_issues(data),
        "device": None if device is None else {
            "device_id": device.device_id,
            "name": device.name,
            "cooldown_minutes": device.manual_cooldown_minutes,
            "auto_drop_enabled": device.auto_drop_enabled,
        },
        "truncated": list(data.truncated),
    }


def schedule_label(sched: ScheduleRow) -> str:
    """``8:00 AM daily`` / ``8:00 AM Mon, Wed`` (invalid stored times are shown as stored)."""
    try:
        hh, mm = (int(p) for p in sched.time_of_day.split(":", 1))
        when = fmt_time(time(hh, mm))
    except (ValueError, TypeError):
        when = sched.time_of_day
    if sched.frequency == Frequency.DAILY.value or len(sched.days) == 7:
        return f"{when} daily"
    days = ", ".join(d.title() for d in sched.days) or "no days"
    return f"{when} {days}"


# --------------------------------------------------------------------------- sections


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return {k: v for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])) if v}


def _doses_per_day(data: ReportData) -> dict[int, float]:
    active_meds = {m.medication_id for m in data.medications if m.active}
    out: dict[int, float] = defaultdict(float)
    for sched in data.schedules:
        if sched.medication_id in active_meds:
            out[sched.medication_id] += sched.doses_per_day
    return dict(out)


def _supply(pills: int, per_day: float) -> float | None:
    if per_day <= 0:
        return None
    return round(max(pills, 0) / per_day, 1)


def _per_day(
    local_dates: Iterable[date],
    day_doses: dict[date, _DoseTally],
    day_drops: dict[date, _DropTally],
    day_msgs: Counter[date],
) -> list[dict[str, Any]]:
    rows = []
    for d in sorted(set(local_dates) | set(day_doses) | set(day_drops)):
        t = day_doses.get(d, _DoseTally())
        dr = day_drops.get(d, _DropTally()).to_dict()
        rows.append({
            "date": d.isoformat(),
            "weekday": _WEEKDAYS[d.weekday()],
            "scheduled": t.scheduled,
            "dispensed": t.dispensed,
            "on_time": t.on_time,
            "late": t.late,
            "missed": t.missed,
            "pending": t.pending + t.hardware_errors,
            "cancelled": t.cancelled,
            "adherence_rate": t.adherence_rate,
            "drops": dr["dropped"],
            "on_request_drops": dr["on_request_drops"],
            "denied": dr["denied"],
            "refused": dr["refused_requests"],
            "failed": dr["failed"],
            "uncertain": dr["uncertain"],
            "patient_messages": day_msgs.get(d, 0),
        })
    return rows


def _per_medication(
    data: ReportData,
    med_doses: dict[int, _DoseTally],
    med_drops: dict[int, _DropTally],
    dpd: dict[int, float],
) -> list[dict[str, Any]]:
    containers: dict[int, list[int]] = defaultdict(list)
    pills: dict[int, int] = defaultdict(int)
    for c in data.containers:
        if c.medication_id is not None:
            containers[c.medication_id].append(c.container_number)
            pills[c.medication_id] += c.pill_count
    wanted = [m for m in data.medications
              if m.active or m.medication_id in med_doses or m.medication_id in med_drops]
    known = {m.medication_id for m in wanted}
    names: dict[int, str] = {}
    for dose in data.doses:
        names.setdefault(dose.medication_id, dose.medication_name)
    for drop in data.drops:
        if drop.medication_id is not None and drop.medication_name:
            names.setdefault(drop.medication_id, drop.medication_name)
    rows = []
    for med in wanted:
        rows.append(_med_row(med.medication_id, med.name, med.strength, med.active, data,
                             containers, pills, med_doses, med_drops, dpd))
    for mid in sorted((set(med_doses) | set(med_drops)) - known):   # e.g. deleted medications
        rows.append(_med_row(mid, names.get(mid) or f"Medication {mid}", None, False, data,
                             containers, pills, med_doses, med_drops, dpd))
    rows.sort(key=lambda r: (min(r["containers"]) if r["containers"] else 99, r["name"].lower(), r["medication_id"]))
    return rows


def _med_row(
    mid: int, name: str, strength: str | None, active: bool, data: ReportData,
    containers: dict[int, list[int]], pills: dict[int, int],
    med_doses: dict[int, _DoseTally], med_drops: dict[int, _DropTally], dpd: dict[int, float],
) -> dict[str, Any]:
    t = med_doses.get(mid, _DoseTally())
    dr = med_drops.get(mid, _DropTally()).to_dict()
    per_day = round(dpd.get(mid, 0.0), 3)
    has_container = mid in containers
    return {
        "medication_id": mid,
        "name": name,
        "strength": strength,
        "active": active,
        "containers": sorted(containers.get(mid, [])),
        "schedule": [schedule_label(s) for s in data.schedules if s.medication_id == mid and s.active],
        "scheduled": t.scheduled,
        "dispensed": t.dispensed,
        "on_time": t.on_time,
        "late": t.late,
        "missed": t.missed,
        "pending": t.pending + t.hardware_errors,
        "adherence_rate": t.adherence_rate,
        "drops": dr["dropped"],
        "drops_by_source": {src: v["dropped"] for src, v in dr["by_source"].items() if v["dropped"]},
        "on_request_drops": dr["on_request_drops"],
        "denied": dr["denied"],
        "failed": dr["failed"],
        "uncertain": dr["uncertain"],
        "pill_count": pills.get(mid, 0) if has_container else None,
        "doses_per_day": per_day,
        "days_of_supply": _supply(pills.get(mid, 0), per_day) if has_container else None,
    }


def _inventory(data: ReportData, dpd: dict[int, float]) -> list[dict[str, Any]]:
    rows = []
    for c in data.containers:
        per_day = round(dpd.get(c.medication_id, 0.0), 3) if c.medication_id is not None else 0.0
        rows.append({
            "device_id": c.device_id,
            "slot": c.slot,
            "container_number": c.container_number,
            "medication_id": c.medication_id,
            "medication_name": c.medication_name,
            "strength": c.strength,
            "pill_count": c.pill_count,
            "capacity": c.capacity,
            "low_stock_threshold": c.low_stock_threshold,
            "low_stock": c.low_stock,
            "empty": c.empty,
            "active": c.active,
            "doses_per_day": per_day,
            "days_of_supply": _supply(c.pill_count, per_day) if c.medication_id is not None else None,
        })
    return rows


def _conversation_stats(data: ReportData) -> dict[str, Any]:
    roles = Counter(m.role for m in data.messages)
    modes = Counter((m.input_mode or "text") for m in data.messages if m.role == "user")
    tools = Counter(m.tool_name or "unknown" for m in data.messages if m.role == "tool")
    pill_outcomes: Counter[str] = Counter()
    for m in data.messages:
        if m.role == "tool" and m.tool_name == "request_pill":
            status = (m.tool_result or {}).get("status")
            pill_outcomes[str(status) if status else "UNKNOWN"] += 1
    return {
        "conversations": len({m.conversation_id for m in data.messages}),
        "patient_messages": roles.get("user", 0),
        "agent_messages": roles.get("assistant", 0),
        "tool_messages": roles.get("tool", 0),
        "voice_messages": modes.get("voice", 0),
        "text_messages": sum(v for k, v in modes.items() if k != "voice"),
        "tool_calls": _sorted_counts(tools),
        "agent_pill_requests": sum(pill_outcomes.values()),
        "agent_pill_requests_by_status": _sorted_counts(pill_outcomes),
    }


def _alert_counts(data: ReportData) -> dict[str, int]:
    counts = Counter(a.kind for a in data.alerts)
    return {kind: counts.get(kind, 0) for kind in ALERT_KINDS}


def _issues(data: ReportData) -> dict[str, Any]:
    items: list[tuple[datetime, dict[str, Any]]] = []
    for dose in data.doses:
        if dose.status == _S.MISSED.value:
            items.append((dose.scheduled_at, {
                "kind": "MISSED",
                "at": dose.scheduled_at.isoformat(),
                "at_local": data.local(dose.scheduled_at).isoformat(),
                "medication_id": dose.medication_id,
                "medication_name": dose.medication_name,
                "container_number": dose.container_number,
                "source": DropSource.SCHEDULE.value,
                "reason": dose.hardware_result,
                "needs_review": dose.needs_review,
                "drop_id": dose.drop_id,
                "event_id": dose.event_id,
            }))
    for drop in data.drops:
        if drop.status not in (DropStatus.FAILED.value, DropStatus.UNCERTAIN.value):
            continue
        items.append((drop.requested_at, {
            "kind": drop.status,
            "at": drop.requested_at.isoformat(),
            "at_local": data.local(drop.requested_at).isoformat(),
            "medication_id": drop.medication_id,
            "medication_name": drop.medication_name,
            "container_number": drop.container_number,
            "source": drop.source,
            "reason": drop.reason or drop.hardware_result,
            "needs_review": drop.needs_review,
            "review_note": drop.review_note,
            "drop_id": drop.drop_id,
            "event_id": drop.dose_event_id,
        }))
    items.sort(key=lambda it: (it[0], it[1]["kind"]))
    return {"issues": [it[1] for it in items[-MAX_ISSUES:]], "issues_total": len(items)}
