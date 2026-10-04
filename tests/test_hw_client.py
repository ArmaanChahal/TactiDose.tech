"""HardwareClient against the real-time SimulatedDevice (sped up) and a scripted fake device."""

from __future__ import annotations

import threading
import time
from itertools import pairwise

import pytest

from tactidose.core.bus import Topic
from tactidose.hardware.protocol import DeviceState, Err, Ev, GateState, HostCode, Message
from tactidose.hardware.serial_client import HardwareClient, NullHardware, create_hardware
from tactidose.hardware.simulator import SimConfig, SimulatedDevice
from tactidose.hardware.transports import TransportError
from tests.fakes import wait_until

HW_THREADS = ("hw-supervisor", "hw-reader", "sim-device")

# --------------------------------------------------------------------------- helpers


@pytest.fixture
def rig(settings, bus):
    """``rig(**overrides)`` -> (HardwareClient, SimulatedDevice) at x50 sim speed; closed at teardown."""
    clients: list[HardwareClient] = []

    def make(*, config: SimConfig | None = None, start: bool = True, speed: float = 50.0, **overrides):
        values = {"hardware_mode": "sim", "sim_speed": speed, "hw_heartbeat_s": 60.0,
                  "hw_reconnect_max_s": 0.5, **overrides}
        s = settings.model_copy(update=values)
        sim = SimulatedDevice(s, bus=bus, config=config)
        hw = HardwareClient(s, bus=bus, mode="sim", sim_device=sim)
        clients.append(hw)
        if start:
            hw.start()
        return hw, sim

    yield make
    for hw in clients:
        hw.close()


def wait_ready(hw, timeout: float = 5.0) -> bool:
    def ready() -> bool:
        s = hw.snapshot()
        return s.connected and s.state is DeviceState.READY

    return wait_until(ready, timeout)


def tx_lines(sub) -> list[str]:
    return [e.data["line"] for e in sub.drain() if e.topic == Topic.DEVICE_LINE and e.data["dir"] == "tx"]


def in_thread(fn):
    box: dict[str, object] = {}
    th = threading.Thread(target=lambda: box.setdefault("result", fn()), daemon=True)
    th.start()
    return th, box


def lines(result) -> list[str]:
    return [m.to_line() for m in result.messages]


class ScriptedDevice:
    """Fake transport: canned replies preceded by boot-banner noise, delivered 7 bytes per read."""

    def __init__(self, name: str = "fake://noisy") -> None:
        self.name = name
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self.fail_writes = False
        self.written: list[str] = []
        self.replies: dict[str, list[str]] = {
            "PING": ["OK PONG"],
            "STATUS": ["OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=6 fw=fake-1.0"],
            # a stale reply for another slot first: must not count for DISPENSE_SLOT 3
            "DISPENSE_SLOT 3": ["OK AT_SLOT 2", "OK MOVING 3", "OK AT_SLOT 3", "OK GATE_OPEN"],
        }
        self.noise = (b"ets Jun  8 2016 00:22:57\r\n", b"rst:0x1 (POWERON_RESET),boot:0x13\r\n",
                      b"#debug speed=1600\r\n", b"\x00\xff\xfe garbage\r\n", b"OK\r\n", b"ERR\r\n",
                      b"OKAY then\r\n", b"EVENTUALLY\r\n")

    @property
    def is_open(self) -> bool:
        return not self._closed

    def read(self, size: int = 1024) -> bytes:
        with self._cond:
            if not self._buf and not self._closed:
                self._cond.wait(0.05)
            if self._closed:
                raise TransportError("closed")
            data = bytes(self._buf[: min(size, 7)])
            del self._buf[: len(data)]
            return data

    def write(self, data: bytes) -> int:
        if self._closed:
            raise TransportError("closed")
        if self.fail_writes:
            raise TransportError("simulated write failure")
        line = data.decode("ascii").strip()
        self.written.append(line)
        replies = self.replies.get(line, ["ERR UNKNOWN_COMMAND"])
        self.emit(b"".join(self.noise) + b"".join(f"{r}\r\n".encode() for r in replies))
        return len(data)

    def emit(self, raw: bytes) -> None:
        with self._cond:
            self._buf += raw
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# --------------------------------------------------------------------------- handshake / snapshot


def test_handshake_sees_boot_banner_and_fills_snapshot(rig, bus):
    sub = bus.subscribe([Topic.DEVICE_LINE, Topic.DEVICE_STATE])
    hw, sim = rig()
    assert wait_ready(hw)
    snap = hw.snapshot()
    assert snap.mode == "sim" and snap.port == "sim://" and snap.connected and snap.responsive
    assert snap.state is DeviceState.READY and snap.homed is True and snap.slot == 0
    assert snap.gate is GateState.CLOSED and snap.fw_version == "sim-1.1.0"
    assert snap.proto == "1.1" and snap.drop_sensor is True and snap.to_dict()["proto"] == "1.1"
    assert snap.num_slots_reported == 6 and snap.resets_seen == 1 and snap.in_flight is None
    assert snap.last_error is None and snap.ready_for_motion and snap.last_rx_age_s is not None
    events = sub.drain()
    seen = [(e.data["dir"], e.data["line"]) for e in events if e.topic == Topic.DEVICE_LINE]
    assert seen[0] == ("rx", "EVENT BOOT sim-1.1.0")
    assert ("tx", "PING") in seen and ("tx", "STATUS") in seen and ("tx", "HOME") not in seen
    states = [e.data for e in events if e.topic == Topic.DEVICE_STATE]
    assert states[-1]["state"] == "READY" and states[-1]["ready_for_motion"] is True
    assert {t.name for t in threading.enumerate()} >= set(HW_THREADS)


def test_auto_home_after_connect_when_device_is_not_homed(rig, bus):
    hw, sim = rig(start=False)
    sim.start()
    link = sim.open_transport()
    assert wait_until(lambda: sim.physical()["state"] == "READY", 3)
    link.write(b"STOP\n")
    assert wait_until(lambda: sim.physical()["state"] == "SAFE_STOP", 3)
    link.close()
    sub = bus.subscribe([Topic.DEVICE_LINE])
    hw.start()
    assert wait_ready(hw)
    assert tx_lines(sub)[:3] == ["PING", "STATUS", "HOME"]
    assert hw.snapshot().homed is True and hw.snapshot().resets_seen == 0


def test_no_auto_home_when_disabled(rig, bus):
    hw, sim = rig(start=False, hw_auto_home=False)
    sim.start()
    link = sim.open_transport()
    assert wait_until(lambda: sim.physical()["state"] == "READY", 3)
    link.write(b"STOP\n")
    assert wait_until(lambda: sim.physical()["state"] == "SAFE_STOP", 3)
    link.close()
    sub = bus.subscribe([Topic.DEVICE_LINE])
    hw.start()
    assert wait_until(lambda: hw.snapshot().connected, 3)
    time.sleep(0.2)
    assert hw.snapshot().state is DeviceState.SAFE_STOP and "HOME" not in tx_lines(sub)


def test_fault_at_connect_is_reported_and_not_auto_homed(rig, bus):
    hw, sim = rig(start=False, config=SimConfig(home_sensor="dead"))
    sim.start()
    assert wait_until(lambda: sim.physical()["state"] == "FAULT", 5)
    notices = bus.subscribe([Topic.NOTICE])
    lines_sub = bus.subscribe([Topic.DEVICE_LINE])
    hw.start()
    assert wait_until(lambda: hw.snapshot().connected, 3)
    time.sleep(0.2)
    snap = hw.snapshot()
    assert snap.state is DeviceState.FAULT and snap.homed is False and not snap.ready_for_motion
    assert "HOME" not in tx_lines(lines_sub)
    assert any(e.data["level"] == "error" and "fault" in e.data["message"] for e in notices.drain())


def test_unsolicited_fault_reaches_listeners(rig, bus):
    hw, sim = rig(start=False, config=SimConfig(home_sensor="dead"))
    got: list[Message] = []
    hw.add_event_listener(got.append)
    notices = bus.subscribe([Topic.NOTICE])
    hw.start()
    assert wait_until(lambda: any(m.is_err(Err.HOME_TIMEOUT) for m in got), 5)
    assert got[0].is_event(Ev.BOOT)
    assert hw.snapshot().state is DeviceState.FAULT and hw.snapshot().last_error == "HOME_TIMEOUT"
    assert any("HOME_TIMEOUT" in e.data["message"] for e in notices.drain())


def test_slot_count_mismatch_publishes_warning(rig, bus):
    notices = bus.subscribe([Topic.NOTICE])
    hw, _ = rig(config=SimConfig(num_slots=8))
    assert wait_ready(hw)
    warnings = [e.data["message"] for e in notices.drain() if e.data["level"] == "warning"]
    assert any("8 compartments" in m for m in warnings)
    assert hw.snapshot().num_slots_reported == 8


# --------------------------------------------------------------------------- commands


def test_ping_and_status(rig):
    hw, _ = rig()
    assert wait_ready(hw)
    r = hw.ping()
    assert r.ok and r.code == "PONG" and r.definitive and lines(r) == ["OK PONG"]
    r = hw.status()
    assert r.ok and r.code == "STATUS"
    assert lines(r) == [
        "OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=6 fw=sim-1.1.0 proto=1.1 drop_sensor=1"
    ]


def test_dispense_success_then_close_gate(rig):
    hw, sim = rig()
    assert wait_ready(hw)
    r = hw.dispense_slot(3)
    assert r.ok and r.code == "GATE_OPEN" and r.definitive and not r.gate_may_be_open
    assert lines(r) == ["OK MOVING 3", "OK AT_SLOT 3", "OK GATE_OPEN"]
    assert r.hardware_result == "OK GATE_OPEN"
    snap = hw.snapshot()
    assert snap.state is DeviceState.GATE_OPEN and snap.gate is GateState.OPEN and snap.slot == 3
    phys = sim.physical()
    assert phys["slot"] == 3 and phys["gate_open"] is True and phys["angle_deg"] == 180.0
    r = hw.close_gate()
    assert r.ok and r.code == "GATE_CLOSED"
    assert wait_ready(hw) and hw.snapshot().slot == 3 and not sim.physical()["gate_open"]


def test_device_interlock_refusals_are_definitive(rig):
    hw, _ = rig()
    assert wait_ready(hw)
    assert hw.open_gate().code == "GATE_OPEN"
    for result in (hw.move_slot(1), hw.dispense_slot(1), hw.home()):
        assert not result.ok and result.code == Err.INVALID_STATE.value and result.definitive
        assert not result.gate_may_be_open and result.hardware_result == "ERR INVALID_STATE"
    stop = hw.stop()
    assert stop.ok and lines(stop) == ["OK STOPPED"]                # OK GATE_CLOSED is unrelated to STOP
    assert hw.snapshot().state is DeviceState.SAFE_STOP and hw.snapshot().gate is GateState.CLOSED
    assert hw.dispense_slot(2).code == "NOT_HOMED"
    assert hw.open_gate().code == "NOT_HOMED"
    assert hw.home().code == "HOMED" and wait_ready(hw)


def test_invalid_slots_are_refused_locally_and_never_sent(rig, bus):
    hw, _ = rig()
    assert wait_ready(hw)
    sub = bus.subscribe([Topic.DEVICE_LINE])
    for result in (hw.dispense_slot(6), hw.move_slot(-1), hw.move_slot(True), hw.dispense_slot("2")):  # type: ignore[arg-type]
        assert result.code == HostCode.INVALID_ARGUMENT.value and result.definitive and not result.ok
    assert hw.dispense_slot(6).command.to_line() == "DISPENSE_SLOT 6"
    assert tx_lines(sub) == []


def test_send_raw_validates_before_sending(rig, bus):
    hw, _ = rig()
    assert wait_ready(hw)
    sub = bus.subscribe([Topic.DEVICE_LINE])
    for bad in ("MOVE_SLOT 9", "FOO", "", "PING extra", "DISPENSE_SLOT x", "X" * 80):
        r = hw.send_raw(bad)
        assert r.code == HostCode.INVALID_ARGUMENT.value and not r.ok and "not sent" in r.detail, bad
    assert tx_lines(sub) == []
    assert hw.send_raw("  ping ").code == "PONG"
    assert hw.send_raw("move_slot 002").code == "AT_SLOT"
    assert hw.send_raw("stop").code == "STOPPED"
    assert tx_lines(sub) == ["PING", "MOVE_SLOT 2", "STOP"]


def test_exchange_raw_reaches_the_firmware_parser(rig):
    hw, _ = rig()
    assert wait_ready(hw)
    reply = hw.exchange_raw("FOO")
    assert reply is not None and reply.is_err(Err.UNKNOWN_COMMAND)
    assert hw.exchange_raw("PING").to_line() == "OK PONG"


def test_concurrent_commands_get_busy_local(rig):
    hw, _ = rig(speed=5)
    assert wait_ready(hw)
    th, box = in_thread(lambda: hw.move_slot(3))
    assert wait_until(lambda: hw.snapshot().in_flight == "MOVE_SLOT 3", 2)
    busy = hw.ping()
    assert busy.code == HostCode.BUSY_LOCAL.value and busy.definitive and "MOVE_SLOT 3" in busy.detail
    assert hw.dispense_slot(1).code == HostCode.BUSY_LOCAL.value
    th.join(5)
    assert box["result"].code == "AT_SLOT"
    assert hw.ping().ok


def test_stop_from_another_thread_interrupts_dispense(rig):
    hw, sim = rig(speed=10)
    assert wait_ready(hw)
    th, box = in_thread(lambda: hw.dispense_slot(3))
    assert wait_until(lambda: hw.snapshot().state is DeviceState.MOVING, 2)
    assert hw.snapshot().in_flight == "DISPENSE_SLOT 3"
    stop = hw.stop()
    th.join(5)
    assert stop.ok and stop.code == "STOPPED"
    dispensed = box["result"]
    assert not dispensed.ok and dispensed.code == Err.STOPPED.value and dispensed.definitive
    assert not dispensed.gate_may_be_open and lines(dispensed) == ["OK MOVING 3", "ERR STOPPED"]
    snap = hw.snapshot()
    assert snap.state is DeviceState.SAFE_STOP and snap.homed is False and snap.in_flight is None
    time.sleep(0.1)
    assert not sim.physical()["gate_open"]


def test_cancel_button_during_dispense(rig):
    hw, sim = rig(speed=10)
    assert wait_ready(hw)
    got: list[Message] = []
    hw.add_event_listener(got.append)
    th, box = in_thread(lambda: hw.dispense_slot(2))
    assert wait_until(lambda: hw.snapshot().state is DeviceState.MOVING, 2)
    sim.press("CANCEL")
    th.join(5)
    assert box["result"].code == Err.STOPPED.value
    assert [m.to_line() for m in got] == ["EVENT CANCEL_BUTTON"]
    assert hw.snapshot().state is DeviceState.SAFE_STOP


def test_event_listeners_and_device_event_topic(rig, bus):
    hw, sim = rig()
    assert wait_ready(hw)
    got: list[Message] = []
    broken_calls: list[Message] = []

    def broken(msg: Message) -> None:
        broken_calls.append(msg)
        raise RuntimeError("listener bug")

    hw.add_event_listener(broken)
    unsubscribe = hw.add_event_listener(got.append)
    events = bus.subscribe([Topic.DEVICE_EVENT])
    sim.press("CONFIRM")
    assert wait_until(lambda: any(m.is_event(Ev.CONFIRM_BUTTON) for m in got), 2)
    assert broken_calls and hw.snapshot().state is DeviceState.READY
    assert [e.data for e in events.drain()] == [{"code": "CONFIRM_BUTTON", "line": "EVENT CONFIRM_BUTTON"}]
    unsubscribe()
    unsubscribe()
    sim.press("CANCEL")
    assert wait_until(lambda: len(broken_calls) == 2, 2)
    assert len(got) == 1


# --------------------------------------------------------------------------- failures


def test_unresponsive_device_gives_uncertain_timeout_then_fails_closed(rig, bus):
    hw, sim = rig(timeout_dispense_s=0.4, timeout_ping_s=0.3, timeout_status_s=0.3)
    assert wait_ready(hw)
    notices = bus.subscribe([Topic.NOTICE])
    sim.set_fault("unresponsive", True)
    r = hw.dispense_slot(2)
    assert r.code == HostCode.TIMEOUT.value and not r.ok
    assert not r.definitive and r.gate_may_be_open and r.hardware_result.startswith("UNCERTAIN TIMEOUT")
    assert not sim.physical()["gate_open"]                          # the bytes were dropped
    sub = bus.subscribe([Topic.DEVICE_LINE])
    r = hw.close_gate()
    assert r.code == HostCode.NOT_CONNECTED.value and r.definitive and "resync" in r.detail
    assert tx_lines(sub) == ["STATUS"]                              # CLOSE_GATE itself never sent
    snap = hw.snapshot()
    assert snap.connected and not snap.responsive and snap.last_error == HostCode.TIMEOUT.value
    assert any(e.data["level"] == "warning" for e in notices.drain())
    sim.set_fault("unresponsive", False)
    r = hw.close_gate()
    assert r.ok and tx_lines(sub) == ["STATUS", "CLOSE_GATE"]
    assert hw.snapshot().responsive


def test_disconnect_mid_dispense_is_uncertain_and_reconnects(rig):
    hw, sim = rig(speed=10)
    assert wait_ready(hw)
    th, box = in_thread(lambda: hw.dispense_slot(3))
    assert wait_until(lambda: hw.snapshot().state is DeviceState.MOVING, 2)
    sim.set_fault("disconnect", True)
    th.join(5)
    r = box["result"]
    assert r.code == HostCode.DISCONNECTED.value and not r.definitive and r.gate_may_be_open
    snap = hw.snapshot()
    assert not snap.connected and snap.state is DeviceState.UNKNOWN and snap.gate is GateState.UNKNOWN
    after = hw.dispense_slot(1)
    assert after.code == HostCode.NOT_CONNECTED.value and after.definitive and not after.gate_may_be_open
    assert wait_until(lambda: sim.physical()["gate_open"], 3)     # the device carried on alone
    sim.set_fault("disconnect", False)
    assert wait_until(lambda: hw.snapshot().connected, 5)
    snap = hw.snapshot()
    assert snap.state is DeviceState.GATE_OPEN and snap.gate is GateState.OPEN and snap.slot == 3


def test_brownout_at_gate_gives_device_reset(rig, bus):
    hw, sim = rig()
    assert wait_ready(hw)
    notices = bus.subscribe([Topic.NOTICE])
    sim.set_fault("brownout_on_gate", True)
    r = hw.dispense_slot(1)
    assert r.code == HostCode.DEVICE_RESET.value and not r.ok
    assert r.definitive and not r.gate_may_be_open
    assert lines(r) == ["OK MOVING 1", "OK AT_SLOT 1"]
    assert wait_ready(hw)
    snap = hw.snapshot()
    assert snap.resets_seen == 2 and snap.slot == 0 and snap.homed is True
    assert not sim.physical()["gate_open"]
    assert any("restarted" in e.data["message"] for e in notices.drain())


def test_heartbeat_tracks_responsiveness(rig):
    hw, sim = rig(hw_heartbeat_s=0.5, timeout_ping_s=0.2, timeout_status_s=0.2)
    assert wait_ready(hw)
    sim.set_fault("unresponsive", True)
    assert wait_until(lambda: not hw.snapshot().responsive, 5)
    assert hw.snapshot().connected
    sim.set_fault("unresponsive", False)
    assert wait_until(lambda: hw.snapshot().responsive, 5)


def test_noise_and_garbage_lines_are_ignored(settings, bus):
    dev = ScriptedDevice()
    hw = HardwareClient(settings.model_copy(update={"hw_heartbeat_s": 60.0}), bus=bus,
                        transport_factory=lambda: dev, mode="serial")
    sub = bus.subscribe([Topic.DEVICE_LINE])
    hw.start()
    try:
        assert wait_until(lambda: hw.snapshot().connected, 3)
        dev.emit(b"\xff" * 2000)                                     # no newline: flushed as noise
        dev.emit(b"\r\nOK BOGUS_CODE 1\r\nEVENT\r\n\r\n")
        r = hw.ping()
        assert r.ok and lines(r) == ["OK PONG"]
        r = hw.dispense_slot(3)
        assert r.ok and lines(r) == ["OK MOVING 3", "OK AT_SLOT 3", "OK GATE_OPEN"]
        snap = hw.snapshot()
        assert snap.port == "fake://noisy" and snap.state is DeviceState.GATE_OPEN and snap.slot == 3
        assert snap.last_error is None and snap.fw_version == "fake-1.0"
        rx = [e.data["line"] for e in sub.drain() if e.data["dir"] == "rx"]
        assert "ets Jun  8 2016 00:22:57" in rx and "#debug speed=1600" in rx
        assert dev.written[:2] == ["PING", "STATUS"]
    finally:
        hw.close()


def test_failed_write_is_treated_as_uncertain(settings):
    devices: list[ScriptedDevice] = []

    def factory() -> ScriptedDevice:
        devices.append(ScriptedDevice(f"fake://{len(devices)}"))
        return devices[-1]

    hw = HardwareClient(settings.model_copy(update={"hw_reconnect_max_s": 0.5}),
                        transport_factory=factory, mode="serial")
    hw.start()
    try:
        assert wait_until(lambda: hw.snapshot().connected, 3)
        devices[0].fail_writes = True
        r = hw.dispense_slot(3)
        assert r.code == HostCode.DISCONNECTED.value and not r.definitive and r.gate_may_be_open
        assert wait_until(lambda: len(devices) == 2 and hw.snapshot().connected, 5)
        assert hw.snapshot().port == "fake://1"
    finally:
        hw.close()


def test_start_never_raises_and_retries_with_backoff(settings, bus):
    s = settings.model_copy(update={"hardware_mode": "sim", "sim_speed": 50.0, "hw_reconnect_max_s": 0.5})
    sim = SimulatedDevice(s)
    attempts: list[float] = []

    def factory():
        attempts.append(time.monotonic())
        if len(attempts) < 3:
            raise TransportError("port is busy")
        return sim.open_transport()

    hw = HardwareClient(s, bus=bus, transport_factory=factory, mode="sim", sim_device=sim)
    try:
        hw.start()
        hw.start()
        assert hw.ping().code == HostCode.NOT_CONNECTED.value
        assert wait_ready(hw, 5)
        gaps = [b - a for a, b in pairwise(attempts)]
        assert len(attempts) == 3 and all(g >= 0.4 for g in gaps)
        assert sim.started
    finally:
        hw.close()
    assert not any(t.name == "sim-device" and t.is_alive() for t in threading.enumerate())


def test_reconnect_on_request(rig):
    hw, _ = rig()
    assert wait_ready(hw)
    assert hw.reconnect() is True
    assert wait_until(lambda: hw.snapshot().connected, 3)
    assert hw.snapshot().resets_seen == 1 and hw.ping().ok


def test_reconnect_refused_while_command_in_flight(rig):
    hw, _ = rig(speed=5)
    assert wait_ready(hw)
    th, box = in_thread(lambda: hw.move_slot(3))
    assert wait_until(lambda: hw.snapshot().in_flight == "MOVE_SLOT 3", 2)
    assert hw.reconnect() is False
    th.join(5)
    assert box["result"].ok


def test_close_is_idempotent_and_joins_threads(rig):
    hw, sim = rig()
    assert wait_ready(hw)
    hw.close()
    hw.close()
    assert [t.name for t in threading.enumerate() if t.name in HW_THREADS and t.is_alive()] == []
    assert hw.ping().code == HostCode.NOT_CONNECTED.value
    assert hw.stop().code == HostCode.NOT_CONNECTED.value
    assert not hw.snapshot().connected


def test_unstarted_client_is_not_connected(settings):
    hw = HardwareClient(settings, transport_factory=ScriptedDevice, mode="serial")
    assert hw.stop().code == HostCode.NOT_CONNECTED.value
    assert hw.dispense_slot(1).code == HostCode.NOT_CONNECTED.value
    hw.close()


# --------------------------------------------------------------------------- factory / null


def test_clients_implement_the_hardware_controller_protocol(settings):
    from tactidose.core.interfaces import HardwareController

    assert isinstance(NullHardware(settings), HardwareController)
    assert isinstance(HardwareClient(settings, transport_factory=ScriptedDevice), HardwareController)


def test_null_hardware_fails_closed(settings, bus):
    hw = NullHardware(settings, bus=bus)
    hw.start()
    snap = hw.snapshot()
    assert snap.mode == "none" and not snap.connected and not snap.ready_for_motion
    for r in (hw.ping(), hw.status(), hw.home(), hw.move_slot(1), hw.dispense_slot(1),
              hw.open_gate(), hw.close_gate(), hw.stop(), hw.send_raw("PING")):
        assert r.code == HostCode.NOT_CONNECTED.value and r.definitive and not r.gate_may_be_open
    assert hw.send_raw("FOO").code == HostCode.INVALID_ARGUMENT.value
    assert hw.dispense_slot(9).code == HostCode.INVALID_ARGUMENT.value
    hw.add_event_listener(lambda m: None)()
    assert hw.reconnect() is False
    hw.close()


def test_create_hardware_modes(settings, bus, monkeypatch):
    hw, sim = create_hardware(settings, bus=bus)
    assert isinstance(hw, NullHardware) and sim is None

    sim_settings = settings.model_copy(update={"hardware_mode": "sim", "sim_speed": 50.0})
    hw, sim = create_hardware(sim_settings, bus=bus)
    assert isinstance(hw, HardwareClient) and isinstance(sim, SimulatedDevice)
    assert not sim.started                                         # the client starts it
    hw.start()
    try:
        assert wait_ready(hw) and sim.started
    finally:
        hw.close()
    with pytest.raises(TransportError):
        sim.open_transport()                                       # closed together with the client

    monkeypatch.setattr("tactidose.hardware.serial_client.resolve_port", lambda value: None)
    serial_settings = settings.model_copy(update={"hardware_mode": "serial", "hw_reconnect_max_s": 0.5})
    hw, sim = create_hardware(serial_settings, bus=bus)
    assert isinstance(hw, HardwareClient) and sim is None
    hw.start()
    try:
        time.sleep(0.2)
        snap = hw.snapshot()
        assert snap.mode == "serial" and snap.port == "auto" and not snap.connected
        assert hw.dispense_slot(1).code == HostCode.NOT_CONNECTED.value
    finally:
        hw.close()
