"""The fixed, non-clinical question catalog and question-specific wording.

These are informal self-reported observations. No scores are computed and no
condition is inferred from any combination of answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class QuestionId(StrEnum):
    MOOD = "mood"
    STRESS = "stress"
    SLEEP = "sleep"
    SUPPORT = "support"


@dataclass(frozen=True)
class Question:
    id: QuestionId
    label: str
    prompt: str
    options: tuple[str, ...]
    # Words that *suggest* an option. A synonym match is never recorded
    # directly: the user is always asked to confirm it.
    synonyms: dict[str, str] = field(default_factory=dict)
    yes_no: bool = False
    offers_note: bool = False


MOOD = Question(
    id=QuestionId.MOOD,
    label="Mood",
    prompt="How is your mood today: good, okay, or low?",
    options=("good", "okay", "low"),
    synonyms={
        "great": "good",
        "happy": "good",
        "excellent": "good",
        "wonderful": "good",
        "fine": "okay",
        "alright": "okay",
        "all right": "okay",
        "so so": "okay",
        "average": "okay",
        "meh": "okay",
        "bad": "low",
        "down": "low",
        "sad": "low",
    },
    offers_note=True,
)

STRESS = Question(
    id=QuestionId.STRESS,
    label="Stress",
    prompt="How is your stress level: low, medium, or high?",
    options=("low", "medium", "high"),
    synonyms={
        "calm": "low",
        "relaxed": "low",
        "minimal": "low",
        "moderate": "medium",
        "some": "medium",
        "average": "medium",
        "lot": "high",
        "lots": "high",
        "overwhelmed": "high",
    },
    offers_note=True,
)

SLEEP = Question(
    id=QuestionId.SLEEP,
    label="Sleep",
    prompt="How did you sleep: good, okay, or poor?",
    options=("good", "okay", "poor"),
    synonyms={
        "well": "good",
        "great": "good",
        "fine": "okay",
        "alright": "okay",
        "all right": "okay",
        "so so": "okay",
        "bad": "poor",
        "badly": "poor",
        "terrible": "poor",
        "awful": "poor",
    },
    offers_note=True,
)

SUPPORT = Question(
    id=QuestionId.SUPPORT,
    label="Support from a person",
    prompt=(
        "Would you like support from a person, such as someone you trust? "
        "Please say yes or no."
    ),
    options=("yes", "no"),
    yes_no=True,
)

QUESTIONS: tuple[Question, ...] = (MOOD, STRESS, SLEEP, SUPPORT)
QUESTIONS_BY_ID: dict[QuestionId, Question] = {q.id: q for q in QUESTIONS}

# Follow-up wording is specific to both the question and the confirmed answer,
# so the user always knows what the note is about.
NOTE_PROMPTS: dict[tuple[QuestionId, str], str] = {
    (QuestionId.MOOD, "good"): "Would you like to share what is making you feel good?",
    (QuestionId.MOOD, "okay"): "Would you like to share anything about how your mood has been?",
    (QuestionId.MOOD, "low"): "Would you like to share what is making you feel low?",
    (QuestionId.STRESS, "low"): "Would you like to share what is helping keep your stress low?",
    (QuestionId.STRESS, "medium"): "Would you like to share what is contributing to your stress?",
    (QuestionId.STRESS, "high"): "Would you like to share what is contributing to your stress?",
    (QuestionId.SLEEP, "good"): "Would you like to share what helped you sleep well?",
    (QuestionId.SLEEP, "okay"): "Would you like to share anything about how you slept?",
    (QuestionId.SLEEP, "poor"): "Would you like to share what affected your sleep?",
}

NOTE_OFFER_SUFFIX = "This is optional. You can tell me now, or say no to move on."


def note_prompt(question_id: QuestionId, value: str) -> str:
    return f"{NOTE_PROMPTS[(question_id, value)]} {NOTE_OFFER_SUFFIX}"
