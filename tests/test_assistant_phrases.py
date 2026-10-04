"""Spoken sentences, v2 (tactidose/core/phrases.py)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tactidose.core import phrases
from tests.conftest import TEST_TZ

TZ = ZoneInfo(TEST_TZ)
NOW = datetime(2026, 10, 5, 7, 55, tzinfo=TZ)   # Monday


def at(hour: int, minute: int = 0, day: int = 5) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(8, 0, "8:00 AM"), (13, 30, "1:30 PM"), (0, 5, "12:05 AM"), (12, 0, "12:00 PM"), (23, 59, "11:59 PM")],
)
def test_spoken_time(hour, minute, expected):
    assert phrases.spoken_time(datetime(2026, 1, 1, hour, minute)) == expected


def test_relative_day_and_when_phrase():
    assert phrases.relative_day(at(9), NOW) == "today"
    assert phrases.relative_day(at(9, day=6), NOW) == "tomorrow"
    assert phrases.relative_day(at(20, day=4), NOW) == "yesterday"
    assert phrases.relative_day(at(8, day=8), NOW) == "on Thursday"
    assert phrases.relative_day(at(8, day=20), NOW) == "on Tuesday October 20"
    assert phrases.relative_day(at(8, day=20), None) == "today"
    assert phrases.when_phrase(at(9, 5), NOW) == "at 9:05 AM"
    assert phrases.when_phrase(at(9, 5), NOW, say_today=True) == "today at 9:05 AM"
    assert phrases.when_phrase(at(8, day=6), NOW) == "tomorrow at 8:00 AM"


@pytest.mark.parametrize("seconds,expected", [
    (0, "less than a minute"), (59, "less than a minute"), (60, "1 minute"), (61, "2 minutes"),
    (2700, "45 minutes"), (3600, "1 hour"), (3900, "1 hour and 5 minutes"), (7260, "2 hours and 1 minute"),
])
def test_duration_phrase(seconds, expected):
    assert phrases.duration_phrase(seconds) == expected


def test_short_med_name():
    assert phrases.short_med_name("Vitamin C (demo candy)") == "Vitamin C"
    assert phrases.short_med_name("  Omega-3   (demo candy) ") == "Omega-3"
    assert phrases.short_med_name("(demo)") == "(demo)"
    assert phrases.short_med_name("") == "your medication" and phrases.short_med_name(None) == "your medication"
    assert len(phrases.short_med_name("x " * 100)) <= 60


def test_pill_dropped_with_stock_notes():
    assert phrases.pill_dropped("Vitamin C (demo candy)", 1) == "Vitamin C dropped from container 1."
    assert phrases.pill_dropped("Calcium", 2, pill_count_after=2) == (
        "Calcium dropped from container 2. Container 2 has 2 pills left.")
    assert phrases.pill_dropped("Calcium", 2, pill_count_after=1).endswith("Container 2 has 1 pill left.")
    assert phrases.pill_dropped("Calcium", 2, pill_count_after=0).endswith("That was the last pill in container 2.")
    assert phrases.pill_dropped("Calcium", 2, pill_count_after=12) == "Calcium dropped from container 2."
    assert phrases.pill_dropped(None, None) == "Your pill dropped."


def test_cooldown_speaks_the_next_time():
    assert phrases.cooldown(at(9, 0), NOW, 3900) == (
        "It's too soon for another pill. The next pill can drop at 9:00 AM, in 1 hour and 5 minutes.")
    assert phrases.cooldown(at(7, 0, day=6), NOW, None) == (
        "It's too soon for another pill. The next pill can drop tomorrow at 7:00 AM.")
    assert phrases.cooldown(None, NOW, 100) == phrases.COOLDOWN


def test_refusal_sentences():
    assert phrases.container_empty(3) == "Container 3 is empty. Please ask your caregiver to refill it."
    assert phrases.container_empty(None) == phrases.CONTAINER_EMPTY
    assert phrases.no_pill(1).startswith("No pill came out of container 1.")
    assert phrases.no_medication(2) == "Container 2 has no medication set up. Please ask your caregiver."
    assert phrases.no_such_container(5, 3) == "There is no container 5. Your containers are numbered 1 to 3."
    assert phrases.already_dropped("Calcium (demo token)") == "Your Calcium has already dropped."
    assert phrases.already_dropped("Calcium", at(13)) == "Your 1:00 PM Calcium has already dropped."
    assert phrases.already_dropped() == phrases.ALREADY_DROPPED


def test_schedule_sentences():
    assert phrases.due_now("Vitamin C", at(8), NOW, auto_drop=True, offer=True) == (
        "Your Vitamin C is due at 8:00 AM. It will drop by itself then. Would you like me to drop it now?")
    assert phrases.due_now("Vitamin C", at(7, 30), NOW, auto_drop=True, offer=False) == (
        "Your 7:30 AM Vitamin C is due now.")
    assert phrases.next_pill("Calcium", at(13), NOW) == "Your next scheduled pill is Calcium at 1:00 PM."
    assert phrases.next_pill("Calcium", at(13, day=6), NOW).endswith("tomorrow at 1:00 PM.")
    assert phrases.last_pill("Vitamin C", at(7), NOW) == "Your last pill was Vitamin C, today at 7:00 AM."
    assert phrases.missed_dose("Vitamin C", at(8), NOW) == "You missed your 8:00 AM Vitamin C."
    assert phrases.missed_dose("Vitamin C", at(20, day=4), NOW) == "You missed your 8:00 PM Vitamin C yesterday."
    assert phrases.last_drop_unconfirmed("Calcium", at(7, 50), NOW) == (
        "I'm not sure your last pill, Calcium today at 7:50 AM, dropped. Your caregiver needs to check it.")


def test_container_sentences():
    assert phrases.container_summary(2, "Calcium", 2, low_stock=True) == "Container 2, Calcium: 2 pills left, running low."
    assert phrases.container_summary(3, "Omega-3", 0, low_stock=False) == "Container 3, Omega-3: empty."
    assert phrases.container_summary(1, None, 0, low_stock=False) == "Container 1 has no medication set up."
    opts = [(1, "Vitamin C (demo candy)"), (2, "Calcium (demo token)"), (3, "Omega-3 (demo candy)")]
    assert phrases.which_pill(opts) == (
        "Which pill would you like? Vitamin C in container 1, Calcium in container 2, or Omega-3 in container 3.")
    assert phrases.which_pill(opts[:2]) == "Which pill would you like? Vitamin C in container 1 or Calcium in container 2."
    assert phrases.which_pill([]) == phrases.WHICH_PILL
    assert phrases.medication_list(opts[:2]) == "You have Vitamin C in container 1 and Calcium in container 2."
    assert phrases.medication_list([]) == phrases.NO_CONTAINERS
    assert phrases.container_holds(2, "Calcium (demo token)") == "Container 2 holds Calcium."
    assert phrases.taken_noted("Vitamin C (demo candy)") == "Thank you. I've noted that you took your Vitamin C."


def test_safety_wording():
    assert "911" in phrases.EMERGENCY and phrases.EMERGENCY.endswith("now.")
    assert "doctor" in phrases.SYMPTOMS and "doctor" in phrases.MEDICATION_CHANGE
    assert "Drop button" in phrases.AGENT_ERROR and "caregiver" in phrases.AGENT_ERROR
    assert "nothing was dropped" in phrases.DEVICE_UNAVAILABLE and "not sure" in phrases.DROP_UNCERTAIN


def test_critical_phrases_are_static_unique_and_complete():
    crit = phrases.CRITICAL_PHRASES
    assert len(crit) == len(set(crit)) >= 40
    for text in crit:
        assert text == " ".join(text.split()) and text[-1] in ".!?", text
        assert not re.search(r"\d{1,2}:\d{2}", text), text            # no clock times
        assert not re.search(r"\d", text.replace("911", "")), text    # no numbers except 911
        assert "Vitamin" not in text and "Calcium" not in text
    constants = {name: value for name, value in vars(phrases).items() if name.isupper() and isinstance(value, str)}
    assert [name for name, value in constants.items() if value not in crit] == []


def test_replies_are_short():
    samples = list(phrases.CRITICAL_PHRASES) + [
        phrases.cooldown(at(9), NOW, 3600),
        phrases.due_now("Vitamin C", at(8), NOW, auto_drop=True, offer=True),
        phrases.pill_dropped("Calcium", 2, pill_count_after=1),
    ]
    for text in samples:
        assert len(re.findall(r"[.!?](?:\s|$)", text)) <= 3, text


def test_no_dosage_advice():
    advice = re.compile(r"\b(mg|milligram|dosage|you should take|take two|take another|double|extra pill)\b", re.I)
    samples = list(phrases.CRITICAL_PHRASES) + [
        phrases.pill_dropped("Vitamin C", 1, pill_count_after=2),
        phrases.due_now("Vitamin C", at(8), NOW, auto_drop=False, offer=True),
        phrases.cooldown(at(9), NOW, 60),
    ]
    for text in samples:
        assert not advice.search(text), text


def test_count_words_and_plural():
    assert [phrases.count_words(n) for n in (0, 1, 2, 12, 13)] == ["zero", "one", "two", "twelve", "13"]
    assert phrases.plural(1, "pill") == "1 pill" and phrases.plural(2, "pill") == "2 pills"
    assert phrases.join("A.", "", None, " B. ") == "A. B."
    assert phrases.relative_day(NOW + timedelta(hours=1), NOW) == "today"
