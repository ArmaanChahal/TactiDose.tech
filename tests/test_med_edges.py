"""Edge cases of the domain services: races, partial failures, defensive paths (all fail closed)."""

from __future__ import annotations

import hashlib
import threading
from datetime import date

import pytest
from sqlalchemy import func, select, update

import tactidose.medication.dispense as dispense_mod
from tactidose.core.interfaces import CancelStatus, ConfirmStatus, DispenseStatus, IntentSource
from tactidose.db.models import Compartment, Device, Frequency, LabelScan, Medication, User
from tactidose.hardware.protocol import CommandName, CommandResult, DeviceState, Err, GateState, HostCode, Ok
from tactidose.medication.compartments import CompartmentService
from tactidose.medication.dispense import DoseService, DueReport
from tactidose.medication.errors import ConflictError, DomainError, NotFoundError, ValidationError
from tactidose.medication.onboarding import OnboardingService
from tactidose.medication.scheduler import parse_frequency
from tests.fakes import FakeHardware, wait_until
from tests.test_med_dispense import background
from tests.test_med_support import (  # noqa: F401 - fixtures
    CANCELLED,
    DISPENSED,
    DISPENSING,
    DUE,
    HARDWARE_ERROR,
    KIND_DEVICE_EVENT,
    MISSED,
    FlakyDB,
    Med,
    flaky,
    med,
    med_template,
)

V = IntentSource.VOICE


# --------------------------------------------------------------------------- claim races


def test_claim_reevaluates_after_homing(med: Med):
    """A caregiver skips the dose while the carousel homes: nothing is dispensed."""
    med.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    target = med.due_0800().event_id

    def home_while_caregiver_skips(cmd):
        med.dose.skip_dose(target, note="skipped during homing")
        med.hw.set_state(state=DeviceState.READY, homed=True, slot=0)
        return CommandResult(cmd, True, Ok.HOMED.value)

    med.hw.script_fn(CommandName.HOME, home_while_caregiver_skips)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.NOTHING_DUE
    assert med.hw.commands(CommandName.DISPENSE_SLOT) == [] and med.event(target).status == CANCELLED


def test_claim_conflict_gives_up_without_motion(med: Med, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dispense_mod, "cas_transition", lambda *a, **k: None)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.IN_PROGRESS and out.reason == "CLAIM_CONFLICT"
    assert med.hw.sent == [] and med.due_0800().status == DUE


def test_outcome_not_applied_when_event_changed_meanwhile(med: Med):
    """Another process's startup recovery locked the dose mid-dispense: the review lock wins."""
    med.hw.hold(CommandName.DISPENSE_SLOT)
    t, out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        assert DoseService(med.db, FakeHardware(), med.clock, med.settings).recover_on_startup() == 1
    finally:
        med.hw.release()
    t.join(5)
    res = out["value"]
    assert res.status is DispenseStatus.DISPENSED                 # the gate did open: the user is told
    ev = med.due_0800()
    assert ev.status == HARDWARE_ERROR and ev.needs_review and res.dose.status == HARDWARE_ERROR
    assert med.devlog("OUTCOME_NOT_APPLIED")


# --------------------------------------------------------------------------- preparation failures


def _status_fails(m: Med) -> None:
    m.hw.set_state(state=DeviceState.UNKNOWN, homed=None)
    m.hw.script(CommandName.STATUS, HostCode.TIMEOUT)


def _close_fails(m: Med) -> None:
    m.hw.set_state(state=DeviceState.GATE_OPEN, gate=GateState.OPEN)
    m.hw.script(CommandName.CLOSE_GATE, Err.BUSY)


def _home_but_not_ready(m: Med) -> None:
    m.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
    m.hw.script(CommandName.HOME, Ok.HOMED)            # reports success, state never becomes READY


@pytest.mark.parametrize("setup,reason,sent", [
    (_status_fails, HostCode.TIMEOUT.value, ["STATUS"]),
    (_close_fails, Err.BUSY.value, ["CLOSE_GATE"]),
    (_home_but_not_ready, "NOT_READY_SAFE_STOP", ["HOME"]),
])
def test_preparation_failures_never_claim(med: Med, setup, reason, sent):
    setup(med)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == reason
    assert med.hw.sent == sent
    ev = med.due_0800()
    assert ev.status == DUE and ev.attempts == 0


@pytest.mark.parametrize("final,reason", [
    ({"state": DeviceState.FAULT, "homed": False}, "FAULT"),
    ({"connected": False}, HostCode.NOT_CONNECTED.value),
])
def test_homing_that_ends_badly(med: Med, final, reason):
    med.hw.set_state(state=DeviceState.HOMING, homed=False)
    timer = threading.Timer(0.05, lambda: med.hw.set_state(**final))
    timer.start()
    try:
        out = med.dose.dispense_next(V)
    finally:
        timer.cancel()
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == reason
    assert med.hw.sent == []


def test_snapshot_failure_fails_closed(med: Med, monkeypatch: pytest.MonkeyPatch):
    def broken() -> None:
        raise RuntimeError("reader thread died")

    monkeypatch.setattr(med.hw, "snapshot", broken)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_UNAVAILABLE and out.reason == "NO_STATUS"
    assert med.dose.interrupt(V) is False and med.dose.close_gate("x") is None
    assert med.hw.sent == []


def test_interrupt_while_waiting_for_homing(med: Med, monkeypatch: pytest.MonkeyPatch):
    med.hw.set_state(state=DeviceState.HOMING, homed=False)
    calls = {"n": 0}
    real = med.hw.snapshot

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(med.hw, "snapshot", counting)
    t, out = background(lambda: med.dose.dispense_next(V))
    assert wait_until(lambda: calls["n"] >= 4, timeout=5)        # inside the HOMING wait loop
    assert med.dose.interrupt(V) is True                         # HOMING is motion: STOP
    t.join(5)
    assert out["value"].status is DispenseStatus.CANCELLED
    assert med.hw.sent == ["STOP"] and med.due_0800().attempts == 0


# --------------------------------------------------------------------------- locks / internal errors


def test_unexpected_error_is_reported_and_lock_released(med: Med, monkeypatch: pytest.MonkeyPatch):
    def bug(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(med.dose, "_dispense_locked", bug)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.DB_ERROR and out.reason == "INTERNAL_ERROR"
    monkeypatch.delattr(med.dose, "_dispense_locked")
    assert med.dose.dispense_next(V).status is DispenseStatus.DISPENSED


def test_bug_after_claim_locks_dose_for_review(med: Med, monkeypatch: pytest.MonkeyPatch):
    def bug(*_a, **_k):
        raise RuntimeError("bug while recording")

    monkeypatch.setattr(med.dose, "_finish", bug)
    out = med.dose.dispense_next(V)
    assert out.status is DispenseStatus.HARDWARE_ERROR and out.reason == "INTERNAL_ERROR"
    ev = med.due_0800()
    assert ev.status == HARDWARE_ERROR and ev.needs_review and ev.hardware_result == "UNCERTAIN INTERNAL_ERROR"
    monkeypatch.delattr(med.dose, "_finish")
    blocked = med.dose.dispense_next(V)
    assert blocked.status is DispenseStatus.BLOCKED and len(med.hw.commands(CommandName.DISPENSE_SLOT)) == 1
    assert med.dose.resolve_review(ev.event_id, accessed=True)["status"] == DISPENSED


def test_lock_timeouts_fail_closed(med: Med):
    med.dose._lock_timeout_s = 0.05
    assert med.dose._lock.acquire(timeout=1)
    try:
        assert med.dose.dispense_next(V).status is DispenseStatus.IN_PROGRESS
        assert med.dose.confirm_taken(V).status is ConfirmStatus.DB_ERROR
        assert med.dose.cancel(V).status is CancelStatus.FAILED
        busy = med.dose.close_gate("timer")
        assert busy is not None and busy.code == HostCode.BUSY_LOCAL.value
        with pytest.raises(ConflictError):
            med.dose.present_compartment(1)
        with pytest.raises(ConflictError):
            med.dose.finish_loading(1)
    finally:
        med.dose._lock.release()
    assert med.hw.sent == []


def test_failed_stop_does_not_corrupt_the_outcome(med: Med, monkeypatch: pytest.MonkeyPatch):
    med.hw.hold(CommandName.DISPENSE_SLOT)

    def broken_stop():
        raise RuntimeError("serial write failed")

    monkeypatch.setattr(med.hw, "stop", broken_stop)
    t, out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        assert med.dose.interrupt(V) is False                    # STOP could not be sent
    finally:
        med.hw.release()
    t.join(5)
    assert out["value"].status is DispenseStatus.DISPENSED and med.due_0800().status == DISPENSED


def test_stale_stop_is_not_reported_by_cancel(med: Med):
    med.hw.hold(CommandName.DISPENSE_SLOT)
    t, _out = background(lambda: med.dose.dispense_next(V))
    try:
        assert med.hw.holding.wait(5)
        assert med.dose.interrupt(V)
    finally:
        med.hw.release()
    t.join(5)
    med.dose._last_stop.at -= 60
    assert med.dose.cancel(V).status is CancelStatus.NOTHING_TO_CANCEL


# --------------------------------------------------------------------------- loading mode edges


def test_present_refusals(med: Med, flaky: FlakyDB):
    ev = med.due_0800()
    med.set_event(ev.event_id, status=DISPENSING)
    with pytest.raises(ConflictError):
        med.dose.present_compartment(1)                           # a dose is in flight
    med.set_event(ev.event_id, status=DUE)
    med.hw.set_state(state=DeviceState.MOVING)
    with pytest.raises(ConflictError):
        med.dose.present_compartment(1)                           # carousel busy
    med.hw.set_state(state=DeviceState.READY)
    flaky.fail = True
    with pytest.raises(ConflictError):
        med.dose.present_compartment(1)                           # cannot prove it is safe
    flaky.fail = False
    assert med.hw.sent == []


def test_present_move_failure_never_opens_gate(med: Med):
    med.hw.script(CommandName.MOVE_SLOT, Err.MOTOR_FAULT)
    result = med.dose.present_compartment(1)
    assert not result.ok and result.code == Err.MOTOR_FAULT.value and med.hw.sent == ["MOVE_SLOT 1"]
    assert med.outbox(KIND_DEVICE_EVENT)[-1].payload["event_type"] == "present_failed"


def test_present_refused_while_outcome_unrecorded(med: Med, flaky: FlakyDB):
    def timeout_and_db_dies(cmd):
        flaky.fail = True
        return CommandResult.host_failure(cmd, HostCode.TIMEOUT)

    med.hw.script_fn(CommandName.DISPENSE_SLOT, timeout_and_db_dies)
    med.dose.dispense_next(V)
    with pytest.raises(ConflictError):
        med.dose.present_compartment(1)


def test_finish_loading_survives_db_failure(med: Med, flaky: FlakyDB):
    flaky.fail = True
    result = med.dose.finish_loading(1)
    assert result.ok and med.hw.sent == ["CLOSE_GATE"]


# --------------------------------------------------------------------------- misc dose service


def test_awaiting_confirmation_db_error(med: Med, flaky: FlakyDB):
    flaky.fail = True
    assert med.dose.awaiting_confirmation() is None


def test_demo_dose_skips_unconfirmed_lower_slot(med: Med):
    with med.db.session() as s:
        raw = Medication(user_id=med.ids["user_id"], name="Typed, unconfirmed", confirmed_by_user=False)
        s.add(raw)
        s.flush()
        s.execute(update(Compartment).where(Compartment.slot_number == 0).values(medication_id=raw.medication_id))
    med.travel("11:00")
    out = med.dose.create_demo_dose_now()
    assert out["event"]["medication_id"] == med.med1 and out["schedule"]["frequency"] == Frequency.DAILY.value


def test_source_strings_and_note_validation(med: Med):
    out = med.dose.dispense_next("kiosk")  # type: ignore[arg-type]
    assert med.event(out.dose.event_id).dispense_source == "kiosk"
    with pytest.raises(ValidationError):
        med.dose.skip_dose(med.event_at(med.sched_1300, "13:00").event_id, note=5)  # type: ignore[arg-type]


def test_due_report_without_error_matches_due_summary(med: Med):
    report = DueReport(now_local=med.clock.local_now())
    assert report.to_dict() == {"now_local": "2026-10-05T07:55:00-07:00", "due": [], "awaiting_confirmation": [],
                                "accessed": [], "blocked": [], "next_upcoming": None}


# --------------------------------------------------------------------------- scheduler / bootstrap / onboarding


def test_schedule_edit_leaves_review_locked_and_closed_window_doses_alone(med: Med):
    today = med.due_0800()
    med.set_event(today.event_id, status=HARDWARE_ERROR, needs_review=True, attempts=1)
    yesterday = med.event_at(med.sched_0800, "08:00", date(2026, 10, 4))
    med.set_event(yesterday.event_id, status=DUE)                  # window closed, not yet refreshed
    med.scheduler.update_schedule(med.sched_0800, time_of_day="09:00")
    assert med.event(today.event_id).status == HARDWARE_ERROR       # caregiver decides, not the edit
    assert med.event_at(med.sched_0800, "09:00") is None            # possibly accessed today: no 2nd dose
    assert med.event(yesterday.event_id).status == MISSED           # left to refresh()


def test_schedule_rejects_other_users_medication(med: Med):
    with med.db.session() as s:
        other = User(display_name="Other")
        s.add(other)
        s.flush()
        foreign = Medication(user_id=other.user_id, name="Not yours", confirmed_by_user=True)
        s.add(foreign)
        s.flush()
        foreign_id = foreign.medication_id
    with pytest.raises(ValidationError):
        med.scheduler.create_schedule(foreign_id, "09:00")
    assert parse_frequency(Frequency.WEEKLY) == "WEEKLY"


def test_concurrent_bootstrap_creates_one_device(settings, db):
    services = [CompartmentService(db, settings) for _ in range(4)]
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
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(User)) == 1
        assert s.scalar(select(func.count()).select_from(Device)) == 1
        assert s.scalar(select(func.count()).select_from(Compartment)) == 6


def test_scan_survives_image_store_failure(med: Med, fake_extractor):
    med.settings.data_dir.mkdir(parents=True, exist_ok=True)
    (med.settings.data_dir / "label_scans").write_text("a file where the directory should be")
    image = b"\xff\xd8\xff" + b"label" * 10
    ob = OnboardingService(med.db, fake_extractor, med.catalog, med.settings, med.clock)
    out = ob.scan(image, "image/jpeg")
    assert out["status"] == "PENDING_REVIEW"
    with med.db.session() as s:
        row = s.get(LabelScan, out["scan_id"])
        assert row.image_path is None and row.image_sha256 == hashlib.sha256(image).hexdigest()


def test_domain_errors_carry_http_status():
    assert (ValidationError("bad").status_code, NotFoundError("x").status_code,
            ConflictError("y").status_code, DomainError("z").status_code) == (422, 404, 409, 400)
    assert str(ValidationError("Readable message.")) == "Readable message."
    assert ConflictError("c").message == "c"
    with pytest.raises(DomainError):
        raise NotFoundError("subclass of DomainError")
