"""Deterministic text -> :class:`Intent` parser and the Vosk command grammar.

Speech is a request, not an authorization (ARCHITECTURE §1.3): this module only turns
words into an :class:`Intent`. The dose service decides whether anything moves.

Pipeline: :func:`normalise` (lower-case, smart quotes, ``[unk]`` removed, punctuation
stripped, contractions expanded, common ASR variants such as "dispence" / "this pens"
fixed, whitespace collapsed), then ordered rules:

1. **CANCEL always wins** ("stop", "cancel", "never mind", "close", ...), even inside
   other text and even when negated. Stopping is the safe direction.
2. **Actuating intents** (CONFIRM_TAKEN, DISPENSE) are refused when the utterance contains
   a negation ("I haven't taken it", "don't dispense"). That gives UNKNOWN with
   ``negated=True``. When phrased as a status question ("did I take it?", "is it open?"),
   they become CHECK_DUE. When both match ("taken, open the next one"), the result is
   UNKNOWN, because ambiguity fails closed.
3. REPEAT (strong phrases), HELP, CHECK_DUE, then the weak REPEAT word "again".
4. Anything else is UNKNOWN. ``negated`` is set if a negation word was present.

:data:`GRAMMAR_PHRASES` is the Vosk grammar. Every word in it was checked against the
``vosk-model-small-en-us-0.15`` vocabulary, with no brand words. The grammar deliberately
contains the negated forms and a few filler words ("yes", "okay", "thank you"). These
absorb speech that would otherwise be forced onto the nearest command; for example,
"I haven't taken it" must not be heard as "taken".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from tactidose.core.interfaces import Intent, ParsedIntent

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- normalisation

_CONTRACTIONS: dict[str, str] = {
    "what's": "what is", "whats": "what is", "that's": "that is", "it's": "it is",
    "there's": "there is", "where's": "where is", "when's": "when is",
    "i've": "i have", "ive": "i have", "i'm": "i am", "im": "i am", "i'd": "i would",
    "i'll": "i will", "you've": "you have", "we've": "we have", "let's": "let us",
    "haven't": "have not", "havent": "have not", "hasn't": "has not", "hasnt": "has not",
    "hadn't": "had not", "didn't": "did not", "didnt": "did not", "don't": "do not",
    "dont": "do not", "doesn't": "does not", "doesnt": "does not", "isn't": "is not",
    "isnt": "is not", "wasn't": "was not", "wasnt": "was not", "won't": "will not",
    "wont": "will not", "can't": "can not", "cant": "can not", "cannot": "can not",
    "couldn't": "could not", "shouldn't": "should not", "wouldn't": "would not",
    "aren't": "are not", "weren't": "were not", "ain't": "is not",
}

#: Single-token ASR / typing variants.
_TOKEN_VARIANTS: dict[str, str] = {
    "dispence": "dispense", "dispens": "dispense", "despense": "dispense",
    "despence": "dispense", "dispensed": "dispense", "dispensing": "dispense",
    "dispenses": "dispense",
    "cancelled": "cancel", "canceled": "cancel", "cancels": "cancel",
    "cancelling": "cancel", "canceling": "cancel", "cancell": "cancel",
    "nevermind": "never mind",
    "repeats": "repeat", "repeating": "repeat",
    "ok": "okay",
}

#: Multi-word ASR splits, applied after token normalisation (whole words only).
_PHRASE_VARIANTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{pattern}\b"), replacement)
    for pattern, replacement in (
        (r"this pens?", "dispense"),
        (r"this pen[cs]e", "dispense"),
        (r"this spen[cs]e", "dispense"),
        (r"the spen[cs]e", "dispense"),
        (r"dis pen[cs]e", "dispense"),
        (r"dis pens", "dispense"),
        (r"take in", "taken"),
        (r"can sell", "cancel"),
        (r"can cel", "cancel"),
        (r"re peat", "repeat"),
        (r"what is do", "what is due"),
    )
)

_NON_WORD = re.compile(r"[^a-z0-9' ]+")


def normalise(text: str) -> str:
    """Canonical form used for matching (see the module docstring)."""
    t = (text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = t.replace("[unk]", " ").replace("<unk>", " ")
    t = _NON_WORD.sub(" ", t)
    words: list[str] = []
    for token in t.split():
        token = token.strip("'")
        if not token:
            continue
        token = _CONTRACTIONS.get(token, token)
        token = _TOKEN_VARIANTS.get(token, token)
        words.append(token.replace("'", ""))
    joined = " ".join(" ".join(words).split())
    for pattern, replacement in _PHRASE_VARIANTS:
        joined = pattern.sub(replacement, joined)
    return " ".join(joined.split())


# --------------------------------------------------------------------------- rules

_MED = (
    r"(?:medication|medications|medicine|medicines|meds|pill|pills|tablet|tablets|"
    r"dose|doses|candy|token|tokens|vitamin|vitamins)"
)


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]


def _rules(*specs: str | tuple[str, str]) -> tuple[_Rule, ...]:
    """Plain strings are literal phrases (whole words); tuples are ``(label, regex)``."""
    out: list[_Rule] = []
    for spec in specs:
        if isinstance(spec, tuple):
            label, rx = spec
        else:
            label, rx = spec, re.escape(spec)
        out.append(_Rule(label, re.compile(rf"\b(?:{rx})\b")))
    return tuple(out)


_CANCEL = _rules(
    "cancel", "stop", "never mind", "close", "halt", "abort", "forget it", "shut",
)

_CONFIRM = _rules(
    "taken",
    ("took it", r"took (?:it|them|that|those|this|everything)"),
    ("took my medication", rf"took (?:my |the |a |an |one |both |all )?(?:\w+ )?{_MED}"),
    ("took", r"^(?:i )?(?:just )?(?:already )?took$"),
    ("done", r"(?<!well )done"),
    "finished", "swallowed",
    ("confirm", r"confirm|confirmed"),
)

_DISPENSE = _rules(
    "dispense",
    ("open", r"(?<!is )(?<!was )(?<!are )(?<!still )open"),
    "unlock",
    ("give me my medication", rf"(?:give|get|bring|hand|fetch)(?: \w+){{0,3}} {_MED}"),
    ("i want my medication", rf"i (?:want|need) (?:my|the)(?: next)? {_MED}"),
)

_REPEAT_STRONG = _rules(
    "repeat", "pardon", "come again", "one more time", "excuse me",
    ("say that again", r"say (?:that|it) again"),
    ("what did you say", r"what did you (?:just )?say"),
    ("what was that", r"what was that"),
    ("i did not hear", r"(?:did not|could not|can not) (?:hear|catch|understand) (?:that|you|it)"),
)

_HELP = _rules(
    "help", "commands", "command", "options",
    ("what can i say", r"what (?:can|do|should) i (?:say|ask)"),
    ("what can i do", r"what (?:can|do|should) i do"),
    ("how does this work", r"how does (?:this|it) work"),
    ("i do not understand", r"(?:do not|did not) understand"),
    ("i do not know what to say", r"do not know what to (?:say|do)"),
)

_CHECK_DUE = _rules(
    ("what do i take", r"what (?:do|should|must|can|shall) i (?:need to |have to |)?take"),
    ("did i take it", r"(?:did|have) i (?:already |just )?(?:take|taken|took)"),
    ("is it time", r"is it time|time for (?:my|the)|what time"),
    ("what am i supposed to take", r"what am i (?:supposed|meant) to take"),
    ("what is due", r"what is (?:due|next)"),
    ("due", r"due"),
    ("what now", r"what now|now what"),
    ("next dose", rf"next {_MED}"),
    ("check my schedule", r"schedule"),
    ("what is my medication", rf"what is my (?:next )?{_MED}"),
    ("is anything due", r"(?:is|are) (?:there )?(?:anything|any \w+) (?:due|left|ready)"),
    ("what should i be taking", r"(?:what|which)(?: \w+){0,3} (?:take|taking)"),
    ("do i have any medication", rf"do i have (?:any|anything)(?: \w+)?(?: {_MED})?"),
    ("when is my next dose", r"when (?:is|do|should|will) (?:my|i)"),
    ("my medication", rf"^(?:my |the )?(?:next )?{_MED}(?: now| please| today)?$"),
)

_REPEAT_WEAK = _rules("again")

_NEGATIONS = frozenset({"not", "no", "never", "nope", "nothing", "neither", "nor"})
_TAKE = re.compile(r"\b(?:take|taking)\b")

#: Actuating intents (DISPENSE / CONFIRM_TAKEN) are only recognised in short, command-like
#: utterances; a long sentence that happens to contain "open" or "took" is not a command.
MAX_COMMAND_WORDS = 10

_WH = frozenset({"what", "when", "which", "where", "who", "why", "how"})
_AUX = frozenset({
    "did", "do", "does", "have", "has", "had", "is", "was", "are", "were",
    "should", "am", "can", "could", "shall", "may", "must",
})
_SUBJECTS = frozenset({
    "i", "it", "my", "the", "this", "that", "there", "anything", "something", "we",
    "you", "everything",
})
_REQUEST_STARTS = (
    "can you", "could you", "would you", "will you", "please", "can we", "would you please",
)


def _first(rules: tuple[_Rule, ...], text: str) -> str | None:
    for rule in rules:
        if rule.pattern.search(text):
            return rule.label
    return None


def _is_question(raw: str, norm: str) -> bool:
    if norm.startswith(_REQUEST_STARTS):
        return False
    if raw.rstrip().endswith("?"):
        return True
    tokens = norm.split()
    if not tokens:
        return False
    if tokens[0] in _WH:
        return True
    return len(tokens) > 1 and tokens[0] in _AUX and tokens[1] in _SUBJECTS


def has_negation(norm: str) -> bool:
    return any(token in _NEGATIONS for token in norm.split())


def parse_intent(text: str, *, confidence: float = 1.0) -> ParsedIntent:
    """Map an utterance to an :class:`Intent` (pure, deterministic, never raises)."""
    raw = text if isinstance(text, str) else ""
    norm = normalise(raw)
    if not norm:
        return ParsedIntent(Intent.UNKNOWN, text=raw, confidence=confidence)

    def result(intent: Intent, matched: str | None, negated: bool = False) -> ParsedIntent:
        return ParsedIntent(intent, text=raw, confidence=confidence, matched=matched, negated=negated)

    cancel = _first(_CANCEL, norm)
    if cancel:
        return result(Intent.CANCEL, cancel)

    negated = has_negation(norm)
    short = len(norm.split()) <= MAX_COMMAND_WORDS
    confirm = _first(_CONFIRM, norm) if short else None
    dispense = _first(_DISPENSE, norm) if short else None
    if confirm or dispense:
        matched = confirm or dispense
        if negated:
            return result(Intent.UNKNOWN, matched, negated=True)
        if _is_question(raw, norm):
            if norm.startswith("how "):
                return result(Intent.HELP, matched)
            return result(Intent.CHECK_DUE, matched)
        if confirm and dispense:
            return result(Intent.UNKNOWN, f"ambiguous: {confirm} / {dispense}")
        return result(Intent.CONFIRM_TAKEN if confirm else Intent.DISPENSE, matched)

    for intent, rules in (
        (Intent.REPEAT, _REPEAT_STRONG),
        (Intent.HELP, _HELP),
        (Intent.CHECK_DUE, _CHECK_DUE),
        (Intent.REPEAT, _REPEAT_WEAK),
    ):
        label = _first(rules, norm)
        if label:
            return result(intent, label)
    if negated and _TAKE.search(norm):
        return result(Intent.UNKNOWN, "take", negated=True)  # "I didn't take it"
    return result(Intent.UNKNOWN, None, negated=negated)


#: Words that carry no command meaning on their own. An unrecognised utterance made only of
#: these (e.g. Vosk grammar-mode noise such as "that [unk]") is ignored silently.
FILLER_WORDS = frozenset({
    "a", "an", "the", "that", "this", "it", "i", "is", "to", "do", "my", "me", "you",
    "what", "say", "now", "and", "of", "in", "on", "at", "so", "there", "have", "has",
    "did", "does", "be", "am", "are", "was", "uh", "um", "hmm", "huh", "oh", "ah", "er",
    "eh", "mm", "hm", "yes", "yeah", "no", "okay", "alright", "right", "please", "hey",
    "hello", "hi", "thank", "thanks", "well", "just", "like", "one", "all",
})


def content_words(text: str) -> list[str]:
    """Normalised words that are not fillers (used to tell speech from noise)."""
    return [w for w in normalise(text).split() if w not in FILLER_WORDS]


# --------------------------------------------------------------------------- Vosk grammar

#: Vosk grammar for ``KaldiRecognizer(model, rate, json.dumps(GRAMMAR_PHRASES))``.
#: Lower-case; every word exists in vosk-model-small-en-us-0.15; ends with ``[unk]``.
GRAMMAR_PHRASES: list[str] = [
    # CHECK_DUE
    "what do i take now", "what do i take", "what should i take", "what should i take now",
    "what do i need to take", "what do i have to take", "what is due", "what's due",
    "what's due now", "is anything due", "is there anything due", "what now",
    "next dose", "my next dose", "when is my next dose", "what is my next dose",
    "check my schedule", "check schedule", "what's my schedule",
    "did i take it", "did i take my medication", "have i taken it",
    "have i taken my medication",
    # DISPENSE
    "dispense", "dispense it", "dispense my medication", "dispense my dose",
    "please dispense", "open", "open it", "open the compartment",
    "give me my medication", "give me my medicine", "give me my dose", "get my dose",
    "get my medication",
    # CONFIRM_TAKEN
    "taken", "i have taken it", "i've taken it", "i took it", "took it",
    "i took my medication", "done", "i'm done", "i am done", "all done", "finished",
    "i'm finished", "i have finished", "confirm", "confirm taken",
    # REPEAT
    "repeat", "repeat that", "please repeat", "say that again", "again", "pardon",
    "pardon me", "what did you say", "come again", "one more time",
    # CANCEL
    "cancel", "stop", "never mind", "nevermind", "close", "close it",
    "close the compartment", "halt", "abort", "forget it",
    # HELP
    "help", "help me", "what can i say", "commands", "options", "what are my options",
    "how does this work",
    # negations: recognised as such so they are never heard as a command
    "not taken", "not yet", "i have not taken it", "i haven't taken it",
    "i did not take it", "i didn't take it", "don't dispense", "do not dispense",
    "don't open", "do not open", "no",
    # fillers that absorb non-command speech
    "yes", "okay", "thank you", "thanks", "please", "hello",
    "[unk]",
]


def grammar_words() -> set[str]:
    """Every distinct word used by :data:`GRAMMAR_PHRASES` (excluding ``[unk]``)."""
    return {w for phrase in GRAMMAR_PHRASES for w in phrase.split() if w != "[unk]"}
