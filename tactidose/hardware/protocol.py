"""TactiDose host <-> ESP32 serial protocol, v1.

This module is the host-side single source of truth for the wire format.
The normative description lives in ``docs/SERIAL_PROTOCOL.md``; the shared
conformance scenarios live in ``tactidose/hardware/conformance.json``.

It is deliberately dependency-free and pure (no I/O) so that the serial client,
the ESP32 simulator, the conformance runner and the tests can all share it.

Vocabulary
----------
* **Command**  – a line the host sends (``DISPENSE_SLOT 3``).
* **Message**  – a line the device sends (``OK GATE_OPEN``, ``ERR BUSY``,
  ``EVENT CONFIRM_BUTTON``). Lines that are not messages are *noise*.
* **Disposition** – how a message relates to the command currently in flight
  (progress / success / failure / unrelated). See :func:`classify`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

PROTOCOL_VERSION = 1
DEFAULT_BAUD = 115200
MAX_LINE_LENGTH = 64
DEFAULT_NUM_SLOTS = 6
MIN_SLOTS = 2
MAX_SLOTS = 12
MAX_SLOT_DIGITS = 3

__all__ = [
    "PROTOCOL_VERSION", "DEFAULT_BAUD", "MAX_LINE_LENGTH", "DEFAULT_NUM_SLOTS",
    "MIN_SLOTS", "MAX_SLOTS",
    "DeviceState", "GateState", "CommandName", "MessageKind", "Ok", "Err", "Ev",
    "HostCode", "Disposition", "ProtocolError",
    "Command", "ParsedCommand", "Message", "StatusReport", "CommandResult",
    "parse_command", "parse_message", "format_message", "classify",
    "DEFAULT_TIMEOUTS_S", "BUSY_STATES", "UNHOMED_STATES", "GATE_OPENING_COMMANDS",
    "LONG_RUNNING_COMMANDS", "compartment_number", "compartment_label",
]


# --------------------------------------------------------------------------- enums


class DeviceState(str, Enum):
    """Firmware state machine states (docs §3). ``UNKNOWN`` is host-side only."""

    BOOT = "BOOT"
    HOMING = "HOMING"
    READY = "READY"
    MOVING = "MOVING"
    AT_TARGET = "AT_TARGET"
    GATE_OPEN = "GATE_OPEN"
    SAFE_STOP = "SAFE_STOP"
    FAULT = "FAULT"
    UNKNOWN = "UNKNOWN"

    @property
    def busy(self) -> bool:
        return self in BUSY_STATES

    @classmethod
    def parse(cls, text: str | None) -> "DeviceState":
        if not text:
            return cls.UNKNOWN
        try:
            return cls(text.strip().upper())
        except ValueError:
            return cls.UNKNOWN


BUSY_STATES = frozenset({DeviceState.HOMING, DeviceState.MOVING, DeviceState.AT_TARGET})
UNHOMED_STATES = frozenset(
    {DeviceState.BOOT, DeviceState.HOMING, DeviceState.SAFE_STOP, DeviceState.FAULT}
)


class GateState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, text: str | None) -> "GateState":
        if not text:
            return cls.UNKNOWN
        try:
            return cls(text.strip().upper())
        except ValueError:
            return cls.UNKNOWN


class CommandName(str, Enum):
    PING = "PING"
    STATUS = "STATUS"
    HOME = "HOME"
    MOVE_SLOT = "MOVE_SLOT"
    DISPENSE_SLOT = "DISPENSE_SLOT"
    OPEN_GATE = "OPEN_GATE"
    CLOSE_GATE = "CLOSE_GATE"
    STOP = "STOP"


SLOT_COMMANDS = frozenset({CommandName.MOVE_SLOT, CommandName.DISPENSE_SLOT})
#: Commands whose (possibly uncertain) execution could leave the gate open.
GATE_OPENING_COMMANDS = frozenset({CommandName.DISPENSE_SLOT, CommandName.OPEN_GATE})
#: Commands that run over time on the device and can be interrupted (``ERR STOPPED``).
LONG_RUNNING_COMMANDS = frozenset(
    {CommandName.HOME, CommandName.MOVE_SLOT, CommandName.DISPENSE_SLOT}
)


class MessageKind(str, Enum):
    OK = "OK"
    ERR = "ERR"
    EVENT = "EVENT"


class Ok(str, Enum):
    """Codes that follow ``OK``."""

    PONG = "PONG"
    STATUS = "STATUS"
    HOMING = "HOMING"
    HOMED = "HOMED"
    READY = "READY"
    MOVING = "MOVING"
    AT_SLOT = "AT_SLOT"
    GATE_OPEN = "GATE_OPEN"
    GATE_CLOSED = "GATE_CLOSED"
    STOPPED = "STOPPED"


class Err(str, Enum):
    """Codes that follow ``ERR``."""

    INVALID_SLOT = "INVALID_SLOT"
    NOT_HOMED = "NOT_HOMED"
    BUSY = "BUSY"
    HOME_TIMEOUT = "HOME_TIMEOUT"
    MOTOR_FAULT = "MOTOR_FAULT"
    INVALID_STATE = "INVALID_STATE"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    STOPPED = "STOPPED"


class Ev(str, Enum):
    """Codes that follow ``EVENT``."""

    CONFIRM_BUTTON = "CONFIRM_BUTTON"
    CANCEL_BUTTON = "CANCEL_BUTTON"
    BOOT = "BOOT"


class HostCode(str, Enum):
    """Synthetic, host-side result codes. Never sent on the wire."""

    TIMEOUT = "TIMEOUT"                # no terminal message in time (outcome uncertain)
    DISCONNECTED = "DISCONNECTED"      # link dropped after the command was written (uncertain)
    NOT_CONNECTED = "NOT_CONNECTED"    # command was never written (definitive no-op)
    DEVICE_RESET = "DEVICE_RESET"      # EVENT BOOT seen while in flight (gate closes on boot)
    BUSY_LOCAL = "BUSY_LOCAL"          # host refused: another command already in flight
    INVALID_ARGUMENT = "INVALID_ARGUMENT"  # host refused to build/send the command


class Disposition(str, Enum):
    PROGRESS = "PROGRESS"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNRELATED = "UNRELATED"


class ProtocolError(ValueError):
    """Raised when a command cannot be built or a line cannot be interpreted."""


# --------------------------------------------------------------------------- timeouts

#: Default per-command timeouts in seconds (docs §9.3). The client may override.
DEFAULT_TIMEOUTS_S: Mapping[CommandName, float] = {
    CommandName.PING: 2.0,
    CommandName.STATUS: 2.0,
    CommandName.HOME: 45.0,
    CommandName.MOVE_SLOT: 20.0,
    CommandName.DISPENSE_SLOT: 25.0,
    CommandName.OPEN_GATE: 5.0,
    CommandName.CLOSE_GATE: 5.0,
    CommandName.STOP: 3.0,
}


# --------------------------------------------------------------------------- helpers


def compartment_number(slot: int) -> int:
    """User-facing compartment number for a protocol slot (slot 0 -> compartment 1)."""
    return int(slot) + 1


def compartment_label(slot: int | None) -> str:
    """Human label used in speech and the UI, e.g. ``"compartment 4"`` for slot 3."""
    if slot is None:
        return "an unassigned compartment"
    return f"compartment {compartment_number(slot)}"


def _normalise(line: str) -> str:
    return " ".join(line.strip().split())


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True)
class Command:
    """A host -> device command. Build with the factory helpers."""

    name: CommandName
    slot: int | None = None

    def __post_init__(self) -> None:
        if self.name in SLOT_COMMANDS:
            if self.slot is None or isinstance(self.slot, bool) or not isinstance(self.slot, int):
                raise ProtocolError(f"{self.name.value} requires an integer slot")
            if self.slot < 0:
                raise ProtocolError(f"slot must be >= 0, got {self.slot}")
        elif self.slot is not None:
            raise ProtocolError(f"{self.name.value} takes no slot argument")

    # -- encoding -------------------------------------------------------------
    def to_line(self) -> str:
        if self.slot is None:
            return self.name.value
        return f"{self.name.value} {self.slot}"

    def encode(self) -> bytes:
        return (self.to_line() + "\n").encode("ascii")

    @property
    def default_timeout_s(self) -> float:
        return DEFAULT_TIMEOUTS_S[self.name]

    @property
    def may_open_gate(self) -> bool:
        return self.name in GATE_OPENING_COMMANDS

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.to_line()

    # -- factories ------------------------------------------------------------
    @staticmethod
    def _check_slot(slot: int, num_slots: int) -> int:
        if not (MIN_SLOTS <= num_slots <= MAX_SLOTS):
            raise ProtocolError(f"num_slots must be in [{MIN_SLOTS}, {MAX_SLOTS}]")
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise ProtocolError(f"slot must be an int, got {slot!r}")
        if not (0 <= slot < num_slots):
            raise ProtocolError(f"slot {slot} out of range 0..{num_slots - 1}")
        return slot

    @classmethod
    def ping(cls) -> "Command":
        return cls(CommandName.PING)

    @classmethod
    def status(cls) -> "Command":
        return cls(CommandName.STATUS)

    @classmethod
    def home(cls) -> "Command":
        return cls(CommandName.HOME)

    @classmethod
    def move_slot(cls, slot: int, num_slots: int = DEFAULT_NUM_SLOTS) -> "Command":
        return cls(CommandName.MOVE_SLOT, cls._check_slot(slot, num_slots))

    @classmethod
    def dispense_slot(cls, slot: int, num_slots: int = DEFAULT_NUM_SLOTS) -> "Command":
        return cls(CommandName.DISPENSE_SLOT, cls._check_slot(slot, num_slots))

    @classmethod
    def open_gate(cls) -> "Command":
        return cls(CommandName.OPEN_GATE)

    @classmethod
    def close_gate(cls) -> "Command":
        return cls(CommandName.CLOSE_GATE)

    @classmethod
    def stop(cls) -> "Command":
        return cls(CommandName.STOP)


@dataclass(frozen=True)
class ParsedCommand:
    """Result of device-side parsing of one input line (used by the simulator).

    Exactly one of these holds:
      * ``empty``            – blank line, ignore silently;
      * ``command`` set      – valid command;
      * ``error`` set        – reply ``ERR <error>`` (``UNKNOWN_COMMAND`` or ``INVALID_SLOT``).
    ``name`` is set whenever the command word was recognised (even with a bad slot).
    """

    command: Command | None = None
    error: Err | None = None
    name: CommandName | None = None
    empty: bool = False


_DIGITS = re.compile(r"^[0-9]+$")


def parse_command(line: str, num_slots: int = DEFAULT_NUM_SLOTS) -> ParsedCommand:
    """Parse a host line exactly as the firmware must (docs §1, §7).

    * case-insensitive, surrounding/repeated whitespace ignored;
    * lines longer than :data:`MAX_LINE_LENGTH` -> ``ERR UNKNOWN_COMMAND``;
    * slot argument: exactly one token of 1–3 ASCII digits, value < ``num_slots``,
      otherwise ``ERR INVALID_SLOT`` (validated before any state checks).
    """
    raw = line.rstrip("\r\n")
    if len(raw) > MAX_LINE_LENGTH:
        return ParsedCommand(error=Err.UNKNOWN_COMMAND)
    text = _normalise(raw)
    if not text:
        return ParsedCommand(empty=True)
    tokens = text.upper().split(" ")
    try:
        name = CommandName(tokens[0])
    except ValueError:
        return ParsedCommand(error=Err.UNKNOWN_COMMAND)
    args = tokens[1:]
    if name in SLOT_COMMANDS:
        if len(args) != 1 or not _DIGITS.match(args[0]) or len(args[0]) > MAX_SLOT_DIGITS:
            return ParsedCommand(error=Err.INVALID_SLOT, name=name)
        slot = int(args[0])
        if slot >= num_slots:
            return ParsedCommand(error=Err.INVALID_SLOT, name=name)
        return ParsedCommand(command=Command(name, slot), name=name)
    if args:
        return ParsedCommand(error=Err.UNKNOWN_COMMAND, name=name)
    return ParsedCommand(command=Command(name), name=name)


# --------------------------------------------------------------------------- messages


@dataclass(frozen=True)
class Message:
    """A device -> host line that starts with OK / ERR / EVENT."""

    kind: MessageKind
    code: str
    args: tuple[str, ...] = ()
    raw: str = ""

    # -- predicates -----------------------------------------------------------
    def is_ok(self, code: Ok | str | None = None) -> bool:
        return self.kind is MessageKind.OK and (code is None or self.code == _code(code))

    def is_err(self, code: Err | str | None = None) -> bool:
        return self.kind is MessageKind.ERR and (code is None or self.code == _code(code))

    def is_event(self, code: Ev | str | None = None) -> bool:
        return self.kind is MessageKind.EVENT and (code is None or self.code == _code(code))

    @property
    def slot(self) -> int | None:
        """Slot argument of ``OK MOVING n`` / ``OK AT_SLOT n`` (else ``None``)."""
        if self.kind is MessageKind.OK and self.code in (Ok.MOVING.value, Ok.AT_SLOT.value):
            if self.args and _DIGITS.match(self.args[0]):
                return int(self.args[0])
        return None

    def to_line(self) -> str:
        return " ".join((self.kind.value, self.code, *self.args)).strip()

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.to_line()


def _code(code: Enum | str) -> str:
    return code.value if isinstance(code, Enum) else str(code)


def parse_message(line: str) -> Message | None:
    """Parse one device line. Returns ``None`` for noise (boot banner, ``#debug``…).

    The kind and code are upper-cased; arguments are kept verbatim except that
    the ``STATUS`` key=value pairs are left for :class:`StatusReport` to parse.
    """
    text = _normalise(line.replace("\x00", ""))
    if not text:
        return None
    tokens = text.split(" ")
    head = tokens[0].upper()
    try:
        kind = MessageKind(head)
    except ValueError:
        return None
    if len(tokens) < 2:
        return None
    code = tokens[1].upper()
    if not re.match(r"^[A-Z_]+$", code):
        return None
    return Message(kind=kind, code=code, args=tuple(tokens[2:]), raw=text)


def format_message(kind: MessageKind | str, code: Enum | str, *args: object) -> str:
    """Build a device line (used by the simulator): ``format_message('OK', Ok.MOVING, 3)``."""
    k = kind.value if isinstance(kind, MessageKind) else str(kind).upper()
    parts = [k, _code(code), *(str(a) for a in args)]
    return " ".join(parts)


# --------------------------------------------------------------------------- STATUS


@dataclass(frozen=True)
class StatusReport:
    """Parsed ``OK STATUS state=… homed=… slot=… gate=… slots=… fw=…``."""

    state: DeviceState = DeviceState.UNKNOWN
    homed: bool | None = None
    slot: int | None = None
    gate: GateState = GateState.UNKNOWN
    num_slots: int | None = None
    fw: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, msg: Message | str) -> "StatusReport":
        if isinstance(msg, str):
            parsed = parse_message(msg)
            if parsed is None:
                raise ProtocolError(f"not a STATUS line: {msg!r}")
            msg = parsed
        if not msg.is_ok(Ok.STATUS):
            raise ProtocolError(f"not a STATUS message: {msg.to_line()!r}")
        pairs: dict[str, str] = {}
        for token in msg.args:
            if "=" in token:
                k, v = token.split("=", 1)
                pairs[k.strip().lower()] = v.strip()
        homed_raw = pairs.pop("homed", None)
        slot_raw = pairs.pop("slot", None)
        slots_raw = pairs.pop("slots", None)
        homed = None if homed_raw is None else homed_raw in ("1", "true", "TRUE", "yes")
        slot = None
        if slot_raw is not None and re.match(r"^-?[0-9]+$", slot_raw):
            slot = int(slot_raw)
            if slot < 0:
                slot = None
        num_slots = int(slots_raw) if slots_raw and slots_raw.isdigit() else None
        return cls(
            state=DeviceState.parse(pairs.pop("state", None)),
            homed=homed,
            slot=slot,
            gate=GateState.parse(pairs.pop("gate", None)),
            num_slots=num_slots,
            fw=pairs.pop("fw", None),
            extra=dict(pairs),
        )

    def to_line(self) -> str:
        """Canonical device-side rendering (used by the simulator)."""
        parts = [
            f"state={self.state.value}",
            f"homed={1 if self.homed else 0}",
            f"slot={-1 if self.slot is None else self.slot}",
            f"gate={self.gate.value}",
        ]
        if self.num_slots is not None:
            parts.append(f"slots={self.num_slots}")
        if self.fw:
            parts.append(f"fw={self.fw}")
        parts.extend(f"{k}={v}" for k, v in self.extra.items())
        return "OK STATUS " + " ".join(parts)


# --------------------------------------------------------------------------- classify

_PROGRESS: Mapping[CommandName, frozenset[str]] = {
    CommandName.HOME: frozenset({Ok.HOMING.value}),
    CommandName.MOVE_SLOT: frozenset({Ok.MOVING.value}),
    CommandName.DISPENSE_SLOT: frozenset({Ok.MOVING.value, Ok.AT_SLOT.value}),
}

_SUCCESS: Mapping[CommandName, str] = {
    CommandName.PING: Ok.PONG.value,
    CommandName.STATUS: Ok.STATUS.value,
    CommandName.HOME: Ok.HOMED.value,
    CommandName.MOVE_SLOT: Ok.AT_SLOT.value,
    CommandName.DISPENSE_SLOT: Ok.GATE_OPEN.value,
    CommandName.OPEN_GATE: Ok.GATE_OPEN.value,
    CommandName.CLOSE_GATE: Ok.GATE_CLOSED.value,
    CommandName.STOP: Ok.STOPPED.value,
}

_MOTION_FAILURES = frozenset(
    e.value
    for e in (
        Err.INVALID_SLOT, Err.NOT_HOMED, Err.BUSY, Err.INVALID_STATE,
        Err.MOTOR_FAULT, Err.STOPPED, Err.UNKNOWN_COMMAND,
    )
)

_FAILURES: Mapping[CommandName, frozenset[str]] = {
    CommandName.PING: frozenset({Err.UNKNOWN_COMMAND.value}),
    CommandName.STATUS: frozenset({Err.UNKNOWN_COMMAND.value}),
    CommandName.HOME: frozenset(
        e.value
        for e in (
            Err.BUSY, Err.INVALID_STATE, Err.HOME_TIMEOUT, Err.MOTOR_FAULT,
            Err.STOPPED, Err.UNKNOWN_COMMAND,
        )
    ),
    CommandName.MOVE_SLOT: _MOTION_FAILURES,
    CommandName.DISPENSE_SLOT: _MOTION_FAILURES,
    CommandName.OPEN_GATE: frozenset(
        e.value for e in (Err.NOT_HOMED, Err.BUSY, Err.INVALID_STATE, Err.UNKNOWN_COMMAND)
    ),
    CommandName.CLOSE_GATE: frozenset({Err.BUSY.value, Err.UNKNOWN_COMMAND.value}),
    CommandName.STOP: frozenset(),
}

_SLOT_TAGGED = frozenset({Ok.MOVING.value, Ok.AT_SLOT.value})


def classify(command: Command, msg: Message) -> Disposition:
    """How ``msg`` relates to the in-flight ``command`` (docs §9.2).

    ``EVENT`` lines are always ``UNRELATED`` (they go to event listeners).
    ``OK MOVING n`` / ``OK AT_SLOT n`` only count when ``n`` matches the command's slot.
    """
    if msg.kind is MessageKind.EVENT:
        return Disposition.UNRELATED
    name = command.name
    if msg.kind is MessageKind.OK:
        if msg.code in _SLOT_TAGGED and command.slot is not None and msg.slot != command.slot:
            return Disposition.UNRELATED
        if msg.code == _SUCCESS[name]:
            return Disposition.SUCCESS
        if msg.code in _PROGRESS.get(name, frozenset()):
            return Disposition.PROGRESS
        return Disposition.UNRELATED
    # ERR
    if msg.code in _FAILURES[name]:
        return Disposition.FAILURE
    return Disposition.UNRELATED


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command as seen by the host.

    ``code`` is the terminal message code (e.g. ``"GATE_OPEN"``, ``"INVALID_SLOT"``)
    or a :class:`HostCode` value. ``definitive`` is True when the device explicitly
    answered (or the outcome is otherwise certain, e.g. ``NOT_CONNECTED``).
    ``gate_may_be_open`` is True only for uncertain outcomes of gate-opening
    commands — callers must then fail closed (docs §9.4).
    """

    command: Command
    ok: bool
    code: str
    messages: tuple[Message, ...] = ()
    elapsed_s: float = 0.0
    definitive: bool = True
    gate_may_be_open: bool = False
    detail: str = ""

    @property
    def summary(self) -> str:
        return f"{self.command.to_line()} -> {'OK' if self.ok else 'FAIL'} {self.code}"

    @property
    def hardware_result(self) -> str:
        """Compact string stored in ``DoseEvent.hardware_result``."""
        prefix = "OK" if self.ok else ("ERR" if self.definitive else "UNCERTAIN")
        text = f"{prefix} {self.code}"
        return text if not self.detail else f"{text} ({self.detail})"[:255]

    @classmethod
    def host_failure(
        cls,
        command: Command,
        code: HostCode,
        *,
        messages: tuple[Message, ...] = (),
        elapsed_s: float = 0.0,
        detail: str = "",
    ) -> "CommandResult":
        """Build a result for a host-side failure with the right certainty flags."""
        uncertain = code in (HostCode.TIMEOUT, HostCode.DISCONNECTED)
        return cls(
            command=command,
            ok=False,
            code=code.value,
            messages=messages,
            elapsed_s=elapsed_s,
            definitive=not uncertain,
            gate_may_be_open=uncertain and command.may_open_gate,
            detail=detail,
        )
