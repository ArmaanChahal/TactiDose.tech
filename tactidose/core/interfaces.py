"""Cross-module contracts.

Everything that crosses a module boundary is defined here so that modules can
be built and tested independently (with fakes) and wired together in
``tactidose/app.py``. Keep this file free of heavy imports.

Layering (arrows = "may call")::

    voice/ ──► core/assistant ──► medication/ (DoseServiceAPI) ──► hardware/ (HardwareController)
       ▲              │                     │
    audio/ ◄──────────┘ (Speaker)           └──► db/ (+ outbox -> integrations/snowflake)
    integrations/gemini (LabelExtractor) ◄── medication/onboarding

Rule (handoff §33): AI components (Gemini, speech recognition, TTS) only produce
*data* (text, extracted fields, audio). Only deterministic code in ``medication/``
decides eligibility, and only ``hardware/`` talks to the motor controller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from tactidose.hardware.protocol import (
    CommandResult,
    DeviceState,
    GateState,
    Message,
    compartment_label,
)

# =========================================================================== hardware


@dataclass(frozen=True)
class DeviceSnapshot:
    """Host-side mirror of the device, updated from every received line."""

    mode: str = "none"                    # "sim" | "serial" | "none"
    port: str | None = None
    connected: bool = False
    responsive: bool = False              # answered recently (heartbeat)
    state: DeviceState = DeviceState.UNKNOWN
    homed: bool | None = None
    slot: int | None = None               # slot at the gate, None = unknown/between
    target_slot: int | None = None        # destination while MOVING
    gate: GateState = GateState.UNKNOWN
    in_flight: str | None = None          # command line currently awaiting a reply
    last_error: str | None = None         # last ERR code / host failure code
    fw_version: str | None = None
    num_slots_reported: int | None = None
    last_rx_age_s: float | None = None    # seconds since last line from the device
    resets_seen: int = 0                  # EVENT BOOT count since start

    @property
    def ready_for_motion(self) -> bool:
        return self.connected and self.state is DeviceState.READY and self.homed is True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["gate"] = self.gate.value
        d["ready_for_motion"] = self.ready_for_motion
        d["compartment_label"] = compartment_label(self.slot) if self.slot is not None else None
        return d


@runtime_checkable
class HardwareController(Protocol):
    """Deterministic motor-controller client (serial or simulator).

    All command methods block until a terminal message, timeout or disconnect and
    never raise for device-level failures — they return a ``CommandResult``.
    At most one normal command runs at a time (others get ``HostCode.BUSY_LOCAL``);
    ``stop()`` bypasses that lock and may be called from any thread.
    """

    num_slots: int

    def start(self) -> None: ...
    def close(self) -> None: ...
    def snapshot(self) -> DeviceSnapshot: ...

    def ping(self) -> CommandResult: ...
    def status(self) -> CommandResult: ...
    def home(self) -> CommandResult: ...
    def move_slot(self, slot: int) -> CommandResult: ...
    def dispense_slot(self, slot: int) -> CommandResult: ...
    def open_gate(self) -> CommandResult: ...
    def close_gate(self) -> CommandResult: ...
    def stop(self) -> CommandResult: ...

    def send_raw(self, line: str) -> CommandResult:
        """Demo console: parse ``line`` with ``protocol.parse_command`` and send it."""
        ...

    def add_event_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        """Receive every ``EVENT …`` message (button presses, boot). Returns an unsubscribe fn.
        Callbacks run on the reader thread: they must be quick and must not call
        blocking command methods (enqueue work instead)."""
        ...


# =========================================================================== intents


class Intent(str, Enum):
    CHECK_DUE = "CHECK_DUE"            # "What do I take now?"
    DISPENSE = "DISPENSE"              # "Dispense."
    CONFIRM_TAKEN = "CONFIRM_TAKEN"    # "Taken."
    REPEAT = "REPEAT"                  # "Repeat."
    CANCEL = "CANCEL"                  # "Cancel." / "Stop."
    HELP = "HELP"                      # "Help."
    #: The big tactile button / kiosk primary button: context-sensitive
    #: (confirm if a dose awaits confirmation, else dispense if due, else check).
    PRIMARY_ACTION = "PRIMARY_ACTION"
    #: Internal: gate has been open too long without confirmation.
    GATE_TIMEOUT = "GATE_TIMEOUT"
    UNKNOWN = "UNKNOWN"


class IntentSource(str, Enum):
    VOICE = "voice"
    BUTTON = "button"        # physical button on the ESP32
    UI = "ui"                # kiosk / touchscreen
    KEYBOARD = "keyboard"    # demo operator typed text
    API = "api"
    SYSTEM = "system"        # timers, startup


@dataclass(frozen=True)
class ParsedIntent:
    intent: Intent
    text: str = ""
    confidence: float = 1.0
    matched: str | None = None     # the phrase/keyword that matched (debugging)
    negated: bool = False          # e.g. "I have not taken it" -> UNKNOWN with negated=True


# =========================================================================== speech output


@runtime_checkable
class Speaker(Protocol):
    """Speaks text and publishes captions (``Topic.SPOKEN``) for the UI.

    ``say`` is non-blocking (queued, played in order). ``interrupt=True`` drops
    anything queued and cuts off current playback (used for errors / cancel).
    ``is_speaking`` lets the recognizer mute the microphone (no self-hearing).
    """

    def start(self) -> None: ...
    def close(self) -> None: ...
    def say(self, text: str, *, kind: str = "info", interrupt: bool = False,
            meta: dict[str, Any] | None = None) -> None: ...
    def wait_idle(self, timeout: float | None = None) -> bool: ...
    @property
    def is_speaking(self) -> bool: ...


# =========================================================================== label onboarding


class LabelExtraction(BaseModel):
    """Structured output requested from Gemini (handoff §18 schema + ``legible``).

    Transcription only: every field must be text *visibly printed* on the label.
    Empty string / empty list when not visible. Never inferred.
    """

    medication_name: str = Field("", description="Medication or product name exactly as printed.")
    strength: str = Field("", description="Strength exactly as printed, e.g. '500 mg'. Empty if not visible.")
    visible_instructions: str = Field("", description="Directions exactly as printed. Empty if not visible.")
    warnings_visible: list[str] = Field(default_factory=list, description="Warning texts exactly as printed.")
    confidence_notes: str = Field("", description="What was hard to read, cut off, blurry or ambiguous.")
    legible: bool = Field(True, description="False if no medication label is visible or it cannot be read.")


@dataclass(frozen=True)
class ExtractionResult:
    ok: bool
    data: LabelExtraction | None = None
    model: str = ""
    error: str | None = None            # short machine-ish reason ("timeout", "blocked", "unreadable")
    user_message: str | None = None     # shown in the UI on failure
    raw_text: str | None = None         # raw model text, for audit

    COULD_NOT_READ = (
        "Could not reliably read label. Please enter or verify information manually."
    )


@runtime_checkable
class LabelExtractor(Protocol):
    name: str

    def extract(self, image: bytes, mime_type: str) -> ExtractionResult: ...


# =========================================================================== domain outcomes


@dataclass(frozen=True)
class DoseInfo:
    """Everything the assistant/UI needs to talk about one dose event."""

    event_id: int
    medication_id: int
    medication_name: str
    strength: str | None
    instructions: str | None
    slot: int | None
    scheduled_at: datetime            # aware UTC
    scheduled_local: datetime         # aware local
    status: str
    dispensed_at: datetime | None = None
    confirmed_taken_at: datetime | None = None
    needs_review: bool = False
    attempts: int = 0
    hardware_result: str | None = None

    @property
    def label(self) -> str:
        return f"dose_{self.event_id}"

    @property
    def compartment(self) -> str:
        return compartment_label(self.slot)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("scheduled_at", "scheduled_local", "dispensed_at", "confirmed_taken_at"):
            v = d.get(k)
            d[k] = v.isoformat() if v else None
        d["label"] = self.label
        d["compartment"] = self.compartment
        d["compartment_number"] = None if self.slot is None else self.slot + 1
        return d


class BlockReason(str, Enum):
    NEEDS_REVIEW = "NEEDS_REVIEW"              # uncertain outcome / attempts exhausted
    NO_COMPARTMENT = "NO_COMPARTMENT"          # medication not assigned to a slot
    UNCONFIRMED_MEDICATION = "UNCONFIRMED_MEDICATION"
    INACTIVE = "INACTIVE"                      # schedule or medication deactivated
    TOO_SOON = "TOO_SOON"                      # same medication accessed < min interval ago
    IN_PROGRESS = "IN_PROGRESS"                # this dose is DISPENSING


@dataclass(frozen=True)
class DueSummary:
    now_local: datetime
    due: tuple[DoseInfo, ...] = ()                      # dispensable now, in dispense order
    awaiting_confirmation: tuple[DoseInfo, ...] = ()    # DISPENSED, confirmable, not TAKEN
    accessed: tuple[DoseInfo, ...] = ()                 # in window and DISPENSED/TAKEN
    blocked: tuple[tuple[DoseInfo, BlockReason], ...] = ()
    next_upcoming: DoseInfo | None = None               # next SCHEDULED dose after now

    def to_dict(self) -> dict[str, Any]:
        return {
            "now_local": self.now_local.isoformat(),
            "due": [d.to_dict() for d in self.due],
            "awaiting_confirmation": [d.to_dict() for d in self.awaiting_confirmation],
            "accessed": [d.to_dict() for d in self.accessed],
            "blocked": [{"dose": d.to_dict(), "reason": r.value} for d, r in self.blocked],
            "next_upcoming": self.next_upcoming.to_dict() if self.next_upcoming else None,
        }


class DispenseStatus(str, Enum):
    DISPENSED = "DISPENSED"                    # gate open, dose accessible
    DUPLICATE = "DUPLICATE"                    # already accessed -> no motor command
    NOTHING_DUE = "NOTHING_DUE"
    IN_PROGRESS = "IN_PROGRESS"                # another dispense is running
    BLOCKED = "BLOCKED"                        # in-window dose(s) blocked; see reason
    HARDWARE_UNAVAILABLE = "HARDWARE_UNAVAILABLE"  # disconnected/fault/could not home; no dose state change
    HARDWARE_ERROR = "HARDWARE_ERROR"          # dispense command failed
    CANCELLED = "CANCELLED"                    # stopped by user while preparing; dose still due
    DB_ERROR = "DB_ERROR"                      # state unknown -> fail closed, no motor command


@dataclass(frozen=True)
class DispenseOutcome:
    status: DispenseStatus
    dose: DoseInfo | None = None
    reason: str = ""                           # BlockReason value or hardware code
    hardware: CommandResult | None = None
    remaining_due: int = 0                     # how many more doses are dispensable now
    next_upcoming: DoseInfo | None = None      # filled for NOTHING_DUE

    @property
    def uncertain(self) -> bool:
        return bool(self.hardware and self.hardware.gate_may_be_open)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "dose": self.dose.to_dict() if self.dose else None,
            "reason": self.reason,
            "hardware": self.hardware.hardware_result if self.hardware else None,
            "uncertain": self.uncertain,
            "remaining_due": self.remaining_due,
            "next_upcoming": self.next_upcoming.to_dict() if self.next_upcoming else None,
        }


class ConfirmStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
    NOTHING_TO_CONFIRM = "NOTHING_TO_CONFIRM"
    DB_ERROR = "DB_ERROR"


@dataclass(frozen=True)
class ConfirmOutcome:
    status: ConfirmStatus
    dose: DoseInfo | None = None
    gate_closed: bool | None = None            # None = gate was not open / not attempted
    hardware: CommandResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "dose": self.dose.to_dict() if self.dose else None,
            "gate_closed": self.gate_closed,
            "hardware": self.hardware.hardware_result if self.hardware else None,
        }


class CancelStatus(str, Enum):
    STOPPED_MOTION = "STOPPED_MOTION"          # STOP sent while moving/homing
    CLOSED_GATE = "CLOSED_GATE"
    NOTHING_TO_CANCEL = "NOTHING_TO_CANCEL"
    FAILED = "FAILED"                          # could not stop/close -> ask for assistance


@dataclass(frozen=True)
class CancelOutcome:
    status: CancelStatus
    dose: DoseInfo | None = None
    hardware: CommandResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "dose": self.dose.to_dict() if self.dose else None,
            "hardware": self.hardware.hardware_result if self.hardware else None,
        }


@runtime_checkable
class DoseServiceAPI(Protocol):
    """Deterministic dose logic used by the assistant (implemented in medication/dispense.py).

    Every method is safe to call from any thread, serialises hardware access
    internally, and never raises for expected failures (DB/hardware problems are
    reported through the outcome's status — fail closed).
    """

    def check_due(self) -> DueSummary: ...

    def dispense_next(
        self,
        source: IntentSource,
        *,
        on_motion_start: Callable[[DoseInfo], None] | None = None,
    ) -> DispenseOutcome:
        """Dispense the next eligible dose. ``on_motion_start`` is called (on the calling
        thread) right before ``DISPENSE_SLOT`` is sent, so the assistant can warn
        "keep your hands clear". It is not called when nothing will move."""
        ...

    def confirm_taken(self, source: IntentSource) -> ConfirmOutcome: ...
    def cancel(self, source: IntentSource) -> CancelOutcome: ...

    def interrupt(self, source: IntentSource) -> bool:
        """Send STOP *immediately* if carousel motion/homing is in flight (bypasses all locks;
        safe while another thread is blocked in ``dispense_next``). Returns True if STOP was sent."""
        ...

    def awaiting_confirmation(self) -> DoseInfo | None: ...

    def close_gate(self, reason: str) -> CommandResult | None:
        """Close the gate if it is (or may be) open. Returns None if nothing was sent."""
        ...


# =========================================================================== assistant replies


class ReplyKind(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"        # refusals: duplicate, nothing due, blocked
    ERROR = "error"            # hardware/db problems: "please ask for assistance"
    PROMPT = "prompt"          # asks the user to do something next


@dataclass
class Reply:
    """What the assistant said/did in response to one intent."""

    intent: Intent
    source: IntentSource
    text: str
    kind: ReplyKind = ReplyKind.INFO
    outcome: dict[str, Any] = field(default_factory=dict)
    spoken: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "source": self.source.value,
            "text": self.text,
            "kind": self.kind.value,
            "outcome": self.outcome,
            "spoken": self.spoken,
        }
