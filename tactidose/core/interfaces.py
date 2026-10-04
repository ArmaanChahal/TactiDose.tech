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
    #: v1.1: protocol version from STATUS (None = v1 firmware, no DROP_SLOT) and drop sensor presence.
    proto: str | None = None
    drop_sensor: bool | None = None

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

    def drop_slot(self, slot: int) -> CommandResult:
        """v1.1: drop one pill from container ``slot``. Sends ``DROP_SLOT n`` when the device
        reports ``proto >= 1.1``; otherwise emulates it with ``DISPENSE_SLOT n`` + wait
        ``drop_close_delay_ms`` + ``CLOSE_GATE`` and returns a result whose
        ``protocol.drop_certainty`` is meaningful either way."""
        ...

    def reconnect(self) -> bool:
        """Drop the link and reconnect now. False if refused (a command is in flight,
        not started, closed, or hardware_mode='none')."""
        ...

    def send_raw(self, line: str) -> CommandResult:
        """Demo console: parse ``line`` with ``protocol.parse_command`` and send it."""
        ...

    def add_event_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        """Receive every ``EVENT …`` message (button presses, boot) plus unsolicited
        ``ERR HOME_TIMEOUT`` / ``ERR MOTOR_FAULT`` that did not terminate an in-flight
        command (``msg.kind is MessageKind.ERR``). Returns an unsubscribe fn.
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
    #: Internal: device/system announcement ("The device restarted", "needs attention").
    NOTICE = "NOTICE"
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
    """Outcome of one label extraction. Consumers must branch on ``ok``.

    ``data`` may also be set when ``ok`` is False (``error == "unreadable"``) — for audit only,
    never to be treated as a successful read. ``error`` vocabulary: timeout | network | blocked |
    invalid_response | unreadable | invalid_image | api_error:<http code|unknown|no_api_key|sdk_missing>.
    """

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
    #: Set (e.g. "DB_ERROR") when the schedule could not be read: callers must fail closed
    #: and must NOT report "nothing due".
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "now_local": self.now_local.isoformat(),
            "due": [d.to_dict() for d in self.due],
            "awaiting_confirmation": [d.to_dict() for d in self.awaiting_confirmation],
            "accessed": [d.to_dict() for d in self.accessed],
            "blocked": [{"dose": d.to_dict(), "reason": r.value} for d, r in self.blocked],
            "next_upcoming": self.next_upcoming.to_dict() if self.next_upcoming else None,
        }
        if self.error:
            out["error"] = self.error
        return out


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
        """Dispense the next eligible dose. ``on_motion_start`` is called once, on the calling
        thread, right before the first carousel motion (a preparatory ``HOME`` or the
        ``DISPENSE_SLOT``), so the assistant can warn "keep your hands clear". It is not
        called when nothing will move."""
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


# =========================================================================== v2: drops, inventory, agent, reports, auth
#
# v2 product structure (2026-10-03): pills DROP from 3 containers; scheduled doses drop
# automatically; one global cooldown blocks repeated manual/agent drops; an LLM agent talks
# with the patient (voice + text) and may *request* a drop; PDF reports; patient vs
# doctor/family portals. Deterministic rules in DropService have the final say on every drop.


@dataclass(frozen=True)
class ContainerInfo:
    """One container (slot) of the patient's device with its inventory."""

    slot: int
    compartment_id: int
    medication_id: int | None
    medication_name: str | None
    strength: str | None
    pill_count: int
    capacity: int
    low_stock_threshold: int
    loaded_at: datetime | None = None

    @property
    def container_number(self) -> int:
        return self.slot + 1

    @property
    def empty(self) -> bool:
        return self.pill_count <= 0

    @property
    def low_stock(self) -> bool:
        return 0 < self.pill_count <= self.low_stock_threshold

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["loaded_at"] = self.loaded_at.isoformat() if self.loaded_at else None
        d.update(container_number=self.container_number, empty=self.empty, low_stock=self.low_stock)
        return d


@dataclass(frozen=True)
class DropOutcome:
    """Result of one drop request (manual, agent, schedule, button or demo).

    ``status``: DROPPED | DENIED | FAILED | UNCERTAIN (db.models.DropStatus values).
    ``reason``: a db.models.DenyReason value for DENIED, a hardware code for FAILED/UNCERTAIN.
    ``message``: deterministic, user-facing sentence (the agent may rephrase but must not contradict it).
    """

    status: str
    source: str
    message: str
    reason: str | None = None
    drop_id: int | None = None
    slot: int | None = None
    medication_id: int | None = None
    medication_name: str | None = None
    pill_count_after: int | None = None
    cooldown_remaining_s: int = 0
    next_allowed_at: datetime | None = None
    hardware: CommandResult | None = None

    @property
    def dropped(self) -> bool:
        return self.status == "DROPPED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "drop_id": self.drop_id,
            "slot": self.slot,
            "container_number": None if self.slot is None else self.slot + 1,
            "medication_id": self.medication_id,
            "medication_name": self.medication_name,
            "source": self.source,
            "pill_count_after": self.pill_count_after,
            "cooldown_remaining_s": self.cooldown_remaining_s,
            "next_allowed_at": self.next_allowed_at.isoformat() if self.next_allowed_at else None,
            "hardware": self.hardware.hardware_result if self.hardware else None,
        }


@dataclass(frozen=True)
class PatientStatus:
    """Everything the agent and the portals need to describe the patient's situation now."""

    patient_id: int
    display_name: str
    now_local: datetime
    containers: tuple[ContainerInfo, ...] = ()
    cooldown_minutes: int = 0
    cooldown_remaining_s: int = 0
    next_manual_allowed_at: datetime | None = None
    last_drop: dict[str, Any] | None = None          # PillDropView
    today: tuple[dict[str, Any], ...] = ()            # DoseView dicts for today's local date
    next_scheduled: dict[str, Any] | None = None      # DoseView
    auto_drop_enabled: bool = True
    device: dict[str, Any] = field(default_factory=dict)
    alerts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "patient_id": self.patient_id,
            "display_name": self.display_name,
            "now_local": self.now_local.isoformat(),
            "containers": [c.to_dict() for c in self.containers],
            "cooldown_minutes": self.cooldown_minutes,
            "cooldown_remaining_s": self.cooldown_remaining_s,
            "next_manual_allowed_at": self.next_manual_allowed_at.isoformat() if self.next_manual_allowed_at else None,
            "last_drop": self.last_drop,
            "today": list(self.today),
            "next_scheduled": self.next_scheduled,
            "auto_drop_enabled": self.auto_drop_enabled,
            "device": self.device,
            "alerts": list(self.alerts),
        }


@runtime_checkable
class DropServiceAPI(Protocol):
    """Deterministic drop logic (medication/drops.py). Never raises for expected failures.

    Rules applied to every request (ARCHITECTURE v2 §5): the patient has a device; the slot
    holds an active, confirmed medication; pill_count > 0; global cooldown for
    manual/agent/button sources; scheduled doses not already satisfied; device ready; one drop
    at a time (lock + DB claim); uncertain hardware outcomes fail closed (UNCERTAIN, needs review).
    """

    def request_drop(
        self,
        *,
        patient_id: int,
        source: str,
        slot: int | None = None,
        medication_id: int | None = None,
        requested_by_user_id: int | None = None,
        conversation_id: int | None = None,
        dose_event_id: int | None = None,
    ) -> DropOutcome: ...

    def patient_status(self, patient_id: int) -> PatientStatus: ...

    def recent_drops(self, patient_id: int, *, days: int = 7, limit: int = 200) -> list[dict[str, Any]]: ...

    def run_scheduled_drops(self) -> int:
        """Drop every scheduled dose whose time has come and that is not yet satisfied.
        Called by the scheduler loop; returns the number of drop attempts made."""
        ...

    def interrupt(self) -> bool:
        """Send STOP immediately if a drop/motion is in flight (bypasses locks)."""
        ...


@runtime_checkable
class NotificationServiceAPI(Protocol):
    def notify(
        self,
        *,
        patient_id: int,
        kind: str,
        title: str,
        body: str = "",
        data: dict[str, Any] | None = None,
        to_patient: bool = True,
        to_caregivers: bool = True,
    ) -> list[int]:
        """Store one Notification per recipient, publish ``Topic.NOTIFICATION``; returns ids."""
        ...

    def list_for_user(self, user_id: int, *, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]: ...

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int: ...


@dataclass
class AgentReply:
    conversation_id: int
    text: str
    model: str                                   # e.g. "gemini-3.8-flash" or "rules"
    actions: list[dict[str, Any]] = field(default_factory=list)   # DropOutcome.to_dict() per drop attempt
    messages: list[dict[str, Any]] = field(default_factory=list)  # messages stored this turn
    audio_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "text": self.text,
            "model": self.model,
            "actions": self.actions,
            "messages": self.messages,
            "audio_url": self.audio_url,
        }


@runtime_checkable
class AgentServiceAPI(Protocol):
    """Conversational agent for the *patient*. Every turn is persisted (patients only)."""

    def chat(
        self,
        *,
        patient_id: int,
        text: str,
        input_mode: str = "text",
        conversation_id: int | None = None,
    ) -> AgentReply: ...

    def conversations(self, patient_id: int, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def messages(self, patient_id: int, conversation_id: int) -> list[dict[str, Any]]: ...


@runtime_checkable
class ReportServiceAPI(Protocol):
    def generate(self, *, patient_id: int, days: int, created_by_user_id: int) -> dict[str, Any]: ...
    def list(self, patient_id: int) -> list[dict[str, Any]]: ...
    def get(self, report_id: int) -> dict[str, Any]: ...
    def pdf_bytes(self, report_id: int) -> bytes: ...
    def send(self, report_id: int, *, sent_by_user_id: int, to_email: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AuthUser:
    user_id: int
    display_name: str
    role: str                       # db.models.Role value
    email: str | None = None

    @property
    def is_patient(self) -> bool:
        return self.role == "patient"

    @property
    def is_caregiver(self) -> bool:
        return self.role in ("doctor", "family")

    def to_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "display_name": self.display_name, "role": self.role, "email": self.email}


@runtime_checkable
class AuthServiceAPI(Protocol):
    def register(self, *, email: str, password: str, display_name: str, role: str,
                 phone: str | None = None) -> AuthUser: ...
    def login(self, email: str, password: str, *, user_agent: str | None = None) -> tuple[AuthUser, str]: ...
    def resolve(self, token: str) -> AuthUser | None: ...
    def logout(self, token: str) -> None: ...
    def link_patient(self, *, caregiver: AuthUser, patient_id: int, link_code: str) -> dict[str, Any]: ...
    def unlink_patient(self, *, caregiver: AuthUser, patient_id: int) -> None: ...
    def can_view(self, user: AuthUser, patient_id: int) -> bool: ...
    def can_edit(self, user: AuthUser, patient_id: int) -> bool: ...
    def linked_patient_ids(self, user: AuthUser) -> list[int]: ...
    def caregiver_ids(self, patient_id: int) -> list[int]: ...
