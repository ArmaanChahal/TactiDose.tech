"""Shared protocol conformance suite (conformance.json) against the Python VirtualESP32."""

from __future__ import annotations

import pytest

from tactidose.hardware.conformance import (
    load_scenarios,
    main,
    run_all,
    run_scenario,
    scenario_skip_reason,
)
from tactidose.hardware.simulator import ConformanceSimTarget, SimConfig

SCENARIOS = load_scenarios()["scenarios"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_scenario_passes_on_simulator(scenario):
    target = ConformanceSimTarget()
    assert scenario_skip_reason(target, scenario, include_slow=True) is None
    result = run_scenario(target, scenario)
    assert result.ok, result.describe()


def test_run_all_runs_every_scenario_including_slow():
    results = run_all(ConformanceSimTarget(), include_slow=True)
    assert len(results) == len(SCENARIOS)
    assert [r.describe() for r in results if not r.ok or r.skipped] == []


def test_cli_reports_all_scenarios_passed(capsys):
    assert main(["--target", "sim"]) == 0
    out = capsys.readouterr().out
    assert f"{len(SCENARIOS)}/{len(SCENARIOS)} scenarios passed, 0 skipped" in out


def test_suite_detects_a_deviating_device():
    """Sanity check of the harness itself: a device reporting the wrong slot count fails."""
    scenario = next(s for s in SCENARIOS if s["name"] == "boot_homes_and_reports_ready")
    result = run_scenario(ConformanceSimTarget(SimConfig(num_slots=8)), scenario)
    assert not result.ok
    assert "slots=8" in " ".join(result.steps[-1].got)
