"""Host-side client for the TactiDose motor controller (docs/SERIAL_PROTOCOL.md §9).

:class:`HardwareClient` implements ``core.interfaces.HardwareController`` over any
:class:`~tactidose.hardware.transports.Transport` (USB serial, ``socket://`` TCP simulator,
in-process simulator). It only *executes* deterministic commands; deciding whether
anything may move is the job of ``medication/`` (handoff §13/§33).

Threads
-------
* ``hw-supervisor`` - opens the link (``transport_factory``; in serial mode the port is
  re-resolved on every attempt), waits ``hw_boot_wait_s`` for a possible auto-reset boot
  banner, handshakes with ``PING`` (+ retries) and ``STATUS``, auto-homes if configured,
  sends heartbeat ``PING`` s when idle, re-synchronises with ``STATUS`` after a timeout and
  reconnects with exponential backoff (0.5 s .. ``hw_reconnect_max_s``).
* ``hw-reader`` (one per link) - bytes -> lines -> :class:`DeviceSnapshot` updates, in-flight
  command resolution (``protocol.classify``) and event dispatch.

Safety rules implemented here
-----------------------------
* At most one normal command in flight (others get ``BUSY_LOCAL`` immediately); ``stop()``
  bypasses that lock and is written even while another command is waiting.
* A command that was written but got no terminal reply (timeout, link loss) is *uncertain*
  (``definitive=False``; ``gate_may_be_open`` for ``DROP_SLOT``/``DISPENSE_SLOT``/``OPEN_GATE``).
  A write that raised counts as written (conservative). Nothing is ever retried automatically.
* After a timeout the next command is preceded by a ``STATUS`` resync; if the device does
  not answer it, the command is not sent (``NOT_CONNECTED``).
* Device-level failures never raise; they are returned as ``CommandResult``.

Pill drops (protocol v1.1, docs §12.5)
--------------------------------------
:meth:`HardwareClient.drop_slot` sends ``DROP_SLOT n`` when the last ``STATUS`` reported
``proto >= 1.1``; for v1 firmware it emulates the drop as one locked sequence ``DISPENSE_SLOT n``
-> wait ``drop_close_delay_ms`` -> ``CLOSE_GATE``. Either way ``protocol.drop_certainty(result)``
tells the caller whether a pill dropped (DROPPED / NOT_DROPPED / UNCERTAIN). Deciding *whether*
to drop is the job of ``medication/drops.py``; this module only executes.

This is a hackathon prototype for demonstrations with candy/tokens - not a medical device.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from tactidose.core.bus import Topic
from tactidose.core.interfaces import DeviceSnapshot
from tactidose.hardware.ports import resolve_port
from tactidose.hardware.protocol import (
    BUSY_STATES,
    Command,
    CommandName,
    CommandResult,
    DeviceState,
    Disposition,
    Err,
    Ev,
    GateState,
    HostCode,
    Message,
    MessageKind,
    Ok,
    ParsedCommand,
    ProtocolError,
    StatusReport,
    classify,
    parse_command,
    parse_message,
    supports_drop_slot,
)
from tactidose.hardware.transports import PySerialTransport, Transport, TransportError

if TYPE_CHECKING:
    from tactidose.config import Settings
    from tactidose.core.bus import EventBus
    from tactidose.core.clock import Clock
    from tactidose.core.interfaces import HardwareController
    from tactidose.hardware.simulator import SimulatedDevice

log = logging.getLogger(__name__)

__all__ = ["HardwareClient", "NullHardware", "create_hardware"]

HANDSHAKE_PING_ATTEMPTS = 3
#: Consecutive unanswered commands/heartbeats before ``responsive`` turns False.
UNRESPONSIVE_AFTER_FAILURES = 2
RECONNECT_MIN_S = 0.5
SUPERVISOR_TICK_S = 0.1
#: While a STATUS resync is pending and the device still answers, retry this often.
RESYNC_RETRY_S = 0.25
#: Max wait for the command lock by internal (supervisor) commands.
INTERNAL_LOCK_WAIT_S = 5.0
#: In sim mode the device boots right after the link opens: wait this long for its banner.
SIM_BOOT_BANNER_WAIT_S = 0.5
READ_CHUNK = 512
#: A "line" longer than this without a newline is flushed as noise.
MAX_LINE_BYTES = 1024
JOIN_TIMEOUT_S = 2.0

_PROBES = frozenset({CommandName.PING, CommandName.STATUS})
_SLOT_COMMANDS = frozenset({CommandName.MOVE_SLOT, CommandName.DISPENSE_SLOT, CommandName.DROP_SLOT})
_SLOT_FACTORIES: dict[CommandName, Callable[[int, int], Command]] = {
    CommandName.MOVE_SLOT: Command.move_slot,
    CommandName.DISPENSE_SLOT: Command.dispense_slot,
    CommandName.DROP_SLOT: Command.drop_slot,
}
_FAULT_ERRORS = frozenset({Err.HOME_TIMEOUT.value, Err.MOTOR_FAULT.value})
_GATE_NOT_CLOSED_NOTICE = (
    "The dispenser gate did not close after a pill drop. Please check the device."
)


def _any_reply(msg: Message) -> Disposition:
    return Disposition.SUCCESS if msg.kind in (MessageKind.OK, MessageKind.ERR) else Disposition.UNRELATED


class _Pending:
    """One command (or raw diagnostic line) waiting for its terminal message."""

    __slots__ = ("command", "line", "accept", "written", "messages", "done", "result", "reply", "started")

    def __init__(
        self,
        command: Command | None,
        line: str,
        accept: Callable[[Message], Disposition],
    ) -> None:
        self.command = command
        self.line = line
        self.accept = accept
        self.written = False
        self.messages: list[Message] = []
        self.done = threading.Event()
        self.result: CommandResult | None = None
        self.reply: Message | None = None
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def finish(self, result: CommandResult | None, reply: Message | None = None) -> None:
        if self.done.is_set():
            return
        self.result = result
        self.reply = reply
        self.done.set()


def _placeholder_command(name: CommandName | None, slot: object = None) -> Command:
    """A Command to carry a host-side INVALID_ARGUMENT result (CommandResult needs one)."""
    if name is not None:
        try:
            if name in _SLOT_COMMANDS:
                return Command(name, slot)  # type: ignore[arg-type]
            return Command(name)
        except (ProtocolError, TypeError):
            pass
    return Command.ping()


def _build_slot_command(name: CommandName, slot: object, num_slots: int) -> Command | CommandResult:
    """The validated slot command, or an ``INVALID_ARGUMENT`` result (nothing is sent)."""
    try:
        return _SLOT_FACTORIES[name](slot, num_slots)  # type: ignore[arg-type]
    except ProtocolError as exc:
        return CommandResult.host_failure(
            _placeholder_command(name, slot), HostCode.INVALID_ARGUMENT, detail=str(exc)
        )


def _invalid_line_result(line: str, parsed: ParsedCommand) -> CommandResult:
    if parsed.empty:
        reason = "empty line"
    else:
        reason = f"the device would answer ERR {(parsed.error or Err.UNKNOWN_COMMAND).value}"
    return CommandResult.host_failure(
        _placeholder_command(parsed.name),
        HostCode.INVALID_ARGUMENT,
        detail=f"{line.strip()!r} not sent: {reason}"[:200],
    )


# =========================================================================== HardwareClient


class HardwareClient:
    """``HardwareController`` over a serial-like :class:`Transport` (see module docs).

    ``transport_factory`` returns an *open* transport or raises ``TransportError``; default:
    the in-process link of ``sim_device`` if given, else ``PySerialTransport`` on
    ``resolve_port(settings.serial_port)``. If ``sim_device`` is given the client owns it:
    :meth:`start` starts it (right after the first link opens, so its boot banner is seen)
    and :meth:`close` closes it.
    """

    def __init__(
        self,
        settings: "Settings",
        *,
        bus: "EventBus | None" = None,
        clock: "Clock | None" = None,
        transport_factory: Callable[[], Transport] | None = None,
        mode: str = "serial",
        sim_device: "SimulatedDevice | None" = None,
    ) -> None:
        self.settings = settings
        self.num_slots: int = settings.num_slots
        self.mode = mode
        self._bus = bus
        self._clock = clock
        self._sim = sim_device
        if transport_factory is None:
            transport_factory = sim_device.open_transport if sim_device is not None else self._open_serial
        self._factory: Callable[[], Transport] = transport_factory

        self._lock = threading.RLock()          # snapshot, pendings, link, listeners
        self._cmd_lock = threading.Lock()       # one normal command in flight
        self._stop_lock = threading.Lock()      # serialises STOPs (never blocks normal commands)
        self._write_lock = threading.Lock()     # whole lines on the wire
        self._pub_lock = threading.RLock()      # ordered DEVICE_STATE publication

        self._transport: Transport | None = None
        self._link_id = 0
        self._pending: _Pending | None = None
        self._pending_stop: _Pending | None = None
        self._listeners: list[Callable[[Message], None]] = []
        self._probe_active = False
        self._resync_needed = False
        self._failures = 0
        self._last_rx: float | None = None
        self._last_valid_rx: float | None = None
        self._last_probe = 0.0
        self._boot_seen = threading.Event()
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._started = False
        self._closed = False
        self._backoff = RECONNECT_MIN_S
        self._connect_failures = 0
        self._supervisor: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        port = "sim://" if sim_device is not None else settings.serial_port
        self._snap = DeviceSnapshot(mode=mode, port=port)
        self._published: DeviceSnapshot | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the supervisor (non-blocking, never raises). Idempotent."""
        try:
            with self._lock:
                if self._started or self._closed:
                    return
                self._started = True
            self._publish_state()
            thread = threading.Thread(target=self._supervise, name="hw-supervisor", daemon=True)
            self._supervisor = thread
            thread.start()
        except Exception:  # noqa: BLE001 - start() must never raise
            log.exception("could not start the hardware client")

    def close(self) -> None:
        """Disconnect, stop the threads (and an owned simulator). Idempotent, never raises."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._closing.set()
        self._wake.set()
        try:
            self._drop_link("client closed", notify=False)
            current = threading.current_thread()
            for thread in (self._supervisor, self._reader):
                if thread is not None and thread is not current and thread.is_alive():
                    thread.join(JOIN_TIMEOUT_S)
            if self._sim is not None:
                self._sim.close()
            self._publish_state()
        except Exception:  # noqa: BLE001
            log.exception("error while closing the hardware client")

    def reconnect(self) -> bool:
        """Drop the link and reconnect immediately (``POST /api/hardware/reconnect``).

        Refused (returns False) while a command is in flight or before :meth:`start`.
        """
        if not self._started or self._closed:
            return False
        if not self._cmd_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if self._pending_stop is not None:
                    return False
            self._backoff = RECONNECT_MIN_S
            self._drop_link("reconnect requested", notify=False)
        finally:
            self._cmd_lock.release()
        self._wake.set()
        return True

    def __enter__(self) -> "HardwareClient":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ HardwareController
    def snapshot(self) -> DeviceSnapshot:
        with self._lock:
            return replace(self._snap, last_rx_age_s=self._rx_age_locked())

    def ping(self) -> CommandResult:
        return self._execute(Command.ping())

    def status(self) -> CommandResult:
        return self._execute(Command.status())

    def home(self) -> CommandResult:
        return self._execute(Command.home())

    def move_slot(self, slot: int) -> CommandResult:
        return self._slot_command(CommandName.MOVE_SLOT, slot)

    def dispense_slot(self, slot: int) -> CommandResult:
        return self._slot_command(CommandName.DISPENSE_SLOT, slot)

    def drop_slot(self, slot: int) -> CommandResult:
        """Drop one pill from container ``slot`` (docs §12.5); blocks until the drop is over.

        * Device reported ``proto >= 1.1``: ``DROP_SLOT n`` (timeout ``timeout_drop_s``);
          success = ``OK DROPPED n``, ``ERR NO_PILL`` = drop sensor saw nothing.
        * v1 firmware: ``DISPENSE_SLOT n``, then - only if the gate opened - wait
          ``drop_close_delay_ms`` and ``CLOSE_GATE``, all under one command lock. Returns the
          ``DISPENSE_SLOT`` result (``OK GATE_OPEN`` = DROPPED) with the ``CLOSE_GATE`` outcome in
          ``detail`` and ``elapsed_s`` covering the whole sequence; a failed close publishes a
          ``Topic.NOTICE`` (the gate may still be open).

        Refusals carry the ``DROP_SLOT`` command: ``INVALID_ARGUMENT`` (bad slot, nothing sent),
        ``NOT_CONNECTED``, ``BUSY_LOCAL``. Use ``protocol.drop_certainty(result)`` for the outcome.
        """
        cmd = _build_slot_command(CommandName.DROP_SLOT, slot, self.num_slots)
        if isinstance(cmd, CommandResult):
            return cmd
        return self._guarded(cmd, lambda: self._drop_locked(cmd))

    def open_gate(self) -> CommandResult:
        return self._execute(Command.open_gate())

    def close_gate(self) -> CommandResult:
        return self._execute(Command.close_gate())

    def stop(self) -> CommandResult:
        """Send ``STOP`` now, even while another command is in flight (that one then
        resolves with ``ERR STOPPED``). Waits for ``OK STOPPED`` up to ``timeout_stop_s``."""
        cmd = Command.stop()
        if self._closing.is_set():
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="client closed")
        if not self._link_up():
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="no hardware link")
        with self._stop_lock:
            return self._roundtrip(cmd, stop=True)

    # ------------------------------------------------------------------ buzzer (optional extension)
    def buzzer_on(self, ms: int) -> CommandResult:
        """``BUZZER ON <ms>`` (docs §13). Never waits for another command: if one is in flight the
        result is ``BUSY_LOCAL`` at once. While it is in flight, a drop request waits for it (like a
        heartbeat probe) instead of being refused. ``ERR UNKNOWN_COMMAND`` = firmware without the
        extension, ``ERR NO_BUZZER`` = no buzzer fitted. Timeout: ``buzzer_config.COMMAND_TIMEOUT_S``."""
        try:
            cmd = Command.buzzer_on(int(ms))
        except (ProtocolError, TypeError, ValueError) as exc:
            return CommandResult.host_failure(Command.buzzer_off(), HostCode.INVALID_ARGUMENT, detail=str(exc))
        return self._buzzer(cmd)

    def buzzer_off(self) -> CommandResult:
        return self._buzzer(Command.buzzer_off())

    def buzzer_query(self) -> CommandResult:
        """``BUZZER`` -> ``OK BUZZER ON <remaining ms>`` / ``OK BUZZER OFF`` (also the capability probe)."""
        return self._buzzer(Command.buzzer_query())

    def _buzzer(self, cmd: Command) -> CommandResult:
        if self._closing.is_set():
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="client closed")
        if not self._is_connected():
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="hardware not connected")
        result = self._try_probe(cmd)   # probe-style lock: a drop waits for it, it never waits for a drop
        if result is None:
            busy = self._in_flight_line()
            return CommandResult.host_failure(
                cmd, HostCode.BUSY_LOCAL, detail=f"{busy} in flight" if busy else "another command is in flight"
            )
        return result

    def send_raw(self, line: str) -> CommandResult:
        """Demo console: validate with ``protocol.parse_command``; invalid lines are not sent."""
        parsed = parse_command(line, self.num_slots)
        if parsed.command is None:
            return _invalid_line_result(line, parsed)
        if parsed.command.name is CommandName.STOP:
            return self.stop()
        return self._execute(parsed.command)

    def add_event_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        """Receive every ``EVENT`` message, plus *unsolicited* ``ERR HOME_TIMEOUT`` /
        ``ERR MOTOR_FAULT`` (e.g. boot-time homing failed). Runs on ``hw-reader``: be quick."""
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    # ------------------------------------------------------------------ diagnostics
    def exchange_raw(self, line: str, *, timeout_s: float = 2.0) -> Message | None:
        """Diagnostics only (``hw-test``): write ``line`` *without validation* and return the
        first ``OK``/``ERR`` reply (None on timeout, busy or no link).

        Meant for lines the firmware must reject (``FOO``). Valid commands other than
        PING/STATUS are refused here - they must go through the normal command path."""
        parsed = parse_command(line, self.num_slots)
        if parsed.command is not None and parsed.command.name not in _PROBES:
            log.warning("exchange_raw refused %r: use the command methods for real commands", line)
            return None
        if not self._is_connected() or not self._acquire_cmd_lock(internal=False):
            return None
        try:
            pending = _Pending(None, line, _any_reply)
            with self._lock:
                transport, link_id = self._transport, self._link_id
                if transport is None:
                    return None
                self._pending = pending
                self._snap = replace(self._snap, in_flight=line)
            self._publish_state()
            self._send(pending, transport, link_id, (line + "\n").encode("ascii", errors="replace"), line)
            pending.done.wait(timeout_s)
            with self._lock:
                if self._pending is pending:
                    self._pending = None
                pending.finish(None)
                self._snap = replace(self._snap, in_flight=self._in_flight_locked())
            self._publish_state()
            return pending.reply
        finally:
            self._cmd_lock.release()

    # ------------------------------------------------------------------ command path
    def _slot_command(self, name: CommandName, slot: int) -> CommandResult:
        cmd = _build_slot_command(name, slot, self.num_slots)
        if isinstance(cmd, CommandResult):
            return cmd
        return self._execute(cmd)

    def _execute(self, cmd: Command, *, internal: bool = False) -> CommandResult:
        return self._guarded(cmd, lambda: self._execute_locked(cmd), internal=internal)

    def _guarded(
        self, cmd: Command, body: Callable[[], CommandResult], *, internal: bool = False
    ) -> CommandResult:
        """Run ``body`` holding the command lock; refusals are reported against ``cmd``."""
        if self._closing.is_set():
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="client closed")
        if not internal and not self._is_connected():
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="hardware not connected")
        if not self._acquire_cmd_lock(internal=internal):
            busy = self._in_flight_line()
            return CommandResult.host_failure(
                cmd, HostCode.BUSY_LOCAL, detail=f"{busy} in flight" if busy else "another command is in flight"
            )
        try:
            if not internal and not self._is_connected():
                return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="hardware not connected")
            return body()
        finally:
            self._cmd_lock.release()

    def _acquire_cmd_lock(self, *, internal: bool) -> bool:
        if self._cmd_lock.acquire(blocking=False):
            return True
        if internal:
            return self._cmd_lock.acquire(timeout=INTERNAL_LOCK_WAIT_S)
        if self._probe_active:
            # A heartbeat/resync probe holds the lock for at most one PING/STATUS round trip:
            # wait for it rather than failing a user command with BUSY_LOCAL.
            wait = max(self.settings.timeout_ping_s, self.settings.timeout_status_s) + 0.5
            return self._cmd_lock.acquire(timeout=wait)
        return False

    def _try_probe(self, cmd: Command) -> CommandResult | None:
        """Heartbeat / resync from the supervisor; skipped (None) if a command is in flight."""
        self._probe_active = True  # set before acquiring so a racing user command waits
        if not self._cmd_lock.acquire(blocking=False):
            self._probe_active = False
            return None
        try:
            return self._execute_locked(cmd)
        finally:
            self._probe_active = False
            self._cmd_lock.release()

    def _execute_locked(self, cmd: Command) -> CommandResult:
        failure = self._resync_failure(cmd)
        return failure if failure is not None else self._roundtrip(cmd)

    def _resync_failure(self, cmd: Command) -> CommandResult | None:
        """After a timeout: resync with STATUS first; a failed resync means ``cmd`` is not sent."""
        if not self._resync_needed or cmd.name in _PROBES:
            return None
        sync = self._roundtrip(Command.status())
        if sync.ok:
            return None
        return CommandResult.host_failure(
            cmd, HostCode.NOT_CONNECTED, detail=f"not sent: STATUS resync failed ({sync.code})"
        )

    # ------------------------------------------------------------------ pill drops (§12.5)
    def _drop_locked(self, cmd: Command) -> CommandResult:
        failure = self._resync_failure(cmd)     # a resync may also update the device's proto
        if failure is not None:
            return failure
        with self._lock:
            proto = self._snap.proto
        if supports_drop_slot(proto):
            return self._roundtrip(cmd)
        assert cmd.slot is not None
        return self._emulated_drop_locked(cmd.slot)

    def _emulated_drop_locked(self, slot: int) -> CommandResult:
        """v1 firmware: ``DISPENSE_SLOT n`` (the release opens, the pill falls), wait, ``CLOSE_GATE``."""
        started = time.monotonic()
        dispense = Command.dispense_slot(slot, self.num_slots)
        result = self._roundtrip(dispense)
        if not result.ok:
            return result           # never opened (ERR ...) or uncertain: nothing more to do here
        delay_ms = int(self.settings.drop_close_delay_ms)
        self._closing.wait(delay_ms / 1000.0)
        close = self._execute_locked(Command.close_gate())
        detail = f"v1 emulation: CLOSE_GATE after {delay_ms} ms -> {close.hardware_result}"
        if not close.ok:
            gate = self.snapshot().gate
            log.warning("hardware: pill drop from slot %d: %s (gate %s)", slot, detail, gate.value)
            if gate is not GateState.CLOSED:
                self._notice("error", _GATE_NOT_CLOSED_NOTICE)
        return replace(result, detail=detail, elapsed_s=round(time.monotonic() - started, 3))

    def _roundtrip(self, cmd: Command, *, stop: bool = False) -> CommandResult:
        line = cmd.to_line()
        pending = _Pending(cmd, line, lambda msg: classify(cmd, msg))
        with self._lock:
            transport, link_id = self._transport, self._link_id
            if transport is None:
                return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="no hardware link")
            if stop:
                self._pending_stop = pending
            else:
                self._pending = pending
            self._snap = replace(self._snap, in_flight=self._in_flight_locked())
        self._publish_state()
        self._send(pending, transport, link_id, cmd.encode(), line)
        timeout = self.settings.command_timeout_s(cmd.name)
        timed_out = False
        if not pending.done.wait(timeout):
            with self._lock:
                if not pending.done.is_set():
                    self._fail(pending, HostCode.TIMEOUT, f"no reply within {timeout:g} s")
                    timed_out = True
        with self._lock:
            if stop and self._pending_stop is pending:
                self._pending_stop = None
            elif not stop and self._pending is pending:
                self._pending = None
            result = pending.result
            assert result is not None
            changes: dict[str, Any] = {"in_flight": self._in_flight_locked()}
            if not result.ok:
                changes["last_error"] = result.code
            self._snap = replace(self._snap, **changes)
        if timed_out:
            self._note_timeout(cmd)
        self._publish_state()
        if not result.ok:
            log.info("hardware: %s", result.summary if not result.detail else f"{result.summary} ({result.detail})")
        return result

    def _send(self, pending: _Pending, transport: Transport, link_id: int, data: bytes, line: str) -> None:
        self._publish_line("tx", line)
        try:
            with self._write_lock:
                pending.written = True  # a write that raises may still have reached the device
                transport.write(data)
        except TransportError as exc:
            self._on_link_lost(link_id, f"write failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - treat any transport bug as link loss
            log.exception("unexpected transport write error")
            self._on_link_lost(link_id, f"write failed: {exc!r}")

    def _fail(self, pending: _Pending, code: HostCode, detail: str) -> None:
        """Resolve ``pending`` with a host-side failure (caller holds ``_lock``)."""
        if pending.done.is_set():
            return
        if pending.command is None:
            pending.finish(None)
            return
        pending.finish(
            CommandResult.host_failure(
                pending.command, code, messages=tuple(pending.messages),
                elapsed_s=pending.elapsed(), detail=detail,
            )
        )

    def _note_timeout(self, cmd: Command) -> None:
        went_silent = False
        with self._lock:
            self._failures += 1
            failures = self._failures
            connected = self._snap.connected
            self._resync_needed = True
            if failures >= UNRESPONSIVE_AFTER_FAILURES and self._snap.responsive:
                self._snap = replace(self._snap, responsive=False)
                went_silent = True
        log.log(logging.WARNING if connected else logging.INFO,
                "hardware: %s got no reply (%d in a row)", cmd.to_line(), failures)
        if went_silent:
            self._notice("warning", "The dispenser is not answering. Please check the device.")
        self._wake.set()

    # ------------------------------------------------------------------ supervisor
    def _supervise(self) -> None:
        while not self._closing.is_set():
            try:
                if not self._link_up():
                    self._connect_cycle()
                else:
                    self._maintain()
            except Exception:  # noqa: BLE001 - the supervisor must survive anything
                log.exception("hardware supervisor error")
                self._wait(1.0)

    def _connect_cycle(self) -> None:
        link_id = self._open_link()
        if link_id is not None and self._handshake(link_id):
            self._backoff = RECONNECT_MIN_S
            return
        if link_id is not None:
            self._on_link_lost(link_id, "handshake failed", notify=False)
        if self._closing.is_set():
            return
        delay = self._backoff
        self._backoff = min(self._backoff * 2.0, max(RECONNECT_MIN_S, self.settings.hw_reconnect_max_s))
        self._wait(delay)

    def _open_serial(self) -> Transport:
        port = resolve_port(self.settings.serial_port)
        if port is None:
            raise TransportError(
                "no ESP32 serial port found (auto-detect matched no known USB VID:PID); "
                "plug the board in or set TACTIDOSE_SERIAL_PORT"
            )
        with self._lock:
            self._snap = replace(self._snap, port=port)
        return PySerialTransport(port, self.settings.serial_baud)

    def _open_link(self) -> int | None:
        """Open a transport and start its reader; returns the new link id (None on failure)."""
        try:
            transport = self._factory()
        except Exception as exc:  # noqa: BLE001 - TransportError or anything a factory raises
            self._connect_failures += 1
            first = self._connect_failures == 1
            log.log(
                logging.WARNING if first or self._connect_failures % 50 == 0 else logging.DEBUG,
                "hardware connect attempt %d failed: %s", self._connect_failures, exc,
            )
            self._start_sim()
            return None
        with self._lock:
            if self._closing.is_set():
                stale: Transport | None = transport
            else:
                stale = None
                self._link_id += 1
                link_id = self._link_id
                self._transport = transport
                self._boot_seen.clear()
                self._failures = 0
                self._resync_needed = False
                self._snap = replace(
                    self._snap, port=getattr(transport, "name", None) or self._snap.port,
                    connected=False, responsive=False,
                )
        if stale is not None:
            stale.close()
            return None
        self._connect_failures = 0
        reader = threading.Thread(
            target=self._read_loop, args=(transport, link_id), name="hw-reader", daemon=True
        )
        self._reader = reader
        reader.start()
        log.info("hardware link open: %s", getattr(transport, "name", transport))
        if self._start_sim():
            self._boot_seen.wait(SIM_BOOT_BANNER_WAIT_S)
        return link_id

    def _start_sim(self) -> bool:
        """Start an owned simulator on first use; True if it was started now."""
        sim = self._sim
        if sim is None or sim.started or self._closing.is_set():
            return False
        sim.start()
        return True

    def _link_current(self, link_id: int) -> bool:
        with self._lock:
            return self._transport is not None and self._link_id == link_id and not self._closing.is_set()

    def _handshake(self, link_id: int) -> bool:
        wait_s = float(self.settings.hw_boot_wait_s)
        deadline = time.monotonic() + wait_s
        while wait_s > 0 and self._link_current(link_id):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._boot_seen.wait(min(remaining, 0.1)):
                break
        pong: CommandResult | None = None
        for attempt in range(1, HANDSHAKE_PING_ATTEMPTS + 1):
            if not self._link_current(link_id):
                return False
            result = self._execute(Command.ping(), internal=True)
            if result.ok:
                pong = result
                break
            if result.code in (HostCode.DISCONNECTED.value, HostCode.NOT_CONNECTED.value):
                return False
            log.info("handshake PING %d/%d failed: %s", attempt, HANDSHAKE_PING_ATTEMPTS, result.hardware_result)
        if pong is None:
            log.warning("device on %s does not answer PING; will retry", self._snap.port)
            return False
        status = self._execute(Command.status(), internal=True)
        if not status.ok:
            log.warning("device on %s did not answer STATUS (%s); will retry", self._snap.port, status.code)
            return False
        report = StatusReport.parse(status.messages[-1])
        with self._lock:
            # The link may have dropped (or close() started) after STATUS was answered.
            if self._transport is None or self._link_id != link_id or self._closing.is_set():
                return False
            self._snap = replace(self._snap, connected=True, responsive=True)
            self._failures = 0
            snap = self._snap
        self._publish_state()
        log.info(
            "hardware connected on %s: fw=%s proto=%s drop_sensor=%s state=%s homed=%s slot=%s "
            "gate=%s slots=%s",
            snap.port, snap.fw_version, snap.proto, snap.drop_sensor, snap.state.value, snap.homed,
            snap.slot, snap.gate.value, snap.num_slots_reported,
        )
        self._after_connect(report)
        return True

    def _after_connect(self, report: StatusReport) -> None:
        if report.num_slots is not None and report.num_slots != self.num_slots:
            self._notice(
                "warning",
                f"The device reports {report.num_slots} compartments but TACTIDOSE_NUM_SLOTS is "
                f"{self.num_slots}. Fix the configuration before dispensing.",
            )
        if not supports_drop_slot(report.proto):
            log.info("device firmware %s has no DROP_SLOT (protocol v1): pill drops use "
                     "DISPENSE_SLOT + CLOSE_GATE", report.fw or "?")
        snap = self.snapshot()
        if snap.state is DeviceState.FAULT:
            self._notice(
                "error",
                "The dispenser reports a fault (homing or motor failure). A caregiver must check "
                "it and re-home it before anything can be dispensed.",
            )
            return
        if not self.settings.hw_auto_home:
            return
        if not self._cmd_lock.acquire(timeout=INTERNAL_LOCK_WAIT_S):
            return
        try:
            # Re-check under the lock: a user command may have run since the handshake.
            snap = self.snapshot()
            if (not snap.connected or snap.homed is True or snap.state in BUSY_STATES
                    or snap.state is DeviceState.FAULT or self._closing.is_set()):
                return
            log.info("auto-homing after connect (device state %s)", snap.state.value)
            result = self._execute_locked(Command.home())
        finally:
            self._cmd_lock.release()
        if not result.ok:
            self._notice("warning", f"Automatic homing failed ({result.hardware_result}).")

    def _maintain(self) -> None:
        now = time.monotonic()
        with self._lock:
            connected = self._snap.connected
            responsive = self._snap.responsive
            last_rx = self._last_valid_rx or 0.0
            resync = self._resync_needed
        heartbeat = float(self.settings.hw_heartbeat_s)
        if connected:
            if resync:
                if now - self._last_probe >= (RESYNC_RETRY_S if responsive else heartbeat):
                    self._last_probe = now
                    self._try_probe(Command.status())
            elif now - self._last_probe >= heartbeat and now - last_rx >= heartbeat:
                self._last_probe = now
                self._try_probe(Command.ping())
        self._wait(SUPERVISOR_TICK_S)

    def _wait(self, seconds: float) -> None:
        if self._closing.is_set():
            return
        self._wake.wait(seconds)
        self._wake.clear()

    # ------------------------------------------------------------------ link handling
    def _link_up(self) -> bool:
        return self._transport is not None

    def _is_connected(self) -> bool:
        with self._lock:
            return self._transport is not None and self._snap.connected

    def _drop_link(self, reason: str, *, notify: bool = True) -> None:
        with self._lock:
            link_id = self._link_id
        self._on_link_lost(link_id, reason, notify=notify)

    def _on_link_lost(self, link_id: int, reason: str, *, notify: bool = True) -> None:
        with self._lock:
            if link_id != self._link_id or self._transport is None:
                return
            transport = self._transport
            self._transport = None
            self._link_id += 1
            was_connected = self._snap.connected
            for p in (self._pending, self._pending_stop):
                if p is not None:
                    self._fail(p, HostCode.DISCONNECTED if p.written else HostCode.NOT_CONNECTED, reason)
            self._resync_needed = False
            self._snap = replace(
                self._snap, connected=False, responsive=False, state=DeviceState.UNKNOWN,
                homed=None, slot=None, target_slot=None, gate=GateState.UNKNOWN,
                last_error=HostCode.DISCONNECTED.value if was_connected else self._snap.last_error,
            )
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            log.debug("error closing transport", exc_info=True)
        if was_connected and notify:
            log.warning("hardware link lost: %s", reason)
            self._notice("warning", "Lost the connection to the dispenser. Reconnecting...")
        else:
            log.info("hardware link closed: %s", reason)
        self._publish_state()
        self._wake.set()

    # ------------------------------------------------------------------ reader
    def _read_loop(self, transport: Transport, link_id: int) -> None:
        buf = bytearray()
        while not self._closing.is_set() and link_id == self._link_id:
            try:
                data = transport.read(READ_CHUNK)
            except TransportError as exc:
                self._on_link_lost(link_id, f"read failed: {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected transport read error")
                self._on_link_lost(link_id, f"read failed: {exc!r}")
                return
            if not data:
                continue
            buf += data
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(buf[:nl])
                del buf[: nl + 1]
                self._on_raw_line(raw, link_id)
            if len(buf) > MAX_LINE_BYTES:
                raw = bytes(buf)
                buf.clear()
                self._on_raw_line(raw, link_id)

    def _on_raw_line(self, raw: bytes, link_id: int) -> None:
        if link_id != self._link_id:
            return
        now = time.monotonic()
        with self._lock:
            self._last_rx = now
        text = raw.decode("ascii", errors="replace").rstrip("\r\n")
        if not text.strip():
            return
        self._publish_line("rx", text)
        msg = parse_message(text)
        if msg is None:
            log.debug("ignoring non-protocol line: %r", text)
            return
        self._on_message(msg, now)

    def _on_message(self, msg: Message, now: float) -> None:
        dispatch: list[Message] = []
        notices: list[tuple[str, str]] = []
        recovered = False
        with self._lock:
            self._last_valid_rx = now
            self._failures = 0
            if self._snap.connected and not self._snap.responsive:
                self._snap = replace(self._snap, responsive=True)
                recovered = True
            was_connected = self._snap.connected
            self._apply(msg)
            pendings = [p for p in (self._pending, self._pending_stop) if p is not None and not p.done.is_set()]
            if msg.kind is MessageKind.EVENT:
                if msg.code == Ev.BOOT.value:
                    self._boot_seen.set()
                    banner = msg.to_line()
                    for p in pendings:
                        self._fail(p, HostCode.DEVICE_RESET, f"{banner} while the command was in flight")
                    if was_connected:
                        notices.append(("warning", "The dispenser restarted (reset or power dip) and is re-homing."))
                dispatch.append(msg)
            else:
                consumed = False
                for p in pendings:
                    disposition = p.accept(msg)
                    if disposition is Disposition.UNRELATED:
                        continue
                    consumed = True
                    p.messages.append(msg)
                    if disposition is Disposition.PROGRESS:
                        continue
                    if p.command is None:
                        p.finish(None, reply=msg)
                    else:
                        p.finish(CommandResult(
                            command=p.command, ok=disposition is Disposition.SUCCESS, code=msg.code,
                            messages=tuple(p.messages), elapsed_s=p.elapsed(),
                        ))
                if not consumed and msg.kind is MessageKind.ERR and msg.code in _FAULT_ERRORS:
                    dispatch.append(msg)
                    notices.append(("error", f"The dispenser reported {msg.code} and needs attention."))
            if msg.is_ok(Ok.STATUS):
                self._resync_needed = False
        if recovered:
            log.info("hardware is answering again")
        self._publish_state()
        for level, text in notices:
            self._notice(level, text)
        for m in dispatch:
            self._dispatch(m)

    def _apply(self, msg: Message) -> None:
        """Update the snapshot conservatively from one message (caller holds ``_lock``)."""
        s = self._snap
        upd: dict[str, Any] = {}
        code = msg.code
        if msg.kind is MessageKind.OK:
            if code == Ok.STATUS.value:
                rep = StatusReport.parse(msg)
                if rep.state is not DeviceState.UNKNOWN:
                    upd["state"] = rep.state
                if rep.homed is not None:
                    upd["homed"] = rep.homed
                upd["slot"] = rep.slot
                if rep.gate is not GateState.UNKNOWN:
                    upd["gate"] = rep.gate
                if rep.num_slots is not None:
                    upd["num_slots_reported"] = rep.num_slots
                if rep.fw:
                    upd["fw_version"] = rep.fw
                # Every STATUS describes the whole device: no proto key = v1 firmware (no DROP_SLOT).
                upd["proto"] = rep.proto
                upd["drop_sensor"] = rep.drop_sensor
                if upd.get("state", s.state) is not DeviceState.MOVING:
                    upd["target_slot"] = None
            elif code == Ok.HOMING.value:
                upd.update(state=DeviceState.HOMING, homed=False, slot=None, target_slot=None, gate=GateState.CLOSED)
            elif code == Ok.HOMED.value:
                upd.update(homed=True, slot=0, target_slot=None)
            elif code == Ok.READY.value:
                upd.update(state=DeviceState.READY, gate=GateState.CLOSED, target_slot=None)
            elif code == Ok.MOVING.value:
                upd.update(state=DeviceState.MOVING, target_slot=msg.slot, slot=None, gate=GateState.CLOSED)
            elif code == Ok.AT_SLOT.value:
                upd.update(state=DeviceState.AT_TARGET, slot=msg.slot, target_slot=None)
            elif code == Ok.GATE_OPEN.value:
                upd.update(state=DeviceState.GATE_OPEN, gate=GateState.OPEN)
            elif code == Ok.GATE_CLOSED.value:
                upd.update(gate=GateState.CLOSED)
            elif code == Ok.DROPPED.value:
                # The release cycle is over (gate closed); OK READY follows and sets the state.
                upd.update(gate=GateState.CLOSED, target_slot=None)
                if msg.slot is not None:
                    upd["slot"] = msg.slot
            elif code == Ok.STOPPED.value:
                upd.update(state=DeviceState.SAFE_STOP, homed=False, gate=GateState.CLOSED, slot=None, target_slot=None)
        elif msg.kind is MessageKind.ERR:
            upd["last_error"] = code
            if code in _FAULT_ERRORS:
                upd.update(state=DeviceState.FAULT, homed=False, slot=None, target_slot=None)
        elif code == Ev.BOOT.value:
            upd.update(
                state=DeviceState.BOOT, homed=False, gate=GateState.CLOSED, slot=None, target_slot=None,
                resets_seen=s.resets_seen + 1,
            )
            if msg.args:
                upd["fw_version"] = msg.args[0]
        if upd:
            self._snap = replace(s, **upd)

    # ------------------------------------------------------------------ publication
    def _rx_age_locked(self) -> float | None:
        return None if self._last_rx is None else round(time.monotonic() - self._last_rx, 3)

    def _in_flight_locked(self) -> str | None:
        p = self._pending or self._pending_stop
        return p.line if p is not None else None

    def _in_flight_line(self) -> str | None:
        with self._lock:
            return self._in_flight_locked()

    def _publish_state(self) -> None:
        if self._bus is None:
            return
        with self._pub_lock:
            with self._lock:
                snap = self._snap
                if snap == self._published:
                    return
                self._published = snap
                data = replace(snap, last_rx_age_s=self._rx_age_locked()).to_dict()
            self._bus.publish(Topic.DEVICE_STATE, data)

    def _publish_line(self, direction: str, line: str) -> None:
        if self._bus is not None:
            self._bus.publish(Topic.DEVICE_LINE, {"dir": direction, "line": line})

    def _notice(self, level: str, message: str) -> None:
        log.log(logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO,
                "hardware notice: %s", message)
        if self._bus is not None:
            self._bus.publish(Topic.NOTICE, {"level": level, "message": message, "source": "hardware"})

    def _dispatch(self, msg: Message) -> None:
        if msg.kind is MessageKind.EVENT and self._bus is not None:
            self._bus.publish(Topic.DEVICE_EVENT, {"code": msg.code, "line": msg.raw or msg.to_line()})
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(msg)
            except Exception:  # noqa: BLE001 - a broken listener must not kill the reader
                log.exception("hardware event listener %r failed", callback)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        s = self._snap
        return f"HardwareClient(mode={self.mode!r}, port={s.port!r}, connected={s.connected}, state={s.state.value})"


# =========================================================================== NullHardware


class NullHardware:
    """``hardware_mode=none``: nothing is connected, every command -> ``NOT_CONNECTED``."""

    def __init__(self, settings: "Settings", bus: "EventBus | None" = None) -> None:
        self.settings = settings
        self.num_slots: int = settings.num_slots
        self._bus = bus
        self._snap = DeviceSnapshot(mode="none", port=None, connected=False, responsive=False)

    def start(self) -> None:
        if self._bus is not None:
            self._bus.publish(Topic.DEVICE_STATE, self._snap.to_dict())

    def close(self) -> None:
        return None

    def reconnect(self) -> bool:
        return False

    def snapshot(self) -> DeviceSnapshot:
        return self._snap

    def _none(self, cmd: Command) -> CommandResult:
        return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED, detail="hardware_mode=none")

    def _slot(self, name: CommandName, slot: int) -> CommandResult:
        cmd = _build_slot_command(name, slot, self.num_slots)
        return cmd if isinstance(cmd, CommandResult) else self._none(cmd)

    def ping(self) -> CommandResult:
        return self._none(Command.ping())

    def status(self) -> CommandResult:
        return self._none(Command.status())

    def home(self) -> CommandResult:
        return self._none(Command.home())

    def move_slot(self, slot: int) -> CommandResult:
        return self._slot(CommandName.MOVE_SLOT, slot)

    def dispense_slot(self, slot: int) -> CommandResult:
        return self._slot(CommandName.DISPENSE_SLOT, slot)

    def drop_slot(self, slot: int) -> CommandResult:
        """Nothing is connected: ``NOT_CONNECTED`` (``INVALID_ARGUMENT`` for a bad slot)."""
        return self._slot(CommandName.DROP_SLOT, slot)

    def open_gate(self) -> CommandResult:
        return self._none(Command.open_gate())

    def close_gate(self) -> CommandResult:
        return self._none(Command.close_gate())

    def stop(self) -> CommandResult:
        return self._none(Command.stop())

    def send_raw(self, line: str) -> CommandResult:
        parsed = parse_command(line, self.num_slots)
        if parsed.command is None:
            return _invalid_line_result(line, parsed)
        return self._none(parsed.command)

    def add_event_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        return lambda: None


# =========================================================================== factory


def create_hardware(
    settings: "Settings",
    *,
    bus: "EventBus | None" = None,
    clock: "Clock | None" = None,
) -> tuple["HardwareController", "SimulatedDevice | None"]:
    """Build the controller for ``settings.hardware_mode`` (ARCHITECTURE §4).

    * ``sim``    -> (HardwareClient over a new SimulatedDevice, that SimulatedDevice). The client
      owns the simulator: ``hardware.start()`` starts it and ``hardware.close()`` closes it.
      The returned device is for the demo panel (faults, button presses, physical view).
    * ``serial`` -> (HardwareClient on ``resolve_port(settings.serial_port)``, None)
    * ``none``   -> (NullHardware, None)
    """
    mode = settings.hardware_mode
    if mode == "sim":
        from tactidose.hardware.simulator import SimulatedDevice

        sim = SimulatedDevice(settings, bus=bus)
        return HardwareClient(settings, bus=bus, clock=clock, mode="sim", sim_device=sim), sim
    if mode == "serial":
        return HardwareClient(settings, bus=bus, clock=clock, mode="serial"), None
    return NullHardware(settings, bus=bus), None
