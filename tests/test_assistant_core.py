"""Assistant: the single action worker (tactidose/core/assistant.py).

Uses a scriptable fake DoseServiceAPI (below) and tests/fakes.FakeSpeaker. The gate timer
runs on a fake monotonic clock, so no test waits for real timeouts.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pytest

from tactidose.core import phrases
from tactidose.core.assistant import Assistant, Phase
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import (
    BlockReason,
    CancelOutcome,
    CancelStatus,
    ConfirmOutcome,
    ConfirmStatus,
    DispenseOutcome,
    DispenseStatus,
    DoseInfo,
    DoseServiceAPI,
    DueSummary,
    Intent,
    IntentSource,
    ReplyKind,
)
from tactidose.hardware.protocol import Command, CommandResult, Ev, HostCode, Message, MessageKind
from tests.conftest import TEST_NOW_LOCAL, TEST_TZ
from tests.fakes import FakeSpeaker, wait_until

TZ = ZoneInfo(TEST_TZ)
NOW = TEST_NOW_LOCAL.replace(tzinfo=TZ)
UI, VOICE, BUTTON = IntentSource.UI, IntentSource.VOICE, IntentSource.BUTTON


def dose(*, event_id: int = 12, hour: int = 8, minute: int = 0, slot: int | None = 2,
         name: str = "Vitamin C (demo candy)", status: str = "DUE",
         instructions: str | None = "Take one piece.") -> DoseInfo:
    local = datetime(2026, 10, 5, hour, minute, tzinfo=TZ)
    return DoseInfo(event_id=event_id, medication_id=3, medication_name=name, strength="1 piece",
                    instructions=instructions, slot=slot, scheduled_at=local.astimezone(timezone.utc),
                    scheduled_local=local, status=status)


VITC = dose()
CALCIUM = dose(event_id=13, hour=13, slot=4, name="Calcium (demo token)", instructions="Take with water.")


def ok_close() -> CommandResult:
    return CommandResult(Command.close_gate(), True, "GATE_CLOSED")


def failed_close() -> CommandResult:
    return CommandResult.host_failure(Command.close_gate(), HostCode.NOT_CONNECTED)


class FakeDose:
    """Scriptable DoseServiceAPI. ``hold`` makes dispense_next block until released/interrupted."""

    def __init__(self) -> None:
        self.summary = DueSummary(now_local=NOW)
        self.awaiting: DoseInfo | None = None
        self.dispense_results: deque[DispenseOutcome] = deque()
        self.confirm_results: deque[ConfirmOutcome] = deque()
        self.cancel_results: deque[CancelOutcome] = deque()
        self.close_results: deque[CommandResult | None] = deque()
        self.calls: list[str] = []
        self.interrupts: list[tuple[IntentSource, bool]] = []
        self.motion_dose: DoseInfo | None = None
        self.raise_in: set[str] = set()
        self.hold = False
        self.holding = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.block_check = threading.Event()  # set = check_due blocks until released
        self.check_released = threading.Event()

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.raise_in:
            raise RuntimeError(f"boom in {name}")

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c == name)

    # -- DoseServiceAPI
    def check_due(self) -> DueSummary:
        self._call("check_due")
        if self.block_check.is_set():
            self.check_released.wait(5)
        return self.summary

    def dispense_next(self, source: IntentSource, *,
                      on_motion_start: Callable[[DoseInfo], None] | None = None) -> DispenseOutcome:
        self._call("dispense_next")
        if self.motion_dose is not None and on_motion_start is not None:
            on_motion_start(self.motion_dose)
        if self.hold:
            self.holding.set()
            self.release.wait(5)
            self.holding.clear()
            if self.stopped.is_set():
                return DispenseOutcome(DispenseStatus.CANCELLED, dose=self.motion_dose)
        if self.dispense_results:
            return self.dispense_results.popleft()
        return DispenseOutcome(DispenseStatus.NOTHING_DUE)

    def confirm_taken(self, source: IntentSource) -> ConfirmOutcome:
        self._call("confirm_taken")
        return self.confirm_results.popleft() if self.confirm_results else ConfirmOutcome(ConfirmStatus.NOTHING_TO_CONFIRM)

    def cancel(self, source: IntentSource) -> CancelOutcome:
        self._call("cancel")
        return self.cancel_results.popleft() if self.cancel_results else CancelOutcome(CancelStatus.NOTHING_TO_CANCEL)

    def interrupt(self, source: IntentSource) -> bool:
        busy = self.holding.is_set()
        self.interrupts.append((source, busy))
        self.calls.append("interrupt")
        if busy:
            self.stopped.set()
            self.release.set()
        return busy

    def awaiting_confirmation(self) -> DoseInfo | None:
        self._call("awaiting_confirmation")
        return self.awaiting

    def close_gate(self, reason: str) -> CommandResult | None:
        self._call(f"close_gate:{reason}")
        return self.close_results.popleft() if self.close_results else None


class MonoClock(Clock):
    """Frozen wall clock + a monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        super().__init__(TEST_TZ, frozen_at=TEST_NOW_LOCAL)
        self.mono = 1000.0

    def monotonic(self) -> float:  # type: ignore[override]
        return self.mono


class RecordingSpeaker(FakeSpeaker):
    def __init__(self) -> None:
        super().__init__()
        self.flags: list[bool] = []

    def say(self, text: str, *, kind: str = "info", interrupt: bool = False,
            meta: dict[str, Any] | None = None) -> None:
        self.flags.append(interrupt)
        super().say(text, kind=kind, interrupt=interrupt, meta=meta)


class H:
    def __init__(self, assistant: Assistant, dose: FakeDose, clock: MonoClock, speaker: RecordingSpeaker,
                 bus: EventBus) -> None:
        self.a, self.dose, self.clock, self.speaker, self.bus = assistant, dose, clock, speaker, bus

    def run(self, intent: Intent | str, source: IntentSource = UI, **kw: Any):
        reply = self.a.submit(intent, source, wait=True, timeout=5, **kw)
        assert reply is not None, f"no reply for {intent}"
        return reply

    def text(self, text: str, source: IntentSource = IntentSource.KEYBOARD, confidence: float = 1.0):
        return self.a.handle_text(text, source, confidence, wait=True)

    def idle(self) -> None:
        def quiet() -> bool:
            snap = self.a.state()  # one snapshot: busy/pending must be read together
            return not snap["busy"] and snap["pending"] == 0

        assert wait_until(quiet, timeout=5)

    def events(self, topic: str) -> list[dict[str, Any]]:
        return [e.data for e in self.bus.recent(400, [topic])]


@pytest.fixture
def make(settings, bus):
    made: list[Assistant] = []

    def factory(*, start: bool = True, **overrides: Any) -> H:
        s = settings.model_copy(update=overrides) if overrides else settings
        fake, clock, speaker = FakeDose(), MonoClock(), RecordingSpeaker()
        assert isinstance(fake, DoseServiceAPI)
        assistant = Assistant(fake, speaker, bus, s, clock)
        made.append(assistant)
        if start:
            assistant.start()
        return H(assistant, fake, clock, speaker, bus)

    yield factory
    for assistant in made:
        assistant.close()


# =========================================================================== CHECK_DUE


def test_check_due_announces_and_asks_for_consent(make):
    h = make()
    h.dose.summary = DueSummary(now_local=NOW, due=(VITC,))
    reply = h.run(Intent.CHECK_DUE)
    assert reply.intent is Intent.CHECK_DUE and reply.kind is ReplyKind.PROMPT
    assert reply.text == ("Your 8:00 AM Vitamin C (demo candy) is due now. Say 'dispense' or press the big button.")
    assert h.speaker.last() == reply.text and reply.spoken
    assert h.dose.count("dispense_next") == 0  # consent required
    assert reply.outcome["due"][0]["event_id"] == 12


def test_check_due_auto_dispense_setting(make):
    h = make(check_due_auto_dispense=True)
    h.dose.summary = DueSummary(now_local=NOW, due=(VITC,))
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    reply = h.run(Intent.CHECK_DUE)
    assert reply.intent is Intent.DISPENSE and reply.outcome["requested"] == "CHECK_DUE"
    assert h.dose.count("dispense_next") == 1 and reply.kind is ReplyKind.SUCCESS


def test_check_due_while_a_dose_awaits_confirmation(make):
    h = make()
    opened = dose(status="DISPENSED")
    h.dose.summary = DueSummary(now_local=NOW, awaiting_confirmation=(opened,), due=(CALCIUM,))
    reply = h.run(Intent.CHECK_DUE)
    assert reply.text == ("Compartment 3 was opened for your 8:00 AM Vitamin C (demo candy). When you have "
                          "taken it, say 'taken' or press the big button. You also have one more dose due after that.")


def test_check_due_nothing_due_mentions_next_dose(make):
    h = make()
    h.dose.summary = DueSummary(now_local=NOW, next_upcoming=CALCIUM)
    reply = h.run(Intent.CHECK_DUE)
    assert reply.text == ("You do not have a scheduled medication due right now. "
                          "Your next dose is Calcium (demo token) at 1:00 PM.")
    assert reply.kind is ReplyKind.INFO


@pytest.mark.parametrize(
    "summary,expected,kind",
    [
        (DueSummary(now_local=NOW, blocked=((VITC, BlockReason.IN_PROGRESS),)), phrases.IN_PROGRESS, ReplyKind.INFO),
        (DueSummary(now_local=NOW, accessed=(dose(status="TAKEN"),), blocked=((CALCIUM, BlockReason.NEEDS_REVIEW),)),
         phrases.NEEDS_REVIEW, ReplyKind.ERROR),
        (DueSummary(now_local=NOW, accessed=(dose(status="TAKEN"),)),
         "Your 8:00 AM Vitamin C (demo candy) is already recorded as taken.", ReplyKind.INFO),
        (DueSummary(now_local=NOW, accessed=(dose(status="DISPENSED"),)), phrases.ALREADY_ACCESSED, ReplyKind.INFO),
        (DueSummary(now_local=NOW, blocked=((VITC, BlockReason.TOO_SOON), (CALCIUM, BlockReason.NO_COMPARTMENT))),
         phrases.TOO_SOON, ReplyKind.WARNING),
        (DueSummary(now_local=NOW, blocked=((VITC, BlockReason.NO_COMPARTMENT),)), phrases.NO_COMPARTMENT,
         ReplyKind.WARNING),
        (DueSummary(now_local=NOW, blocked=((VITC, BlockReason.UNCONFIRMED_MEDICATION),)),
         phrases.UNCONFIRMED_MEDICATION, ReplyKind.WARNING),
    ],
)
def test_check_due_refusal_priority(make, summary, expected, kind):
    h = make()
    h.dose.summary = summary
    reply = h.run(Intent.CHECK_DUE)
    assert reply.text == expected and reply.kind is kind


# =========================================================================== DISPENSE


READY = ("Your Vitamin C (demo candy) is ready in compartment 3. The label says: Take one piece. "
         "When you have taken it, say 'taken' or press the big button.")
UNCERTAIN = CommandResult.host_failure(Command.dispense_slot(2), HostCode.TIMEOUT)


@pytest.mark.parametrize(
    "outcome,expected,kind",
    [
        (DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC), READY, ReplyKind.SUCCESS),
        (DispenseOutcome(DispenseStatus.DUPLICATE, dose=VITC), phrases.ALREADY_ACCESSED, ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.DUPLICATE, dose=VITC, next_upcoming=CALCIUM),
         "That scheduled dose has already been accessed. Your next dose is Calcium (demo token) at 1:00 PM.",
         ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.DUPLICATE, dose=VITC, reason="TOO_SOON"), phrases.TOO_SOON, ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.NOTHING_DUE), phrases.NOTHING_DUE, ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.NOTHING_DUE, next_upcoming=CALCIUM),
         "You do not have a scheduled medication due right now. Your next dose is Calcium (demo token) at 1:00 PM.",
         ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.IN_PROGRESS), phrases.IN_PROGRESS, ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.BLOCKED, dose=VITC, reason="NEEDS_REVIEW"), phrases.NEEDS_REVIEW,
         ReplyKind.ERROR),
        (DispenseOutcome(DispenseStatus.BLOCKED, dose=VITC, reason="NO_COMPARTMENT"), phrases.NO_COMPARTMENT,
         ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.BLOCKED, dose=VITC, reason="INACTIVE"), phrases.INACTIVE, ReplyKind.WARNING),
        (DispenseOutcome(DispenseStatus.HARDWARE_UNAVAILABLE, reason="NOT_CONNECTED"), phrases.HARDWARE_UNAVAILABLE,
         ReplyKind.ERROR),
        (DispenseOutcome(DispenseStatus.HARDWARE_ERROR, dose=VITC, reason="MOTOR_FAULT"), phrases.COULD_NOT_PREPARE,
         ReplyKind.ERROR),
        (DispenseOutcome(DispenseStatus.HARDWARE_ERROR, dose=VITC, hardware=UNCERTAIN), phrases.COULD_NOT_PREPARE,
         ReplyKind.ERROR),
        (DispenseOutcome(DispenseStatus.CANCELLED, dose=VITC), phrases.DISPENSE_CANCELLED, ReplyKind.INFO),
        (DispenseOutcome(DispenseStatus.DB_ERROR), phrases.DB_UNAVAILABLE, ReplyKind.ERROR),
    ],
)
def test_dispense_outcome_phrases(make, outcome, expected, kind):
    h = make()
    h.dose.dispense_results.append(outcome)
    reply = h.run(Intent.DISPENSE)
    assert (reply.intent, reply.text, reply.kind) == (Intent.DISPENSE, expected, kind)
    assert reply.outcome["status"] == outcome.status.value
    assert h.speaker.last() == expected
    expected_phase = Phase.ATTENTION if kind is ReplyKind.ERROR else Phase.IDLE
    if outcome.status is DispenseStatus.DISPENSED:
        expected_phase = Phase.AWAITING_CONFIRMATION
    assert h.a.state()["phase"] == expected_phase.value


def test_safety_warning_is_spoken_before_motion(make):
    h = make()
    h.dose.motion_dose = VITC
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    phases_seen: list[str] = []
    h.bus.add_listener(lambda ev: phases_seen.append(ev.data["phase"]), [Topic.ASSISTANT_STATE])
    reply = h.run(Intent.DISPENSE)
    assert h.speaker.texts[-2:] == [phrases.PREPARING, READY]
    assert h.speaker.flags[-2:] == [True, False]  # the warning cuts off anything still playing
    assert phases_seen[-2:] == ["PREPARING", "AWAITING_CONFIRMATION"]
    state = h.a.state()
    assert state["awaiting"]["event_id"] == 12 and state["gate_timer_s"] == 60.0
    assert reply.spoken


def test_no_safety_warning_when_nothing_moves(make):
    h = make()
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DUPLICATE, dose=VITC))
    h.run(Intent.DISPENSE)
    assert phrases.PREPARING not in h.speaker.texts


def test_generic_wording_never_speaks_medication_names(make):
    h = make(tts_include_med_names=False)
    h.dose.motion_dose = VITC
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    h.dose.summary = DueSummary(now_local=NOW, due=(VITC,), next_upcoming=CALCIUM)
    h.run(Intent.CHECK_DUE)
    h.run(Intent.DISPENSE)
    h.dose.confirm_results.append(ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=True))
    h.dose.summary = DueSummary(now_local=NOW)
    h.run(Intent.CONFIRM_TAKEN)
    spoken = " ".join(h.speaker.texts)
    assert "Vitamin" not in spoken and "Calcium" not in spoken and "label says" not in spoken
    assert "Your 8:00 AM medication is ready in compartment 3." in spoken


# =========================================================================== CONFIRM_TAKEN


@pytest.mark.parametrize(
    "outcome,expected,kind",
    [
        (ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=True),
         "Thank you. Your Vitamin C (demo candy) is recorded as taken.", ReplyKind.SUCCESS),
        (ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=None),
         "Thank you. Your Vitamin C (demo candy) is recorded as taken.", ReplyKind.SUCCESS),
        (ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=False),
         "Thank you. Your Vitamin C (demo candy) is recorded as taken. " + phrases.GATE_CLOSE_FAILED, ReplyKind.ERROR),
        (ConfirmOutcome(ConfirmStatus.ALREADY_CONFIRMED, dose=dose(status="TAKEN")),
         "Your 8:00 AM Vitamin C (demo candy) is already recorded as taken.", ReplyKind.INFO),
        (ConfirmOutcome(ConfirmStatus.NOTHING_TO_CONFIRM), phrases.NOTHING_TO_CONFIRM, ReplyKind.WARNING),
        (ConfirmOutcome(ConfirmStatus.DB_ERROR), phrases.CONFIRM_DB_ERROR, ReplyKind.ERROR),
    ],
)
def test_confirm_outcome_phrases(make, outcome, expected, kind):
    h = make()
    h.dose.confirm_results.append(outcome)
    reply = h.run(Intent.CONFIRM_TAKEN)
    assert (reply.intent, reply.text, reply.kind) == (Intent.CONFIRM_TAKEN, expected, kind)
    assert reply.outcome["status"] == outcome.status.value


def test_confirm_tells_the_user_about_more_due_doses(make):
    h = make()
    h.dose.confirm_results.append(ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=True))
    h.dose.summary = DueSummary(now_local=NOW, due=(CALCIUM,))
    reply = h.run(Intent.CONFIRM_TAKEN)
    assert reply.text.endswith("You have one more dose due. Say 'dispense' when you are ready.")


# =========================================================================== CANCEL


@pytest.mark.parametrize(
    "outcome,awaiting,expected,kind",
    [
        (CancelOutcome(CancelStatus.STOPPED_MOTION), None, phrases.CANCEL_STOPPED, ReplyKind.INFO),
        (CancelOutcome(CancelStatus.CLOSED_GATE), None, phrases.CANCEL_CLOSED_GATE, ReplyKind.INFO),
        (CancelOutcome(CancelStatus.CLOSED_GATE), VITC, phrases.CANCEL_CLOSED_GATE_REMINDER, ReplyKind.INFO),
        (CancelOutcome(CancelStatus.NOTHING_TO_CANCEL), None, phrases.CANCELLED, ReplyKind.INFO),
        (CancelOutcome(CancelStatus.NOTHING_TO_CANCEL), VITC, phrases.CANCELLED_REMINDER, ReplyKind.INFO),
        (CancelOutcome(CancelStatus.FAILED), None, phrases.CANCEL_FAILED, ReplyKind.ERROR),
    ],
)
def test_cancel_outcome_phrases(make, outcome, awaiting, expected, kind):
    h = make()
    h.dose.awaiting = awaiting
    h.dose.cancel_results.append(outcome)
    reply = h.run(Intent.CANCEL)
    assert (reply.intent, reply.text, reply.kind) == (Intent.CANCEL, expected, kind)
    assert h.speaker.flags[-1] is True  # cancel replies interrupt current speech
    assert h.dose.interrupts == [(UI, False)]  # STOP attempted synchronously first


def test_cancel_interrupts_a_blocked_dispense_immediately_and_speaks_once(make):
    h = make()
    h.dose.hold = True
    h.dose.motion_dose = VITC
    assert h.a.submit(Intent.DISPENSE, VOICE) is None
    assert h.dose.holding.wait(2)
    assert h.a.submit(Intent.CANCEL, VOICE) is None
    # interrupt() ran synchronously inside submit(), while the worker was still blocked:
    assert h.dose.interrupts == [(VOICE, True)]
    h.idle()
    cancel_lines = [t for t in h.speaker.texts if t.startswith("Cancelled")]
    assert cancel_lines == [phrases.DISPENSE_CANCELLED]
    assert h.speaker.texts == [phrases.PREPARING, phrases.DISPENSE_CANCELLED]
    assert h.dose.count("cancel") == 1  # the queued CANCEL still ran (and found nothing left to do)
    assert h.a.state()["phase"] == "IDLE"


def test_cancel_button_during_dispense_relies_on_firmware_stop(make):
    h = make()
    h.dose.hold = True
    h.dose.motion_dose = VITC
    h.a.submit(Intent.DISPENSE, VOICE)
    assert h.dose.holding.wait(2)
    h.a.on_hardware_event(Message(MessageKind.EVENT, Ev.CANCEL_BUTTON.value, raw="EVENT CANCEL_BUTTON"))
    assert h.dose.interrupts == []  # never blocks the serial reader thread with a STOP
    h.dose.stopped.set()            # the firmware stopped locally -> ERR STOPPED
    h.dose.release.set()
    h.idle()
    assert [t for t in h.speaker.texts if t.startswith("Cancelled")] == [phrases.DISPENSE_CANCELLED]


def test_cancel_that_arrives_after_motion_started_but_dispense_succeeded(make):
    h = make()
    h.dose.hold = True
    h.dose.motion_dose = VITC
    original_interrupt = h.dose.interrupt
    h.dose.interrupt = lambda source: (h.dose.interrupts.append((source, False)), False)[1]  # STOP raced: too late
    h.a.submit(Intent.DISPENSE, UI)
    assert h.dose.holding.wait(2)
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    h.dose.cancel_results.append(CancelOutcome(CancelStatus.CLOSED_GATE))
    h.dose.awaiting = dose(status="DISPENSED")
    h.a.submit(Intent.CANCEL, UI)
    h.dose.release.set()
    h.idle()
    assert READY not in h.speaker.texts  # never announce "ready" for a dispense the user cancelled
    assert h.speaker.last() == phrases.CANCEL_CLOSED_GATE_REMINDER
    h.dose.interrupt = original_interrupt


def test_cancel_drops_queued_motion_requests(make):
    h = make()
    h.dose.block_check.set()
    h.a.submit(Intent.CHECK_DUE, UI)                      # occupies the worker
    assert wait_until(lambda: h.dose.count("check_due") == 1)
    results: list[Any] = []
    waiter = threading.Thread(target=lambda: results.append(h.a.submit(Intent.DISPENSE, UI, wait=True, timeout=5)))
    waiter.start()
    assert wait_until(lambda: h.a.state()["pending"] == 1)
    h.a.submit(Intent.CANCEL, UI)
    waiter.join(timeout=5)
    assert results and results[0].outcome["status"] == "CANCELLED" and results[0].spoken is False
    h.dose.check_released.set()
    h.idle()
    assert h.dose.count("dispense_next") == 0


# =========================================================================== PRIMARY_ACTION


def test_primary_action_confirms_when_awaiting(make):
    h = make()
    h.dose.awaiting = dose(status="DISPENSED")
    h.dose.confirm_results.append(ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=True))
    reply = h.run(Intent.PRIMARY_ACTION, BUTTON)
    assert reply.intent is Intent.CONFIRM_TAKEN and reply.outcome["requested"] == "PRIMARY_ACTION"
    assert h.dose.count("dispense_next") == 0


def test_primary_action_dispenses_when_due(make):
    h = make()
    h.dose.summary = DueSummary(now_local=NOW, due=(VITC,))
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    reply = h.run(Intent.PRIMARY_ACTION, BUTTON)
    assert reply.intent is Intent.DISPENSE and reply.kind is ReplyKind.SUCCESS
    mapped = [e for e in h.events(Topic.INTENT) if e.get("via") == "PRIMARY_ACTION"]
    assert mapped[-1]["intent"] == "DISPENSE"


def test_primary_action_checks_otherwise(make):
    h = make()
    h.dose.summary = DueSummary(now_local=NOW, next_upcoming=CALCIUM)
    reply = h.run(Intent.PRIMARY_ACTION, UI)
    assert reply.intent is Intent.CHECK_DUE and reply.text.startswith(phrases.NOTHING_DUE)
    assert h.dose.count("dispense_next") == 0 and h.dose.count("confirm_taken") == 0


# =========================================================================== gate timer


def dispense_ok(h: H) -> None:
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    h.dose.awaiting = dose(status="DISPENSED")
    h.run(Intent.DISPENSE)


def test_gate_timeout_closes_the_gate(make):
    h = make()
    dispense_ok(h)
    h.dose.close_results.append(ok_close())
    h.clock.mono += 59
    time.sleep(0.12)
    assert "close_gate:timeout" not in h.dose.calls
    h.clock.mono += 2
    assert wait_until(lambda: "close_gate:timeout" in h.dose.calls, timeout=2)
    assert wait_until(lambda: h.speaker.last() == phrases.GATE_CLOSED_TIMEOUT, timeout=2)
    assert h.a.state()["gate_timer_s"] is None
    assert [e["intent"] for e in h.events(Topic.INTENT)][-1] == "GATE_TIMEOUT"
    assert h.a.state()["phase"] == "AWAITING_CONFIRMATION"  # still confirmable after the close


def test_gate_timer_is_cancelled_by_confirmation(make):
    h = make()
    dispense_ok(h)
    h.dose.confirm_results.append(ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=True))
    h.dose.awaiting = None
    h.run(Intent.CONFIRM_TAKEN)
    h.clock.mono += 120
    time.sleep(0.15)
    assert not any(c.startswith("close_gate") for c in h.dose.calls)
    assert h.a.state()["phase"] == "IDLE"


def test_gate_timer_is_cancelled_by_cancel(make):
    h = make()
    dispense_ok(h)
    h.dose.cancel_results.append(CancelOutcome(CancelStatus.CLOSED_GATE))
    h.run(Intent.CANCEL)
    h.clock.mono += 120
    time.sleep(0.15)
    assert not any(c.startswith("close_gate") for c in h.dose.calls)


def test_gate_timer_survives_a_failed_gate_close_on_confirm(make):
    h = make()
    dispense_ok(h)
    h.dose.confirm_results.append(ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=False))
    h.run(Intent.CONFIRM_TAKEN)
    h.dose.close_results.append(ok_close())
    h.dose.awaiting = None
    h.clock.mono += 61
    assert wait_until(lambda: "close_gate:timeout" in h.dose.calls, timeout=2)
    assert wait_until(lambda: h.speaker.last() == phrases.GATE_CLOSED, timeout=2)


def test_gate_timeout_close_failure_needs_attention(make):
    h = make()
    dispense_ok(h)
    h.dose.close_results.append(failed_close())
    h.clock.mono += 61
    assert wait_until(lambda: h.speaker.last() == phrases.GATE_CLOSE_FAILED, timeout=2)
    assert wait_until(lambda: h.a.state()["phase"] == "ATTENTION", timeout=2)


def test_gate_timeout_when_gate_already_closed_only_reminds(make):
    h = make()
    dispense_ok(h)
    h.clock.mono += 61  # close_gate returns None: nothing was open
    assert wait_until(lambda: "close_gate:timeout" in h.dose.calls, timeout=2)
    assert wait_until(lambda: h.speaker.last() == phrases.TAKEN_REMINDER, timeout=2)


def test_uncertain_dispense_arms_a_silent_safety_close(make):
    h = make()
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.HARDWARE_ERROR, dose=VITC, hardware=UNCERTAIN))
    h.run(Intent.DISPENSE)
    h.dose.close_results.append(ok_close())
    h.clock.mono += 61
    assert wait_until(lambda: "close_gate:timeout" in h.dose.calls, timeout=2)
    assert wait_until(lambda: h.speaker.last() == phrases.GATE_CLOSED, timeout=2)


def test_explicit_gate_timeout_intent(make):
    h = make()
    h.dose.close_results.append(ok_close())
    reply = h.run(Intent.GATE_TIMEOUT, IntentSource.SYSTEM)
    assert reply.text == phrases.GATE_CLOSED and reply.outcome["ok"] is True


# =========================================================================== REPEAT / HELP / UNKNOWN


def test_repeat_replays_the_last_reply(make):
    h = make()
    assert h.run(Intent.REPEAT).text == phrases.NOTHING_TO_REPEAT
    h.run(Intent.HELP)
    reply = h.run(Intent.REPEAT)
    assert reply.text == phrases.HELP and reply.intent is Intent.REPEAT and reply.outcome["repeat_of"] == "HELP"
    h.text("banana sandwich")  # "Sorry..." is not what the user wants repeated
    assert h.run(Intent.REPEAT).text == phrases.HELP
    assert h.speaker.flags[-1] is True  # repeat restarts cleanly


def test_help(make):
    h = make()
    reply = h.run(Intent.HELP)
    assert reply.text == phrases.HELP and reply.kind is ReplyKind.INFO


def test_typed_text_paths(make):
    h = make()
    h.dose.summary = DueSummary(now_local=NOW, due=(VITC,))
    assert h.text("What do I take now?").intent is Intent.CHECK_DUE
    assert h.text("asdf qwerty").text == phrases.NOT_UNDERSTOOD
    assert h.text("").text == phrases.NOT_UNDERSTOOD
    assert h.text("I haven't taken it").text == phrases.NEGATED
    h.dose.awaiting = dose(status="DISPENSED")
    assert h.text("I didn't take it").text == phrases.NEGATED_REMINDER
    assert h.text("don't dispense").text == phrases.NEGATED_REMINDER
    assert h.dose.count("confirm_taken") == 0 and h.dose.count("dispense_next") == 0


@pytest.mark.parametrize(
    "text,confidence,expected_speech,expected_call",
    [
        ("taken", 0.9, "Thank you.", "confirm_taken"),
        ("taken", 0.3, phrases.NOT_UNDERSTOOD, None),              # low confidence with real words
        ("[unk]", 0.9, None, None),                                 # noise: silent
        ("that [unk]", 0.92, None, None),                           # grammar-mode noise: silent
        ("", 0.9, None, None),
        ("yes okay", 0.9, None, None),                              # fillers only: silent
        ("stop", 0.4, phrases.CANCELLED, "cancel"),                 # stop accepted at lower confidence
        ("stop", 0.2, phrases.NOT_UNDERSTOOD, None),
        ("[unk] dispense", 0.9, phrases.NOT_UNDERSTOOD, None),      # never actuate on a partial guess
        ("[unk] help", 0.9, phrases.HELP, None),
        ("banana", 0.9, phrases.NOT_UNDERSTOOD, None),
        ("the weather is nice today in vancouver and sunny", 0.95, None, None),  # background speech
        ("I have not taken it", 0.9, phrases.NEGATED, None),
        ("dispense", 0.9, phrases.NOTHING_DUE, "dispense_next"),
    ],
)
def test_voice_rules(make, text, confidence, expected_speech, expected_call):
    h = make()
    h.dose.confirm_results.append(ConfirmOutcome(ConfirmStatus.CONFIRMED, dose=dose(status="TAKEN"), gate_closed=True))
    h.a.handle_voice_text(text, confidence)
    h.idle()
    if expected_speech is None:
        assert h.speaker.texts == []
    else:
        assert h.speaker.texts and h.speaker.texts[-1].startswith(expected_speech)
    for name in ("confirm_taken", "dispense_next", "cancel"):
        assert (h.dose.count(name) == 1) is (name == expected_call), name


def test_voice_text_never_raises(make):
    h = make()
    h.a.handle_voice_text(None, 0.9)  # type: ignore[arg-type]
    h.idle()


# =========================================================================== hardware events / notices


def test_confirm_button_becomes_primary_action(make):
    h = make()
    h.dose.summary = DueSummary(now_local=NOW, due=(VITC,))
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DISPENSED, dose=VITC))
    h.a.on_hardware_event(Message(MessageKind.EVENT, Ev.CONFIRM_BUTTON.value, raw="EVENT CONFIRM_BUTTON"))
    h.idle()
    assert h.dose.count("dispense_next") == 1
    first = h.events(Topic.INTENT)[0]
    assert (first["intent"], first["source"]) == ("PRIMARY_ACTION", "button")


def test_boot_event_announces_restart(make):
    h = make()
    h.a.on_hardware_event(Message(MessageKind.EVENT, Ev.BOOT.value, ("1.0.0",), raw="EVENT BOOT 1.0.0"))
    assert wait_until(lambda: h.speaker.last() == phrases.DEVICE_RESTARTED, timeout=2)
    notices = h.events(Topic.NOTICE)
    assert notices[-1]["level"] == "warning" and notices[-1]["message"] == phrases.DEVICE_RESTARTED
    assert h.run(Intent.REPEAT).text == phrases.DEVICE_RESTARTED


def test_unknown_hardware_messages_are_ignored(make):
    h = make()
    h.a.on_hardware_event(Message(MessageKind.EVENT, "SOMETHING_NEW", raw="EVENT SOMETHING_NEW"))
    h.a.on_hardware_event("not even a message")  # type: ignore[arg-type]
    h.idle()
    assert h.speaker.texts == []


def test_unsolicited_fault_is_announced_once_per_transition(make):
    h = make()
    h.bus.publish(Topic.DEVICE_STATE, {"state": "FAULT"})
    assert wait_until(lambda: h.speaker.last() == phrases.DEVICE_NEEDS_ATTENTION, timeout=2)
    h.bus.publish(Topic.DEVICE_STATE, {"state": "FAULT"})
    h.idle()
    assert h.speaker.texts.count(phrases.DEVICE_NEEDS_ATTENTION) == 1
    assert h.a.state()["phase"] == "ATTENTION"
    h.bus.publish(Topic.DEVICE_STATE, {"state": "READY"})
    h.bus.publish(Topic.DEVICE_STATE, {"state": "FAULT"})
    assert wait_until(lambda: h.speaker.texts.count(phrases.DEVICE_NEEDS_ATTENTION) == 2, timeout=2)


def test_fault_after_a_hardware_error_reply_is_not_announced_twice(make):
    h = make()
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.HARDWARE_ERROR, dose=VITC, reason="MOTOR_FAULT"))
    h.run(Intent.DISPENSE)
    h.bus.publish(Topic.DEVICE_STATE, {"state": "FAULT"})
    h.idle()
    assert h.speaker.texts == [phrases.COULD_NOT_PREPARE]


# =========================================================================== robustness


def test_worker_survives_handler_exceptions(make):
    h = make()
    h.dose.raise_in = {"check_due"}
    reply = h.run(Intent.CHECK_DUE)
    assert reply.text == phrases.ERROR_GENERIC == "Please ask for assistance." and reply.kind is ReplyKind.ERROR
    assert reply.outcome["error"] == "RuntimeError"
    assert h.a.state()["phase"] == "ATTENTION"
    h.dose.raise_in = set()
    assert h.run(Intent.HELP).text == phrases.HELP
    assert h.a.state()["phase"] == "ATTENTION"  # passive intents do not clear attention


def test_exception_during_dispense_arms_the_gate_safety_close(make):
    h = make()
    h.dose.raise_in = {"dispense_next"}
    assert h.run(Intent.DISPENSE).text == phrases.ERROR_GENERIC
    h.dose.close_results.append(ok_close())
    h.clock.mono += 61
    assert wait_until(lambda: "close_gate:timeout" in h.dose.calls, timeout=2)


def test_speaker_failure_is_reported_not_fatal(make):
    h = make()

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audio stack gone")

    h.speaker.say = broken  # type: ignore[method-assign]
    reply = h.run(Intent.HELP)
    assert reply.spoken is False
    assert h.run(Intent.HELP).text == phrases.HELP


def test_wait_timeout_returns_none_and_work_still_completes(make):
    h = make()
    h.dose.hold = True
    started = time.monotonic()
    assert h.a.submit(Intent.DISPENSE, UI, wait=True, timeout=0.05) is None
    assert time.monotonic() - started < 1.0
    assert h.dose.holding.wait(2)
    h.dose.dispense_results.append(DispenseOutcome(DispenseStatus.DUPLICATE, dose=VITC))
    h.dose.release.set()
    assert wait_until(lambda: h.speaker.last() == phrases.ALREADY_ACCESSED, timeout=2)


def test_submit_accepts_strings_and_rejects_unknown_names(make):
    h = make()
    assert h.a.submit("HELP", "keyboard", wait=True, timeout=5).text == phrases.HELP
    with pytest.raises(ValueError):
        h.a.submit("FLY_TO_THE_MOON", "ui")


def test_unknown_intent_submitted_directly(make):
    h = make()
    assert h.run(Intent.UNKNOWN, text="mumble").text == phrases.NOT_UNDERSTOOD


# =========================================================================== bus contract / state


def test_every_intent_is_published(make):
    h = make()
    h.run(Intent.HELP)
    h.text("what do i take now")
    h.run(Intent.CANCEL, VOICE)
    published = h.events(Topic.INTENT)
    assert [(e["intent"], e["source"]) for e in published] == [
        ("HELP", "ui"), ("CHECK_DUE", "keyboard"), ("CANCEL", "voice")]
    assert published[1]["text"] == "what do i take now"


def test_speech_metadata_carries_intent_and_source(make):
    h = make()
    h.run(Intent.HELP, VOICE)
    assert h.speaker.meta[-1] == {"intent": "HELP", "source": "voice"}


def test_state_contract_and_json(make):
    h = make()
    dispense_ok(h)
    state = h.a.state()
    for key in ("phase", "last_reply", "awaiting", "dose", "busy", "pending", "gate_timer_s", "running"):
        assert key in state
    assert state["phase"] == "AWAITING_CONFIRMATION" and state["running"] is True
    assert state["last_reply"]["intent"] == "DISPENSE" and state["last_reply"]["kind"] == "success"
    json.dumps(state)
    published = h.events(Topic.ASSISTANT_STATE)[-1]
    assert published["phase"] == "AWAITING_CONFIRMATION" and published["dose"]["event_id"] == 12
    assert published["message"] == READY


def test_dose_updates_elsewhere_refresh_the_awaiting_dose(make):
    h = make()
    assert h.a.state()["awaiting"] is None
    h.dose.awaiting = dose(status="DISPENSED")
    h.bus.publish(Topic.DOSE_UPDATED, {"event_id": 12, "status": "DISPENSED"})
    assert wait_until(lambda: (h.a.state()["awaiting"] or {}).get("event_id") == 12, timeout=2)
    assert h.a.state()["phase"] == "AWAITING_CONFIRMATION"
    h.dose.awaiting = None
    h.bus.publish(Topic.DOSE_UPDATED, {"event_id": 12, "status": "TAKEN"})
    assert wait_until(lambda: h.a.state()["phase"] == "IDLE", timeout=2)
    assert h.speaker.texts == []  # refreshes are silent


def test_static_replies_are_all_precached(make):
    """Every reply without names/times must be in CRITICAL_PHRASES (offline cache)."""
    h = make()
    scenario = [
        (DispenseStatus.DUPLICATE, None), (DispenseStatus.NOTHING_DUE, None), (DispenseStatus.IN_PROGRESS, None),
        (DispenseStatus.BLOCKED, "NEEDS_REVIEW"), (DispenseStatus.BLOCKED, "NO_COMPARTMENT"),
        (DispenseStatus.HARDWARE_UNAVAILABLE, None), (DispenseStatus.HARDWARE_ERROR, None),
        (DispenseStatus.CANCELLED, None), (DispenseStatus.DB_ERROR, None),
    ]
    for status, reason in scenario:
        h.dose.dispense_results.append(DispenseOutcome(status, dose=VITC, reason=reason or ""))
        h.run(Intent.DISPENSE)
    for status in (ConfirmStatus.NOTHING_TO_CONFIRM, ConfirmStatus.DB_ERROR):
        h.dose.confirm_results.append(ConfirmOutcome(status))
        h.run(Intent.CONFIRM_TAKEN)
    for status in CancelStatus:
        h.dose.cancel_results.append(CancelOutcome(status))
        h.run(Intent.CANCEL)
    h.run(Intent.HELP)
    h.text("gibberish words")
    h.text("not taken")
    h.dose.awaiting = VITC
    h.text("not taken")
    h.dose.cancel_results.append(CancelOutcome(CancelStatus.CLOSED_GATE))
    h.run(Intent.CANCEL)
    h.a.on_hardware_event(Message(MessageKind.EVENT, Ev.BOOT.value, raw="EVENT BOOT x"))
    h.idle()
    static = [t for t in h.speaker.texts if not any(ch.isdigit() for ch in t) and "Vitamin" not in t]
    assert len(static) >= 20
    assert [t for t in static if t not in phrases.CRITICAL_PHRASES] == []


# =========================================================================== lifecycle


def test_lifecycle(make):
    h = make(start=False)
    results: list[Any] = []
    waiter = threading.Thread(target=lambda: results.append(h.a.submit(Intent.HELP, UI, wait=True, timeout=5)))
    waiter.start()
    assert wait_until(lambda: h.a.state()["pending"] == 1)
    h.a.close()
    waiter.join(timeout=5)
    assert results == [None]  # pending requests resolve when closing
    h.a.close()                # idempotent
    h.a.start()                # a closed assistant stays closed
    assert h.a.state()["running"] is False
    assert h.a.submit(Intent.HELP, UI, wait=True, timeout=1) is None


def test_start_is_idempotent_and_thread_is_named(make):
    h = make()
    h.a.start()
    workers = [t for t in threading.enumerate() if t.name == "assistant" and t.is_alive()]
    assert len(workers) >= 1
    assert h.run(Intent.HELP).text == phrases.HELP
    assert h.speaker.texts.count(phrases.HELP) == 1


def test_cancel_still_interrupts_when_closed(make):
    h = make()
    h.a.close()
    assert h.a.submit(Intent.CANCEL, BUTTON) is None
    assert h.dose.interrupts == [(BUTTON, False)]
