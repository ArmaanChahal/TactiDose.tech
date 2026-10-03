"""Time source for all domain logic.

Never call ``datetime.now()`` in domain code — use :class:`Clock`. This lets the
demo operator "time travel" (e.g. jump to 08:00 to show a due dose) and lets
tests freeze time. Timers/timeouts that must not be affected by travel use
:meth:`Clock.monotonic`.

Timezone: ``TACTIDOSE_TIMEZONE`` (IANA name) if set, otherwise the operating
system's local zone (DST handled by the OS).
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo


def resolve_timezone(name: str | None) -> tzinfo | None:
    """IANA zone for ``name``; ``None`` means "system local" (handled via astimezone())."""
    if not name:
        return None
    return ZoneInfo(name)


class Clock:
    def __init__(self, tz_name: str | None = None, *, frozen_at: datetime | None = None) -> None:
        self._tz = resolve_timezone(tz_name)
        self._tz_name = tz_name
        self._offset = timedelta(0)
        self._frozen: datetime | None = None
        self._lock = threading.Lock()
        if frozen_at is not None:
            self.freeze(frozen_at)

    # ------------------------------------------------------------------ zone
    @property
    def tz(self) -> tzinfo | None:
        """Configured zone, or ``None`` for the OS local zone."""
        return self._tz

    @property
    def tz_name(self) -> str:
        if self._tz_name:
            return self._tz_name
        return datetime.now().astimezone().tzname() or "local"

    def to_local(self, dt: datetime) -> datetime:
        """Aware UTC (or any aware) datetime -> aware local datetime."""
        if dt.tzinfo is None:
            raise ValueError("to_local() needs an aware datetime")
        return dt.astimezone(self._tz) if self._tz is not None else dt.astimezone()

    def localize(self, naive_local: datetime) -> datetime:
        """Naive local wall-clock time -> aware local datetime (fold=0 on DST overlaps)."""
        if naive_local.tzinfo is not None:
            return self.to_local(naive_local)
        if self._tz is not None:
            return naive_local.replace(tzinfo=self._tz)
        return naive_local.astimezone()  # Python treats naive as system-local here

    def local_to_utc(self, naive_local: datetime) -> datetime:
        return self.localize(naive_local).astimezone(timezone.utc)

    # ------------------------------------------------------------------ now
    def now(self) -> datetime:
        """Current (possibly travelled) time, aware UTC."""
        with self._lock:
            if self._frozen is not None:
                return self._frozen
            return datetime.now(timezone.utc) + self._offset

    def local_now(self) -> datetime:
        return self.to_local(self.now())

    def today_local(self) -> date:
        return self.local_now().date()

    @staticmethod
    def monotonic() -> float:
        """Seconds from a monotonic clock — unaffected by time travel."""
        return time.monotonic()

    # ------------------------------------------------------------------ travel (demo)
    @property
    def offset(self) -> timedelta:
        with self._lock:
            return self._offset

    @property
    def is_travelling(self) -> bool:
        with self._lock:
            return self._offset != timedelta(0) or self._frozen is not None

    def set_offset(self, offset: timedelta) -> None:
        with self._lock:
            self._offset = offset

    def travel_to(self, target: datetime) -> None:
        """Make ``now()`` read ``target`` (aware, or naive local wall-clock)."""
        if target.tzinfo is None:
            target = self.localize(target)
        with self._lock:
            if self._frozen is not None:
                self._frozen = target.astimezone(timezone.utc)
            else:
                self._offset = target.astimezone(timezone.utc) - datetime.now(timezone.utc)

    def reset(self) -> None:
        with self._lock:
            self._offset = timedelta(0)
            self._frozen = None

    # ------------------------------------------------------------------ tests
    def freeze(self, at: datetime) -> None:
        if at.tzinfo is None:
            at = self.localize(at)
        with self._lock:
            self._frozen = at.astimezone(timezone.utc)

    def advance(self, delta: timedelta) -> None:
        with self._lock:
            if self._frozen is not None:
                self._frozen = self._frozen + delta
            else:
                self._offset = self._offset + delta
