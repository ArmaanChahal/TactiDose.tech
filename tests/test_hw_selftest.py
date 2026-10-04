"""hw-test checklist (run_hw_test) and SerialConformanceTarget, end-to-end against the simulator."""

from __future__ import annotations

import pytest

from tactidose.hardware.conformance import load_scenarios, run_all, scenario_skip_reason
from tactidose.hardware.selftest import EXIT_FAILED, EXIT_NO_DEVICE, EXIT_OK, SerialConformanceTarget, run_hw_test
from tactidose.hardware.simulator import SimulatedDevice
from tests.fakes import wait_until
from tests.test_hw_transports import running_tcp_simulator


@pytest.fixture
def make_sim(settings):
    devices: list[SimulatedDevice] = []

    def make(speed: float = 40.0) -> SimulatedDevice:
        dev = SimulatedDevice(settings.model_copy(update={"sim_speed": speed}))
        devices.append(dev)
        dev.start()
        return dev

    yield make
    for dev in devices:
        dev.close()


def test_checklist_passes_including_buttons(settings, make_sim):
    sim = make_sim()
    out: list[str] = []

    def console(line: str) -> None:            # plays the person pressing the buttons
        out.append(line)
        if ">>> Press the CONFIRM" in line:
            sim.press("CONFIRM")
        elif ">>> Press the CANCEL" in line:
            sim.press("CANCEL")

    code = run_hw_test(settings.model_copy(update={"sim_speed": 40.0}), transport_factory=sim.open_transport,
                       repeat=2, interactive=True, out=console)
    text = "\n".join(out)
    assert code == EXIT_OK, text
    assert "18/18 checks passed" in text
    for name in ("PING", "STATUS", "HOME", "MOVE_SLOT 5", "DISPENSE_SLOT 2 (run 2/2)", "STOP during MOVE_SLOT",
                 "HOME after STOP", "Unknown command FOO", "Confirm button", "Cancel button"):
        assert any(name in line and "PASS" in line for line in out), name
    assert sim.physical()["slot"] == 0 and not sim.physical()["gate_open"]   # left homed and closed


def test_checklist_over_tcp_uses_the_real_serial_stack(settings, make_sim):
    s = settings.model_copy(update={"sim_speed": 40.0})
    sim = make_sim()
    out: list[str] = []
    with running_tcp_simulator(s, sim) as url:
        code = run_hw_test(s, port=url, out=out.append)
    text = "\n".join(out)
    assert code == EXIT_OK, text
    assert "14/14 checks passed, 2 skipped" in text and url in text


def test_checklist_reports_a_failing_board(settings, make_sim):
    sim = make_sim(speed=50.0)
    assert wait_until(lambda: sim.physical()["state"] == "READY", 3)
    sim.set_fault("motor_jam", True)
    out: list[str] = []
    code = run_hw_test(settings.model_copy(update={"sim_speed": 50.0}), transport_factory=sim.open_transport,
                       out=out.append)
    text = "\n".join(out)
    assert code == EXIT_FAILED, text
    assert "FAIL  HOME" in text and "HOME_TIMEOUT" in text
    assert "MOVE_SLOT 0" not in text                       # motion checks skipped after HOME failed
    assert "PASS  Unknown command FOO" in text


def test_no_port_found(settings, monkeypatch):
    monkeypatch.setattr("tactidose.hardware.selftest.resolve_port", lambda value: None)
    monkeypatch.setattr("tactidose.hardware.selftest.format_ports", lambda: "  COM3  (no USB id)")
    out: list[str] = []
    assert run_hw_test(settings, out=out.append) == EXIT_NO_DEVICE
    assert "No ESP32 serial port found" in out[0] and "COM3" in "\n".join(out)


def test_silent_device_is_reported_as_no_answer(settings, make_sim):
    sim = make_sim(speed=50.0)
    sim.set_fault("unresponsive", True)
    out: list[str] = []
    s = settings.model_copy(update={"timeout_ping_s": 0.1, "timeout_status_s": 0.1})
    code = run_hw_test(s, transport_factory=sim.open_transport, out=out.append, connect_timeout_s=1.0)
    assert code == EXIT_NO_DEVICE and "no answer to PING/STATUS" in "\n".join(out)


def test_serial_conformance_target_runs_hardware_safe_scenarios(make_sim):
    sim = make_sim(speed=10.0)
    target = SerialConformanceTarget("sim", transport=sim.open_transport(), reset=sim.reboot)
    try:
        assert target.name == "serial:sim://"
        names = ["boot_homes_and_reports_ready", "ping_variants_and_blank_lines",
                 "unknown_and_overlong_commands", "dispense_happy_path", "stop_with_gate_open_closes_gate"]
        results = run_all(target, names=names, include_slow=False)
        assert len(results) == len(names) and not any(r.skipped for r in results)
        assert [r.describe() for r in results if not r.ok] == []
        reasons = {sc["name"]: scenario_skip_reason(target, sc, include_slow=False)
                   for sc in load_scenarios()["scenarios"]}
        assert reasons["motor_jam_during_move_faults"] == "target cannot inject faults"
        assert reasons["confirm_button_is_event_only"] == "target cannot press buttons"
        assert reasons["gate_auto_close_safety_net"] == "slow scenario not requested"
        with pytest.raises(NotImplementedError):
            target.boot("dead")
        with pytest.raises(NotImplementedError):
            target.set_jam(True)
    finally:
        target.close()
