"""Byte transports between the host and the motor controller.

A :class:`Transport` is a minimal, thread-safe byte pipe used by
``serial_client.HardwareClient``: one thread blocks in :meth:`Transport.read`
while others :meth:`Transport.write`. Any link failure (port missing, USB unplug,
closed handle, socket reset) surfaces as :class:`TransportError` so the client can
treat every kind of link loss the same way (fail closed, reconnect with backoff).

Implementations:

* :class:`PySerialTransport` - real ports through ``serial.serial_for_url``:
  ``COM5``, ``/dev/ttyUSB0``, ``socket://127.0.0.1:7777`` (the TCP simulator below)
  and ``loop://`` (pyserial loopback, handy in tests).
* ``simulator.SimulatedDevice.open_transport()`` - in-process pipe to the virtual ESP32.

:func:`serve_simulator_tcp` exposes a :class:`~tactidose.hardware.simulator.SimulatedDevice`
on a TCP port so the complete pyserial code path can be exercised without hardware
(``TACTIDOSE_SERIAL_PORT=socket://127.0.0.1:7777``; CLI ``python -m tactidose simulator``).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from tactidose.hardware.protocol import DEFAULT_BAUD

if TYPE_CHECKING:
    from tactidose.config import Settings
    from tactidose.hardware.simulator import SimulatedDevice

log = logging.getLogger(__name__)

#: Blocking time of one ``read()`` when no byte arrives (keeps reader threads responsive).
DEFAULT_READ_TIMEOUT_S = 0.05
#: A write that cannot complete within this time is treated as a broken link.
DEFAULT_WRITE_TIMEOUT_S = 2.0
#: How long RTS is asserted to pull EN low when resetting a dev board.
RESET_PULSE_S = 0.1

__all__ = [
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_WRITE_TIMEOUT_S",
    "Transport",
    "TransportError",
    "PySerialTransport",
    "serve_simulator_tcp",
]


class TransportError(Exception):
    """The link to the device is unusable (cannot open, unplugged, closed, reset by peer)."""


@runtime_checkable
class Transport(Protocol):
    """Byte pipe to the device.

    ``read`` returns ``b""`` when nothing arrived within the transport's short read
    timeout and raises :class:`TransportError` when the link is gone; ``write``
    returns the number of bytes written. ``close`` is idempotent.
    """

    name: str

    @property
    def is_open(self) -> bool: ...

    def read(self, size: int = 1024) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- pyserial


class PySerialTransport:
    """pyserial-backed transport (opened in the constructor).

    DTR and RTS are deasserted *before* the port is opened: on ESP32 dev boards
    those lines drive EN/IO0 through the auto-reset circuit, and asserting them on
    open would reboot the board (it would still be tolerated - the host re-syncs
    on ``EVENT BOOT`` - but it costs a re-home).
    """

    def __init__(
        self,
        url: str,
        baud: int = DEFAULT_BAUD,
        *,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
    ) -> None:
        self.name = str(url)
        self.baud = int(baud)
        self._read_timeout_s = read_timeout_s
        self._write_timeout_s = write_timeout_s
        self._closed = False
        self._ser: Any = self._open()

    @classmethod
    def from_settings(cls, settings: "Settings", url: str | None = None) -> "PySerialTransport":
        """Open ``url`` (default: ``settings.serial_port`` taken literally) at ``settings.serial_baud``."""
        return cls(url or settings.serial_port, settings.serial_baud)

    def _open(self) -> Any:
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover - pyserial is a core dependency
            raise TransportError("pyserial is not installed") from exc
        try:
            ser = serial.serial_for_url(self.name, do_not_open=True)
            ser.baudrate = self.baud
            ser.bytesize = serial.EIGHTBITS
            ser.parity = serial.PARITY_NONE
            ser.stopbits = serial.STOPBITS_ONE
            ser.xonxoff = False
            ser.rtscts = False
            ser.dsrdtr = False
            ser.timeout = self._read_timeout_s
            ser.write_timeout = self._write_timeout_s
            # Must happen before open(): pyserial applies the stored line states when opening.
            ser.dtr = False
            ser.rts = False
            ser.open()
        except Exception as exc:  # SerialException, OSError, ValueError (bad URL) ...
            raise TransportError(f"cannot open {self.name}: {exc}") from exc
        log.info("opened serial transport %s @ %d baud", self.name, self.baud)
        return ser

    @property
    def is_open(self) -> bool:
        return not self._closed and bool(getattr(self._ser, "is_open", False))

    def _require_open(self) -> Any:
        if not self.is_open:
            raise TransportError(f"{self.name} is closed")
        return self._ser

    def read(self, size: int = 1024) -> bytes:
        ser = self._require_open()
        try:
            # Block (up to the read timeout) for the first byte only, then take what is
            # already buffered: avoids waiting the full timeout for every partial chunk.
            first = ser.read(1)
            if not first:
                return b""
            buf = bytearray(first)
            while len(buf) < size:
                waiting = ser.in_waiting
                if not waiting:
                    break
                chunk = ser.read(min(int(waiting), size - len(buf)))
                if not chunk:
                    break
                buf += chunk
            return bytes(buf)
        except Exception as exc:  # noqa: BLE001 - any failure here means the link is gone
            if self._closed:
                raise TransportError(f"{self.name} is closed") from exc
            raise TransportError(f"read from {self.name} failed: {exc}") from exc

    def write(self, data: bytes) -> int:
        ser = self._require_open()
        try:
            n = ser.write(data)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"write to {self.name} failed: {exc}") from exc
        return len(data) if n is None else int(n)

    def pulse_reset(self, hold_s: float = RESET_PULSE_S) -> None:
        """Reboot an ESP32 dev board via its auto-reset circuit (EN low through RTS, IO0 high).

        Works on boards with the classic two-transistor DTR/RTS circuit (CP210x / CH340 /
        CH9102 bridges) and on the ESP32-S3/C3 USB-Serial-JTAG port. Boards without an
        auto-reset circuit (or with TinyUSB CDC firmware) ignore it: power-cycle by hand.
        No-op for ``socket://`` and ``loop://``.
        """
        ser = self._require_open()
        try:
            ser.dtr = False          # IO0 high -> normal boot, not the ROM bootloader
            ser.rts = True           # EN low -> chip held in reset
            ser.dtr = ser.dtr        # usbser.sys only sends line-state changes together with DTR
            time.sleep(hold_s)
            ser.rts = False          # EN high -> chip boots
            ser.dtr = ser.dtr
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"reset pulse on {self.name} failed: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ser.close()
        except Exception:  # noqa: BLE001 - closing a dead port must not raise
            log.debug("error closing %s", self.name, exc_info=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PySerialTransport({self.name!r}, baud={self.baud}, open={self.is_open})"


# --------------------------------------------------------------------------- TCP simulator


def serve_simulator_tcp(
    settings: "Settings",
    host: str = "127.0.0.1",
    port: int = 7777,
    stop_event: threading.Event | None = None,
    *,
    device: "SimulatedDevice | None" = None,
    on_ready: Callable[[str, int], None] | None = None,
) -> None:
    """Serve a simulated ESP32 on ``host:port`` until ``stop_event`` is set (blocking).

    Connect with ``socket://host:port``. Like a real serial port, one client is served
    at a time; the device keeps running between connections (no reset on connect).
    ``device`` lets the caller supply (and keep ownership of) a ``SimulatedDevice``;
    otherwise one is created from ``settings``, started, and closed on exit.
    ``on_ready(host, bound_port)`` is called once listening (use ``port=0`` for an
    ephemeral port in tests).
    """
    from tactidose.hardware.simulator import SimulatedDevice

    owns_device = device is None
    dev = device if device is not None else SimulatedDevice(settings)
    dev.start()
    stop = stop_event if stop_event is not None else threading.Event()
    server = socket.create_server((host, port))
    server.settimeout(0.2)
    try:
        bound_host, bound_port = server.getsockname()[:2]
        log.info("simulated ESP32 listening on socket://%s:%d", bound_host, bound_port)
        if on_ready is not None:
            on_ready(bound_host, bound_port)
        while not stop.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop.is_set():
                    break
                raise
            log.info("simulator client connected from %s:%d", addr[0], addr[1])
            try:
                _serve_connection(dev, conn, stop)
            finally:
                log.info("simulator client %s:%d disconnected", addr[0], addr[1])
    finally:
        server.close()
        if owns_device:
            dev.close()


def _serve_connection(dev: "SimulatedDevice", conn: socket.socket, stop: threading.Event) -> None:
    conn.settimeout(0.05)
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:  # pragma: no cover - not fatal
        pass
    try:
        transport = dev.open_transport()
    except TransportError as exc:
        # Simulated "USB unplugged": accept and immediately drop, like a vanished port.
        log.info("refusing simulator client: %s", exc)
        conn.close()
        return
    done = threading.Event()

    def device_to_socket() -> None:
        try:
            while not done.is_set() and not stop.is_set():
                try:
                    data = transport.read(4096)
                except TransportError:
                    break
                if data:
                    try:
                        conn.sendall(data)
                    except OSError:
                        break
        finally:
            done.set()

    pump = threading.Thread(target=device_to_socket, name="sim-tcp-tx", daemon=True)
    pump.start()
    try:
        while not done.is_set() and not stop.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            try:
                transport.write(data)
            except TransportError:
                break
    finally:
        done.set()
        transport.close()
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
        pump.join(timeout=1.0)
