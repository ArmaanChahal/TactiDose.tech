"""hardware/commands.py: thin builders over protocol.Command (v1.1, 3-container default)."""

from __future__ import annotations

import pytest

from tactidose.hardware import commands
from tactidose.hardware.protocol import Command, CommandName, ProtocolError


def test_builders_are_the_protocol_factories():
    assert commands.ping() == Command.ping()
    assert commands.dispense_slot(3, 6).encode() == b"DISPENSE_SLOT 3\n"
    assert commands.drop_slot(2).encode() == b"DROP_SLOT 2\n"
    assert commands.drop_slot(2) == Command.drop_slot(2, 3)
    assert commands.stop().to_line() == "STOP"


def test_default_is_the_v2_device_with_three_containers():
    assert commands.DEFAULT_NUM_SLOTS == 3
    assert commands.build("DROP_SLOT", 2).to_line() == "DROP_SLOT 2"
    for name in ("MOVE_SLOT", "DISPENSE_SLOT", "DROP_SLOT"):
        with pytest.raises(ProtocolError):
            commands.build(name, 3)


@pytest.mark.parametrize(
    "name,slot,line",
    [("PING", None, "PING"), ("status", None, "STATUS"), (" home ", None, "HOME"),
     ("move_slot", 2, "MOVE_SLOT 2"), (CommandName.DISPENSE_SLOT, 1, "DISPENSE_SLOT 1"),
     ("drop_slot", 0, "DROP_SLOT 0"), (CommandName.DROP_SLOT, 2, "DROP_SLOT 2"),
     ("OPEN_GATE", None, "OPEN_GATE"), ("close_gate", None, "CLOSE_GATE"), ("STOP", None, "STOP")],
)
def test_build(name, slot, line):
    assert commands.build(name, slot).to_line() == line


@pytest.mark.parametrize(
    "name,slot,num_slots",
    [("FOO", None, 6), ("MOVE_SLOT", None, 6), ("MOVE_SLOT", 6, 6), ("DISPENSE_SLOT", -1, 6),
     ("DISPENSE_SLOT", True, 6), ("PING", 1, 6), ("MOVE_SLOT", 7, 8 + 5),
     ("DROP_SLOT", None, 3), ("DROP_SLOT", 3, 3), ("drop_slot", "1", 3), ("DROP_SLOT", 1.0, 3),
     ("DROP_SLOT", False, 3)],
)
def test_build_rejects_invalid(name, slot, num_slots):
    with pytest.raises(ProtocolError):
        commands.build(name, slot, num_slots)


def test_build_honours_num_slots():
    assert commands.build("MOVE_SLOT", 7, num_slots=8).slot == 7
    assert commands.build("DISPENSE_SLOT", 5, num_slots=6).to_line() == "DISPENSE_SLOT 5"
    assert commands.build("DROP_SLOT", 11, num_slots=12).to_line() == "DROP_SLOT 11"
