"""Offline deterministic agent and the text safety checks shared by every provider.

:func:`analyse` turns one patient message into :class:`TextFlags` with
``voice.intents.parse_intent`` plus v2 patterns (emergencies, prompt injection, negations,
several pills at once, dose-change questions, symptoms, container numbers). The flags are
used three ways:

* :func:`turn_guard` -> :class:`~tactidose.agent.tools.TurnGuard`: what the tools must refuse
  in this turn whatever a model asks (any provider);
* ``AgentService`` short-circuits emergencies and "stop" deterministically (any provider);
* :class:`RulesAgent` answers offline with the same tools and storage as Gemini.

Rules agent behaviour (replies come from :mod:`tactidose.core.phrases`, a few short sentences):
"drop my vitamin c", "give me pill 2", "can I have my calcium?" -> status, then one
``request_pill``; "what's due" / "when can I have my next pill"; "when did I last take my pill";
"how many pills are left"; "I took it" -> ``confirm_pill_taken``; "help"; greetings; "repeat".
Questions ("did my pill drop?") never request a pill. Neither do negations ("don't drop it",
"I don't want my pill"), requests for several pills, prompt-injection attempts, emergencies or
speech containing ``[unk]``. When the patient did not say which pill: the only container with a
medication, else the dose due now, else "Which pill would you like?" (the next message may answer
with a name or number); "yes" after "Would you like me to drop it now?" drops the due dose.

:func:`describe_outcome` is the deterministic sentence for a ``request_pill`` result; the
service and the device voice loop use it too.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from tactidose.agent.tools import (
    CONFIRM_PILL_TAKEN,
    GET_RECENT_DROPS,
    NOT_REQUESTED,
    REQUEST_PILL,
    STOP_DEVICE,
    PatientTools,
    TurnGuard,
    as_int,
    due_doses,
    match_containers,
    medication_options,
    name_score,
    parse_dt,
)
from tactidose.config import Settings
from tactidose.core import phrases
from tactidose.core.clock import Clock
from tactidose.core.interfaces import ContainerInfo, Intent, ParsedIntent, PatientStatus
from tactidose.voice.intents import normalise, parse_intent

log = logging.getLogger(__name__)

MODEL_NAME = "rules"

# --------------------------------------------------------------------------- patterns (normalised text)


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b")


_EMERGENCY = _any(
    r"chest (?:pain|pains|hurts?|hurting|is hurting|tight\w*|pressure)",
    r"pain in (?:my|the) chest",
    r"heart attack",
    r"stroke",
    r"(?:can not|could not|unable to|trouble|difficulty|hard to|struggling to) (?:breathe|breathing|breath)",
    r"short(?:ness)? of breath",
    r"not breathing",
    r"choking",
    r"overdos\w*",
    r"(?:took|taken|take|swallowed|ate|had) (?:way )?(?:too many|too much|all (?:of )?(?:my|the))",
    r"too many (?:pills|tablets|candies|tokens|of them)",
    r"suicid\w*",
    r"kill (?:myself|me)",
    r"end my life",
    r"(?:want|going) to die",
    r"hurt(?:ing)? myself",
    r"self harm",
    r"unconscious",
    r"passed out",
    r"faint(?:ed|ing)",
    r"collapsed",
    r"seizures?",
    r"bleeding (?:a lot|heavily|badly|everywhere)",
    r"allergic reaction",
    r"anaphyla\w*",
    r"(?:throat|tongue|face|lips) (?:is |are )?(?:closing|swelling|swollen)",
    r"(?:this is|it is|having|have|in) an emergency",
    r"medical emergency",
    r"emergency (?:help|services|room)",
    r"911",
    r"ambulance",
    r"poison\w*",
)

_INJECTION = _any(
    r"ignore (?:all |any |your |the |my |previous |prior |these |those |above |earlier |every )*"
    r"(?:rules?|instructions?|prompts?|guidelines?|limits?|restrictions?|programming|safety|system)",
    r"disregard (?:\w+ ){0,3}(?:rules?|instructions?|prompts?|guidelines?|limits?)",
    r"forget (?:all |your |the |previous |prior )*(?:rules?|instructions?|prompts?|guidelines?)",
    r"(?:system|developer|hidden) (?:prompt|message|instructions?|mode)",
    r"jailbreak\w*",
    r"pretend (?:to be|you are|that you)",
    r"you are now",
    r"act as (?:a|an|my|if)",
    r"(?:your|these are|here are|follow|obey) (?:the |my |your |these )?new (?:rules?|instructions?)",
    r"new (?:rules?|instructions?) for you",
    r"override\w*",
    r"bypass\w*",
    r"no (?:rules?|limits?|restrictions?)",
    r"without (?:any )?(?:rules?|limits?|restrictions?)",
    r"admin(?:istrator)? mode",
    r"god mode",
    r"sudo",
    r"(?:reveal|show|print|repeat) (?:me )?(?:your|the) (?:system )?(?:prompt|instructions?|rules)",
)

_COUNT = (r"(?:[2-9]|[1-9][0-9]|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|all|both|"
          r"several|multiple|a few|a couple of|couple of|lots of|a lot of)")
_MULTIPLE = _any(
    rf"{_COUNT} (?:of (?:my |the |your )?)?(?:more |extra )?"
    r"(?:pills|tablets|doses|candies|tokens|pieces|capsules|vitamins)",
    r"double (?:dose|doses|pill|pills|up)",
    r"(?:all|both) (?:of )?(?:them|my pills|the pills|pills)",
    r"every (?:pill|container)",
)

_MED_CHANGE = _any(
    r"stop taking", r"quit taking", r"start taking",
    r"(?:change|increase|decrease|raise|lower|reduce|adjust|cut) (?:my |the )?(?:\w+ )?"
    r"(?:dose|doses|dosage|medication|medications|medicine|prescription)",
    r"skip (?:my |the |a )?(?:\w+ )?(?:dose|pill|medication)",
    r"should i (?:stop|skip|change|double|increase|decrease|take more|take less|take another|take extra)",
    r"how (?:many|much) (?:\w+ )?should i (?:take|have)",
    r"(?:is it|is that|would it be) (?:safe|ok|okay|fine|bad|dangerous) to (?:take|mix|combine|have|drink|skip)",
    r"(?:can|should) i (?:take|have) (?:it|this|them|my \w+(?: \w+)?) with",
    r"interact\w*",
    r"what (?:is|are) (?:it|this|my \w+) (?:for|used for)",
)

_SYMPTOM = _any(
    r"headaches?", r"migraines?", r"dizz\w*", r"light ?headed", r"nause\w*", r"vomit\w*",
    r"throwing up", r"threw up", r"rash", r"hives", r"itch\w*", r"fever", r"side effects?",
    r"stomach ?aches?", r"upset stomach", r"diarrh\w*", r"constipat\w*", r"cough\w*",
    r"sore throat", r"aches?", r"aching", r"pains?", r"painful", r"hurts?", r"hurting", r"unwell",
    r"sick", r"tired", r"fatigue\w*", r"exhausted", r"sleepy", r"drowsy", r"swelling", r"swollen",
    r"blurry", r"blurred", r"confus\w*", r"anxious", r"anxiety", r"shaky", r"shaking", r"numb\w*",
    r"tingl\w*", r"palpitations?", r"heart (?:is )?(?:racing|pounding)",
    r"feel(?:ing)? (?:bad|awful|terrible|worse|weird|funny|strange|off|wrong|down|ill)",
    r"(?:not|do not) feel(?:ing)? (?:well|good|right)",
)

_REQUEST_VERB = _any(
    r"(?:can|could|may|might) i (?:please )?(?:have|get|take|grab)",
    r"(?:can|could|would|will) you (?:please )?(?:drop|give|get|dispense|release|hand|bring|send)",
    r"i (?:want|need|would like|will have|will take)",
    r"(?:give|get|bring|hand|fetch|send) me",
    r"drop|dispense|release",
    r"let me (?:have|take|get)",
)
_POLITE_START = re.compile(r"^(?:please )?(?:can|could|would|will|may|might) (?:you|i)\b")
_PILL_OBJECT = _any(
    r"pill", r"pills", r"dose", r"doses", r"medication", r"medications", r"medicine", r"medicines",
    r"meds", r"tablet", r"tablets", r"vitamin", r"vitamins", r"candy", r"candies", r"token", r"tokens",
    r"capsule", r"capsules", r"it", r"one", r"another",
)
_INFO_SEEKING = _any(r"(?:want|need) to know", r"tell me", r"remind me", r"explain", r"what happens")
_DROP_NEGATION = _any(
    r"(?:do not|never|not)(?: (?!forget\b)\w+){0,2} (?:drop|dispense|release|give|want|need)",
    r"no (?:more )?(?:pills?|medication|medicine|dose)",
)
_PILLS_LEFT = _any(
    r"how (?:many|much) (?:\w+ ){0,3}(?:left|remaining|remain|have|got|in|still|there)",
    r"pill count", r"pills left", r"running (?:low|out)", r"(?:is|are) (?:\w+ ){0,3}empty", r"refill\w*",
)
_LAST_PILL = _any(
    r"(?:last|latest|previous|most recent) (?:\w+ ){0,2}"
    r"(?:pill|pills|dose|doses|medication|medicine|drop|vitamin|one|time)",
    r"when did i",
    r"when was (?:my|the) last",
    r"(?:did|has|have)(?: not)? (?:my|the|it|a) (?:\w+ ){0,3}(?:drop|dropped|come out|fall|fallen)",
    r"(?:did|have) i (?:already |just )?(?:take|taken|took|had|have) (?:my|a|the|it|any)",
)
_WHEN_CAN = _any(r"when can i", r"how long (?:until|till|before|do i have to wait)", r"how much longer")
_DUE_QUESTION = _any(
    r"next (?:pill|dose|one|medication|medicine)",
    r"what is (?:due|next)",
    r"(?:is|are) (?:my|the) (?:\w+ ){0,2}(?:pill|pills|dose|medication|medicine) (?:ready|due)",
    r"schedule\w*",
    r"what (?:do|should|can) i (?:need to |have to )?take",
)
_CONFIRM_SAID = _any(r"(?:i|i have|i just|i already) (?:just |already )?(?:took|taken|swallowed)")
_GREETING = re.compile(r"^(?:hi|hello|hey|good (?:morning|afternoon|evening)|hiya|howdy)(?: there| again)?$")
_THANKS_ONLY = re.compile(
    r"^(?:okay |great |perfect |good )?(?:thank you|thanks|thank|cheers)(?: very much| so much| a lot| you)?$")
_AFFIRMATIVE = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|sure|okay|please|alright|all right|definitely|of course|go ahead|do it|"
    r"please do)(?: please| thanks| thank you| do| go ahead| now)?){1,2}$")
_NEGATIVE = re.compile(r"^(?:no|nope|not now|not yet|no thanks|no thank you|later|maybe later|not today)$")
_WH = frozenset({"what", "when", "which", "where", "who", "why", "how"})
_AUX = frozenset({"did", "do", "does", "have", "has", "had", "is", "was", "are", "were", "should", "am",
                  "can", "could", "shall", "may", "must", "will", "would"})
_SUBJECTS = frozenset({"i", "it", "my", "the", "this", "that", "there", "anything", "something", "we", "you",
                       "everything", "your", "a", "any", "all", "she", "he", "they"})
_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_CONTAINER_REF = re.compile(
    rf"\b(?:containers?|pill|slot|number|box|compartment|bin|tube|chamber|cartridge)\s+(?:number\s+)?{_NUM}\b")
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}
_ORDINAL_REF = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\s+"
    r"(?:container|pill|slot|box|compartment|one)\b")
#: Vosk hears a final "two" as "to" ("give me pill to"); only accepted at the very end.
_ASR_TWO_REF = re.compile(r"\b(?:containers?|pill|slot|number|box|compartment)\s+(?:number\s+)?(?:to|too)$")
#: parse_intent labels of the actuating rules (voice/intents.py).
_DISPENSE_LABELS = frozenset({"dispense", "open", "unlock", "drop", "give me my medication",
                              "i want my medication"})
#: CONFIRM_TAKEN labels that clearly mean "I took it" ("done" / "finished" do not, in a chat).
_TAKEN_LABELS = frozenset({"taken", "took it", "took my medication", "took", "swallowed", "confirm"})
_CONFIRM_LABELS = _TAKEN_LABELS | {"done", "finished"}
#: A "stop" message longer than this is a sentence, not a stop command.
MAX_STOP_WORDS = 4


# --------------------------------------------------------------------------- analysis


@dataclass(frozen=True)
class TextFlags:
    """Deterministic reading of one patient message (see :func:`analyse`)."""

    text: str
    norm: str
    parsed: ParsedIntent
    words: int = 0
    unclear: bool = False
    emergency: bool = False
    injection: bool = False
    stop: bool = False
    question: bool = False
    #: A request verb ("can I have", "give me", "drop") - with a pill word it is a drop request.
    request_verb: bool = False
    drop_request: bool = False
    drop_negated: bool = False
    confirm: bool = False
    confirm_negated: bool = False
    multiple: bool = False
    med_change: bool = False
    symptom: bool = False
    help: bool = False
    greeting: bool = False
    thanks: bool = False
    repeat: bool = False
    affirmative: bool = False
    negative: bool = False
    please: bool = False
    pills_left: bool = False
    last_pill: bool = False
    when_can: bool = False
    due_question: bool = False
    container_refs: tuple[int, ...] = field(default_factory=tuple)


def container_refs(norm: str) -> tuple[int, ...]:
    """Container numbers named in normalised text: "pill 2", "container two", "the third one"."""
    refs: list[int] = []
    for m in _CONTAINER_REF.finditer(norm):
        n = as_int(m.group(1))
        if n is not None and n not in refs:
            refs.append(n)
    for m in _ORDINAL_REF.finditer(norm):
        n = _ORDINALS[m.group(1)]
        if n not in refs:
            refs.append(n)
    if _ASR_TWO_REF.search(norm) and 2 not in refs:
        refs.append(2)
    return tuple(refs)


#: Polite fillers that contain "no" but negate nothing ("no problem, drop my pill").
_POLITE_NO = re.compile(r"\b(?:no problem|no worries|no rush|no hurry)\b", re.IGNORECASE)


def analyse(text: str) -> TextFlags:
    """Read one patient message. Pure and deterministic; never raises."""
    raw = text if isinstance(text, str) else ""
    softened = _POLITE_NO.sub("okay", raw)
    norm = normalise(softened)
    parsed = parse_intent(softened)
    tokens = norm.split()
    words = len(tokens)
    unclear = "[unk]" in raw.lower() or "<unk>" in raw.lower()
    if not norm:
        return TextFlags(text=raw, norm=norm, parsed=parsed, unclear=unclear)
    request_verb = bool(_REQUEST_VERB.search(norm)) and not _INFO_SEEKING.search(norm)
    polite = bool(_POLITE_START.match(norm))
    question = (
        tokens[0] in _WH
        or (words > 1 and tokens[0] in _AUX and tokens[1] in _SUBJECTS and not polite)
        or (raw.rstrip().endswith("?") and not request_verb)
    )
    negated_actuation = parsed.intent is Intent.UNKNOWN and parsed.negated
    drop_negated = (
        (negated_actuation and parsed.matched in _DISPENSE_LABELS)
        or bool(_DROP_NEGATION.search(norm))
        or bool(_NEGATIVE.match(norm))
    )
    drop_request = not question and not drop_negated and (
        parsed.intent is Intent.DISPENSE or (request_verb and bool(_PILL_OBJECT.search(norm))))
    confirm_negated = negated_actuation and (parsed.matched in _CONFIRM_LABELS or parsed.matched == "take")
    confirm = not question and not confirm_negated and (
        (parsed.intent is Intent.CONFIRM_TAKEN and parsed.matched in _TAKEN_LABELS)
        or (bool(_CONFIRM_SAID.search(norm)) and not parsed.negated))
    return TextFlags(
        text=raw,
        norm=norm,
        parsed=parsed,
        words=words,
        unclear=unclear,
        emergency=bool(_EMERGENCY.search(norm)),
        injection=bool(_INJECTION.search(norm)),
        stop=parsed.intent is Intent.CANCEL and words <= MAX_STOP_WORDS,
        question=question,
        request_verb=request_verb,
        drop_request=drop_request,
        drop_negated=drop_negated,
        confirm=confirm,
        confirm_negated=confirm_negated,
        multiple=bool(_MULTIPLE.search(norm)),
        med_change=bool(_MED_CHANGE.search(norm)),
        symptom=bool(_SYMPTOM.search(norm)),
        help=parsed.intent is Intent.HELP,
        greeting=bool(_GREETING.match(norm)),
        thanks=bool(_THANKS_ONLY.match(norm)),
        repeat=parsed.intent is Intent.REPEAT,
        affirmative=bool(_AFFIRMATIVE.match(norm)),
        negative=bool(_NEGATIVE.match(norm)),
        please="please" in tokens,
        pills_left=bool(_PILLS_LEFT.search(norm)),
        last_pill=bool(_LAST_PILL.search(norm)),
        when_can=bool(_WHEN_CAN.search(norm)),
        due_question=parsed.intent is Intent.CHECK_DUE or bool(_DUE_QUESTION.search(norm)),
        container_refs=container_refs(norm),
    )


def turn_guard(flags: TextFlags) -> TurnGuard:
    """Actions the tools must refuse in this turn, whatever a model asks for."""
    block_drop: tuple[str, str] | None = None
    block_confirm: tuple[str, str] | None = None
    if flags.emergency:
        block_drop = block_confirm = ("EMERGENCY", phrases.EMERGENCY)
    elif flags.unclear:
        block_drop = block_confirm = ("UNCLEAR_SPEECH", phrases.UNCLEAR_SPEECH)
    elif flags.injection:
        block_drop = ("INJECTION", phrases.INJECTION_REFUSED)
    elif flags.drop_negated:
        block_drop = ("NEGATED", phrases.NEGATED)
    elif flags.multiple:
        block_drop = ("MULTIPLE", phrases.ONE_PILL_ONLY)
    elif flags.med_change:
        block_drop = ("MEDICATION_CHANGE", phrases.MEDICATION_CHANGE)
    if block_confirm is None and flags.confirm_negated:
        block_confirm = ("NEGATED", phrases.NOT_MARKED)
    return TurnGuard(block_drop=block_drop, block_confirm=block_confirm)


# --------------------------------------------------------------------------- outcome sentences


def describe_outcome(outcome: Mapping[str, Any] | None, *, clock: Clock) -> str:
    """Deterministic sentence for a ``request_pill`` result (DropOutcome dict or a local refusal)."""
    if not outcome:
        return phrases.AGENT_ERROR
    status = str(outcome.get("status") or "")
    reason = str(outcome.get("reason") or "")
    number = as_int(outcome.get("container_number"))
    if number is None:
        slot = as_int(outcome.get("slot"))
        number = None if slot is None else slot + 1
    name = outcome.get("medication_name")
    name = name if isinstance(name, str) and name.strip() else None
    message = str(outcome.get("message") or "").strip()
    if status == "DROPPED":
        return phrases.pill_dropped(name, number, pill_count_after=as_int(outcome.get("pill_count_after")))
    if status == "UNCERTAIN":
        return phrases.DROP_UNCERTAIN
    if status == "FAILED":
        return phrases.no_pill(number) if reason == "NO_PILL" else phrases.DROP_FAILED
    if status == "DENIED":
        if reason == "COOLDOWN":
            nxt = parse_dt(outcome.get("next_allowed_at"))
            remaining = outcome.get("cooldown_remaining_s")
            return phrases.cooldown(clock.to_local(nxt) if nxt else None, clock.local_now(),
                                    float(remaining) if isinstance(remaining, (int, float)) else None)
        simple = {
            "EMPTY": phrases.container_empty(number),
            "ALREADY_SATISFIED": phrases.already_dropped(name),
            "NO_MEDICATION": phrases.no_medication(number),
            "UNKNOWN_MEDICATION": phrases.UNKNOWN_MEDICATION,
            "IN_PROGRESS": phrases.IN_PROGRESS,
            "DEVICE_UNAVAILABLE": phrases.DEVICE_UNAVAILABLE,
            "NEEDS_REVIEW": phrases.NEEDS_REVIEW,
            "NOT_ALLOWED": phrases.NOT_ALLOWED,
            "DB_ERROR": phrases.DB_UNAVAILABLE,
        }
        return simple.get(reason) or message or phrases.NOTHING_DROPPED
    if status == NOT_REQUESTED:
        return message or phrases.WHICH_PILL
    return message or phrases.AGENT_ERROR


# --------------------------------------------------------------------------- the agent


class RulesAgent:
    """Offline deterministic agent: same tools and storage as the Gemini agent."""

    model = MODEL_NAME

    def __init__(self, settings: Settings, clock: Clock) -> None:
        self.settings = settings
        self._clock = clock

    def respond(self, text: str, tools: PatientTools, *,
                history: Sequence[Mapping[str, Any]] = ()) -> str:
        """Reply to ``text``, calling ``tools`` as needed. Never raises (errors -> AGENT_ERROR)."""
        try:
            return self._respond(analyse(text), tools, history)
        except Exception:  # noqa: BLE001 - the offline agent is the last line: always answer
            log.exception("rules agent failed")
            return phrases.AGENT_ERROR

    # ------------------------------------------------------------------ routing
    def _respond(self, f: TextFlags, tools: PatientTools, history: Sequence[Mapping[str, Any]]) -> str:
        if f.emergency:
            return phrases.EMERGENCY
        if not f.norm:
            return phrases.UNCLEAR_SPEECH if f.unclear else phrases.NOT_UNDERSTOOD
        if tools.last_outcome is not None:
            # Fallback after another provider already requested a pill in this turn: report it.
            return describe_outcome(tools.last_outcome, clock=self._clock)
        if tools.drop_requested:
            return phrases.AGENT_ERROR  # a request was sent but its outcome is unknown: never retry
        if f.stop:
            return self._stop(tools)
        if f.injection:
            return phrases.INJECTION_REFUSED
        last_reply = last_assistant_text(history)
        if f.repeat:
            return last_reply or phrases.NOTHING_TO_REPEAT
        if f.help:
            return phrases.HELP
        if f.greeting:
            return phrases.GREETING
        if f.thanks:
            return phrases.WELCOME
        if f.med_change:
            return phrases.MEDICATION_CHANGE
        status = tools.status()
        if status is None:
            return phrases.DB_UNAVAILABLE
        containers = list(status.containers)
        mentions = match_containers(f.norm, containers)
        offered = last_reply.endswith(phrases.OFFER_DROP)
        asked_which = last_reply.startswith(phrases.WHICH_PILL)
        names_pill = bool(mentions or f.container_refs)
        wants_drop = (
            f.drop_request
            or (f.affirmative and offered)
            or (names_pill and not f.question and (f.request_verb or asked_which or offered or f.please))
        )
        if f.pills_left and not f.drop_request:
            return self._pills_left(containers, mentions, f.container_refs)
        if f.last_pill and not wants_drop:
            return self._last_pill(status, tools)
        if f.confirm_negated:
            return phrases.NOT_MARKED
        if f.confirm and not wants_drop:
            return self._confirm(tools, mentions)
        if wants_drop:
            reply = self._drop(f, status, tools, mentions, offered)
            return phrases.join(reply, phrases.SYMPTOMS_NOTE) if f.symptom else reply
        if f.question or f.due_question or f.when_can:
            return self._due_summary(status, when_can=f.when_can)
        if f.negative or f.drop_negated:
            return phrases.NEGATED
        if f.symptom:
            return phrases.SYMPTOMS
        return phrases.NOT_UNDERSTOOD

    # ------------------------------------------------------------------ handlers
    def _stop(self, tools: PatientTools) -> str:
        result = tools.execute(STOP_DEVICE, internal=True)
        if result.get("error"):
            return phrases.DEVICE_NEEDS_ATTENTION
        return phrases.STOPPED if result.get("stopped") else phrases.NOT_MOVING

    def _drop(self, f: TextFlags, status: PatientStatus, tools: PatientTools,
              mentions: list[ContainerInfo], offered: bool) -> str:
        if f.unclear:
            return phrases.UNCLEAR_SPEECH
        if f.drop_negated:
            return phrases.NEGATED
        if f.multiple or len(f.container_refs) > 1:
            return phrases.ONE_PILL_ONLY
        refs = f.container_refs
        if len(mentions) > 1 and not refs:
            strong = [c for c in mentions if name_score(f.norm, c.medication_name) >= 0.95]
            if len(strong) > 1:
                return phrases.ONE_PILL_ONLY  # two medications named
            return phrases.which_pill(medication_options(mentions))
        args: dict[str, Any] = {"reason": " ".join(f.text.split())[:120]}
        if refs:
            args["container_number"] = refs[0]
            if mentions:
                args["medication_name"] = mentions[0].medication_name
        elif mentions:
            args["medication_name"] = mentions[0].medication_name
        else:
            due = due_doses(status, now=self._clock.now(), settings=self.settings)
            if offered and not due:
                return phrases.NOTHING_DUE
            if cooldown_left(status) > 0 and len(medication_options(status.containers)) > 1 and not due:
                return self._cooldown(status)  # any container would be refused: no need to ask which
        return describe_outcome(tools.execute(REQUEST_PILL, args), clock=self._clock)

    def _cooldown(self, status: PatientStatus) -> str:
        return cooldown_sentence(status, clock=self._clock)

    def _confirm(self, tools: PatientTools, mentions: list[ContainerInfo]) -> str:
        args = {"medication_name": mentions[0].medication_name} if len(mentions) == 1 else {}
        result = tools.execute(CONFIRM_PILL_TAKEN, args)
        return str(result.get("message") or phrases.NOTHING_TO_CONFIRM)

    def _pills_left(self, containers: list[ContainerInfo], mentions: list[ContainerInfo],
                    refs: tuple[int, ...]) -> str:
        if not containers:
            return phrases.NO_CONTAINERS
        if refs and not any(c.container_number in refs for c in containers):
            return phrases.no_such_container(refs[0], int(self.settings.num_slots))
        wanted = [c for c in containers if c.container_number in refs] or mentions or containers
        return " ".join(phrases.container_summary(c.container_number, c.medication_name, c.pill_count,
                                                  low_stock=c.low_stock) for c in wanted)

    def _last_pill(self, status: PatientStatus, tools: PatientTools) -> str:
        now_local = self._clock.local_now()
        last = status.last_drop if isinstance(status.last_drop, Mapping) else None
        if last is None or str(last.get("status") or "") not in ("DROPPED", "UNCERTAIN"):
            history = tools.execute(GET_RECENT_DROPS, {"days": 14})
            if history.get("error"):
                return phrases.DB_UNAVAILABLE
            last = next((d for d in tools.last_recent_drops
                         if str(d.get("status") or "") in ("DROPPED", "UNCERTAIN")), None)
            if last is None:
                return phrases.NO_RECENT_DROPS
        at = parse_dt(last.get("completed_at")) or parse_dt(last.get("requested_at"))
        at_local = self._clock.to_local(at) if at else None
        name = last.get("medication_name")
        if str(last.get("status")) == "UNCERTAIN":
            return phrases.last_drop_unconfirmed(name, at_local, now_local)
        if at_local is None:
            return f"Your last pill was {phrases.short_med_name(name)}."
        return phrases.last_pill(name, at_local, now_local)

    def _due_summary(self, status: PatientStatus, *, when_can: bool = False) -> str:
        return status_summary(status, clock=self._clock, settings=self.settings, when_can=when_can)


def cooldown_left(status: PatientStatus) -> int:
    """Seconds of global cooldown left (tolerates a missing value)."""
    try:
        return max(0, int(status.cooldown_remaining_s or 0))
    except (TypeError, ValueError):
        return 0


def cooldown_sentence(status: PatientStatus, *, clock: Clock) -> str:
    """The cooldown refusal for ``status`` with the spoken time of the next allowed drop."""
    nxt = parse_dt(status.next_manual_allowed_at)
    return phrases.cooldown(clock.to_local(nxt) if nxt else None, clock.local_now(),
                            float(cooldown_left(status)))


def status_summary(status: PatientStatus, *, clock: Clock, settings: Settings, when_can: bool = False,
                   offer: bool = True) -> str:
    """What is due now (offering a drop when the cooldown allows it and ``offer``), else
    "Nothing is due right now" + the next scheduled pill. ``when_can`` leads with the cooldown."""
    now_local = clock.local_now()
    cooling = cooldown_left(status) > 0
    lead = ""
    if when_can:
        lead = cooldown_sentence(status, clock=clock) if cooling else phrases.CAN_REQUEST_NOW
    due = due_doses(status, now=clock.now(), settings=settings)
    if due:
        dose = due[0]
        at = parse_dt(dose.get("scheduled_at")) or parse_dt(dose.get("scheduled_local"))
        if at is not None:
            return phrases.join(lead, phrases.due_now(
                dose.get("medication_name"), clock.to_local(at), now_local,
                auto_drop=bool(status.auto_drop_enabled), offer=offer and not cooling and not when_can))
    nxt = status.next_scheduled if isinstance(status.next_scheduled, Mapping) else None
    at = (parse_dt(nxt.get("scheduled_at")) or parse_dt(nxt.get("scheduled_local"))) if nxt else None
    tail = (phrases.next_pill(nxt.get("medication_name"), clock.to_local(at), now_local)
            if nxt and at else phrases.NO_MORE_SCHEDULED)
    return phrases.join(lead or phrases.NOTHING_DUE, tail)


def last_assistant_text(history: Sequence[Mapping[str, Any]]) -> str:
    """Content of the newest assistant message in ``history`` ("" if none)."""
    for msg in reversed(list(history)):
        if msg.get("role") == "assistant":
            return " ".join(str(msg.get("content") or "").split())
    return ""
