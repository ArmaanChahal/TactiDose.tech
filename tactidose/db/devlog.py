"""Append-only device/audit log (operational DB). Cheap, best-effort helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from tactidose.db.models import DeviceLog, LogCategory


def log_event(
    session: Session,
    device_id: str,
    category: LogCategory | str,
    event: str,
    detail: dict[str, Any] | None = None,
    *,
    event_id: int | None = None,
    at: datetime | None = None,
) -> DeviceLog:
    """Append one audit row. Pass ``at=clock.now()`` so rows follow the demo clock."""
    row = DeviceLog(
        device_id=device_id,
        category=category.value if isinstance(category, LogCategory) else str(category),
        event=event[:64],
        detail=detail or {},
        event_id=event_id,
    )
    if at is not None:
        row.created_at = at
    session.add(row)
    return row
