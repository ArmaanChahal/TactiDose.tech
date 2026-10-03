"""Tests for the frozen foundation: protocol, config, clock, bus, DB types/models, fakes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.db.models import DoseEvent, DoseStatus, Medication, Schedule
from tactidose.db.outbox import adherence_payload, enqueue_adherence, pseudonymize, time_window
from tactidose.hardware import protocol as p
from tests.fakes import FakeHardware, seed_minimal


# --------------------------------------------------------------------------- protocol


def test_command_encoding_and_validation():
    assert p.Command.ping().encode() == b"PING\n"
    assert p.Command.dispense_slot(3, 6).to_line() == "DISPENSE_SLOT 3"
    with pytest.raises(p.ProtocolError):
        p.Command.move_slot(6, 6)
    with pytest.raises(p.ProtocolError):
        p.Command.move_slot(-1, 6)
    with pytest.raises(p.ProtocolError):
        p.Command.move_slot(True, 6)  # bool is not a slot
    with pytest.raises(p.ProtocolError):
        p.Command(p.CommandName.PING, slot=1)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("PING", ("PING", None, None)),
        ("  ping  ", ("PING", None, None)),
        ("move_slot   2", ("MOVE_SLOT", 2, None)),
        ("MOVE_SLOT 002", ("MOVE_SLOT", 2, None)),
        ("MOVE_SLOT 6", (None, None, "INVALID_SLOT")),
        ("MOVE_SLOT -1", (None, None, "INVALID_SLOT")),
        ("MOVE_SLOT +1", (None, None, "INVALID_SLOT")),
        ("MOVE_SLOT 1 2", (None, None, "INVALID_SLOT")),
        ("MOVE_SLOT 1000", (None, None, "INVALID_SLOT")),
        ("MOVE_SLOT", (None, None, "INVALID_SLOT")),
        ("DISPENSE_SLOT x", (None, None, "INVALID_SLOT")),
        ("FOO", (None, None, "UNKNOWN_COMMAND")),
        ("OPEN GATE", (None, None, "UNKNOWN_COMMAND")),
        ("PING extra", (None, None, "UNKNOWN_COMMAND")),
        ("P" * 65, (None, None, "UNKNOWN_COMMAND")),
    ],
)
def test_parse_command(line, expected):
    parsed = p.parse_command(line, 6)
    name, slot, err = expected
    if err:
        assert parsed.command is None and parsed.error is not None and parsed.error.value == err
    else:
        assert parsed.command is not None
        assert parsed.command.name.value == name and parsed.command.slot == slot


def test_parse_command_blank_is_empty():
    assert p.parse_command("   ", 6).empty
    assert p.parse_command("\r\n", 6).empty


def test_parse_message_and_noise():
    m = p.parse_message("OK MOVING 3\r\n")
    assert m and m.kind is p.MessageKind.OK and m.code == "MOVING" and m.slot == 3
    assert p.parse_message("ets Jun  8 2016 00:22:57") is None
    assert p.parse_message("# debug: speed=1600") is None
    assert p.parse_message("OK") is None
    assert p.parse_message("") is None
    e = p.parse_message("event confirm_button")
    assert e and e.is_event(p.Ev.CONFIRM_BUTTON)
    assert p.parse_message("ERR BUSY").is_err(p.Err.BUSY)


def test_status_report_round_trip():
    rep = p.StatusReport(state=p.DeviceState.GATE_OPEN, homed=True, slot=3, gate=p.GateState.OPEN,
                         num_slots=6, fw="1.0.0")
    line = rep.to_line()
    assert line == "OK STATUS state=GATE_OPEN homed=1 slot=3 gate=OPEN slots=6 fw=1.0.0"
    back = p.StatusReport.parse(line)
    assert back.state is p.DeviceState.GATE_OPEN and back.homed and back.slot == 3
    assert back.gate is p.GateState.OPEN and back.num_slots == 6 and back.fw == "1.0.0"
    odd = p.StatusReport.parse("OK STATUS gate=closed slot=-1 state=moving extra=x homed=0")
    assert odd.state is p.DeviceState.MOVING and odd.slot is None and odd.homed is False
    assert odd.extra == {"extra": "x"}


def _m(line: str) -> p.Message:
    msg = p.parse_message(line)
    assert msg is not None
    return msg


def test_classify_dispense_sequence():
    cmd = p.Command.dispense_slot(3, 6)
    D = p.Disposition
    assert p.classify(cmd, _m("OK MOVING 3")) is D.PROGRESS
    assert p.classify(cmd, _m("OK AT_SLOT 3")) is D.PROGRESS
    assert p.classify(cmd, _m("OK GATE_OPEN")) is D.SUCCESS
    assert p.classify(cmd, _m("OK AT_SLOT 2")) is D.UNRELATED  # stale reply for another slot
    assert p.classify(cmd, _m("OK READY")) is D.UNRELATED
    assert p.classify(cmd, _m("ERR STOPPED")) is D.FAILURE
    assert p.classify(cmd, _m("ERR HOME_TIMEOUT")) is D.UNRELATED
    assert p.classify(cmd, _m("EVENT CONFIRM_BUTTON")) is D.UNRELATED


def test_classify_other_commands():
    D = p.Disposition
    assert p.classify(p.Command.move_slot(2, 6), _m("OK AT_SLOT 2")) is D.SUCCESS
    assert p.classify(p.Command.ping(), _m("ERR HOME_TIMEOUT")) is D.UNRELATED
    assert p.classify(p.Command.stop(), _m("ERR STOPPED")) is D.UNRELATED
    assert p.classify(p.Command.stop(), _m("OK STOPPED")) is D.SUCCESS
    assert p.classify(p.Command.home(), _m("OK HOMING")) is D.PROGRESS
    assert p.classify(p.Command.home(), _m("ERR HOME_TIMEOUT")) is D.FAILURE
    assert p.classify(p.Command.close_gate(), _m("OK GATE_CLOSED")) is D.SUCCESS
    assert p.classify(p.Command.status(), _m("OK STATUS state=READY")) is D.SUCCESS


def test_command_result_certainty():
    cmd = p.Command.dispense_slot(1, 6)
    t = p.CommandResult.host_failure(cmd, p.HostCode.TIMEOUT)
    assert not t.definitive and t.gate_may_be_open and t.hardware_result.startswith("UNCERTAIN")
    r = p.CommandResult.host_failure(cmd, p.HostCode.DEVICE_RESET)
    assert r.definitive and not r.gate_may_be_open
    n = p.CommandResult.host_failure(p.Command.home(), p.HostCode.DISCONNECTED)
    assert not n.definitive and not n.gate_may_be_open


def test_compartment_labels():
    assert p.compartment_label(0) == "compartment 1"
    assert p.compartment_label(5) == "compartment 6"


def test_conformance_file_is_well_formed():
    path = Path(p.__file__).with_name("conformance.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    names = [s["name"] for s in data["scenarios"]]
    assert len(names) == len(set(names)) >= 20
    allowed = {"boot", "send", "press", "sensor", "jam", "wait_ms", "expect", "within_ms", "quiet_ms"}
    for sc in data["scenarios"]:
        assert sc["steps"][0].get("boot"), sc["name"]
        for step in sc["steps"]:
            assert set(step) <= allowed, (sc["name"], step)
            assert "expect" in step, (sc["name"], step)


class _ScriptedTarget:
    """Tiny target: boots instantly, answers PING, emits a delayed line for SLOW."""

    name = "scripted"
    supports_faults = False
    supports_buttons = False
    supports_boot = True

    def __init__(self):
        self.out: list[str] = []
        self.t = 0
        self.timers: list[tuple[int, str]] = []

    def reset(self):
        self.out, self.t, self.timers = [], 0, []

    def boot(self, mode):
        self.out += ["EVENT BOOT x", "OK HOMING", "OK HOMED", "OK READY"]

    def send(self, line):
        if line.strip().upper() == "PING":
            self.out.append("OK PONG")
        elif line == "SLOW":
            self.out.append("OK MOVING 1")
            self.timers.append((self.t + 50, "OK AT_SLOT 1"))
        elif line == "STATUS":
            self.out.append("OK STATUS state=READY homed=1 slot=0 gate=CLOSED")

    def tick(self, ms):
        self.t += ms
        due = [l for at, l in self.timers if at <= self.t]
        self.timers = [(at, l) for at, l in self.timers if at > self.t]
        self.out += due
        out, self.out = self.out, []
        return out

    def set_button(self, name, pressed): ...
    def set_sensor(self, mode): ...
    def set_jam(self, on): ...
    def close(self): ...


def test_conformance_runner_semantics():
    from tactidose.hardware.conformance import match_expectation, run_scenario

    assert match_expectation("status:state=READY,slot=0", "OK STATUS state=READY homed=1 slot=0")
    assert not match_expectation("status:slot=1", "OK STATUS slot=0")
    assert match_expectation(r"re:EVENT BOOT \S+", "EVENT BOOT 1.0")
    t = _ScriptedTarget()
    ok = run_scenario(t, {"name": "a", "steps": [
        {"boot": "ok", "expect": ["re:EVENT BOOT \\S+", "OK HOMING", "OK HOMED", "OK READY"]},
        {"send": "SLOW", "expect": ["OK MOVING 1"]},
        {"send": "PING", "expect": ["OK PONG"]},
        {"wait_ms": 1000, "expect": ["OK AT_SLOT 1"]},
        {"send": "STATUS", "quiet_ms": 100, "expect": ["status:state=READY,gate=CLOSED"]},
        {"wait_ms": 200, "expect": []},
    ]})
    assert ok.ok, ok.describe()
    bad = run_scenario(t, {"name": "b", "steps": [
        {"boot": "ok", "expect": ["re:EVENT BOOT \\S+", "OK HOMING"]},
        {"send": "PING", "expect": ["OK PONG"]},
    ]})
    assert not bad.ok and "OK HOMED" in bad.steps[1].got  # carried-over lines are reported


# --------------------------------------------------------------------------- config


def test_settings_defaults(settings: Settings):
    assert settings.num_slots == 6 and settings.hardware_mode == "none"
    assert settings.effective_label_extractor == "fake"
    assert not settings.snowflake_configured and not settings.gemini_configured
    summary = settings.public_summary()
    assert "gemini_api_key" not in json.dumps(summary)


def test_settings_env_aliases(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "abc")
    monkeypatch.setenv("TIDB_HOST", "gateway01.example.tidbcloud.com")
    monkeypatch.setenv("TACTIDOSE_SERIAL_PORT", "")
    s = Settings(_env_file=None)
    assert s.gemini_configured and s.effective_label_extractor == "gemini"
    assert s.tidb_host == "gateway01.example.tidbcloud.com" and s.serial_port == "auto"


# --------------------------------------------------------------------------- clock


def test_clock_travel_and_localize():
    c = Clock("America/Vancouver", frozen_at=datetime(2026, 10, 5, 7, 55))
    assert c.local_now().hour == 7 and c.local_now().minute == 55
    assert c.now().tzinfo is timezone.utc
    c.travel_to(datetime(2026, 10, 5, 13, 0))
    assert c.local_now().hour == 13
    c.advance(timedelta(minutes=5))
    assert c.local_now().minute == 5
    utc = c.local_to_utc(datetime(2026, 10, 5, 8, 0))
    assert utc.hour == 15  # PDT = UTC-7
    live = Clock(None)
    live.set_offset(timedelta(hours=1))
    assert live.is_travelling and live.now() > datetime.now(timezone.utc) + timedelta(minutes=59)


# --------------------------------------------------------------------------- bus


def test_bus_publish_subscribe_and_history():
    bus = EventBus(history=3)
    sub = bus.subscribe([Topic.DEVICE_STATE, "assistant.*"])
    seen = []
    bus.add_listener(lambda ev: seen.append(ev.topic), [Topic.SPOKEN])
    bus.publish(Topic.DEVICE_STATE, {"a": 1})
    bus.publish(Topic.SPOKEN, {"text": "hi"})
    bus.publish(Topic.DOSE_UPDATED, {"x": 1})
    got = sub.drain()
    assert [e.topic for e in got] == [Topic.DEVICE_STATE, Topic.SPOKEN]
    assert seen == [Topic.SPOKEN]
    bus.publish(Topic.NOTICE, {})
    assert len(bus.recent(10)) == 3
    sub.close()


def test_bus_drops_oldest_when_full():
    bus = EventBus()
    sub = bus.subscribe(maxsize=2)
    for i in range(5):
        bus.publish("t", {"i": i})
    assert [e.data["i"] for e in sub.drain()] == [3, 4]


# --------------------------------------------------------------------------- db


def test_db_models_and_utc_round_trip(db, settings):
    ids = seed_minimal(db, settings)
    with db.session() as s:
        sched = s.get(Schedule, ids["schedule_ids"][0])
        at = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
        ev = DoseEvent(schedule_id=sched.schedule_id, medication_id=sched.medication_id,
                       user_id=ids["user_id"], device_id=ids["device_id"], scheduled_at=at,
                       status=DoseStatus.DUE.value)
        s.add(ev)
        s.flush()
        enqueue_adherence(s, ev, salt="salt")
        eid = ev.event_id
    with db.session() as s:
        ev = s.get(DoseEvent, eid)
        assert ev.scheduled_at == datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
        assert ev.scheduled_at.tzinfo is timezone.utc
        meds = s.scalars(select(Medication)).all()
        assert all(m.confirmed_by_user for m in meds)


def test_naive_datetime_rejected(db, settings):
    ids = seed_minimal(db, settings)
    with pytest.raises(Exception):
        with db.session() as s:
            s.add(DoseEvent(schedule_id=ids["schedule_ids"][0], medication_id=ids["med_ids"][0],
                            user_id=ids["user_id"], device_id=ids["device_id"],
                            scheduled_at=datetime(2026, 10, 5, 8, 0)))


def test_unique_event_per_schedule_time(db, settings):
    ids = seed_minimal(db, settings)
    at = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    kw = dict(schedule_id=ids["schedule_ids"][0], medication_id=ids["med_ids"][0],
              user_id=ids["user_id"], device_id=ids["device_id"], scheduled_at=at)
    with db.session() as s:
        s.add(DoseEvent(**kw))
    with pytest.raises(Exception):
        with db.session() as s:
            s.add(DoseEvent(**kw))


def test_adherence_payload_is_deidentified():
    ev = DoseEvent(event_id=7, schedule_id=1, medication_id=2, user_id=3, device_id="dev",
                   scheduled_at=datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc),
                   dispensed_at=datetime(2026, 10, 5, 15, 10, tzinfo=timezone.utc),
                   status=DoseStatus.DISPENSED.value, attempts=1)
    payload = adherence_payload(ev, salt="s", tz=Clock("America/Vancouver").tz)
    assert payload["event_uid"] == "dev:7" and payload["dispense_delay_minutes"] == 10.0
    assert payload["user_hash"] == pseudonymize(3, "s") and "3" != payload["user_hash"]
    assert payload["time_window"] == "morning" and payload["scheduled_local_hour"] == 8
    text = json.dumps(payload)
    assert "medication" not in text and "name" not in text
    assert time_window(23) == "night"


# --------------------------------------------------------------------------- fakes


def test_fake_hardware_interlocks():
    hw = FakeHardware()
    assert hw.dispense_slot(3).code == "GATE_OPEN"
    assert hw.move_slot(1).code == "INVALID_STATE"
    assert hw.close_gate().ok
    assert hw.stop().code == "STOPPED"
    assert hw.dispense_slot(1).code == "NOT_HOMED"
    assert hw.home().ok and hw.dispense_slot(1).ok
    assert hw.dispense_slot(9).code == "INVALID_ARGUMENT"
    hw.close_gate()
    hw.script(p.CommandName.DISPENSE_SLOT, "TIMEOUT")
    r = hw.dispense_slot(2)
    assert not r.definitive and r.gate_may_be_open
