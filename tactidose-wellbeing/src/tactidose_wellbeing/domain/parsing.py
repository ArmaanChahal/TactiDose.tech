"""Deterministic parsing of transcribed or typed user input.

Rules (documented in README "Answer parsing"):

* A reply that reduces to exactly one option word (``"low"``, ``"I feel
  pretty low"``) is an *exact* answer.
* A reply that only suggests an option (``"great"``, ``"good I guess"``) is a
  *candidate* and must be confirmed by the user before it is recorded.
* Anything containing a negation, several options, or nothing recognisable is
  *unclear* and triggers a clarification prompt. Ambiguous statements are never
  silently mapped to a category.
* Control phrases (skip, repeat, cancel, finish) only apply when they are the
  entire utterance.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from .questions import Question

_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")

NEGATIONS = frozenset(
    {"not", "no", "never", "dont", "isnt", "wasnt", "didnt", "cant", "neither", "nor", "hardly"}
)

# Words that carry no answer content in a reply such as "I think my mood is
# pretty low today".
FILLER = frozenset(
    {
        "i", "im", "am", "feel", "feeling", "felt", "it", "its", "is", "was", "been",
        "has", "have", "my", "mood", "stress", "level", "levels", "sleep", "slept",
        "sleeping", "the", "a", "would", "say", "id", "today", "tonight", "last",
        "night", "right", "now", "think", "pretty", "quite", "fairly", "kind", "of",
        "kinda", "rather", "really", "very", "bit", "little", "um", "uh", "so",
        "just", "answer", "please", "thanks", "thank", "you",
    }
)

_POLITE = frozenset({"please", "thanks", "thank", "you", "um", "uh", "oh", "well", "okay", "ok"})

YES_PHRASES = frozenset(
    {
        "yes", "yeah", "yep", "yup", "sure", "correct", "right", "thats right",
        "that is right", "yes it is", "i would", "i do", "affirmative", "definitely",
        "of course", "absolutely", "yes i would", "yes i do",
    }
)
NO_PHRASES = frozenset(
    {
        "no", "nope", "nah", "not now", "not really", "not today", "i dont",
        "i would not", "i wouldnt", "negative", "wrong", "incorrect", "thats wrong",
        "no it isnt", "no i dont", "no i wouldnt",
    }
)
_YES_LEADS = frozenset({"yes", "yeah", "yep", "yup"})
_NO_LEADS = frozenset({"no", "nope", "nah"})

CONTROL_PHRASES: dict[str, str] = {}
for _phrase in ("repeat", "repeat that", "say that again", "again", "pardon", "what", "can you repeat that"):
    CONTROL_PHRASES[_phrase] = "repeat"
for _phrase in ("skip", "skip it", "skip this", "skip this question", "skip question", "pass", "next"):
    CONTROL_PHRASES[_phrase] = "skip"
for _phrase in (
    "cancel", "cancel it", "cancel check in", "cancel the check in", "stop", "stop check in",
    "quit", "exit", "never mind", "nevermind", "end check in",
):
    CONTROL_PHRASES[_phrase] = "cancel"
for _phrase in ("finish", "finish check in", "done", "im done", "i am done", "thats all", "that is all", "finished"):
    CONTROL_PHRASES[_phrase] = "finish"

CHANGE_PHRASES = frozenset(
    {"change", "change it", "correct it", "correct", "edit", "edit it", "redo", "let me change it", "change the note"}
)
REMOVE_PHRASES = frozenset(
    {"remove", "remove it", "delete", "delete it", "leave it out", "dont save it", "discard", "discard it", "remove the note"}
)


def normalize(text: str) -> str:
    """Lower-case, drop apostrophes and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("'", "").replace("’", "")
    text = _NON_WORD.sub(" ", text).replace("_", " ")
    tokens = ["okay" if t == "ok" else t for t in _SPACES.split(text.strip()) if t]
    return " ".join(tokens)


def _strip_polite(norm: str) -> str:
    tokens = norm.split()
    while tokens and tokens[0] in _POLITE:
        tokens.pop(0)
    while tokens and tokens[-1] in _POLITE:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True)
class ChoiceParse:
    kind: Literal["exact", "candidate", "unclear"]
    value: str | None = None


def parse_choice(question: Question, text: str) -> ChoiceParse:
    """Parse an answer to a multiple-choice question."""
    if question.yes_no:
        yn = parse_yes_no(text)
        return ChoiceParse("exact", yn) if yn else ChoiceParse("unclear")

    norm = normalize(text)
    tokens = norm.split()
    if not tokens or any(t in NEGATIONS for t in tokens):
        return ChoiceParse("unclear")

    residual = " ".join(t for t in tokens if t not in FILLER)
    if residual in question.options:
        return ChoiceParse("exact", residual)
    if residual in question.synonyms:
        return ChoiceParse("candidate", question.synonyms[residual])
    if norm in question.synonyms:
        return ChoiceParse("candidate", question.synonyms[norm])

    suggested = {t for t in tokens if t in question.options}
    suggested |= {question.synonyms[t] for t in tokens if t in question.synonyms}
    if len(suggested) == 1:
        return ChoiceParse("candidate", suggested.pop())
    return ChoiceParse("unclear")


def parse_yes_no(text: str) -> Literal["yes", "no"] | None:
    norm = normalize(text)
    stripped = _strip_polite(norm)
    for candidate in (stripped, norm):
        if candidate in YES_PHRASES:
            return "yes"
        if candidate in NO_PHRASES:
            return "no"
    tokens = stripped.split()
    if not tokens:
        return None
    if tokens[0] in _NO_LEADS:
        return "no"
    if tokens[0] in _YES_LEADS and not any(t in NEGATIONS for t in tokens):
        return "yes"
    return None


def parse_control(text: str) -> str | None:
    """Return a control command name if the whole utterance is a control phrase."""
    norm = normalize(text)
    return CONTROL_PHRASES.get(norm) or CONTROL_PHRASES.get(_strip_polite(norm))


def is_change_request(text: str) -> bool:
    return _strip_polite(normalize(text)) in CHANGE_PHRASES


def is_remove_request(text: str) -> bool:
    return _strip_polite(normalize(text)) in REMOVE_PHRASES


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_note_text(text: str) -> str:
    """Keep the user's wording; only trim whitespace and remove control characters."""
    return _CONTROL_CHARS.sub("", text).strip()
