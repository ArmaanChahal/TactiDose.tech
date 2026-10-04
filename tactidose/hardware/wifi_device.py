"""The ESP32 dispenser over Wi-Fi (``TACTIDOSE_HARDWARE_MODE=wifi``): three HTTP endpoints.

:class:`WifiDispenser` is a ``HardwareController`` (the same interface as the USB client), so
``DropService`` drives it unchanged and every drop rule still applies. Address and endpoints:
:mod:`tactidose.hardware.wifi_config`.

* ``drop_slot(n)`` -> ``GET /dispense?pill=<n+1>``. HTTP 2xx = the pill dropped (``OK DROPPED``);
  another HTTP status = it did not (definite failure); cannot connect = never sent (device
  unavailable); connected but no answer in time = the pill MAY have dropped -> UNCERTAIN, so
  DropService locks further drops until a caregiver checks (fail closed).
* ``set_lid(True/False)`` -> ``GET /lid?state=open|close`` (the Open / Close lid buttons).
* A background check (``GET /`` every ``HEALTH_INTERVAL_S``) keeps the connected / offline state;
  while offline every drop is refused.
* There is no stop, home, gate or raw-command endpoint: those calls answer "not supported" and
  change nothing. One request runs at a time (the ESP32 serves requests one by one).

Redirects are never followed and nothing here raises into callers.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from tactidose.core.interfaces import DeviceSnapshot
from tactidose.hardware import wifi_config
from tactidose.hardware.protocol import (
    Command,
    CommandResult,
    DeviceState,
    GateState,
    HostCode,
    Message,
    ProtocolError,
    parse_message,
)

if TYPE_CHECKING:
    from tactidose.config import Settings
    from tactidose.core.bus import EventBus

log = logging.getLogger(__name__)

__all__ = ["HTTP_ERROR", "WifiDispenser"]

#: Failure code of a dispense the ESP32 answered with a non-2xx status (definitely not dropped).
HTTP_ERROR = "HTTP_ERROR"
_UNSUPPORTED = "not available on the Wi-Fi dispenser"


class WifiDispenser:
    """``HardwareController`` for the Wi-Fi ESP32 (see the module docstring). Thread-safe."""

    def __init__(self, settings: "Settings", *, bus: "EventBus | None" = None, clock: Any = None,
                 client: Any = None, config: Any = wifi_config) -> None:
        self.settings = settings
        self.num_slots: int = settings.num_slots
        self.mode = "wifi"
        self._bus = bus
        self._config = config
        self.base_url = (settings.esp32_url or config.ESP32_BASE_URL).strip().rstrip("/")
        if client is None:
            import httpx

            client = httpx.Client(follow_redirects=False)
        self._client = client
        self._lock = threading.Lock()          # snapshot + lid state
        self._req_lock = threading.Lock()      # one request to the ESP32 at a time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lid_state: str | None = None      # "open" | "closed" | None (unknown)
        self._snap = DeviceSnapshot(mode="wifi", port=self.base_url, state=DeviceState.UNKNOWN,
                                    gate=GateState.CLOSED, proto="1.1", drop_sensor=False,
                                    num_slots_reported=settings.num_slots, fw_version="esp32-wifi")

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="esp32-wifi", daemon=True)
        self._thread.start()
        log.info("Wi-Fi dispenser at %s (endpoints: tactidose/hardware/wifi_config.py)", self.base_url)

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=self._config.CONNECT_TIMEOUT_S + 1)
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def reconnect(self) -> bool:
        return self._check(wait=True)

    def snapshot(self) -> DeviceSnapshot:
        with self._lock:
            return self._snap

    # ------------------------------------------------------------------ the three endpoints
    def drop_slot(self, slot: int) -> CommandResult:
        try:
            cmd = Command.drop_slot(slot, self.num_slots)
        except ProtocolError as exc:
            return CommandResult.host_failure(Command.ping(), HostCode.INVALID_ARGUMENT, detail=str(exc))
        path = self._config.DISPENSE_PATH.format(pill=slot + 1)
        status, failure = self._request(path, self._config.DISPENSE_TIMEOUT_S)
        if failure is not None:
            return CommandResult.host_failure(cmd, failure, detail=f"GET {path}")
        if 200 <= status < 300:
            return CommandResult(command=cmd, ok=True, code="DROPPED", messages=(_msg(f"OK DROPPED {slot}"),),
                                 detail=f"GET {path} -> HTTP {status}")
        return CommandResult(command=cmd, ok=False, code=HTTP_ERROR, detail=f"GET {path} -> HTTP {status}")

    def set_lid(self, open_: bool) -> dict[str, Any]:
        """Open / close the lid. ``{ok, lid, detail}``; ``lid`` stays as it was when it failed."""
        path = self._config.LID_OPEN_PATH if open_ else self._config.LID_CLOSE_PATH
        status, failure = self._request(path, self._config.LID_TIMEOUT_S)
        ok = failure is None and 200 <= status < 300
        if ok:
            with self._lock:
                self.lid_state = "open" if open_ else "closed"
        detail = f"GET {path} -> " + (failure.value if failure is not None else f"HTTP {status}")
        log.info("Wi-Fi dispenser lid %s: %s", "open" if open_ else "close", detail)
        return {"ok": ok, "lid": self.lid_state, "detail": detail}

    # ------------------------------------------------------------------ protocol surface
    def ping(self) -> CommandResult:
        return self._probe(Command.ping(), "PONG")

    def status(self) -> CommandResult:
        return self._probe(Command.status(), "STATUS")

    def home(self) -> CommandResult:
        # Nothing to home: the ESP32 handles its own positioning inside /dispense.
        return self._probe(Command.home(), "HOMED")

    def move_slot(self, slot: int) -> CommandResult:
        return self._unsupported(Command.ping(), "MOVE_SLOT")

    def dispense_slot(self, slot: int) -> CommandResult:
        return self._unsupported(Command.ping(), "DISPENSE_SLOT")

    def open_gate(self) -> CommandResult:
        return self._unsupported(Command.open_gate(), "OPEN_GATE (use the lid buttons)")

    def close_gate(self) -> CommandResult:
        return self._unsupported(Command.close_gate(), "CLOSE_GATE (use the lid buttons)")

    def stop(self) -> CommandResult:
        return self._unsupported(Command.stop(), "STOP (the ESP32 has no stop endpoint)")

    def send_raw(self, line: str) -> CommandResult:
        return self._unsupported(Command.ping(), "raw protocol commands")

    def add_event_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        return lambda: None   # the ESP32 sends no events over HTTP

    # ------------------------------------------------------------------ internals
    def _url(self, path: str) -> str:
        return self.base_url + path

    def _request(self, path: str, read_timeout_s: float) -> tuple[int, HostCode | None]:
        """``(status, None)`` when the ESP32 answered, else ``(0, NOT_CONNECTED | TIMEOUT | BUSY_LOCAL)``.

        NOT_CONNECTED = the connection never opened (nothing was sent); TIMEOUT = it opened but no
        answer came (the ESP32 may have acted)."""
        import httpx

        if not self._req_lock.acquire(timeout=self._config.CONNECT_TIMEOUT_S + read_timeout_s):
            return 0, HostCode.BUSY_LOCAL
        try:
            timeout = httpx.Timeout(read_timeout_s, connect=self._config.CONNECT_TIMEOUT_S)
            resp = self._client.request(self._config.METHOD, self._url(path), timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            log.warning("Wi-Fi dispenser not reachable at %s (%s)", self.base_url, type(exc).__name__)
            self._set_connected(False, "NOT_CONNECTED")
            return 0, HostCode.NOT_CONNECTED
        except Exception as exc:  # noqa: BLE001 - read timeout, dropped connection, ...
            log.warning("Wi-Fi dispenser: GET %s got no answer (%s)", path, type(exc).__name__)
            return 0, HostCode.TIMEOUT
        finally:
            self._req_lock.release()
        self._set_connected(True)
        return int(resp.status_code), None

    def _check(self, *, wait: bool = False) -> bool:
        """Reachability check (``GET /``). Skipped while another request runs (it proves it anyway)."""
        import httpx

        if not self._req_lock.acquire(timeout=self._config.CONNECT_TIMEOUT_S if wait else 0):
            return self.snapshot().connected
        try:
            timeout = httpx.Timeout(self._config.CONNECT_TIMEOUT_S)
            self._client.request(self._config.METHOD, self._url(self._config.HEALTH_PATH), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._set_connected(False, type(exc).__name__)
            return False
        finally:
            self._req_lock.release()
        self._set_connected(True)
        return True

    def _watch(self) -> None:
        while not self._stop.is_set():
            self._check()
            self._stop.wait(float(self._config.HEALTH_INTERVAL_S))

    def _set_connected(self, on: bool, error: str | None = None) -> None:
        with self._lock:
            old = self._snap
            if on:
                new = replace(old, connected=True, responsive=True, state=DeviceState.READY, homed=True,
                              slot=None, in_flight=None)
            else:
                new = replace(old, connected=False, responsive=False, state=DeviceState.UNKNOWN,
                              homed=None, last_error=error or old.last_error)
            self._snap = new
        if (old.connected, old.state) != (new.connected, new.state):
            log.info("Wi-Fi dispenser %s", "connected" if on else "offline")
            if self._bus is not None:
                from tactidose.core.bus import Topic

                self._bus.publish(Topic.DEVICE_STATE, new.to_dict())

    def _probe(self, cmd: Command, code: str) -> CommandResult:
        if self._check(wait=True):
            return CommandResult(command=cmd, ok=True, code=code)
        return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail=f"{self.base_url} not reachable")

    @staticmethod
    def _unsupported(cmd: Command, what: str) -> CommandResult:
        return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail=f"{what} is {_UNSUPPORTED}")


def _msg(line: str) -> Message:
    m = parse_message(line)
    assert m is not None
    return m
