"""VirtualESP32 (deterministic firmware twin) and SimulatedDevice (real-time + faults)."""

from __future__ import annotations

import threading
import time
from typing import Callable

import pytest

from tactidose.core.bus import EventBus, Topic
from tactidose.hardware.simulator import (
    FAULT_NAMES,
    SimConfig,
    SimulatedDevice,
    VirtualESP32,
    _Profile,
)
from tactidose.hardware.transports import TransportError
from tests.fakes import wait_until

# --------------------------------------------------------------------------- helpers


def run_until_lines(esp: VirtualESP32, line: str, limit_ms: int = 60000) -> tuple[int, list[str]]:
    """Tick 1 ms at a time until ``line`` is emitted; return (sim time, lines of that tick)."""
    seen: list[str] = []
    for _ in range(limit_ms):
        esp.tick(1)
        out = esp.drain_output()
        seen += out
        if line in out:
            return esp.time_ms, out
    raise AssertionError(f"{line!r} not emitted within {limit_ms} ms; got {seen}")


def run_until(esp: VirtualESP32, line: str, limit_ms: int = 60000) -> int:
    return run_until_lines(esp, line, limit_ms)[0]


def homed_esp(config: SimConfig | None = None) -> VirtualESP32:
    esp = VirtualESP32(config)
    esp.boot()
    run_until(esp, "OK READY")
    esp.drain_output()
    return esp


def send(esp: VirtualESP32, line: str, ms: int = 1) -> list[str]:
    esp.feed_line(line)
    esp.tick(ms)
    return esp.drain_output()


def read_lines(transport, until: Callable[[list[str]], bool], timeout: float = 3.0) -> list[str]:
    buf = b""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf += transport.read(256)
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            assert raw.endswith(b"\r"), "device lines must end with CRLF"
            lines.append(raw[:-1].decode("ascii"))
        if until(lines):
            return lines
    raise AssertionError(f"condition not met within {timeout} s; got {lines}")


def has(*wanted: str) -> Callable[[list[str]], bool]:
    return lambda lines: all(w in lines for w in wanted)


@pytest.fixture
def make_device(settings):
    devices: list[SimulatedDevice] = []

    def make(speed: float = 50.0, config: SimConfig | None = None, bus: EventBus | None = None,
             start: bool = True) -> SimulatedDevice:
        dev = SimulatedDevice(settings.model_copy(update={"sim_speed": speed}), bus=bus, config=config)
        devices.append(dev)
        if start:
            dev.start()
        return dev

    yield make
    for dev in devices:
        dev.close()


# --------------------------------------------------------------------------- VirtualESP32


def test_boot_homes_from_initial_offset_and_aligns_exactly():
    esp = VirtualESP32()
    assert esp.physical()["angle_deg"] == 180.0 and not esp.booted
    esp.boot()
    assert esp.drain_output() == ["EVENT BOOT sim-1.0.0", "OK HOMING"]
    homed_at = run_until(esp, "OK HOMED")
    # 1600 steps at 400 steps/s to the sensor edge, plus debounce and return-to-edge.
    assert 4000 <= homed_at <= 4020
    phys = esp.physical()
    assert phys["state"] == "READY" and phys["homed"] is True
    assert phys["slot"] == 0 and phys["angle_deg"] == 0.0 and phys["physical_steps"] == 0
    assert phys["firmware_position"] == 0 and phys["gate_open"] is False and phys["sensor_active"]
    assert send(esp, "STATUS") == ["OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=6 fw=sim-1.0.0"]


def test_slot_targets_are_absolute_and_moves_take_the_shortest_path():
    esp = homed_esp()
    assert esp.slot_targets == (0, 533, 1067, 1600, 2133, 2667)
    out = send(esp, "MOVE_SLOT 5")
    assert out == ["OK MOVING 5"]
    esp.tick(100)
    assert esp.physical()["firmware_position"] < 0          # went backwards through home
    _, out = run_until_lines(esp, "OK AT_SLOT 5")
    assert out == ["OK AT_SLOT 5", "OK READY"]
    phys = esp.physical()
    assert phys["slot"] == 5 and phys["physical_steps"] == 2667 and phys["angle_deg"] == 300.04
    # every slot reachable, physically aligned with the firmware's belief
    for slot in (2, 4, 1, 3, 0):
        esp.feed_line(f"MOVE_SLOT {slot}")
        run_until(esp, f"OK AT_SLOT {slot}")
        assert esp.physical()["slot"] == slot
        assert esp.physical()["physical_steps"] == esp.slot_targets[slot]


def test_motion_profile_trapezoid_and_triangle():
    trap = _Profile.plan(1600, 1600, 3200)
    assert trap.total_s == pytest.approx(1.5)
    tri = _Profile.plan(533, 1600, 3200)
    assert tri.t_flat == 0 and tri.total_s == pytest.approx(2 * (533 / 3200) ** 0.5)
    samples = [trap.steps_at(t / 1000) for t in range(0, 1600)]
    assert samples == sorted(samples) and samples[0] == 0 and samples[-1] == 1600
    assert _Profile.plan(0, 1600, 3200).total_s == 0


def test_move_duration_follows_the_profile():
    esp = homed_esp()
    esp.feed_line("MOVE_SLOT 3")
    esp.tick(1)
    started = esp.time_ms
    assert esp.drain_output() == ["OK MOVING 3"]
    arrived = run_until(esp, "OK AT_SLOT 3")
    assert arrived - started == 1500


def test_jam_loses_steps_and_faults_after_motion_timeout():
    esp = homed_esp()
    esp.set_jam(True)
    esp.feed_line("MOVE_SLOT 3")
    esp.tick(1)
    started = esp.time_ms
    assert esp.drain_output() == ["OK MOVING 3"]
    esp.tick(2000)
    phys = esp.physical()
    assert phys["jammed"] and phys["physical_steps"] == 0          # carousel did not move
    assert phys["firmware_position"] == 1600                        # ... but the counter did
    assert phys["state"] == "MOVING"                                # never acknowledged
    faulted = run_until(esp, "ERR MOTOR_FAULT")
    assert faulted - started == 2 * 1500 + 2000 + 1
    assert send(esp, "STATUS") == ["OK STATUS state=FAULT homed=0 slot=-1 gate=CLOSED slots=6 fw=sim-1.0.0"]
    assert send(esp, "DISPENSE_SLOT 1") == ["ERR NOT_HOMED"]


def test_dead_sensor_aborts_homing_after_one_and_a_quarter_revolutions():
    esp = VirtualESP32()
    esp.boot("dead")
    assert esp.drain_output() == ["EVENT BOOT sim-1.0.0", "OK HOMING"]
    t = run_until(esp, "ERR HOME_TIMEOUT")
    assert 10000 <= t <= 10010                                     # 4000 steps at 400 steps/s
    phys = esp.physical()
    assert phys["state"] == "FAULT" and phys["sensor_mode"] == "dead"
    assert phys["physical_steps"] == (-1600 + 4001) % 3200


def test_home_timeout_ms_limit():
    esp = VirtualESP32(SimConfig(home_timeout_ms=3000, home_sensor="dead"))
    esp.boot()
    assert 3000 <= run_until(esp, "ERR HOME_TIMEOUT") <= 3002


def test_homing_when_already_on_the_sensor_backs_off_first():
    esp = homed_esp()
    assert esp.physical()["sensor_active"]
    esp.feed_line("HOME")
    esp.tick(1)
    start = esp.time_ms
    t = run_until(esp, "OK HOMED")
    assert t - start < 100                      # backed off and re-approached, no full turn
    assert esp.physical()["physical_steps"] == 0


def test_no_sensor_build_assumes_alignment_and_homes_by_dead_reckoning():
    esp = VirtualESP32()
    esp.boot("none")
    assert esp.drain_output() == ["EVENT BOOT sim-1.0.0", "OK HOMING", "OK HOMED", "OK READY"]
    phys = esp.physical()
    assert phys["sensor_mode"] == "none" and phys["homed"]
    assert phys["angle_deg"] == 180.0 and phys["slot"] == 3        # hand alignment was wrong
    assert send(esp, "STATUS")[0].startswith("OK STATUS state=READY homed=1 slot=0")
    esp.feed_line("MOVE_SLOT 2")
    run_until(esp, "OK AT_SLOT 2")
    esp.feed_line("HOME")
    run_until(esp, "OK HOMED")
    assert esp.physical()["firmware_position"] == 0 and esp.physical()["angle_deg"] == 180.0


def test_gate_travel_is_atomic_and_input_waits():
    esp = homed_esp()
    esp.feed_bytes(b"OPEN_GATE\nSTATUS\n")
    esp.tick(1)
    started = esp.time_ms
    assert esp.drain_output() == []
    esp.tick(200)
    phys = esp.physical()
    assert phys["gate_open"] and phys["gate_pos"] == pytest.approx(0.5, abs=0.01)
    esp.tick(199)
    assert esp.drain_output() == []                                 # STATUS not answered yet
    esp.tick(1)
    assert esp.time_ms - started == 400
    out = esp.drain_output()
    assert out[0] == "OK GATE_OPEN"
    assert out[1].startswith("OK STATUS state=GATE_OPEN homed=1 slot=0 gate=OPEN")


def test_dispense_settles_before_opening():
    esp = homed_esp()
    esp.feed_line("DISPENSE_SLOT 1")
    at_slot = run_until(esp, "OK AT_SLOT 1")
    assert send(esp, "STATUS", ms=1)[0].startswith("OK STATUS state=AT_TARGET homed=1 slot=1 gate=CLOSED")
    opened = run_until(esp, "OK GATE_OPEN")
    assert opened - at_slot == 300 + 400                           # settle + servo travel
    assert esp.physical()["slot"] == 1 and esp.physical()["gate_pos"] == 1.0


def test_stop_during_settle_never_opens_the_gate():
    esp = homed_esp()
    esp.feed_line("DISPENSE_SLOT 2")
    run_until(esp, "OK AT_SLOT 2")
    assert send(esp, "STOP") == ["ERR STOPPED", "OK STOPPED"]
    esp.tick(2000)
    assert esp.drain_output() == []
    assert esp.physical()["gate_pos"] == 0.0 and esp.physical()["state"] == "SAFE_STOP"


def test_buttons_are_debounced_and_act_on_press():
    esp = homed_esp()
    esp.set_button("CONFIRM", True)
    esp.tick(20)
    esp.set_button("CONFIRM", False)
    esp.tick(100)
    assert esp.drain_output() == []                                # bounce shorter than 30 ms
    esp.set_button("confirm_button", True)
    t0 = esp.time_ms
    t = run_until(esp, "EVENT CONFIRM_BUTTON")
    assert 30 <= t - t0 <= 32
    esp.tick(500)                                                  # held: no repeat
    esp.set_button("CONFIRM", False)
    esp.tick(100)
    assert esp.drain_output() == []
    with pytest.raises(ValueError):
        esp.set_button("BIG", True)


def test_button_held_through_reboot_is_not_a_new_press():
    esp = homed_esp()
    esp.set_button("CANCEL", True)
    run_until(esp, "EVENT CANCEL_BUTTON")
    esp.boot()
    esp.tick(5000)
    assert "EVENT CANCEL_BUTTON" not in esp.drain_output()


def test_cancel_button_while_homing_stops():
    esp = VirtualESP32()
    esp.boot()
    esp.tick(500)
    esp.drain_output()
    esp.set_button("CANCEL", True)
    esp.tick(40)
    assert esp.drain_output() == ["EVENT CANCEL_BUTTON", "ERR STOPPED", "OK STOPPED"]
    assert esp.physical()["state"] == "SAFE_STOP"


def test_line_endings_partial_lines_and_overlong_input():
    esp = homed_esp()
    assert send(esp, "") == []
    esp.feed_bytes(b"PING\r\nping\rPI")
    esp.tick(1)
    assert esp.drain_output() == ["OK PONG", "OK PONG"]
    esp.feed_bytes(b"NG\n")
    esp.tick(1)
    assert esp.drain_output() == ["OK PONG"]
    esp.feed_bytes(b"P" * 200 + b"\nPING\n")
    esp.tick(1)
    assert esp.drain_output() == ["ERR UNKNOWN_COMMAND", "OK PONG"]
    assert send(esp, "MOVE_SLOT 6") == ["ERR INVALID_SLOT"]
    assert send(esp, "\xe9t\xe9") == ["ERR UNKNOWN_COMMAND"]


def test_brownout_flag_reboots_at_gate_opening():
    esp = homed_esp()
    esp.brownout_on_gate = True
    out = send(esp, "OPEN_GATE")
    assert out == ["EVENT BOOT sim-1.0.0", "OK HOMING"]
    run_until(esp, "OK READY")
    phys = esp.physical()
    assert phys["boots"] == 2 and phys["gate_pos"] == 0.0 and phys["state"] == "READY"


def test_reboot_with_gate_open_closes_gate_before_boot_event():
    esp = homed_esp()
    esp.feed_line("OPEN_GATE")
    run_until(esp, "OK GATE_OPEN")
    esp.boot()
    assert esp.drain_output() == []                                # servo closing first
    t0 = esp.time_ms
    t = run_until(esp, "EVENT BOOT sim-1.0.0")
    assert t - t0 == 400 and esp.physical()["gate_pos"] == 0.0


def test_gate_auto_close_is_exact():
    esp = homed_esp(SimConfig(gate_max_open_ms=2000))
    esp.feed_line("OPEN_GATE")
    opened = run_until(esp, "OK GATE_OPEN")
    esp.feed_line("OPEN_GATE")                                     # idempotent, no timer reset
    esp.tick(1)
    assert esp.drain_output() == ["OK GATE_OPEN"]
    closed, out = run_until_lines(esp, "OK GATE_CLOSED")
    assert closed - opened == 2000 + 400
    assert out == ["OK GATE_CLOSED", "OK READY"]


def test_idle_skipping_is_equivalent_to_ticking_every_millisecond():
    # "+NAME"/"-NAME" press/release a button: a bounce, then a too-short press, then a real one.
    script = [(0, None), (5000, "OPEN_GATE"), (5500, "STATUS"), (9000, "CLOSE_GATE"),
              (9600, "DISPENSE_SLOT 4"), (12000, "PING"),
              (13000, "+CONFIRM"), (13020, "-CONFIRM"), (13400, "+CONFIRM"), (13410, "-CONFIRM"),
              (13600, "+CONFIRM"), (13700, "-CONFIRM"),
              (14000, "+CANCEL"), (14010, "-CANCEL"), (14200, "+CANCEL"), (14215, "-CANCEL"),
              (14400, "+CANCEL"), (14500, "-CANCEL"), (20000, "STATUS")]
    config = SimConfig(gate_max_open_ms=1500)

    def run(fine: bool) -> tuple[list[str], list[dict]]:
        esp = VirtualESP32(config)
        esp.boot()
        out: list[str] = []
        snaps: list[dict] = []
        for at, action in script:
            gap = at - esp.time_ms
            if fine:
                for _ in range(gap):
                    esp.tick(1)
            else:
                esp.tick(gap)
            snaps.append(esp.physical())
            if action and action[0] in "+-":
                esp.set_button(action[1:], action[0] == "+")
            elif action:
                esp.feed_line(action)
            out += esp.drain_output()
        esp.tick(1)
        return out + esp.drain_output(), snaps

    fine, coarse = run(True), run(False)
    assert fine == coarse
    assert fine[0].count("EVENT CONFIRM_BUTTON") == 1 and fine[0].count("EVENT CANCEL_BUTTON") == 1


def test_physical_reports_everything_the_demo_needs():
    phys = homed_esp().physical()
    for key in ("angle_deg", "physical_steps", "slot", "gate_open", "state", "homed",
                "firmware_position", "jammed", "sensor_mode", "time_ms"):
        assert key in phys


def test_config_validation():
    with pytest.raises(ValueError):
        SimConfig(num_slots=1)
    with pytest.raises(ValueError):
        SimConfig(home_sensor="broken")
    with pytest.raises(ValueError):
        VirtualESP32().set_sensor("none")


# --------------------------------------------------------------------------- SimulatedDevice


def test_sim_device_runs_in_scaled_real_time(make_device):
    dev = make_device(speed=20, start=False)
    link = dev.open_transport()
    t0 = time.monotonic()
    dev.start()
    assert threading.Thread.__name__ and any(t.name == "sim-device" for t in threading.enumerate())
    read_lines(link, has("EVENT BOOT sim-1.0.0", "OK READY"))
    elapsed = time.monotonic() - t0
    assert 0.1 < elapsed < 1.5                                     # ~4 s of homing at x20
    sim_t0, real_t0 = dev.time_ms, time.monotonic()
    time.sleep(0.3)
    ratio = (dev.time_ms - sim_t0) / ((time.monotonic() - real_t0) * 1000)
    assert 10 < ratio <= 21


def test_transport_round_trip_with_crlf(make_device):
    dev = make_device()
    link = dev.open_transport()
    assert link.is_open and link.name == "sim://"
    wait_until(lambda: dev.physical()["state"] == "READY", 3)
    link.write(b"PING\nSTATUS\n")
    lines = read_lines(link, lambda ls: any(l.startswith("OK STATUS") for l in ls))
    assert "OK PONG" in lines
    link.close()
    link.close()
    with pytest.raises(TransportError):
        link.read()
    with pytest.raises(TransportError):
        link.write(b"PING\n")


def test_new_transport_replaces_the_old_one(make_device):
    dev = make_device()
    old = dev.open_transport()
    new = dev.open_transport()
    with pytest.raises(TransportError):
        old.read()
    new.write(b"PING\n")
    assert "OK PONG" in read_lines(new, has("OK PONG"))


def test_fault_unresponsive_drops_input_and_output(make_device):
    dev = make_device()
    link = dev.open_transport()
    wait_until(lambda: dev.physical()["state"] == "READY", 3)
    link.write(b"DISPENSE_SLOT 2\n")
    read_lines(link, has("OK MOVING 2"))
    dev.set_fault("unresponsive", True)
    assert dev.faults()["unresponsive"] is True
    assert wait_until(lambda: dev.physical()["state"] == "GATE_OPEN", 3)  # firmware keeps running
    link.write(b"PING\n")                                           # dropped
    time.sleep(0.2)
    assert link.read() == b""                                       # nothing came through
    dev.set_fault("unresponsive", False)
    link.write(b"PING\n")
    lines = read_lines(link, has("OK PONG"))
    assert "OK GATE_OPEN" not in lines                              # output while silent is lost


def test_fault_disconnect_breaks_link_until_cleared(make_device):
    dev = make_device()
    link = dev.open_transport()
    dev.set_fault("disconnect", True)
    with pytest.raises(TransportError):
        link.read()
    with pytest.raises(TransportError):
        link.write(b"PING\n")
    with pytest.raises(TransportError):
        dev.open_transport()
    dev.set_fault("disconnect", False)
    with pytest.raises(TransportError):
        link.read()                                                 # the old handle stays dead
    fresh = dev.open_transport()
    fresh.write(b"PING\n")
    assert "OK PONG" in read_lines(fresh, has("OK PONG"))


def test_fault_brownout_on_gate_reboots_without_opening(make_device):
    dev = make_device()
    link = dev.open_transport()
    read_lines(link, has("OK READY"))                               # boot sequence
    dev.set_fault("brownout_on_gate", True)
    link.write(b"DISPENSE_SLOT 1\n")
    lines = read_lines(link, has("EVENT BOOT sim-1.0.0", "OK READY"))
    assert lines[:4] == ["OK MOVING 1", "OK AT_SLOT 1", "EVENT BOOT sim-1.0.0", "OK HOMING"]
    assert "OK GATE_OPEN" not in lines
    phys = dev.physical()
    assert phys["boots"] == 2 and not phys["gate_open"] and phys["slot"] == 0


def test_fault_home_sensor_dead_and_motor_jam(make_device):
    dev = make_device(speed=100)
    link = dev.open_transport()
    wait_until(lambda: dev.physical()["state"] == "READY", 3)
    dev.set_fault("motor_jam", True)
    link.write(b"MOVE_SLOT 3\n")
    read_lines(link, has("OK MOVING 3", "ERR MOTOR_FAULT"))
    dev.set_fault("motor_jam", False)
    dev.set_fault("home_sensor_dead", True)
    dev.reboot()
    lines = read_lines(link, has("ERR HOME_TIMEOUT"))
    assert "EVENT BOOT sim-1.0.0" in lines
    assert dev.physical()["sensor_mode"] == "dead" and dev.physical()["state"] == "FAULT"
    dev.set_fault("home_sensor_dead", False)
    link.write(b"HOME\n")
    read_lines(link, has("OK HOMED", "OK READY"))


def test_press_is_a_100ms_physical_press(make_device):
    dev = make_device()
    link = dev.open_transport()
    wait_until(lambda: dev.physical()["state"] == "READY", 3)
    dev.press("CONFIRM")
    assert "EVENT CONFIRM_BUTTON" in read_lines(link, has("EVENT CONFIRM_BUTTON"))
    assert wait_until(lambda: dev.physical()["buttons"]["CONFIRM"] is False, 2)
    with pytest.raises(ValueError):
        dev.press("HELP")
    with pytest.raises(ValueError):
        dev.set_fault("gremlins", True)
    assert set(dev.faults()) == set(FAULT_NAMES)


def test_sim_physical_published_at_most_10hz(make_device):
    bus = EventBus()
    sub = bus.subscribe([Topic.SIM_PHYSICAL])
    dev = make_device(speed=5, bus=bus)
    t0 = time.monotonic()
    time.sleep(0.7)                                                 # homing in progress all along
    events = sub.drain()
    elapsed = time.monotonic() - t0
    assert 3 <= len(events) <= elapsed * 10 + 2
    data = events[-1].data
    assert {"angle_deg", "slot", "gate_open", "state", "faults"} <= set(data)
    dev.close()


def test_close_is_idempotent_and_stops_the_thread(make_device):
    dev = make_device()
    link = dev.open_transport()
    dev.close()
    dev.close()
    assert not any(t.name == "sim-device" and t.is_alive() for t in threading.enumerate())
    with pytest.raises(TransportError):
        link.read()
    with pytest.raises(TransportError):
        dev.open_transport()
