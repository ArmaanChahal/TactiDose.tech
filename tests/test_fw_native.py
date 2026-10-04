"""Reference firmware core (natively compiled) against the shared protocol conformance suite.

``firmware/native/bin/harness`` runs ``firmware/tactidose_esp32/TactiDoseCore.cpp`` (the code
flashed to the ESP32) on a simulated carousel. These tests:

* build the harness in Docker when it is missing or stale (skip with a reason when Docker or the
  gcc image is unavailable -- the tests never pull images or touch the network);
* run every scenario of ``tactidose/hardware/conformance.json`` against :class:`NativeTarget`;
* use the fake HAL as a physical safety oracle: after each scenario no physical rule may have been
  violated (carousel stepping with the gate open, gate opening between slots, early OK GATE_* ...);
* cover what the scenarios cannot see: exact rule-8.6 timeouts, millis() wrap-around, config
  variants, a stuck home sensor, gate-travel atomicity, line terminators, and that the firmware
  parser matches ``protocol.parse_command`` byte for byte.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from tactidose.hardware.conformance import load_scenarios, run_all, run_scenario, scenario_skip_reason
from tactidose.hardware.conformance_native import (
    DEFAULT_IMAGE,
    ENV_COMMAND,
    ENV_IMAGE,
    HarnessBuildError,
    HarnessError,
    NativeTarget,
    build_harness,
    docker_unavailable_reason,
    harness_is_stale,
    resolve_binary,
)
from tactidose.hardware.protocol import parse_command

pytestmark = pytest.mark.native

# Captured at import time: the autouse hermetic fixture removes TACTIDOSE_* variables per test.
_COMMAND = os.environ.get(ENV_COMMAND) or None
_IMAGE = os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE
_GCC_IMAGE = os.environ.get("TACTIDOSE_GCC_IMAGE") or DEFAULT_IMAGE

SCENARIOS: list[dict[str, Any]] = load_scenarios()["scenarios"]
BOOT_LINES = ["re:EVENT BOOT \\S+", "OK HOMING", "OK HOMED", "OK READY"]
WRAP = 1 << 32

#: Gate openings the physics must have seen at the end of a scenario (0 = gate never opened).
GATE_OPENS = {
    "stop_during_dispense_never_opens_gate": 0,
    "motor_jam_during_dispense_never_opens_gate": 0,
    "motor_jam_during_move_faults": 0,
    "busy_while_moving": 0,
    "stop_during_move_requires_rehome": 0,
    "cancel_button_during_move_stops": 0,
    "home_timeout_enters_fault_and_recovers": 0,
    "dispense_happy_path": 2,
    "interlocks_while_gate_open": 1,
    "cancel_button_with_gate_open_closes_gate": 1,
    "gate_auto_close_safety_net": 1,
}


# --------------------------------------------------------------------------- fixtures


def _linux_local_build() -> bool:
    return sys.platform.startswith("linux") and subprocess.run(
        ["sh", "-c", "command -v ${CXX:-g++}"], capture_output=True, check=False
    ).returncode == 0


def _require_docker_images(*images: str) -> None:
    reason = docker_unavailable_reason()
    if reason:
        pytest.skip(f"native firmware tests need Docker on this machine: {reason}")
    for image in dict.fromkeys(images):
        found = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False, timeout=60)
        if found.returncode != 0:
            pytest.skip(f"Docker image {image} is not present (tests never pull images); "
                        f"run firmware/native/build.ps1 or 'docker pull {image}' once")


@pytest.fixture(scope="session")
def harness_binary() -> Path:
    binary = resolve_binary()
    if _COMMAND is not None:  # user-supplied harness command: trust it
        return binary
    local = _linux_local_build()
    stale = harness_is_stale(binary)
    needed = ([] if sys.platform.startswith("linux") else [_IMAGE]) + ([_GCC_IMAGE] if stale and not local else [])
    if needed:
        _require_docker_images(*needed)
    if stale:
        try:
            build_harness(image=_GCC_IMAGE, local=local)
        except HarnessBuildError as exc:
            pytest.fail(f"the firmware core / native harness does not compile:\n{exc}")
    return binary


def _target(binary: Path, **kwargs: Any) -> NativeTarget:
    return NativeTarget(binary, command=_COMMAND, image=_IMAGE, **kwargs)


@pytest.fixture(scope="module")
def native(harness_binary: Path) -> Iterator[NativeTarget]:
    target = _target(harness_binary)
    yield target
    target.close()


# --------------------------------------------------------------------------- helpers


class Session:
    """Drives a NativeTarget directly; keeps (time_ms, line) for every device line.

    Like the conformance runner, lines that have not been returned yet carry over: ``run`` and
    ``wait`` return everything after the last line they returned. A line is stamped with the
    time at the end of the tick that delivered it (lines caused synchronously by ``send`` are
    stamped 1 ms after the send)."""

    def __init__(self, target: NativeTarget) -> None:
        self.t = target
        self.log: list[tuple[int, str]] = []
        self._cursor = 0

    def start(self, mode: str = "ok", *, wait: bool = True) -> None:
        self.t.reset()
        self.log.clear()
        self._cursor = 0
        self.t.boot(mode)
        if wait:
            got = self.wait(4, 45000)
            assert got[0].startswith("EVENT BOOT ") and got[1:] == ["OK HOMING", "OK HOMED", "OK READY"], got

    def run(self, ms: int, step: int = 1) -> list[str]:
        """Advance exactly ``ms``; return every line not returned before."""
        self._advance(ms, step)
        return self._take(len(self.log))

    def wait(self, count: int, within_ms: int, step: int = 1) -> list[str]:
        """Advance until ``count`` unreturned lines exist (or ``within_ms`` elapsed); return them."""
        elapsed = 0
        while len(self.log) - self._cursor < count and elapsed < within_ms:
            self._tick(step)
            elapsed += step
        return self._take(min(len(self.log), self._cursor + count))

    def send(self, line: str) -> int:
        """Send a line; returns the simulated time of delivery."""
        sent_at = self.t.now_ms
        self.t.send(line)
        return sent_at

    def press(self, name: str, hold_ms: int = 100) -> None:
        self.t.set_button(name, True)
        self._advance(hold_ms, 1)
        self.t.set_button(name, False)

    def time_of(self, line: str) -> int:
        return next(t for t, seen in self.log if seen == line)

    def _take(self, end: int) -> list[str]:
        got = [line for _, line in self.log[self._cursor:end]]
        self._cursor = end
        return got

    def _advance(self, ms: int, step: int) -> None:
        for _ in range(ms // step):
            self._tick(step)

    def _tick(self, ms: int) -> None:
        for line in self.t.tick(ms):
            if not line.startswith("#"):
                self.log.append((self.t.now_ms, " ".join(line.split())))


class Recorder:
    """ConformanceTarget wrapper that records (time_ms, line) for every line tick() returns."""

    def __init__(self, target: NativeTarget) -> None:
        self.target = target
        self.name, self.supports_faults = target.name, True
        self.supports_buttons, self.supports_boot = True, True
        self.now = 0
        self.log: list[tuple[int, str]] = []

    def reset(self) -> None:
        self.now = 0
        self.target.reset()

    def boot(self, mode: str) -> None:
        self.target.boot(mode)

    def send(self, line: str) -> None:
        self.target.send(line)

    def tick(self, ms: int) -> list[str]:
        lines = self.target.tick(ms)
        self.now += ms
        self.log.extend((self.now, line) for line in lines)
        return lines

    def set_button(self, name: str, pressed: bool) -> None:
        self.target.set_button(name, pressed)

    def set_sensor(self, mode: str) -> None:
        self.target.set_sensor(mode)

    def set_jam(self, on: bool) -> None:
        self.target.set_jam(on)

    def close(self) -> None:
        self.target.close()


def _assert_physically_safe(target: NativeTarget) -> dict[str, str]:
    phys = target.physical()
    assert phys["violations"] == "0", f"physical safety rule violated: {phys['last_violation']} ({phys})"
    return phys


# --------------------------------------------------------------------------- NativeTarget client logic (no Docker)

#: Minimal stand-in harness: acknowledges every line, keeps a clock, reports every peek as silent.
_FAKE_HARNESS = """
import sys
t = 0
for line in sys.stdin:
    line = line.rstrip("\\n")
    if line.startswith("!tick "):
        t += int(line.split()[1])
    elif line.startswith("!peek "):
        print("!peek " + line.split()[1])
    elif line == "> PING":
        print("OK PONG")
    print("!ack %d" % t, flush=True)
    if line == "!quit":
        break
"""


def test_missing_binary_explains_how_to_build(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="build.ps1"):
        NativeTarget(tmp_path / "no-harness-here")


def test_custom_command_and_speculative_bookkeeping(tmp_path: Path) -> None:
    script = tmp_path / "fake_harness.py"
    script.write_text(_FAKE_HARNESS, encoding="utf-8")
    with NativeTarget(command=[sys.executable, str(script)], timeout_s=20) as target:
        target.reset()
        target.send("PING")                      # reply is returned by the next tick()
        assert target.tick(5) == ["OK PONG"]
        trips = target.round_trips
        for _ in range(100):                     # inside the 1000 ms window the fake declared silent
            assert target.tick(5) == []
        assert target.round_trips == trips and target.now_ms == 505
        target.speculate = False
        assert target.tick(5) == [] and target.round_trips == trips + 1 and target.now_ms == 510


def test_command_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fake harness.py"          # a space in the path on purpose
    script.write_text(_FAKE_HARNESS, encoding="utf-8")
    line = subprocess.list2cmdline([sys.executable, str(script)]) if os.name == "nt" else \
        " ".join(__import__("shlex").quote(p) for p in (sys.executable, str(script)))
    monkeypatch.setenv(ENV_COMMAND, line)
    with NativeTarget(tmp_path / "unused-binary", timeout_s=20) as target:
        target.reset()
        target.send("PING")
        assert target.tick(1) == ["OK PONG"]


def test_unresponsive_harness_times_out_and_is_discarded(tmp_path: Path) -> None:
    script = tmp_path / "mute.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    target = NativeTarget(command=[sys.executable, str(script)], timeout_s=1, startup_timeout_s=1)
    with pytest.raises(HarnessError, match="did not acknowledge"):
        target.reset()
    assert target._proc is None                  # killed, not left running
    target.close()


def test_exiting_harness_reports_its_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "crash.py"
    script.write_text("import sys\nsys.stdin.readline()\nsys.stderr.write('boom\\n')\nsys.exit(3)\n",
                      encoding="utf-8")
    target = NativeTarget(command=[sys.executable, str(script)], timeout_s=20, startup_timeout_s=20)
    with pytest.raises(HarnessError, match="exited"):
        target.reset()
    target.close()


# --------------------------------------------------------------------------- the conformance suite


def test_native_target_supports_every_scenario() -> None:
    skipped = [s["name"] for s in SCENARIOS if scenario_skip_reason(NativeTarget, s, include_slow=True)]
    assert not skipped and len(SCENARIOS) >= 20


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_conformance_scenario(native: NativeTarget, scenario: dict[str, Any]) -> None:
    result = run_scenario(native, scenario)
    assert result.ok, result.describe()
    phys = _assert_physically_safe(native)
    if scenario["name"] in GATE_OPENS:
        assert int(phys["gate_opens"]) == GATE_OPENS[scenario["name"]], phys


@pytest.fixture
def configured(native: NativeTarget) -> Iterator[NativeTarget]:
    """The module's harness; settings changed with ``configure()`` are undone afterwards."""
    yield native
    native.restore_defaults()


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"millisOffset": WRAP - 15_000}, id="millis-wraps-15s-after-boot"),
        pytest.param({"millisOffset": WRAP - 60_000}, id="millis-wraps-60s-after-boot"),
        pytest.param({"homeBackoffSteps": 0}, id="single-pass-homing"),
        pytest.param({"homeOffsetSteps": 12}, id="home-offset-calibration"),
        pytest.param({"verifySlot": False, "debugLog": False, "holdWhenIdle": False},
                     id="no-slot-check-no-debug-release-when-idle"),
    ],
)
def test_full_suite_under_variants(configured: NativeTarget, settings: dict[str, Any]) -> None:
    """Wrap-safe timing (millis() wraps every 49.7 days) and config variants keep conformance."""
    configured.configure(**settings)
    failed = []
    for scenario in SCENARIOS:
        result = run_scenario(configured, scenario)
        phys = configured.physical()
        if not result.ok or phys["violations"] != "0":
            failed.append(f"{result.describe()} | violations={phys['violations']} {phys['last_violation']}")
    assert not failed, "\n".join(failed)


#: Short scenario mixing every kind of event, for the plain-vs-speculative cross-check.
CROSS_CHECK: dict[str, Any] = {
    "name": "cross_check",
    "steps": [
        {"boot": "none", "expect": BOOT_LINES},
        {"send": "DISPENSE_SLOT 1", "expect": ["OK MOVING 1"]},
        {"press": "CONFIRM", "expect": ["EVENT CONFIRM_BUTTON"]},
        {"send": "PING", "expect": ["OK PONG"]},
        {"wait_ms": 3000, "expect": ["OK AT_SLOT 1", "OK GATE_OPEN"]},
        {"press": "CANCEL", "expect": ["EVENT CANCEL_BUTTON", "OK GATE_CLOSED", "OK READY"]},
        {"send": "MOVE_SLOT 3", "expect": ["OK MOVING 3"]},
        {"wait_ms": 200, "expect": []},
        {"send": "STOP", "expect": ["ERR STOPPED", "OK STOPPED"]},
        {"send": "HOME", "expect": ["OK HOMING", "OK HOMED", "OK READY"]},
        {"send": "OPEN_GATE", "expect": ["OK GATE_OPEN"]},
        {"wait_ms": 1500, "expect": []},
        {"send": "STATUS", "quiet_ms": 300, "expect": ["status:state=GATE_OPEN,gate=OPEN"]},
    ],
}


def test_speculative_ticking_matches_plain_ticking(native: NativeTarget) -> None:
    """The !peek optimisation must not change a single line or the millisecond it is reported."""
    fast = Recorder(native)
    assert run_scenario(fast, CROSS_CHECK).ok
    native.speculate = False
    try:
        plain = Recorder(native)
        trips = native.round_trips
        assert run_scenario(plain, CROSS_CHECK).ok
        plain_trips = native.round_trips - trips
    finally:
        native.speculate = True
    assert fast.log == plain.log and fast.now == plain.now
    assert plain_trips > 500  # really ticked one chunk at a time


def test_harness_rejects_unknown_settings_and_inputs(configured: NativeTarget) -> None:
    with pytest.raises(HarnessError, match="unknown key"):
        configured.configure(noSuchSetting=1)
    with pytest.raises(ValueError):
        configured.send("PING\nPING")
    with pytest.raises(ValueError):
        configured.boot("sideways")
    configured.reset()  # still in sync after the rejected input
    configured.boot("ok")
    assert any(line.startswith("EVENT BOOT ") for line in configured.tick(1000))


def test_run_all_api(native: NativeTarget) -> None:
    results = run_all(native, names=["dispense_happy_path", "gate_auto_close_safety_net"])
    assert [r.ok for r in results] == [True, True] and not any(r.skipped for r in results)


# --------------------------------------------------------------------------- parser equivalence


def _fuzz_lines(seed: int = 20261003, count: int = 1500) -> list[str]:
    rng = random.Random(seed)
    words = ["PING", "STATUS", "HOME", "MOVE_SLOT", "DISPENSE_SLOT", "OPEN_GATE", "CLOSE_GATE", "STOP",
             "move_slot", "Dispense_Slot", "pInG", "MOVE_SLOTS", "OPEN", "GATE", "FOO", "PING\x00", "STOP!"]
    args = ["0", "1", "5", "6", "9", "11", "12", "00", "002", "005", "006", "099", "000", "0000", "1000",
            "-1", "+1", "1.0", "x", "abc", "0x1", "1e1", "\x001", "", "2 3"]
    spaces = [" ", "  ", "\t", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", " \t ", ""]
    alphabet = "".join(chr(c) for c in range(0x00, 0x80) if chr(c) not in "\r\n")
    lines = ["", " ", "\t\t", "PING", "P" * 64, "P" * 65, "PING" + " " * 60, "PING" + " " * 61,
             "MOVE_SLOT 1" + " " * 53, "MOVE_SLOT 1" + " " * 54, "\x1fSTATUS\x1c"]
    while len(lines) < count:
        kind = rng.random()
        if kind < 0.7:
            parts = [rng.choice(spaces), rng.choice(words)]
            for _ in range(rng.choice([0, 1, 1, 1, 2])):
                parts += [rng.choice(spaces[:-1]), rng.choice(args)]
            parts.append(rng.choice(spaces))
            lines.append("".join(parts))
        elif kind < 0.9:
            lines.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 70))))
        else:
            word = rng.choice(words)
            lines.append(word + " " * rng.randint(55, 70 - len(word)) + rng.choice(["", "1", "x"]))
    return lines


def _python_verdict(line: str, num_slots: int) -> str:
    p = parse_command(line, num_slots)
    if p.empty:
        return "empty"
    if p.command is not None:
        slot = "-" if p.command.slot is None else str(p.command.slot)
        return f"ok {p.command.name.value} {slot}"
    assert p.error is not None
    return f"err {p.error.value} {p.name.value if p.name else '-'}"


@pytest.mark.parametrize("num_slots", [6, 2, 12])
def test_firmware_parser_matches_protocol_parse_command(native: NativeTarget, num_slots: int) -> None:
    lines = _fuzz_lines(seed=num_slots)
    firmware = native.parse_lines(lines, num_slots=num_slots)
    mismatches = [(line, fw, _python_verdict(line, num_slots))
                  for line, fw in zip(lines, firmware) if fw != _python_verdict(line, num_slots)]
    assert not mismatches, mismatches[:10]


# --------------------------------------------------------------------------- behaviour beyond the scenarios


def test_boot_closes_gate_before_event_boot(native: NativeTarget) -> None:
    s = Session(native)
    native.reset()
    native.boot("ok")
    assert s.run(399) == []                      # gate travel (GATE_TRAVEL_MS=400) comes first
    assert s.wait(2, 10)[0].startswith("EVENT BOOT ")
    assert native.physical()["gate"] == "closed"
    _assert_physically_safe(native)


@pytest.mark.parametrize("slot,limit_ms", [(3, 5000), (1, 3632)])
def test_motion_timeout_is_twice_expected_plus_two_seconds(native: NativeTarget, slot: int, limit_ms: int) -> None:
    """Rule 8.6 with 1600 steps/s, 3200 steps/s^2: slot 3 = 1600 steps = 1.5 s, slot 1 = 533 steps = 0.816 s."""
    s = Session(native)
    s.start()
    native.set_jam(True)
    sent_at = s.send(f"MOVE_SLOT {slot}")
    assert s.wait(2, 30000) == [f"OK MOVING {slot}", "ERR MOTOR_FAULT"]
    took = s.time_of("ERR MOTOR_FAULT") - sent_at
    assert limit_ms <= took <= limit_ms + 1, took
    phys = _assert_physically_safe(native)
    assert phys["fw_state"] == "FAULT" and phys["driver"] == "0" and phys["gate"] == "closed"


def test_dead_sensor_gives_up_after_one_and_a_quarter_turns(native: NativeTarget) -> None:
    s = Session(native)
    native.reset()
    start = int(native.physical()["pos"])
    native.boot("dead")
    assert s.wait(3, 45000)[1:] == ["OK HOMING", "ERR HOME_TIMEOUT"]
    travelled = int(native.physical()["pos"]) - start
    assert travelled == 4000                      # max travel (1.25 x 3200), well before HOME_TIMEOUT_MS
    assert s.time_of("ERR HOME_TIMEOUT") < 15000


def test_jammed_homing_times_out_on_the_clock(native: NativeTarget) -> None:
    s = Session(native)
    native.reset()
    native.set_jam(True)
    native.boot("ok")
    assert s.wait(3, 45000)[1:] == ["OK HOMING", "ERR HOME_TIMEOUT"]
    took = s.time_of("ERR HOME_TIMEOUT") - s.time_of("OK HOMING")
    assert 20000 <= took <= 20002, took          # no travel at all: HOME_TIMEOUT_MS decides


def test_stuck_home_sensor_is_never_accepted_as_home(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    native.set_sensor("stuck")                    # always "active", e.g. shorted hall sensor
    s.send("HOME")
    assert s.wait(2, 45000) == ["OK HOMING", "ERR HOME_TIMEOUT"]
    s.send("STATUS")
    assert "state=FAULT homed=0" in s.wait(1, 100)[0]
    s.send("DISPENSE_SLOT 2")
    assert s.wait(1, 100) == ["ERR NOT_HOMED"]
    assert native.physical()["gate_opens"] == "0"


def test_cancel_pressed_during_gate_travel_is_not_lost(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    s.send("DISPENSE_SLOT 1")
    assert s.wait(2, 25000) == ["OK MOVING 1", "OK AT_SLOT 1"]
    s.run(300 + 50)                               # settle, then 50 ms into the 400 ms opening travel
    assert native.physical()["gate"] == "moving"
    s.press("CANCEL", hold_ms=60)                 # short press, entirely inside the travel
    assert s.wait(4, 5000) == ["OK GATE_OPEN", "EVENT CANCEL_BUTTON", "OK GATE_CLOSED", "OK READY"]
    _assert_physically_safe(native)


def test_serial_input_waits_for_gate_travel(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    s.send("OPEN_GATE")
    s.send("PING")
    s.send("STATUS")
    got = s.wait(3, 2000)
    assert got[:2] == ["OK GATE_OPEN", "OK PONG"] and "state=GATE_OPEN" in got[2]
    assert s.time_of("OK PONG") - s.time_of("OK GATE_OPEN") <= 1
    s.send("CLOSE_GATE")
    assert s.wait(2, 2000) == ["OK GATE_CLOSED", "OK READY"]


def test_status_reports_slot_per_state(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    s.send("DISPENSE_SLOT 4")
    assert s.wait(1, 100) == ["OK MOVING 4"]
    s.send("STATUS")
    assert "state=MOVING homed=1 slot=-1 gate=CLOSED" in s.wait(1, 100)[0]
    assert s.wait(1, 25000) == ["OK AT_SLOT 4"]
    s.send("STATUS")
    assert "state=AT_TARGET homed=1 slot=4 gate=CLOSED" in s.wait(1, 100)[0]
    assert s.wait(1, 5000) == ["OK GATE_OPEN"]
    phys = _assert_physically_safe(native)
    assert phys["phys_slot"] == "4" and phys["gate"] == "open"


@pytest.mark.parametrize("slot", range(6))
def test_dispense_aligns_the_physical_compartment(native: NativeTarget, slot: int) -> None:
    s = Session(native)
    s.start()
    s.send(f"DISPENSE_SLOT {slot}")
    assert s.wait(3, 25000) == [f"OK MOVING {slot}", f"OK AT_SLOT {slot}", "OK GATE_OPEN"]
    phys = _assert_physically_safe(native)
    assert phys["phys_slot"] == str(slot) and phys["gate"] == "open" and phys["driver"] == "1"


def test_auto_close_is_not_extended_by_repeated_open_gate(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    s.send("OPEN_GATE")
    assert s.wait(1, 1000) == ["OK GATE_OPEN"]
    opened = s.time_of("OK GATE_OPEN")
    s.run(60000, step=5)
    s.send("OPEN_GATE")                           # idempotent: immediate reply, timer keeps running
    assert s.wait(1, 10) == ["OK GATE_OPEN"]
    assert s.wait(2, 70000, step=5) == ["OK GATE_CLOSED", "OK READY"]
    closed = s.time_of("OK GATE_CLOSED")
    assert opened + 120000 + 400 <= closed <= opened + 120000 + 400 + 5


def test_line_terminators_and_length_limit(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    native.send_raw(b"PING\r")                    # CR alone terminates a line
    native.send_raw(b"PI")
    native.send_raw(b"NG\r\n")                    # split delivery, CRLF -> one reply
    native.send_raw(b"\n\n\r\n")                  # blank lines: no reply
    native.send_raw(b"PING" + b" " * 60 + b"\n")  # exactly 64 characters
    native.send_raw(b"PING" + b" " * 61 + b"\n")  # 65 characters -> discarded
    native.send_raw(b"X" * 300 + b"\nPING\n")     # long garbage: one error, then recovery
    got = s.wait(6, 500)
    assert got == ["OK PONG", "OK PONG", "OK PONG", "ERR UNKNOWN_COMMAND", "ERR UNKNOWN_COMMAND", "OK PONG"]
    assert s.run(300) == []


def test_button_held_through_reset_does_not_fire(native: NativeTarget) -> None:
    s = Session(native)
    native.reset()
    native.set_button("CONFIRM", True)
    native.boot("ok")
    assert all(not line.startswith("EVENT CONFIRM") for line in s.wait(4, 45000))
    native.set_button("CONFIRM", False)
    assert s.run(200) == []
    s.press("CONFIRM")
    assert s.wait(1, 500) == ["EVENT CONFIRM_BUTTON"]


def test_button_bounce_shorter_than_debounce_is_ignored(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    bounced: list[str] = []
    for _ in range(5):                            # 20 ms blips < DEBOUNCE_MS (30 ms)
        s.press("CANCEL", hold_ms=20)
        bounced += s.run(20)
    assert bounced + s.run(300) == []
    s.press("CANCEL", hold_ms=40)
    assert s.wait(1, 200) == ["EVENT CANCEL_BUTTON"]


def test_no_firmware_output_before_boot(native: NativeTarget) -> None:
    s = Session(native)
    native.reset()
    s.send("PING")
    assert s.run(1000, step=5) == []
    assert native.physical()["booted"] == "0"


def test_stop_halts_the_carousel_immediately(native: NativeTarget) -> None:
    s = Session(native)
    s.start()
    s.send("MOVE_SLOT 3")
    s.run(700)                                    # cruising at full speed
    before = int(native.physical()["pos"])
    s.send("STOP")
    assert s.wait(2, 100) == ["ERR STOPPED", "OK STOPPED"]
    s.run(500)
    after = native.physical()
    assert int(after["pos"]) == before and after["fw_state"] == "SAFE_STOP" and after["fw_homed"] == "0"
