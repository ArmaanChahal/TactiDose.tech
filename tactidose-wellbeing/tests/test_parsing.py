import pytest

from tactidose_wellbeing.domain import parsing
from tactidose_wellbeing.domain.questions import MOOD, SLEEP, STRESS, SUPPORT
from tactidose_wellbeing.domain.safety import CrisisResource, SafetyConfig


@pytest.mark.parametrize(
    "question,text,value",
    [
        (MOOD, "low", "low"),
        (MOOD, "  LOW. ", "low"),
        (MOOD, "I feel pretty low today", "low"),
        (MOOD, "my mood is good", "good"),
        (MOOD, "ok", "okay"),
        (STRESS, "my stress level is high", "high"),
        (SLEEP, "I slept okay", "okay"),
        (SLEEP, "poor", "poor"),
    ],
)
def test_exact_answers(question, text, value):
    assert parsing.parse_choice(question, text) == parsing.ChoiceParse("exact", value)


@pytest.mark.parametrize(
    "question,text,value",
    [
        (MOOD, "great", "good"),
        (MOOD, "I'm fine thanks", "okay"),
        (MOOD, "sad", "low"),
        (MOOD, "good I guess", "good"),
        (SLEEP, "slept well", "good"),
        (SLEEP, "badly", "poor"),
        (STRESS, "a lot", "high"),
    ],
)
def test_suggestive_answers_need_confirmation(question, text, value):
    assert parsing.parse_choice(question, text) == parsing.ChoiceParse("candidate", value)


@pytest.mark.parametrize(
    "question,text",
    [
        (MOOD, "not good"),
        (MOOD, "not bad"),
        (MOOD, "good and low"),
        (MOOD, "great but sad"),
        (MOOD, "I don't know"),
        (MOOD, "dispense my medication"),
        (MOOD, ""),
        (STRESS, "stressed"),
        (SLEEP, "never good"),
    ],
)
def test_ambiguous_answers_are_never_mapped(question, text):
    assert parsing.parse_choice(question, text).kind == "unclear"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", "yes"), ("Yeah, please", "yes"), ("yes save it", "yes"), ("sure", "yes"),
        ("no", "no"), ("no thank you", "no"), ("nope", "no"), ("not now", "no"),
        ("maybe", None), ("okay", None), ("yes but not really", None), ("", None),
    ],
)
def test_yes_no(text, expected):
    assert parsing.parse_yes_no(text) == expected


def test_support_question_only_accepts_clear_yes_no():
    assert parsing.parse_choice(SUPPORT, "yes please").value == "yes"
    assert parsing.parse_choice(SUPPORT, "maybe later").kind == "unclear"


@pytest.mark.parametrize(
    "text,cmd",
    [("skip", "skip"), ("Skip this question.", "skip"), ("say that again", "repeat"),
     ("cancel", "cancel"), ("stop", "cancel"), ("I'm done", "finish")],
)
def test_control_phrases(text, cmd):
    assert parsing.parse_control(text) == cmd


def test_control_words_inside_sentences_are_not_commands():
    assert parsing.parse_control("I want to skip breakfast less") is None
    assert parsing.parse_control("stop worrying about work") is None


def test_note_text_wording_is_preserved():
    assert parsing.clean_note_text("  My dog woke me up\x07 at 4am!  ") == "My dog woke me up at 4am!"


def test_urgent_phrase_matching_is_word_bounded_and_configurable():
    cfg = SafetyConfig()
    assert cfg.is_urgent("I am in danger right now")
    assert not cfg.is_urgent("the stranger was kind")
    custom = SafetyConfig(urgent_phrases=("synthetic emergency phrase",),
                          crisis_resources=(CrisisResource("Example line", "configured-contact"),))
    assert custom.is_urgent("this is a Synthetic Emergency Phrase.")
    assert not custom.is_urgent("I am in danger")
    assert "Example line: configured-contact." in custom.urgent_speech()
    assert "No crisis contacts have been configured" in cfg.urgent_speech()
