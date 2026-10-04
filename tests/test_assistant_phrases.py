"""Spoken sentences (tactidose/core/phrases.py)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tactidose.core import phrases
from tactidose.core.interfaces import BlockReason, DoseInfo
from tests.conftest import TEST_TZ

TZ = ZoneInfo(TEST_TZ)
NOW = datetime(2026, 10, 5, 7, 55, tzinfo=TZ)


def dose(*, hour: int = 8, minute: int = 0, day: int = 5, slot: int | None = 2,
         name: str = "Vitamin C (demo candy)", instructions: str | None = "Take one piece.",
         status: str = "DUE", event_id: int = 12) -> DoseInfo:
    local = datetime(2026, 10, day, hour, minute, tzinfo=TZ)
    return DoseInfo(event_id=event_id, medication_id=3, medication_name=name, strength="1 piece",
                    instructions=instructions, slot=slot, scheduled_at=local.astimezone(timezone.utc),
                    scheduled_local=local, status=status)


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(8, 0, "8:00 AM"), (13, 30, "1:30 PM"), (0, 5, "12:05 AM"), (12, 0, "12:00 PM"), (23, 59, "11:59 PM"),
     (11, 59, "11:59 AM")],
)
def test_spoken_time(hour, minute, expected):
    assert phrases.spoken_time(datetime(2026, 1, 1, hour, minute)) == expected


def test_compartment_numbers_are_one_based():
    assert phrases.compartment(dose(slot=0)) == "compartment 1"
    assert phrases.compartment(dose(slot=2)) == "compartment 3"
    assert phrases.compartment(dose(slot=None)) is None


def test_dose_ready_with_names_reads_label_verbatim():
    text = phrases.dose_ready(dose(), include_names=True)
    assert text == ("Your Vitamin C (demo candy) is ready in compartment 3. The label says: Take one piece. "
                    "When you have taken it, say 'taken' or press the big button.")


def test_dose_ready_generic_wording_hides_names_and_label():
    text = phrases.dose_ready(dose(), include_names=False)
    assert text == ("Your 8:00 AM medication is ready in compartment 3. "
                    "When you have taken it, say 'taken' or press the big button.")
    assert "Vitamin" not in text and "label" not in text


def test_dose_ready_without_instructions_or_slot():
    assert phrases.dose_ready(dose(instructions=None, slot=None), include_names=True) == (
        "Your Vitamin C (demo candy) is ready. When you have taken it, say 'taken' or press the big button.")


def test_label_text_is_normalised_and_capped():
    assert phrases.label_says("  Take   with water  ") == "The label says: Take with water."
    assert phrases.label_says("Shake well!") == "The label says: Shake well!"
    assert phrases.label_says("") == "" and phrases.label_says(None) == ""
    long = phrases.label_says("word " * 200)
    assert long.endswith("The label text continues.") and len(long) < 360


def test_due_now_prompts_for_consent():
    assert phrases.due_now(dose(), include_names=True) == (
        "Your 8:00 AM Vitamin C (demo candy) is due now. Say 'dispense' or press the big button.")
    assert phrases.due_now(dose(), count=2, include_names=False) == (
        "You have two doses due. The first is your 8:00 AM medication. Say 'dispense' or press the big button.")
    assert phrases.due_now(dose(), include_names=True, button_dispenses=False).endswith(
        "Say 'dispense' when you are ready.")


def test_awaiting_confirmation_tells_what_to_do_next():
    text = phrases.awaiting_confirmation(dose(status="DISPENSED"), include_names=True, more_due=1)
    assert text == ("Compartment 3 was opened for your 8:00 AM Vitamin C (demo candy). When you have taken it, "
                    "say 'taken' or press the big button. You also have one more dose due after that.")


def test_next_dose_today_tomorrow_and_later():
    assert phrases.next_dose(dose(hour=13), include_names=True, now_local=NOW) == (
        "Your next dose is Vitamin C (demo candy) at 1:00 PM.")
    assert phrases.next_dose(dose(day=6), include_names=False, now_local=NOW) == (
        "Your next medication is tomorrow at 8:00 AM.")
    assert phrases.next_dose(dose(day=8), include_names=False, now_local=NOW) == (
        "Your next medication is on Thursday at 8:00 AM.")
    assert phrases.next_dose(dose(day=20), include_names=False, now_local=NOW) == (
        "Your next medication is on Tuesday October 20 at 8:00 AM.")
    assert phrases.next_dose(None, include_names=True, now_local=NOW) == ""


def test_nothing_due_and_already_accessed_keep_mandated_wording():
    assert phrases.nothing_due(None, include_names=True) == "You do not have a scheduled medication due right now."
    assert phrases.nothing_due(dose(hour=13, name="Calcium (demo token)"), include_names=True, now_local=NOW) == (
        "You do not have a scheduled medication due right now. Your next dose is Calcium (demo token) at 1:00 PM.")
    assert phrases.already_accessed(None, include_names=True) == "That scheduled dose has already been accessed."


def test_confirmed_variants():
    d = dose(status="TAKEN")
    assert phrases.confirmed(d, include_names=True) == "Thank you. Your Vitamin C (demo candy) is recorded as taken."
    assert phrases.confirmed(d, include_names=False) == "Thank you. Your 8:00 AM medication is recorded as taken."
    assert phrases.confirmed(None, include_names=True) == "Thank you. Your dose is recorded as taken."
    assert phrases.confirmed(d, include_names=True, gate_closed=False).endswith(phrases.GATE_CLOSE_FAILED)
    assert phrases.confirmed(d, include_names=True, more_due=2).endswith(
        "You have two more doses due. Say 'dispense' when you are ready.")
    assert phrases.already_confirmed(d, include_names=False) == "Your 8:00 AM medication is already recorded as taken."
    assert phrases.already_confirmed(None, include_names=True) == phrases.ALREADY_CONFIRMED


@pytest.mark.parametrize("reason", list(BlockReason))
def test_every_block_reason_has_a_static_phrase(reason):
    text = phrases.blocked(reason)
    assert text in phrases.CRITICAL_PHRASES
    assert phrases.blocked(reason.value) == text
    assert phrases.blocked("SOMETHING_NEW") == phrases.ASK_FOR_ASSISTANCE


def test_mandated_wording_is_verbatim():
    assert phrases.ALREADY_ACCESSED == "That scheduled dose has already been accessed."
    assert phrases.COULD_NOT_PREPARE == "I could not prepare the compartment. Please ask for assistance."
    assert phrases.CANCELLED == "Cancelled."
    assert phrases.ASK_FOR_ASSISTANCE == "Please ask for assistance."
    assert phrases.NETWORK_UNAVAILABLE == "Network unavailable."
    assert phrases.HARDWARE_ERROR == "Hardware error."
    assert phrases.NOTHING_DUE == "You do not have a scheduled medication due right now."
    assert phrases.GATE_CLOSED_TIMEOUT == "I've closed the compartment. If you took your dose, say 'taken'."
    assert phrases.PREPARING == "Preparing your dose. Please keep your hands clear of the opening."
    for text in (phrases.ALREADY_ACCESSED, phrases.COULD_NOT_PREPARE, phrases.CANCELLED, phrases.ASK_FOR_ASSISTANCE,
                 phrases.NETWORK_UNAVAILABLE, phrases.HARDWARE_ERROR, phrases.NOTHING_DUE):
        assert text in phrases.CRITICAL_PHRASES


def test_critical_phrases_are_static_unique_and_complete():
    crit = phrases.CRITICAL_PHRASES
    assert len(crit) == len(set(crit)) >= 30
    for text in crit:
        assert text == " ".join(text.split()) and text[-1] in ".!?", text
        assert not re.search(r"\d", text), text          # no times / numbers
        assert "Vitamin" not in text and "Calcium" not in text
    fragments = {"SAY_DISPENSE", "SAY_DISPENSE_WHEN_READY", "SAY_TAKEN", "ERROR_GENERIC"}
    constants = {name: value for name, value in vars(phrases).items()
                 if name.isupper() and isinstance(value, str) and name not in fragments}
    missing = [name for name, value in constants.items() if value not in crit]
    assert missing == []


def test_no_dosage_advice_outside_label_text():
    advice = re.compile(r"\b(mg|milligram|dosage|you should take|take two|take one|double)\b", re.I)
    samples = list(phrases.CRITICAL_PHRASES) + [
        phrases.dose_ready(dose(instructions=None), include_names=True),
        phrases.due_now(dose(), include_names=True),
        phrases.confirmed(dose(), include_names=True, more_due=1),
        phrases.awaiting_confirmation(dose(), include_names=True, more_due=1),
    ]
    for text in samples:
        assert not advice.search(text), text


def test_long_or_messy_names_are_cleaned_for_speech():
    d = dose(name="  Super   long " + "x" * 200)
    ref = phrases.dose_ref(d, include_names=True, with_time=False)
    assert ref.startswith("your Super long") and len(ref) < 100
    assert phrases.med_name(dose(name="   ")) == "medication"


def test_count_words():
    assert [phrases.count_words(n) for n in (0, 1, 2, 12, 13)] == ["zero", "one", "two", "twelve", "13"]


def test_day_phrase_handles_unknown_now():
    assert phrases.day_phrase(datetime(2026, 10, 9, 8, tzinfo=TZ), None) == ""
    assert phrases.day_phrase(NOW + timedelta(hours=1), NOW) == ""
