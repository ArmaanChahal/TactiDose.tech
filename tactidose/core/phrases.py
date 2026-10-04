"""Every sentence TactiDose speaks, in one place.

Rules (handoff §3, §4, §17, §33; ARCHITECTURE §6):

* Short sentences that end by telling the user what to do next.
* No dosage advice, ever. Confirmed label instructions are only read verbatim, prefixed
  with "The label says:".
* People hear 1-based compartment numbers: slot 2 is "compartment 3"
  (``protocol.compartment_label``).
* Times are spoken in the dose's local time ("8:00 AM", "1:30 PM").
* With ``tts_include_med_names=False`` no medication names or label text are spoken. Those
  would be sent to the cloud TTS, so generic wording is used instead ("your 8:00 AM
  medication").
* Handoff-mandated wording is kept verbatim (see the constants marked *mandated*).
* :data:`CRITICAL_PHRASES` lists every static sentence (no names, no times) the assistant
  can say, so ``warm-tts-cache`` can pre-render them for offline use.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from tactidose.core.interfaces import BlockReason, DoseInfo
from tactidose.hardware.protocol import compartment_label

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- mandated wording
#: Handoff §4.2 / ARCHITECTURE §6 (duplicate request, no motor command).
ALREADY_ACCESSED = "That scheduled dose has already been accessed."
#: Handoff §15 (dispense command failed).
COULD_NOT_PREPARE = "I could not prepare the compartment. Please ask for assistance."
CANCELLED = "Cancelled."
ASK_FOR_ASSISTANCE = "Please ask for assistance."
NETWORK_UNAVAILABLE = "Network unavailable."
HARDWARE_ERROR = "Hardware error."
NOTHING_DUE = "You do not have a scheduled medication due right now."
#: Handoff §15/§17. Kept for other modules; the assistant names the dose instead.
MEDICATION_READY = "Your scheduled medication is ready."
#: Handoff §17 (label onboarding, spoken by the UI/onboarding flow).
LABEL_NEEDS_REVIEW = "I detected a new medication label. Please review it before saving."
#: ARCHITECTURE §6 (host gate timer closed the gate).
GATE_CLOSED_TIMEOUT = "I've closed the compartment. If you took your dose, say 'taken'."

# --------------------------------------------------------------------------- prompts / building blocks
PREPARING = "Preparing your dose. Please keep your hands clear of the opening."
SAY_DISPENSE = "Say 'dispense' or press the big button."
#: Used when the big button would confirm an open dose instead of dispensing.
SAY_DISPENSE_WHEN_READY = "Say 'dispense' when you are ready."
SAY_TAKEN = "When you have taken it, say 'taken' or press the big button."
TAKEN_REMINDER = "If you took your dose, say 'taken'."

# --------------------------------------------------------------------------- dispense outcomes
DISPENSE_CANCELLED = "Cancelled. Nothing was dispensed. Say 'dispense' when you are ready."
HARDWARE_UNAVAILABLE = "Hardware error. Nothing was dispensed. Please ask for assistance."
DB_UNAVAILABLE = "I can't check your schedule right now. Please ask for assistance."
IN_PROGRESS = "Your dose is already being prepared. Please wait."
NEEDS_REVIEW = "This dose needs to be checked by a caregiver. Please ask for assistance."
NO_COMPARTMENT = "This medication is not assigned to a compartment. Please ask for assistance."
UNCONFIRMED_MEDICATION = (
    "This medication has not been confirmed by a caregiver. Please ask for assistance."
)
INACTIVE = "This medication is not active. Please ask for assistance."
TOO_SOON = "That medication was opened a short time ago, so I can't open it again yet."

# --------------------------------------------------------------------------- confirm outcomes
NOTHING_TO_CONFIRM = "There is no open dose to confirm right now."
ALREADY_CONFIRMED = "That dose is already recorded as taken."
CONFIRM_DB_ERROR = "I could not record that right now. Please ask for assistance."
GATE_CLOSE_FAILED = "I could not close the compartment. Please ask for assistance."

# --------------------------------------------------------------------------- cancel outcomes
CANCEL_STOPPED = "Cancelled. The carousel has stopped."
CANCEL_CLOSED_GATE = "Cancelled. I've closed the compartment."
CANCEL_CLOSED_GATE_REMINDER = (
    "Cancelled. I've closed the compartment. If you took your dose, say 'taken'."
)
CANCELLED_REMINDER = "Cancelled. If you took your dose, say 'taken'."
CANCEL_FAILED = (
    "I could not stop the device. Please keep your hands clear and ask for assistance."
)

# --------------------------------------------------------------------------- gate timer
GATE_CLOSED = "I've closed the compartment."

# --------------------------------------------------------------------------- dialogue
NOT_UNDERSTOOD = "Sorry, I didn't catch that. Say 'help' to hear what you can say."
NEGATED = "Okay. I have not changed anything."
NEGATED_REMINDER = (
    "Okay. I have not changed anything. When you have taken your dose, say 'taken'."
)
HELP = (
    "You can say: what do I take now, dispense, taken, repeat, cancel, or help. "
    "You can also press the big button."
)
NOTHING_TO_REPEAT = "I have nothing to repeat yet. Say 'help' to hear what you can say."

# --------------------------------------------------------------------------- device notices
DEVICE_RESTARTED = "The device restarted. Please keep your hands clear while it gets ready."
DEVICE_NEEDS_ATTENTION = "The device needs attention. Please ask for assistance."

#: Spoken when a handler fails unexpectedly (fail closed).
ERROR_GENERIC = ASK_FOR_ASSISTANCE

#: Every static sentence the system may say verbatim (no names, no times). Pre-rendered by
#: ``warm-tts-cache`` so the core interaction stays understandable offline (handoff §17).
CRITICAL_PHRASES: list[str] = [
    ALREADY_ACCESSED,
    COULD_NOT_PREPARE,
    CANCELLED,
    ASK_FOR_ASSISTANCE,
    NETWORK_UNAVAILABLE,
    HARDWARE_ERROR,
    NOTHING_DUE,
    MEDICATION_READY,
    LABEL_NEEDS_REVIEW,
    GATE_CLOSED_TIMEOUT,
    PREPARING,
    DISPENSE_CANCELLED,
    HARDWARE_UNAVAILABLE,
    DB_UNAVAILABLE,
    IN_PROGRESS,
    NEEDS_REVIEW,
    NO_COMPARTMENT,
    UNCONFIRMED_MEDICATION,
    INACTIVE,
    TOO_SOON,
    NOTHING_TO_CONFIRM,
    ALREADY_CONFIRMED,
    CONFIRM_DB_ERROR,
    GATE_CLOSE_FAILED,
    CANCEL_STOPPED,
    CANCEL_CLOSED_GATE,
    CANCEL_CLOSED_GATE_REMINDER,
    CANCELLED_REMINDER,
    CANCEL_FAILED,
    GATE_CLOSED,
    TAKEN_REMINDER,
    NOT_UNDERSTOOD,
    NEGATED,
    NEGATED_REMINDER,
    HELP,
    NOTHING_TO_REPEAT,
    DEVICE_RESTARTED,
    DEVICE_NEEDS_ATTENTION,
]

_BLOCKED: dict[BlockReason, str] = {
    BlockReason.NEEDS_REVIEW: NEEDS_REVIEW,
    BlockReason.NO_COMPARTMENT: NO_COMPARTMENT,
    BlockReason.UNCONFIRMED_MEDICATION: UNCONFIRMED_MEDICATION,
    BlockReason.INACTIVE: INACTIVE,
    BlockReason.TOO_SOON: TOO_SOON,
    BlockReason.IN_PROGRESS: IN_PROGRESS,
}

_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MAX_NAME_CHARS = 80
_MAX_LABEL_CHARS = 300


# --------------------------------------------------------------------------- small helpers


def spoken_time(dt: datetime) -> str:
    """``08:00`` -> ``"8:00 AM"``, ``13:30`` -> ``"1:30 PM"``, ``00:05`` -> ``"12:05 AM"``."""
    hour12 = dt.hour % 12 or 12
    return f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def count_words(n: int) -> str:
    """Small counts as words, which TTS engines read more naturally ("two doses")."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _plural(n: int, word: str) -> str:
    return f"{count_words(n)} {word}{'' if n == 1 else 's'}"


def _clean(text: str | None, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0]
    return cut or cleaned[:limit]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def med_name(dose: DoseInfo) -> str:
    """The confirmed medication name, whitespace-normalised and length-capped for speech."""
    return _clean(dose.medication_name, _MAX_NAME_CHARS) or "medication"


def dose_ref(dose: DoseInfo, *, include_names: bool, with_time: bool = True) -> str:
    """Lower-case reference to a dose: "your 8:00 AM Vitamin C" / "your 8:00 AM medication".

    Generic wording (``include_names=False``) always carries the time. Without the name or
    the time, a listener cannot tell doses apart.
    """
    when = spoken_time(dose.scheduled_local)
    if not include_names:
        return f"your {when} medication"
    return f"your {when} {med_name(dose)}" if with_time else f"your {med_name(dose)}"


def compartment(dose: DoseInfo) -> str | None:
    """``"compartment 3"`` for slot 2 (``None`` when the dose has no slot)."""
    return compartment_label(dose.slot) if dose.slot is not None else None


def label_says(instructions: str | None) -> str:
    """Verbatim confirmed instructions: ``"The label says: Take one piece."`` (or "")."""
    text = _clean(instructions, _MAX_LABEL_CHARS)
    if not text:
        return ""
    truncated = len(" ".join((instructions or "").split())) > len(text)
    out = f"The label says: {_sentence(text)}"
    return out + " The label text continues." if truncated else out


def day_phrase(when: datetime, now_local: datetime | None) -> str:
    """"" for today (or when ``now_local`` is unknown), "tomorrow", or "on Wednesday"."""
    if now_local is None:
        return ""
    today: date = now_local.date()
    delta = (when.date() - today).days
    if delta == 0:
        return ""
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return f"on {_WEEKDAYS[when.weekday()]}"
    return f"on {_WEEKDAYS[when.weekday()]} {when.strftime('%B')} {when.day}"


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


# --------------------------------------------------------------------------- dynamic sentences


def next_dose(dose: DoseInfo | None, *, include_names: bool, now_local: datetime | None) -> str:
    """"Your next dose is Calcium at 1:00 PM." / "Your next medication is tomorrow at 8:00 AM."."""
    if dose is None:
        return ""
    when = _join(day_phrase(dose.scheduled_local, now_local), f"at {spoken_time(dose.scheduled_local)}")
    if include_names:
        return f"Your next dose is {med_name(dose)} {when}."
    return f"Your next medication is {when}."


def nothing_due(next_up: DoseInfo | None = None, *, include_names: bool,
                now_local: datetime | None = None) -> str:
    return _join(NOTHING_DUE, next_dose(next_up, include_names=include_names, now_local=now_local))


def already_accessed(next_up: DoseInfo | None = None, *, include_names: bool,
                     now_local: datetime | None = None) -> str:
    return _join(ALREADY_ACCESSED, next_dose(next_up, include_names=include_names, now_local=now_local))


def already_taken(dose: DoseInfo, *, include_names: bool, next_up: DoseInfo | None = None,
                  now_local: datetime | None = None) -> str:
    """CHECK_DUE when the in-window dose was already confirmed."""
    return _join(
        f"{_cap(dose_ref(dose, include_names=include_names))} is already recorded as taken.",
        next_dose(next_up, include_names=include_names, now_local=now_local),
    )


def due_now(dose: DoseInfo, *, count: int = 1, include_names: bool,
            button_dispenses: bool = True) -> str:
    """CHECK_DUE consent prompt: announce the dose and ask for 'dispense'."""
    ref = dose_ref(dose, include_names=include_names)
    if count > 1:
        head = f"You have {_plural(count, 'dose')} due. The first is {ref}."
    else:
        head = f"{_cap(ref)} is due now."
    return _join(head, SAY_DISPENSE if button_dispenses else SAY_DISPENSE_WHEN_READY)


def dose_ready(dose: DoseInfo, *, include_names: bool) -> str:
    """After ``OK GATE_OPEN``: where the dose is, the verbatim label, and how to confirm."""
    where = compartment(dose)
    ref = _cap(dose_ref(dose, include_names=include_names, with_time=False))
    head = f"{ref} is ready in {where}." if where else f"{ref} is ready."
    label = label_says(dose.instructions) if include_names else ""
    return _join(head, label, SAY_TAKEN)


def awaiting_confirmation(dose: DoseInfo, *, include_names: bool, more_due: int = 0) -> str:
    """CHECK_DUE while a dispensed dose still waits for "taken"."""
    ref = dose_ref(dose, include_names=include_names)
    where = compartment(dose)
    head = f"{_cap(where)} was opened for {ref}." if where else f"{_cap(ref)} was opened."
    tail = ""
    if more_due > 0:
        tail = f"You also have {_plural(more_due, 'more dose')} due after that."
    return _join(head, SAY_TAKEN, tail)


def confirmed(dose: DoseInfo | None, *, include_names: bool, gate_closed: bool | None = None,
              more_due: int = 0) -> str:
    """"Thank you. Your Vitamin C is recorded as taken." (+ gate problem / what next)."""
    if dose is None:
        head = "Thank you. Your dose is recorded as taken."
    else:
        ref = dose_ref(dose, include_names=include_names, with_time=False)
        head = f"Thank you. {_cap(ref)} is recorded as taken."
    gate = GATE_CLOSE_FAILED if gate_closed is False else ""
    nxt = ""
    if more_due > 0:
        nxt = f"You have {_plural(more_due, 'more dose')} due. {SAY_DISPENSE_WHEN_READY}"
    return _join(head, gate, nxt)


def already_confirmed(dose: DoseInfo | None, *, include_names: bool) -> str:
    if dose is None:
        return ALREADY_CONFIRMED
    return f"{_cap(dose_ref(dose, include_names=include_names))} is already recorded as taken."


def blocked(reason: BlockReason | str | None) -> str:
    """Static refusal sentence for a :class:`BlockReason` (unknown -> ask for assistance)."""
    try:
        key = reason if isinstance(reason, BlockReason) else BlockReason(str(reason))
    except ValueError:
        return ASK_FOR_ASSISTANCE
    return _BLOCKED.get(key, ASK_FOR_ASSISTANCE)
