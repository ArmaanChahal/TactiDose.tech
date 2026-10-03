"""Portable column types."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Aware-UTC datetimes in Python, naive UTC in the database.

    * Binding an aware datetime converts it to UTC; binding a naive one is an error
      (prevents silently storing local wall-clock times).
    * Loaded values are always ``tzinfo=timezone.utc``.
    * MySQL/TiDB get microsecond precision (``DATETIME(6)``).
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[override]
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expects datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime bound to UTCDateTime column; pass an aware datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[override]
        if value is None:
            return None
        if isinstance(value, str):  # defensive: some SQLite paths return text
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
