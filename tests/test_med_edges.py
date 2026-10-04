"""Edge cases of the v2 domain: races, partial failures and defensive paths (all fail closed)."""

from __future__ import annotations

import hashlib
import threading
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select

from tactidose.db.models import Compartment, Device, LabelScan, User
from tactidose.hardware.protocol import (
    CommandName,
    CommandResult,
    DeviceState,
    HostCode,
    Ok,
    parse_message,
)
from tactidose.medication.compartments import CompartmentService
from tactidose.medication.errors import ConflictError, DomainError, NotFoundError, ValidationError
from tactidose.medication.onboarding import OnboardingService
from tests.fakes import FakeDropHardware, wait_until
from tests.test_med_support import (  # noqa: F401 - fixtures
    DISPENSED,
    DUE,
    HARDWARE_ERROR,
    MISSED,
    Env,
    FlakyDB,
    background,
    env,
    env_template,
    flaky,
)


def _dropped(cmd) -> CommandResult:
    return CommandResult(cmd, True, Ok.DROPPED.value, (parse_message(f"OK DROPPED {cmd.slot}"),))


# --------------------------------------------------------------------------- concurrency


def test_eight_concurrent_requests_drop_exactly_one_pill(env: Env):
    env.hw.hold(CommandName.DROP_SLOT)
    results: list[Any] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(slot: int) -> None:
        barrier.wait(timeout=10)
        r = env.manual(slot)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i % 3,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    try:
        assert wait_until(lambda: len(results) == 7, timeout=30)
        assert env.hw.holding.wait(10)
        assert all((r.status, r.reason) == ("DENIED", "IN_PROGRESS") for r in results)
        assert results[0].message == "Another pill is dropping right now. Please wait a moment."
    finally:
        env.hw.release()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 8 and [r.status for r in results].count("DROPPED") == 1
    assert len(env.drop_commands()) == 1
    rows = env.drop_rows()
    assert len(rows) == 8 and [r.status for r in rows].count("DROPPED") == 1      # every request is stored


def test_a_request_during_a_scheduled_round_is_in_progress(env: Env):
    env.travel("08:00")
    env.hw.hold(CommandName.DROP_SLOT)
    t, out = background(env.drops.run_scheduled_drops)
    try:
        assert env.hw.holding.wait(5)
        assert env.manual(1).reason == "IN_PROGRESS"
        assert env.drops.run_scheduled_drops() == 0                 # a second round never starts
    finally:
        env.hw.release()
    t.join(5)
    assert out["value"] == 1 and env.dose_0800().status == DISPENSED


def test_claim_rechecks_after_homing(env: Env):
    """A caregiver skips the dose while the dispenser homes: nothing is dropped."""
    env.travel("08:00")
    env.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    target = env.dose_0800().event_id

    def home_while_caregiver_skips(cmd):
        env.drops.skip_dose(target, note="skipped during homing")
        env.hw.set_state(state=DeviceState.READY, homed=True, slot=0)
        return CommandResult(cmd, True, Ok.HOMED.value)

    env.hw.script_fn(CommandName.HOME, home_while_caregiver_skips)
    assert env.drops.run_scheduled_drops() == 1
    assert env.drop_commands() == [] and env.event(target).status == "CANCELLED"
    assert env.drop_rows()[-1].reason == "NOT_ALLOWED"


def test_claim_conflict_on_the_dose_is_refused_without_a_drop(env: Env, monkeypatch: pytest.MonkeyPatch):
    import tactidose.medication.drops as drops_mod

    env.travel("08:00")
    monkeypatch.setattr(drops_mod, "cas_transition", lambda *a, **k: None)
    out = env.scheduled(env.dose_0800().event_id)
    assert (out.status, out.reason) == ("DENIED", "IN_PROGRESS") and env.hw.sent == []
    assert env.dose_0800().status == DUE


# --------------------------------------------------------------------------- database failures


def test_db_failure_means_no_hardware_command(env: Env, flaky: FlakyDB):
    flaky.fail = True
    out = env.manual(0)
    assert (out.status, out.reason, out.drop_id) == ("DENIED", "DB_ERROR", None)
    assert out.message == "I can't check the records right now, so no pill was dropped. Please try again in a moment."
    assert env.hw.sent == []
    flaky.fail = False
    assert env.drop_rows() == []


def test_db_failure_during_the_claim_sends_no_drop(env: Env, flaky: FlakyDB):
    env.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)

    def home_then_db_dies(cmd):
        env.hw.set_state(state=DeviceState.READY, homed=True, slot=0)
        flaky.fail = True                                         # the database goes away while homing
        return CommandResult(cmd, True, Ok.HOMED.value)

    env.hw.script_fn(CommandName.HOME, home_then_db_dies)
    out = env.manual(0)
    assert out.reason == "DB_ERROR" and env.hw.sent == ["HOME"] and env.drop_commands() == []


def test_unrecorded_drop_blocks_further_drops_until_it_is_written(env: Env, flaky: FlakyDB):
    def drop_then_db_dies(cmd):
        flaky.fail = True
        return _dropped(cmd)

    env.hw.script_fn(CommandName.DROP_SLOT, drop_then_db_dies)
    sub = env.subscribe("system.notice")
    out = env.manual(0)
    assert (out.status, out.reason) == ("DROPPED", "UNRECORDED")         # the pill did drop: say so
    assert out.message == ("Vitamin C (demo candy) dropped from container 1. The record could not be saved yet, "
                           "so no more pills will drop until it is.")
    assert env.drops.has_unrecorded_outcome and sub.drain()[0].data["level"] == "error"
    blocked = env.manual(1)
    assert (blocked.status, blocked.reason) == ("DENIED", "DB_ERROR")
    assert blocked.message.startswith("An earlier drop has not been saved yet")
    assert env.drops.run_scheduled_drops() == 0
    flaky.fail = False
    later = env.manual(1)                                              # flushes the pending outcome first
    assert later.reason == "COOLDOWN" and not env.drops.has_unrecorded_outcome
    row = env.drop(out.drop_id)
    assert (row.status, row.pill_count_after) == ("DROPPED", 19) and env.compartment(0).pill_count == 19
    assert env.drop_commands() == ["DROP_SLOT 0"]


def test_unrecorded_failure_is_written_later(env: Env, flaky: FlakyDB):
    def timeout_and_db_dies(cmd):
        flaky.fail = True
        return CommandResult.host_failure(cmd, HostCode.TIMEOUT)

    env.hw.script_fn(CommandName.DROP_SLOT, timeout_and_db_dies)
    out = env.manual(0)
    assert (out.status, out.reason) == ("UNCERTAIN", "TIMEOUT") and "could not be saved" in out.message
    flaky.fail = False
    assert env.manual(1).reason == "NEEDS_REVIEW"
    assert env.drop(out.drop_id).needs_review


def test_outcome_not_applied_when_recovery_closed_the_row_first(env: Env):
    """Another process's startup recovery closes our in-flight row mid-drop: its review lock wins."""
    from tactidose.medication.drops import DropService

    env.hw.hold(CommandName.DROP_SLOT)
    t, out = background(lambda: env.manual(0))
    try:
        assert env.hw.holding.wait(5)
        other = DropService(env.db, FakeDropHardware(), env.clock, env.settings)
        assert other.recover_on_startup() == 1
    finally:
        env.hw.release()
    t.join(5)
    res = out["value"]
    assert (res.status, res.reason) == ("UNCERTAIN", "RESTART")
    row = env.drop(res.drop_id)
    assert row.needs_review and env.compartment(0).pill_count == 20
    assert env.devlog("DROP_OUTCOME_NOT_APPLIED")


# --------------------------------------------------------------------------- interrupt / STOP


def test_interrupt_during_a_held_drop_fails_without_cooldown_or_inventory_change(env: Env):
    env.hw.hold(CommandName.DROP_SLOT)
    t, out = background(lambda: env.manual(0))
    assert env.hw.holding.wait(5)
    assert env.drops.interrupt() is True
    t.join(5)
    res = out["value"]
    assert (res.status, res.reason, res.message) == ("FAILED", "STOPPED", "Stopped. No pill was dropped.")
    assert res.cooldown_remaining_s == 0 and res.next_allowed_at is None
    assert env.compartment(0).pill_count == 20 and env.hw.sent == ["DROP_SLOT 0", "STOP"]
    assert env.drops.patient_status(env.patient).cooldown_remaining_s == 0
    assert env.dose_0800().status == DUE
    again = env.manual(0)                                            # allowed at once; the device re-homes
    assert again.status == "DROPPED" and env.hw.sent[-2:] == ["HOME", "DROP_SLOT 0"]
    assert env.note_kinds(env.patient) == ["DROP_FAILED", "PILL_DROPPED"]


def test_interrupt_when_idle_sends_nothing(env: Env):
    assert env.drops.interrupt() is False and env.hw.sent == []


def test_interrupt_while_homing_before_the_drop(env: Env):
    env.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    env.hw.hold(CommandName.HOME)
    t, out = background(lambda: env.manual(0))
    assert env.hw.holding.wait(5)
    assert env.drops.interrupt() is True
    t.join(5)
    res = out["value"]
    assert (res.status, res.reason) == ("FAILED", "STOPPED")
    assert env.hw.sent == ["HOME", "STOP"] and env.drop_commands() == []
    assert env.drop(res.drop_id).hardware_result == "ERR STOPPED"


def test_interrupt_while_waiting_for_homing(env: Env, monkeypatch: pytest.MonkeyPatch):
    env.hw.set_state(state=DeviceState.HOMING, homed=False)
    calls = {"n": 0}
    real = env.hw.snapshot

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(env.hw, "snapshot", counting)
    t, out = background(lambda: env.manual(0))
    assert wait_until(lambda: calls["n"] >= 4, timeout=5)            # inside the HOMING wait loop
    assert env.drops.interrupt() is True                             # HOMING is motion: STOP
    t.join(5)
    assert out["value"].reason == "STOPPED" and env.hw.sent == ["STOP"]


def test_interrupt_between_claim_and_drop_sends_nothing(env: Env, monkeypatch: pytest.MonkeyPatch):
    real = env.drops._claim

    def claim_then_stop(req):
        ctx = real(req)
        env.drops._interrupt.set()                                  # STOP arrived right after the claim
        return ctx

    monkeypatch.setattr(env.drops, "_claim", claim_then_stop)
    out = env.manual(0)
    assert (out.status, out.reason) == ("FAILED", "STOPPED") and env.drop_commands() == []
    assert env.drop(out.drop_id).hardware_result == "ERR STOPPED (stopped before sending)"


def test_failed_stop_does_not_corrupt_the_outcome(env: Env, monkeypatch: pytest.MonkeyPatch):
    env.hw.hold(CommandName.DROP_SLOT)

    def broken_stop():
        raise RuntimeError("serial write failed")

    monkeypatch.setattr(env.hw, "stop", broken_stop)
    t, out = background(lambda: env.manual(0))
    try:
        assert env.hw.holding.wait(5)
        assert env.drops.interrupt() is False                        # STOP could not be sent
    finally:
        env.hw.release()
    t.join(5)
    assert out["value"].status == "DROPPED"


# --------------------------------------------------------------------------- internal errors


def test_a_bug_after_the_claim_locks_the_drop_for_review(env: Env, monkeypatch: pytest.MonkeyPatch):
    def bug(_slot: int):
        raise RuntimeError("bug")

    monkeypatch.setattr(env.drops, "_send_drop", bug)
    out = env.manual(0)
    assert (out.status, out.reason) == ("UNCERTAIN", "INTERNAL_ERROR")
    row = env.drop(out.drop_id)
    assert row.needs_review and row.hardware_result == "UNCERTAIN INTERNAL_ERROR"
    monkeypatch.undo()
    assert env.manual(1).reason == "NEEDS_REVIEW"


def test_a_bug_before_the_claim_is_reported_without_a_drop(env: Env, monkeypatch: pytest.MonkeyPatch):
    def bug():
        raise RuntimeError("bug")

    monkeypatch.setattr(env.drops, "_ready_now", bug)
    out = env.manual(0)
    assert (out.status, out.reason) == ("DENIED", "DB_ERROR") and out.message.startswith("Something went wrong")
    assert env.hw.sent == []
    monkeypatch.undo()
    assert env.manual(0).status == "DROPPED"                         # the lock was released


def test_a_bug_while_recording_after_the_claim_is_uncertain(env: Env, monkeypatch: pytest.MonkeyPatch):
    def bug(*_a, **_k):
        raise RuntimeError("bug while recording")

    monkeypatch.setattr(env.drops, "_finalise", bug)
    out = env.manual(0)
    assert (out.status, out.reason) == ("UNCERTAIN", "INTERNAL_ERROR") and out.drop_id is not None
    monkeypatch.undo()
    assert env.manual(1).reason == "IN_PROGRESS"                    # its row is still in flight: blocked


# --------------------------------------------------------------------------- hardware edge cases


def test_waits_for_homing_to_finish(env: Env):
    env.hw.set_state(state=DeviceState.HOMING, homed=False)
    timer = threading.Timer(0.05, lambda: env.hw.set_state(state=DeviceState.READY, homed=True, slot=0))
    timer.start()
    try:
        assert env.manual(0).status == "DROPPED"
    finally:
        timer.cancel()
    assert env.hw.sent == ["DROP_SLOT 0"]


@pytest.mark.parametrize("final", [{"state": DeviceState.FAULT, "homed": False}, {"connected": False}])
def test_homing_that_ends_badly(env: Env, final):
    env.hw.set_state(state=DeviceState.HOMING, homed=False)
    timer = threading.Timer(0.05, lambda: env.hw.set_state(**final))
    timer.start()
    try:
        out = env.manual(0)
    finally:
        timer.cancel()
    assert out.reason == "DEVICE_UNAVAILABLE" and env.hw.sent == []


def test_snapshot_failure_fails_closed(env: Env, monkeypatch: pytest.MonkeyPatch):
    def broken():
        raise RuntimeError("reader thread died")

    monkeypatch.setattr(env.hw, "snapshot", broken)
    assert env.manual(0).reason == "DEVICE_UNAVAILABLE"
    assert env.drops.interrupt() is False and env.hw.sent == []
    assert env.drops.patient_status(env.patient).device == {}


def test_busy_local_is_retried_because_nothing_was_sent(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.BUSY_LOCAL)
    assert env.manual(0).status == "DROPPED"


def test_persistent_busy_local_fails_without_a_pill(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.BUSY_LOCAL, times=10_000)
    out = env.manual(0)
    assert (out.status, out.reason) == ("FAILED", "BUSY_LOCAL") and env.compartment(0).pill_count == 20


def test_slot_outside_the_hardware_range_is_refused(env: Env):
    env.hw.num_slots = 2
    out = env.manual(2)
    assert out.reason == "DEVICE_UNAVAILABLE"
    assert out.message == "Container 3 is not available on this dispenser right now."
    assert env.hw.sent == []
    assert env.manual(1).status == "DROPPED"


def test_ready_device_skips_the_pre_check_but_never_the_rules(env: Env, monkeypatch: pytest.MonkeyPatch):
    calls = {"check": 0}
    real = env.drops._check

    def counting(*a, **k):
        calls["check"] += 1
        return real(*a, **k)

    monkeypatch.setattr(env.drops, "_check", counting)
    assert env.manual(0).status == "DROPPED" and calls["check"] == 1     # checked once, in the claim
    assert env.manual(1).reason == "COOLDOWN" and calls["check"] == 2
    env.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    env.set_cooldown(0)
    assert env.manual(1).status == "DROPPED" and calls["check"] == 4     # not ready: checked before HOME too


def test_hardware_without_drop_support_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch):
    class Legacy:
        num_slots = 3

        def __init__(self, inner):
            self._inner = inner

        def snapshot(self):
            return self._inner.snapshot()

    env.drops.hardware = Legacy(env.hw)  # type: ignore[assignment]
    out = env.manual(0)
    assert out.reason == "DEVICE_UNAVAILABLE"
    assert out.message == "The dispenser is not connected, so no pill was dropped. Please ask your caregiver for help."


# --------------------------------------------------------------------------- bootstrap / scheduler / onboarding


def test_concurrent_bootstrap_creates_one_device(settings_v2, db_v2):
    services = [CompartmentService(db_v2, settings_v2) for _ in range(4)]
    barrier = threading.Barrier(len(services))
    errors: list[BaseException] = []

    def run(svc: CompartmentService) -> None:
        try:
            barrier.wait(timeout=5)
            svc.ensure_device()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(svc,)) for svc in services]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    with db_v2.session() as s:
        assert s.scalar(select(func.count()).select_from(User)) == 1
        assert s.scalar(select(func.count()).select_from(Device)) == 1
        assert s.scalar(select(func.count()).select_from(Compartment)) == 3


def test_schedule_edit_leaves_review_locked_and_closed_window_doses_alone(env: Env):
    today = env.dose_0800()
    env.set_event(today.event_id, status=HARDWARE_ERROR, needs_review=True, attempts=1)
    env.travel("10:30")
    other_schedule = env.event_at(env.sched(1), "13:00")
    env.scheduler.update_schedule(env.sched(0), time_of_day="09:00", patient_id=env.patient)
    assert env.event(today.event_id).status == HARDWARE_ERROR          # a caregiver decides, not the edit
    assert env.event_at(env.sched(0), "09:00") is None                 # possibly dropped today: no 2nd dose
    assert env.event(other_schedule.event_id).status == "SCHEDULED"
    assert env.event_at(env.sched(0), "09:00", date(2026, 10, 6)) is not None


def test_scan_survives_image_store_failure(env: Env, fake_extractor):
    env.settings.data_dir.mkdir(parents=True, exist_ok=True)
    (env.settings.data_dir / "label_scans").write_text("a file where the directory should be")
    image = b"\xff\xd8\xff" + b"label" * 10
    ob = OnboardingService(env.db, fake_extractor, env.catalog, env.settings, env.clock)
    out = ob.scan(image, "image/jpeg", patient_id=env.patient)
    assert out["status"] == "PENDING_REVIEW"
    with env.db.session() as s:
        row = s.get(LabelScan, out["scan_id"])
        assert row.image_path is None and row.image_sha256 == hashlib.sha256(image).hexdigest()
        assert row.user_id == env.patient


def test_domain_errors_carry_http_status():
    assert (ValidationError("bad").status_code, NotFoundError("x").status_code,
            ConflictError("y").status_code, DomainError("z").status_code) == (422, 404, 409, 400)
    assert str(ValidationError("Readable message.")) == "Readable message."
    assert ConflictError("c").message == "c"
    with pytest.raises(DomainError):
        raise NotFoundError("subclass of DomainError")


def test_missed_transition_without_notification_service(settings_v2, clock, bus, fake_drop_hw, env_template):
    from tests.test_med_support import from_template

    e = from_template(env_template, settings_v2, clock, bus, fake_drop_hw, with_notifications=False)
    try:
        e.travel("10:01")
        e.tick()
        assert e.dose_0800().status == MISSED and e.notes() == []
    finally:
        e.db.dispose()
