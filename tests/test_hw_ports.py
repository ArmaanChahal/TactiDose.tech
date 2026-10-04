"""ESP32 serial-port auto-detection (monkeypatched port enumeration, no real hardware)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import serial.tools.list_ports

from tactidose.hardware.ports import (
    describe_ports,
    find_esp32_ports,
    format_ports,
    match_usb_id,
    resolve_port,
)


def port(device: str, vid: int | None = None, pid: int | None = None, description: str = "") -> SimpleNamespace:
    hwid = f"USB VID:PID={vid:04X}:{pid:04X}" if vid is not None and pid is not None else "PCI\\VEN_8086"
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description or device,
                           hwid=hwid, manufacturer=None, serial_number=None)


INTEL_AMT = port("COM3", description="Intel(R) Active Management Technology - SOL (COM3)")
CP2102 = port("COM5", 0x10C4, 0xEA60, "Silicon Labs CP210x USB to UART Bridge (COM5)")


@pytest.fixture
def system_ports(monkeypatch):
    """Replace ``serial.tools.list_ports.comports`` with a list the test controls."""
    current: list[SimpleNamespace] = []
    monkeypatch.setattr(serial.tools.list_ports, "comports", lambda *a, **k: list(current))
    return current


def test_auto_never_picks_a_port_without_a_known_usb_id(system_ports):
    system_ports.append(INTEL_AMT)                 # this laptop's COM3: no VID/PID at all
    assert find_esp32_ports() == []
    assert resolve_port("auto") is None
    system_ports.append(port("COM8", 0x2341, 0x0043, "Arduino Uno (COM8)"))   # USB, but not an ESP32 bridge
    assert resolve_port("auto") is None


def test_auto_picks_the_esp32_bridge(system_ports):
    system_ports.extend([INTEL_AMT, CP2102])
    assert resolve_port("auto") == "COM5"
    assert resolve_port("AUTO") == "COM5" and resolve_port("") == "COM5" and resolve_port(None) == "COM5"


def test_candidates_are_ranked_then_naturally_ordered():
    ports = [
        port("COM7", 0x1A86, 0x7523, "USB-SERIAL CH340"),
        port("COM4", 0x0403, 0x6001, "FT232R USB UART"),
        port("COM12", 0x10C4, 0xEA60),
        port("COM9", 0x303A, 0x1001, "USB JTAG/serial debug unit"),
        port("COM10", 0x10C4, 0xEA60),
        port("COM2", 0x1A86, 0x55D4, "USB-Enhanced-SERIAL CH9102"),
        port("COM6", 0x1A86, 0x55D3, "USB-Enhanced-SERIAL CH343"),
        port("COM11", 0x0403, 0x6015, "FT231X"),
        INTEL_AMT,
    ]
    assert [c.device for c in find_esp32_ports(ports)] == [
        "COM9", "COM2", "COM6", "COM10", "COM12", "COM7", "COM4", "COM11",
    ]
    assert resolve_port("auto", ports) == "COM9"
    best = find_esp32_ports(ports)[0]
    assert best.usb_id == "303A:1001" and "Espressif" in (best.chip or "")


def test_espressif_vendor_matches_any_product():
    assert match_usb_id(0x303A, 0x4001) is not None
    assert match_usb_id(0x10C4, 0x0001) is None
    assert match_usb_id(None, None) is None


def test_explicit_values_are_used_as_given(system_ports):
    for value in ("COM9", "/dev/ttyUSB0", "socket://127.0.0.1:7777", "loop://"):
        assert resolve_port(value) == value
    assert resolve_port("  COM9 ") == "COM9"


def test_describe_and_format_ports_for_doctor(system_ports):
    system_ports.extend([INTEL_AMT, CP2102])
    rows = {r["device"]: r for r in describe_ports()}
    assert rows["COM3"]["esp32_candidate"] is False and rows["COM3"]["auto_selected"] is False
    assert "ignored by auto-detect" in rows["COM3"]["reason"]
    assert rows["COM5"]["auto_selected"] is True and rows["COM5"]["usb_id"] == "10C4:EA60"
    text = format_ports()
    assert "COM3" in text and "* COM5" in text
    system_ports.clear()
    assert format_ports() == "No serial ports found."
    system_ports.append(INTEL_AMT)
    assert "auto-detect finds no ESP32" in format_ports()


def test_describe_ports_accepts_a_generator():
    rows = describe_ports(p for p in (INTEL_AMT, CP2102))
    assert [r["device"] for r in rows] == ["COM3", "COM5"] and rows[1]["auto_selected"]


def test_enumeration_failure_is_safe(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("SetupAPI failure")

    monkeypatch.setattr(serial.tools.list_ports, "comports", boom)
    assert resolve_port("auto") is None
    assert describe_ports() == []
