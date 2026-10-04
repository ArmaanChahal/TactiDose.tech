"""Device endpoints and the demo controls (console, clock travel, simulator, reset)."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any

from tactidose.core.bus import Topic
from tactidose.hardware.protocol import CommandName
from tests import test_api_support as support

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api


RESULT_KEYS = {"command", "ok", "code", "definitive", "gate_may_be_open", "elapsed_s", "messages",
               "hardware_result", "detail", "drop_certainty"}


# --------------------------------------------------------------------------- device


def test_device_snapshot(api):
    d = api.get("/api/device", actor="family").json()
    assert d["connected"] is True and d["state"] == "READY" and d["proto"] == "1.1"
    assert d["ready_for_motion"] is True and d["num_slots_reported"] == 3


def test_home_stop_reconnect(api):
    hw = api.services.hardware
    r = api.post("/api/device/home", actor="doctor").json()
    assert r["ok"] is True and set(r["result"]) == RESULT_KEYS and r["result"]["command"] == "HOME"
    assert r["device"]["homed"] is True
    stop = api.post("/api/device/stop", actor="patient").json()
    assert stop["result"]["code"] == "STOPPED" and stop["interrupted"] is False
    assert api.services.drops.interrupts == 1          # DropService is told first
    assert hw.commands() == ["HOME", "STOP"]
    rc = api.post("/api/device/reconnect", actor="family").json()
    assert rc == {"ok": True, "device": rc["device"]} and hw.reconnects == 1


def test_device_without_a_bound_patient(api, monkeypatch):
    from tactidose.api import views

    monkeypatch.setattr(views, "device_owner_id", lambda db, settings: None)
    assert api.get("/api/device").status_code == 403
    assert api.post("/api/demo/command", json={"line": "DROP_SLOT 0"}).status_code == 409
    assert api.post("/api/demo/jump-to-next-dose").status_code == 409


# --------------------------------------------------------------------------- console


def test_console_drop_is_a_recorded_demo_drop(api):
    r = api.post("/api/demo/command", actor="doctor", json={"line": "drop_slot 1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["drop"]["status"] == "DROPPED" and body["drop"]["source"] == "demo"
    assert api.services.drops.requests == [dict(
        patient_id=api.pid, source="demo", slot=1, medication_id=None, requested_by_user_id=api.uid("doctor"),
        conversation_id=None, dose_event_id=None)]
    assert api.services.hardware.commands(CommandName.DROP_SLOT) == []   # only DropService actuates


def test_console_refuses_unrecorded_releases(api):
    for line in ("DISPENSE_SLOT 1", "open_gate"):
        r = api.post("/api/demo/command", json={"line": line})
        assert r.status_code == 409 and "DROP_SLOT" in r.json()["detail"]
    assert api.services.hardware.commands() == [] and api.services.drops.requests == []


def test_console_rejects_invalid_lines(api):
    for line in ("", "   ", "FOO", "DROP_SLOT 9", "MOVE_SLOT x", "P" * 65):
        assert api.post("/api/demo/command", json={"line": line}).status_code == 422, line
    assert api.services.hardware.commands() == []


def test_console_raw_commands_and_stop(api):
    hw = api.services.hardware
    status = api.post("/api/demo/command", json={"line": "status"}).json()
    assert status["ok"] is True and status["result"]["code"] == "STATUS" and status["result"]["messages"]
    move = api.post("/api/demo/command", json={"line": "MOVE_SLOT 2"}).json()
    assert move["result"]["code"] == "AT_SLOT" and move["device"]["slot"] == 2
    stop = api.post("/api/demo/command", json={"line": "STOP"}).json()
    assert stop["result"]["code"] == "STOPPED" and api.services.drops.interrupts == 1
    assert hw.commands() == ["STATUS", "MOVE_SLOT 2", "STOP"]


# --------------------------------------------------------------------------- clock


def _local(view: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(view["now_local"])


def test_clock_get_shape(api):
    view = api.get("/api/demo/clock").json()
    assert set(view) == {"now_local", "now_utc", "offset_s", "travelling", "tz"}
    assert view["tz"] == "America/Vancouver" and view["travelling"] is True
    assert _local(view).strftime("%Y-%m-%d %H:%M") == "2026-10-05 07:55"


def test_clock_travel_runs_a_tick_and_wakes_the_loop(api):
    s = api.services
    sub = s.bus.subscribe([Topic.CLOCK_CHANGED])
    ticks, triggers = s.scheduler.ticks, s.scheduler_loop.triggers
    view = api.post("/api/demo/clock", json={"local_time": "08:00"}).json()
    assert _local(view).strftime("%Y-%m-%d %H:%M") == "2026-10-05 08:00"
    assert s.scheduler.ticks == ticks + 1 and s.scheduler_loop.triggers == triggers + 1
    [ev] = sub.drain()
    assert ev.data["now_local"] == view["now_local"] and "offset_s" in ev.data
    view = api.post("/api/demo/clock", json={"local_datetime": "2026-10-06T13:05"}).json()
    assert _local(view).strftime("%Y-%m-%d %H:%M") == "2026-10-06 13:05"
    view = api.post("/api/demo/clock", json={"local_datetime": "2026-10-06T20:00:00+00:00"}).json()
    assert _local(view).strftime("%H:%M") == "13:00"


def test_clock_offset_and_reset(api):
    view = api.post("/api/demo/clock", json={"offset_minutes": 30}).json()
    assert abs(view["offset_s"] - 1800) < 120
    view = api.post("/api/demo/clock", json={"reset": True}).json()
    assert view["travelling"] is False and abs(view["offset_s"]) < 120


def test_clock_validation(api):
    before = api.services.clock.now()
    for body in ({}, {"local_time": "08:00", "reset": True}, {"local_time": "25:00"}, {"local_time": "8"},
                 {"local_datetime": "tomorrow"}, {"offset_minutes": 100000}, {"reset": False},
                 {"local_datetime": "2030-01-01T08:00"}, {"speed": 2}):
        assert api.post("/api/demo/clock", json=body).status_code == 422, body
    assert api.services.clock.now() == before


def test_jump_to_next_dose(api):
    s = api.services
    at = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    s.drops.next_scheduled = {"event_id": 5, "scheduled_at": at.isoformat(), "status": "SCHEDULED"}
    r = api.post("/api/demo/jump-to-next-dose").json()
    assert r["next"]["event_id"] == 5 and s.clock.now() == at
    assert _local(r["clock"]).strftime("%H:%M") == "13:00"
    s.drops.next_scheduled = None
    before = s.clock.now()
    r = api.post("/api/demo/jump-to-next-dose").json()
    assert r["next"] is None and s.clock.now() == before


def test_jump_never_goes_backwards(api):
    s = api.services
    s.drops.next_scheduled = {"event_id": 1, "scheduled_at": (s.clock.now() - timedelta(hours=1)).isoformat()}
    before = s.clock.now()
    api.post("/api/demo/jump-to-next-dose")
    assert s.clock.now() == before


# --------------------------------------------------------------------------- simulator


def test_simulator_controls(api):
    sim = api.services.sim
    view = api.get("/api/demo/simulator").json()
    assert view["available"] is True and view["physical"]["pills"] == {"0": 20, "1": 20, "2": 20}
    assert api.post("/api/demo/simulator", json={"fault": "motor_jam", "enabled": True}).json()["faults"]["motor_jam"]
    assert api.post("/api/demo/simulator", json={"press": "confirm"}).status_code == 200 and sim.pressed == ["CONFIRM"]
    assert api.post("/api/demo/simulator", json={"reboot": True}).status_code == 200 and sim.reboots == 1
    r = api.post("/api/demo/simulator", json={"pills": {"slot": 2, "count": 0}})
    assert r.status_code == 200 and sim.pills[2] == 0 and r.json()["physical"]["pills"]["2"] == 0


def test_simulator_validation(api):
    for body in ({}, {"fault": "bogus", "enabled": True}, {"fault": "motor_jam"}, {"press": "JUMP"},
                 {"reboot": False}, {"pills": {"slot": 3, "count": 1}}, {"pills": {"slot": 0, "count": -1}},
                 {"reboot": True, "press": "CONFIRM"}):
        assert api.post("/api/demo/simulator", json=body).status_code == 422, body
    sim = api.services.sim
    assert sim.pressed == [] and sim.reboots == 0 and not any(sim.faults_on.values())


def test_simulator_absent_outside_sim_mode(api_settings, make_api):
    h = make_api(api_settings, sim=False)
    h.services.sim = None
    assert h.get("/api/demo/simulator").json() == {"available": False, "physical": None, "faults": {}}
    assert h.post("/api/demo/simulator", json={"reboot": True}).status_code == 409


# --------------------------------------------------------------------------- reset


def test_demo_reset_uses_the_seed_module_and_resets_the_clock(api, monkeypatch):
    calls: list[dict[str, Any]] = []

    def reset_demo(db: Any, settings: Any, clock: Any, *, auth: Any = None,
                   keep_sessions: bool = False) -> dict[str, Any]:
        calls.append({"db": db, "auth": auth, "keep_sessions": keep_sessions,
                      "locked": api.services.scheduler_loop._lock._is_owned(),
                      "travelling": clock.is_travelling})
        return {"wiped": {"pill_drops": 3}, "containers": [{"slot": 0, "pill_count": 20},
                                                          {"slot": 2, "pill_count": 3}]}

    monkeypatch.setitem(sys.modules, "tactidose.db.seed", types.SimpleNamespace(reset_demo=reset_demo))
    sub = api.services.bus.subscribe([Topic.NOTICE, Topic.CLOCK_CHANGED])
    ticks = api.services.scheduler.ticks
    api.services.sim.pills.update({0: 5, 1: 5, 2: 5})
    r = api.post("/api/demo/reset", actor="family", json={"reseed": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["summary"]["wiped"] == {"pill_drops": 3}
    # seeded on the real clock (no-backfill rule), with the scheduler loop paused
    assert calls == [{"db": api.services.db, "auth": api.services.auth, "keep_sessions": True, "locked": True,
                      "travelling": False}]
    assert api.services.sim.pills == {0: 20, 1: 5, 2: 3}     # physical counts follow the reset
    assert api.services.clock.is_travelling is False and api.services.scheduler.ticks == ticks + 1
    assert {ev.topic for ev in sub.drain()} == {Topic.NOTICE, Topic.CLOCK_CHANGED}


def test_demo_reset_unavailable_is_503(api, monkeypatch):
    monkeypatch.setitem(sys.modules, "tactidose.db.seed", None)
    assert api.post("/api/demo/reset", json={}).status_code == 503
