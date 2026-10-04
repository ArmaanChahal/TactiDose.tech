"""Host -> ESP32 command builders (the handoff §32 ``hardware/commands.py`` module).

A thin re-export of :class:`tactidose.hardware.protocol.Command` so that
``protocol.py`` stays the single source of truth for the wire format (protocol v1.1)::

    from tactidose.hardware import commands
    commands.drop_slot(2).encode()                    # b"DROP_SLOT 2\\n"  (v2 device: 3 containers)
    commands.dispense_slot(3, num_slots=6).encode()   # b"DISPENSE_SLOT 3\\n"
    commands.build("move_slot", 2)                    # Command(MOVE_SLOT, 2)

Slot arguments are validated against ``num_slots`` (default :data:`DEFAULT_NUM_SLOTS` = 3,
the v2 device); pass the configured ``settings.num_slots`` for other builds.
"""

from __future__ import annotations

from typing import Callable

from tactidose.hardware.protocol import (
    DEFAULT_NUM_SLOTS,
    Command,
    CommandName,
    ParsedCommand,
    ProtocolError,
    parse_command,
)

__all__ = [
    "DEFAULT_NUM_SLOTS",
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
    "drop_slot",
    "open_gate",
    "close_gate",
    "stop",
]

ping = Command.ping
status = Command.status
home = Command.home
move_slot = Command.move_slot
dispense_slot = Command.dispense_slot
drop_slot = Command.drop_slot
open_gate = Command.open_gate
close_gate = Command.close_gate
stop = Command.stop

_SLOT_BUILDERS: dict[CommandName, Callable[[int, int], Command]] = {
    CommandName.MOVE_SLOT: Command.move_slot,
    CommandName.DISPENSE_SLOT: Command.dispense_slot,
    CommandName.DROP_SLOT: Command.drop_slot,
}


def build(name: str | CommandName, slot: int | None = None, num_slots: int = DEFAULT_NUM_SLOTS) -> Command:
    """Build a validated command from its name (case-insensitive) and optional slot.

    Raises :class:`ProtocolError` for unknown names, a missing/out-of-range slot on
    ``MOVE_SLOT``/``DISPENSE_SLOT``/``DROP_SLOT``, or a slot given to a command that takes none.
    """
    try:
        cname = name if isinstance(name, CommandName) else CommandName(str(name).strip().upper())
    except ValueError:
        raise ProtocolError(f"unknown command {name!r}") from None
    factory = _SLOT_BUILDERS.get(cname)
    if factory is not None:
        return factory(slot, num_slots)  # type: ignore[arg-type]
    if slot is not None:
        raise ProtocolError(f"{cname.value} takes no slot argument")
    return Command(cname)
