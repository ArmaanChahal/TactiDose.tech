"""In-process publish/subscribe bus.

Every component publishes what it does (device state, serial lines, spoken text,
dose updates…). The web UI receives everything via Server-Sent Events, and tests
can assert on it. ``publish`` is thread-safe, never blocks and never raises.

Subscribers either pull from a thread-safe queue (:meth:`EventBus.subscribe`) or
an asyncio queue bound to their event loop (:meth:`EventBus.subscribe_async`).
Slow subscribers drop their *oldest* events rather than blocking publishers.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)


class Topic:
    """Well-known topics. Payloads are JSON-serialisable dicts."""

    DEVICE_STATE = "device.state"          # DeviceSnapshot.to_dict()
    DEVICE_LINE = "device.line"            # {"dir": "rx"|"tx", "line": str}
    DEVICE_EVENT = "device.event"          # {"code": "CONFIRM_BUTTON"|..., "line": str}
    SIM_PHYSICAL = "sim.physical"          # {"angle_deg", "slot", "gate_open", "state", ...}
    SPOKEN = "assistant.spoken"            # {"text", "kind", "intent", "source", "audio": "elevenlabs"|"cache"|"offline"|"none"}
    INTENT = "assistant.intent"            # {"intent", "source", "text"}
    ASSISTANT_STATE = "assistant.state"    # {"phase", "dose": DoseInfo|None, "message"}
    VOICE_HEARD = "voice.heard"            # {"text", "confidence", "intent", "accepted"}
    VOICE_STATUS = "voice.status"          # {"enabled", "listening", "muted", "error"}
    DOSE_UPDATED = "dose.updated"          # {"event_id", "status", ...}
    DATA_CHANGED = "data.changed"          # {"entity": "medication"|"schedule"|"compartment"|"scan", "id"}
    CLOCK_CHANGED = "clock.changed"        # {"now_local", "offset_s"}
    NOTICE = "system.notice"               # {"level": "info"|"warning"|"error", "message"}
    ANALYTICS_SYNC = "analytics.sync"      # SnowflakeSync.sync_once() report
    # ---- v2 (every payload carries "patient_id" so the SSE endpoint can filter per user)
    NOTIFICATION = "notification"          # Notification view incl. "user_id" (recipient) and "patient_id"
    DROP = "drop.updated"                  # PillDropView (has "patient_id")
    PATIENT_STATUS = "patient.status"      # {"patient_id", "reason"} — refetch hint for portals
    AGENT = "agent.message"                # {"patient_id", "conversation_id", "message_id", "role"}
    REPORT = "report.updated"              # {"patient_id", "report_id", "status"}
    #: The well-being check-in asked after a drop: {"user_id" (= the patient), "patient_id",
    #: "session_id", "drop_id", "text", "next_question"} - sent to the patient only.
    WELLBEING_PROMPT = "wellbeing.prompt"
    #: Guided judge demo progress (tactidose/guided/runner.py): {"patient_id", "run_id", "state",
    #: "slot", "step", "say", "audio_url", "awaiting", "heard", "buzzer", "outcome", "results"}.
    DEMO_GUIDED = "demo.guided"


@dataclass(frozen=True)
class BusEvent:
    seq: int
    topic: str
    data: dict[str, Any]
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "topic": self.topic, "data": self.data, "ts": self.ts}


def _matches(topics: frozenset[str] | None, topic: str) -> bool:
    if topics is None:
        return True
    if topic in topics:
        return True
    return any(t.endswith(".*") and topic.startswith(t[:-1]) for t in topics)


class Subscription:
    """Thread-safe queue subscription. Use as an iterator or call :meth:`get`."""

    def __init__(self, bus: "EventBus", topics: frozenset[str] | None, maxsize: int) -> None:
        self._bus = bus
        self.topics = topics
        self._q: queue.Queue[BusEvent] = queue.Queue(maxsize=maxsize)
        self.closed = False

    def _deliver(self, ev: BusEvent) -> None:
        if self.closed or not _matches(self.topics, ev.topic):
            return
        while True:
            try:
                self._q.put_nowait(ev)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()  # drop oldest
                except queue.Empty:
                    pass

    def get(self, timeout: float | None = None) -> BusEvent | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[BusEvent]:
        out: list[BusEvent] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self.closed = True
        self._bus._remove(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncSubscription:
    """asyncio subscription bound to the loop that created it (for SSE handlers)."""

    def __init__(self, bus: "EventBus", topics: frozenset[str] | None, maxsize: int) -> None:
        self._bus = bus
        self.topics = topics
        self._loop = asyncio.get_running_loop()
        self._q: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=maxsize)
        self.closed = False

    def _put(self, ev: BusEvent) -> None:
        if self.closed:
            return
        if self._q.full():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._q.put_nowait(ev)

    def _deliver(self, ev: BusEvent) -> None:
        if self.closed or not _matches(self.topics, ev.topic):
            return
        try:
            self._loop.call_soon_threadsafe(self._put, ev)
        except RuntimeError:  # loop closed
            self.close()

    async def get(self, timeout: float | None = None) -> BusEvent | None:
        try:
            if timeout is None:
                return await self._q.get()
            return await asyncio.wait_for(self._q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.closed = True
        self._bus._remove(self)


class EventBus:
    def __init__(self, history: int = 400) -> None:
        self._lock = threading.Lock()
        self._subs: list[Subscription | AsyncSubscription] = []
        self._listeners: list[tuple[frozenset[str] | None, Callable[[BusEvent], None]]] = []
        self._history: deque[BusEvent] = deque(maxlen=history)
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ publish
    def publish(self, topic: str, data: dict[str, Any] | None = None) -> BusEvent:
        with self._lock:
            ev = BusEvent(seq=next(self._seq), topic=topic, data=dict(data or {}))
            self._history.append(ev)
            subs = list(self._subs)
            listeners = list(self._listeners)
        for sub in subs:
            try:
                sub._deliver(ev)
            except Exception:  # noqa: BLE001 - never let a subscriber break a publisher
                log.exception("bus subscriber failed")
        for topics, cb in listeners:
            if _matches(topics, topic):
                try:
                    cb(ev)
                except Exception:  # noqa: BLE001
                    log.exception("bus listener failed for %s", topic)
        return ev

    # ------------------------------------------------------------------ subscribe
    def subscribe(self, topics: Iterable[str] | None = None, *, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, frozenset(topics) if topics else None, maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def subscribe_async(self, topics: Iterable[str] | None = None, *, maxsize: int = 1000) -> AsyncSubscription:
        """Must be called from inside a running event loop."""
        sub = AsyncSubscription(self, frozenset(topics) if topics else None, maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def add_listener(
        self, callback: Callable[[BusEvent], None], topics: Iterable[str] | None = None
    ) -> Callable[[], None]:
        """Synchronous callback run in the publisher's thread. Keep it fast."""
        entry = (frozenset(topics) if topics else None, callback)
        with self._lock:
            self._listeners.append(entry)

        def _remove() -> None:
            with self._lock:
                if entry in self._listeners:
                    self._listeners.remove(entry)

        return _remove

    def recent(self, limit: int = 100, topics: Iterable[str] | None = None) -> list[BusEvent]:
        t = frozenset(topics) if topics else None
        with self._lock:
            items = [e for e in self._history if _matches(t, e.topic)]
        return items[-limit:]

    def _remove(self, sub: Subscription | AsyncSubscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
