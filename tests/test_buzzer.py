"""Buzzer: the interface and its backends (laptop / serial / both / none), the serial fallback,
the BUZZER protocol extension in the protocol module and the simulator (max duration, STOP,
reboot, faults), the HardwareClient methods, the guided-demo runner's use of the interface only,
and the ``buzzer-test`` CLI. The shared conformance scenarios (incl. the 8 buzzer ones) run in
test_hw_conformance_sim.py and, against the native firmware core, in test_fw_native.py.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tactidose.config import Settings
from tactidose.hardware import buzzer_config
from tactidose.hardware import protocol as p
from tactidose.hardware.buzzer import (
    BothBuzzer,
    LaptopToneBuzzer,
    NullBuzzer,
    SerialBuzzer,
    create_buzzer,
)
from tactidose.hardware.simulator import SimConfig, VirtualESP32

ROOT = Path(__file__).resolve().parents[1]


# =========================================================================== fakes


@dataclass
class _Snap:
    connected: bool = True
    fw_version: str = "fw-1"
    resets_seen: int = 0


class FakeBuzzerHardware:
    """HardwareClient stand-in: scripted BUZZER replies, call log."""

    def __init__(self, *codes: str) -> None:
        self.codes = list(codes)
        self.calls: list[str] = []
        self.snap = _Snap()

    def snapshot(self) -> _Snap:
        return self.snap

    def _result(self, cmd: p.Command) -> p.CommandResult:
        code = self.codes.pop(0) if self.codes else "BUZZER"
        if code == "RAISE":
            raise RuntimeError("serial port vanished")
        if code in ("TIMEOUT", "BUSY_LOCAL", "NOT_CONNECTED"):
            return p.CommandResult.host_failure(cmd, p.HostCode(code))
        return p.CommandResult(command=cmd, ok=code == "BUZZER", code=code)

    def buzzer_on(self, ms: int) -> p.CommandResult:
        self.calls.append(f"ON {ms}")
        return self._result(p.Command.buzzer_on(ms))

    def buzzer_off(self) -> p.CommandResult:
        self.calls.append("OFF")
        return self._result(p.Command.buzzer_off())


def _settings(**over: Any) -> Settings:
    return Settings(_env_file=None, **over)


# =========================================================================== backends


def test_laptop_tone_on_off_listener_and_auto_off(monkeypatch):
    b = LaptopToneBuzzer()
    seen: list[bool] = []
    b.add_listener(lambda bz: seen.append(bz.tone_active))
    assert b.supports_hardware is False and b.tone_active is False
    assert b.on(60) is True and b.tone_active is True
    time.sleep(0.25)
    assert b.tone_active is False                # stopped by itself after ms
    b.on(5000)
    b.off()
    b.off()                                      # idempotent
    assert seen == [True, False, True, False]
    monkeypatch.setattr(buzzer_config, "MAX_ON_MS", 50)
    b.on(60_000)                                 # clamped to MAX_ON_MS
    time.sleep(0.25)
    assert b.tone_active is False


def test_null_buzzer_is_silent():
    b = NullBuzzer()
    assert b.on(100) is False and not b.tone_active and not b.hardware_active and not b.supports_hardware
    b.off()


def test_serial_buzzer_success_uses_the_device_only():
    hw = FakeBuzzerHardware("BUZZER", "BUZZER")
    b = SerialBuzzer(hw)
    assert b.supports_hardware is True
    assert b.on(1500) is True
    assert hw.calls == ["ON 1500"] and b.hardware_active and not b.tone_active
    b.off()
    assert hw.calls == ["ON 1500", "OFF"] and not b.hardware_active


@pytest.mark.parametrize("code", ["UNKNOWN_COMMAND", "NO_BUZZER"])
def test_serial_buzzer_unsupported_falls_back_and_remembers(code, caplog):
    hw = FakeBuzzerHardware(code)
    b = SerialBuzzer(hw)
    with caplog.at_level("WARNING"):
        assert b.on(1000) is True
    assert b.tone_active and not b.hardware_active
    assert any("laptop tone" in r.message for r in caplog.records if r.levelname == "WARNING")
    b.off()
    b.on(1000)
    assert hw.calls == ["ON 1000"]               # remembered: no second wire call
    hw.snap.resets_seen += 1                     # the device rebooted (maybe new firmware): ask again
    b.on(1000)
    assert hw.calls == ["ON 1000", "ON 1000"] and b.hardware_active


@pytest.mark.parametrize("code", ["TIMEOUT", "BUSY_LOCAL"])
def test_serial_buzzer_retries_then_falls_back(code):
    hw = FakeBuzzerHardware(*[code] * 5)
    b = SerialBuzzer(hw)
    assert b.on(800) is True
    assert len(hw.calls) == 1 + buzzer_config.RETRIES
    assert b.tone_active and not b.hardware_active and b.last_error == code


def test_serial_buzzer_never_raises():
    b = SerialBuzzer(FakeBuzzerHardware("RAISE"))
    assert b.on(500) is True and b.tone_active
    b.off()
    no_support = SerialBuzzer(object())           # hardware without buzzer methods (e.g. NullHardware)
    assert no_support.on(500) is True and no_support.tone_active
    off_fails = FakeBuzzerHardware("BUZZER", "RAISE")
    sb = SerialBuzzer(off_fails)
    sb.on(500)
    sb.off()                                      # a failing BUZZER OFF is only logged
    assert not sb.hardware_active


def test_both_sounds_device_and_laptop_and_survives_a_device_failure():
    both = BothBuzzer(FakeBuzzerHardware("BUZZER"))
    assert both.on(700) is True and both.tone_active and both.hardware_active
    both.off()
    assert not both.tone_active and not both.hardware_active
    broken = BothBuzzer(FakeBuzzerHardware("NO_BUZZER"))
    assert broken.on(700) is True and broken.tone_active and not broken.hardware_active


def test_factory_and_setting():
    assert _settings().buzzer_backend == "laptop"
    hw = FakeBuzzerHardware()
    assert isinstance(create_buzzer(_settings(), hw), LaptopToneBuzzer)
    assert isinstance(create_buzzer(_settings(buzzer_backend="serial"), hw), SerialBuzzer)
    assert isinstance(create_buzzer(_settings(buzzer_backend="both"), hw), BothBuzzer)
    assert isinstance(create_buzzer(_settings(buzzer_backend="none"), hw), NullBuzzer)
    assert isinstance(create_buzzer(_settings(buzzer_backend="serial"), None), LaptopToneBuzzer)
    with pytest.raises(Exception):
        _settings(buzzer_backend="loud")


def test_runner_depends_only_on_the_interface():
    src = (ROOT / "tactidose" / "guided" / "runner.py").read_text(encoding="utf-8")
    assert "buzzer_on(" not in src and "buzzer_off(" not in src      # no device calls
    assert "AudioContext" not in src and "LaptopToneBuzzer" not in src and "SerialBuzzer" not in src
    assert not re.search(r"\bhardware\.(?!buzzer)", src)              # no hardware access at all
    assert "self.buzzer.on(" in src and "self.buzzer.off()" in src


# =========================================================================== protocol + simulator


def test_protocol_buzzer_commands():
    assert p.Command.buzzer_on(500).to_line() == "BUZZER ON 500"
    assert p.Command.buzzer_off().to_line() == "BUZZER OFF" and p.Command.buzzer_query().to_line() == "BUZZER"
    for bad in (0, 65536, -1, True, 1.5):
        with pytest.raises(p.ProtocolError):
            p.Command.buzzer_on(bad)  # type: ignore[arg-type]
    with pytest.raises(p.ProtocolError):
        p.Command(p.CommandName.PING, args=("X",))
    assert p.parse_command("buzzer on 0500").command == p.Command(p.CommandName.BUZZER, args=("ON", "0500"))
    for bad in ("BUZZER ON", "BUZZER ON 0", "BUZZER ON 65536", "BUZZER ON 123456", "BUZZER OFF 1", "BUZZER X"):
        assert p.parse_command(bad).error is p.Err.UNKNOWN_COMMAND, bad
    cmd = p.Command.buzzer_on(5)
    assert p.classify(cmd, p.parse_message("OK BUZZER ON 5")) is p.Disposition.SUCCESS
    assert p.classify(cmd, p.parse_message("ERR NO_BUZZER")) is p.Disposition.FAILURE
    assert p.classify(cmd, p.parse_message("ERR UNKNOWN_COMMAND")) is p.Disposition.FAILURE
    assert p.PROTOCOL_VERSION == "1.1"                                  # no version bump


def _booted(**config: Any) -> VirtualESP32:
    esp = VirtualESP32(SimConfig(**config))
    esp.boot()
    esp.tick(10_000)
    esp.drain_output()
    return esp


def _send(esp: VirtualESP32, line: str, ms: int = 5) -> list[str]:
    esp.feed_line(line)
    esp.tick(ms)
    return esp.drain_output()


def test_simulator_buzzer_max_stop_reboot_and_status_unchanged():
    esp = _booted()
    status_before = _send(esp, "STATUS")
    assert _send(esp, "BUZZER ON 65535") == ["OK BUZZER ON 10000"]
    assert esp.physical()["buzzer_on"] is True
    esp.tick(10_000)
    assert esp.physical()["buzzer_on"] is False and _send(esp, "BUZZER") == ["OK BUZZER OFF"]
    _send(esp, "BUZZER ON 5000")
    assert _send(esp, "STOP") == ["OK STOPPED"] and esp.physical()["buzzer_on"] is False
    _send(esp, "HOME", 10_000)
    _send(esp, "BUZZER ON 5000")
    esp.boot()
    esp.tick(10_000)
    esp.drain_output()
    assert esp.physical()["buzzer_on"] is False
    assert _send(esp, "STATUS") == status_before                      # STATUS line unchanged


def test_simulator_hard_max_holds_during_a_drop():
    esp = _booted()
    _send(esp, "BUZZER ON 300")
    esp.feed_line("DROP_SLOT 3")
    esp.tick(400)
    assert esp.physical()["buzzer_on"] is False                       # off at 300 ms, mid-move
    esp.tick(15_000)
    assert esp.drain_output()[-2:] == ["OK DROPPED 3", "OK READY"]


def test_simulator_buzzer_variants_and_faults():
    assert _send(_booted(buzzer=False), "BUZZER ON 5") == ["ERR UNKNOWN_COMMAND"]       # firmware without it
    assert _send(_booted(proto=None), "BUZZER ON 5") == ["ERR UNKNOWN_COMMAND"]         # v1 firmware
    assert _send(_booted(buzzer_fitted=False), "BUZZER ON 5") == ["ERR NO_BUZZER"]      # BUZZER_PIN -1
    esp = _booted()
    esp.buzzer_fault = "missing"
    assert _send(esp, "BUZZER") == ["ERR UNKNOWN_COMMAND"]
    esp.buzzer_fault = "no_pin"
    assert _send(esp, "BUZZER ON 5") == ["ERR NO_BUZZER"]
    esp.buzzer_fault = "unresponsive"
    assert _send(esp, "BUZZER ON 5", 200) == []
    esp.buzzer_fault = None
    assert _send(esp, "BUZZER ON 5") == ["OK BUZZER ON 5"]


# =========================================================================== client + simulator


@pytest.fixture
def sim_hw():
    from tactidose.hardware.serial_client import create_hardware

    settings = _settings(hardware_mode="sim", sim_speed=50, hw_boot_wait_s=0, num_slots=3)
    hw, sim = create_hardware(settings)
    hw.start()
    deadline = time.monotonic() + 30
    while not hw.snapshot().connected and time.monotonic() < deadline:
        time.sleep(0.05)
    yield hw, sim
    hw.close()


def test_client_and_serial_buzzer_against_the_simulator(sim_hw):
    hw, sim = sim_hw
    assert hw.buzzer_query().code == "BUZZER"
    b = SerialBuzzer(hw)
    assert b.on(2000) and b.hardware_active and sim.physical()["buzzer_on"]
    b.off()
    assert not sim.physical()["buzzer_on"]


def test_unresponsive_device_buzzer_never_blocks_the_drop(sim_hw):
    hw, sim = sim_hw
    sim.set_buzzer_fault("unresponsive")
    b = SerialBuzzer(hw)
    started = time.monotonic()
    assert b.on(2000) is True and b.tone_active                       # laptop tone instead
    assert time.monotonic() - started < buzzer_config.COMMAND_TIMEOUT_S * (2 + buzzer_config.RETRIES)
    sim.set_buzzer_fault(None)
    assert hw.drop_slot(0).code == "DROPPED"                          # the drop still works
    b.off()


# =========================================================================== guided demo + CLI


class RecordingBuzzer(NullBuzzer):
    name = "recording"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def on(self, ms: int | None = None) -> bool:
        self.calls.append("on")
        return True

    def off(self) -> None:
        self.calls.append("off")


def test_guided_demo_uses_the_injected_buzzer(tmp_path):
    from datetime import datetime

    from tactidose.app import build_services, create_app
    from tactidose.core.clock import Clock
    from tactidose.guided import GuidedDemoRunner
    from tests.test_api_support import TestClient

    settings = _settings(data_dir=tmp_path / "data", hardware_mode="sim", sim_speed=50, num_slots=3,
                         voice_enabled=False, tts_provider="none", agent_provider="rules", demo_mode=True,
                         hw_boot_wait_s=0, scheduler_tick_s=600, demo_pause_seconds=0, demo_buzzer_seconds=0,
                         demo_answer_timeout_s=5, timezone="America/Vancouver", session_ttl_hours=96)
    services = build_services(settings, clock=Clock("America/Vancouver", frozen_at=datetime(2026, 10, 5, 10, 30)))
    with TestClient(create_app(services=services)):
        deadline = time.monotonic() + 30
        while not services.hardware.snapshot().connected and time.monotonic() < deadline:
            time.sleep(0.05)
        buzzer = RecordingBuzzer()
        runner = GuidedDemoRunner(services, buzzer=buzzer)
        pid = services.device_patient_id()
        runner.start(pid, reset=True)
        for answer in ["yes", "yes", "fine", "no", "fine", "no", "fine"]:
            assert runner.wait_awaiting(pid, 30)
            runner.answer(pid, answer)
        assert runner.wait_done(pid, 60)
        state = runner.state(pid)
    assert state["results"][0]["outcome"] == "DROPPED"
    assert buzzer.calls[:2] == ["on", "off"] and buzzer.calls.count("on") == 1   # one YES slot
    assert state["buzzer_backend"] == "recording"


def test_buzzer_test_cli(capsys):
    from tactidose.__main__ import main

    assert main(["buzzer-test", "--backend", "serial", "--sim", "--ms", "300"]) == 0
    assert "device's buzzer was switched on" in capsys.readouterr().out
    assert main(["buzzer-test", "--backend", "none", "--ms", "50"]) == 0
