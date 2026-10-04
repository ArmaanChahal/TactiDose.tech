"""Configurable urgent-support response.

IMPORTANT: this is a simple phrase match against a configured list of explicit
statements. It is NOT a crisis detector, risk classifier, or monitoring system
and will miss many situations. Crisis resources must come from configuration;
none are hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parsing import normalize

DEFAULT_URGENT_PHRASES: tuple[str, ...] = (
    "i want to die",
    "i want to kill myself",
    "kill myself",
    "end my life",
    "hurt myself",
    "suicide",
    "i am in danger",
    "im in danger",
    "i need emergency help",
)

DEFAULT_URGENT_MESSAGE = (
    "It sounds like you may need help right now. This check-in cannot contact "
    "anyone or monitor your safety. If you are in immediate danger, please contact "
    "your local emergency services or someone near you now."
)

DEFAULT_NO_RESOURCES_MESSAGE = "No crisis contacts have been configured on this device."

DEFAULT_HANDOFF_MESSAGE = (
    "I can't contact anyone myself, but your TactiDose app can help you choose "
    "how to reach someone."
)


@dataclass(frozen=True)
class CrisisResource:
    name: str
    contact: str
    notes: str | None = None


@dataclass(frozen=True)
class SafetyConfig:
    urgent_phrases: tuple[str, ...] = DEFAULT_URGENT_PHRASES
    urgent_message: str = DEFAULT_URGENT_MESSAGE
    no_resources_message: str = DEFAULT_NO_RESOURCES_MESSAGE
    crisis_resources: tuple[CrisisResource, ...] = ()
    support_handoff_message: str = DEFAULT_HANDOFF_MESSAGE
    _normalized: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        phrases = tuple(p for p in (normalize(x) for x in self.urgent_phrases) if p)
        object.__setattr__(self, "_normalized", phrases)

    def is_urgent(self, text: str) -> bool:
        padded = f" {normalize(text)} "
        return any(f" {phrase} " in padded for phrase in self._normalized)

    def urgent_speech(self) -> str:
        if self.crisis_resources:
            listed = " ".join(
                f"{r.name}: {r.contact}." + (f" {r.notes}" if r.notes else "")
                for r in self.crisis_resources
            )
            resources = f"Configured contacts: {listed}"
        else:
            resources = self.no_resources_message
        return f"{self.urgent_message} {resources}"
