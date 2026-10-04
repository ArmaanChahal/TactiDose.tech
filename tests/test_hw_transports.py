"""Transports: PySerialTransport (loop://, socket://, DTR/RTS) and the TCP simulator server."""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Iterator

import pytest
import serial

from tactidose.hardware.protocol import DeviceState
from tactidose.hardware.serial_client import HardwareClient
from tactidose.hardware.simulator import SimulatedDevice
from tactidose.hardware.transports import PySerialTransport, TransportError, serve_simulator_tcp
from tests.fakes import wait_until

# --------------------------------------------------------------------------- helpers


def read_until(transport, marker: bytes, timeout: float = 3.0) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while marker not in buf and time.monotonic() < deadline:
        buf += transport.read(256)
    assert marker in buf, f"{marker!r} not received; got {buf!r}"
    return buf


@contextlib.contextmanager
def running_tcp_simulator(settings, device: SimulatedDevice | None = None) -> Iterator[str]:
    """Serve ``device`` (or a server-owned one) on an ephemeral localhost port; yields the URL."""
    stop = threading.Event()
    ready = threading.Event()
    bound: dict[str, int] = {}

    def on_ready(host: str, port: int) -> None:
        bound["port"] = port
        ready.set()

    thread = threading.Thread(
        target=serve_simulator_tcp, args=(settings,), name="test-sim-tcp", daemon=True,
        kwargs={"host": "127.0.0.1", "port": 0, "stop_event": stop, "device": device, "on_ready": on_ready},
    )
    thread.start()
    assert ready.wait(5), "simulator server did not start"
    try:
        yield f"socket://127.0.0.1:{bound['port']}"
    finally:
        stop.set()
        thread.join(5)
        assert not thread.is_alive(), "simulator server did not stop"


@pytest.fixture
def fast(settings):
    return settings.model_copy(update={"sim_speed": 50.0, "hw_reconnect_max_s": 0.5, "hw_heartbeat_s": 60.0})


# --------------------------------------------------------------------------- PySerialTransport


def test_loop_url_round_trip_and_close():
    t = PySerialTransport("loop://", 115200)
    try:
        assert t.is_open and t.name == "loop://"
        assert t.read() == b""                                     # read timeout -> b""
        assert t.write(b"PING\n") == 5
        assert read_until(t, b"\n") == b"PING\n"
        t.pulse_reset(hold_s=0.0)                                  # no-op on loop://
    finally:
        t.close()
        t.close()
    assert not t.is_open
    with pytest.raises(TransportError):
        t.read()
    with pytest.raises(TransportError):
        t.write(b"PING\n")


def test_open_failures_become_transport_errors():
    with pytest.raises(TransportError):
        PySerialTransport("nosuchscheme://device")
    with pytest.raises(TransportError):
        PySerialTransport("socket://127.0.0.1:notaport")


def test_dtr_and_rts_are_deasserted_before_open(monkeypatch):
    calls: list[tuple] = []

    class FakeSerial:
        def __init__(self) -> None:
            object.__setattr__(self, "is_open", False)

        def __setattr__(self, name, value) -> None:
            calls.append(("set", name, value))
            object.__setattr__(self, name, value)

        def open(self) -> None:
            calls.append(("open",))
            object.__setattr__(self, "is_open", True)

        def close(self) -> None:
            object.__setattr__(self, "is_open", False)

    def fake_serial_for_url(url, *args, **kwargs):
        calls.append(("for_url", url, args, kwargs))
        return FakeSerial()

    monkeypatch.setattr(serial, "serial_for_url", fake_serial_for_url)
    t = PySerialTransport("COM42", 57600)
    assert calls[0] == ("for_url", "COM42", (), {"do_not_open": True})
    before_open = calls[: calls.index(("open",))]
    assert ("set", "dtr", False) in before_open and ("set", "rts", False) in before_open
    assert ("set", "baudrate", 57600) in before_open
    assert ("set", "timeout", 0.05) in before_open
    t.close()


def test_from_settings_uses_configured_port_and_baud(settings):
    s = settings.model_copy(update={"serial_port": "loop://", "serial_baud": 9600})
    t = PySerialTransport.from_settings(s)
    try:
        assert t.name == "loop://" and t.baud == 9600
    finally:
        t.close()


# --------------------------------------------------------------------------- TCP simulator


def test_tcp_simulator_speaks_the_protocol(fast):
    dev = SimulatedDevice(fast)
    try:
        with running_tcp_simulator(fast, dev) as url:
            t = PySerialTransport(url)
            try:
                t.write(b"PING\n")
                assert b"OK PONG\r\n" in read_until(t, b"OK PONG\r\n")
            finally:
                t.close()
            # the device keeps running between clients; a second client is served after the first
            t2 = PySerialTransport(url)
            try:
                t2.write(b"ping\r\n")
                read_until(t2, b"OK PONG\r\n")
            finally:
                t2.close()
        assert dev.started
    finally:
        dev.close()


def test_tcp_simulator_owns_its_device_when_none_given(fast):
    with running_tcp_simulator(fast) as url:
        t = PySerialTransport(url)
        try:
            t.write(b"STATUS\n")
            assert b"OK STATUS" in read_until(t, b"\r\n")
        finally:
            t.close()
    assert not any(th.name == "sim-device" and th.is_alive() for th in threading.enumerate())


def test_hardware_client_over_real_pyserial_socket_path(fast):
    dev = SimulatedDevice(fast)
    try:
        with running_tcp_simulator(fast, dev) as url:
            s = fast.model_copy(update={"hardware_mode": "serial", "serial_port": url})
            hw = HardwareClient(s, mode="serial")              # default factory: resolve_port + pyserial
            hw.start()
            try:
                assert wait_until(lambda: (s := hw.snapshot()).connected and s.state is DeviceState.READY, 8)
                assert hw.snapshot().port == url
                r = hw.dispense_slot(2)
                assert r.ok and r.code == "GATE_OPEN"
                assert hw.close_gate().ok
                # simulated USB unplug: the TCP link drops, reconnects are refused until cleared
                dev.set_fault("disconnect", True)
                assert wait_until(lambda: not hw.snapshot().connected, 3)
                assert hw.ping().code == "NOT_CONNECTED"
                dev.set_fault("disconnect", False)
                assert wait_until(lambda: hw.snapshot().connected, 5)
                assert hw.ping().ok and hw.snapshot().slot == 2
            finally:
                hw.close()
    finally:
        dev.close()


def test_client_notices_when_the_server_goes_away(fast):
    dev = SimulatedDevice(fast)
    try:
        with running_tcp_simulator(fast, dev) as url:
            hw = HardwareClient(fast.model_copy(update={"serial_port": url}), mode="serial")
            hw.start()
            assert wait_until(lambda: hw.snapshot().connected, 5)
        assert wait_until(lambda: not hw.snapshot().connected, 3)
        assert hw.dispense_slot(1).code == "NOT_CONNECTED"
        hw.close()
    finally:
        dev.close()
