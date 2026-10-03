"""Append-only device/audit log (operational DB). Cheap, best-effort helpers."""

from __future__ import annotations

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
) -> DeviceLog:
    row = DeviceLog(
        device_id=device_id,
        category=category.value if isinstance(category, LogCategory) else str(category),
        event=event[:64],
        detail=detail or {},
        event_id=event_id,
    )
    session.add(row)
    return row
