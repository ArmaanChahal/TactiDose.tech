"""DoseService — the only code path that turns a dose request into carousel motion.

Implements ``core.interfaces.DoseServiceAPI`` for the assistant plus the caregiver
dose operations (review, skip, mark taken, compartment loading, demo helpers).

Safety design (ARCHITECTURE §1, §5; handoff §3, §13, §30, §33):

* **Deterministic authorization.** Whether anything may move is decided only by
  :func:`tactidose.medication.safety.evaluate`; a request (voice, button, UI) is never
  an authorization by itself.
* **One at a time.** An internal lock serialises dispense / confirm / cancel /
  close-gate / present; :meth:`DoseService.dispense_next` never waits for it (a
  concurrent request gets ``IN_PROGRESS``), and the dose is claimed with a database
  compare-and-set so it can never be dispensed twice, even across processes.
* **Fail closed.** Database errors before motion -> ``DB_ERROR`` and no command.
  Hardware not ready -> ``HARDWARE_UNAVAILABLE`` without touching the dose. An
  uncertain outcome (timeout / disconnect: the gate may be open) is recorded as
  ``HARDWARE_ERROR`` with ``needs_review`` and is never retried automatically.
  If an outcome cannot be written, an in-memory guard blocks all further
  dispensing until it is recorded.
* :meth:`DoseService.interrupt` never takes the lock: STOP must reach the device
  while another thread is blocked inside ``DISPENSE_SLOT``.

Every dose status change writes the outbox row and the audit log in the same
transaction and publishes ``Topic.DOSE_UPDATED`` after commit.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import (
    BlockReason,
    CancelOutcome,
    CancelStatus,
    ConfirmOutcome,
    ConfirmStatus,
    DeviceSnapshot,
    DispenseOutcome,
    DispenseStatus,
    DoseInfo,
    DueSummary,
    HardwareController,
    IntentSource,
)
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    Compartment,
    DoseEvent,
    DoseStatus,
    Frequency,
    LogCategory,
    Medication,
    Schedule,
)
from tactidose.db.outbox import enqueue_device_event
from tactidose.db.session import Database
from tactidose.hardware.protocol import (
    BUSY_STATES,
    Command,
    CommandName,
    CommandResult,
    DeviceState,
    Err,
    GateState,
    HostCode,
    LONG_RUNNING_COMMANDS,
    Ok,
)
from tactidose.medication import safety
from tactidose.medication.compartments import assigned_slots, iso
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tactidose.medication.safety import Verdict
from tactidose.medication.scheduler import (
    Scheduler,
    cas_transition,
    dose_update_payload,
    is_id,
    publish_all,
    record_dose_change,
)

log = logging.getLogger(__name__)

__all__ = ["DoseService", "DueReport", "UNRECORDED", "event_view"]

#: ``DispenseOutcome.reason`` when the gate opened but the DISPENSED state could not be written.
UNRECORDED = "UNRECORDED"
#: ``DispenseOutcome.reason`` for DUPLICATE caused by an in-window dose already accessed.
ALREADY_ACCESSED = "ALREADY_ACCESSED"
_MOTION_COMMANDS = frozenset(c.value for c in LONG_RUNNING_COMMANDS)   # HOME, MOVE_SLOT, DISPENSE_SLOT
_RECENT_STOP_S = 30.0
_MAX_NOTE = 255


@dataclass(frozen=True)
class DueReport(DueSummary):
    """``DueSummary`` plus ``error`` — returned by :meth:`DoseService.check_due` when the
    schedule cannot be read (``error="DB_ERROR"``), so callers never mistake a database
    outage for "nothing due". ``to_dict`` adds ``"error"`` only when it is set."""

    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass(frozen=True)
class _Eval:
    """Plain-data snapshot of a SafetyDecision (built inside the session)."""

    verdict: Verdict
    reason: BlockReason | None
    dose: DoseInfo | None
    next_upcoming: DoseInfo | None
    due_count: int

    @classmethod
    def of(cls, d: safety.SafetyDecision, clock: Clock) -> "_Eval":
        return cls(d.verdict, d.reason, d.dose_info(clock), d.next_upcoming_info(clock), len(d.due))


@dataclass(frozen=True)
class _Prep:
    ok: bool
    reason: str = ""
    result: CommandResult | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class _Claim:
    event_id: int
    previous: str
    attempts: int
    slot: int
    dose: DoseInfo


@dataclass(frozen=True)
class _PendingRecord:
    """A dose outcome that still has to be written (CAS from DISPENSING)."""

    event_id: int
    values: dict[str, Any]
    action: str
    detail: dict[str, Any]
    device_event: tuple[str, str] | None
    dose: DoseInfo          # what the user is told (already in the target status)


@dataclass
class _StopRecord:
    at: float
    result: CommandResult | None
    dose: DoseInfo | None
    reported: bool = False


def _source(source: IntentSource | str | None) -> str:
    if isinstance(source, IntentSource):
        return source.value
    return str(source) if source else IntentSource.API.value


def _clean_note(value: object, field: str = "note") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.")
    text = value.strip()
    return text[:_MAX_NOTE] if text else None


def event_view(session: Session, event: DoseEvent, clock: Clock, settings: Settings,
               slots: dict[int, tuple[int, int]] | None = None) -> dict[str, Any]:
    """API.md ``DoseEventView`` = ``DoseInfo`` + schedule/source/review/missed/cancelled fields."""
    slot_map = slots if slots is not None else assigned_slots(session, settings)
    d = safety.to_dose_info(event, clock, slot=safety.display_slot(event, slot_map)).to_dict()
    d.update({
        "schedule_id": event.schedule_id,
        "dispense_source": event.dispense_source,
        "confirm_source": event.confirm_source,
        "review_note": event.review_note,
        "missed_at": iso(event.missed_at),
        "cancelled_at": iso(event.cancelled_at),
    })
    return d


class DoseService:
    """Deterministic dose logic (implements ``DoseServiceAPI``). Thread-safe."""

    #: Pauses between the 3 attempts to write a dose outcome (tests may shorten them).
    db_retry_delays_s: tuple[float, ...] = (0.05, 0.15)
    #: Poll interval while waiting for HOMING to finish / a host-side command slot.
    poll_interval_s: float = 0.05
    #: How long to wait for another host command (e.g. a heartbeat PING) to finish.
    busy_wait_s: float = 3.0

    def __init__(
        self,
        db: Database,
        hardware: HardwareController,
        clock: Clock,
        settings: Settings,
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.hardware = hardware
        self.clock = clock
        self.settings = settings
        self.bus = bus
        self._lock = threading.Lock()            # dispense / confirm / cancel / close / present
        self._guard_lock = threading.RLock()     # protects _pending
        self._pending: _PendingRecord | None = None
        self._interrupt = threading.Event()      # set by interrupt(): abort before/while moving
        self._last_stop: _StopRecord | None = None
        self._current_dose: DoseInfo | None = None
        self._gate_possibly_open = False         # a dispense/present may have left the gate open
        self._lock_timeout_s = settings.timeout_home_s + settings.timeout_dispense_s + 10.0
        self._scheduler = Scheduler(db, clock, settings, bus=bus)

    # ================================================================== DoseServiceAPI
    def check_due(self) -> DueSummary:
        """What is due / awaiting / accessed / blocked now. Never raises."""
        self._flush_pending()
        now = self.clock.now()
        try:
            with self.db.session() as s:
                return safety.evaluate(s, now, self.settings).due_summary(self.clock)
        except Exception:  # noqa: BLE001 - fail closed, report through the summary
            log.exception("check_due: cannot read the schedule")
            return DueReport(now_local=self.clock.to_local(now), error=DispenseStatus.DB_ERROR.value)

    def dispense_next(
        self,
        source: IntentSource,
        *,
        on_motion_start: Callable[[DoseInfo], None] | None = None,
    ) -> DispenseOutcome:
        if not self._lock.acquire(blocking=False):
            log.info("dispense request (%s) refused: another operation is in progress", _source(source))
            return DispenseOutcome(DispenseStatus.IN_PROGRESS, reason=BlockReason.IN_PROGRESS.value)
        try:
            self._interrupt.clear()
            return self._dispense_locked(_source(source), on_motion_start)
        except Exception:  # noqa: BLE001 - last line of defence: never raise to the assistant
            log.exception("dispense_next failed unexpectedly")
            return DispenseOutcome(DispenseStatus.DB_ERROR, reason="INTERNAL_ERROR")
        finally:
            self._current_dose = None
            self._lock.release()

    def confirm_taken(self, source: IntentSource) -> ConfirmOutcome:
        src = _source(source)
        if not self._lock.acquire(timeout=self._lock_timeout_s):
            log.error("confirm_taken: device lock not available")
            return ConfirmOutcome(ConfirmStatus.DB_ERROR)
        try:
            if not self._flush_pending():
                with self._guard_lock:
                    pending = self._pending
                gate = self._close_gate_if_open("confirm while unrecorded")
                return ConfirmOutcome(ConfirmStatus.DB_ERROR, dose=pending.dose if pending else None,
                                      gate_closed=None if gate is None else gate.ok, hardware=gate)
            now = self.clock.now()
            payload: dict[str, Any] | None = None
            try:
                with self.db.session() as s:
                    ev = safety.find_confirmable(s, now, self.settings)
                    changed = None
                    if ev is not None:
                        changed = cas_transition(s, ev.event_id, DoseStatus.DISPENSED.value, {
                            "status": DoseStatus.TAKEN.value,
                            "confirmed_taken_at": now,
                            "confirm_source": src,
                        })
                    if changed is not None:
                        record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                                           action="DOSE_TAKEN", detail={"source": src})
                        payload = dose_update_payload(changed, self.clock,
                                                      previous=DoseStatus.DISPENSED.value, action="taken")
                        status, info = ConfirmStatus.CONFIRMED, safety.to_dose_info(changed, self.clock)
                    else:
                        latest = safety.latest_accessed(s, now, self.settings)
                        if latest is not None and latest.status == DoseStatus.TAKEN.value:
                            status, info = ConfirmStatus.ALREADY_CONFIRMED, safety.to_dose_info(latest, self.clock)
                        else:
                            status, info = ConfirmStatus.NOTHING_TO_CONFIRM, None
            except Exception:  # noqa: BLE001
                log.exception("confirm_taken: database error")
                return ConfirmOutcome(ConfirmStatus.DB_ERROR)
            if payload is not None:
                publish_all(self.bus, Topic.DOSE_UPDATED, [payload])
            if status is ConfirmStatus.CONFIRMED:
                gate = self._close_gate_if_open("confirmed taken")
                return ConfirmOutcome(status, info, gate_closed=None if gate is None else gate.ok, hardware=gate)
            return ConfirmOutcome(status, info)
        finally:
            self._lock.release()

    def cancel(self, source: IntentSource) -> CancelOutcome:
        if self.interrupt(source):
            stop = self._take_stop()
            result = stop.result if stop else None
            return CancelOutcome(
                CancelStatus.STOPPED_MOTION if result is not None and result.ok else CancelStatus.FAILED,
                dose=stop.dose if stop else None, hardware=result,
            )
        stop = self._take_stop(max_age_s=_RECENT_STOP_S)
        if stop is not None:
            # interrupt() already stopped the motion (assistant calls it synchronously first).
            ok = stop.result is not None and stop.result.ok
            return CancelOutcome(CancelStatus.STOPPED_MOTION if ok else CancelStatus.FAILED,
                                 dose=stop.dose, hardware=stop.result)
        if not self._lock.acquire(timeout=self._lock_timeout_s):
            log.error("cancel: device lock not available")
            return CancelOutcome(CancelStatus.FAILED)
        try:
            gate = self._close_gate_if_open("cancel")
            if gate is None:
                return CancelOutcome(CancelStatus.NOTHING_TO_CANCEL)
            # The dose stays DISPENSED: it was accessible, so duplicate prevention still applies.
            return CancelOutcome(CancelStatus.CLOSED_GATE if gate.ok else CancelStatus.FAILED,
                                 dose=self._awaiting_info(), hardware=gate)
        finally:
            self._lock.release()

    def interrupt(self, source: IntentSource) -> bool:
        """Send STOP now if carousel motion/homing is in flight. Never takes the lock."""
        snap = self._snapshot()
        in_flight = (snap.in_flight or "").split(" ")[0].upper() if snap is not None else ""
        moving = snap is not None and (in_flight in _MOTION_COMMANDS or snap.state in BUSY_STATES)
        if not moving:
            if self._lock.locked():
                # A dispense may be between its claim and DISPENSE_SLOT: make it abort before moving.
                self._interrupt.set()
            return False
        self._interrupt.set()
        try:
            result: CommandResult | None = self.hardware.stop()
        except Exception:  # noqa: BLE001
            log.exception("STOP failed")
            result = None
        self._last_stop = _StopRecord(at=time.monotonic(), result=result, dose=self._current_dose)
        log.warning("STOP sent on %s request while %s: %s", _source(source),
                    in_flight or snap.state.value, result.summary if result else "exception")
        return result is not None

    def awaiting_confirmation(self) -> DoseInfo | None:
        self._flush_pending()
        return self._awaiting_info()

    def close_gate(self, reason: str) -> CommandResult | None:
        if not self._lock.acquire(timeout=self._lock_timeout_s):
            log.error("close_gate(%s): device lock not available", reason)
            return CommandResult.host_failure(Command.close_gate(), HostCode.BUSY_LOCAL, detail="lock timeout")
        try:
            return self._close_gate_if_open(reason)
        finally:
            self._lock.release()

    # ================================================================== dispense internals
    def _dispense_locked(self, src: str, on_motion_start: Callable[[DoseInfo], None] | None) -> DispenseOutcome:
        if not self._flush_pending():
            with self._guard_lock:
                pending = self._pending
            log.error("dispensing blocked: an earlier dose outcome is still unrecorded")
            return DispenseOutcome(DispenseStatus.DB_ERROR, dose=pending.dose if pending else None, reason=UNRECORDED)
        try:
            with self.db.session() as s:
                first = _Eval.of(safety.evaluate(s, self.clock.now(), self.settings), self.clock)
        except Exception:  # noqa: BLE001 - state unknown: fail closed, nothing is sent
            log.exception("cannot read dose state; refusing to dispense")
            return DispenseOutcome(DispenseStatus.DB_ERROR)
        if first.verdict is not Verdict.ALLOW:
            return self._refusal(first, src)
        self._current_dose = first.dose

        announced: set[int] = set()

        def announce(info: DoseInfo | None) -> None:
            # Called right before the first carousel motion (HOME or DISPENSE_SLOT) for a dose.
            if on_motion_start is None or info is None or info.event_id in announced:
                return
            announced.add(info.event_id)
            try:
                on_motion_start(info)
            except Exception:  # noqa: BLE001 - advisory callback
                log.exception("on_motion_start callback failed")

        prep = self._prepare_hardware(before_motion=lambda: announce(first.dose))
        if not prep.ok:
            return self._prep_failure(prep, first.dose, src)

        claim_or_outcome = self._claim(src)
        if isinstance(claim_or_outcome, DispenseOutcome):
            return claim_or_outcome
        claim = claim_or_outcome
        self._current_dose = claim.dose
        try:
            announce(claim.dose)
            if self._interrupt.is_set():
                return self._revert_claim(claim)
            result = self._send(Command(CommandName.DISPENSE_SLOT, claim.slot),
                                lambda: self.hardware.dispense_slot(claim.slot))
            return self._finish(claim, result)
        except Exception:  # noqa: BLE001 - a bug after the claim: the outcome is unknown
            log.exception("unexpected error while dispensing %s; locking it for review", claim.dose.label)
            return self._lock_for_review(claim, "INTERNAL_ERROR")

    def _lock_for_review(self, claim: _Claim, why: str) -> DispenseOutcome:
        """Claimed dose with an unknown outcome -> HARDWARE_ERROR(needs_review); never retried."""
        self._gate_possibly_open = True
        values = {"status": DoseStatus.HARDWARE_ERROR.value, "needs_review": True,
                  "hardware_result": f"UNCERTAIN {why}"}
        rec = _PendingRecord(claim.event_id, values, "DOSE_HARDWARE_ERROR", {"why": why},
                             ("dispense_uncertain", why),
                             replace(claim.dose, status=DoseStatus.HARDWARE_ERROR.value, needs_review=True,
                                     hardware_result=values["hardware_result"]))
        _ok, dose = self._record(rec)
        return DispenseOutcome(DispenseStatus.HARDWARE_ERROR, dose=dose, reason=why)

    def _refusal(self, ev: _Eval, src: str) -> DispenseOutcome:
        if ev.verdict is Verdict.IN_PROGRESS:
            out = DispenseOutcome(DispenseStatus.IN_PROGRESS, dose=ev.dose, reason=BlockReason.IN_PROGRESS.value)
        elif ev.verdict is Verdict.DUPLICATE:
            reason = BlockReason.TOO_SOON.value if ev.reason is BlockReason.TOO_SOON else ALREADY_ACCESSED
            out = DispenseOutcome(DispenseStatus.DUPLICATE, dose=ev.dose, reason=reason)
        elif ev.verdict is Verdict.BLOCKED:
            out = DispenseOutcome(DispenseStatus.BLOCKED, dose=ev.dose,
                                  reason=ev.reason.value if ev.reason else "")
        else:
            out = DispenseOutcome(DispenseStatus.NOTHING_DUE, next_upcoming=ev.next_upcoming)
        self._audit(LogCategory.SAFETY, "DISPENSE_REFUSED",
                    {"status": out.status.value, "reason": out.reason, "source": src},
                    event_id=ev.dose.event_id if ev.dose else None)
        log.info("dispense refused: %s %s", out.status.value, out.reason)
        return out

    def _prep_failure(self, prep: _Prep, dose: DoseInfo | None, src: str) -> DispenseOutcome:
        if prep.cancelled:
            self._audit(LogCategory.SAFETY, "DISPENSE_CANCELLED", {"stage": "prepare", "source": src},
                        event_id=dose.event_id if dose else None)
            return DispenseOutcome(DispenseStatus.CANCELLED, dose=dose, reason=Err.STOPPED.value, hardware=prep.result)
        self._audit(
            LogCategory.HARDWARE, "DISPENSE_HW_UNAVAILABLE",
            {"reason": prep.reason, "result": prep.result.hardware_result if prep.result else None, "source": src},
            event_id=dose.event_id if dose else None,
            device_event=("dispense_refused", prep.reason),
        )
        log.warning("dispense refused, hardware unavailable: %s", prep.reason)
        return DispenseOutcome(DispenseStatus.HARDWARE_UNAVAILABLE, dose=dose, reason=prep.reason, hardware=prep.result)

    def _claim(self, src: str) -> _Claim | DispenseOutcome:
        """Re-evaluate and claim the selected dose with a compare-and-set (ARCHITECTURE §5 Claim)."""
        hw_slots = getattr(self.hardware, "num_slots", self.settings.num_slots)
        for _attempt in range(3):
            payload: dict[str, Any] | None = None
            refusal: _Eval | None = None
            claim: _Claim | None = None
            try:
                with self.db.session() as s:
                    decision = safety.evaluate(s, self.clock.now(), self.settings)
                    if not decision.allowed or decision.event is None or decision.slot is None:
                        refusal = _Eval.of(decision, self.clock)
                    elif decision.slot >= hw_slots:
                        log.error("slot %s is outside the hardware's %s slots", decision.slot, hw_slots)
                        return DispenseOutcome(DispenseStatus.HARDWARE_UNAVAILABLE,
                                               dose=decision.dose_info(self.clock), reason=Err.INVALID_SLOT.value)
                    else:
                        ev, previous = decision.event, decision.event.status
                        changed = cas_transition(s, ev.event_id, previous, {
                            "status": DoseStatus.DISPENSING.value,
                            "attempts": DoseEvent.attempts + 1,
                            "compartment_id": decision.compartment_id,
                            "slot_number": decision.slot,
                            "dispense_source": src,
                        }, require_no_review=True)
                        if changed is not None:
                            record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                                               action="DOSE_DISPENSING",
                                               detail={"previous": previous, "slot": decision.slot,
                                                       "source": src, "attempt": changed.attempts})
                            payload = dose_update_payload(changed, self.clock, previous=previous, action="dispensing")
                            claim = _Claim(changed.event_id, previous, int(changed.attempts), decision.slot,
                                           safety.to_dose_info(changed, self.clock))
            except Exception:  # noqa: BLE001 - nothing has been sent for this dose yet
                log.exception("claim failed; refusing to dispense")
                return DispenseOutcome(DispenseStatus.DB_ERROR)
            if refusal is not None:
                return self._refusal(refusal, src)
            if claim is not None:
                publish_all(self.bus, Topic.DOSE_UPDATED, [payload] if payload else [])
                return claim
            log.info("dose changed while claiming; re-evaluating")
        return DispenseOutcome(DispenseStatus.IN_PROGRESS, reason="CLAIM_CONFLICT")

    def _revert_claim(self, claim: _Claim) -> DispenseOutcome:
        """Cancel arrived after the claim but before DISPENSE_SLOT: nothing was sent, undo the claim."""
        target = (DoseStatus.DUE.value
                  if claim.previous in (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value) else claim.previous)
        rec = _PendingRecord(
            event_id=claim.event_id,
            values={"status": target, "attempts": max(0, claim.attempts - 1)},
            action="DOSE_CLAIM_REVERTED",
            detail={"why": "cancelled before motion"},
            device_event=None,
            dose=replace(claim.dose, status=target, attempts=max(0, claim.attempts - 1)),
        )
        _ok, dose = self._record(rec)
        return DispenseOutcome(DispenseStatus.CANCELLED, dose=dose, reason="CANCELLED")

    def _finish(self, claim: _Claim, result: CommandResult) -> DispenseOutcome:
        """Record the DISPENSE_SLOT outcome (ARCHITECTURE §5 outcome mapping table)."""
        now = self.clock.now()
        code = result.code
        detail: dict[str, Any] = {"slot": claim.slot, "result": result.hardware_result, "attempt": claim.attempts}
        if result.ok and code == Ok.GATE_OPEN.value:
            status, target, reason = DispenseStatus.DISPENSED, DoseStatus.DISPENSED.value, ""
            values: dict[str, Any] = {"status": target, "dispensed_at": now,
                                      "hardware_result": result.hardware_result, "needs_review": False}
            device_event = None
            self._gate_possibly_open = True
            dose = replace(claim.dose, status=target, dispensed_at=now, hardware_result=result.hardware_result)
        elif not result.ok and result.definitive and code == Err.STOPPED.value:
            # User cancel: nothing was dispensed, the dose is due again.
            status, target, reason = DispenseStatus.CANCELLED, DoseStatus.DUE.value, code
            values = {"status": target, "hardware_result": result.hardware_result}
            device_event = None
            dose = replace(claim.dose, status=target, hardware_result=result.hardware_result)
        else:
            uncertain = (not result.definitive) or result.gate_may_be_open or result.ok
            needs_review = uncertain or claim.attempts >= self.settings.max_dispense_attempts
            status, target, reason = DispenseStatus.HARDWARE_ERROR, DoseStatus.HARDWARE_ERROR.value, code
            values = {"status": target, "hardware_result": result.hardware_result, "needs_review": needs_review}
            device_event = ("dispense_uncertain" if uncertain else "dispense_failed", code)
            detail.update({"uncertain": uncertain, "needs_review": needs_review})
            if uncertain:
                self._gate_possibly_open = True
            dose = replace(claim.dose, status=target, needs_review=needs_review,
                           hardware_result=result.hardware_result)
        rec = _PendingRecord(claim.event_id, values, f"DOSE_{target}", detail, device_event, dose)
        recorded, dose = self._record(rec)
        log.info("dispense of %s -> %s (%s)%s", claim.dose.label, target, result.hardware_result,
                 "" if recorded else " [UNRECORDED]")
        if not recorded:
            if status is DispenseStatus.DISPENSED:
                # The gate IS open: the user must be told; further dispensing stays blocked.
                return DispenseOutcome(status, dose=dose, reason=UNRECORDED, hardware=result)
            return DispenseOutcome(status, dose=dose, reason=reason, hardware=result)
        return DispenseOutcome(status, dose=dose, reason=reason, hardware=result,
                               remaining_due=self._remaining_due())

    def _remaining_due(self) -> int:
        try:
            with self.db.session() as s:
                return len(safety.evaluate(s, self.clock.now(), self.settings).due)
        except Exception:  # noqa: BLE001
            log.warning("could not compute remaining doses", exc_info=True)
            return 0

    # ================================================================== hardware helpers
    def _prepare_hardware(self, before_motion: Callable[[], None] | None = None) -> _Prep:
        """ARCHITECTURE §5 "Hardware preparation before the claim". No dose state is touched."""
        hw = self.hardware
        snap = self._snapshot()
        if snap is None:
            return _Prep(False, "NO_STATUS")
        if not snap.connected:
            return _Prep(False, HostCode.NOT_CONNECTED.value)
        if snap.state is DeviceState.UNKNOWN or snap.homed is None or not snap.responsive:
            r = self._send(Command.status(), hw.status)        # resynchronise the host mirror
            if not r.ok:
                return _Prep(False, r.code, r)
            snap = self._snapshot() or snap
            if not snap.connected:
                return _Prep(False, HostCode.NOT_CONNECTED.value)
            if snap.state is DeviceState.UNKNOWN:
                return _Prep(False, f"NOT_READY_{DeviceState.UNKNOWN.value}")   # never move blind
        if snap.state is DeviceState.FAULT:
            return _Prep(False, DeviceState.FAULT.value)       # handoff §30: caregiver must re-home
        if snap.state is DeviceState.HOMING:
            snap = self._wait_while_homing()
            if self._interrupt.is_set():
                return _Prep(False, Err.STOPPED.value, cancelled=True)
            if snap is None:
                return _Prep(False, "NO_STATUS")
            if not snap.connected:
                return _Prep(False, HostCode.NOT_CONNECTED.value)
            if snap.state is DeviceState.HOMING:
                return _Prep(False, "HOMING_TIMEOUT")
            if snap.state is DeviceState.FAULT:
                return _Prep(False, DeviceState.FAULT.value)
        in_flight = (snap.in_flight or "").split(" ")[0].upper()
        if snap.state in (DeviceState.MOVING, DeviceState.AT_TARGET) or in_flight in _MOTION_COMMANDS:
            return _Prep(False, Err.BUSY.value)
        if snap.gate is GateState.OPEN or snap.state is DeviceState.GATE_OPEN:
            r = self._send(Command.close_gate(), hw.close_gate)
            self._after_close(r, "prepare dispense")
            if not r.ok:
                return _Prep(False, r.code, r)
            snap = self._snapshot() or snap
        if snap.state in (DeviceState.SAFE_STOP, DeviceState.BOOT) or snap.homed is not True:
            if not self.settings.hw_auto_home:
                return _Prep(False, Err.NOT_HOMED.value)
            if self._interrupt.is_set():
                return _Prep(False, Err.STOPPED.value, cancelled=True)
            if before_motion is not None:
                before_motion()
            r = self._send(Command.home(), hw.home)
            if self._interrupt.is_set() or r.code == Err.STOPPED.value:
                return _Prep(False, Err.STOPPED.value, r, cancelled=True)
            if not r.ok:
                return _Prep(False, r.code, r)
            snap = self._snapshot() or snap
        if self._interrupt.is_set():
            return _Prep(False, Err.STOPPED.value, cancelled=True)
        if not snap.ready_for_motion:
            return _Prep(False, f"NOT_READY_{snap.state.value}")
        return _Prep(True)

    def _wait_while_homing(self) -> DeviceSnapshot | None:
        deadline = time.monotonic() + self.settings.timeout_home_s
        while True:
            snap = self._snapshot()
            if snap is None or snap.state is not DeviceState.HOMING or not snap.connected:
                return snap
            if self._interrupt.is_set() or time.monotonic() >= deadline:
                return snap
            time.sleep(self.poll_interval_s)

    def _send(self, command: Command, fn: Callable[[], CommandResult]) -> CommandResult:
        """Run one hardware command; retry (bounded) while another host command holds the link."""
        deadline = time.monotonic() + self.busy_wait_s
        while True:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - contract: never raises; if it does, outcome is uncertain
                log.exception("hardware %s raised", command.to_line())
                return CommandResult.host_failure(command, HostCode.DISCONNECTED,
                                                  detail=f"exception: {type(exc).__name__}")
            if result.code != HostCode.BUSY_LOCAL.value or time.monotonic() >= deadline:
                return result
            # BUSY_LOCAL = never written (e.g. a heartbeat PING in flight): safe to retry.
            time.sleep(min(self.poll_interval_s, 0.02))

    def _snapshot(self) -> DeviceSnapshot | None:
        try:
            return self.hardware.snapshot()
        except Exception:  # noqa: BLE001
            log.exception("hardware snapshot failed")
            return None

    def _gate_maybe_open(self, snap: DeviceSnapshot | None) -> bool:
        if snap is None:
            return self._gate_possibly_open
        return (
            snap.gate is GateState.OPEN
            or snap.state is DeviceState.GATE_OPEN
            or (snap.gate is GateState.UNKNOWN and self._gate_possibly_open)
        )

    def _close_gate_if_open(self, reason: str) -> CommandResult | None:
        """CLOSE_GATE if the gate is (or, right after a dispense, may be) open. Lock must be held."""
        if not self._gate_maybe_open(self._snapshot()):
            return None
        result = self._send(Command.close_gate(), self.hardware.close_gate)
        self._after_close(result, reason)
        return result

    def _after_close(self, result: CommandResult, reason: str) -> None:
        if result.ok:
            self._gate_possibly_open = False
            self._audit(LogCategory.HARDWARE, "GATE_CLOSED", {"reason": reason})
        else:
            log.warning("CLOSE_GATE (%s) failed: %s", reason, result.hardware_result)
            self._audit(LogCategory.HARDWARE, "GATE_CLOSE_FAILED",
                        {"reason": reason, "result": result.hardware_result},
                        device_event=("gate_close_failed", result.code))

    def _awaiting_info(self) -> DoseInfo | None:
        with self._guard_lock:
            pending = self._pending
        if pending is not None and pending.dose.status == DoseStatus.DISPENSED.value:
            return pending.dose
        try:
            with self.db.session() as s:
                ev = safety.find_confirmable(s, self.clock.now(), self.settings)
                return safety.to_dose_info(ev, self.clock) if ev is not None else None
        except Exception:  # noqa: BLE001
            log.warning("awaiting_confirmation: database error", exc_info=True)
            return None

    def _take_stop(self, max_age_s: float | None = None) -> _StopRecord | None:
        rec = self._last_stop
        if rec is None or rec.reported:
            return None
        if max_age_s is not None and time.monotonic() - rec.at > max_age_s:
            return None
        rec.reported = True
        return rec

    # ================================================================== outcome recording
    def _record(self, rec: _PendingRecord) -> tuple[bool, DoseInfo]:
        """Write an outcome (3 tries). On failure keep it pending: dispensing is blocked."""
        delays = tuple(self.db_retry_delays_s)
        tries = len(delays) + 1
        for i in range(tries):
            try:
                return True, self._write_outcome(rec)
            except Exception:  # noqa: BLE001
                log.warning("could not record %s for event %s (try %d/%d)", rec.action, rec.event_id,
                            i + 1, tries, exc_info=True)
                if i < len(delays):
                    time.sleep(delays[i])
        with self._guard_lock:
            self._pending = rec
        log.error("dose outcome %s for event %s NOT recorded; dispensing blocked until it is",
                  rec.action, rec.event_id)
        if self.bus is not None:
            self.bus.publish(Topic.NOTICE, {
                "level": "error",
                "message": "A dose outcome could not be saved. Dispensing is paused; please ask for assistance.",
            })
        return False, rec.dose

    def _write_outcome(self, rec: _PendingRecord) -> DoseInfo:
        payload: dict[str, Any] | None = None
        with self.db.session() as s:
            changed = cas_transition(s, rec.event_id, DoseStatus.DISPENSING.value, rec.values)
            if changed is None:
                current = s.get(DoseEvent, rec.event_id)
                log.error("event %s is %s (not DISPENSING); outcome %s not applied", rec.event_id,
                          current.status if current else "missing", rec.action)
                log_event(s, self.settings.device_id, LogCategory.SAFETY, "OUTCOME_NOT_APPLIED",
                          {"action": rec.action, "status": current.status if current else None},
                          event_id=rec.event_id)
                return safety.to_dose_info(current, self.clock) if current is not None else rec.dose
            record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                               action=rec.action, detail=rec.detail)
            if rec.device_event is not None:
                enqueue_device_event(s, device_id=self.settings.device_id, event_type=rec.device_event[0],
                                     code=rec.device_event[1], detail={"command": CommandName.DISPENSE_SLOT.value})
            payload = dose_update_payload(changed, self.clock, previous=DoseStatus.DISPENSING.value,
                                          action=rec.action.removeprefix("DOSE_").lower())
            info = safety.to_dose_info(changed, self.clock)
        publish_all(self.bus, Topic.DOSE_UPDATED, [payload])
        return info

    def _flush_pending(self) -> bool:
        """Retry writing an unrecorded outcome. True when nothing is pending any more."""
        with self._guard_lock:
            rec = self._pending
            if rec is None:
                return True
            try:
                self._write_outcome(rec)
            except Exception:  # noqa: BLE001
                log.warning("unrecorded dose outcome still cannot be written", exc_info=True)
                return False
            self._pending = None
        log.info("previously unrecorded outcome %s for event %s is now recorded", rec.action, rec.event_id)
        return True

    @property
    def has_unrecorded_outcome(self) -> bool:
        with self._guard_lock:
            return self._pending is not None

    def _audit(
        self,
        category: LogCategory,
        event: str,
        detail: dict[str, Any],
        *,
        event_id: int | None = None,
        device_event: tuple[str, str] | None = None,
    ) -> None:
        """Best-effort audit row (+ de-identified device event). Never raises."""
        try:
            with self.db.session() as s:
                log_event(s, self.settings.device_id, category, event, detail, event_id=event_id)
                if device_event is not None:
                    enqueue_device_event(s, device_id=self.settings.device_id, event_type=device_event[0],
                                         code=device_event[1])
        except Exception:  # noqa: BLE001
            log.warning("audit log write failed (%s)", event, exc_info=True)

    # ================================================================== startup / queries
    def recover_on_startup(self) -> int:
        """Any DISPENSING dose left by a crash -> HARDWARE_ERROR(needs_review, 'UNCERTAIN RESTART')."""
        payloads: list[dict[str, Any]] = []
        try:
            with self.db.session() as s:
                rows = s.scalars(
                    select(DoseEvent)
                    .options(selectinload(DoseEvent.medication))
                    .where(DoseEvent.device_id == self.settings.device_id,
                           DoseEvent.status == DoseStatus.DISPENSING.value)
                ).all()
                for ev in rows:
                    changed = cas_transition(s, ev.event_id, DoseStatus.DISPENSING.value, {
                        "status": DoseStatus.HARDWARE_ERROR.value,
                        "needs_review": True,
                        "hardware_result": "UNCERTAIN RESTART",
                    })
                    if changed is None:
                        continue
                    record_dose_change(s, changed, settings=self.settings, clock=self.clock,
                                       action="DOSE_RECOVERED", detail={"why": "restart while dispensing"})
                    enqueue_device_event(s, device_id=self.settings.device_id, event_type="uncertain_restart",
                                         code="RESTART", detail={"command": CommandName.DISPENSE_SLOT.value})
                    payloads.append(dose_update_payload(changed, self.clock, previous=DoseStatus.DISPENSING.value,
                                                        action="recovered"))
        except Exception:  # noqa: BLE001 - leaving DISPENSING rows blocks dispensing (fail closed)
            log.exception("startup recovery failed; in-flight doses stay locked")
            return 0
        if payloads:
            self._gate_possibly_open = True
            log.warning("startup recovery: %d dose(s) had an uncertain outcome and need review", len(payloads))
        publish_all(self.bus, Topic.DOSE_UPDATED, payloads)
        return len(payloads)

    def list_events(self, local_date: date | None = None) -> list[dict[str, Any]]:
        """``DoseEventView`` dicts for one local day (default today), ordered by time."""
        day = local_date or self.clock.today_local()
        start = self.clock.local_to_utc(datetime.combine(day, dtime(0, 0)))
        end = self.clock.local_to_utc(datetime.combine(day + timedelta(days=1), dtime(0, 0)))
        with self.db.session() as s:
            rows = s.scalars(
                select(DoseEvent)
                .options(selectinload(DoseEvent.medication))
                .where(DoseEvent.device_id == self.settings.device_id,
                       DoseEvent.scheduled_at >= start, DoseEvent.scheduled_at < end)
                .order_by(DoseEvent.scheduled_at, DoseEvent.event_id)
            ).all()
            slots = assigned_slots(s, self.settings)
            return [event_view(s, ev, self.clock, self.settings, slots) for ev in rows]

    def get_event(self, event_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            ev = s.get(DoseEvent, event_id) if is_id(event_id) else None
            if ev is None:
                raise NotFoundError(f"Dose event {event_id} not found.")
            return event_view(s, ev, self.clock, self.settings)

    # ================================================================== caregiver operations
    def resolve_review(self, event_id: int, *, accessed: bool, note: str | None = None,
                       by: str | None = None) -> dict[str, Any]:
        """HARDWARE_ERROR -> DISPENSED (accessed) or DUE / MISSED (not accessed; attempts reset)."""
        if not isinstance(accessed, bool):
            raise ValidationError("accessed must be true or false.")
        note_text = _clean_note(note)

        def values(ev: DoseEvent, now: datetime) -> dict[str, Any]:
            out: dict[str, Any] = {"needs_review": False}
            if accessed:
                out.update(status=DoseStatus.DISPENSED.value, dispensed_at=ev.dispensed_at or now)
            else:
                start, end = safety.dispense_window(ev, self.settings)
                if now > end:
                    out.update(status=DoseStatus.MISSED.value, missed_at=now)
                else:
                    status = DoseStatus.DUE if now >= start else DoseStatus.SCHEDULED
                    out.update(status=status.value, attempts=0)
            if note_text is not None:
                out["review_note"] = note_text
            return out

        return self._caregiver_transition(
            event_id, (DoseStatus.HARDWARE_ERROR.value,), values, "DOSE_REVIEW_RESOLVED",
            {"accessed": accessed, "by": _clean_note(by, "by")}, "resolved",
        )

    def skip_dose(self, event_id: int, *, note: str | None = None, by: str | None = None) -> dict[str, Any]:
        """SCHEDULED / DUE / HARDWARE_ERROR -> CANCELLED."""
        note_text = _clean_note(note)

        def values(_ev: DoseEvent, now: datetime) -> dict[str, Any]:
            out: dict[str, Any] = {"status": DoseStatus.CANCELLED.value, "cancelled_at": now}
            if note_text is not None:
                out["review_note"] = note_text
            return out

        return self._caregiver_transition(
            event_id,
            (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value, DoseStatus.HARDWARE_ERROR.value),
            values, "DOSE_SKIPPED", {"by": _clean_note(by, "by")}, "skipped",
        )

    def mark_taken_by_caregiver(self, event_id: int, *, by: str | None = None) -> dict[str, Any]:
        """DISPENSED -> TAKEN (confirm_source 'caregiver')."""
        return self._caregiver_transition(
            event_id, (DoseStatus.DISPENSED.value,),
            lambda _ev, now: {"status": DoseStatus.TAKEN.value, "confirmed_taken_at": now,
                              "confirm_source": "caregiver"},
            "DOSE_MARKED_TAKEN", {"by": _clean_note(by, "by")}, "marked taken",
        )

    def _caregiver_transition(
        self,
        event_id: int,
        allowed: tuple[str, ...],
        values_for: Callable[[DoseEvent, datetime], dict[str, Any]],
        action: str,
        detail: dict[str, Any],
        verb: str,
    ) -> dict[str, Any]:
        now = self.clock.now()
        with self.db.session() as s:
            ev = s.get(DoseEvent, event_id) if is_id(event_id) else None
            if ev is None:
                raise NotFoundError(f"Dose event {event_id} not found.")
            if ev.status not in allowed:
                raise ConflictError(f"{ev.label} is {ev.status}; it cannot be {verb}.")
            previous = ev.status
            changed = cas_transition(s, ev.event_id, previous, values_for(ev, now))
            if changed is None:
                raise ConflictError(f"{ev.label} changed at the same time; reload and try again.")
            record_dose_change(s, changed, settings=self.settings, clock=self.clock, action=action,
                               detail={"previous": previous, **detail}, category=LogCategory.ADMIN)
            payload = dose_update_payload(changed, self.clock, previous=previous,
                                          action=action.removeprefix("DOSE_").lower())
            view = event_view(s, changed, self.clock, self.settings)
        publish_all(self.bus, Topic.DOSE_UPDATED, [payload])
        log.info("%s %s by %s (%s -> %s)", view["label"], verb, detail.get("by"), previous, view["status"])
        return view

    # ------------------------------------------------------------------ loading mode
    def present_compartment(self, slot: int, *, by: str | None = None) -> CommandResult:
        """Caregiver loading: rotate ``slot`` to the gate and open it (MOVE_SLOT + OPEN_GATE)."""
        self._check_slot(slot)
        if not self._lock.acquire(blocking=False):
            raise ConflictError("The device is busy; try again in a moment.")
        try:
            self._interrupt.clear()
            if not self._flush_pending():
                raise ConflictError("A dose outcome has not been saved yet; resolve the database problem first.")
            try:
                with self.db.session() as s:
                    awaiting = safety.find_confirmable(s, self.clock.now(), self.settings)
                    awaiting_label = awaiting.label if awaiting is not None else None
                    dispensing = s.scalars(select(DoseEvent.event_id).where(
                        DoseEvent.device_id == self.settings.device_id,
                        DoseEvent.status == DoseStatus.DISPENSING.value).limit(1)).first()
            except Exception as exc:  # noqa: BLE001 - cannot prove it is safe: refuse
                log.exception("present_compartment: database error")
                raise ConflictError("Cannot read the dose state right now (database unavailable).") from exc
            if awaiting_label is not None:
                raise ConflictError(f"{awaiting_label} is awaiting confirmation; confirm it or close the gate first.")
            if dispensing is not None:
                raise ConflictError("A dose is being dispensed.")
            snap = self._snapshot()
            if snap is not None and snap.state is DeviceState.FAULT:
                raise ConflictError("The device is in FAULT; re-home it before loading.")
            if snap is not None and snap.state in BUSY_STATES:
                raise ConflictError("The device is busy; try again in a moment.")
            move = Command(CommandName.MOVE_SLOT, slot)
            prep = self._prepare_hardware()
            if not prep.ok:
                result = prep.result or CommandResult(command=move, ok=False, code=prep.reason or "NOT_READY",
                                                      definitive=True, detail="refused before sending")
            else:
                result = self._send(move, lambda: self.hardware.move_slot(slot))
                if result.ok:
                    result = self._send(Command.open_gate(), self.hardware.open_gate)
                    if result.ok or result.gate_may_be_open:
                        self._gate_possibly_open = True
            self._audit(LogCategory.ADMIN, "PRESENT_COMPARTMENT",
                        {"slot": slot, "by": _clean_note(by, "by"), "result": result.hardware_result},
                        device_event=None if result.ok else ("present_failed", result.code))
            return result
        finally:
            self._lock.release()

    def finish_loading(self, slot: int, *, by: str | None = None) -> CommandResult:
        """Caregiver finished loading ``slot``: CLOSE_GATE and stamp ``Compartment.loaded_at``."""
        self._check_slot(slot)
        if not self._lock.acquire(timeout=self._lock_timeout_s):
            raise ConflictError("The device is busy; try again in a moment.")
        try:
            result = self._send(Command.close_gate(), self.hardware.close_gate)
            self._after_close(result, "loading finished")
            try:
                with self.db.session() as s:
                    comp = s.scalars(select(Compartment).where(
                        Compartment.device_id == self.settings.device_id,
                        Compartment.slot_number == slot)).first()
                    if comp is not None:
                        comp.loaded_at = self.clock.now()
                    log_event(s, self.settings.device_id, LogCategory.ADMIN, "COMPARTMENT_LOADED",
                              {"slot": slot, "by": _clean_note(by, "by"), "gate": result.hardware_result})
            except Exception:  # noqa: BLE001 - the gate result matters more to the caller
                log.exception("finish_loading: could not stamp loaded_at for slot %s", slot)
            if self.bus is not None:
                self.bus.publish(Topic.DATA_CHANGED, {"entity": "compartment", "id": slot})
            return result
        finally:
            self._lock.release()

    def _check_slot(self, slot: int) -> None:
        n = self.settings.num_slots
        if not is_id(slot) or not 0 <= slot < n:
            raise ValidationError(f"Slot must be a whole number from 0 to {n - 1}.")

    # ------------------------------------------------------------------ demo helper
    def create_demo_dose_now(self, medication_id: int | None = None) -> dict[str, Any]:
        """Demo: schedule ``medication_id`` (default: first assigned, active, confirmed one) daily
        at the current local HH:MM so a dose is DUE right now. Returns ``{schedule, event}``."""
        local_now = self.clock.local_now()
        hhmm = f"{local_now.hour:02d}:{local_now.minute:02d}"
        with self.db.session() as s:
            if medication_id is not None:
                med = s.get(Medication, medication_id) if is_id(medication_id) else None
                if med is None:
                    raise NotFoundError(f"Medication {medication_id} not found.")
                if not med.active or not med.confirmed_by_user:
                    raise ValidationError("Only an active, confirmed medication can be scheduled.")
            else:
                med = None
                for mid, _ in sorted(assigned_slots(s, self.settings).items(), key=lambda kv: kv[1][0]):
                    candidate = s.get(Medication, mid)
                    if candidate is not None and candidate.active and candidate.confirmed_by_user:
                        med = candidate
                        break
                if med is None:
                    raise ValidationError("No confirmed medication is assigned to a compartment.")
            med_id = med.medication_id
            existing = s.scalars(select(Schedule).where(
                Schedule.medication_id == med_id, Schedule.time_of_day == hhmm,
                Schedule.frequency == Frequency.DAILY.value, Schedule.active.is_(True),
            ).order_by(Schedule.schedule_id).limit(1)).first()
            schedule_id = existing.schedule_id if existing is not None else None
        if schedule_id is None:
            schedule = self._scheduler.create_schedule(med_id, hhmm, Frequency.DAILY.value)   # ticks
            schedule_id = schedule["schedule_id"]
        else:
            self._scheduler.tick()
            schedule = self._scheduler.get_schedule(schedule_id)
        at = self.clock.local_to_utc(datetime.combine(local_now.date(), dtime(local_now.hour, local_now.minute)))
        with self.db.session() as s:
            ev = s.scalars(select(DoseEvent).where(
                DoseEvent.schedule_id == schedule_id, DoseEvent.scheduled_at == at)).first()
            if ev is None:
                raise ConflictError("Could not create a dose for right now: this schedule's dose for today "
                                    "was already dispensed.")
            view = event_view(s, ev, self.clock, self.settings)
        log.info("demo dose %s created for medication %s at %s", view["label"], med_id, hhmm)
        return {"schedule": schedule, "event": view}
