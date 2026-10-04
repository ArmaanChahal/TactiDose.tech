"""Every sentence TactiDose says to the patient (v2), in one place.

Used by the offline rules agent (``agent/rules_agent.py``), the device-side voice loop
(``agent/voice_loop.py``) and the reply checks of the Gemini agent. The Gemini agent words
its own replies, but they must not contradict these deterministic outcomes.

Style rules (ARCHITECTURE v2 §7; users may be blind, have low vision or be older):

* At most three short sentences per reply, in plain words with no lists or symbols. Every
  outcome is said in words ("dropped", "nothing was dropped"), never only by a sound or colour.
* No medical advice: never diagnose, recommend, change doses or suggest extra pills.
  Symptoms -> "contact your doctor"; emergencies -> "call 911 now".
* People hear 1-based container numbers: slot 0 is "container 1".
* Times are spoken in local time ("8:00 AM", "1:30 PM"), with "tomorrow" / "on Wednesday"
  when not today.
* Medication names are spoken without their parenthetical suffix:
  "Vitamin C (demo candy)" -> "Vitamin C".
* :data:`CRITICAL_PHRASES` lists every static sentence (no names, no clock times) so
  ``warm-tts-cache`` can pre-render them for offline use.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Sequence

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- drop outcomes
PILL_DROPPED = "Pill dropped."
COOLDOWN = "It's too soon for another pill. Please wait a little longer."
CONTAINER_EMPTY = "That container is empty. Please ask your caregiver to refill it."
ALREADY_DROPPED = "That dose has already dropped."
DEVICE_UNAVAILABLE = (
    "The dispenser is not ready right now, so nothing was dropped. Please ask your caregiver for help."
)
NEEDS_REVIEW = "Your caregiver needs to check the last drop first, so nothing was dropped."
IN_PROGRESS = "A pill is already dropping. Please wait."
NO_MEDICATION = "That container has no medication set up. Please ask your caregiver."
UNKNOWN_MEDICATION = "I couldn't find that medication in your containers."
NOT_ALLOWED = "I'm not allowed to drop a pill for this account."
DB_UNAVAILABLE = (
    "I can't check your records right now, so nothing was dropped. Please ask your caregiver for help."
)
DROP_FAILED = "The pill did not drop. Please ask your caregiver for help."
NO_PILL = "No pill came out. The container may be empty. Please ask your caregiver to check it."
DROP_UNCERTAIN = (
    "I'm not sure the pill dropped. Please check, and ask your caregiver to look at the dispenser."
)
NOTHING_DROPPED = "Nothing was dropped. Please ask your caregiver for help."

# --------------------------------------------------------------------------- schedule & status
NOTHING_DUE = "Nothing is due right now."
NO_MORE_SCHEDULED = "I don't see another scheduled pill."
MISSED_DOSE = "You missed a scheduled dose."
NO_RECENT_DROPS = "I don't see any pills dropped in the last two weeks."
NO_CONTAINERS = "No containers are set up yet. Please ask your caregiver."
OFFER_DROP = "Would you like me to drop it now?"
CAN_REQUEST_NOW = "You can ask me for a pill now."
NOTHING_TO_CONFIRM = "I don't see a recent pill to mark as taken."
ALREADY_TAKEN = "That pill is already marked as taken."
TAKEN_NOTED = "Thank you. I've noted that you took it."
NOT_MARKED = "Okay. I haven't marked anything as taken."
RECORD_FAILED = "I couldn't record that right now. Please tell your caregiver."

# --------------------------------------------------------------------------- dialogue & safety
HELP = (
    "You can ask me to drop a pill, what is due, when your last pill dropped, or how many pills "
    "are left. You can also press the Drop button."
)
GREETING = "Hello. How can I help you?"
WELCOME = "You're welcome."
NOT_UNDERSTOOD = "Sorry, I didn't understand. You can say: drop my pill, what is due, or help."
UNCLEAR_SPEECH = "Sorry, I didn't catch all of that. Please say it again."
NEGATED = "Okay. I won't drop a pill."
DEFERRED = "Okay, I won't drop it now. Ask me again when you are ready for it."
QUESTION_NO_DROP = 'I won\'t drop a pill for a question. If you want your pill now, say "drop my pill".'
WHICH_PILL = "Which pill would you like?"
NOTHING_TO_REPEAT = "I have nothing to repeat yet."
EMERGENCY = "This could be an emergency. Please call 911 or your local emergency number now."
SYMPTOMS = "I can't give medical advice. Please contact your doctor about how you feel."
SYMPTOMS_NOTE = "For how you feel, please contact your doctor."
MEDICATION_CHANGE = "I can't change your medication or doses. Please talk to your doctor."
INJECTION_REFUSED = "I can't change my rules. I can drop one pill when you ask and the rules allow it."
ONE_PILL_ONLY = "I can only drop one pill at a time. Ask me for one pill if you need it."
AGENT_ERROR = "I can't do that right now. Please use the Drop button or ask your caregiver."

# --------------------------------------------------------------------------- device
STOPPED = "Stopped."
NOT_MOVING = "Okay. The dispenser is not moving."
CANCELLED = "Cancelled."
ASK_FOR_ASSISTANCE = "Please ask your caregiver for help."
DEVICE_RESTARTED = "The dispenser restarted. Please wait while it gets ready."
DEVICE_NEEDS_ATTENTION = "The dispenser needs attention. Please ask your caregiver for help."
NOT_SET_UP = "This dispenser is not set up yet. Please ask your caregiver."

# ---- guided judge demo (tactidose/guided/runner.py); candy, not medicine
DEMO_INTRO = ("Welcome to the CareBridge guided demo. This demo uses candy, not real medicine. "
              "We will go through your morning, noon and night pills.")
DEMO_ASK_TAKE = {
    "morning": "It's time for your morning pill. Do you want to take it?",
    "noon": "It's time for your noon pill. Do you want to take it?",
    "night": "It's time for your night pill. Do you want to take it?",
}
DEMO_REASK_YES_NO = "Sorry, I didn't catch that. Please say yes or no."
DEMO_DECLINED = "Okay, I won't drop it. I've noted that you skipped this one."
DEMO_BUZZER = "I'm turning on the buzzer. Follow the sound to the table and take your pill."
DEMO_ASK_TAKEN = "Did you take the pill?"
DEMO_TAKEN_YES = TAKEN_NOTED
DEMO_TAKEN_NO = "Okay. I've noted that you haven't taken it."
DEMO_NO_DOSE = "I couldn't find this pill in your schedule, so nothing was dropped."
DEMO_ASK_CHECKIN = "How has your day been? How are you feeling? Any problems?"
DEMO_CHECKIN_THANKS = "Thank you for telling me. I've noted it for your care team."
DEMO_CHECKIN_NONE = "Okay, no answer this time."
DEMO_ALERT_SENT = "I've let your care team know. The demo has stopped."
DEMO_NEXT = "Next pill coming up."
DEMO_GOODBYE = "That's the end of the demo. Thank you. Here is your summary."
DEMO_STOPPED = "The demo was stopped."

#: Every static sentence the system may say verbatim (no names, no clock times). Pre-rendered
#: by ``warm-tts-cache`` so the core interaction stays understandable offline.
CRITICAL_PHRASES: list[str] = [
    DEMO_INTRO,
    *DEMO_ASK_TAKE.values(),
    DEMO_REASK_YES_NO,
    DEMO_DECLINED,
    DEMO_BUZZER,
    DEMO_ASK_TAKEN,
    DEMO_TAKEN_NO,
    DEMO_NO_DOSE,
    DEMO_ASK_CHECKIN,
    DEMO_CHECKIN_THANKS,
    DEMO_CHECKIN_NONE,
    DEMO_ALERT_SENT,
    DEMO_NEXT,
    DEMO_GOODBYE,
    DEMO_STOPPED,
    PILL_DROPPED,
    COOLDOWN,
    CONTAINER_EMPTY,
    ALREADY_DROPPED,
    DEVICE_UNAVAILABLE,
    NEEDS_REVIEW,
    IN_PROGRESS,
    NO_MEDICATION,
    UNKNOWN_MEDICATION,
    NOT_ALLOWED,
    DB_UNAVAILABLE,
    DROP_FAILED,
    NO_PILL,
    DROP_UNCERTAIN,
    NOTHING_DROPPED,
    NOTHING_DUE,
    NO_MORE_SCHEDULED,
    MISSED_DOSE,
    NO_RECENT_DROPS,
    NO_CONTAINERS,
    OFFER_DROP,
    CAN_REQUEST_NOW,
    NOTHING_TO_CONFIRM,
    ALREADY_TAKEN,
    TAKEN_NOTED,
    NOT_MARKED,
    RECORD_FAILED,
    HELP,
    GREETING,
    WELCOME,
    NOT_UNDERSTOOD,
    UNCLEAR_SPEECH,
    NEGATED,
    DEFERRED,
    QUESTION_NO_DROP,
    WHICH_PILL,
    NOTHING_TO_REPEAT,
    EMERGENCY,
    SYMPTOMS,
    SYMPTOMS_NOTE,
    MEDICATION_CHANGE,
    INJECTION_REFUSED,
    ONE_PILL_ONLY,
    AGENT_ERROR,
    STOPPED,
    NOT_MOVING,
    CANCELLED,
    ASK_FOR_ASSISTANCE,
    DEVICE_RESTARTED,
    DEVICE_NEEDS_ATTENTION,
    NOT_SET_UP,
]

_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MAX_NAME_CHARS = 60
_PARENTHETICAL = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


# --------------------------------------------------------------------------- small helpers


def spoken_time(dt: datetime) -> str:
    """``08:00`` -> ``"8:00 AM"``, ``13:30`` -> ``"1:30 PM"``, ``00:05`` -> ``"12:05 AM"``."""
    hour12 = dt.hour % 12 or 12
    return f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def count_words(n: int) -> str:
    """Small counts as words, which TTS engines read more naturally ("two pills")."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def plural(n: int, word: str) -> str:
    """``plural(1, "pill") == "1 pill"``, ``plural(12, "pill") == "12 pills"``."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def relative_day(when: datetime, now_local: datetime | None) -> str:
    """``"today"``, ``"tomorrow"``, ``"yesterday"``, ``"on Wednesday"`` or
    ``"on Wednesday October 21"`` (``"today"`` when ``now_local`` is unknown)."""
    if now_local is None:
        return "today"
    today: date = now_local.date()
    delta = (when.date() - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if -7 < delta < 7:
        return f"on {_WEEKDAYS[when.weekday()]}"
    return f"on {_WEEKDAYS[when.weekday()]} {when.strftime('%B')} {when.day}"


def when_phrase(when: datetime, now_local: datetime | None, *, say_today: bool = False) -> str:
    """``"at 8:00 AM"`` today (``"today at 8:00 AM"`` with ``say_today``), otherwise
    ``"tomorrow at 8:00 AM"`` / ``"on Wednesday at 8:00 AM"``."""
    day = relative_day(when, now_local)
    at = f"at {spoken_time(when)}"
    if day == "today" and not say_today:
        return at
    return f"{day} {at}"


def duration_phrase(seconds: float) -> str:
    """Remaining time, rounded up to whole minutes: ``"less than a minute"``, ``"1 minute"``,
    ``"45 minutes"``, ``"1 hour"``, ``"2 hours and 5 minutes"``."""
    total = max(0, int(round(float(seconds))))
    if total < 60:
        return "less than a minute"
    minutes = -(-total // 60)
    hours, rest = divmod(minutes, 60)
    if hours == 0:
        return plural(minutes, "minute")
    if rest == 0:
        return plural(hours, "hour")
    return f"{plural(hours, 'hour')} and {plural(rest, 'minute')}"


def short_med_name(name: str | None) -> str:
    """Speech-friendly medication name: parenthetical suffixes removed, whitespace collapsed,
    length capped. ``"Vitamin C (demo candy)" -> "Vitamin C"``; empty -> ``"your medication"``."""
    raw = " ".join(str(name or "").split())
    short = " ".join(_PARENTHETICAL.sub("", raw).split()) or raw
    if len(short) > _MAX_NAME_CHARS:
        short = short[:_MAX_NAME_CHARS].rsplit(" ", 1)[0] or short[:_MAX_NAME_CHARS]
    return short or "your medication"


def container_label(number: int) -> str:
    """``container_label(2) == "container 2"`` (1-based, as people hear it)."""
    return f"container {int(number)}"


def join(*parts: str | None) -> str:
    """Join sentences with single spaces, skipping empty parts."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _options(options: Sequence[tuple[int, str]], conjunction: str = "or") -> str:
    items = [f"{short_med_name(name)} in {container_label(number)}" for number, name in options]
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return ", ".join(items[:-1]) + f", {conjunction} {items[-1]}"


# --------------------------------------------------------------------------- dynamic sentences


def pill_dropped(name: str | None, container_number: int | None, *,
                 pill_count_after: int | None = None, low_stock_at: int = 3) -> str:
    """``"Vitamin C dropped from container 1."`` (+ a low-stock / last-pill note)."""
    med = short_med_name(name) if name else "Your pill"
    if container_number is None:
        head = f"{_cap(med)} dropped."
    else:
        head = f"{_cap(med)} dropped from {container_label(container_number)}."
    note = ""
    if container_number is not None and pill_count_after is not None:
        if pill_count_after <= 0:
            note = f"That was the last pill in {container_label(container_number)}."
        elif pill_count_after <= low_stock_at:
            note = f"{_cap(container_label(container_number))} has {plural(pill_count_after, 'pill')} left."
    return join(head, note)


def cooldown(next_allowed_local: datetime | None, now_local: datetime | None,
             remaining_s: float | None = None) -> str:
    """Global cooldown refusal with the spoken time of the next allowed drop:
    ``"It's too soon for another pill. The next pill can drop at 9:00 AM, in 45 minutes."``"""
    if next_allowed_local is None:
        return COOLDOWN
    when = when_phrase(next_allowed_local, now_local)
    head = "It's too soon for another pill."
    if remaining_s is not None and remaining_s > 0:
        return f"{head} The next pill can drop {when}, in {duration_phrase(remaining_s)}."
    return f"{head} The next pill can drop {when}."


def container_empty(container_number: int | None) -> str:
    if container_number is None:
        return CONTAINER_EMPTY
    return f"{_cap(container_label(container_number))} is empty. Please ask your caregiver to refill it."


def no_pill(container_number: int | None) -> str:
    """``ERR NO_PILL``: the drop sensor saw nothing pass."""
    if container_number is None:
        return NO_PILL
    return (f"No pill came out of {container_label(container_number)}. It may be empty. "
            "Please ask your caregiver to check it.")


def no_medication(container_number: int | None) -> str:
    if container_number is None:
        return NO_MEDICATION
    return f"{_cap(container_label(container_number))} has no medication set up. Please ask your caregiver."


def no_such_container(number: int, num_slots: int) -> str:
    return f"There is no {container_label(number)}. Your containers are numbered 1 to {num_slots}."


def already_dropped(name: str | None = None, scheduled_local: datetime | None = None,
                    now_local: datetime | None = None) -> str:
    """Scheduled dose already satisfied: ``"Your 8:00 AM Vitamin C has already dropped."``"""
    if not name:
        return ALREADY_DROPPED
    if scheduled_local is None:
        return f"Your {short_med_name(name)} has already dropped."
    return f"Your {spoken_time(scheduled_local)} {short_med_name(name)} has already dropped."


def due_now(name: str | None, scheduled_local: datetime, now_local: datetime, *,
            auto_drop: bool, offer: bool) -> str:
    """A dose whose window is open. Mentions the automatic drop when it is still ahead and
    offers a drop now when the cooldown allows it."""
    med = short_med_name(name)
    if scheduled_local > now_local:
        head = f"Your {med} is due at {spoken_time(scheduled_local)}."
        mid = "It will drop by itself then." if auto_drop else ""
    else:
        head = f"Your {spoken_time(scheduled_local)} {med} is due now."
        mid = ""
    return join(head, mid, OFFER_DROP if offer else "")


def next_pill(name: str | None, scheduled_local: datetime, now_local: datetime | None) -> str:
    """``"Your next scheduled pill is Calcium at 1:00 PM."`` / ``"... tomorrow at 8:00 AM."``"""
    return f"Your next scheduled pill is {short_med_name(name)} {when_phrase(scheduled_local, now_local)}."


def last_pill(name: str | None, dropped_local: datetime, now_local: datetime | None) -> str:
    """``"Your last pill was Vitamin C, today at 8:00 AM."``"""
    return (f"Your last pill was {short_med_name(name)}, "
            f"{when_phrase(dropped_local, now_local, say_today=True)}.")


def last_drop_unconfirmed(name: str | None, dropped_local: datetime | None,
                          now_local: datetime | None) -> str:
    """An UNCERTAIN last drop: ``"I'm not sure your last pill, Vitamin C today at 8:00 AM,
    dropped. Your caregiver needs to check it."``"""
    when = f" {when_phrase(dropped_local, now_local, say_today=True)}" if dropped_local else ""
    return (f"I'm not sure your last pill, {short_med_name(name)}{when}, dropped. "
            "Your caregiver needs to check it.")


def missed_dose(name: str | None, scheduled_local: datetime, now_local: datetime | None) -> str:
    """``"You missed your 8:00 AM Vitamin C."`` (with the day when it was not today)."""
    day = relative_day(scheduled_local, now_local)
    day_part = "" if day == "today" else f" {day}"
    return f"You missed your {spoken_time(scheduled_local)} {short_med_name(name)}{day_part}."


def container_summary(number: int, name: str | None, count: int, *, low_stock: bool) -> str:
    """``"Container 2, Calcium: 2 pills left, running low."`` / ``"Container 3, Omega-3: empty."``"""
    label = _cap(container_label(number))
    if not name:
        return f"{label} has no medication set up."
    med = short_med_name(name)
    if count <= 0:
        return f"{label}, {med}: empty."
    tail = ", running low" if low_stock else ""
    return f"{label}, {med}: {plural(count, 'pill')} left{tail}."


def which_pill(options: Sequence[tuple[int, str]]) -> str:
    """``"Which pill would you like? Vitamin C in container 1, or Calcium in container 2."``"""
    if not options:
        return WHICH_PILL
    return f"{WHICH_PILL} {_cap(_options(options))}."


def medication_list(options: Sequence[tuple[int, str]]) -> str:
    """``"You have Vitamin C in container 1, and Calcium in container 2."`` (for refusals)."""
    if not options:
        return NO_CONTAINERS
    return f"You have {_options(options, 'and')}."


def container_holds(number: int, name: str | None) -> str:
    """``"Container 2 holds Calcium."``"""
    return f"{_cap(container_label(number))} holds {short_med_name(name)}."


def taken_noted(name: str | None) -> str:
    if not name:
        return TAKEN_NOTED
    return f"Thank you. I've noted that you took your {short_med_name(name)}."
