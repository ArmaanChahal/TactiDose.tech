"""DoseService: demo flows, duplicate prevention, concurrency, outcome mapping, hardware
preparation, interrupt/cancel/confirm/close, recovery, fail-closed DB handling, caregiver ops."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Any, Callable

import pytest

from tactidose.core.bus import Topic
from tactidose.core.interfaces import (
    BlockReason,
    CancelStatus,
    ConfirmStatus,
    DispenseStatus,
    DoseInfo,
    DoseServiceAPI,
    IntentSource,
)
from tactidose.db.models import LogCategory
from tactidose.hardware.protocol import CommandName, CommandResult, DeviceState, Err, GateState, HostCode, Ok
from tactidose.medication.dispense import UNRECORDED, DoseService, DueReport
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tests.fakes import FakeHardware, wait_until
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    DISPENSING,
    DUE,
    HARDWARE_ERROR,
    KIND_DEVICE_EVENT,
    MISSED,
    TAKEN,
    FlakyDB,
    Med,
    build,
    flaky,
    med,
    med_template,
)

V = IntentSource.VOICE


def background(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["value"] = fn()
        except BaseException as exc:  # pragma: no cover - surfaced by the test
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _device_events(m: Med) -> list[dict[str, Any]]:
    return [r.payload for r in m.outbox(KIND_DEVICE_EVENT)]


# --------------------------------------------------------------------------- demo flows


def test_dose_service_implements_protocol(med: Med):
    assert isinstance(med.dose, DoseServiceAPI)


def test_demo_flow_a_dispense_then_confirm(med: Med):
    sub = med.subscribe(Topic.DOSE_UPDATED)
    calls: list[DoseInfo] = []
    out = med.dose.dispense_next(V, on_motion_start=calls.append)
    assert out.status is DispenseStatus.DISPENSED and out.reason == "" and out.remaining_due == 0
    assert med.hw.sent == ["DISPENSE_SLOT 2"]
    assert [d.event_id for d in calls] == [out.dose.event_id] and calls[0].status == DISPENSING
    ev = med.event(out.dose.event_id)
    assert ev.status == DISPENSED and ev.dispensed_at == med.clock.now() and ev.attempts == 1
    assert ev.slot_number == 2 and ev.compartment_id == med.compartment(2).compartment_id
    assert ev.dispense_source == "voice" and ev.hardware_result == "OK GATE_OPEN"
    assert out.dose.status == DISPENSED and out.dose.slot == 2 and out.to_dict()["dose"]["compartment"] == "compartment 3"
    assert med.dose.awaiting_confirmation().event_id == ev.event_id

    confirm = med.dose.confirm_taken(IntentSource.BUTTON)
    assert confirm.status is ConfirmStatus.CONFIRMED and confirm.gate_closed is True
    assert confirm.hardware.code == Ok.GATE_CLOSED.value
    assert med.hw.sent == ["DISPENSE_SLOT 2", "CLOSE_GATE"]
    ev = med.event(ev.event_id)
    assert ev.status == TAKEN and ev.confirmed_taken_at == med.clock.now() and ev.confirm_source == "button"
    assert med.adherence_statuses(ev.event_id) == [DUE, DISPENSING, DISPENSED, TAKEN]
    assert [r.event for r in med.devlog() if r.event_id == ev.event_id and r.category == "DOSE"] == \
        ["DOSE_MATERIALIZED", "DOSE_DUE", "DOSE_DISPENSING", "DOSE_DISPENSED", "DOSE_TAKEN"]
    assert [e.data["change"] for e in sub.drain() if e.data["event_id"] == ev.event_id] == \
        ["dispensing", "dispensed", "taken"]
    assert med.dose.awaiting_confirmation() is None


def test_demo_flow_b_second_request_is_duplicate_without_motion(med: Med):
    med.dose.dispense_next(V)
    med.dose.confirm_taken(V)
    sent = list(med.hw.sent)
    calls: list[DoseInfo] = []
    out = med.dose.dispense_next(V, on_motion_start=calls.append)
    assert out.status is DispenseStatus.DUPLICATE and out.reason == "ALREADY_ACCESSED"
    assert out.dose.status == TAKEN and out.hardware is None
    assert med.hw.sent == sent and calls == []                       # ZERO new hardware commands
    refused = med.devlog("DISPENSE_REFUSED")[-1]
    assert refused.category == LogCategory.SAFETY.value and refused.detail["status"] == "DUPLICATE"


def test_duplicate_while_gate_is_still_open(med: Med):
    first = med.dose.dispense_next(V)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.DUPLICATE and out.dose.event_id == first.dose.event_id
    assert med.hw.sent == ["DISPENSE_SLOT 2"]


def test_too_soon_blocks_overlapping_dose_of_same_medication(settings, clock, db, bus, fake_hw):
    m = build(settings, clock, db, bus, fake_hw, min_dose_interval_minutes=180)
    assert m.dose.dispense_next(V).status is DispenseStatus.DISPENSED       # 08:00 dose at 07:55
    m.dose.confirm_taken(V)
    m.scheduler.create_schedule(m.med1, "10:30")
    m.travel("10:01")
    m.tick()
    sent = list(m.hw.sent)
    out = m.dose.dispense_next(V)
    assert out.status is DispenseStatus.DUPLICATE and out.reason == BlockReason.TOO_SOON.value
    assert out.dose.scheduled_local.strftime("%H:%M") == "10:30"
    assert m.hw.sent == sent


def test_nothing_due_reports_next_upcoming(med: Med):
    med.travel("10:30")
    med.tick()
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.NOTHING_DUE and out.dose is None
    assert out.next_upcoming.scheduled_local.strftime("%H:%M") == "13:00"
    assert med.hw.sent == []


def test_remaining_due_after_dispense(med: Med):
    med.scheduler.create_schedule(med.med2, "08:00")
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.DISPENSED and out.dose.slot == 2 and out.remaining_due == 1
    assert len(med.dose.check_due().due) == 1


def test_motion_callback_failure_does_not_stop_dispense(med: Med):
    def boom(_dose: DoseInfo) -> None:
        raise RuntimeError("speaker exploded")

    assert med.dose.dispense_next(V, on_motion_start=boom).status is DispenseStatus.DISPENSED


# --------------------------------------------------------------------------- concurrency


def test_concurrent_requests_dispense_exactly_once(med: Med):
    med.hw.hold(CommandName.DISPENSE_SLOT)
    results: list[Any] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=5)
        r = med.dose.dispense_next(IntentSource.UI)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    try:
        assert wait_until(lambda: len(results) == 7, timeout=10)
        assert med.hw.holding.wait(5)
        assert all(r.status in (DispenseStatus.IN_PROGRESS, DispenseStatus.DUPLICATE) for r in results)
    finally:
        med.hw.release()
    for t in threads:
        t.join(timeout=10)
    statuses = [r.status for r in results]
    assert statuses.count(DispenseStatus.DISPENSED) == 1 and len(statuses) == 8
    assert med.hw.commands(CommandName.DISPENSE_SLOT) == ["DISPENSE_SLOT 2"]
    assert med.due_0800().attempts == 1 and med.due_0800().status == DISPENSED


# --------------------------------------------------------------------------- outcome mapping


@pytest.mark.parametrize("code,needs_review,result_text,event_type", [
    (Err.MOTOR_FAULT, False, "ERR MOTOR_FAULT", "dispense_failed"),
    (Err.INVALID_STATE, False, "ERR INVALID_STATE", "dispense_failed"),
    (HostCode.DEVICE_RESET, False, "ERR DEVICE_RESET", "dispense_failed"),
    (HostCode.NOT_CONNECTED, False, "ERR NOT_CONNECTED", "dispense_failed"),
    (HostCode.TIMEOUT, True, "UNCERTAIN TIMEOUT", "dispense_uncertain"),
    (HostCode.DISCONNECTED, True, "UNCERTAIN DISCONNECTED", "dispense_uncertain"),
])
def test_outcome_mapping_failures(med: Med, code, needs_review, result_text, event_type):
    med.hw.script(CommandName.DISPENSE_SLOT, code)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_ERROR and out.reason == code.value
    assert out.uncertain is needs_review and out.dose.needs_review is needs_review
    ev = med.event(out.dose.event_id)
    assert ev.status == HARDWARE_ERROR and ev.needs_review is needs_review
    assert ev.hardware_result == result_text and ev.attempts == 1 and ev.dispensed_at is None
    assert med.adherence_statuses(ev.event_id)[-2:] == [DISPENSING, HARDWARE_ERROR]
    dev = _device_events(med)[-1]
    assert dev["event_type"] == event_type and dev["code"] == code.value
    assert "Vitamin" not in str(dev) and "user" not in str(dev)


def test_err_stopped_returns_dose_to_due(med: Med):
    med.hw.script(CommandName.DISPENSE_SLOT, Err.STOPPED)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.CANCELLED and out.reason == Err.STOPPED.value
    ev = med.event(out.dose.event_id)
    assert ev.status == DUE and ev.hardware_result == "ERR STOPPED" and not ev.needs_review
    assert _device_events(med) == []
    assert med.dose.dispense_next(V).status is DispenseStatus.DISPENSED


def test_definitive_failures_retry_until_max_attempts_then_lock(med: Med):
    med.hw.script(CommandName.DISPENSE_SLOT, Err.INVALID_STATE, times=3)
    reviews = []
    for expected_attempt in (1, 2, 3):
        out = med.dose.dispense_next(V)
        assert out.status is DispenseStatus.HARDWARE_ERROR
        ev = med.event(out.dose.event_id)
        assert ev.attempts == expected_attempt
        reviews.append(ev.needs_review)
    assert reviews == [False, False, True]
    locked = med.dose.dispense_next(V)
    assert locked.status is DispenseStatus.BLOCKED and locked.reason == BlockReason.NEEDS_REVIEW.value
    assert len(med.hw.commands(CommandName.DISPENSE_SLOT)) == 3


def test_uncertain_dose_is_never_retried_automatically(med: Med):
    med.hw.script(CommandName.DISPENSE_SLOT, HostCode.TIMEOUT)
    assert med.dose.dispense_next(V).uncertain
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.BLOCKED and out.reason == BlockReason.NEEDS_REVIEW.value
    assert len(med.hw.commands(CommandName.DISPENSE_SLOT)) == 1
    med.hw.set_state(gate=GateState.UNKNOWN)
    assert med.dose.close_gate("uncertain").ok                     # gate may be open -> closed


def test_hardware_exception_is_treated_as_uncertain(med: Med):
    def boom(_cmd):
        raise RuntimeError("driver bug")

    med.hw.script_fn(CommandName.DISPENSE_SLOT, boom)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_ERROR and out.uncertain
    ev = med.event(out.dose.event_id)
    assert ev.needs_review and ev.hardware_result.startswith("UNCERTAIN DISCONNECTED")


def test_busy_local_is_retried_because_nothing_was_sent(med: Med):
    med.hw.script(CommandName.DISPENSE_SLOT, HostCode.BUSY_LOCAL)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.DISPENSED and med.event(out.dose.event_id).attempts == 1


# --------------------------------------------------------------------------- preparation


@pytest.mark.parametrize("state", [DeviceState.SAFE_STOP, DeviceState.BOOT])
def test_unhomed_device_is_homed_first(med: Med, state):
    med.hw.set_state(state=state, homed=False, slot=None)
    order: list[str] = []
    out = med.dose.dispense_next(V, on_motion_start=lambda d: order.append(f"warn@{len(med.hw.sent)}"))
    assert out.status is DispenseStatus.DISPENSED
    assert med.hw.sent == ["HOME", "DISPENSE_SLOT 2"]
    assert order == ["warn@0"]            # warned once, before the first carousel motion (HOME)


def test_auto_home_disabled_refuses_without_touching_dose(settings, clock, db, bus, fake_hw):
    m = build(settings, clock, db, bus, fake_hw, hw_auto_home=False)
    m.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    out = m.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == Err.NOT_HOMED.value
    assert m.hw.sent == [] and m.due_0800().status == DUE and m.due_0800().attempts == 0


def test_fault_refuses_without_claiming(med: Med):
    med.hw.set_state(state=DeviceState.FAULT, homed=False)
    calls: list[DoseInfo] = []
    out = med.dose.dispense_next(V, on_motion_start=calls.append)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == "FAULT"
    assert med.hw.sent == [] and calls == []
    ev = med.due_0800()
    assert ev.status == DUE and ev.attempts == 0 and med.adherence_statuses(ev.event_id) == [DUE]
    assert _device_events(med)[-1]["event_type"] == "dispense_refused"


def test_not_connected_refuses_without_sending(settings, clock, db, bus):
    m = build(settings, clock, db, bus, FakeHardware(connected=False))
    out = m.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == HostCode.NOT_CONNECTED.value
    assert m.hw.sent == [] and m.due_0800().status == DUE


def test_open_gate_is_closed_first(med: Med):
    med.hw.set_state(state=DeviceState.GATE_OPEN, gate=GateState.OPEN)
    assert med.dose.dispense_next(V).status is DispenseStatus.DISPENSED
    assert med.hw.sent == ["CLOSE_GATE", "DISPENSE_SLOT 2"]


def test_home_failure_is_hardware_unavailable(med: Med):
    med.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    med.hw.script(CommandName.HOME, Err.HOME_TIMEOUT)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == Err.HOME_TIMEOUT.value
    assert out.hardware.code == Err.HOME_TIMEOUT.value and med.hw.sent == ["HOME"]
    assert med.due_0800().status == DUE and med.due_0800().attempts == 0
    assert _device_events(med)[-1]["code"] == Err.HOME_TIMEOUT.value


def test_waits_for_homing_to_finish(med: Med):
    med.hw.set_state(state=DeviceState.HOMING, homed=False, slot=None)
    timer = threading.Timer(0.15, lambda: med.hw.set_state(state=DeviceState.READY, homed=True, slot=0))
    timer.start()
    try:
        out = med.dose.dispense_next(V)
    finally:
        timer.cancel()
    assert out.status is DispenseStatus.DISPENSED and med.hw.sent == ["DISPENSE_SLOT 2"]


def test_homing_timeout(settings, clock, db, bus, fake_hw):
    m = build(settings, clock, db, bus, fake_hw, timeout_home_s=0.1)
    m.hw.set_state(state=DeviceState.HOMING, homed=False)
    out = m.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == "HOMING_TIMEOUT"
    assert m.hw.sent == []


def test_unknown_state_resyncs_then_fails_closed(med: Med):
    med.hw.set_state(state=DeviceState.UNKNOWN, homed=None)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == "NOT_READY_UNKNOWN"
    assert med.hw.sent == ["STATUS"]


def test_device_moving_for_someone_else_is_busy(med: Med):
    med.hw.set_state(state=DeviceState.MOVING)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == Err.BUSY.value
    assert med.hw.sent == []


def test_slot_outside_hardware_range_is_refused(settings, clock, db, bus):
    m = build(settings, clock, db, bus, FakeHardware(num_slots=4))
    m.compartments.assign(5, m.med1)
    out = m.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == Err.INVALID_SLOT.value
    assert m.hw.commands(CommandName.DISPENSE_SLOT) == [] and m.due_0800().status == DUE


# --------------------------------------------------------------------------- interrupt / cancel


def test_interrupt_during_dispense_cancels_and_dose_is_due_again(med: Med):
    med.hw.hold(CommandName.DISPENSE_SLOT)
    t, out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        started = time.monotonic()
        assert med.dose.interrupt(V) is True
        assert time.monotonic() - started < 1.0          # does not wait for the dispense lock
    finally:
        med.hw.release()
    t.join(5)
    res = out["value"]
    assert res.status is DispenseStatus.CANCELLED and res.reason == Err.STOPPED.value
    ev = med.event(res.dose.event_id)
    assert ev.status == DUE and ev.hardware_result == "ERR STOPPED"
    assert med.hw.sent == ["DISPENSE_SLOT 2", "STOP"]
    cancel = med.dose.cancel(V)                           # assistant flow: interrupt() first, then cancel()
    assert cancel.status is CancelStatus.STOPPED_MOTION and cancel.dose.event_id == ev.event_id
    assert med.dose.cancel(V).status is CancelStatus.NOTHING_TO_CANCEL
    again = med.dose.dispense_next(V)                     # SAFE_STOP -> re-home first
    assert again.status is DispenseStatus.DISPENSED and med.hw.sent[-2:] == ["HOME", "DISPENSE_SLOT 2"]


def test_interrupt_when_idle_sends_nothing(med: Med):
    assert med.dose.interrupt(V) is False
    assert med.hw.sent == []


def test_cancel_while_moving_stops_immediately(med: Med):
    med.hw.hold(CommandName.DISPENSE_SLOT)
    t, out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        cancel = med.dose.cancel(V)
        assert cancel.status is CancelStatus.STOPPED_MOTION and cancel.hardware.code == Ok.STOPPED.value
    finally:
        med.hw.release()
    t.join(5)
    assert out["value"].status is DispenseStatus.CANCELLED


def test_interrupt_during_homing_cancels_before_claim(med: Med):
    med.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    med.hw.hold(CommandName.HOME)
    t, out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        assert med.dose.interrupt(V) is True
    finally:
        med.hw.release()
    t.join(5)
    assert out["value"].status is DispenseStatus.CANCELLED
    assert med.hw.sent == ["HOME", "STOP"]
    ev = med.due_0800()
    assert ev.status == DUE and ev.attempts == 0 and med.adherence_statuses(ev.event_id) == [DUE]


def test_cancel_between_claim_and_motion_sends_nothing(med: Med):
    def cancel_now(_dose: DoseInfo) -> None:
        assert med.dose.interrupt(V) is False             # nothing moving yet ...

    out = med.dose.dispense_next(V, on_motion_start=cancel_now)
    assert out.status is DispenseStatus.CANCELLED and out.reason == "CANCELLED"   # ... but it never starts
    assert med.hw.sent == []
    ev = med.due_0800()
    assert ev.status == DUE and ev.attempts == 0
    assert med.adherence_statuses(ev.event_id) == [DUE, DISPENSING, DUE]


def test_cancel_closes_open_gate_and_dose_stays_dispensed(med: Med):
    first = med.dose.dispense_next(V)
    cancel = med.dose.cancel(V)
    assert cancel.status is CancelStatus.CLOSED_GATE and cancel.dose.event_id == first.dose.event_id
    assert med.hw.sent == ["DISPENSE_SLOT 2", "CLOSE_GATE"]
    assert med.event(first.dose.event_id).status == DISPENSED
    assert med.dose.dispense_next(V).status is DispenseStatus.DUPLICATE


def test_cancel_reports_failure_when_gate_will_not_close(med: Med):
    med.dose.dispense_next(V)
    med.hw.script(CommandName.CLOSE_GATE, Err.BUSY)
    assert med.dose.cancel(V).status is CancelStatus.FAILED


def test_cancel_with_nothing_to_do(med: Med):
    assert med.dose.cancel(V).status is CancelStatus.NOTHING_TO_CANCEL
    assert med.hw.sent == []


# --------------------------------------------------------------------------- close gate / confirm


def test_close_gate_only_when_open_or_possibly_open(med: Med):
    assert med.dose.close_gate("idle") is None
    med.dose.dispense_next(V)
    first = med.dose.close_gate("gate timeout")
    assert first is not None and first.ok
    assert med.dose.close_gate("again") is None
    med.hw.set_state(gate=GateState.UNKNOWN)
    assert med.dose.close_gate("unknown, nothing dispensed since") is None


def test_close_gate_when_unknown_right_after_dispense(med: Med):
    med.dose.dispense_next(V)
    med.hw.set_state(state=DeviceState.UNKNOWN, gate=GateState.UNKNOWN)
    result = med.dose.close_gate("gate timeout")
    assert result is not None and result.ok and med.hw.sent[-1] == "CLOSE_GATE"


def test_confirm_without_dispense(med: Med):
    out = med.dose.confirm_taken(V)
    assert out.status is ConfirmStatus.NOTHING_TO_CONFIRM and out.dose is None and out.gate_closed is None
    assert med.hw.sent == []


def test_confirm_twice_is_already_confirmed(med: Med):
    med.dose.dispense_next(V)
    assert med.dose.confirm_taken(V).status is ConfirmStatus.CONFIRMED
    again = med.dose.confirm_taken(V)
    assert again.status is ConfirmStatus.ALREADY_CONFIRMED and again.dose.status == TAKEN
    assert med.hw.sent == ["DISPENSE_SLOT 2", "CLOSE_GATE"]


def test_confirm_after_gate_closed_does_not_send(med: Med):
    med.dose.dispense_next(V)
    med.dose.close_gate("timer")
    out = med.dose.confirm_taken(V)
    assert out.status is ConfirmStatus.CONFIRMED and out.gate_closed is None and out.hardware is None


def test_confirm_window_expires(med: Med):
    med.dose.dispense_next(V)
    med.clock.advance(timedelta(minutes=181))
    assert med.dose.confirm_taken(V).status is ConfirmStatus.NOTHING_TO_CONFIRM


def test_confirm_db_error(med: Med, flaky: FlakyDB):
    flaky.fail = True
    assert med.dose.confirm_taken(V).status is ConfirmStatus.DB_ERROR


# --------------------------------------------------------------------------- recovery / fail closed


def test_recover_on_startup_marks_dispensing_uncertain(med: Med):
    ev = med.due_0800()
    med.set_event(ev.event_id, status=DISPENSING, attempts=1, slot_number=2)
    assert med.dose.dispense_next(V).status is DispenseStatus.IN_PROGRESS
    fresh = DoseService(med.db, med.hw, med.clock, med.settings, bus=med.bus)
    assert fresh.recover_on_startup() == 1
    rec = med.event(ev.event_id)
    assert rec.status == HARDWARE_ERROR and rec.needs_review and rec.hardware_result == "UNCERTAIN RESTART"
    assert med.adherence_statuses(ev.event_id)[-1] == HARDWARE_ERROR
    assert _device_events(med)[-1]["code"] == "RESTART"
    out = fresh.dispense_next(V)
    assert out.status is DispenseStatus.BLOCKED and out.reason == BlockReason.NEEDS_REVIEW.value
    assert med.hw.sent == [] and fresh.recover_on_startup() == 0


def test_recover_on_startup_survives_db_error(med: Med, flaky: FlakyDB):
    flaky.fail = True
    assert med.dose.recover_on_startup() == 0


def test_db_failure_means_no_hardware_command(med: Med, flaky: FlakyDB):
    flaky.fail = True
    calls: list[DoseInfo] = []
    out = med.dose.dispense_next(V, on_motion_start=calls.append)
    assert out.status is DispenseStatus.DB_ERROR and med.hw.sent == [] and calls == []


def test_db_failure_during_claim_sends_no_dispense(med: Med, flaky: FlakyDB):
    med.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)

    def home_then_db_dies(cmd):
        med.hw.set_state(state=DeviceState.READY, homed=True, slot=0)
        flaky.fail = True
        return CommandResult(cmd, True, Ok.HOMED.value)

    med.hw.script_fn(CommandName.HOME, home_then_db_dies)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.DB_ERROR and med.hw.sent == ["HOME"]


def test_check_due_reports_db_error(med: Med, flaky: FlakyDB):
    flaky.fail = True
    summary = med.dose.check_due()
    assert isinstance(summary, DueReport) and summary.error == "DB_ERROR" and summary.due == ()
    assert summary.to_dict()["error"] == "DB_ERROR"
    flaky.fail = False
    assert "error" not in med.dose.check_due().to_dict()


def test_unrecorded_dispense_blocks_until_recorded(med: Med, flaky: FlakyDB):
    notices = med.subscribe(Topic.NOTICE)

    def open_then_db_dies(cmd):
        med.hw.set_state(state=DeviceState.GATE_OPEN, gate=GateState.OPEN, slot=cmd.slot)
        flaky.fail = True
        return CommandResult(cmd, True, Ok.GATE_OPEN.value)

    med.hw.script_fn(CommandName.DISPENSE_SLOT, open_then_db_dies)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.DISPENSED and out.reason == UNRECORDED   # gate IS open: tell the user
    assert out.dose.status == DISPENSED and med.dose.has_unrecorded_outcome
    assert notices.drain()[-1].data["level"] == "error"
    sent = list(med.hw.sent)
    blocked = med.dose.dispense_next(V)
    assert blocked.status is DispenseStatus.DB_ERROR and blocked.reason == UNRECORDED
    assert blocked.dose.event_id == out.dose.event_id and med.hw.sent == sent
    assert med.dose.awaiting_confirmation().event_id == out.dose.event_id
    confirm = med.dose.confirm_taken(V)
    assert confirm.status is ConfirmStatus.DB_ERROR and confirm.gate_closed is True    # gate still closed for safety
    flaky.fail = False
    assert med.dose.dispense_next(V).status is DispenseStatus.DUPLICATE                # recorded on the next call
    ev = med.event(out.dose.event_id)
    assert ev.status == DISPENSED and ev.dispensed_at == med.clock.now()
    assert not med.dose.has_unrecorded_outcome
    assert med.hw.commands(CommandName.DISPENSE_SLOT) == ["DISPENSE_SLOT 2"]


def test_unrecorded_failure_outcome_is_written_later(med: Med, flaky: FlakyDB):
    def timeout_and_db_dies(cmd):
        flaky.fail = True
        return CommandResult.host_failure(cmd, HostCode.TIMEOUT)

    med.hw.script_fn(CommandName.DISPENSE_SLOT, timeout_and_db_dies)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_ERROR and out.reason == HostCode.TIMEOUT.value
    assert med.dose.has_unrecorded_outcome
    flaky.fail = False
    med.dose.check_due()
    ev = med.event(out.dose.event_id)
    assert ev.status == HARDWARE_ERROR and ev.needs_review and not med.dose.has_unrecorded_outcome


# --------------------------------------------------------------------------- caregiver review ops


def _uncertain(m: Med) -> int:
    m.hw.script(CommandName.DISPENSE_SLOT, HostCode.TIMEOUT)
    return m.dose.dispense_next(V).dose.event_id


def test_resolve_review_accessed_marks_dispensed(med: Med):
    eid = _uncertain(med)
    sub = med.subscribe(Topic.DOSE_UPDATED)
    view = med.dose.resolve_review(eid, accessed=True, note="  gate was open  ", by="nurse")
    assert view["status"] == DISPENSED and view["needs_review"] is False and view["review_note"] == "gate was open"
    ev = med.event(eid)
    assert ev.dispensed_at == med.clock.now() and not ev.needs_review
    assert med.devlog("DOSE_REVIEW_RESOLVED")[-1].category == LogCategory.ADMIN.value
    assert sub.drain()[-1].data["status"] == DISPENSED
    assert med.dose.confirm_taken(V).status is ConfirmStatus.CONFIRMED


def test_resolve_review_not_accessed_makes_dose_due_again(med: Med):
    eid = _uncertain(med)
    view = med.dose.resolve_review(eid, accessed=False, note=None, by=None)
    assert view["status"] == DUE and view["attempts"] == 0 and view["needs_review"] is False
    assert med.dose.dispense_next(V).status is DispenseStatus.DISPENSED


def test_resolve_review_after_window_is_missed(med: Med):
    eid = _uncertain(med)
    med.travel("10:30")
    view = med.dose.resolve_review(eid, accessed=False, note="not taken", by="nurse")
    assert view["status"] == MISSED and view["missed_at"] is not None


def test_resolve_review_errors(med: Med):
    with pytest.raises(NotFoundError):
        med.dose.resolve_review(98765, accessed=True)
    with pytest.raises(ConflictError):
        med.dose.resolve_review(med.due_0800().event_id, accessed=True)
    with pytest.raises(ValidationError):
        med.dose.resolve_review(med.due_0800().event_id, accessed="yes")  # type: ignore[arg-type]


def test_skip_dose(med: Med):
    view = med.dose.skip_dose(med.due_0800().event_id, note="traveling", by="caregiver")
    assert view["status"] == CANCELLED and view["cancelled_at"] is not None and view["review_note"] == "traveling"
    assert med.dose.dispense_next(V).status is DispenseStatus.NOTHING_DUE
    with pytest.raises(ConflictError):
        med.dose.skip_dose(med.due_0800().event_id)              # already CANCELLED


def test_skip_hardware_error_dose(med: Med):
    eid = _uncertain(med)
    assert med.dose.skip_dose(eid)["status"] == CANCELLED


def test_mark_taken_by_caregiver(med: Med):
    with pytest.raises(ConflictError):
        med.dose.mark_taken_by_caregiver(med.due_0800().event_id)
    eid = med.dose.dispense_next(V).dose.event_id
    view = med.dose.mark_taken_by_caregiver(eid, by="nurse")
    assert view["status"] == TAKEN and view["confirm_source"] == "caregiver"
    with pytest.raises(NotFoundError):
        med.dose.mark_taken_by_caregiver(424242)


# --------------------------------------------------------------------------- loading mode


def test_present_and_finish_loading(med: Med):
    result = med.dose.present_compartment(3, by="caregiver")
    assert result.ok and result.code == Ok.GATE_OPEN.value
    assert med.hw.sent == ["MOVE_SLOT 3", "OPEN_GATE"]
    assert med.devlog("PRESENT_COMPARTMENT")[-1].detail["slot"] == 3
    done = med.dose.finish_loading(3, by="caregiver")
    assert done.ok and med.hw.sent[-1] == "CLOSE_GATE"
    assert med.compartment(3).loaded_at == med.clock.now()


def test_present_homes_first_and_dispense_closes_loading_gate(med: Med):
    med.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    assert med.dose.present_compartment(1).ok
    assert med.hw.sent == ["HOME", "MOVE_SLOT 1", "OPEN_GATE"]
    assert med.dose.dispense_next(V).status is DispenseStatus.DISPENSED
    assert med.hw.sent[-2:] == ["CLOSE_GATE", "DISPENSE_SLOT 2"]


def test_present_refused_while_dose_awaits_confirmation(med: Med):
    med.dose.dispense_next(V)
    with pytest.raises(ConflictError):
        med.dose.present_compartment(3)
    assert med.hw.sent == ["DISPENSE_SLOT 2"]


def test_present_refused_in_fault_or_busy(med: Med):
    med.hw.set_state(state=DeviceState.FAULT, homed=False)
    with pytest.raises(ConflictError):
        med.dose.present_compartment(1)
    med.hw.set_state(state=DeviceState.READY, homed=True)
    med.hw.hold(CommandName.DISPENSE_SLOT)
    t, _out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        with pytest.raises(ConflictError):
            med.dose.present_compartment(1)
    finally:
        med.hw.release()
    t.join(5)


@pytest.mark.parametrize("slot", [-1, 6, True, "2"])
def test_present_validates_slot(med: Med, slot):
    with pytest.raises(ValidationError):
        med.dose.present_compartment(slot)
    with pytest.raises(ValidationError):
        med.dose.finish_loading(slot)


def test_present_when_disconnected_returns_failure(settings, clock, db, bus):
    m = build(settings, clock, db, bus, FakeHardware(connected=False))
    result = m.dose.present_compartment(2)
    assert not result.ok and result.code == HostCode.NOT_CONNECTED.value and m.hw.sent == []


# --------------------------------------------------------------------------- demo helper / views


def test_create_demo_dose_now_is_due_and_dispensable(med: Med):
    med.travel("11:17")
    med.tick()
    assert med.dose.check_due().due == ()
    out = med.dose.create_demo_dose_now()
    assert out["schedule"]["time_of_day"] == "11:17" and out["schedule"]["frequency"] == "DAILY"
    assert out["event"]["status"] == DUE and out["event"]["scheduled_local"].startswith("2026-10-05T11:17")
    assert out["event"]["medication_id"] == med.med1 and out["event"]["slot"] == 2
    again = med.dose.create_demo_dose_now()
    assert again["schedule"]["schedule_id"] == out["schedule"]["schedule_id"]
    assert again["event"]["event_id"] == out["event"]["event_id"]
    dispensed = med.dose.dispense_next(IntentSource.KEYBOARD)
    assert dispensed.status is DispenseStatus.DISPENSED and dispensed.dose.event_id == out["event"]["event_id"]


def test_create_demo_dose_now_for_specific_medication(med: Med):
    med.travel("11:30")
    out = med.dose.create_demo_dose_now(med.med2)
    assert out["event"]["medication_id"] == med.med2 and out["event"]["slot"] == 4
    with pytest.raises(NotFoundError):
        med.dose.create_demo_dose_now(5555)
    med.catalog.archive(med.med2)
    with pytest.raises(ValidationError):
        med.dose.create_demo_dose_now(med.med2)
    med.compartments.assign(2, None)
    with pytest.raises(ValidationError):
        med.dose.create_demo_dose_now()                      # nothing assigned any more


def test_list_events_shape_and_order(med: Med):
    rows = med.dose.list_events()
    assert [r["scheduled_local"][11:16] for r in rows] == ["08:00", "13:00", "20:00"]
    expected = {"event_id", "label", "medication_id", "medication_name", "strength", "instructions", "slot",
                "compartment", "compartment_number", "scheduled_at", "scheduled_local", "status",
                "dispensed_at", "confirmed_taken_at", "needs_review", "attempts", "hardware_result",
                "schedule_id", "dispense_source", "confirm_source", "review_note", "missed_at", "cancelled_at"}
    assert all(set(r) == expected for r in rows)
    assert rows[0]["status"] == DUE and rows[0]["slot"] == 2 and rows[0]["compartment"] == "compartment 3"
    yesterday = med.dose.list_events(date(2026, 10, 4))
    assert [r["status"] for r in yesterday] == [MISSED, MISSED, MISSED]
    assert med.dose.list_events(date(2026, 12, 25)) == []
    assert med.dose.get_event(rows[0]["event_id"])["event_id"] == rows[0]["event_id"]
    with pytest.raises(NotFoundError):
        med.dose.get_event(999999)


def test_check_due_summary(med: Med):
    summary = med.dose.check_due()
    assert [d.slot for d in summary.due] == [2] and summary.next_upcoming.slot == 4
    med.dose.dispense_next(V)
    summary = med.dose.check_due()
    assert summary.due == () and [d.status for d in summary.awaiting_confirmation] == [DISPENSED]
    assert [d.status for d in summary.accessed] == [DISPENSED]
