"""Protocol conformance runner (docs/SERIAL_PROTOCOL.md, scenarios in conformance.json).

One runner, three kinds of targets implementing :class:`ConformanceTarget`:

* ``tactidose.hardware.simulator.ConformanceSimTarget``      – Python VirtualESP32 (simulated time)
* ``tactidose.hardware.conformance_native.NativeTarget``     – native firmware core harness (simulated time)
* ``tactidose.hardware.selftest.SerialConformanceTarget``    – a real board (wall-clock time;
  only ``hardware_safe`` scenarios, no fault injection)

CLI::

    python -m tactidose.hardware.conformance --target sim
    python -m tactidose.hardware.conformance --target native --binary firmware/native/bin/harness
    python -m tactidose.hardware.conformance --target serial --port COM5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

SCENARIO_FILE = Path(__file__).with_name("conformance.json")
DEFAULT_WITHIN_MS = 5000
DEFAULT_BOOT_WITHIN_MS = 45000
PRESS_MS = 100
TICK_CHUNK_MS = 5


@runtime_checkable
class ConformanceTarget(Protocol):
    name: str
    supports_faults: bool     # boot dead/none, sensor, jam
    supports_buttons: bool    # press steps
    supports_boot: bool       # can (re)boot on demand

    def reset(self) -> None:
        """Fresh device: physics at the initial offset, sensor ok, jam off, not booted."""

    def boot(self, mode: str) -> None:
        """(Re)boot firmware with home-sensor mode ok|dead|none (keeps physical position)."""

    def send(self, line: str) -> None: ...

    def tick(self, ms: int) -> list[str]:
        """Advance (simulated or real) time by ``ms`` and return every device line emitted
        since the previous ``tick()`` call — including lines produced synchronously by
        ``boot()``/``send()``/``set_button()``."""

    def set_button(self, name: str, pressed: bool) -> None: ...
    def set_sensor(self, mode: str) -> None: ...
    def set_jam(self, on: bool) -> None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------- results


@dataclass
class StepResult:
    index: int
    step: dict[str, Any]
    ok: bool
    got: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    def describe(self) -> str:
        if self.skipped:
            return f"SKIP {self.name}: {self.skip_reason}"
        if self.ok:
            return f"PASS {self.name}"
        bad = next((s for s in self.steps if not s.ok), None)
        if bad is None:
            return f"FAIL {self.name}"
        return (
            f"FAIL {self.name} at step {bad.index} {json.dumps(bad.step)}\n"
            f"     got: {bad.got}\n     {bad.message}"
        )


# --------------------------------------------------------------------------- matching


def load_scenarios(path: Path | str | None = None) -> dict[str, Any]:
    return json.loads(Path(path or SCENARIO_FILE).read_text(encoding="utf-8"))


def _status_pairs(line: str) -> dict[str, str] | None:
    tokens = line.split()
    if len(tokens) < 2 or tokens[0].upper() != "OK" or tokens[1].upper() != "STATUS":
        return None
    out: dict[str, str] = {}
    for tok in tokens[2:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.lower()] = v
    return out


def match_expectation(expected: str, line: str) -> bool:
    if expected.startswith("re:"):
        return re.fullmatch(expected[3:], line) is not None
    if expected.startswith("status:"):
        pairs = _status_pairs(line)
        if pairs is None:
            return False
        for item in expected[len("status:"):].split(","):
            k, v = item.split("=", 1)
            if pairs.get(k.strip().lower(), "").upper() != v.strip().upper():
                return False
        return True
    return " ".join(line.split()) == expected


def _is_device_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("#", "!"))


# --------------------------------------------------------------------------- runner


class _Collector:
    def __init__(self, target: ConformanceTarget) -> None:
        self.target = target
        self.pending: list[str] = []

    def _tick(self, ms: int) -> None:
        for line in self.target.tick(ms):
            line = line.rstrip("\r\n")
            if _is_device_line(line):
                self.pending.append(" ".join(line.split()))

    def collect(self, count: int, within_ms: int) -> list[str]:
        elapsed = 0
        while len(self.pending) < count and elapsed < within_ms:
            step = min(TICK_CHUNK_MS, within_ms - elapsed)
            self._tick(step)
            elapsed += step
        got, self.pending = self.pending[:count], self.pending[count:]
        return got

    def advance_exact(self, ms: int) -> None:
        elapsed = 0
        while elapsed < ms:
            step = min(TICK_CHUNK_MS, ms - elapsed)
            self._tick(step)
            elapsed += step

    def take_all(self) -> list[str]:
        got, self.pending = self.pending, []
        return got


def scenario_skip_reason(target: ConformanceTarget, scenario: dict[str, Any], *, include_slow: bool) -> str | None:
    tags = set(scenario.get("tags", []))
    steps = scenario["steps"]
    if "fault" in tags and not target.supports_faults:
        return "target cannot inject faults"
    if "buttons" in tags and not target.supports_buttons:
        return "target cannot press buttons"
    if "slow" in tags and not include_slow:
        return "slow scenario not requested"
    if not target.supports_faults and any(
        ("sensor" in s) or ("jam" in s) or (s.get("boot") not in (None, "ok")) for s in steps
    ):
        return "scenario needs fault injection"
    if not target.supports_boot and not scenario.get("hardware_safe", False):
        return "scenario is not hardware_safe"
    return None


def run_scenario(target: ConformanceTarget, scenario: dict[str, Any]) -> ScenarioResult:
    result = ScenarioResult(name=scenario["name"], ok=True)
    target.reset()
    col = _Collector(target)
    for i, step in enumerate(scenario["steps"]):
        expect: list[str] = list(step.get("expect", []))
        within = int(step.get("within_ms", DEFAULT_BOOT_WITHIN_MS if "boot" in step else DEFAULT_WITHIN_MS))
        quiet = int(step.get("quiet_ms", 0))
        pure_wait = "wait_ms" in step and not any(k in step for k in ("boot", "send", "press", "sensor", "jam"))
        try:
            if "boot" in step:
                target.boot(step["boot"])
            elif "send" in step:
                target.send(step["send"])
            elif "press" in step:
                target.set_button(step["press"], True)
                col.advance_exact(PRESS_MS)
                target.set_button(step["press"], False)
            elif "sensor" in step:
                target.set_sensor(step["sensor"])
            elif "jam" in step:
                target.set_jam(bool(step["jam"]))

            if pure_wait and not expect:
                col.advance_exact(int(step["wait_ms"]))
                got = col.take_all()
            elif pure_wait:
                got = col.collect(len(expect), int(step["wait_ms"]))
            elif expect:
                got = col.collect(len(expect), within)
            else:
                got = []
            ok = len(got) == len(expect) and all(match_expectation(e, g) for e, g in zip(expect, got))
            msg = "" if ok else f"expected {expect}"
            if ok and quiet:
                col.advance_exact(quiet)
                extra = col.take_all()
                if extra:
                    ok, msg = False, f"unexpected extra lines within quiet_ms={quiet}: {extra}"
                    got = got + extra
            if ok and not expect and not pure_wait and col.pending:
                ok, msg = False, f"unexpected lines: {col.pending}"
                got = col.take_all()
        except Exception as exc:  # noqa: BLE001 - report harness errors as failures
            ok, got, msg = False, [], f"harness error: {type(exc).__name__}: {exc}"
        result.steps.append(StepResult(i, step, ok, got, msg))
        if not ok:
            result.ok = False
            break
    return result


def run_all(
    target: ConformanceTarget,
    *,
    names: Iterable[str] | None = None,
    include_slow: bool = True,
    path: Path | str | None = None,
) -> list[ScenarioResult]:
    data = load_scenarios(path)
    wanted = set(names) if names else None
    out: list[ScenarioResult] = []
    for sc in data["scenarios"]:
        if wanted is not None and sc["name"] not in wanted:
            continue
        reason = scenario_skip_reason(target, sc, include_slow=include_slow)
        if reason:
            out.append(ScenarioResult(sc["name"], ok=True, skipped=True, skip_reason=reason))
            continue
        out.append(run_scenario(target, sc))
    return out


# --------------------------------------------------------------------------- CLI


def _make_target(args: argparse.Namespace) -> ConformanceTarget:
    if args.target == "sim":
        from tactidose.hardware.simulator import ConformanceSimTarget

        return ConformanceSimTarget()
    if args.target == "native":
        from tactidose.hardware.conformance_native import NativeTarget

        return NativeTarget(args.binary)
    if args.target == "serial":
        from tactidose.hardware.selftest import SerialConformanceTarget

        return SerialConformanceTarget(args.port, baud=args.baud)
    raise SystemExit(f"unknown target {args.target}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run TactiDose serial-protocol conformance scenarios.")
    ap.add_argument("--target", choices=["sim", "native", "serial"], default="sim")
    ap.add_argument("--binary", default="firmware/native/bin/harness", help="native harness executable")
    ap.add_argument("--port", default="auto", help="serial port for --target serial")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--scenario", action="append", help="run only this scenario (repeatable)")
    ap.add_argument("--no-slow", action="store_true", help="skip scenarios tagged slow")
    args = ap.parse_args(argv)
    target = _make_target(args)
    try:
        results = run_all(target, names=args.scenario, include_slow=not args.no_slow)
    finally:
        target.close()
    failed = [r for r in results if not r.ok]
    for r in results:
        print(r.describe())
    ran = sum(1 for r in results if not r.skipped)
    print(f"\n{ran - len(failed)}/{ran} scenarios passed, {sum(r.skipped for r in results)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
