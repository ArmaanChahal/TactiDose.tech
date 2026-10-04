"""Intent orchestrator: the single action worker behind every user action.

Every request (voice, the big tactile button, kiosk buttons, typed demo text, the HTTP API)
becomes an :class:`Intent`. It goes through :meth:`Assistant.submit` into one queue served
by the ``assistant`` thread. The worker calls the deterministic :class:`DoseServiceAPI`
(blocking), turns the outcome into a :class:`Reply` using :mod:`tactidose.core.phrases`,
speaks it and publishes ``Topic.ASSISTANT_STATE``. Speech is a request, not an
authorization: only the dose service decides whether anything moves (ARCHITECTURE §1).

Dialogue contract (ARCHITECTURE §6):

* CHECK_DUE announces what is due and asks for consent ("Say 'dispense' or press the big
  button"). It dispenses directly only with ``check_due_auto_dispense``.
* "Preparing your dose. Please keep your hands clear of the opening." is spoken right
  before motion, via ``dispense_next(on_motion_start=...)``.
* After DISPENSED, a gate timer (``gate_open_timeout_s``, ``Clock.monotonic``) queues
  GATE_TIMEOUT, which calls ``dose.close_gate('timeout')`` and says "I've closed the
  compartment. If you took your dose, say 'taken'." A confirmation, a cancel or
  :meth:`Assistant.close` cancels the timer.
* PRIMARY_ACTION (big button / kiosk): confirm if a dose awaits confirmation, else
  dispense if one is due, else check.
* CANCEL takes effect immediately. :meth:`Assistant.submit` calls ``dose.interrupt()`` on
  the caller's thread *before* queueing (the worker may be blocked inside a dispense) and
  drops queued motion requests. The user hears "Cancelled" once, not twice.
* Hardware events arrive on the serial reader thread and are only queued. An unsolicited
  device FAULT (seen on ``Topic.DEVICE_STATE``) is announced.
* REPEAT replays the last reply. Unknown or low-confidence speech is ignored silently
  unless it contained real words; negated phrases ("I haven't taken it") never confirm.
* A failing handler never kills the worker. The user hears "Please ask for assistance."
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from tactidose.config import Settings
from tactidose.core import phrases
from tactidose.core.bus import BusEvent, EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import (
    BlockReason,
    CancelStatus,
    ConfirmStatus,
    DispenseStatus,
    DoseInfo,
    DoseServiceAPI,
    DueSummary,
    Intent,
    IntentSource,
    ParsedIntent,
    Reply,
    ReplyKind,
    Speaker,
)
from tactidose.hardware.protocol import Err, Ev, Message
from tactidose.voice.intents import content_words, normalise, parse_intent

log = logging.getLogger(__name__)


class Phase(str, Enum):
    """``state()["phase"]`` / ``Topic.ASSISTANT_STATE`` values (docs/API.md)."""

    IDLE = "IDLE"
    PREPARING = "PREPARING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    ATTENTION = "ATTENTION"


#: Queued intents that may move the carousel; a CANCEL drops them before they start.
_MOTION_INTENTS = frozenset({Intent.DISPENSE, Intent.PRIMARY_ACTION})
_TAKEN = "TAKEN"
_KEEP: Any = object()


@dataclass
class _Job:
    seq: int
    intent: Intent
    source: IntentSource
    text: str = ""
    confidence: float = 1.0
    kind: str = "intent"                     # intent | unknown | notice | refresh
    future: Future[Reply | None] | None = None
    parsed: ParsedIntent | None = None
    response: str = ""                       # kind "unknown": "sorry" | "negated"
    notice: str = ""                         # kind "notice": "BOOT" | "FAULT"
    gate_gen: int | None = None              # timer GATE_TIMEOUT: generation it was armed with
    active_seq: int | None = None            # CANCEL: job being handled when it arrived
    dispensed: DoseInfo | None = None        # set when this job dispensed a dose
    published: bool = False


def _dose_key(dose: DoseInfo | None) -> tuple[int, str] | None:
    return (dose.event_id, dose.status) if dose is not None else None


class Assistant:
    """Single action worker + dialogue state. The public API is thread-safe."""

    #: Worker wake-up interval while the gate timer is armed.
    timer_poll_s: float = 0.05
    #: A voice CANCEL is accepted down to ``voice_min_confidence * cancel_confidence_factor``.
    cancel_confidence_factor: float = 0.6
    #: Unrecognised voice utterances longer than this are treated as background speech.
    max_unknown_words: int = 8
    #: The unsolicited-FAULT notice is not spoken this soon after a hardware-error reply.
    fault_notice_grace_s: float = 3.0
    #: close() waits this long for the worker (it may be blocked in a hardware command).
    join_timeout_s: float = 5.0

    def __init__(self, dose: DoseServiceAPI, speaker: Speaker, bus: EventBus,
                 settings: Settings, clock: Clock) -> None:
        self._dose = dose
        self._speaker = speaker
        self._bus = bus
        self.settings = settings
        self._clock = clock
        # queue + worker (guarded by _cond)
        self._cond = threading.Condition()
        self._jobs: deque[_Job] = deque()
        self._seq = itertools.count(1)
        self._thread: threading.Thread | None = None
        self._closed = False
        self._active: _Job | None = None
        self._refresh_pending = False
        self._gate_deadline: float | None = None
        self._gate_gen = 0
        self._device_state: str | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        # worker-only bookkeeping
        self._cancel_requested_for: int | None = None
        self._cancel_announced_for: int | None = None
        self._last_hw_error_at: float | None = None
        # dialogue state (guarded by _state_lock; read by state())
        self._state_lock = threading.Lock()
        self._phase = Phase.IDLE
        self._phase_dose: DoseInfo | None = None
        self._awaiting: DoseInfo | None = None
        self._last_reply: Reply | None = None
        self._repeatable: Reply | None = None

    # ================================================================== lifecycle
    def start(self) -> None:
        """Start the ``assistant`` worker. Idempotent; never raises."""
        with self._cond:
            if self._closed:
                log.warning("assistant was closed; start() ignored")
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="assistant", daemon=True)
            self._thread.start()
            subscribe = not self._unsubscribe
        if subscribe:
            try:
                self._unsubscribe.append(self._bus.add_listener(self._on_device_state, [Topic.DEVICE_STATE]))
                self._unsubscribe.append(self._bus.add_listener(self._on_dose_updated, [Topic.DOSE_UPDATED]))
            except Exception:  # noqa: BLE001
                log.exception("assistant could not subscribe to the event bus")
        self._request_refresh()

    def close(self) -> None:
        """Stop the worker; pending requests resolve to ``None``. Idempotent; never raises."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            pending = list(self._jobs)
            self._jobs.clear()
            self._gate_deadline = None
            self._gate_gen += 1
            self._cond.notify_all()
            thread = self._thread
        for unsubscribe in self._unsubscribe:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribe.clear()
        for job in pending:
            self._resolve(job, None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.join_timeout_s)
            if thread.is_alive():
                log.warning("assistant worker still busy after %.0f s (blocked in a hardware call?)",
                            self.join_timeout_s)

    # ================================================================== public API
    def submit(self, intent: Intent | str, source: IntentSource | str, *, text: str = "",
               wait: bool = False, timeout: float = 60) -> Reply | None:
        """Queue ``intent``. With ``wait=True`` return its :class:`Reply` (``None`` on timeout
        or if the assistant is closed). CANCEL sends STOP synchronously first."""
        intent = Intent(intent)
        source = IntentSource(source)
        if intent is Intent.CANCEL:
            self._interrupt(source)
        return self._dispatch(self._new_job(intent, source, text=text or ""), wait=wait, timeout=timeout)

    def handle_text(self, text: str, source: IntentSource | str, confidence: float = 1.0, *,
                    wait: bool = False) -> Reply | None:
        """Parse free text (typed or recognised) and queue the resulting intent."""
        source = IntentSource(source)
        text = text if isinstance(text, str) else ""
        parsed = parse_intent(text, confidence=confidence)
        decision = self._triage(text, parsed, source, confidence)
        if decision == "ignore":
            log.debug("ignoring %r from %s (confidence %.2f)", text, source.value, confidence)
            return None
        if decision == "submit":
            if parsed.intent is Intent.CANCEL:
                self._interrupt(source)
            job = self._new_job(parsed.intent, source, text=text, confidence=confidence, parsed=parsed)
        else:
            job = self._new_job(Intent.UNKNOWN, source, text=text, confidence=confidence,
                                kind="unknown", parsed=parsed, response=decision)
        return self._dispatch(job, wait=wait)

    def handle_voice_text(self, text: str, confidence: float) -> None:
        """VoiceRecognizer callback (``voice`` thread). Only queues; never raises."""
        try:
            self.handle_text(text, IntentSource.VOICE, confidence, wait=False)
        except Exception:  # noqa: BLE001
            log.exception("handle_voice_text failed for %r", text)

    def on_hardware_event(self, msg: Message) -> None:
        """``HardwareController`` EVENT listener. Runs on the serial reader thread: only queues.

        The cancel button needs no host STOP: the firmware stops locally (protocol §8.8),
        and a blocking STOP from here would stall the thread that must read its reply.
        """
        try:
            line = msg.raw or msg.to_line()
            if msg.is_event(Ev.CONFIRM_BUTTON):
                self._dispatch(self._new_job(Intent.PRIMARY_ACTION, IntentSource.BUTTON, text=line))
            elif msg.is_event(Ev.CANCEL_BUTTON):
                self._dispatch(self._new_job(Intent.CANCEL, IntentSource.BUTTON, text=line))
            elif msg.is_event(Ev.BOOT):
                self._notice("BOOT")
            elif msg.is_err(Err.HOME_TIMEOUT) or msg.is_err(Err.MOTOR_FAULT):
                self._notice("FAULT")  # only if a client forwards unsolicited ERR lines
        except Exception:  # noqa: BLE001 - never break the reader thread
            log.exception("on_hardware_event failed for %r", msg)

    def state(self) -> dict[str, Any]:
        """``{phase, last_reply, awaiting, dose, busy, pending, gate_timer_s, running}``."""
        with self._cond:
            busy = self._active is not None and self._active.kind != "refresh"
            pending = sum(1 for job in self._jobs if job.kind != "refresh")
            deadline = self._gate_deadline
            running = not self._closed and self._thread is not None and self._thread.is_alive()
        remaining = None if deadline is None else max(0.0, round(deadline - self._clock.monotonic(), 1))
        with self._state_lock:
            return {
                "phase": self._phase.value,
                "last_reply": self._last_reply.to_dict() if self._last_reply else None,
                "awaiting": self._awaiting.to_dict() if self._awaiting else None,
                "dose": self._phase_dose.to_dict() if self._phase_dose else None,
                "busy": busy,
                "pending": pending,
                "gate_timer_s": remaining,
                "running": running,
            }

    # ================================================================== intake
    def _new_job(self, intent: Intent, source: IntentSource, **fields: Any) -> _Job:
        return _Job(seq=next(self._seq), intent=intent, source=source, **fields)

    def _interrupt(self, source: IntentSource) -> bool:
        try:
            sent = bool(self._dose.interrupt(source))
        except Exception:  # noqa: BLE001 - a failing STOP must not block the cancel itself
            log.exception("dose.interrupt() failed")
            return False
        if sent:
            log.info("cancel from %s: STOP sent immediately", source.value)
        return sent

    def _triage(self, text: str, parsed: ParsedIntent, source: IntentSource, confidence: float) -> str:
        """``submit`` | ``sorry`` | ``negated`` | ``ignore`` (ARCHITECTURE §6 voice rules)."""
        if source is not IntentSource.VOICE:
            if parsed.intent is Intent.UNKNOWN:
                return "negated" if parsed.negated and parsed.matched else "sorry"
            return "submit"
        threshold = float(self.settings.voice_min_confidence)
        words = content_words(text)
        if parsed.intent is Intent.CANCEL:
            if confidence >= threshold * self.cancel_confidence_factor:
                return "submit"  # stopping is the safe direction
            return "sorry" if words else "ignore"
        if confidence < threshold:
            return "sorry" if words else "ignore"
        if parsed.intent is Intent.UNKNOWN:
            if parsed.negated and parsed.matched:
                return "negated"
            if words and len(normalise(text).split()) <= self.max_unknown_words:
                return "sorry"
            return "ignore"
        if parsed.intent in (Intent.DISPENSE, Intent.CONFIRM_TAKEN) and "[unk]" in text.lower():
            return "sorry"  # partly unintelligible: never actuate on a guess
        return "submit"

    def _dispatch(self, job: _Job, *, wait: bool = False, timeout: float = 60) -> Reply | None:
        if wait and threading.current_thread() is self._thread:
            log.warning("submit(wait=True) from the assistant worker would deadlock; not waiting")
            wait = False
        if self._closed:
            log.warning("assistant closed; %s from %s ignored", job.intent.value, job.source.value)
            return None
        future: Future[Reply | None] | None = Future() if wait else None
        job.future = future
        self._publish_intent(job)
        dropped: list[_Job] = []
        with self._cond:
            if self._closed:
                return None
            if job.intent is Intent.CANCEL:
                active = self._active
                if active is not None and active.kind != "refresh":
                    job.active_seq = active.seq
                    self._cancel_requested_for = active.seq
                dropped = self._drop_motion_jobs_locked()
            self._jobs.append(job)
            self._cond.notify_all()
        for old in dropped:
            log.info("cancel: dropped queued %s from %s", old.intent.value, old.source.value)
            self._resolve(old, Reply(old.intent, old.source, phrases.CANCELLED, ReplyKind.INFO,
                                     outcome={"status": "CANCELLED", "reason": "cancelled before it started"},
                                     spoken=False))
        if future is None:
            return None
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            log.warning("%s from %s not answered within %.1f s", job.intent.value, job.source.value, timeout)
            return None

    def _drop_motion_jobs_locked(self) -> list[_Job]:
        motion = set(_MOTION_INTENTS)
        if self.settings.check_due_auto_dispense:
            motion.add(Intent.CHECK_DUE)
        kept: deque[_Job] = deque()
        dropped: list[_Job] = []
        for queued in self._jobs:
            (dropped if queued.kind == "intent" and queued.intent in motion else kept).append(queued)
        self._jobs = kept
        return dropped

    def _notice(self, code: str) -> None:
        job = self._new_job(Intent.UNKNOWN, IntentSource.SYSTEM, kind="notice", notice=code, published=True)
        with self._cond:
            if self._closed:
                return
            self._jobs.append(job)
            self._cond.notify_all()

    def _request_refresh(self) -> None:
        with self._cond:
            if self._closed or self._refresh_pending:
                return
            self._refresh_pending = True
            self._jobs.append(self._new_job(Intent.UNKNOWN, IntentSource.SYSTEM, kind="refresh", published=True))
            self._cond.notify_all()

    def _on_device_state(self, ev: BusEvent) -> None:
        """Bus listener (publisher's thread): announce unsolicited transitions into FAULT."""
        state = str((ev.data or {}).get("state") or "")
        with self._cond:
            previous, self._device_state = self._device_state, state
        if state == "FAULT" and previous != "FAULT":
            self._notice("FAULT")

    def _on_dose_updated(self, ev: BusEvent) -> None:
        """Bus listener: dose state changed somewhere (caregiver, scheduler...) -> refresh."""
        self._request_refresh()

    def _publish_intent(self, job: _Job) -> None:
        job.published = True
        payload: dict[str, Any] = {"intent": job.intent.value, "source": job.source.value,
                                   "text": job.text, "confidence": job.confidence}
        if job.kind == "unknown":
            payload["response"] = job.response
        self._bus.publish(Topic.INTENT, payload)

    # ================================================================== worker
    def _run(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                return
            try:
                self._process(job)
            except Exception:  # noqa: BLE001 - the worker must never die
                log.exception("assistant failed while handling %s", job.intent.value)
                self._resolve(job, None)
            finally:
                with self._cond:
                    self._active = None

    def _next_job(self) -> _Job | None:
        with self._cond:
            while True:
                if self._closed:
                    return None
                self._check_gate_timer_locked()
                if self._jobs:
                    job = self._jobs.popleft()
                    if job.kind == "refresh":
                        self._refresh_pending = False
                    self._active = job
                    return job
                self._cond.wait(self.timer_poll_s if self._gate_deadline is not None else None)

    def _check_gate_timer_locked(self) -> None:
        if self._gate_deadline is None or self._clock.monotonic() < self._gate_deadline:
            return
        self._gate_deadline = None
        self._jobs.append(self._new_job(Intent.GATE_TIMEOUT, IntentSource.SYSTEM, gate_gen=self._gate_gen))

    def _arm_gate_timer(self) -> None:
        with self._cond:
            self._gate_gen += 1
            self._gate_deadline = self._clock.monotonic() + float(self.settings.gate_open_timeout_s)
            self._cond.notify_all()

    def _disarm_gate_timer(self) -> None:
        with self._cond:
            self._gate_gen += 1
            self._gate_deadline = None

    def _process(self, job: _Job) -> None:
        if job.kind == "refresh":
            self._do_refresh()
            return
        if not job.published:
            self._publish_intent(job)
        try:
            reply = self._handle(job)
        except Exception as exc:  # noqa: BLE001 - fail closed and keep the worker alive
            log.exception("handler for %s failed", job.intent.value)
            reply = Reply(job.intent, job.source, phrases.ERROR_GENERIC, ReplyKind.ERROR,
                          outcome={"error": type(exc).__name__})
            if job.intent in _MOTION_INTENTS or job.intent is Intent.CHECK_DUE:
                self._arm_gate_timer()  # gate state unknown: close it later if it is open
        if reply.spoken and reply.text:
            reply.spoken = self._say(reply.text, reply.kind, interrupt=self._interrupts(reply),
                                     meta={"intent": reply.intent.value, "source": reply.source.value})
        self._after(job, reply)
        self._resolve(job, reply)

    def _handle(self, job: _Job) -> Reply:
        if job.kind == "notice":
            return self._handle_notice(job)
        if job.kind == "unknown":
            return self._handle_unknown(job)
        handlers: dict[Intent, Callable[[_Job], Reply]] = {
            Intent.CHECK_DUE: self._handle_check_due,
            Intent.DISPENSE: self._handle_dispense,
            Intent.CONFIRM_TAKEN: self._handle_confirm,
            Intent.REPEAT: self._handle_repeat,
            Intent.CANCEL: self._handle_cancel,
            Intent.HELP: self._handle_help,
            Intent.PRIMARY_ACTION: self._handle_primary,
            Intent.GATE_TIMEOUT: self._handle_gate_timeout,
            Intent.UNKNOWN: self._handle_unknown,
        }
        return handlers[job.intent](job)

    # ================================================================== handlers
    @property
    def _names(self) -> bool:
        return bool(self.settings.tts_include_med_names)

    def _handle_check_due(self, job: _Job, summary: DueSummary | None = None) -> Reply:
        summary = summary if summary is not None else self._dose.check_due()
        outcome = summary.to_dict()
        if summary.awaiting_confirmation:
            dose = summary.awaiting_confirmation[0]
            text = phrases.awaiting_confirmation(dose, include_names=self._names, more_due=len(summary.due))
            return self._reply(job, Intent.CHECK_DUE, text, ReplyKind.PROMPT, outcome)
        if summary.due:
            if self.settings.check_due_auto_dispense:
                reply = self._handle_dispense(job)
                reply.outcome["requested"] = Intent.CHECK_DUE.value
                return reply
            text = phrases.due_now(summary.due[0], count=len(summary.due), include_names=self._names)
            return self._reply(job, Intent.CHECK_DUE, text, ReplyKind.PROMPT, outcome)
        text, kind = self._explain_nothing_due(summary)
        return self._reply(job, Intent.CHECK_DUE, text, kind, outcome)

    def _explain_nothing_due(self, summary: DueSummary) -> tuple[str, ReplyKind]:
        """Mirror the dose service's refusal priority (ARCHITECTURE §5)."""
        names, now_local, nxt = self._names, summary.now_local, summary.next_upcoming
        reasons = [reason for _, reason in summary.blocked]
        if BlockReason.IN_PROGRESS in reasons:
            return phrases.IN_PROGRESS, ReplyKind.INFO
        if BlockReason.NEEDS_REVIEW in reasons:
            return phrases.NEEDS_REVIEW, ReplyKind.ERROR
        if summary.accessed:
            latest = max(summary.accessed, key=lambda d: d.scheduled_at)
            if latest.status == _TAKEN:
                return phrases.already_taken(latest, include_names=names, next_up=nxt, now_local=now_local), ReplyKind.INFO
            return phrases.already_accessed(nxt, include_names=names, now_local=now_local), ReplyKind.INFO
        if BlockReason.TOO_SOON in reasons:
            return phrases.TOO_SOON, ReplyKind.WARNING
        if reasons:
            return phrases.blocked(reasons[0]), ReplyKind.WARNING
        return phrases.nothing_due(nxt, include_names=names, now_local=now_local), ReplyKind.INFO

    def _handle_dispense(self, job: _Job) -> Reply:
        def on_motion_start(dose: DoseInfo) -> None:
            try:
                self._set_phase(Phase.PREPARING, dose, phrases.PREPARING)
                self._say(phrases.PREPARING, ReplyKind.INFO, interrupt=True,
                          meta={"intent": Intent.DISPENSE.value, "source": job.source.value})
            except Exception:  # noqa: BLE001 - must not disturb the dispense itself
                log.exception("on_motion_start failed")

        outcome = self._dose.dispense_next(job.source, on_motion_start=on_motion_start)
        status = outcome.status
        cancel_requested = self._cancel_requested_for == job.seq
        names = self._names
        spoken = True
        if status is DispenseStatus.DISPENSED:
            dose = outcome.dose
            text = phrases.dose_ready(dose, include_names=names) if dose else \
                f"{phrases.MEDICATION_READY} {phrases.SAY_TAKEN}"
            kind = ReplyKind.SUCCESS
            job.dispensed = dose
            self._arm_gate_timer()
            if cancel_requested:
                spoken = False  # the queued CANCEL closes the gate and explains
        elif status is DispenseStatus.CANCELLED or (
            cancel_requested and status is DispenseStatus.HARDWARE_UNAVAILABLE
        ):
            text, kind = phrases.DISPENSE_CANCELLED, ReplyKind.INFO
            self._cancel_announced_for = job.seq
        elif status is DispenseStatus.DUPLICATE:
            if outcome.reason == BlockReason.TOO_SOON.value:
                text = phrases.TOO_SOON
            else:
                text = phrases.already_accessed(outcome.next_upcoming, include_names=names,
                                                now_local=self._clock.local_now())
            kind = ReplyKind.WARNING
        elif status is DispenseStatus.NOTHING_DUE:
            text = phrases.nothing_due(outcome.next_upcoming, include_names=names,
                                       now_local=self._clock.local_now())
            kind = ReplyKind.WARNING
        elif status is DispenseStatus.IN_PROGRESS:
            text, kind = phrases.IN_PROGRESS, ReplyKind.WARNING
        elif status is DispenseStatus.BLOCKED:
            text = phrases.blocked(outcome.reason)
            kind = ReplyKind.ERROR if outcome.reason == BlockReason.NEEDS_REVIEW.value else ReplyKind.WARNING
        elif status is DispenseStatus.HARDWARE_UNAVAILABLE:
            text, kind = phrases.HARDWARE_UNAVAILABLE, ReplyKind.ERROR
            self._last_hw_error_at = self._clock.monotonic()
        elif status is DispenseStatus.HARDWARE_ERROR:
            text, kind = phrases.COULD_NOT_PREPARE, ReplyKind.ERROR
            self._last_hw_error_at = self._clock.monotonic()
            if outcome.uncertain:
                self._arm_gate_timer()  # the gate may be open: close it later if so
        elif status is DispenseStatus.DB_ERROR:
            text, kind = phrases.DB_UNAVAILABLE, ReplyKind.ERROR
        else:
            text, kind = phrases.ASK_FOR_ASSISTANCE, ReplyKind.ERROR
        return self._reply(job, Intent.DISPENSE, text, kind, outcome.to_dict(), spoken=spoken)

    def _handle_confirm(self, job: _Job) -> Reply:
        out = self._dose.confirm_taken(job.source)
        status = out.status
        if status is ConfirmStatus.CONFIRMED:
            text = phrases.confirmed(out.dose, include_names=self._names, gate_closed=out.gate_closed,
                                     more_due=self._count_due())
            if out.gate_closed is False:
                kind = ReplyKind.ERROR  # recorded, but the gate may still be open: keep the timer
            else:
                kind = ReplyKind.SUCCESS
                self._disarm_gate_timer()
        elif status is ConfirmStatus.ALREADY_CONFIRMED:
            text, kind = phrases.already_confirmed(out.dose, include_names=self._names), ReplyKind.INFO
        elif status is ConfirmStatus.NOTHING_TO_CONFIRM:
            text, kind = phrases.NOTHING_TO_CONFIRM, ReplyKind.WARNING
        elif status is ConfirmStatus.DB_ERROR:
            text, kind = phrases.CONFIRM_DB_ERROR, ReplyKind.ERROR
        else:
            text, kind = phrases.ASK_FOR_ASSISTANCE, ReplyKind.ERROR
        return self._reply(job, Intent.CONFIRM_TAKEN, text, kind, out.to_dict())

    def _handle_cancel(self, job: _Job) -> Reply:
        out = self._dose.cancel(job.source)
        status = out.status
        outcome = out.to_dict()
        if status is CancelStatus.FAILED:
            self._last_hw_error_at = self._clock.monotonic()
            return self._reply(job, Intent.CANCEL, phrases.CANCEL_FAILED, ReplyKind.ERROR, outcome)
        self._disarm_gate_timer()
        already_said = job.active_seq is not None and job.active_seq == self._cancel_announced_for
        if already_said and status in (CancelStatus.NOTHING_TO_CANCEL, CancelStatus.STOPPED_MOTION):
            # The interrupted dispense already said "Cancelled. Nothing was dispensed..."
            return self._reply(job, Intent.CANCEL, phrases.DISPENSE_CANCELLED, ReplyKind.INFO, outcome,
                               spoken=False)
        awaiting = self._fetch_awaiting()
        if status is CancelStatus.STOPPED_MOTION:
            text = phrases.CANCEL_STOPPED
        elif status is CancelStatus.CLOSED_GATE:
            text = phrases.CANCEL_CLOSED_GATE_REMINDER if awaiting else phrases.CANCEL_CLOSED_GATE
        else:
            text = phrases.CANCELLED_REMINDER if awaiting else phrases.CANCELLED
        return self._reply(job, Intent.CANCEL, text, ReplyKind.INFO, outcome)

    def _handle_primary(self, job: _Job) -> Reply:
        summary: DueSummary | None = None
        if self._dose.awaiting_confirmation() is not None:
            mapped = Intent.CONFIRM_TAKEN
        else:
            summary = self._dose.check_due()
            mapped = Intent.DISPENSE if summary.due else Intent.CHECK_DUE
        self._bus.publish(Topic.INTENT, {"intent": mapped.value, "source": job.source.value,
                                         "text": job.text, "via": Intent.PRIMARY_ACTION.value})
        if mapped is Intent.CONFIRM_TAKEN:
            reply = self._handle_confirm(job)
        elif mapped is Intent.DISPENSE:
            reply = self._handle_dispense(job)
        else:
            reply = self._handle_check_due(job, summary)
        reply.outcome["requested"] = Intent.PRIMARY_ACTION.value
        return reply

    def _handle_repeat(self, job: _Job) -> Reply:
        with self._state_lock:
            last = self._repeatable
        if last is None:
            return self._reply(job, Intent.REPEAT, phrases.NOTHING_TO_REPEAT, ReplyKind.INFO)
        return self._reply(job, Intent.REPEAT, last.text, last.kind, {"repeat_of": last.intent.value})

    def _handle_help(self, job: _Job) -> Reply:
        return self._reply(job, Intent.HELP, phrases.HELP, ReplyKind.INFO)

    def _handle_gate_timeout(self, job: _Job) -> Reply:
        if job.gate_gen is not None and job.gate_gen != self._gate_gen:
            return self._reply(job, Intent.GATE_TIMEOUT, "", ReplyKind.INFO, {"stale": True}, spoken=False)
        result = self._dose.close_gate("timeout")
        awaiting = self._fetch_awaiting()
        outcome = {"sent": result is not None, "ok": None if result is None else result.ok,
                   "hardware": None if result is None else result.hardware_result}
        if result is None:
            text = phrases.TAKEN_REMINDER if awaiting else ""
            return self._reply(job, Intent.GATE_TIMEOUT, text, ReplyKind.PROMPT, outcome)
        if result.ok:
            if awaiting:
                return self._reply(job, Intent.GATE_TIMEOUT, phrases.GATE_CLOSED_TIMEOUT, ReplyKind.PROMPT, outcome)
            return self._reply(job, Intent.GATE_TIMEOUT, phrases.GATE_CLOSED, ReplyKind.INFO, outcome)
        self._last_hw_error_at = self._clock.monotonic()
        return self._reply(job, Intent.GATE_TIMEOUT, phrases.GATE_CLOSE_FAILED, ReplyKind.ERROR, outcome)

    def _handle_unknown(self, job: _Job) -> Reply:
        parsed = job.parsed or parse_intent(job.text, confidence=job.confidence)
        outcome = {"heard": job.text, "confidence": job.confidence, "negated": parsed.negated,
                   "matched": parsed.matched}
        negated = job.response == "negated" or (not job.response and parsed.negated and bool(parsed.matched))
        if negated:
            text = phrases.NEGATED_REMINDER if self._fetch_awaiting() else phrases.NEGATED
        else:
            text = phrases.NOT_UNDERSTOOD
        return self._reply(job, Intent.UNKNOWN, text, ReplyKind.INFO, outcome)

    def _handle_notice(self, job: _Job) -> Reply:
        if job.notice == "BOOT":
            text, kind, level = phrases.DEVICE_RESTARTED, ReplyKind.WARNING, "warning"
        else:
            last = self._last_hw_error_at
            if last is not None and self._clock.monotonic() - last < self.fault_notice_grace_s:
                return self._reply(job, Intent.UNKNOWN, phrases.DEVICE_NEEDS_ATTENTION, ReplyKind.ERROR,
                                   {"notice": job.notice, "suppressed": True}, spoken=False)
            text, kind, level = phrases.DEVICE_NEEDS_ATTENTION, ReplyKind.ERROR, "error"
        self._bus.publish(Topic.NOTICE, {"level": level, "message": text, "code": f"DEVICE_{job.notice}"})
        return self._reply(job, Intent.UNKNOWN, text, kind, {"notice": job.notice})

    # ================================================================== state + speech
    def _after(self, job: _Job, reply: Reply) -> None:
        if reply.outcome.get("stale"):
            return
        with self._state_lock:
            if reply.spoken and reply.text:
                self._last_reply = reply
                if job.kind == "notice" or reply.intent not in (Intent.REPEAT, Intent.UNKNOWN):
                    self._repeatable = reply
            phase, awaiting = self._phase, self._awaiting
        passive = job.kind == "unknown" or job.intent in (Intent.REPEAT, Intent.HELP)
        if not passive:
            awaiting = self._fetch_awaiting()
            if awaiting is None and job.dispensed is not None:
                awaiting = job.dispensed
            if reply.kind is ReplyKind.ERROR or self._device_state == "FAULT":
                phase = Phase.ATTENTION
            elif awaiting is not None:
                phase = Phase.AWAITING_CONFIRMATION
            else:
                phase = Phase.IDLE
        elif phase is Phase.PREPARING:
            phase = Phase.AWAITING_CONFIRMATION if awaiting is not None else Phase.IDLE
        self._set_phase(phase, awaiting, reply.text if reply.spoken else None,
                        awaiting=awaiting, reply=reply)

    def _do_refresh(self) -> None:
        awaiting = self._fetch_awaiting()
        with self._state_lock:
            phase, previous = self._phase, self._awaiting
        new_phase = phase if phase is Phase.ATTENTION else (
            Phase.AWAITING_CONFIRMATION if awaiting is not None else Phase.IDLE)
        if new_phase is not phase or _dose_key(awaiting) != _dose_key(previous):
            self._set_phase(new_phase, awaiting, None, awaiting=awaiting)

    def _fetch_awaiting(self) -> DoseInfo | None:
        try:
            return self._dose.awaiting_confirmation()
        except Exception:  # noqa: BLE001
            log.exception("awaiting_confirmation() failed")
            with self._state_lock:
                return self._awaiting

    def _count_due(self) -> int:
        try:
            return len(self._dose.check_due().due)
        except Exception:  # noqa: BLE001
            log.exception("check_due() failed after a confirmation")
            return 0

    def _set_phase(self, phase: Phase, dose: DoseInfo | None, message: str | None, *,
                   awaiting: Any = _KEEP, reply: Reply | None = None) -> None:
        with self._state_lock:
            self._phase = phase
            self._phase_dose = dose
            if awaiting is not _KEEP:
                self._awaiting = awaiting
        payload: dict[str, Any] = {"phase": phase.value, "dose": dose.to_dict() if dose else None,
                                   "message": message}
        if reply is not None:
            payload["intent"] = reply.intent.value
            payload["kind"] = reply.kind.value
        self._bus.publish(Topic.ASSISTANT_STATE, payload)

    def _say(self, text: str, kind: ReplyKind, *, interrupt: bool = False,
             meta: dict[str, Any] | None = None) -> bool:
        try:
            self._speaker.say(text, kind=kind.value, interrupt=interrupt, meta=meta)
            return True
        except Exception:  # noqa: BLE001
            log.exception("speaker.say failed")
            return False

    @staticmethod
    def _interrupts(reply: Reply) -> bool:
        """Errors, cancels and repeats cut off whatever is being said."""
        return (reply.kind is ReplyKind.ERROR or reply.intent in (Intent.CANCEL, Intent.REPEAT)
                or reply.text == phrases.DISPENSE_CANCELLED)

    @staticmethod
    def _reply(job: _Job, intent: Intent, text: str, kind: ReplyKind,
               outcome: dict[str, Any] | None = None, *, spoken: bool = True) -> Reply:
        return Reply(intent=intent, source=job.source, text=text, kind=kind,
                     outcome=dict(outcome or {}), spoken=spoken and bool(text))

    @staticmethod
    def _resolve(job: _Job, reply: Reply | None) -> None:
        if job.future is not None and not job.future.done():
            try:
                job.future.set_result(reply)
            except Exception:  # noqa: BLE001 - InvalidStateError if raced
                pass
