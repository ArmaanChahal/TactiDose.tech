"""Real-board checks: the ``hw-test`` checklist and the serial conformance target.

* :func:`run_hw_test` - the handoff §29 "Integration" checklist, executed through the
  production :class:`~tactidose.hardware.serial_client.HardwareClient`: PING, STATUS, HOME,
  every MOVE_SLOT, DISPENSE_SLOT + CLOSE_GATE (``repeat`` times), one pill drop per container
  (``DROP_SLOT n``, or the host's v1 emulation ``DISPENSE_SLOT n`` + ``CLOSE_GATE`` when the
  firmware does not report ``proto >= 1.1``), STOP during a move then HOME, an unexpected
  command, and (``interactive``) the confirm/cancel buttons. Prints a PASS/FAIL table and
  returns an exit code (CLI: ``python -m tactidose hw-test --port COM5``).
* :class:`SerialConformanceTarget` - ``ConformanceTarget`` for a real board in wall-clock time
  (``python -m tactidose.hardware.conformance --target serial --port COM5``).

Both move the carousel, open the gate and drop pills: keep hands clear and load candy/tokens only.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from tactidose.hardware.ports import format_ports, resolve_port
from tactidose.hardware.protocol import (
    DEFAULT_BAUD,
    CommandName,
    CommandResult,
    DeviceState,
    Err,
    Ev,
    GateState,
    Message,
    MessageKind,
    Ok,
    StatusReport,
    parse_message,
    supports_drop_slot,
)
from tactidose.hardware.serial_client import HANDSHAKE_PING_ATTEMPTS, HardwareClient
from tactidose.hardware.transports import PySerialTransport, Transport, TransportError

if TYPE_CHECKING:
    from tactidose.config import Settings

log = logging.getLogger(__name__)

__all__ = ["CheckResult", "run_hw_test", "SerialConformanceTarget"]

#: How long the interactive button checks wait for the press.
BUTTON_WAIT_S = 15.0
#: Exit codes of :func:`run_hw_test`.
EXIT_OK, EXIT_FAILED, EXIT_NO_DEVICE = 0, 1, 2

_SAFETY_BANNER = (
    "The carousel will move, the gate will open and one pill drops from every container. "
    "Keep hands clear; use candy/tokens only."
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    elapsed_s: float = 0.0
    skipped: bool = False

    @property
    def verdict(self) -> str:
        return "SKIP" if self.skipped else ("PASS" if self.ok else "FAIL")


def _wait(predicate: Callable[[], bool], timeout_s: float, interval_s: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _describe(result: CommandResult) -> str:
    lines = " / ".join(m.to_line() for m in result.messages) or result.hardware_result
    if not result.ok:
        lines = f"{result.hardware_result}" + (f" [{lines}]" if result.messages else "")
    elif result.detail:
        lines = f"{lines} [{result.detail}]"
    return f"{lines} ({result.elapsed_s:.2f} s)"


def _protocol_label(proto: str | None) -> str:
    return f"protocol {proto}" if proto else "protocol v1, no DROP_SLOT"


class _HwTest:
    """One run of the checklist (kept as a class so every check stays small)."""

    def __init__(self, client: HardwareClient, settings: "Settings", out: Callable[[str], None]) -> None:
        self.client = client
        self.settings = settings
        self.out = out
        self.n = settings.num_slots
        self.results: list[CheckResult] = []
        self.events: "queue.Queue[Message]" = queue.Queue()
        client.add_event_listener(self.events.put)

    # -- helpers ----------------------------------------------------------------
    def check(self, name: str, fn: Callable[[], tuple[bool, str]]) -> CheckResult:
        t0 = time.monotonic()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check
            log.exception("hw-test check %s crashed", name)
            ok, detail = False, f"error: {exc!r}"
        res = CheckResult(name, ok, detail, round(time.monotonic() - t0, 2))
        self.results.append(res)
        self.out(f"  {res.verdict}  {name}: {detail}")
        return res

    def skip(self, name: str, reason: str) -> None:
        res = CheckResult(name, True, reason, skipped=True)
        self.results.append(res)
        self.out(f"  SKIP  {name}: {reason}")

    def wait_state(self, state: DeviceState, timeout_s: float = 2.0) -> bool:
        return _wait(lambda: self.client.snapshot().state is state, timeout_s)

    def wait_not_busy(self, timeout_s: float) -> bool:
        return _wait(lambda: not self.client.snapshot().state.busy, timeout_s)

    # -- checks -----------------------------------------------------------------
    def ping(self) -> tuple[bool, str]:
        r = self.client.ping()
        return r.ok and r.code == "PONG", _describe(r)

    def status(self) -> tuple[bool, str]:
        r = self.client.status()
        if not r.ok:
            return False, _describe(r)
        rep = StatusReport.parse(r.messages[-1])
        line = r.messages[-1].to_line()
        if rep.num_slots is not None and rep.num_slots != self.n:
            return False, f"device reports slots={rep.num_slots}, host expects {self.n}: {line}"
        return True, line

    def home(self) -> tuple[bool, str]:
        r = self.client.home()
        ok = r.ok and r.code == "HOMED" and self.wait_state(DeviceState.READY)
        return ok, _describe(r)

    def move(self, slot: int) -> tuple[bool, str]:
        r = self.client.move_slot(slot)
        ok = r.ok and r.code == "AT_SLOT" and self.wait_state(DeviceState.READY)
        return ok, _describe(r)

    def dispense(self, slot: int) -> tuple[bool, str]:
        r = self.client.dispense_slot(slot)
        return r.ok and r.code == "GATE_OPEN", _describe(r)

    def close_gate(self) -> tuple[bool, str]:
        r = self.client.close_gate()
        ok = r.ok and r.code == "GATE_CLOSED" and self.wait_state(DeviceState.READY)
        return ok, _describe(r)

    def drop(self, slot: int) -> tuple[bool, str]:
        """One pill from container ``slot``: ``OK DROPPED n`` (v1.1) or ``OK GATE_OPEN`` + a
        successful ``CLOSE_GATE`` (v1 emulation); the device must end READY with the gate closed."""
        r = self.client.drop_slot(slot)
        emulated = r.command.name is CommandName.DISPENSE_SLOT
        ok = r.ok and r.code == (Ok.GATE_OPEN.value if emulated else Ok.DROPPED.value)
        detail = _describe(r)
        if r.code == Err.NO_PILL.value:
            detail += (f" - no pill passed the drop sensor: load candy/tokens into container "
                       f"{slot + 1} or check it for a jam")
        if not ok:
            return False, detail
        if not (self.wait_state(DeviceState.READY) and self.client.snapshot().gate is GateState.CLOSED):
            snap = self.client.snapshot()
            return False, f"{detail}; afterwards state {snap.state.value}, gate {snap.gate.value}"
        return True, detail

    def stop_during_move(self) -> tuple[bool, str]:
        current = self.client.snapshot().slot or 0
        target = (current + self.n // 2) % self.n          # the longest move available
        box: dict[str, CommandResult] = {}
        mover = threading.Thread(
            target=lambda: box.setdefault("move", self.client.move_slot(target)),
            name="hw-test-move", daemon=True,
        )
        mover.start()
        moving = _wait(lambda: "move" in box or self.client.snapshot().state is DeviceState.MOVING,
                       2.0, interval_s=0.002)
        if "move" in box or not moving:
            mover.join(self.settings.timeout_move_s + 1)
            got = box.get("move")
            if got is not None and got.ok:
                return False, f"move finished before STOP could be sent ({_describe(got)})"
            return False, f"carousel never reported MOVING ({_describe(got) if got else 'no result'})"
        stop = self.client.stop()
        mover.join(self.settings.timeout_move_s + 1)
        moved = box.get("move")
        if moved is None:
            return False, "MOVE_SLOT did not return"
        if not stop.ok:
            return False, f"STOP: {_describe(stop)}"
        if moved.code != Err.STOPPED.value:
            return False, f"move was not interrupted: {_describe(moved)}"
        if not self.wait_state(DeviceState.SAFE_STOP):
            return False, f"device state {self.client.snapshot().state.value} after STOP"
        return True, f"MOVE_SLOT {target} -> ERR STOPPED, STOP -> OK STOPPED ({stop.elapsed_s:.2f} s)"

    def unknown_command(self) -> tuple[bool, str]:
        reply = self.client.exchange_raw("FOO", timeout_s=self.settings.timeout_ping_s)
        if reply is None:
            return False, "no reply to FOO"
        return reply.is_err(Err.UNKNOWN_COMMAND), f"FOO -> {reply.to_line()}"

    def button(self, which: Ev) -> tuple[bool, str]:
        while not self.events.empty():
            self.events.get_nowait()
        label = which.value.split("_")[0]
        self.out(f"  >>> Press the {label} button now (waiting {BUTTON_WAIT_S:.0f} s) ...")
        deadline = time.monotonic() + BUTTON_WAIT_S
        while time.monotonic() < deadline:
            try:
                msg = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if msg.kind is MessageKind.EVENT and msg.code == which.value:
                return True, msg.to_line()
        return False, f"no EVENT {which.value} within {BUTTON_WAIT_S:.0f} s"

    # -- the checklist ----------------------------------------------------------
    def run(self, repeat: int, interactive: bool) -> None:
        s = self.settings
        self.check("PING", self.ping)
        self.check("STATUS", self.status)
        snap = self.client.snapshot()
        if snap.state.busy:
            self.out("  ... waiting for the device to finish its current motion (boot homing?)")
            self.wait_not_busy(s.timeout_home_s)
        if self.client.snapshot().state is DeviceState.GATE_OPEN:
            self.client.close_gate()
        if not self.check("HOME", self.home).ok:
            self.out("  HOME failed: skipping the motion checks (fix homing first).")
        else:
            for slot in range(self.n):
                self.check(f"MOVE_SLOT {slot}", lambda slot=slot: self.move(slot))
            for i in range(max(1, repeat)):
                slot = (i + 1) % self.n
                suffix = f" (run {i + 1}/{repeat})" if repeat > 1 else ""
                self.check(f"DISPENSE_SLOT {slot}{suffix}", lambda slot=slot: self.dispense(slot))
                self.check(f"CLOSE_GATE{suffix}", self.close_gate)
            v11 = supports_drop_slot(self.client.snapshot().proto)
            for slot in range(self.n):
                name = f"DROP_SLOT {slot}" if v11 else f"DROP_SLOT {slot} (v1 emulation)"
                self.check(name, lambda slot=slot: self.drop(slot))
            self.check("STOP during MOVE_SLOT", self.stop_during_move)
            self.check("HOME after STOP", self.home)
        self.check("Unknown command FOO", self.unknown_command)
        if interactive:
            self.check("Confirm button", lambda: self.button(Ev.CONFIRM_BUTTON))
            self.check("Cancel button", lambda: self.button(Ev.CANCEL_BUTTON))
        else:
            self.skip("Confirm button", "run with --interactive to test the buttons")
            self.skip("Cancel button", "run with --interactive to test the buttons")


def _print_table(results: list[CheckResult], out: Callable[[str], None], title: str) -> None:
    width = max([len(r.name) for r in results] + [5])
    out("")
    out(title)
    out(f" {'#':>2}  {'Check':<{width}}  Result  {'Time':>6}  Detail")
    for i, r in enumerate(results, 1):
        out(f" {i:>2}  {r.name:<{width}}  {r.verdict:<6}  {r.elapsed_s:>5.2f}s  {r.detail}")
    ran = [r for r in results if not r.skipped]
    passed = sum(r.ok for r in ran)
    skipped = len(results) - len(ran)
    out(f"{passed}/{len(ran)} checks passed" + (f", {skipped} skipped" if skipped else ""))


def run_hw_test(
    settings: "Settings",
    *,
    port: str | None = None,
    repeat: int = 1,
    interactive: bool = False,
    out: Callable[[str], None] = print,
    transport_factory: Callable[[], Transport] | None = None,
    connect_timeout_s: float | None = None,
) -> int:
    """Run the §29 integration checklist on a board; returns 0 (all passed), 1 (a check
    failed) or 2 (no port / no answer). ``transport_factory`` overrides the port (tests,
    TCP simulator); auto-home is disabled so that ``HOME`` itself is tested."""
    target = port or settings.serial_port or "auto"
    test_settings = settings.model_copy(
        update={"hardware_mode": "serial", "hw_auto_home": False, "serial_port": target}
    )
    if transport_factory is None:
        resolved = resolve_port(target)
        if resolved is None:
            out("No ESP32 serial port found (auto-detect needs a known USB VID:PID). Ports:")
            out(format_ports())
            out("Plug the board in or pass --port COMx.")
            return EXIT_NO_DEVICE

        def factory() -> Transport:
            return PySerialTransport(resolved, test_settings.serial_baud)

        label = resolved
    else:
        factory, label = transport_factory, "custom transport"

    out(f"TactiDose hardware self-test on {label}. {_SAFETY_BANNER}")
    client = HardwareClient(test_settings, transport_factory=factory, mode="serial")
    client.start()
    try:
        timeout = connect_timeout_s if connect_timeout_s is not None else (
            test_settings.hw_boot_wait_s
            + HANDSHAKE_PING_ATTEMPTS * test_settings.timeout_ping_s
            + test_settings.timeout_status_s + 5.0
        )
        if not _wait(lambda: client.snapshot().connected, timeout, 0.05):
            out(f"FAIL: no answer to PING/STATUS on {label} within {timeout:.0f} s "
                "(wrong port, baud rate or firmware?)")
            return EXIT_NO_DEVICE
        snap = client.snapshot()
        test = _HwTest(client, test_settings, out)
        test.run(max(1, int(repeat)), interactive)
    except KeyboardInterrupt:
        out("Interrupted: sending STOP.")
        client.stop()                      # never leave the carousel moving or the gate open
        raise
    finally:
        client.close()
    _print_table(test.results, out, f"TactiDose hardware self-test - {label} "
                                    f"(fw {snap.fw_version or '?'}, {_protocol_label(snap.proto)})")
    return EXIT_OK if all(r.ok for r in test.results) else EXIT_FAILED


# =========================================================================== conformance


class SerialConformanceTarget:
    """``ConformanceTarget`` for a real board over serial, in wall-clock time.

    ``supports_boot`` works by pulsing RTS (EN low) through the dev board's auto-reset
    circuit; boards without one (or with TinyUSB CDC firmware) cannot be rebooted from the
    host and every scenario's initial ``boot`` step will then fail - power-cycle manually
    or use the simulator/native targets. Only the home-sensor mode ``ok`` exists on real
    hardware; the physical start position is wherever the carousel happens to be.
    ``transport`` / ``reset`` allow injecting a link and a reset action (tests).
    """

    supports_faults = False
    supports_buttons = False
    supports_boot = True
    #: Makes the runner skip ``hardware_safe: false`` scenarios on real boards even though this
    #: target can reboot the device.
    real_hardware = True

    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        *,
        transport: Transport | None = None,
        reset: Callable[[], None] | None = None,
    ) -> None:
        if transport is None:
            resolved = resolve_port(port)
            if resolved is None:
                raise TransportError("no ESP32 serial port found; pass --port COMx")
            transport = PySerialTransport(resolved, baud)
        self.name = f"serial:{transport.name}"
        self._transport = transport
        self._reset_fn = reset
        self._lines: deque[str] = deque()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="hw-conformance-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._closed.is_set():
            try:
                data = self._transport.read(512)
            except TransportError as exc:
                if not self._closed.is_set():
                    log.warning("conformance serial link lost: %s", exc)
                return
            buf += data
            while (nl := buf.find(b"\n")) >= 0:
                text = bytes(buf[:nl]).decode("ascii", errors="replace").rstrip("\r")
                del buf[: nl + 1]
                if parse_message(text) is not None:     # ROM banner, #debug etc. are noise
                    with self._lock:
                        self._lines.append(" ".join(text.split()))

    def _discard(self) -> None:
        with self._lock:
            self._lines.clear()

    def reset(self) -> None:
        self._discard()

    def boot(self, mode: str) -> None:
        if str(mode).strip().lower() != "ok":
            raise NotImplementedError("a real board cannot change its home-sensor mode")
        self._discard()
        if self._reset_fn is not None:
            self._reset_fn()
        elif isinstance(self._transport, PySerialTransport):
            self._transport.pulse_reset()
        else:
            raise NotImplementedError(f"{self.name} cannot reboot the device")

    def send(self, line: str) -> None:
        self._transport.write((line + "\n").encode("ascii", errors="replace"))

    def tick(self, ms: int) -> list[str]:
        if ms > 0:
            time.sleep(ms / 1000.0)
        with self._lock:
            out = list(self._lines)
            self._lines.clear()
        return out

    def set_button(self, name: str, pressed: bool) -> None:
        raise NotImplementedError("buttons must be pressed by a person on real hardware")

    def set_sensor(self, mode: str) -> None:
        raise NotImplementedError("cannot inject sensor faults on real hardware")

    def set_jam(self, on: bool) -> None:
        raise NotImplementedError("cannot inject motor jams on real hardware")

    def set_pills(self, slot: int, count: int) -> None:
        raise NotImplementedError("cannot change the pill count of a real container from the host")

    def close(self) -> None:
        self._closed.set()
        self._transport.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)
