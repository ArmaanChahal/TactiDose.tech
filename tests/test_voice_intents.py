"""Deterministic intent parser + Vosk grammar (tactidose/voice/intents.py)."""

from __future__ import annotations

import re

import pytest

from tactidose.core.interfaces import Intent
from tactidose.voice.intents import (
    FILLER_WORDS,
    GRAMMAR_PHRASES,
    MAX_COMMAND_WORDS,
    content_words,
    grammar_words,
    normalise,
    parse_intent,
)

C, D, T, R, X, H, U = (Intent.CHECK_DUE, Intent.DISPENSE, Intent.CONFIRM_TAKEN, Intent.REPEAT,
                       Intent.CANCEL, Intent.HELP, Intent.UNKNOWN)

CASES: list[tuple[str, Intent, bool]] = [
    # ---------------------------------------------------------------- CHECK_DUE
    ("What do I take now?", C, False),
    ("what do i take", C, False),
    ("What's due?", C, False),
    ("what is due", C, False),
    ("What do I need to take?", C, False),
    ("what should I take", C, False),
    ("What now?", C, False),
    ("Next dose", C, False),
    ("when is my next dose", C, False),
    ("check my schedule", C, False),
    ("is anything due", C, False),
    ("is there anything due right now", C, False),
    ("did I take my medication", C, False),
    ("have I taken it?", C, False),            # question, never a confirmation
    ("Did I take it already?", C, False),
    ("is it open?", C, False),                 # question, never a dispense
    ("is the compartment open", C, False),
    ("should I open it", C, False),
    ("what medicine do I take", C, False),
    ("hey what do i take now please", C, False),
    ("what's do", C, False),                   # full-vocabulary ASR of "what's due"
    ("my medication", C, False),
    ("is it time for my pills", C, False),
    ("what am I supposed to take", C, False),
    ("what's my next dose", C, False),
    ("taken?", C, False),                      # typed question
    ("are you done?", C, False),
    ("what's next", C, False),
    ("did my pill drop?", C, False),           # v2: a question, never a drop
    # ---------------------------------------------------------------- DISPENSE
    ("Dispense.", D, False),
    ("dispense", D, False),
    ("dispence", D, False),                    # misspelling
    ("this pens", D, False),                   # ASR split
    ("this pence", D, False),
    ("dis pense", D, False),
    ("the spence", D, False),
    ("Please dispense my medication", D, False),
    ("open", D, False),
    ("Open it.", D, False),
    ("open the compartment", D, False),
    ("give me my medication", D, False),
    ("Give me my medicine please", D, False),
    ("get my dose", D, False),
    ("get me my next dose", D, False),
    ("I want my medication", D, False),
    ("can you open it?", D, False),            # polite request, not a question
    ("could you dispense", D, False),
    ("dispense again", D, False),              # "again" is weaker than dispense
    ("unlock", D, False),
    ("dispensed", D, False),                   # ASR variant
    ("tactidose dispense", D, False),
    ("drop my pill", D, False),                # v2
    ("drop it", D, False),
    ("please drop my vitamin", D, False),
    # ---------------------------------------------------------------- CONFIRM_TAKEN
    ("Taken.", T, False),
    ("taken", T, False),
    ("I took it", T, False),
    ("I have taken it", T, False),
    ("I've taken it", T, False),
    ("I’ve taken it", T, False),          # curly apostrophe
    ("Done", T, False),
    ("I'm done", T, False),
    ("all done", T, False),
    ("finished", T, False),
    ("I'm finished", T, False),
    ("took it", T, False),
    ("I took my pills", T, False),
    ("I just took my vitamin", T, False),
    ("confirm", T, False),
    ("confirm taken", T, False),
    ("ok taken", T, False),
    ("I have take in it", T, False),           # ASR split of "taken"
    ("have taken it", T, False),               # dropped pronoun: still declarative
    ("it's taken", T, False),
    ("yes I took it", T, False),
    # ---------------------------------------------------------------- REPEAT
    ("Repeat.", R, False),
    ("repeat that", R, False),
    ("say that again", R, False),
    ("again", R, False),
    ("pardon", R, False),
    ("pardon me", R, False),
    ("What did you say?", R, False),
    ("come again", R, False),
    ("one more time", R, False),
    ("sorry what was that", R, False),
    ("can you repeat that please", R, False),
    # ---------------------------------------------------------------- CANCEL (always wins)
    ("Cancel.", X, False),
    ("stop", X, False),
    ("STOP!", X, False),
    ("never mind", X, False),
    ("nevermind", X, False),
    ("close", X, False),
    ("close it", X, False),
    ("please stop the carousel", X, False),
    ("don't stop", X, False),                  # safety: stop wins even when negated
    ("stop dispensing", X, False),
    ("dispense no wait stop", X, False),       # stop wins inside other text
    ("taken, stop", X, False),
    ("cancelled", X, False),
    ("can sell", X, False),                    # ASR split
    ("forget it", X, False),
    ("halt", X, False),
    ("abort", X, False),
    ("I took it, cancel", X, False),
    # ---------------------------------------------------------------- HELP
    ("Help.", H, False),
    ("help me", H, False),
    ("what can I say", H, False),
    ("commands", H, False),
    ("what are my options", H, False),
    ("how does this work", H, False),
    ("how do I open it", H, False),
    ("I don't understand", H, False),
    ("what do I do", H, False),
    # ---------------------------------------------------------------- negated: never CONFIRM/DISPENSE
    ("not taken", U, True),
    ("I have not taken it", U, True),
    ("I haven't taken it", U, True),
    ("I havent taken it yet", U, True),
    ("I didn't take it", U, True),
    ("I did not take my medication", U, True),
    ("don't dispense", U, True),
    ("do not open", U, True),
    ("do not dispense it", U, True),
    ("no don't open it", U, True),
    ("I'm not done", U, True),
    ("not yet finished", U, True),
    ("never taken", U, True),
    ("nothing taken", U, True),
    ("I can't take it", U, True),
    ("no I took it", U, True),                 # ambiguous correction: fail closed
    ("no", U, True),
    ("don't drop it", U, True),                # v2
    ("do not drop my pill", U, True),
    # ---------------------------------------------------------------- other UNKNOWN
    ("", U, False),
    ("   ", U, False),
    ("[unk]", U, False),
    ("[unk] [unk]", U, False),
    ("the weather is nice today", U, False),
    ("it is open", U, False),                  # a statement, not a request
    ("I took the dog for a walk", U, False),
    ("taken and open the next one", U, False),  # both actuating intents: ambiguous
    ("well done", U, False),
    ("hello", U, False),
    ("yes", U, False),
    ("thank you", U, False),
    ("I took the bus to the store and then walked home and opened the door", U, False),
    ("dispensary", U, False),
    ("stopwatch", U, False),
    ("reopen", U, False),
    ("I dropped my glasses yesterday", U, False),
]


@pytest.mark.parametrize("text,intent,negated", CASES)
def test_parse_intent(text: str, intent: Intent, negated: bool) -> None:
    parsed = parse_intent(text)
    assert parsed.intent is intent, (text, parsed)
    assert parsed.negated is negated, (text, parsed)
    assert parsed.text == text


def test_case_count() -> None:
    assert len(CASES) >= 80


@pytest.mark.parametrize("text", [c[0] for c in CASES if c[2]])
def test_negated_text_never_actuates(text: str) -> None:
    assert parse_intent(text).intent not in (Intent.CONFIRM_TAKEN, Intent.DISPENSE)


@pytest.mark.parametrize("text", ["I haven't taken it", "don't dispense", "not taken", "I didn't take it"])
def test_negated_actuation_reports_what_was_negated(text: str) -> None:
    parsed = parse_intent(text)
    assert parsed.negated and parsed.matched


def test_parse_carries_confidence_and_matched_phrase() -> None:
    parsed = parse_intent("Dispense.", confidence=0.71)
    assert parsed.confidence == 0.71 and parsed.matched == "dispense"


def test_parse_never_raises_on_odd_input() -> None:
    for odd in (None, 123, "\x00\x01", "!!!", "éè café", "a" * 5000):
        assert parse_intent(odd).intent in Intent  # type: ignore[arg-type]


def test_long_utterances_never_actuate() -> None:
    words = ["please"] * MAX_COMMAND_WORDS + ["dispense"]
    assert parse_intent(" ".join(words)).intent is not Intent.DISPENSE
    assert parse_intent("please " * 3 + "dispense").intent is Intent.DISPENSE


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("What's DUE?!", "what is due"),
        ("I’ve   taken   it.", "i have taken it"),
        ("[unk] dispence [unk]", "dispense"),
        ("this pens please", "dispense please"),
        ("I haven't", "i have not"),
        ("don't", "do not"),
        ("nevermind", "never mind"),
        ("i have take in it", "i have taken it"),
        ("OK", "okay"),
        ("", ""),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


def test_content_words_ignore_fillers_and_unk() -> None:
    assert content_words("that [unk]") == []
    assert content_words("yes okay thank you") == []
    assert content_words("banana phone") == ["banana", "phone"]
    assert "the" in FILLER_WORDS and "dispense" not in FILLER_WORDS


# --------------------------------------------------------------------------- grammar


def test_grammar_shape() -> None:
    assert GRAMMAR_PHRASES[-1] == "[unk]"
    assert len(GRAMMAR_PHRASES) == len(set(GRAMMAR_PHRASES))
    for phrase in GRAMMAR_PHRASES:
        assert phrase == phrase.lower() and phrase.strip() == phrase, phrase
        if phrase != "[unk]":
            assert re.fullmatch(r"[a-z' ]+", phrase), phrase
    assert "tactidose" not in grammar_words()
    assert "dispence" not in grammar_words()  # misspelling, not in the model vocabulary


def test_grammar_covers_every_command_intent() -> None:
    intents = {parse_intent(p).intent for p in GRAMMAR_PHRASES}
    assert {C, D, T, R, X, H} <= intents


def test_grammar_negated_phrases_are_recognised_as_negations() -> None:
    negated = [p for p in GRAMMAR_PHRASES if re.search(r"\b(not|no)\b|n't", p)]
    assert len(negated) >= 8
    for phrase in negated:
        parsed = parse_intent(phrase)
        assert parsed.intent is Intent.UNKNOWN and parsed.negated, phrase


def test_grammar_filler_phrases_are_not_commands() -> None:
    for phrase in ("yes", "okay", "thank you", "thanks", "please", "hello"):
        assert phrase in GRAMMAR_PHRASES
        parsed = parse_intent(phrase)
        assert parsed.intent is Intent.UNKNOWN and content_words(phrase) == []


def test_every_grammar_phrase_parses_deterministically() -> None:
    first = [parse_intent(p) for p in GRAMMAR_PHRASES]
    second = [parse_intent(p) for p in GRAMMAR_PHRASES]
    assert first == second
