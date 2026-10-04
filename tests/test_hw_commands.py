"""hardware/commands.py: thin builders over protocol.Command."""

from __future__ import annotations

import pytest

from tactidose.hardware import commands
from tactidose.hardware.protocol import Command, CommandName, ProtocolError


def test_builders_are_the_protocol_factories():
    assert commands.ping() == Command.ping()
    assert commands.dispense_slot(3, 6).encode() == b"DISPENSE_SLOT 3\n"
    assert commands.stop().to_line() == "STOP"


@pytest.mark.parametrize(
    "name,slot,line",
    [("PING", None, "PING"), ("status", None, "STATUS"), (" home ", None, "HOME"),
     ("move_slot", 2, "MOVE_SLOT 2"), (CommandName.DISPENSE_SLOT, 5, "DISPENSE_SLOT 5"),
     ("OPEN_GATE", None, "OPEN_GATE"), ("close_gate", None, "CLOSE_GATE"), ("STOP", None, "STOP")],
)
def test_build(name, slot, line):
    assert commands.build(name, slot).to_line() == line


@pytest.mark.parametrize(
    "name,slot,num_slots",
    [("FOO", None, 6), ("MOVE_SLOT", None, 6), ("MOVE_SLOT", 6, 6), ("DISPENSE_SLOT", -1, 6),
     ("DISPENSE_SLOT", True, 6), ("PING", 1, 6), ("MOVE_SLOT", 7, 8 + 5)],
)
def test_build_rejects_invalid(name, slot, num_slots):
    with pytest.raises(ProtocolError):
        commands.build(name, slot, num_slots)


def test_build_honours_num_slots():
    assert commands.build("MOVE_SLOT", 7, num_slots=8).slot == 7
