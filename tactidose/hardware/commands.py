"""Host -> ESP32 command builders (the handoff §32 ``hardware/commands.py`` module).

A thin re-export of :class:`tactidose.hardware.protocol.Command` so that
``protocol.py`` stays the single source of truth for the wire format::

    from tactidose.hardware import commands
    commands.dispense_slot(3, num_slots=6).encode()   # b"DISPENSE_SLOT 3\\n"
    commands.build("move_slot", 2)                    # Command(MOVE_SLOT, 2)
"""

from __future__ import annotations

from tactidose.hardware.protocol import (
    DEFAULT_NUM_SLOTS,
    Command,
    CommandName,
    ParsedCommand,
    ProtocolError,
    parse_command,
)

__all__ = [
    "Command",
    "CommandName",
    "ParsedCommand",
    "ProtocolError",
    "parse_command",
    "build",
    "ping",
    "status",
    "home",
    "move_slot",
    "dispense_slot",
    "open_gate",
    "close_gate",
    "stop",
]

ping = Command.ping
status = Command.status
home = Command.home
move_slot = Command.move_slot
dispense_slot = Command.dispense_slot
open_gate = Command.open_gate
close_gate = Command.close_gate
stop = Command.stop


def build(name: str | CommandName, slot: int | None = None, num_slots: int = DEFAULT_NUM_SLOTS) -> Command:
    """Build a validated command from its name (case-insensitive) and optional slot.

    Raises :class:`ProtocolError` for unknown names, a missing/out-of-range slot on
    ``MOVE_SLOT``/``DISPENSE_SLOT``, or a slot given to a command that takes none.
    """
    try:
        cname = name if isinstance(name, CommandName) else CommandName(str(name).strip().upper())
    except ValueError:
        raise ProtocolError(f"unknown command {name!r}") from None
    if cname is CommandName.MOVE_SLOT:
        return Command.move_slot(slot, num_slots)  # type: ignore[arg-type]
    if cname is CommandName.DISPENSE_SLOT:
        return Command.dispense_slot(slot, num_slots)  # type: ignore[arg-type]
    if slot is not None:
        raise ProtocolError(f"{cname.value} takes no slot argument")
    return Command(cname)
