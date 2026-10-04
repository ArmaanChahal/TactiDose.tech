"""``GET /api/events`` — Server-Sent Events, filtered per user (docs/API.md v2).

Each message is ``id: <epoch>.<seq>`` + ``event: <bus topic>`` + ``data: {"seq","topic","data","ts"}``.
On connect the last :data:`REPLAY` *permitted* events are replayed (only those after
``Last-Event-ID`` when the browser reconnects); a comment keep-alive is sent every 15 s. The
epoch identifies the event bus (bus sequence numbers restart with the server), so an id from
before a restart replays the history instead of hiding every new event.

Who receives what (:meth:`EventScope.permits`):

* ``notification`` — only its recipient (``data.user_id``);
* ``wellbeing.prompt`` — only the patient it asks (``data.user_id``), never caregivers;
* ``drop.updated`` / ``patient.status`` / ``agent.message`` / ``report.updated`` — the
  patient (``data.patient_id``) themself and caregivers linked to them;
* ``device.state`` (+ the device-side voice captions ``assistant.spoken`` / ``voice.*``) — users
  linked to the device's patient;
* demo mode only: ``device.line``, ``device.event``, ``sim.physical``, ``clock.changed``,
  ``system.notice``, ``demo.guided`` — users linked to the device's patient;
* anything else is never forwarded.

The scope (links, device owner) and the session are re-checked every
:data:`SCOPE_REFRESH_S`; a revoked session ends the stream.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import weakref
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from tactidose.api import views
from tactidose.api.common import TactiRoute
from tactidose.auth.deps import CurrentUser, ServicesDep, session_token
from tactidose.core.bus import BusEvent, Topic
from tactidose.core.interfaces import AuthUser

log = logging.getLogger(__name__)

router = APIRouter(route_class=TactiRoute, tags=["events"])

REPLAY = 50
KEEPALIVE_S = 15.0
SCOPE_REFRESH_S = 30.0
#: Longest single wait on the queue, so shutdown and the stream limit are noticed quickly.
POLL_S = 0.5
MIN_WAIT_S = 0.01

PATIENT_TOPICS = frozenset({Topic.DROP, Topic.PATIENT_STATUS, Topic.AGENT, Topic.REPORT})
DEVICE_TOPICS = frozenset({Topic.DEVICE_STATE, Topic.SPOKEN, Topic.VOICE_HEARD, Topic.VOICE_STATUS})
DEMO_TOPICS = frozenset({Topic.DEVICE_LINE, Topic.DEVICE_EVENT, Topic.SIM_PHYSICAL, Topic.CLOCK_CHANGED, Topic.NOTICE,
                         Topic.DEMO_GUIDED})

HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

_EPOCHS: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


def bus_epoch(bus: Any) -> str:
    """Random tag of one event bus (one server run): the prefix of every SSE event id."""
    epoch = _EPOCHS.get(bus)
    if epoch is None:
        epoch = _EPOCHS.setdefault(bus, secrets.token_hex(4))
    return epoch


def parse_last_event_id(raw: str | None, epoch: str) -> int | None:
    """Sequence number from ``<epoch>.<seq>``; None for ids of another bus/run (replay everything)."""
    if not raw:
        return None
    tag, _, seq = raw.strip().partition(".")
    return int(seq) if tag == epoch and seq.isdigit() else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


@dataclass(frozen=True)
class EventScope:
    """What one signed-in user may see on the event stream."""

    user_id: int
    patient_ids: frozenset[int]
    device_patient_id: int | None
    demo: bool
    #: Shared dispenser (one ESP32 for everyone): device topics go to every patient and caregiver.
    shared: bool = False

    @classmethod
    def load(cls, services: Any, user: AuthUser) -> "EventScope":
        if user.is_patient:
            pids = {user.user_id}
        elif user.is_caregiver:
            pids = {int(p) for p in services.auth.linked_patient_ids(user)}
        else:
            pids = set()
        owner = views.device_owner_id(services.db, services.settings)
        return cls(user.user_id, frozenset(pids), owner, bool(services.settings.demo_mode),
                   bool(services.settings.effective_shared_device))

    @property
    def device_linked(self) -> bool:
        if self.shared and self.patient_ids:
            return True
        return self.device_patient_id is not None and self.device_patient_id in self.patient_ids

    def permits(self, ev: BusEvent) -> bool:
        topic, data = ev.topic, ev.data or {}
        if topic in (Topic.NOTIFICATION, Topic.WELLBEING_PROMPT):
            return _as_int(data.get("user_id")) == self.user_id
        if topic in PATIENT_TOPICS:
            return _as_int(data.get("patient_id")) in self.patient_ids
        if topic in DEVICE_TOPICS:
            return self.device_linked
        if topic in DEMO_TOPICS:
            return self.demo and self.device_linked
        return False


def format_event(ev: BusEvent, epoch: str = "0") -> str:
    payload = json.dumps(ev.to_dict(), separators=(",", ":"), ensure_ascii=False, default=str)
    return f"id: {epoch}.{ev.seq}\nevent: {ev.topic}\ndata: {payload}\n\n"


def _refresh(services: Any, token: str | None) -> EventScope | None:
    """Re-validate the session and reload links; None ends the stream."""
    user = services.auth.resolve(token) if token else None
    return EventScope.load(services, user) if user is not None else None


async def event_stream(
    services: Any,
    scope: EventScope,
    *,
    token: str | None,
    last_event_id: int | None = None,
    request: Request | None = None,
) -> AsyncIterator[str]:
    bus = services.bus
    epoch = bus_epoch(bus)
    sub = bus.subscribe_async(maxsize=1000)
    keepalive = float(getattr(services, "sse_keepalive_s", KEEPALIVE_S))
    max_s = getattr(services, "sse_max_stream_s", None)
    refresh_s = float(getattr(services, "sse_scope_refresh_s", SCOPE_REFRESH_S))
    stopping = getattr(services, "stopping", None)
    started = time.monotonic()
    next_ping = started + keepalive
    next_refresh = started + refresh_s
    sent = int(last_event_id or 0)
    try:
        yield "retry: 3000\n\n"
        history = [ev for ev in bus.recent(limit=100_000) if ev.seq > sent and scope.permits(ev)]
        for ev in history[-REPLAY:]:
            yield format_event(ev, epoch)
            sent = ev.seq
        while True:
            now = time.monotonic()
            if stopping is not None and stopping.is_set():
                break
            if max_s is not None and now - started >= max_s:
                break
            if request is not None and await request.is_disconnected():
                break
            if now >= next_refresh:
                fresh = await run_in_threadpool(_refresh, services, token)
                if fresh is None:
                    break  # signed out: the browser's reconnect then gets a 401
                scope = fresh
                next_refresh = now + refresh_s
            wait = min(POLL_S, next_ping - now, next_refresh - now)
            if max_s is not None:
                wait = min(wait, started + max_s - now)
            # wait_for(..., 0) never returns a queued item: keep a small positive floor.
            ev = await sub.get(timeout=max(MIN_WAIT_S, wait))
            if ev is None:
                if time.monotonic() >= next_ping:
                    yield ": keep-alive\n\n"
                    next_ping = time.monotonic() + keepalive
                continue
            if ev.seq <= sent or not scope.permits(ev):
                continue
            yield format_event(ev, epoch)
            sent = ev.seq
            next_ping = time.monotonic() + keepalive
    finally:
        sub.close()


@router.get("/api/events")
async def events(request: Request, user: CurrentUser, services: ServicesDep) -> StreamingResponse:
    scope = await run_in_threadpool(EventScope.load, services, user)
    token = session_token(request, services.settings)
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    last_id = parse_last_event_id(raw, bus_epoch(services.bus))
    stream = event_stream(services, scope, token=token, last_event_id=last_id, request=request)
    return StreamingResponse(stream, media_type="text/event-stream", headers=HEADERS)
