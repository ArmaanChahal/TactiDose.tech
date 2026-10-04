"""``GET /api/events``: per-user filtering, replay, keep-alive, envelope and headers.

The TestClient buffers a whole response, so the harness limits each stream to
``services.sse_max_stream_s`` (0.15 s; replayed events arrive immediately). Scope refresh / sign-out are tested on the generator itself.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from tactidose.api.events import EventScope, bus_epoch, event_stream, parse_last_event_id
from tactidose.core.bus import Topic
from tactidose.db.models import CareLink
from tests import test_api_support as support

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api


def parse_sse(text: str) -> tuple[list[tuple[str, dict[str, Any], str | None]], list[str]]:
    events: list[tuple[str, dict[str, Any], str | None]] = []
    comments: list[str] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                comments.append(line)
            elif line:
                key, _, value = line.partition(":")
                fields[key] = value[1:] if value.startswith(" ") else value
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"]), fields.get("id")))
    return events, comments


def publish_world(h: Any) -> dict[str, int]:
    """One event of every interesting kind, about Alex and about the other patient."""
    bus = h.services.bus
    alex, other = h.pid, h.uid("other")
    seqs = {}
    seqs["alex_drop"] = bus.publish(Topic.DROP, {"patient_id": alex, "drop_id": 1, "status": "DROPPED"}).seq
    seqs["other_drop"] = bus.publish(Topic.DROP, {"patient_id": other, "drop_id": 2}).seq
    seqs["alex_status"] = bus.publish(Topic.PATIENT_STATUS, {"patient_id": alex, "reason": "drop"}).seq
    seqs["other_agent"] = bus.publish(Topic.AGENT, {"patient_id": other, "conversation_id": 9, "message_id": 1,
                                                    "role": "assistant"}).seq
    seqs["alex_report"] = bus.publish(Topic.REPORT, {"patient_id": alex, "report_id": 3, "status": "READY"}).seq
    for actor in ("patient", "doctor", "family", "stranger", "other"):
        seqs[f"note_{actor}"] = bus.publish(Topic.NOTIFICATION, {
            "notification_id": 100 + h.uid(actor), "user_id": h.uid(actor), "patient_id": alex,
            "kind": "PILL_DROPPED", "title": "Pill dropped"}).seq
    seqs["device_state"] = bus.publish(Topic.DEVICE_STATE, {"connected": True, "state": "READY"}).seq
    seqs["device_line"] = bus.publish(Topic.DEVICE_LINE, {"dir": "tx", "line": "PING"}).seq
    seqs["sim"] = bus.publish(Topic.SIM_PHYSICAL, {"slot": 0}).seq
    seqs["clock"] = bus.publish(Topic.CLOCK_CHANGED, {"now_local": "x", "offset_s": 0}).seq
    seqs["notice"] = bus.publish(Topic.NOTICE, {"level": "info", "message": "hi"}).seq
    seqs["dose"] = bus.publish(Topic.DOSE_UPDATED, {"event_id": 1, "status": "DUE"}).seq
    seqs["data"] = bus.publish(Topic.DATA_CHANGED, {"entity": "schedule", "id": 1}).seq
    seqs["unknown"] = bus.publish("something.else", {"patient_id": alex}).seq
    return seqs


DEVICE_AND_DEMO = {"device_state", "device_line", "sim", "clock", "notice"}


def received(h: Any, actor: str, **kw: Any) -> tuple[set[str], Any]:
    seqs = publish_world(h)
    r = h.get("/api/events", actor=actor, **kw)
    assert r.status_code == 200, r.text
    events, _ = parse_sse(r.text)
    by_seq = {v: k for k, v in seqs.items()}
    return {by_seq[ev[1]["seq"]] for ev in events if ev[1]["seq"] in by_seq}, r


def test_patient_sees_own_events_and_device(api):
    got, r = received(api, "patient")
    assert got == {"alex_drop", "alex_status", "alex_report", "note_patient"} | DEVICE_AND_DEMO
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in r.headers


def test_linked_caregiver_sees_patient_events_and_only_own_notifications(api):
    got, _ = received(api, "doctor")
    assert got == {"alex_drop", "alex_status", "alex_report", "note_doctor"} | DEVICE_AND_DEMO


def test_unlinked_caregiver_sees_only_own_notifications(api):
    got, _ = received(api, "stranger")
    assert got == {"note_stranger"}


def test_other_patient_sees_only_their_events(api):
    got, _ = received(api, "other")
    assert got == {"other_drop", "other_agent", "note_other"}   # not linked to the device's patient


def test_demo_topics_need_demo_mode(api_settings, make_api):
    h = make_api(api_settings.model_copy(update={"demo_mode": False}))
    got, _ = received(h, "patient")
    assert got == {"alex_drop", "alex_status", "alex_report", "note_patient", "device_state"}


def test_envelope_and_live_delivery(api):
    bus = api.services.bus

    def later() -> None:
        bus.publish(Topic.DROP, {"patient_id": api.pid, "drop_id": 77, "status": "DROPPED"})

    api.services.sse_max_stream_s = 1.0
    timer = threading.Timer(0.1, later)
    timer.start()
    try:
        r = api.get("/api/events", actor="family")
    finally:
        timer.cancel()
    events, _ = parse_sse(r.text)
    live = [e for e in events if e[1]["data"].get("drop_id") == 77]
    assert len(live) == 1
    topic, envelope, ev_id = live[0]
    assert topic == "drop.updated" and set(envelope) == {"seq", "topic", "data", "ts"}
    assert envelope["topic"] == topic and ev_id == f"{bus_epoch(api.services.bus)}.{envelope['seq']}"
    assert r.text.startswith("retry: 3000")


def test_replay_is_limited_to_the_last_50_permitted(api):
    bus = api.services.bus
    for i in range(70):
        bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.pid, "reason": f"r{i}"})
        bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.uid("other"), "reason": "not yours"})
    events, _ = parse_sse(api.get("/api/events").text)
    reasons = [e[1]["data"]["reason"] for e in events]
    assert len(reasons) == 50 and reasons[0] == "r20" and reasons[-1] == "r69"


def test_last_event_id_replays_only_newer_events(api):
    bus = api.services.bus
    first = bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.pid, "reason": "old"})
    bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.pid, "reason": "new"})
    last = f"{bus_epoch(bus)}.{first.seq}"
    events, _ = parse_sse(api.get("/api/events", headers={"Last-Event-ID": last}).text)
    assert [e[1]["data"]["reason"] for e in events] == ["new"]


def test_last_event_id_from_before_a_restart_replays_everything(api):
    bus = api.services.bus
    bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.pid, "reason": "after restart"})
    for stale in ("ffffffff.9999", "9999", "garbage"):
        events, _ = parse_sse(api.get("/api/events", headers={"Last-Event-ID": stale}).text)
        assert [e[1]["data"]["reason"] for e in events] == ["after restart"], stale


def test_parse_last_event_id():
    assert parse_last_event_id("ab12.7", "ab12") == 7
    assert parse_last_event_id("ab12.7", "cd34") is None
    assert parse_last_event_id(None, "ab12") is None and parse_last_event_id("ab12.x", "ab12") is None


def test_keepalive_comments(api):
    api.services.sse_keepalive_s = 0.05
    api.services.sse_max_stream_s = 0.5
    _, comments = parse_sse(api.get("/api/events", actor="stranger").text)
    assert ": keep-alive" in comments


def test_stream_ends_when_the_app_stops(api):
    api.services.sse_max_stream_s = 30.0
    api.services.stopping.set()
    r = api.get("/api/events")
    assert r.status_code == 200 and r.text.startswith("retry:")


def test_requires_a_session(api):
    assert api.get("/api/events", actor=None).status_code == 401


# --------------------------------------------------------------------------- generator


def _drive(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


def test_scope_refresh_picks_up_new_links(api):
    s = api.services
    s.sse_scope_refresh_s = 0.0          # re-check links on every wake-up
    s.sse_keepalive_s = 0.05             # a heartbeat proves queued events were filtered
    s.sse_max_stream_s = 20.0
    stranger = s.auth.resolve(api.tokens["stranger"])

    async def until(gen: Any, predicate: Any) -> list[str]:
        seen: list[str] = []
        while True:
            chunk = await gen.__anext__()
            seen.append(chunk)
            if predicate(chunk):
                return seen

    async def main() -> tuple[list[str], list[str]]:
        gen = event_stream(s, EventScope.load(s, stranger), token=api.tokens["stranger"])
        assert (await gen.__anext__()).startswith("retry:")
        s.bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.pid, "reason": "before link"})
        before = await until(gen, lambda c: c.startswith(": keep-alive"))
        with s.db.session() as db:
            db.add(CareLink(caregiver_id=api.uid("stranger"), patient_id=api.pid, relationship_kind="doctor"))
        s.bus.publish(Topic.PATIENT_STATUS, {"patient_id": api.pid, "reason": "after link"})
        after = await until(gen, lambda c: "after link" in c)
        await gen.aclose()
        return before, after

    before, after = _drive(main())
    assert not any("before link" in c for c in before)
    assert "patient.status" in after[-1]


def test_stream_ends_after_sign_out(api):
    s = api.services
    s.sse_scope_refresh_s = 0.0
    s.sse_max_stream_s = 20.0
    token = api.tokens["family"]
    user = s.auth.resolve(token)

    async def main() -> list[str]:
        gen = event_stream(s, EventScope.load(s, user), token=token)
        chunks = [await gen.__anext__()]
        s.auth.logout(token)
        async for chunk in gen:
            chunks.append(chunk)
        return chunks

    chunks = _drive(main())
    assert chunks[0].startswith("retry:")      # ended by itself, well before the 20 s limit


@pytest.mark.parametrize("data,ok", [({"patient_id": "1"}, True), ({"patient_id": True}, False), ({}, False)])
def test_scope_patient_id_coercion(data, ok):
    from tactidose.core.bus import BusEvent

    scope = EventScope(user_id=5, patient_ids=frozenset({1}), device_patient_id=1, demo=True)
    assert scope.permits(BusEvent(1, Topic.DROP, data)) is ok
    assert scope.permits(BusEvent(2, Topic.NOTIFICATION, {"user_id": 5})) is True
    assert scope.permits(BusEvent(3, Topic.NOTIFICATION, {"user_id": 6, "patient_id": 1})) is False
