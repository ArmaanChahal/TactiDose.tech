"""Transactional analytics outbox + de-identified record builders.

Domain code calls :func:`enqueue_adherence` / :func:`enqueue_device_event` inside
the same DB transaction that changes state, so analytics can never disagree with
operational state. ``tactidose.integrations.snowflake`` drains the outbox.

De-identification (handoff §20): records carry a keyed pseudonym of the user id,
the device id, timing and outcome fields — never names or medication details.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone, tzinfo
from typing import Any

from sqlalchemy.orm import Session

from tactidose.db.models import AnalyticsOutbox, DoseEvent, DoseStatus

KIND_ADHERENCE = "adherence"
KIND_DEVICE_EVENT = "device_event"


def pseudonymize(value: object, salt: str) -> str:
    """Stable keyed pseudonym (HMAC-SHA256, 16 hex chars)."""
    return hmac.new(salt.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def time_window(local_hour: int) -> str:
    if 5 <= local_hour < 12:
        return "morning"
    if 12 <= local_hour < 17:
        return "afternoon"
    if 17 <= local_hour < 22:
        return "evening"
    return "night"


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _minutes(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() / 60.0, 2)


def _to_local(dt: datetime, tz: tzinfo | None) -> datetime:
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def adherence_payload(
    event: DoseEvent, *, salt: str, tz: tzinfo | None = None, recorded_at: datetime | None = None
) -> dict[str, Any]:
    """De-identified snapshot of one dose event (latest state wins downstream)."""
    local = _to_local(event.scheduled_at, tz)
    status = event.status
    return {
        "event_uid": f"{event.device_id}:{event.event_id}",
        "device_id": event.device_id,
        "user_hash": pseudonymize(event.user_id, salt),
        "schedule_hash": pseudonymize(f"schedule:{event.schedule_id}", salt),
        "scheduled_at": _iso(event.scheduled_at),
        "scheduled_local_date": local.date().isoformat(),
        "scheduled_local_hour": local.hour,
        "scheduled_local_dow": local.strftime("%a").upper(),
        "time_window": time_window(local.hour),
        "dispensed_at": _iso(event.dispensed_at),
        "confirmed_taken_at": _iso(event.confirmed_taken_at),
        "dispense_delay_minutes": _minutes(event.dispensed_at, event.scheduled_at),
        "confirm_delay_minutes": _minutes(event.confirmed_taken_at, event.scheduled_at),
        "delay_minutes": _minutes(event.confirmed_taken_at or event.dispensed_at, event.scheduled_at),
        "missed": status == DoseStatus.MISSED.value,
        "taken": status == DoseStatus.TAKEN.value,
        "final_status": status,
        "hardware_result": event.hardware_result,
        "needs_review": bool(event.needs_review),
        "attempts": int(event.attempts or 0),
        "slot_number": event.slot_number,
        "recorded_at": _iso(recorded_at or datetime.now(timezone.utc)),
    }


def enqueue(session: Session, kind: str, dedupe_key: str, payload: dict[str, Any]) -> AnalyticsOutbox:
    row = AnalyticsOutbox(kind=kind, dedupe_key=dedupe_key[:128], payload=payload)
    session.add(row)
    return row


def enqueue_adherence(
    session: Session,
    event: DoseEvent,
    *,
    salt: str,
    tz: tzinfo | None = None,
    recorded_at: datetime | None = None,
) -> AnalyticsOutbox:
    """Call after every DoseEvent status change, in the same transaction."""
    if event.event_id is None:
        session.flush()
    payload = adherence_payload(event, salt=salt, tz=tz, recorded_at=recorded_at)
    return enqueue(session, KIND_ADHERENCE, f"{KIND_ADHERENCE}:{payload['event_uid']}", payload)


def enqueue_device_event(
    session: Session,
    *,
    device_id: str,
    event_type: str,
    code: str | None = None,
    at: datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> AnalyticsOutbox:
    """Hardware faults / resets / disconnects for 'device error frequency' analytics.

    ``detail`` must not contain personal data (it is sent to Snowflake verbatim).
    """
    at = at or datetime.now(timezone.utc)
    payload = {
        "device_id": device_id,
        "event_type": event_type,
        "code": code,
        "occurred_at": _iso(at),
        "detail": detail or {},
    }
    key = f"{KIND_DEVICE_EVENT}:{device_id}:{event_type}:{payload['occurred_at']}"
    return enqueue(session, KIND_DEVICE_EVENT, key, payload)
