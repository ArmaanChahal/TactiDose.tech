"""Guided, voice-first judge demo: MORNING -> NOON -> NIGHT (candy, not medicine; demo mode only).

A deterministic state machine (plain code - no model decides the next step). One run per patient,
on its own ``guided-demo`` thread, cancellable at every wait. For each slot (container 1, 2, 3):

1. "It's time for your <slot> pill. Do you want to take it?" -> yes / no / unclear (asked once
   more, then treated as no). Answers are read by ``rules_agent.analyse`` (:func:`classify_yes_no`).
2. No -> the slot's scheduled dose is skipped (``DropService.skip_dose``, note "Declined ...") and
   the slot is DECLINED.
3. Yes -> "I'm turning on the buzzer ..." + ``Buzzer.on()``, then exactly ONE
   ``DropService.request_drop(source="schedule", dose_event_id=<this slot's dose>)``. DropService
   applies every rule as for any scheduled dose (dose window, double-dose guard, inventory, review,
   hardware); a refusal is said honestly with the deterministic sentence (``describe_outcome``).
4. If the pill DROPPED: "Did you take the pill?" -> yes goes through the agent tool path
   ``confirm_pill_taken`` (DISPENSED -> TAKEN).
5. "How has your day been? ..." -> free text, read by :mod:`tactidose.guided.checkin` (rules,
   optionally Gemini, validated). Emergency or severe wording (rules only) -> the fixed emergency
   reply, a HEALTH_CONCERN notification to the patient and their care team, and the run ends.
6. Pause ``demo_pause_seconds``, next slot. After NIGHT: goodbye + a summary of the three slots.

Time: before each slot the demo clock (``Clock.travel_to``, as the demo panel's clock travel)
moves *forward* to ``min(15, dose_early_minutes)`` minutes before that slot's scheduled time, so
the dose window is open but the scheduler's automatic drop (from ``scheduled_at``) has not
started: the patient is asked first. No cooldown is bypassed - scheduled doses are never subject
to the manual cooldown - and no new drop path exists. ``reset=True`` first moves the clock to the
morning slot and re-seeds the demo data there (``reset_demo``), so no earlier dose turns MISSED.

Storage: every prompt and answer in a ``conversations`` row titled "Guided demo"; each slot in
``guided_demo_slots`` (answers, outcome, ``drop_id``, ``dose_event_id``, check-in extraction).
Buzzer: only through the :class:`~tactidose.hardware.buzzer.Buzzer` interface (backend from
``TACTIDOSE_BUZZER_BACKEND``; default the laptop tone). Event field ``buzzer`` = the laptop tone is
on (the screens beep), ``buzzer_hw`` = the device's buzzer is on.
Progress: ``Topic.DEMO_GUIDED`` events (kiosk and demo panel). Speech: the prompt text plus an
``audio_url`` rendered by ``AgentService.speak`` (ElevenLabs -> cache -> offline voice); the
browser falls back to ``speechSynthesis`` and captions.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable

from sqlalchemy import select

from tactidose.agent.rules_agent import analyse, describe_outcome
from tactidose.agent.tools import CONFIRM_PILL_TAKEN, PatientTools
from tactidose.core import phrases
from tactidose.core.bus import Topic
from tactidose.db.models import (
    DISPENSABLE_STATUSES,
    Compartment,
    Conversation,
    ConversationMessage,
    DoseEvent,
    GuidedDemoSlot,
    NotificationKind,
    Schedule,
)
from tactidose.guided.checkin import CheckinExtraction, GeminiCheckinExtractor, extract
from tactidose.hardware import buzzer_config
from tactidose.hardware.buzzer import Buzzer, create_buzzer

log = logging.getLogger(__name__)

__all__ = ["SLOTS", "GuidedDemoError", "GuidedDemoRunner", "classify_yes_no"]

#: (slot name, container index)
SLOTS: tuple[tuple[str, int], ...] = (("morning", 0), ("noon", 1), ("night", 2))
MODEL = "guided-demo"
_DISPENSABLE = frozenset(s.value for s in DISPENSABLE_STATUSES)
_YES_START = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok|okay|i did|i have|i do|i want|i will|please|absolutely|"
                        r"of course|definitely|go ahead|i took)\b(?!\s*(?:not|n't|nt)\b)(?!n't|nt\b)")
_NO_START = re.compile(r"^(?:no|nope|nah|not|i don't|i dont|i do not|i didn't|i didnt|i did not|i haven't|"
                       r"i havent|i have not|don't|dont|never|skip)\b")
TRANSCRIPT_KEEP = 60


class GuidedDemoError(Exception):
    """Cannot start / answer (``status_code`` for the API)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class _Stopped(Exception):
    pass


class _Alerted(Exception):
    pass


def classify_yes_no(text: str | None) -> str:
    """``yes`` | ``no`` | ``unclear`` | ``emergency`` | ``stop`` with the agent's rules parser."""
    if not text or not text.strip():
        return "unclear"
    flags = analyse(text)
    if flags.emergency:
        return "emergency"
    if flags.stop:
        return "stop"
    if flags.unclear:
        return "unclear"
    norm = flags.norm
    no = bool(flags.negative or flags.drop_negated or flags.confirm_negated or _NO_START.match(norm))
    yes = bool(flags.affirmative or flags.confirm or flags.drop_request or _YES_START.match(norm))
    if yes and not no:
        return "yes"
    if no and not yes:
        return "no"
    return "unclear"


@dataclass
class SlotResult:
    index: int
    name: str
    medication_name: str | None = None
    dose_event_id: int | None = None
    take_answer: str = "unclear"
    outcome: str = "STOPPED"
    reason: str | None = None
    drop_id: int | None = None
    taken: bool | None = None
    checkin_text: str | None = None
    extraction: dict[str, Any] | None = None
    message: str | None = None


@dataclass
class _Run:
    run_id: str
    patient_id: int
    started_by: int | None
    reset: bool
    state: str = "starting"           # starting | running | finished | stopped | alerted | error
    slot: dict[str, Any] | None = None
    step: str | None = None
    awaiting: str | None = None       # yes_no | free_text
    answer: str | None = None
    answer_mode: str = "text"
    #: The current question has been said (published); headless callers answer after that.
    prompted: bool = False
    conversation_id: int | None = None
    results: list[SlotResult] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)
    thread: threading.Thread | None = None
    started_at: str | None = None


class GuidedDemoRunner:
    """Runs guided demos for the patient(s) of the configured device. Thread-safe."""

    def __init__(self, services: Any, *, extractor: GeminiCheckinExtractor | None = None,
                 sleep: Callable[[threading.Event, float], bool] | None = None,
                 buzzer: Buzzer | None = None) -> None:
        self.services = services
        self.settings = services.settings
        self.clock = services.clock
        self.bus = services.bus
        self.db = services.db
        self.drops = services.drops
        self.extractor = extractor if extractor is not None else GeminiCheckinExtractor(self.settings)
        self._sleep = sleep or (lambda ev, s: ev.wait(s))
        self.buzzer = buzzer if buzzer is not None else create_buzzer(self.settings, getattr(services, "hardware", None))
        self._lock = threading.Lock()
        self._runs: dict[int, _Run] = {}

    # ================================================================== public API
    def start(self, patient_id: int, *, started_by: int | None = None, reset: bool = False) -> dict[str, Any]:
        if not self.settings.demo_mode:
            raise GuidedDemoError("The guided demo needs demo mode.", 403)
        pid = int(patient_id)
        with self._lock:
            current = self._runs.get(pid)
            if current is not None and current.thread is not None and current.thread.is_alive():
                raise GuidedDemoError("A guided demo is already running. Stop it first.")
            run = _Run(run_id=f"gd-{uuid.uuid4().hex[:12]}", patient_id=pid, started_by=started_by, reset=reset,
                       started_at=self.clock.now().isoformat())
            self._runs[pid] = run
        run.thread = threading.Thread(target=self._main, args=(run,), name="guided-demo", daemon=True)
        run.thread.start()
        log.info("guided demo %s started for patient %s (reset=%s)", run.run_id, pid, reset)
        return self.state(pid) or {}

    def answer(self, patient_id: int, text: str, *, input_mode: str = "text") -> bool:
        """Deliver the patient's answer to the waiting question. False when nothing is waiting."""
        run = self._runs.get(int(patient_id))
        if run is None:
            return False
        with run.cond:
            if run.awaiting is None or run.answer is not None:
                return False   # one answer per question
            run.answer = " ".join(str(text or "").split())[:1000]
            run.answer_mode = "voice" if input_mode == "voice" else "text"
            run.cond.notify_all()
        return True

    def stop(self, patient_id: int) -> bool:
        """Stop the run and the dispenser (stopping is the safe direction)."""
        run = self._runs.get(int(patient_id))
        running = run is not None and run.thread is not None and run.thread.is_alive()
        if run is not None:
            run.stop.set()
            with run.cond:
                run.cond.notify_all()
        try:
            self.drops.interrupt()
        except Exception:  # noqa: BLE001 - the run still ends
            log.exception("guided demo: drops.interrupt() failed")
        return running

    def state(self, patient_id: int) -> dict[str, Any] | None:
        run = self._runs.get(int(patient_id))
        return None if run is None else self._view(run)

    def running(self, patient_id: int) -> bool:
        run = self._runs.get(int(patient_id))
        return run is not None and run.thread is not None and run.thread.is_alive()

    def wait_awaiting(self, patient_id: int, timeout: float = 30.0) -> str | None:
        """Block until a question waits for an answer (tests / headless CLI); its kind or None."""
        run = self._runs.get(int(patient_id))
        if run is None:
            return None
        with run.cond:
            run.cond.wait_for(lambda: (run.awaiting is not None and run.prompted and run.answer is None)
                              or not self._alive(run), timeout)
            return run.awaiting if run.answer is None else None

    def wait_done(self, patient_id: int, timeout: float = 60.0) -> bool:
        run = self._runs.get(int(patient_id))
        if run is None or run.thread is None:
            return True
        run.thread.join(timeout)
        return not run.thread.is_alive()

    def close(self) -> None:
        self.buzzer.close()
        for pid in list(self._runs):
            run = self._runs[pid]
            if run.thread is not None and run.thread.is_alive():
                run.stop.set()
                with run.cond:
                    run.cond.notify_all()
                run.thread.join(5.0)

    def status(self) -> dict[str, Any]:
        return {"running": [pid for pid in self._runs if self.running(pid)], "checkin_ai": self.extractor.status()}

    # ================================================================== the run
    def _main(self, run: _Run) -> None:
        bridge = getattr(self.services, "wellbeing", None)
        suppress = getattr(bridge, "suppress_offers", None)
        if callable(suppress):
            suppress(run.patient_id, True)   # this demo has its own check-in
        unlisten = self.buzzer.add_listener(lambda _b: self._publish_buzzer(run))
        try:
            run.state = "running"
            run.conversation_id = self._open_conversation(run)
            if run.reset:
                self._fresh_start(run)
            self._say(run, phrases.DEMO_INTRO, step="intro")
            for i, (name, slot) in enumerate(SLOTS):
                self._check_stop(run)
                self._slot(run, name, slot)
                if i < len(SLOTS) - 1:
                    self._say(run, phrases.DEMO_NEXT, step="pause")
                    self._pause(run, float(self.settings.demo_pause_seconds))
            self._finish(run)
        except _Stopped:
            run.state = "stopped"
            self.buzzer.off()
            self._say(run, phrases.DEMO_STOPPED, step="stopped", check=False)
        except _Alerted:
            run.state = "alerted"
            self.buzzer.off()
        except Exception:  # noqa: BLE001 - never leave a dangling run
            log.exception("guided demo %s failed", run.run_id)
            run.state = "error"
            self.buzzer.off()
            self._say(run, phrases.AGENT_ERROR, step="error", check=False)
        finally:
            with run.cond:
                run.awaiting = None
                run.cond.notify_all()
            if callable(suppress):
                suppress(run.patient_id, False)
            self.buzzer.off()
            unlisten()
            self._publish(run, step=run.step, final=True)
            log.info("guided demo %s ended: %s %s", run.run_id, run.state,
                     [(r.name, r.outcome, r.taken) for r in run.results])

    def _slot(self, run: _Run, name: str, container: int) -> None:
        dose = self._prepare_slot(run, name, container)
        result = SlotResult(index=container, name=name, medication_name=dose.get("medication_name"),
                            dose_event_id=dose.get("event_id"))
        run.results.append(result)
        run.slot = {"index": container, "name": name, "container_number": container + 1,
                    "medication_name": result.medication_name}
        try:
            # 1-2. do you want to take it?
            answer = self._ask_yes_no(run, phrases.DEMO_ASK_TAKE[name], step="ask_take")
            result.take_answer = answer
            if answer != "yes":
                result.outcome = "DECLINED"
                self._decline(run, result)
                self._say(run, phrases.DEMO_DECLINED, step="declined", outcome=self._result_view(result))
            else:
                self._dispense(run, result)
                # 4. did you take it?
                if result.outcome == "DROPPED":
                    taken = self._ask_yes_no(run, phrases.DEMO_ASK_TAKEN, step="ask_taken")
                    self._record_taken(run, result, taken == "yes")
            # 5. check-in
            self._checkin(run, result)
        finally:
            self._save_slot(run, result)

    def _dispense(self, run: _Run, result: SlotResult) -> None:
        self._say(run, phrases.DEMO_BUZZER, step="buzzer")
        # Sounds through the drop and demo_buzzer_seconds after it; stops by itself at MAX_ON_MS.
        self.buzzer.on(buzzer_config.MAX_ON_MS)
        if result.dose_event_id is None:
            result.outcome, result.message = "NO_DOSE", phrases.DEMO_NO_DOSE
            self.buzzer.off()
            self._say(run, phrases.DEMO_NO_DOSE, step="dispensed", outcome=self._result_view(result))
            return
        # The one and only drop request of this slot (DropService decides). Inside the scheduler
        # loop's exclusive section: a cycle woken by the clock jump would otherwise hold the drop
        # lock for a moment and turn this request into IN_PROGRESS.
        loop = getattr(self.services, "scheduler_loop", None)
        with (loop.exclusive() if loop is not None else _null()):
            outcome = self.drops.request_drop(patient_id=run.patient_id, source="schedule",
                                              dose_event_id=result.dose_event_id)
        view = outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
        result.drop_id = view.get("drop_id")
        result.outcome = str(view.get("status") or "FAILED")
        result.reason = view.get("reason")
        result.message = describe_outcome(view, clock=self.clock)
        self._publish(run, step="dispensed", outcome=self._result_view(result))
        if result.outcome == "DROPPED":
            self._pause(run, float(self.settings.demo_buzzer_seconds))
        self.buzzer.off()
        self._say(run, result.message, step="drop_result", outcome=self._result_view(result))

    def _record_taken(self, run: _Run, result: SlotResult, yes: bool) -> None:
        if not yes:
            result.taken = False
            self._say(run, phrases.DEMO_TAKEN_NO, step="taken")
            return
        tools = PatientTools(db=self.db, drops=self.drops, clock=self.clock, settings=self.settings,
                             patient_id=run.patient_id, conversation_id=run.conversation_id, bus=self.bus)
        res = tools.execute(CONFIRM_PILL_TAKEN, {"medication_name": result.medication_name or ""})
        result.taken = res.get("status") in ("CONFIRMED", "ALREADY_CONFIRMED")
        self._say(run, phrases.DEMO_TAKEN_YES if result.taken else str(res.get("message") or phrases.RECORD_FAILED),
                  step="taken")

    def _checkin(self, run: _Run, result: SlotResult) -> None:
        text = self._ask(run, phrases.DEMO_ASK_CHECKIN, step="ask_checkin", awaiting="free_text")
        if text is not None and analyse(text).stop:
            self._stop_now(run)
        if not text:
            self._say(run, phrases.DEMO_CHECKIN_NONE, step="checkin")
            return
        result.checkin_text = text
        ext = extract(text, self.extractor if self.extractor.enabled else None)
        result.extraction = ext.to_dict()
        if ext.alert:
            self._alert(run, result, text, ext)
        self._say(run, phrases.DEMO_CHECKIN_THANKS, step="checkin", outcome=self._result_view(result))

    def _finish(self, run: _Run) -> None:
        run.state = "finished"
        summary = " ".join(_slot_sentence(r) for r in run.results)
        self._say(run, f"{phrases.DEMO_GOODBYE} {summary}", step="summary", check=False)

    # ================================================================== questions
    def _ask_yes_no(self, run: _Run, text: str, *, step: str) -> str:
        for attempt in range(2):
            raw = self._ask(run, text if attempt == 0 else phrases.DEMO_REASK_YES_NO, step=step, awaiting="yes_no")
            kind = classify_yes_no(raw)
            if kind == "emergency":
                self._alert(run, run.results[-1] if run.results else None, raw or "", None)
            if kind == "stop":
                self._stop_now(run)
            if kind in ("yes", "no"):
                return kind
        return "unclear"   # asked twice: treated as no

    def _ask(self, run: _Run, text: str, *, step: str, awaiting: str) -> str | None:
        """Say ``text`` and wait for one answer (None on timeout)."""
        self._check_stop(run)
        with run.cond:
            run.answer = None
            run.awaiting = awaiting
            run.prompted = False
        self._say(run, text, step=step, awaiting=awaiting)
        with run.cond:
            run.prompted = True
            run.cond.notify_all()
        timeout = float(self.settings.demo_answer_timeout_s)
        with run.cond:
            run.cond.wait_for(lambda: run.answer is not None or run.stop.is_set(), timeout)
            answer, mode = run.answer, run.answer_mode
            run.awaiting = None
            run.answer = None
            run.cond.notify_all()
        self._check_stop(run)
        if answer:
            self._store(run, "user", answer, input_mode=mode)
            run.transcript.append({"who": "patient", "text": answer})
            self._publish(run, step=f"{step}_heard", heard=answer)
        return answer

    # ================================================================== effects
    def _alert(self, run: _Run, result: SlotResult | None, text: str, ext: CheckinExtraction | None) -> None:
        """Emergency / severe wording: the fixed emergency reply + HEALTH_CONCERN notification; end."""
        if result is not None and ext is None:
            result.checkin_text = result.checkin_text or text
        quoted = " ".join(text.split())[:200]
        try:
            notifications = getattr(self.services, "notifications", None)
            if notifications is not None:
                notifications.notify(
                    patient_id=run.patient_id, kind=NotificationKind.HEALTH_CONCERN.value,
                    title="The patient may need help",
                    body=f"During the guided demo the patient said: “{quoted}”. "
                         "This is not a diagnosis; please check on them.",
                    data={"run_id": run.run_id, "slot": result.name if result else None},
                    to_patient=True, to_caregivers=True)
        except Exception:  # noqa: BLE001 - the spoken reply below still happens
            log.exception("guided demo: HEALTH_CONCERN notification failed")
        self.buzzer.off()
        self._say(run, phrases.EMERGENCY, step="emergency", check=False)
        self._say(run, phrases.DEMO_ALERT_SENT, step="emergency", check=False)
        raise _Alerted()

    def _decline(self, run: _Run, result: SlotResult) -> None:
        if result.dose_event_id is None:
            return
        try:
            self.drops.skip_dose(result.dose_event_id, note="Declined by the patient in the guided demo",
                                 patient_id=run.patient_id)
        except Exception as exc:  # noqa: BLE001 - already dropped / missed: the answer is still recorded
            log.info("guided demo: dose %s not skipped (%s)", result.dose_event_id, type(exc).__name__)

    def _publish_buzzer(self, run: _Run) -> None:
        """Buzzer listener (may run on the buzzer's timer thread): publish its state, keep the step."""
        on = self.buzzer.tone_active or self.buzzer.hardware_active
        self.bus.publish(Topic.DEMO_GUIDED, {**self._view(run), "step": "buzzer_on" if on else "buzzer_off",
                                             "final": False})

    # ================================================================== time and doses
    def _lead(self) -> timedelta:
        return timedelta(minutes=min(15, int(self.settings.dose_early_minutes)))

    def _slot_time(self, hhmm: str, now: datetime) -> tuple[datetime | None, datetime]:
        """``(travel target or None, scheduled time)`` of the next dose at ``hhmm`` whose
        scheduled time has not passed yet. The clock only ever moves forward, to ``lead`` before it."""
        hh, mm = (int(x) for x in hhmm.split(":"))
        day = self.clock.to_local(now).date()
        for _ in range(3):
            scheduled = self.clock.localize(datetime.combine(day, time(hh, mm)))
            if scheduled > now:
                at = scheduled - self._lead()
                return (at if at > now else None), scheduled
            day += timedelta(days=1)
        return None, scheduled

    def _slot_plan(self, run: _Run, container: int) -> dict[str, Any]:
        """Medication + schedule time of a container (``{}`` when it has none)."""
        with self.db.session() as s:
            comp = s.scalars(select(Compartment).where(Compartment.device_id == self.settings.device_id,
                                                       Compartment.slot_number == container,
                                                       Compartment.active.is_(True))).first()
            if comp is None or comp.medication_id is None:
                return {}
            sched = s.scalars(select(Schedule).where(Schedule.medication_id == comp.medication_id,
                                                     Schedule.active.is_(True))
                              .order_by(Schedule.time_of_day)).first()
            med = comp.medication
            return {"medication_id": comp.medication_id, "medication_name": med.name if med else None,
                    "time_of_day": sched.time_of_day if sched else None}

    def _prepare_slot(self, run: _Run, name: str, container: int) -> dict[str, Any]:
        plan = self._slot_plan(run, container)
        if not plan.get("time_of_day"):
            return plan
        target, scheduled = self._slot_time(plan["time_of_day"], self.clock.now())
        if target is not None:
            self._travel(target)
        else:
            from tactidose.api.device import _tick_now

            _tick_now(self.services)   # make sure the dose exists and is DUE
        with self.db.session() as s:
            ev = s.scalars(select(DoseEvent).where(DoseEvent.user_id == run.patient_id,
                                                   DoseEvent.medication_id == plan["medication_id"],
                                                   DoseEvent.scheduled_at == scheduled)).first()
            if ev is not None and ev.status in _DISPENSABLE:
                plan["event_id"] = ev.event_id
        return plan

    def _travel(self, target: datetime) -> None:
        from tactidose.api.device import after_clock_change

        self.clock.travel_to(target)
        after_clock_change(self.services)   # materialise + DUE, wake the scheduler, CLOCK_CHANGED
        log.info("guided demo: clock -> %s", self.clock.local_now().isoformat())

    def _fresh_start(self, run: _Run) -> None:
        """Clock to the morning slot, then re-seed the demo data there (no earlier MISSED doses)."""
        from tactidose.api.device import after_clock_change, push_pill_counts
        from tactidose.db.seed import DEMO_MEDICATIONS, reset_demo

        morning = next((m.time_of_day for m in DEMO_MEDICATIONS if m.slot == 0), "08:00")
        loop = getattr(self.services, "scheduler_loop", None)
        guard = loop.exclusive() if loop is not None else _null()
        with guard:
            target, scheduled = self._slot_time(morning, self.clock.now())
            self.clock.travel_to(target or self.clock.now())
            summary = reset_demo(self.db, self.settings, self.clock, auth=self.services.auth, bus=self.bus,
                                 keep_sessions=True)
        push_pill_counts(getattr(self.services, "sim", None), summary.get("containers"))
        after_clock_change(self.services)
        run.conversation_id = self._open_conversation(run)   # the reset wiped the earlier one

    def _pause(self, run: _Run, seconds: float) -> None:
        if seconds > 0 and self._sleep(run.stop, seconds):
            raise _Stopped()
        self._check_stop(run)

    def _stop_now(self, run: _Run) -> None:
        """The patient said stop: halt the dispenser too, then end the run."""
        run.stop.set()
        try:
            self.drops.interrupt()
        except Exception:  # noqa: BLE001
            log.exception("guided demo: drops.interrupt() failed")
        raise _Stopped()

    @staticmethod
    def _check_stop(run: _Run) -> None:
        if run.stop.is_set():
            raise _Stopped()

    @staticmethod
    def _alive(run: _Run) -> bool:
        return run.thread is not None and run.thread.is_alive()

    # ================================================================== storage
    def _open_conversation(self, run: _Run) -> int | None:
        now = self.clock.now()
        try:
            with self.db.session() as s:
                conv = Conversation(patient_id=run.patient_id, channel="voice", title="Guided demo",
                                    started_at=now, last_message_at=now)
                s.add(conv)
                s.flush()
                return conv.conversation_id
        except Exception:  # noqa: BLE001 - the demo runs without a stored transcript
            log.exception("guided demo: could not open a conversation")
            return None

    def _store(self, run: _Run, role: str, text: str, *, input_mode: str | None = None) -> None:
        if run.conversation_id is None or not text:
            return
        now = self.clock.now()
        try:
            with self.db.session() as s:
                s.add(ConversationMessage(conversation_id=run.conversation_id, patient_id=run.patient_id, role=role,
                                          content=text[:2000], input_mode=input_mode,
                                          model=MODEL if role == "assistant" else None, created_at=now))
                conv = s.get(Conversation, run.conversation_id)
                if conv is not None:
                    conv.last_message_at = now
        except Exception:  # noqa: BLE001
            log.exception("guided demo: could not store a message")

    def _save_slot(self, run: _Run, r: SlotResult) -> None:
        ext = r.extraction or {}
        try:
            with self.db.session() as s:
                s.add(GuidedDemoSlot(
                    run_id=run.run_id, patient_id=run.patient_id, slot_index=r.index, slot_name=r.name,
                    dose_event_id=r.dose_event_id, drop_id=r.drop_id, conversation_id=run.conversation_id,
                    take_answer=r.take_answer, outcome=r.outcome, outcome_reason=r.reason, taken=r.taken,
                    checkin_text=r.checkin_text, mood=ext.get("mood"), symptoms=ext.get("symptoms"),
                    concerns=ext.get("concerns"), severity=ext.get("severity"),
                    extraction_source=ext.get("source"), alert=bool(ext.get("alert")),
                    created_at=self.clock.now()))
        except Exception:  # noqa: BLE001 - progress events still show the result
            log.exception("guided demo: could not store slot %s", r.name)

    # ================================================================== speech + events
    def _say(self, run: _Run, text: str, *, step: str, awaiting: str | None = None,
             outcome: dict[str, Any] | None = None, check: bool = True) -> None:
        if check:
            self._check_stop(run)
        self._store(run, "assistant", text)
        run.transcript.append({"who": "assistant", "text": text})
        del run.transcript[:-TRANSCRIPT_KEEP]
        self._publish(run, step=step, say=text, audio_url=self._audio(run, text), awaiting=awaiting, outcome=outcome)

    def _audio(self, run: _Run, text: str) -> str | None:
        agent = getattr(self.services, "agent", None)
        speak = getattr(agent, "speak", None)
        if not callable(speak) or self.settings.tts_provider == "none":
            return None
        try:
            audio_id = speak(run.patient_id, text)
        except Exception:  # noqa: BLE001 - the browser speaks instead
            return None
        return f"/api/agent/audio/{audio_id}.wav" if audio_id else None

    def _publish(self, run: _Run, *, step: str | None, final: bool = False, **extra: Any) -> None:
        run.step = step
        payload = {**self._view(run), **{k: v for k, v in extra.items() if v is not None}, "final": final}
        payload["awaiting"] = extra.get("awaiting") if "awaiting" in extra else run.awaiting
        self.bus.publish(Topic.DEMO_GUIDED, payload)

    def _view(self, run: _Run) -> dict[str, Any]:
        return {
            "patient_id": run.patient_id, "run_id": run.run_id, "state": run.state, "step": run.step,
            "slot": run.slot, "awaiting": run.awaiting, "started_at": run.started_at,
            # buzzer = the laptop tone is on (the screens beep); buzzer_hw = the device's buzzer is on.
            "buzzer": self.buzzer.tone_active, "buzzer_hw": self.buzzer.hardware_active,
            "buzzer_backend": self.buzzer.name,
            "results": [self._result_view(r) for r in run.results], "transcript": list(run.transcript[-20:]),
        }

    @staticmethod
    def _result_view(r: SlotResult) -> dict[str, Any]:
        out = asdict(r)
        out["summary"] = _slot_sentence(r)
        return out


def _slot_sentence(r: SlotResult) -> str:
    name = r.name.capitalize()
    if r.outcome == "DECLINED":
        return f"{name}: you skipped it."
    if r.outcome == "DROPPED":
        if r.taken:
            return f"{name}: the pill dropped and you took it."
        return f"{name}: the pill dropped; you said you haven't taken it yet."
    if r.outcome == "NO_DOSE":
        return f"{name}: no scheduled pill."
    if r.outcome == "STOPPED":
        return f"{name}: not finished."
    return f"{name}: the pill did not drop."


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None
