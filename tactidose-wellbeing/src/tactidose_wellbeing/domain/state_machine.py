"""Server-controlled check-in state machine.

The machine mutates a :class:`CheckinSession` and returns an :class:`Outcome`
describing what to say and which events occurred. It performs no I/O; the
application service handles persistence, idempotency and ownership.

State overview::

    awaiting_consent
        -> awaiting_answer                    (consent yes = save, no/skip = session-only)
    awaiting_answer
        -> awaiting_answer_confirmation       (answer only suggested an option)
        -> awaiting_note_offer                (mood/stress/sleep answer confirmed)
        -> awaiting_answer / awaiting_finish  (skip, or support answered)
    awaiting_answer_confirmation
        -> awaiting_note_offer / awaiting_answer
    awaiting_note_offer
        -> awaiting_note_text                 (user says yes)
        -> awaiting_note_confirmation         (user speaks the note directly)
        -> next question                      (no / skip)
    awaiting_note_text
        -> awaiting_note_confirmation | next question (skip)
    awaiting_note_confirmation
        -> next question                      (yes keeps note, no/remove drops it)
        -> awaiting_note_text                 (change)
    awaiting_finish
        -> completed                          (finish / yes)
    any non-terminal
        -> cancelled                          (cancel)
        -> completed                          (finish; unanswered = not_reached)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import parsing
from .questions import QUESTIONS, Question, QuestionId, note_prompt
from .safety import SafetyConfig
from .session import AnswerStatus, CheckinSession, Command, PendingInput, SessionStatus, StorageMode

INTRO = (
    "This is an optional well-being check-in. It is not a medical assessment, and it "
    "does not affect your medications. You can skip any question, or say cancel at any time."
)
CONSENT_PROMPT = (
    "Would you like me to save your answers, including any notes you add to explain them, "
    "so you can review them later? Say yes to save them, or no to keep them for this session only."
)
NOTE_TEXT_PROMPT = "Please tell me in your own words. You can say skip if you change your mind."


@dataclass
class PromptView:
    kind: str
    prompt: str
    options: list[str]
    question_id: QuestionId | None = None
    position: int | None = None
    total: int = len(QUESTIONS)


@dataclass
class Outcome:
    speech: str
    changed: bool = False
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: tuple[str, str] | None = None
    urgent: bool = False
    commit: bool = False
    handoff: bool = False


def summarize(session: CheckinSession) -> str:
    """Factual summary of confirmed answers only. No interpretation or scoring."""
    parts = []
    for q in QUESTIONS:
        slot = session.answers[q.id]
        if slot.status is AnswerStatus.ANSWERED:
            text = f"{q.label}: {slot.value}"
            if slot.note_text:
                text += ", with a note"
        elif slot.status is AnswerStatus.SKIPPED:
            text = f"{q.label}: skipped"
        else:
            text = f"{q.label}: not answered"
        parts.append(text + ".")
    return "Here is what you told me. " + " ".join(parts)


def current_prompt(session: CheckinSession) -> PromptView | None:
    s = session
    q = s.current_question
    pos = s.question_index + 1 if q else None
    match s.status:
        case SessionStatus.AWAITING_CONSENT:
            notice = f" {s.consent_notice}" if s.consent_notice else ""
            return PromptView("consent", f"{INTRO} {CONSENT_PROMPT}{notice}", ["yes", "no"])
        case SessionStatus.AWAITING_ANSWER:
            return PromptView("question", q.prompt, list(q.options), q.id, pos)
        case SessionStatus.AWAITING_ANSWER_CONFIRMATION:
            value = s.pending.candidate_value if s.pending else None
            return PromptView(
                "confirm_answer",
                f"Should I record your {q.label.lower()} as {value}? Please say yes or no.",
                ["yes", "no"],
                q.id,
                pos,
            )
        case SessionStatus.AWAITING_NOTE_OFFER:
            value = s.answers[q.id].value
            return PromptView("note_offer", note_prompt(q.id, value), ["yes", "no"], q.id, pos)
        case SessionStatus.AWAITING_NOTE_TEXT:
            return PromptView("note_text", NOTE_TEXT_PROMPT, [], q.id, pos)
        case SessionStatus.AWAITING_NOTE_CONFIRMATION:
            note = s.pending.note_text if s.pending else ""
            return PromptView(
                "note_confirmation",
                f"Here is your note: “{note}”. Should I keep it with your answer? "
                "Say yes to keep it, change to say it again, or no to leave it out.",
                ["yes", "change", "no"],
                q.id,
                pos,
            )
        case SessionStatus.AWAITING_FINISH:
            if s.storage_mode is StorageMode.SAVE:
                tail = "Say finish to save this check-in, or cancel to discard it."
            else:
                tail = "Say finish to end the check-in. Nothing will be saved."
            return PromptView("finish_review", f"{summarize(s)} {tail}", ["finish", "cancel"])
    return None


def _prompt_text(session: CheckinSession) -> str:
    view = current_prompt(session)
    return view.prompt if view else ""


class CheckinStateMachine:
    def __init__(self, safety: SafetyConfig | None = None) -> None:
        self.safety = safety or SafetyConfig()

    # ------------------------------------------------------------------ entry
    def apply(
        self, s: CheckinSession, command: Command, text: str | None, now: datetime
    ) -> Outcome:
        if s.status.is_terminal:
            return self._error(s, "session_ended", "This check-in has already ended.")

        if command in (Command.ANSWER, Command.ADD_NOTE) and text and self.safety.is_urgent(text):
            # The input is not recorded as an answer or note.
            return Outcome(
                speech=f"{self.safety.urgent_speech()} Your check-in is paused. "
                "You can say cancel to end it, or repeat to continue.",
                urgent=True,
                handoff=True,
                events=[("support.urgent_response_shown", {"handoff": "host_controlled"})],
            )

        if command is Command.ANSWER and text is not None:
            control = parsing.parse_control(text)
            if control:
                command, text = Command(control), None

        if command is Command.REPEAT:
            return Outcome(speech=_prompt_text(s))
        if command is Command.CANCEL:
            return self._cancel(s, now)
        if command is Command.FINISH:
            return self._finish(s, now)

        handler = {
            SessionStatus.AWAITING_CONSENT: self._on_consent,
            SessionStatus.AWAITING_ANSWER: self._on_answer,
            SessionStatus.AWAITING_ANSWER_CONFIRMATION: self._on_answer_confirmation,
            SessionStatus.AWAITING_NOTE_OFFER: self._on_note_offer,
            SessionStatus.AWAITING_NOTE_TEXT: self._on_note_text,
            SessionStatus.AWAITING_NOTE_CONFIRMATION: self._on_note_confirmation,
            SessionStatus.AWAITING_FINISH: self._on_finish_review,
        }[s.status]
        return handler(s, command, text, now)

    # ---------------------------------------------------------------- helpers
    def _error(self, s: CheckinSession, code: str, message: str) -> Outcome:
        prompt = _prompt_text(s)
        return Outcome(speech=f"{message} {prompt}".strip(), error=(code, message))

    def _invalid(self, s: CheckinSession, command: Command) -> Outcome:
        return self._error(
            s, "invalid_action", f"The action '{command.value}' is not available right now."
        )

    def _clarify(self, s: CheckinSession, hint: str) -> Outcome:
        s.unclear_attempts += 1
        return Outcome(speech=f"Sorry, I didn't get a clear answer. {hint}")

    def _goto(self, s: CheckinSession, status: SessionStatus, ack: str = "") -> Outcome:
        s.status = status
        s.unclear_attempts = 0
        return Outcome(speech=f"{ack} {_prompt_text(s)}".strip(), changed=True)

    def _advance(self, s: CheckinSession, ack: str = "", outcome: Outcome | None = None) -> Outcome:
        s.pending = None
        s.question_index += 1
        status = (
            SessionStatus.AWAITING_FINISH
            if s.question_index >= len(QUESTIONS)
            else SessionStatus.AWAITING_ANSWER
        )
        result = self._goto(s, status, ack)
        if outcome:
            result.events = outcome.events + result.events
            result.handoff = outcome.handoff or result.handoff
        return result

    # ---------------------------------------------------------------- consent
    def _on_consent(self, s: CheckinSession, cmd: Command, text: str | None, now: datetime) -> Outcome:
        decision: str | None = None
        if cmd is Command.CONFIRM:
            decision = "yes"
        elif cmd in (Command.REJECT, Command.SKIP):
            decision = "no"
        elif cmd is Command.ANSWER:
            decision = parsing.parse_yes_no(text or "")
            if decision is None:
                return self._clarify(
                    s, "Say yes to save your answers, or no to keep them for this session only."
                )
        else:
            return self._invalid(s, cmd)

        s.question_index = 0
        if decision == "yes":
            s.storage_mode = StorageMode.SAVE
            ack = (
                "Thank you. When you finish, your answers and any notes you add will be "
                "saved so you can review or delete them later."
            )
        else:
            s.storage_mode = StorageMode.SESSION_ONLY
            ack = "Okay. Nothing from this check-in will be saved."
        return self._goto(s, SessionStatus.AWAITING_ANSWER, ack)

    # ---------------------------------------------------------------- answers
    def _on_answer(self, s: CheckinSession, cmd: Command, text: str | None, now: datetime) -> Outcome:
        q = s.current_question
        if cmd is Command.SKIP:
            slot = s.answers[q.id]
            slot.status, slot.value, slot.recorded_at = AnswerStatus.SKIPPED, None, now
            slot.note_text = slot.note_recorded_at = None
            if q.id is QuestionId.SUPPORT:
                s.support_requested = False
            return self._advance(s, "Okay, skipped.")
        if cmd is not Command.ANSWER:
            return self._invalid(s, cmd)

        parsed = parsing.parse_choice(q, text or "")
        if parsed.kind == "exact":
            return self._record(s, q, parsed.value, now)
        if parsed.kind == "candidate":
            s.pending = PendingInput("answer_candidate", q.id, candidate_value=parsed.value)
            return self._goto(s, SessionStatus.AWAITING_ANSWER_CONFIRMATION)
        return self._clarify(
            s, f"{q.prompt} You can also say skip, repeat, or cancel."
        )

    def _record(self, s: CheckinSession, q: Question, value: str, now: datetime) -> Outcome:
        slot = s.answers[q.id]
        slot.status, slot.value, slot.recorded_at = AnswerStatus.ANSWERED, value, now
        slot.note_text = slot.note_recorded_at = None
        s.pending = None

        if q.id is QuestionId.SUPPORT:
            s.support_requested = value == "yes"
            if s.support_requested:
                support = Outcome(
                    speech="",
                    events=[("support.requested", {"handoff": "host_controlled"})],
                    handoff=True,
                )
                ack = (
                    "Thank you. I've noted that you would like support from a person. "
                    f"{self.safety.support_handoff_message}"
                )
                return self._advance(s, ack, support)
            return self._advance(s, "Okay, noted.")

        ack = f"Got it: {q.label.lower()} {value}."
        if q.offers_note:
            return self._goto(s, SessionStatus.AWAITING_NOTE_OFFER, ack)
        return self._advance(s, ack)

    def _on_answer_confirmation(
        self, s: CheckinSession, cmd: Command, text: str | None, now: datetime
    ) -> Outcome:
        q = s.current_question
        if cmd is Command.ANSWER:
            yn = parsing.parse_yes_no(text or "")
            if yn == "yes":
                cmd = Command.CONFIRM
            elif yn == "no":
                cmd = Command.REJECT
            else:
                # Treat as a fresh answer to the same question.
                s.pending = None
                s.status = SessionStatus.AWAITING_ANSWER
                outcome = self._on_answer(s, Command.ANSWER, text, now)
                outcome.changed = True
                return outcome
        if cmd is Command.CONFIRM:
            return self._record(s, q, s.pending.candidate_value, now)
        if cmd is Command.REJECT:
            s.pending = None
            return self._goto(s, SessionStatus.AWAITING_ANSWER, "Okay, let's try again.")
        if cmd is Command.SKIP:
            s.pending = None
            s.status = SessionStatus.AWAITING_ANSWER
            outcome = self._on_answer(s, Command.SKIP, None, now)
            return outcome
        return self._invalid(s, cmd)

    # ------------------------------------------------------------------ notes
    def _draft_note(self, s: CheckinSession, text: str | None) -> Outcome:
        note = parsing.clean_note_text(text or "")
        if not note:
            return self._clarify(s, "Please say your note, or say skip.")
        s.pending = PendingInput("note_draft", s.current_question.id, note_text=note)
        return self._goto(s, SessionStatus.AWAITING_NOTE_CONFIRMATION)

    def _on_note_offer(self, s: CheckinSession, cmd: Command, text: str | None, now: datetime) -> Outcome:
        if cmd is Command.ANSWER:
            yn = parsing.parse_yes_no(text or "")
            if yn == "yes":
                cmd = Command.CONFIRM
            elif yn == "no":
                cmd = Command.REJECT
            else:
                # The user went straight to explaining; it is read back before keeping.
                return self._draft_note(s, text)
        if cmd is Command.ADD_NOTE:
            return self._draft_note(s, text)
        if cmd is Command.CONFIRM:
            return self._goto(s, SessionStatus.AWAITING_NOTE_TEXT)
        if cmd in (Command.REJECT, Command.SKIP, Command.REMOVE_NOTE):
            return self._advance(s, "Okay.")
        return self._invalid(s, cmd)

    def _on_note_text(self, s: CheckinSession, cmd: Command, text: str | None, now: datetime) -> Outcome:
        if cmd in (Command.ANSWER, Command.ADD_NOTE):
            return self._draft_note(s, text)
        if cmd in (Command.SKIP, Command.REMOVE_NOTE, Command.REJECT):
            return self._advance(s, "Okay, no note.")
        return self._invalid(s, cmd)

    def _on_note_confirmation(
        self, s: CheckinSession, cmd: Command, text: str | None, now: datetime
    ) -> Outcome:
        if cmd is Command.ANSWER:
            raw = text or ""
            yn = parsing.parse_yes_no(raw)
            if parsing.is_change_request(raw):
                s.pending = None
                return self._goto(s, SessionStatus.AWAITING_NOTE_TEXT, "Okay.")
            if parsing.is_remove_request(raw) or yn == "no":
                cmd = Command.REMOVE_NOTE
            elif yn == "yes":
                cmd = Command.CONFIRM
            else:
                # Never treat an unclear reply as a replacement note.
                return self._clarify(
                    s, "Say yes to keep the note, change to say it again, or no to leave it out."
                )
        if cmd is Command.ADD_NOTE:
            return self._draft_note(s, text)  # correction: read back again
        if cmd is Command.CONFIRM:
            slot = s.answers[s.current_question.id]
            slot.note_text, slot.note_recorded_at = s.pending.note_text, now
            return self._advance(s, "Your note is kept with your answer.")
        if cmd in (Command.REJECT, Command.REMOVE_NOTE, Command.SKIP):
            return self._advance(s, "Okay, I left the note out.")
        return self._invalid(s, cmd)

    # --------------------------------------------------------------- endings
    def _on_finish_review(
        self, s: CheckinSession, cmd: Command, text: str | None, now: datetime
    ) -> Outcome:
        if cmd is Command.CONFIRM or (
            cmd is Command.ANSWER and parsing.parse_yes_no(text or "") == "yes"
        ):
            return self._finish(s, now)
        if cmd is Command.ANSWER:
            return self._clarify(s, "Say finish to end the check-in, or cancel to discard it.")
        return self._invalid(s, cmd)

    def _finish(self, s: CheckinSession, now: datetime) -> Outcome:
        if s.status is SessionStatus.AWAITING_CONSENT:
            return self._error(s, "invalid_action", "There is nothing to finish yet.")
        dropped_note = s.pending is not None and s.pending.kind == "note_draft"
        s.pending = None
        s.status = SessionStatus.COMPLETED
        s.ended_at = now
        answered = sum(a.status is AnswerStatus.ANSWERED for a in s.answers.values())
        skipped = sum(a.status is AnswerStatus.SKIPPED for a in s.answers.values())
        saved = s.storage_mode is StorageMode.SAVE

        parts = ["Thank you."]
        if dropped_note:
            parts.append("The note you had not confirmed was not kept.")
        if saved:
            parts.append(
                f"Your check-in is saved with {answered} answered "
                f"{'question' if answered == 1 else 'questions'}. "
                "You can ask to hear or delete your saved check-ins at any time."
            )
        else:
            parts.append("Your check-in is finished, and nothing from it has been saved.")
        if s.support_requested:
            parts.append(
                "You asked for support from a person. " + self.safety.support_handoff_message
            )
        return Outcome(
            speech=" ".join(parts),
            changed=True,
            commit=saved,
            handoff=s.support_requested,
            events=[
                (
                    "checkin.completed",
                    {
                        "saved": saved,
                        "answered_count": answered,
                        "skipped_count": skipped,
                        "support_requested": s.support_requested,
                    },
                )
            ],
        )

    def _cancel(self, s: CheckinSession, now: datetime) -> Outcome:
        s.status = SessionStatus.CANCELLED
        s.ended_at = now
        s.pending = None
        speech = "Okay, I've cancelled the check-in. Nothing from it was saved."
        if s.support_requested:
            speech += " Your earlier request for support from a person was already passed to your app."
        return Outcome(
            speech=speech,
            changed=True,
            events=[("checkin.cancelled", {"saved": False})],
        )
