"""ESP32 serial-port auto-detection by USB VID:PID.

``TACTIDOSE_SERIAL_PORT=auto`` resolves to the best-ranked port whose USB vendor/product
id belongs to a known ESP32 USB bridge. Ports without a matching VID:PID are **never**
chosen automatically - e.g. ``COM3 Intel(R) Active Management Technology - SOL`` or a
Bluetooth serial port - because sending motor commands to an unknown device is unsafe
and would only produce confusing timeouts. Any explicit value (``COM5``,
``/dev/ttyUSB0``, ``socket://...``) is used as given.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "AUTO",
    "KNOWN_USB_IDS",
    "UsbId",
    "PortCandidate",
    "find_esp32_ports",
    "resolve_port",
    "describe_ports",
    "format_ports",
]

AUTO = "auto"


@dataclass(frozen=True)
class UsbId:
    vid: int
    pid: int | None        # None = any product of this vendor
    chip: str
    rank: int              # lower = more likely to be the TactiDose ESP32


#: Known ESP32 USB interfaces, best first. Espressif's own VID is unambiguous; the
#: bridge chips are common on dev boards but also used by other gadgets.
KNOWN_USB_IDS: tuple[UsbId, ...] = (
    UsbId(0x303A, None, "Espressif native USB (ESP32-S2/S3/C3/C6)", 0),
    UsbId(0x10C4, 0xEA60, "Silicon Labs CP210x", 1),
    UsbId(0x1A86, 0x55D4, "WCH CH9102", 1),
    UsbId(0x1A86, 0x55D3, "WCH CH343", 1),
    UsbId(0x1A86, 0x7523, "WCH CH340", 2),
    UsbId(0x0403, 0x6001, "FTDI FT232R", 3),
    UsbId(0x0403, 0x6015, "FTDI FT231X", 3),
)


@dataclass(frozen=True)
class PortCandidate:
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    chip: str | None
    rank: int | None
    manufacturer: str | None = None
    serial_number: str | None = None

    @property
    def is_esp32_candidate(self) -> bool:
        return self.rank is not None

    @property
    def usb_id(self) -> str | None:
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04X}:{self.pid:04X}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vid"] = None if self.vid is None else f"{self.vid:04X}"
        d["pid"] = None if self.pid is None else f"{self.pid:04X}"
        d["usb_id"] = self.usb_id
        d["esp32_candidate"] = self.is_esp32_candidate
        return d


def match_usb_id(vid: int | None, pid: int | None) -> UsbId | None:
    """The :data:`KNOWN_USB_IDS` entry for ``vid:pid``, or None."""
    if vid is None:
        return None
    for known in KNOWN_USB_IDS:
        if known.vid == vid and (known.pid is None or known.pid == pid):
            return known
    return None


def _natural_key(device: str) -> list[Any]:
    """``COM3`` < ``COM10``; ``/dev/ttyUSB2`` < ``/dev/ttyUSB10``."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", device)]


def _list_ports() -> list[Any]:
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial is a core dependency
        log.warning("pyserial is not installed; cannot enumerate serial ports")
        return []
    try:
        return list(list_ports.comports())
    except Exception:  # noqa: BLE001 - enumeration can fail on odd systems; never crash
        log.warning("serial port enumeration failed", exc_info=True)
        return []


def _candidate(info: Any) -> PortCandidate:
    vid = getattr(info, "vid", None)
    pid = getattr(info, "pid", None)
    known = match_usb_id(vid, pid)
    return PortCandidate(
        device=str(getattr(info, "device", "")),
        description=str(getattr(info, "description", "") or ""),
        hwid=str(getattr(info, "hwid", "") or ""),
        vid=vid,
        pid=pid,
        chip=known.chip if known else None,
        rank=known.rank if known else None,
        manufacturer=getattr(info, "manufacturer", None),
        serial_number=getattr(info, "serial_number", None),
    )


def _all_ports(ports: Iterable[Any] | None) -> list[PortCandidate]:
    infos = _list_ports() if ports is None else list(ports)
    return sorted((_candidate(p) for p in infos), key=lambda c: _natural_key(c.device))


def find_esp32_ports(ports: Iterable[Any] | None = None) -> list[PortCandidate]:
    """Ports with a known ESP32 USB VID:PID, best first (rank, then natural port order).

    ``ports`` (``ListPortInfo``-like objects) defaults to ``serial.tools.list_ports.comports()``.
    """
    found = [c for c in _all_ports(ports) if c.rank is not None]
    return sorted(found, key=lambda c: (c.rank, _natural_key(c.device)))


def resolve_port(value: str | None, ports: Iterable[Any] | None = None) -> str | None:
    """``auto``/empty -> best ESP32 candidate (or None if there is none); anything else as given."""
    text = (value or "").strip()
    if text and text.lower() != AUTO:
        return text
    candidates = find_esp32_ports(ports)
    if not candidates:
        log.debug("serial auto-detect: no port with a known ESP32 USB VID:PID")
        return None
    best = candidates[0]
    if len(candidates) > 1:
        log.info(
            "serial auto-detect: %d candidates (%s); using %s - set TACTIDOSE_SERIAL_PORT to choose",
            len(candidates), ", ".join(c.device for c in candidates), best.device,
        )
    else:
        log.info("serial auto-detect: using %s (%s, %s)", best.device, best.chip, best.usb_id)
    return best.device


def describe_ports(ports: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """Every serial port with its USB id and whether auto-detect would consider it (doctor)."""
    infos = _list_ports() if ports is None else list(ports)
    out: list[dict[str, Any]] = []
    best = find_esp32_ports(infos)
    chosen = best[0].device if best else None
    for c in _all_ports(infos):
        d = c.to_dict()
        d["auto_selected"] = c.device == chosen
        d["reason"] = (
            f"known ESP32 USB interface ({c.chip})" if c.rank is not None
            else ("no USB VID:PID - ignored by auto-detect" if c.vid is None
                  else f"USB {c.usb_id or f'{c.vid:04X}'} is not a known ESP32 interface - ignored by auto-detect")
        )
        out.append(d)
    return out


def format_ports(ports: Iterable[Any] | None = None) -> str:
    """Human-readable table of :func:`describe_ports` for CLI output."""
    rows = describe_ports(ports)
    if not rows:
        return "No serial ports found."
    lines = []
    for r in rows:
        mark = "*" if r["auto_selected"] else " "
        lines.append(f"{mark} {r['device']:<14} {r['usb_id'] or '-':<10} {r['description']}  [{r['reason']}]")
    if not any(r["auto_selected"] for r in rows):
        lines.append("  (auto-detect finds no ESP32: plug the board in or set TACTIDOSE_SERIAL_PORT)")
    return "\n".join(lines)
