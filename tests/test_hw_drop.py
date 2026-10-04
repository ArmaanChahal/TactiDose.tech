"""Serial protocol v1.1 pill drops (docs/SERIAL_PROTOCOL.md §12) across the hardware layer.

* VirtualESP32: ``DROP_SLOT`` sequence/timing, atomic release, STOP/cancel, ``ERR NO_PILL``, pill
  counts, drop sensor, v1 firmware (``proto=None``), brown-outs, idle-skip equivalence.
* SimulatedDevice / ConformanceSimTarget: containers from settings, ``set_pills``, harness defaults.
* HardwareClient.drop_slot over the real-time simulator and scripted devices: DROPPED, NO_PILL,
  STOP -> NOT_DROPPED, TIMEOUT -> UNCERTAIN, reset after ``OK GATE_OPEN`` -> UNCERTAIN, the v1
  emulation (``DISPENSE_SLOT`` + ``CLOSE_GATE``), refusals, snapshot ``proto``/``drop_sensor``.
* hw-test: one drop check per container (v1.1 and v1 emulation).
"""

from __future__ import annotations

import pytest

from tactidose.core.bus import Topic
from tactidose.core.interfaces import HardwareController
from tactidose.hardware.conformance import load_scenarios
from tactidose.hardware.protocol import (
    CommandName,
    DeviceState,
    DropCertainty,
    Err,
    GateState,
    HostCode,
    drop_certainty,
)
from tactidose.hardware.selftest import EXIT_FAILED, EXIT_OK, run_hw_test
from tactidose.hardware.serial_client import HardwareClient, NullHardware
from tactidose.hardware.simulator import (
    FAULT_NAMES,
    SIM_FW_V1,
    SIM_FW_V11,
    ConformanceSimTarget,
    SimConfig,
    SimulatedDevice,
    VirtualESP32,
)
from tests.fakes import wait_until
from tests.test_hw_client import ScriptedDevice, in_thread, lines, tx_lines, wait_ready

STATUS_V2 = "OK STATUS state=READY homed=1 slot={slot} gate=CLOSED slots=3 fw=sim-1.1.0 proto=1.1 drop_sensor=1"

# --------------------------------------------------------------------------- helpers


def v2(**overrides) -> SimConfig:
    """The v2 device: 3 containers (everything else as the reference build)."""
    return SimConfig(num_slots=3, **overrides)


def slow_v2(**overrides) -> SimConfig:
    """For real-time tests that must act *during* a motion: short boot homing (0.5 s simulated)."""
    return v2(initial_offset_steps=200, **overrides)


def quick_v2(**overrides) -> SimConfig:
    """Whole-checklist runs: short boot homing and quick servo/release; carousel motion unchanged
    (the STOP-during-move check needs a move that outlasts thread-scheduling jitter)."""
    return v2(initial_offset_steps=200, settle_ms=100, gate_travel_ms=200, drop_open_ms=300,
              homing_speed_sps=800, **overrides)


def collect(esp: VirtualESP32, line: str, limit_ms: int = 60000) -> tuple[int, list[str]]:
    """Tick 1 ms at a time until ``line`` is emitted; return (sim time, every line seen)."""
    seen: list[str] = []
    for _ in range(limit_ms):
        esp.tick(1)
        out = esp.drain_output()
        seen += out
        if line in out:
            return esp.time_ms, seen
    raise AssertionError(f"{line!r} not emitted within {limit_ms} ms; got {seen}")


def homed(config: SimConfig | None = None) -> VirtualESP32:
    esp = VirtualESP32(config if config is not None else v2())
    esp.boot()
    collect(esp, "OK READY")
    return esp


def send(esp: VirtualESP32, line: str, ms: int = 1) -> list[str]:
    esp.feed_line(line)
    esp.tick(ms)
    return esp.drain_output()


@pytest.fixture
def rig(settings_v2, bus):
    """``rig(**overrides)`` -> (HardwareClient, SimulatedDevice) on the v2 settings (3 containers)."""
    clients: list[HardwareClient] = []

    def make(*, config: SimConfig | None = None, start: bool = True, speed: float = 50.0, **overrides):
        values = {"hardware_mode": "sim", "sim_speed": speed, "hw_heartbeat_s": 60.0,
                  "hw_reconnect_max_s": 0.5, **overrides}
        s = settings_v2.model_copy(update=values)
        sim = SimulatedDevice(s, bus=bus, config=config)
        hw = HardwareClient(s, bus=bus, mode="sim", sim_device=sim)
        clients.append(hw)
        if start:
            hw.start()
        return hw, sim

    yield make
    for hw in clients:
        hw.close()


@pytest.fixture
def sim_device(settings_v2):
    """``sim_device(speed, config)`` -> a started SimulatedDevice (closed at teardown)."""
    devices: list[SimulatedDevice] = []

    def make(speed: float = 8.0, config: SimConfig | None = None) -> SimulatedDevice:
        config = config if config is not None else quick_v2()
        dev = SimulatedDevice(settings_v2.model_copy(update={"sim_speed": speed}), config=config)
        devices.append(dev)
        dev.start()
        return dev

    yield make
    for dev in devices:
        dev.close()


# --------------------------------------------------------------------------- VirtualESP32: DROP_SLOT


def test_drop_slot_sequence_timing_and_pill_count():
    esp = homed()
    assert send(esp, "DROP_SLOT 2") == ["OK MOVING 2"]
    at_slot, _ = collect(esp, "OK AT_SLOT 2")
    opened, out = collect(esp, "OK GATE_OPEN")
    assert out == ["OK GATE_OPEN"] and opened - at_slot == 300 + 400          # settle + servo travel
    phys = esp.physical()
    assert phys["gate_open"] and phys["releasing"] and phys["state"] == "AT_TARGET"
    assert phys["pills"] == [20, 20, 19] and phys["pills_dropped"] == 1       # fell as it opened
    closed, out = collect(esp, "OK GATE_CLOSED")
    assert closed - opened == 600 + 400                                        # hold + servo travel
    assert out == ["OK GATE_CLOSED", "OK DROPPED 2", "OK READY"]
    phys = esp.physical()
    assert not phys["gate_open"] and not phys["releasing"] and phys["slot"] == 2 and phys["gate_pos"] == 0.0
    assert send(esp, "STATUS") == [STATUS_V2.format(slot=2)]


@pytest.mark.parametrize("pills,verdict", [(20, "OK DROPPED 1"), (0, "ERR NO_PILL")])
def test_release_is_atomic_and_stop_during_it_is_processed_after_ready(pills, verdict):
    esp = homed()
    esp.set_pills(1, pills)
    esp.feed_line("DROP_SLOT 1")
    collect(esp, "OK GATE_OPEN")
    esp.feed_bytes(b"STOP\nSTATUS\n")
    esp.tick(999)
    assert esp.drain_output() == []                                 # blocked in the release
    esp.tick(1)
    assert esp.drain_output() == [
        "OK GATE_CLOSED", verdict, "OK READY", "OK STOPPED",
        "OK STATUS state=SAFE_STOP homed=0 slot=-1 gate=CLOSED slots=3 fw=sim-1.1.0 proto=1.1 drop_sensor=1",
    ]
    assert esp.pills == (20, max(0, pills - 1), 20)


@pytest.mark.parametrize("trigger", ["STOP", "CANCEL"])
@pytest.mark.parametrize("phase", ["motion", "settle"])
def test_stop_or_cancel_before_the_release_never_drops(phase, trigger):
    esp = homed()
    esp.feed_line("DROP_SLOT 2")
    collect(esp, "OK MOVING 2" if phase == "motion" else "OK AT_SLOT 2")
    if trigger == "STOP":
        assert send(esp, "STOP") == ["ERR STOPPED", "OK STOPPED"]
    else:
        esp.set_button("CANCEL", True)                              # debounced: acts 30 ms later
        esp.tick(40)
        assert esp.drain_output() == ["EVENT CANCEL_BUTTON", "ERR STOPPED", "OK STOPPED"]
        esp.set_button("CANCEL", False)
    esp.tick(3000)
    assert esp.drain_output() == []
    phys = esp.physical()
    assert phys["state"] == "SAFE_STOP" and phys["gate_pos"] == 0.0 and not phys["releasing"]
    assert phys["pills"] == [20, 20, 20] and phys["pills_dropped"] == 0


def test_cancel_held_during_the_release_is_handled_after_it():
    esp = homed()
    esp.feed_line("DROP_SLOT 0")                                    # already there: no motion
    _, out = collect(esp, "OK GATE_OPEN")
    assert out == ["OK MOVING 0", "OK AT_SLOT 0", "OK GATE_OPEN"]
    esp.set_button("CANCEL", True)
    _, out = collect(esp, "EVENT CANCEL_BUTTON")
    assert out == ["OK GATE_CLOSED", "OK DROPPED 0", "OK READY", "EVENT CANCEL_BUTTON"]
    assert esp.physical()["state"] == "READY"                        # cancel while idle: event only


def test_last_pill_then_empty_container_reports_no_pill():
    esp = homed()
    esp.set_pills(1, 1)
    esp.feed_line("DROP_SLOT 1")
    _, out = collect(esp, "OK READY")
    assert out[-3:] == ["OK GATE_CLOSED", "OK DROPPED 1", "OK READY"]
    esp.feed_line("DROP_SLOT 1")
    _, out = collect(esp, "OK READY")
    assert out == ["OK MOVING 1", "OK AT_SLOT 1", "OK GATE_OPEN", "OK GATE_CLOSED", "ERR NO_PILL", "OK READY"]
    assert esp.pills == (20, 0, 20) and esp.physical()["pills_dropped"] == 1


def test_without_a_drop_sensor_ok_dropped_only_means_cycle_completed():
    esp = homed(v2(drop_sensor=False))
    esp.set_pills(0, 0)
    esp.feed_line("DROP_SLOT 0")
    _, out = collect(esp, "OK READY")
    assert out[-2:] == ["OK DROPPED 0", "OK READY"] and esp.pills == (0, 20, 20)
    assert send(esp, "STATUS")[0].endswith(" proto=1.1 drop_sensor=0")


def test_misaligned_carousel_releases_from_the_physical_position():
    esp = VirtualESP32(v2())
    esp.boot("none")                     # sensorless build, aligned by hand 180 degrees off
    esp.drain_output()
    assert esp.physical()["slot"] is None                           # between two containers
    esp.feed_line("DROP_SLOT 0")
    _, out = collect(esp, "OK READY")
    assert out[-2:] == ["ERR NO_PILL", "OK READY"] and esp.pills == (20, 20, 20)


def test_v1_firmware_does_not_know_drop_slot():
    esp = homed(v2(proto=None))
    assert esp.config.fw_version == SIM_FW_V1 and not esp.config.drop_slot_supported
    assert send(esp, "DROP_SLOT 1") == ["ERR UNKNOWN_COMMAND"]
    assert send(esp, "DROP_SLOT 9") == ["ERR UNKNOWN_COMMAND"]       # the word itself is unknown
    assert send(esp, "STATUS") == ["OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=3 fw=sim-1.0.0"]
    esp.feed_line("DISPENSE_SLOT 1")      # what the host's v1 emulation sends: the release opens
    collect(esp, "OK GATE_OPEN")
    assert esp.pills == (20, 19, 20)


def test_drop_slot_acceptance_follows_the_dispense_row():
    esp = VirtualESP32(v2())
    esp.boot()
    esp.tick(1)
    esp.drain_output()
    assert send(esp, "DROP_SLOT 1") == ["ERR BUSY"]                  # HOMING
    assert send(esp, "DROP_SLOT 3") == ["ERR INVALID_SLOT"]          # validated first, in every state
    collect(esp, "OK READY")
    assert send(esp, "DROP_SLOT 2") == ["OK MOVING 2"]
    assert send(esp, "DROP_SLOT 1") == ["ERR BUSY"]                  # MOVING
    collect(esp, "OK AT_SLOT 2")
    assert send(esp, "DROP_SLOT 1") == ["ERR BUSY"]                  # AT_TARGET (settling)
    assert send(esp, "DROP_SLOT -1") == ["ERR INVALID_SLOT"]
    collect(esp, "OK READY")
    esp.feed_line("OPEN_GATE")
    collect(esp, "OK GATE_OPEN")
    assert send(esp, "DROP_SLOT 1") == ["ERR INVALID_STATE"]         # GATE_OPEN
    esp.feed_line("STOP")
    collect(esp, "OK STOPPED")
    assert send(esp, "DROP_SLOT 1") == ["ERR NOT_HOMED"]             # SAFE_STOP
    esp.feed_line("HOME")
    collect(esp, "OK READY")
    esp.set_jam(True)
    esp.feed_line("MOVE_SLOT 1")
    collect(esp, "ERR MOTOR_FAULT")
    assert send(esp, "DROP_SLOT 1") == ["ERR NOT_HOMED"]             # FAULT


def test_brownout_on_release_resets_after_the_pill_fell():
    esp = homed()
    esp.brownout_on_release = True
    esp.feed_line("DROP_SLOT 1")
    opened, out = collect(esp, "OK GATE_OPEN")
    assert out == ["OK MOVING 1", "OK AT_SLOT 1", "OK GATE_OPEN"]
    booted, out = collect(esp, f"EVENT BOOT {SIM_FW_V11}")
    assert out == [f"EVENT BOOT {SIM_FW_V11}", "OK HOMING"]
    assert booted - opened == 600 + 400              # reset as it starts to close; boot closes it first
    _, out = collect(esp, "OK READY")
    assert "OK GATE_CLOSED" not in out and "OK DROPPED 1" not in out
    phys = esp.physical()
    assert phys["pills"] == [20, 19, 20] and phys["boots"] == 2 and not phys["gate_open"] and phys["slot"] == 0


def test_brownout_as_the_release_opens_drops_nothing():
    esp = homed()
    esp.brownout_on_gate = True
    esp.feed_line("DROP_SLOT 2")
    _, out = collect(esp, f"EVENT BOOT {SIM_FW_V11}")
    assert out == ["OK MOVING 2", "OK AT_SLOT 2", f"EVENT BOOT {SIM_FW_V11}", "OK HOMING"]
    assert esp.pills == (20, 20, 20) and esp.physical()["gate_pos"] == 0.0


def test_every_gate_opening_releases_one_pill():
    esp = homed()
    esp.feed_line("OPEN_GATE")
    collect(esp, "OK GATE_OPEN")
    assert send(esp, "OPEN_GATE") == ["OK GATE_OPEN"]               # already open: no second pill
    assert esp.pills == (19, 20, 20)
    esp.feed_line("CLOSE_GATE")
    collect(esp, "OK READY")
    esp.feed_line("OPEN_GATE")
    collect(esp, "OK GATE_OPEN")
    assert esp.pills == (18, 20, 20) and esp.physical()["pills_dropped"] == 2


def test_set_pills_validates_and_survives_reboot():
    esp = VirtualESP32(v2(initial_pills=5))
    assert esp.pills == (5, 5, 5) and esp.physical()["pills"] == [5, 5, 5]
    esp.set_pills(1, 0)
    esp.set_pills(2, 30)
    assert esp.pills == (5, 0, 30)
    for slot, count in ((-1, 1), (3, 1), (True, 1), ("1", 1), (0, -1), (0, 1.5), (0, True)):
        with pytest.raises(ValueError):
            esp.set_pills(slot, count)  # type: ignore[arg-type]
    esp.boot()
    esp.tick(6000)
    assert esp.pills == (5, 0, 30)                                   # physical: kept across a reset


def test_idle_skipping_is_equivalent_to_fine_ticking_during_drops():
    # "+NAME"/"-NAME" press/release a button, "=slot,count" sets pills, anything else is a host line.
    script = [(0, None), (5000, "DROP_SLOT 1"), (6500, "STATUS"), (6800, "+CANCEL"), (6900, "-CANCEL"),
              (9000, "DROP_SLOT 2"), (9100, "STOP"), (9200, "HOME"), (16000, "=0,0"), (16000, "DROP_SLOT 0"),
              (20000, "STATUS")]

    def run(fine: bool) -> tuple[list[str], list[dict]]:
        esp = VirtualESP32(v2())
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
            elif action and action[0] == "=":
                slot, count = action[1:].split(",")
                esp.set_pills(int(slot), int(count))
            elif action:
                esp.feed_line(action)
            out += esp.drain_output()
        esp.tick(1)
        return out + esp.drain_output(), snaps

    fine, coarse = run(True), run(False)
    assert fine == coarse
    lines_ = fine[0]
    assert "OK DROPPED 1" in lines_ and "ERR STOPPED" in lines_ and "ERR NO_PILL" in lines_
    # STATUS sent during the release is answered after it; the short press inside it is never seen
    assert lines_.index("OK DROPPED 1") < next(i for i, l in enumerate(lines_) if l.startswith("OK STATUS"))
    assert "EVENT CANCEL_BUTTON" not in lines_


# --------------------------------------------------------------------------- config / targets / device


def test_sim_config_v11_fields_and_validation(settings_v2):
    c = SimConfig()
    assert c.proto == "1.1" and c.fw_version == SIM_FW_V11 and c.drop_slot_supported
    assert c.drop_open_ms == 600 and c.drop_sensor is True and c.initial_pills == 20
    assert SimConfig(proto=None).fw_version == SIM_FW_V1 and SimConfig(proto="1.0").fw_version == SIM_FW_V1
    assert SimConfig(fw_version="custom-9").fw_version == "custom-9"
    assert SimConfig.from_settings(settings_v2).num_slots == 3
    for bad in ({"drop_open_ms": -1}, {"initial_pills": -1}, {"initial_pills": True}, {"initial_pills": 2.5},
                {"proto": ""}, {"proto": "1 .1"}, {"fw_version": "a b"}):
        with pytest.raises(ValueError):
            SimConfig(**bad)


def test_sim_defaults_are_the_conformance_harness():
    harness = load_scenarios()["harness"]
    c = SimConfig()
    assert c.num_slots == harness["num_slots"] and c.steps_per_rev == harness["steps_per_carousel_rev"]
    assert c.initial_offset_steps == harness["initial_physical_offset_steps"]
    assert c.gate_max_open_ms == harness["gate_max_open_ms"] and c.drop_sensor is harness["drop_sensor"]
    assert c.initial_pills == harness["initial_pills_per_slot"]
    target = ConformanceSimTarget()
    target.set_pills(4, 2)
    assert target.esp.pills[4] == 2
    target.reset()
    assert target.esp.pills == (20,) * 6


def test_simulated_device_from_v2_settings(settings_v2, bus):
    dev = SimulatedDevice(settings_v2.model_copy(update={"sim_speed": 50.0}), bus=bus)
    try:
        assert dev.config.num_slots == 3 and dev.config.proto == "1.1"
        phys = dev.physical()
        assert phys["num_slots"] == 3 and phys["pills"] == [20, 20, 20] and phys["drop_sensor"] is True
        dev.set_pills(2, 4)
        assert dev.physical()["pills"] == [20, 20, 4]
        with pytest.raises(ValueError):
            dev.set_pills(3, 1)
        assert "brownout_on_release" in FAULT_NAMES and set(dev.faults()) == set(FAULT_NAMES)
        dev.set_fault("brownout_on_release", True)
        assert dev.faults()["brownout_on_release"] is True
        dev.set_fault("brownout_on_release", False)
        sub = bus.subscribe([Topic.SIM_PHYSICAL])
        dev.start()
        assert wait_until(lambda: any(e.data.get("pills") == [20, 20, 4] for e in sub.drain()), 3)
    finally:
        dev.close()


# --------------------------------------------------------------------------- HardwareClient.drop_slot (v1.1)


def test_drop_slot_through_the_client(rig, bus):
    hw, sim = rig()
    assert wait_ready(hw)
    snap = hw.snapshot()
    assert snap.proto == "1.1" and snap.drop_sensor is True and snap.num_slots_reported == 3
    sub = bus.subscribe([Topic.DEVICE_LINE])
    r = hw.drop_slot(2)
    assert r.ok and r.code == "DROPPED" and r.definitive and r.command.to_line() == "DROP_SLOT 2"
    assert lines(r) == ["OK MOVING 2", "OK AT_SLOT 2", "OK GATE_OPEN", "OK GATE_CLOSED", "OK DROPPED 2"]
    assert drop_certainty(r) is DropCertainty.DROPPED and r.hardware_result == "OK DROPPED"
    assert tx_lines(sub) == ["DROP_SLOT 2"]
    assert wait_ready(hw)
    snap = hw.snapshot()
    assert snap.slot == 2 and snap.gate is GateState.CLOSED and snap.last_error is None and snap.in_flight is None
    assert sim.physical()["pills"] == [20, 20, 19]


def test_drop_slot_no_pill_is_definitive_not_dropped(rig):
    hw, sim = rig()
    assert wait_ready(hw)
    sim.set_pills(1, 0)
    r = hw.drop_slot(1)
    assert not r.ok and r.code == Err.NO_PILL.value and r.definitive and not r.gate_may_be_open
    assert lines(r) == ["OK MOVING 1", "OK AT_SLOT 1", "OK GATE_OPEN", "OK GATE_CLOSED", "ERR NO_PILL"]
    assert drop_certainty(r) is DropCertainty.NOT_DROPPED and r.hardware_result == "ERR NO_PILL"
    assert wait_ready(hw) and hw.snapshot().last_error == "NO_PILL"


def test_device_without_drop_sensor_is_reported(rig):
    hw, _ = rig(config=v2(drop_sensor=False))
    assert wait_ready(hw)
    assert hw.snapshot().proto == "1.1" and hw.snapshot().drop_sensor is False


def test_stop_during_drop_motion_is_not_dropped(rig):
    hw, sim = rig(speed=3, config=slow_v2())
    assert wait_ready(hw, 10)
    th, box = in_thread(lambda: hw.drop_slot(2))
    assert wait_until(lambda: hw.snapshot().state is DeviceState.MOVING, 3)
    stop = hw.stop()
    th.join(10)
    r = box["result"]
    assert stop.ok and not r.ok and r.code == Err.STOPPED.value and r.definitive
    assert lines(r) == ["OK MOVING 2", "ERR STOPPED"] and drop_certainty(r) is DropCertainty.NOT_DROPPED
    assert hw.snapshot().state is DeviceState.SAFE_STOP
    phys = sim.physical()
    assert phys["pills"] == [20, 20, 20] and not phys["gate_open"]


def test_stop_during_the_release_is_handled_after_the_drop(rig, bus):
    hw, sim = rig(speed=3, config=slow_v2())
    assert wait_ready(hw, 10)
    seen: list[str] = []
    bus.add_listener(lambda ev: ev.data["dir"] == "rx" and seen.append(ev.data["line"]), [Topic.DEVICE_LINE])
    th, box = in_thread(lambda: hw.drop_slot(0))
    # STOP once the release has opened (sticky: a slow test thread may send it a bit later -
    # the device then handles it after the release anyway, which is what is asserted below)
    assert wait_until(lambda: "OK GATE_OPEN" in seen, 5)
    stop = hw.stop()
    th.join(10)
    r = box["result"]
    assert r.ok and r.code == "DROPPED" and drop_certainty(r) is DropCertainty.DROPPED
    assert stop.ok and stop.code == "STOPPED"
    assert wait_until(lambda: hw.snapshot().state is DeviceState.SAFE_STOP, 2)
    assert seen.index("OK DROPPED 0") < seen.index("OK READY") < seen.index("OK STOPPED")
    assert "ERR STOPPED" not in seen and sim.physical()["pills"] == [19, 20, 20]


def test_unresponsive_device_makes_the_drop_uncertain(rig):
    hw, sim = rig(timeout_drop_s=0.4, timeout_ping_s=0.3, timeout_status_s=0.3)
    assert wait_ready(hw)
    sim.set_fault("unresponsive", True)
    r = hw.drop_slot(0)
    assert r.code == HostCode.TIMEOUT.value and not r.ok and not r.definitive and r.gate_may_be_open
    assert drop_certainty(r) is DropCertainty.UNCERTAIN and r.hardware_result.startswith("UNCERTAIN TIMEOUT")
    assert sim.physical()["pills"] == [20, 20, 20]                 # the bytes never reached the firmware
    again = hw.drop_slot(0)                                          # STATUS resync fails -> not sent
    assert again.code == HostCode.NOT_CONNECTED.value and again.definitive and "resync" in again.detail
    assert drop_certainty(again) is DropCertainty.NOT_DROPPED
    sim.set_fault("unresponsive", False)


def test_reset_after_gate_open_makes_the_drop_uncertain(rig):
    hw, sim = rig()
    assert wait_ready(hw)
    sim.set_fault("brownout_on_release", True)
    r = hw.drop_slot(1)
    assert r.code == HostCode.DEVICE_RESET.value and not r.ok and r.definitive
    assert lines(r) == ["OK MOVING 1", "OK AT_SLOT 1", "OK GATE_OPEN"]
    assert drop_certainty(r) is DropCertainty.UNCERTAIN                # a pill may have dropped ...
    assert wait_ready(hw)
    assert sim.physical()["pills"] == [20, 19, 20]                     # ... and here it did
    assert hw.snapshot().resets_seen == 2 and hw.snapshot().gate is GateState.CLOSED


def test_reset_before_the_release_opens_is_not_dropped(rig):
    hw, sim = rig()
    assert wait_ready(hw)
    sim.set_fault("brownout_on_gate", True)
    r = hw.drop_slot(2)
    assert r.code == HostCode.DEVICE_RESET.value and lines(r) == ["OK MOVING 2", "OK AT_SLOT 2"]
    assert drop_certainty(r) is DropCertainty.NOT_DROPPED and sim.physical()["pills"] == [20, 20, 20]


def test_drop_refusals_never_reach_the_wire(rig, bus, settings_v2):
    hw, _ = rig()
    assert wait_ready(hw)
    sub = bus.subscribe([Topic.DEVICE_LINE])
    for bad in (3, -1, True, "1", 1.0, None):
        r = hw.drop_slot(bad)  # type: ignore[arg-type]
        assert r.code == HostCode.INVALID_ARGUMENT.value and r.definitive and not r.ok, bad
        assert drop_certainty(r) is DropCertainty.NOT_DROPPED
    assert hw.drop_slot(3).command.to_line() == "DROP_SLOT 3"
    assert hw.send_raw("DROP_SLOT 3").code == HostCode.INVALID_ARGUMENT.value
    assert tx_lines(sub) == []
    raw = hw.send_raw(" drop_slot  1 ")                              # demo console: sent as typed
    assert raw.ok and raw.code == "DROPPED" and tx_lines(sub) == ["DROP_SLOT 1"]
    unstarted = HardwareClient(settings_v2, transport_factory=ScriptedDevice, mode="serial")
    r = unstarted.drop_slot(1)
    assert r.code == HostCode.NOT_CONNECTED.value and r.command.to_line() == "DROP_SLOT 1"
    unstarted.close()


def test_drop_while_another_command_is_in_flight_is_busy_local(rig):
    hw, _ = rig(speed=3, config=slow_v2())
    assert wait_ready(hw, 10)
    th, box = in_thread(lambda: hw.move_slot(2))
    assert wait_until(lambda: hw.snapshot().in_flight == "MOVE_SLOT 2", 3)
    busy = hw.drop_slot(1)
    assert busy.code == HostCode.BUSY_LOCAL.value and busy.command.to_line() == "DROP_SLOT 1"
    assert drop_certainty(busy) is DropCertainty.NOT_DROPPED and "MOVE_SLOT 2" in busy.detail
    th.join(10)
    assert box["result"].ok


def test_null_hardware_drop_slot(settings_v2):
    hw = NullHardware(settings_v2)
    assert isinstance(hw, HardwareController)
    r = hw.drop_slot(1)
    assert r.code == HostCode.NOT_CONNECTED.value and r.definitive and not r.gate_may_be_open
    assert r.command.to_line() == "DROP_SLOT 1" and drop_certainty(r) is DropCertainty.NOT_DROPPED
    assert hw.drop_slot(3).code == HostCode.INVALID_ARGUMENT.value
    assert hw.send_raw("DROP_SLOT 2").code == HostCode.NOT_CONNECTED.value


def test_every_status_refreshes_proto_and_the_drop_path(settings_v2):
    dev = ScriptedDevice()
    dev.replies["STATUS"] = ["OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=3 fw=x proto=1.1 drop_sensor=0"]
    # no OK GATE_CLOSED / OK READY: OK DROPPED alone must leave the mirror with the gate closed
    dev.replies["DROP_SLOT 2"] = ["OK MOVING 2", "OK AT_SLOT 2", "OK GATE_OPEN", "OK DROPPED 2"]
    hw = HardwareClient(settings_v2.model_copy(update={"hw_heartbeat_s": 60.0}),
                        transport_factory=lambda: dev, mode="serial")
    hw.start()
    try:
        assert wait_until(lambda: hw.snapshot().connected, 3)
        assert hw.snapshot().proto == "1.1" and hw.snapshot().drop_sensor is False
        r = hw.drop_slot(2)
        assert r.ok and r.code == "DROPPED"
        snap = hw.snapshot()
        assert snap.gate is GateState.CLOSED and snap.slot == 2 and snap.target_slot is None
        dev.replies["STATUS"] = ["OK STATUS state=READY homed=1 slot=2 gate=CLOSED slots=3 fw=y"]
        assert hw.status().ok
        snap = hw.snapshot()
        assert snap.proto is None and snap.drop_sensor is None and snap.fw_version == "y"
        r = hw.drop_slot(1)                       # now a v1 device: emulated; it refuses the dispense
        assert dev.written[-1] == "DISPENSE_SLOT 1" and r.code == Err.UNKNOWN_COMMAND.value
        assert r.command.name is CommandName.DISPENSE_SLOT and drop_certainty(r) is DropCertainty.NOT_DROPPED
    finally:
        hw.close()


# --------------------------------------------------------------------------- v1 emulation


def test_v1_firmware_drop_is_emulated_with_dispense_and_close(rig, bus):
    hw, sim = rig(config=v2(proto=None), drop_close_delay_ms=1000)
    assert wait_ready(hw)
    snap = hw.snapshot()
    assert snap.proto is None and snap.drop_sensor is None and snap.fw_version == SIM_FW_V1
    sub = bus.subscribe([Topic.DEVICE_LINE])
    th, box = in_thread(lambda: hw.drop_slot(1))
    # during the close delay the command lock is held: other commands and reconnects are refused
    assert wait_until(lambda: hw.snapshot().gate is GateState.OPEN, 3)
    assert hw.ping().code == HostCode.BUSY_LOCAL.value and hw.reconnect() is False
    th.join(10)
    r = box["result"]
    assert r.ok and r.code == "GATE_OPEN" and r.command.to_line() == "DISPENSE_SLOT 1"
    assert drop_certainty(r) is DropCertainty.DROPPED and r.definitive
    assert lines(r) == ["OK MOVING 1", "OK AT_SLOT 1", "OK GATE_OPEN"]
    assert r.detail == "v1 emulation: CLOSE_GATE after 1000 ms -> OK GATE_CLOSED"
    assert r.elapsed_s >= 1.0 and r.hardware_result.startswith("OK GATE_OPEN (v1 emulation")
    assert tx_lines(sub) == ["DISPENSE_SLOT 1", "CLOSE_GATE"]
    assert wait_ready(hw) and hw.snapshot().gate is GateState.CLOSED
    phys = sim.physical()
    assert phys["pills"] == [20, 19, 20] and not phys["gate_open"]


def test_v1_emulation_does_not_close_when_the_dispense_failed(rig, bus):
    hw, _ = rig(config=v2(proto=None), drop_close_delay_ms=200)
    assert wait_ready(hw)
    assert hw.stop().ok                                     # SAFE_STOP: the device refuses motion
    sub = bus.subscribe([Topic.DEVICE_LINE])
    r = hw.drop_slot(0)
    assert r.code == Err.NOT_HOMED.value and r.command.name is CommandName.DISPENSE_SLOT and r.detail == ""
    assert drop_certainty(r) is DropCertainty.NOT_DROPPED and tx_lines(sub) == ["DISPENSE_SLOT 0"]


def _v1_scripted(close_reply: list[str]) -> ScriptedDevice:
    dev = ScriptedDevice()
    dev.replies.update({
        "STATUS": ["OK STATUS state=READY homed=1 slot=0 gate=CLOSED slots=3 fw=fake-1.0"],
        "DISPENSE_SLOT 1": ["OK MOVING 1", "OK AT_SLOT 1", "OK GATE_OPEN"],
        "CLOSE_GATE": close_reply,
    })
    return dev


@pytest.mark.parametrize("close_reply,gate,alarm", [
    (["ERR BUSY"], GateState.OPEN, True),
    (["EVENT BOOT fake-1.0"], GateState.CLOSED, False),     # a reset closes the gate by itself
])
def test_v1_emulation_close_failure(settings_v2, bus, close_reply, gate, alarm):
    dev = _v1_scripted(close_reply)
    hw = HardwareClient(settings_v2.model_copy(update={"hw_heartbeat_s": 60.0, "drop_close_delay_ms": 200}),
                        bus=bus, transport_factory=lambda: dev, mode="serial")
    notices = bus.subscribe([Topic.NOTICE])
    hw.start()
    try:
        assert wait_until(lambda: hw.snapshot().connected, 3)
        r = hw.drop_slot(1)
        assert r.ok and drop_certainty(r) is DropCertainty.DROPPED            # the pill did drop
        assert r.detail.startswith("v1 emulation: CLOSE_GATE after 200 ms -> ")
        assert ("ERR BUSY" if alarm else "DEVICE_RESET") in r.detail
        assert dev.written[-2:] == ["DISPENSE_SLOT 1", "CLOSE_GATE"]
        assert hw.snapshot().gate is gate
        got = [e.data for e in notices.drain()]
        assert any(n["level"] == "error" and "did not close" in n["message"] for n in got) is alarm
    finally:
        hw.close()


# --------------------------------------------------------------------------- hw-test drop checks


def test_hw_test_drops_one_pill_per_container_and_reports_an_empty_one(settings_v2, sim_device):
    # (the all-pass v1.1 checklist is covered by test_hw_selftest on the 6-slot harness)
    sim = sim_device()
    sim.set_pills(2, 0)
    out: list[str] = []
    code = run_hw_test(settings_v2, transport_factory=sim.open_transport, out=out.append)
    text = "\n".join(out)
    assert code == EXIT_FAILED, text
    for slot in (0, 1):
        assert any(f"PASS  DROP_SLOT {slot}:" in line and f"OK DROPPED {slot}" in line for line in out), slot
    assert any("FAIL  DROP_SLOT 2:" in line and "ERR NO_PILL" in line and "container 3" in line for line in out)
    assert "PASS  HOME after STOP" in text and "PASS  Unknown command FOO" in text
    assert "13/14 checks passed, 2 skipped" in text and "protocol 1.1" in text
    assert sim.physical()["pills"] == [19, 18, 0]           # + the DISPENSE_SLOT 1 check's opening


def test_hw_test_uses_the_v1_emulation_for_old_firmware(settings_v2, sim_device):
    s = settings_v2.model_copy(update={"drop_close_delay_ms": 200})
    sim = sim_device(config=quick_v2(proto=None))
    out: list[str] = []
    code = run_hw_test(s, transport_factory=sim.open_transport, out=out.append)
    text = "\n".join(out)
    assert code == EXIT_OK, text
    for slot in range(3):
        assert f"PASS  DROP_SLOT {slot} (v1 emulation)" in text
    assert "CLOSE_GATE after 200 ms -> OK GATE_CLOSED" in text and "protocol v1, no DROP_SLOT" in text
    assert "14/14 checks passed, 2 skipped" in text
    assert sim.physical()["pills"] == [19, 18, 19] and not sim.physical()["gate_open"]
