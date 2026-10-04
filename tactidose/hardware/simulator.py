"""Simulated TactiDose motor controller (serial protocol v1.1).

Three layers:

* :class:`VirtualESP32` - a deterministic, tick-driven twin of the reference firmware
  state machine (docs/SERIAL_PROTOCOL.md §3-§8 and §12, including the §7 acceptance table)
  plus a model of the physical carousel, home sensor, servo gate/release, pill containers,
  drop sensor and buttons. No threads and no wall clock: time only advances through
  :meth:`VirtualESP32.tick`, and ``loop()`` semantics are reproduced exactly (one firmware
  iteration per simulated millisecond; idle stretches are skipped because they provably
  have no effect).
* :class:`SimulatedDevice` - runs a VirtualESP32 in (scaled) real time on a ``sim-device``
  thread, serves in-process :class:`~tactidose.hardware.transports.Transport` objects and
  injects faults / sets pill counts for the demo panel and the tests.
* :class:`ConformanceSimTarget` - adapter for ``tactidose.hardware.conformance``.

Physical model
--------------
The carousel position (``physical_steps``) is kept separately from the firmware step
counter (``firmware_position``). Every commanded step moves both, except while the motor
is jammed: then the counter advances but the carousel does not (lost steps). A move that
lost steps is never acknowledged (the simulated driver reports a stall), so the firmware's
motion timeout ``2 x expected + 2 s`` produces ``ERR MOTOR_FAULT``. The home sensor is
active while ``physical_steps mod steps_per_rev`` is in ``[0, sensor_zone_steps)``; homing
records the rising edge, debounces it and returns to the edge, so slot ``k`` is physically
centred at ``round(k * steps_per_rev / N)`` steps after homing.

Buzzer (optional extension, docs/SERIAL_PROTOCOL.md §13): ``BUZZER ON <ms>`` sounds it for
``min(ms, buzzer_max_on_ms)`` (non-blocking, serviced every loop - also during gate travel - so
it never delays a drop), ``BUZZER OFF`` / ``STOP`` / a reboot silence it, ``BUZZER`` queries it.
Fault hook :attr:`VirtualESP32.buzzer_fault` (``SimulatedDevice.set_buzzer_fault``): ``missing``
(old firmware: ``ERR UNKNOWN_COMMAND``), ``no_pin`` (``ERR NO_BUZZER``), ``unresponsive`` (no reply).

Pills (v1.1): every container holds ``pills[k]`` pills (physical, survives reboots). Each time
the gate/release finishes opening, one pill falls from the container that is *physically*
over the chute (if it has any) and breaks the drop-sensor beam. ``DROP_SLOT n`` = move ->
settle -> atomic release (``OK GATE_OPEN``, hold ``drop_open_ms``, ``OK GATE_CLOSED``) ->
``OK DROPPED n`` (or ``ERR NO_PILL`` when a drop sensor is fitted and saw nothing) ->
``OK READY``. ``DISPENSE_SLOT`` / ``OPEN_GATE`` open the same release, so a v1 host (or the
host's v1 emulation against ``proto=None``) drops a pill too.

This is a hackathon prototype for demonstrations with candy/tokens - not a medical device.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from tactidose.core.bus import Topic
from tactidose.hardware.protocol import (
    BUSY_STATES,
    MAX_LINE_LENGTH,
    MAX_SLOTS,
    MIN_SLOTS,
    CommandName,
    DeviceState,
    Err,
    Ev,
    GateState,
    MessageKind,
    Ok,
    StatusReport,
    format_message,
    parse_command,
    supports_drop_slot,
)
from tactidose.hardware.transports import DEFAULT_READ_TIMEOUT_S, TransportError

if TYPE_CHECKING:
    from tactidose.config import Settings
    from tactidose.core.bus import EventBus

log = logging.getLogger(__name__)

__all__ = [
    "BUTTONS",
    "FAULT_NAMES",
    "SIM_PORT_NAME",
    "SIM_PROTO",
    "SIM_FW_V11",
    "SIM_FW_V1",
    "BUZZER_FAULTS",
    "SimConfig",
    "VirtualESP32",
    "SimulatedDevice",
    "ConformanceSimTarget",
]

BUTTONS = ("CONFIRM", "CANCEL")
#: Buzzer fault modes (kept apart from FAULT_NAMES, so the demo panel's fault list is unchanged).
BUZZER_FAULTS = ("missing", "no_pin", "unresponsive")
SENSOR_MODES = ("ok", "dead", "none")
FAULT_NAMES = (
    "home_sensor_dead", "motor_jam", "unresponsive", "brownout_on_gate", "brownout_on_release",
    "disconnect",
)
SIM_PORT_NAME = "sim://"
#: Protocol version the simulator implements (``SimConfig.proto`` default).
SIM_PROTO = "1.1"
SIM_FW_V11 = "sim-1.1.0"
#: Firmware version reported when the simulator plays v1 firmware (``proto=None``).
SIM_FW_V1 = "sim-1.0.0"
#: The reference firmware's BUZZER_MAX_ON_MS default (firmware config.h) - the device's own limit,
#: deliberately not the host's buzzer_config.MAX_ON_MS.
SIM_BUZZER_MAX_ON_MS = 10_000

#: Homing aborts after this many carousel revolutions without finding home (§8.5).
HOMING_MAX_REVS = 1.25
#: The home sensor must stay active (or, when backing off, inactive) this long.
SENSOR_DEBOUNCE_MS = 5
#: Motion timeout = factor x expected duration + extra (§8.6).
MOTION_TIMEOUT_FACTOR = 2
MOTION_TIMEOUT_EXTRA_MS = 2000
#: Firmware UART receive buffer; bytes beyond it are lost while the firmware is blocked.
RX_BUFFER_BYTES = 1024
#: Default physical press length for :meth:`SimulatedDevice.press`.
DEFAULT_PRESS_MS = 100
#: Real-time loop pacing and bus publication rate of :class:`SimulatedDevice`.
LOOP_SLEEP_S = 0.001
PUBLISH_INTERVAL_S = 0.1
#: Simulated milliseconds advanced per device-lock hold while catching up.
CATCHUP_CHUNK_MS = 250
#: Host-bound bytes kept per in-process transport when nobody reads.
TRANSPORT_BUFFER_BYTES = 64 * 1024

_OFF, _SEEK, _RETURN = "off", "seek", "return"


# =========================================================================== config


@dataclass
class SimConfig:
    """Physical and firmware parameters of the simulated device.

    The defaults are the conformance harness (``conformance.json`` -> ``harness``: 6 slots,
    3200 steps/rev, 1600 steps before home, drop sensor, 20 pills per container). The app's
    simulator is built with :meth:`from_settings` (``num_slots = settings.num_slots``, 3 on
    the v2 device).
    """

    num_slots: int = 6
    steps_per_rev: int = 3200
    #: Physical position at power-on: this many steps *before* the home sensor edge.
    initial_offset_steps: int = 1600
    max_speed_sps: float = 1600
    accel_sps2: float = 3200
    homing_speed_sps: float = 400
    settle_ms: int = 300
    gate_travel_ms: int = 400
    home_timeout_ms: int = 20000
    gate_max_open_ms: int = 120000
    debounce_ms: int = 30
    sensor_zone_steps: int = 40
    #: None = ``sim-1.1.0`` (``sim-1.0.0`` when ``proto`` does not support DROP_SLOT).
    fw_version: str | None = None
    home_sensor: str = "ok"   # ok | dead | none
    #: v1.1: how long the release stays open inside ``DROP_SLOT`` (between the two servo moves).
    drop_open_ms: int = 600
    #: v1.1: IR break-beam in the chute; without it ``OK DROPPED`` only means "cycle completed".
    drop_sensor: bool = True
    #: v1.1: physical pills in every container at power-on.
    initial_pills: int = 20
    #: Protocol version reported in ``STATUS``. None = behave as v1 firmware: ``DROP_SLOT`` is
    #: ``ERR UNKNOWN_COMMAND`` and ``STATUS`` has no ``proto``/``drop_sensor`` keys.
    proto: str | None = SIM_PROTO
    #: Optional BUZZER extension implemented (False = firmware without it: ERR UNKNOWN_COMMAND).
    buzzer: bool = True
    #: A buzzer is fitted (False = the extension answers ERR NO_BUZZER, like BUZZER_PIN -1).
    buzzer_fitted: bool = True
    #: Hard limit of one BUZZER ON (firmware BUZZER_MAX_ON_MS).
    buzzer_max_on_ms: int = SIM_BUZZER_MAX_ON_MS

    def __post_init__(self) -> None:
        if not (MIN_SLOTS <= self.num_slots <= MAX_SLOTS):
            raise ValueError(f"num_slots must be in [{MIN_SLOTS}, {MAX_SLOTS}]")
        if self.steps_per_rev < self.num_slots * 2:
            raise ValueError("steps_per_rev is too small for the number of slots")
        if min(self.max_speed_sps, self.accel_sps2, self.homing_speed_sps) <= 0:
            raise ValueError("speeds and acceleration must be > 0")
        if not (0 < self.sensor_zone_steps < self.steps_per_rev):
            raise ValueError("sensor_zone_steps must be in (0, steps_per_rev)")
        if min(self.settle_ms, self.gate_travel_ms, self.debounce_ms, self.drop_open_ms) < 0:
            raise ValueError("durations must be >= 0")
        if self.home_timeout_ms <= 0 or self.gate_max_open_ms <= 0:
            raise ValueError("timeouts must be > 0")
        if not 1 <= int(self.buzzer_max_on_ms) <= 65535:
            raise ValueError("buzzer_max_on_ms must be in 1..65535")
        pills = self.initial_pills
        if isinstance(pills, bool) or not isinstance(pills, int) or pills < 0:
            raise ValueError("initial_pills must be an int >= 0")
        if self.proto is not None and not _single_token(self.proto):
            raise ValueError("proto must be None or a single non-empty token such as '1.1'")
        if self.fw_version is None:
            self.fw_version = SIM_FW_V11 if supports_drop_slot(self.proto) else SIM_FW_V1
        if not _single_token(self.fw_version):
            raise ValueError("fw_version must be a single non-empty token")
        self.drop_sensor = bool(self.drop_sensor)
        self.home_sensor = _sensor_mode(self.home_sensor)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SimConfig":
        """Reference build with the configured number of containers (``settings.num_slots``)."""
        return cls(num_slots=settings.num_slots)

    @property
    def drop_slot_supported(self) -> bool:
        """True when the simulated firmware implements ``DROP_SLOT`` (``proto >= 1.1``)."""
        return supports_drop_slot(self.proto)


def _single_token(value: object) -> bool:
    text = str(value)
    return bool(text) and not any(ch.isspace() for ch in text)


def _sensor_mode(mode: str) -> str:
    m = str(mode).strip().lower()
    if m not in SENSOR_MODES:
        raise ValueError(f"home-sensor mode must be one of {SENSOR_MODES}, got {mode!r}")
    return m


def _button_name(name: str) -> str:
    n = str(name).strip().upper()
    if n.endswith("_BUTTON"):
        n = n[: -len("_BUTTON")]
    if n not in BUTTONS:
        raise ValueError(f"button must be one of {BUTTONS}, got {name!r}")
    return n


def _fault_name(name: str) -> str:
    n = str(name).strip().lower()
    if n not in FAULT_NAMES:
        raise ValueError(f"fault must be one of {FAULT_NAMES}, got {name!r}")
    return n


# =========================================================================== firmware internals


@dataclass(frozen=True)
class _Profile:
    """Trapezoidal (or triangular) velocity profile over ``distance`` steps."""

    distance: int
    accel: float
    v_peak: float
    t_acc: float
    t_flat: float

    @classmethod
    def plan(cls, distance: int, vmax: float, accel: float) -> "_Profile":
        if distance <= 0:
            return cls(0, accel, 0.0, 0.0, 0.0)
        ramp = vmax * vmax / accel        # distance spent accelerating + decelerating
        if distance >= ramp:
            t_acc = vmax / accel
            return cls(distance, accel, vmax, t_acc, (distance - ramp) / vmax)
        t_acc = math.sqrt(distance / accel)
        return cls(distance, accel, accel * t_acc, t_acc, 0.0)

    @property
    def total_s(self) -> float:
        return 2.0 * self.t_acc + self.t_flat

    def steps_at(self, t: float) -> int:
        """Whole steps completed ``t`` seconds after the start (monotonic, ends at ``distance``)."""
        if t <= 0.0:
            return 0
        if t >= self.total_s:
            return self.distance
        a = self.accel
        if t < self.t_acc:
            s = 0.5 * a * t * t
        elif t < self.t_acc + self.t_flat:
            s = 0.5 * a * self.t_acc * self.t_acc + self.v_peak * (t - self.t_acc)
        else:
            rest = self.total_s - t
            s = self.distance - 0.5 * a * rest * rest
        return min(self.distance, max(0, int(s)))


#: Move kind per slot command; "home" is the sensorless dead-reckoning HOME.
_MOVE_KINDS = {
    CommandName.MOVE_SLOT: "move",
    CommandName.DISPENSE_SLOT: "dispense",
    CommandName.DROP_SLOT: "drop",
}


@dataclass
class _Move:
    kind: str                  # "move" | "dispense" | "drop" | "home"
    slot: int | None
    direction: int             # +1 / -1
    profile: _Profile
    started_ms: int
    timeout_ms: int
    issued: int = 0            # steps commanded so far
    lost: int = 0              # steps that did not move the carousel (jam)


@dataclass
class _Homing:
    phase: str                 # off (leave an active sensor, -) | seek (+) | return (to the edge, -)
    started_ms: int
    phase_ms: int
    phase_steps: int = 0
    travel: int = 0            # commanded steps since homing started
    edge_pos: int | None = None
    edge_ms: int = 0
    clear_since: int | None = None


@dataclass
class _GateTravel:
    """A blocking servo operation: travel to ``target`` (or hold, when ``target == from_pos``)."""

    target: float
    start_ms: int
    end_ms: int
    from_pos: float
    on_done: Callable[[], None]


@dataclass
class _Button:
    stable: bool = False
    since: int | None = None   # time when the raw level started to differ from ``stable``


# =========================================================================== VirtualESP32


class VirtualESP32:
    """Deterministic twin of the TactiDose firmware + physical carousel (see module docs).

    Typical use::

        esp = VirtualESP32()
        esp.boot()                     # EVENT BOOT, auto-home
        esp.tick(6000)
        esp.feed_line("DROP_SLOT 3")
        esp.tick(5000)
        esp.drain_output()             # [..., 'OK MOVING 3', 'OK AT_SLOT 3', 'OK GATE_OPEN',
                                       #  'OK GATE_CLOSED', 'OK DROPPED 3', 'OK READY']
    """

    _state: DeviceState
    _pos: int
    _homed: bool
    _slot: int | None
    _gate_open: bool
    _move: _Move | None
    _homing: _Homing | None
    _settle_until: int | None
    _settle_kind: str | None
    _travel: _GateTravel | None
    _gate_opened_at: int | None
    _releasing: int | None
    _saw_pill: bool
    _buttons: dict[str, _Button]

    def __init__(self, config: SimConfig | None = None) -> None:
        self.config = config if config is not None else SimConfig()
        c = self.config
        self._rev = c.steps_per_rev
        n = c.num_slots
        # round half up, like the firmware's integer math: floor(k * rev / n + 0.5)
        self._targets = tuple((2 * k * c.steps_per_rev + n) // (2 * n) for k in range(n))
        self._slot_tolerance = max(1, c.steps_per_rev // 720)
        self._max_homing_travel = int(HOMING_MAX_REVS * c.steps_per_rev)
        # ---- physical world (survives reboots)
        self._time = 0
        self._phys = -c.initial_offset_steps
        self._gate_pos = 0.0
        self._jam = False
        self._sensor_ok = c.home_sensor != "dead"
        self._has_sensor = c.home_sensor != "none"
        self._raw_buttons = dict.fromkeys(BUTTONS, False)
        self._pills = [c.initial_pills] * n
        self._pills_dropped = 0
        #: Fault hook: the MCU resets at the instant the gate would start to open.
        self.brownout_on_gate = False
        #: Fault hook: the MCU resets as a ``DROP_SLOT`` release starts to close - after
        #: ``OK GATE_OPEN`` (the pill has fallen) but before ``OK GATE_CLOSED`` / ``OK DROPPED``.
        self.brownout_on_release = False
        #: Buzzer fault hook: None | "missing" | "no_pin" | "unresponsive" (see module docs).
        self.buzzer_fault: str | None = None
        # ---- firmware
        self._booted = False
        self._boots = 0
        self._out: list[str] = []
        self._rx = bytearray()
        self._line = bytearray()
        self._reset_firmware()

    # ------------------------------------------------------------------ public API
    @property
    def time_ms(self) -> int:
        return self._time

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def booted(self) -> bool:
        return self._booted

    @property
    def slot_targets(self) -> tuple[int, ...]:
        """Absolute step target of every slot (relative to the home edge)."""
        return self._targets

    def boot(self, sensor: str | None = None) -> None:
        """(Re)start the firmware - power-on, reset or brown-out. The physical world is kept.

        ``sensor``: ``ok`` (sensor fitted and working), ``dead`` (fitted, never triggers),
        ``none`` (firmware built without a sensor), or ``None`` to keep the current setup.
        Boot closes the gate first, then sends ``EVENT BOOT <fw>`` and homes (§8.9).
        """
        if sensor is not None:
            mode = _sensor_mode(sensor)
            self._has_sensor = mode != "none"
            if mode != "none":
                self._sensor_ok = mode == "ok"
        self._reset_firmware()
        self._booted = True
        self._boots += 1
        if self._gate_pos > 0.0:
            self._start_travel(False, self._finish_boot)
        else:
            self._finish_boot()

    def feed_line(self, line: str) -> None:
        """Deliver one host line (a ``\\n`` terminator is appended)."""
        self.feed_bytes(line.encode("ascii", errors="replace") + b"\n")

    def feed_bytes(self, data: bytes) -> None:
        """Raw serial input. Lines end with ``\\n``, ``\\r\\n`` or ``\\r``; processed by ``loop()``."""
        if not self._booted or not data:
            return
        room = RX_BUFFER_BYTES - len(self._rx)
        if room > 0:
            self._rx += data[:room]

    def tick(self, ms: int = 1) -> None:
        """Advance simulated time by ``ms`` milliseconds, running ``loop()`` every 1 ms."""
        remaining = int(ms)
        while remaining > 0:
            idle = self._idle_ms(remaining)
            if idle:
                self._time += idle
                remaining -= idle
                continue
            self._time += 1
            remaining -= 1
            self._loop()

    def set_button(self, name: str, pressed: bool) -> None:
        """Set the physical level of ``CONFIRM`` / ``CANCEL`` (the firmware debounces it)."""
        self._raw_buttons[_button_name(name)] = bool(pressed)

    def set_sensor(self, mode: str) -> None:
        """Physical home sensor: ``ok`` (works) or ``dead`` (never triggers)."""
        m = _sensor_mode(mode)
        if m == "none":
            raise ValueError("set_sensor accepts 'ok' or 'dead'; use boot('none') for a sensorless build")
        self._sensor_ok = m == "ok"

    def set_jam(self, on: bool) -> None:
        """Jam the carousel: commanded steps no longer move it (moves never complete)."""
        self._jam = bool(on)

    @property
    def pills(self) -> tuple[int, ...]:
        """Physical pill count of every container (index = slot)."""
        return tuple(self._pills)

    def set_pills(self, slot: int, count: int) -> None:
        """Set the physical pill count of container ``slot`` (load / empty it; no lines, no time)."""
        n = self.config.num_slots
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < n:
            raise ValueError(f"slot must be an int in 0..{n - 1}, got {slot!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"pill count must be an int >= 0, got {count!r}")
        self._pills[slot] = count

    def drain_output(self) -> list[str]:
        """Device lines emitted since the previous call (without line terminators)."""
        out, self._out = self._out, []
        return out

    def physical(self) -> dict[str, Any]:
        """Snapshot of the physical world and the firmware's beliefs (for the demo UI/tests)."""
        p = self._phys % self._rev
        sensor = "none" if not self._has_sensor else ("ok" if self._sensor_ok else "dead")
        return {
            "angle_deg": round(p * 360.0 / self._rev, 2),
            "physical_steps": p,
            "slot": self._physical_slot(p),
            "target_slot": self._move.slot if self._move is not None else None,
            "gate_open": self._gate_pos > 0.0,
            "gate_pos": round(self._gate_pos, 3),
            "state": self._state.value,
            "homed": self._homed,
            "firmware_position": self._pos,
            "jammed": self._jam,
            "sensor_mode": sensor,
            "sensor_active": self._sensor_active(),
            "moving": self._move is not None or self._homing is not None,
            "releasing": self._releasing is not None,
            "booted": self._booted,
            "boots": self._boots,
            "buttons": {name: self._raw_buttons[name] for name in BUTTONS},
            "num_slots": self.config.num_slots,
            "fw_version": self.config.fw_version,
            "proto": self.config.proto,
            "drop_sensor": self.config.drop_sensor,
            "pills": list(self._pills),
            "pills_dropped": self._pills_dropped,
            "buzzer_on": self._buzz_until is not None,
            "time_ms": self._time,
        }

    # ------------------------------------------------------------------ firmware: lifecycle
    def _reset_firmware(self) -> None:
        self._state = DeviceState.BOOT
        self._pos = 0
        self._homed = False
        self._slot = None
        self._gate_open = False
        self._move = None
        self._homing = None
        self._settle_until = None
        self._settle_kind = None
        self._travel = None
        self._gate_opened_at = None
        self._releasing = None
        self._saw_pill = False
        self._buzz_until: int | None = None     # a reset silences the buzzer
        # A button held through a reset is not reported as a new press.
        self._buttons = {name: _Button(stable=self._raw_buttons[name]) for name in BUTTONS}
        self._rx.clear()
        self._line.clear()

    def _finish_boot(self) -> None:
        self._gate_open = False
        self._emit(format_message(MessageKind.EVENT, Ev.BOOT, self.config.fw_version))
        if self._has_sensor:
            self._start_homing()
        else:
            # No home sensor (MVP fallback): the carousel is assumed aligned by hand.
            self._ok(Ok.HOMING)
            self._become_homed()

    # ------------------------------------------------------------------ firmware: main loop
    def _loop(self) -> None:
        if not self._booted:
            return
        if self._buzz_until is not None and self._time >= self._buzz_until:
            self._buzz_until = None             # hard max: before anything that can block
        if self._travel is not None:
            if not self._advance_travel():
                return                      # blocked in the servo move (§8.2)
            if self._travel is not None:
                return
        self._poll_buttons()
        if self._travel is not None:
            return
        self._process_input()
        if self._travel is not None:
            return
        if self._move is not None:
            self._run_move()
        elif self._homing is not None:
            self._run_homing()
        elif self._settle_until is not None:
            if self._time >= self._settle_until:
                kind = self._settle_kind
                self._settle_until = self._settle_kind = None
                if kind == "drop":
                    self._start_release()
                else:
                    self._open_gate()
        elif self._state is DeviceState.GATE_OPEN and self._gate_opened_at is not None:
            if self._time - self._gate_opened_at >= self.config.gate_max_open_ms:
                self._close_gate_then(self._enter_ready)   # §8.7 safety net

    def _idle_ms(self, limit: int) -> int:
        """How many upcoming milliseconds provably do nothing (skipped by :meth:`tick`)."""
        if not self._booted:
            return limit
        if self._buzz_until is not None:
            limit = max(0, min(limit, self._buzz_until - self._time - 1))
            if limit == 0:
                return 0
        if (
            self._travel is not None
            or self._move is not None
            or self._homing is not None
            or self._settle_until is not None
            or self._rx
        ):
            return 0
        for name in BUTTONS:
            button = self._buttons[name]
            # A running (or stale, after a bounce) debounce timer needs loop() to update it.
            if self._raw_buttons[name] != button.stable or button.since is not None:
                return 0
        if self._state is DeviceState.GATE_OPEN and self._gate_opened_at is not None:
            deadline = self._gate_opened_at + self.config.gate_max_open_ms
            return max(0, min(limit, deadline - self._time - 1))
        return limit

    # ------------------------------------------------------------------ firmware: serial input
    def _process_input(self) -> None:
        while self._rx and self._travel is None:
            line = self._take_line()
            if line is None:
                return
            self._handle_line(line)

    def _take_line(self) -> str | None:
        rx = self._rx
        for i, b in enumerate(rx):
            if b in (10, 13):
                text = self._line.decode("ascii", errors="replace")
                self._line.clear()
                del rx[: i + 1]
                return text
            # Keep one character beyond the limit so parse_command sees the line as over-long.
            if len(self._line) <= MAX_LINE_LENGTH:
                self._line.append(b)
        rx.clear()
        return None

    def _handle_line(self, text: str) -> None:
        parsed = parse_command(text, self.config.num_slots)
        if parsed.empty:
            return
        if parsed.name is CommandName.DROP_SLOT and not self.config.drop_slot_supported:
            self._err(Err.UNKNOWN_COMMAND)      # v1 firmware does not know the word at all
            return
        if parsed.name is CommandName.BUZZER:
            if not self._buzzer_known():
                self._err(Err.UNKNOWN_COMMAND)  # firmware without the extension
                return
            if self.buzzer_fault == "unresponsive":
                return                          # fault: the reply never comes
        if parsed.command is None:
            self._err(parsed.error or Err.UNKNOWN_COMMAND)
            return
        cmd = parsed.command
        name = cmd.name
        if name is CommandName.PING:
            self._ok(Ok.PONG)
        elif name is CommandName.STATUS:
            self._emit(self._status_line())
        elif name is CommandName.HOME:
            self._cmd_home()
        elif name in _MOVE_KINDS:
            assert cmd.slot is not None
            self._cmd_move(cmd.slot, _MOVE_KINDS[name])
        elif name is CommandName.OPEN_GATE:
            self._cmd_open_gate()
        elif name is CommandName.CLOSE_GATE:
            self._cmd_close_gate()
        elif name is CommandName.STOP:
            self._cmd_stop()
        elif name is CommandName.BUZZER:
            self._cmd_buzzer(cmd.args)

    def _buzzer_known(self) -> bool:
        c = self.config
        return c.buzzer and c.drop_slot_supported and self.buzzer_fault != "missing"

    def _cmd_buzzer(self, args: tuple[str, ...]) -> None:
        """``BUZZER ON <ms>`` / ``BUZZER OFF`` / ``BUZZER``: accepted in every state, never blocks."""
        if not self.config.buzzer_fitted or self.buzzer_fault == "no_pin":
            self._err(Err.NO_BUZZER)
            return
        if not args:
            if self._buzz_until is None:
                self._ok(Ok.BUZZER, "OFF")
            else:
                self._ok(Ok.BUZZER, "ON", max(1, self._buzz_until - self._time))
        elif args[0] == "OFF":
            self._buzz_until = None
            self._ok(Ok.BUZZER, "OFF")
        else:
            ms = min(int(args[1]), int(self.config.buzzer_max_on_ms))
            self._buzz_until = self._time + ms
            self._ok(Ok.BUZZER, "ON", ms)

    def _status_line(self) -> str:
        c = self.config
        v11 = c.drop_slot_supported
        return StatusReport(
            state=self._state,
            homed=self._homed,
            slot=self._slot,
            gate=GateState.OPEN if self._gate_open else GateState.CLOSED,
            num_slots=c.num_slots,
            fw=c.fw_version,
            proto=c.proto,
            drop_sensor=c.drop_sensor if v11 else None,
        ).to_line()

    # ------------------------------------------------------------------ firmware: commands (§7)
    def _cmd_home(self) -> None:
        s = self._state
        if s in BUSY_STATES:
            self._err(Err.BUSY)
        elif s is DeviceState.GATE_OPEN:
            self._err(Err.INVALID_STATE)
        else:
            self._start_homing()

    def _cmd_move(self, slot: int, kind: str) -> None:
        """``MOVE_SLOT`` / ``DISPENSE_SLOT`` / ``DROP_SLOT`` share one acceptance row (§7, §12.2)."""
        s = self._state
        if s in BUSY_STATES:
            self._err(Err.BUSY)
        elif s is DeviceState.GATE_OPEN:
            self._err(Err.INVALID_STATE)
        elif s is not DeviceState.READY:
            self._err(Err.NOT_HOMED)
        else:
            # The gate is closed in READY; the firmware re-asserts it without travel (§8.1).
            delta = self._shortest(self._targets[slot] - self._pos)
            self._ok(Ok.MOVING, slot)
            self._state = DeviceState.MOVING
            self._slot = None
            self._begin_motion(kind, slot, delta)

    def _cmd_open_gate(self) -> None:
        s = self._state
        if s in BUSY_STATES:
            self._err(Err.BUSY)
        elif s is DeviceState.GATE_OPEN:
            self._ok(Ok.GATE_OPEN)          # idempotent; does not extend the auto-close timer
        elif s is not DeviceState.READY:
            self._err(Err.NOT_HOMED)
        else:
            self._open_gate()

    def _cmd_close_gate(self) -> None:
        s = self._state
        if s in BUSY_STATES:
            self._err(Err.BUSY)
        elif s is DeviceState.GATE_OPEN:
            self._close_gate_then(self._enter_ready)
        elif self._gate_pos > 0.0:
            self._close_gate_then(lambda: None)
        else:
            self._ok(Ok.GATE_CLOSED)        # re-assert closed, no state change

    def _cmd_stop(self) -> None:
        self._buzz_until = None                 # STOP silences the buzzer (no extra line)
        s = self._state
        if s in BUSY_STATES:
            self._interrupt()
        elif s is DeviceState.GATE_OPEN:
            self._close_gate_then(self._enter_safe_stop)
        else:
            self._enter_safe_stop()

    # ------------------------------------------------------------------ firmware: buttons (§8.8)
    def _poll_buttons(self) -> None:
        for name in BUTTONS:
            button = self._buttons[name]
            raw = self._raw_buttons[name]
            if raw == button.stable:
                button.since = None
                continue
            if button.since is None:
                button.since = self._time
            if self._time - button.since >= self.config.debounce_ms:
                button.stable = raw
                button.since = None
                if raw:
                    self._on_press(name)
                    if self._travel is not None:
                        return

    def _on_press(self, name: str) -> None:
        if name == "CONFIRM":
            self._emit(format_message(MessageKind.EVENT, Ev.CONFIRM_BUTTON))
            return
        self._emit(format_message(MessageKind.EVENT, Ev.CANCEL_BUTTON))
        if self._state in BUSY_STATES:
            self._interrupt()
        elif self._state is DeviceState.GATE_OPEN:
            self._close_gate_then(self._enter_ready)

    # ------------------------------------------------------------------ firmware: state changes
    def _interrupt(self) -> None:
        self._abort_motion()
        self._err(Err.STOPPED)
        self._enter_safe_stop()

    def _abort_motion(self) -> None:
        self._move = None
        self._homing = None
        self._settle_until = None
        self._settle_kind = None

    def _enter_safe_stop(self) -> None:
        self._abort_motion()
        self._state = DeviceState.SAFE_STOP
        self._homed = False
        self._slot = None
        self._ok(Ok.STOPPED)

    def _enter_ready(self) -> None:
        self._state = DeviceState.READY
        self._ok(Ok.READY)

    def _become_homed(self) -> None:
        self._pos = 0
        self._homed = True
        self._slot = 0
        self._ok(Ok.HOMED)
        self._enter_ready()

    def _fault(self, code: Err) -> None:
        self._abort_motion()
        self._err(code)
        self._state = DeviceState.FAULT
        self._homed = False
        self._slot = None

    # ------------------------------------------------------------------ firmware: gate (§8.2)
    def _start_travel(self, opening: bool, on_done: Callable[[], None]) -> None:
        self._start_servo(1.0 if opening else 0.0, self.config.gate_travel_ms, on_done)

    def _start_hold(self, ms: int, on_done: Callable[[], None]) -> None:
        """Block like a servo move but keep the gate where it is (the DROP_SLOT release hold)."""
        self._start_servo(self._gate_pos, ms, on_done)

    def _start_servo(self, target: float, ms: int, on_done: Callable[[], None]) -> None:
        self._travel = _GateTravel(target, self._time, self._time + ms, self._gate_pos, on_done)
        if ms <= 0:
            self._advance_travel()

    def _advance_travel(self) -> bool:
        tr = self._travel
        assert tr is not None
        if self._time >= tr.end_ms:
            self._travel = None
            self._gate_pos = tr.target
            tr.on_done()
            return True
        frac = (self._time - tr.start_ms) / max(1, tr.end_ms - tr.start_ms)
        self._gate_pos = tr.from_pos + (tr.target - tr.from_pos) * frac
        return False

    def _open_gate(self) -> None:
        if self.brownout_on_gate:
            # The servo inrush browns out the supply: the MCU resets before the gate moves.
            log.debug("virtual ESP32: brown-out at gate opening -> reboot")
            self.boot()
            return
        self._start_travel(True, self._gate_now_open)

    def _gate_now_open(self) -> None:
        self._gate_open = True
        self._state = DeviceState.GATE_OPEN
        self._gate_opened_at = self._time
        self._ok(Ok.GATE_OPEN)
        self._drop_pill()

    # ------------------------------------------------------------------ firmware: DROP_SLOT release (§12)
    # The release is one atomic, blocking sequence (open, hold, close): serial input and buttons
    # are only processed after OK DROPPED / ERR NO_PILL and OK READY (§12.3). The firmware state
    # stays AT_TARGET (busy) throughout; only the gate position changes.
    def _start_release(self) -> None:
        if self.brownout_on_gate:
            log.debug("virtual ESP32: brown-out at release opening -> reboot")
            self.boot()
            return
        self._releasing = self._slot
        self._saw_pill = False
        self._start_travel(True, self._release_opened)

    def _release_opened(self) -> None:
        self._gate_open = True
        self._ok(Ok.GATE_OPEN)
        self._drop_pill()
        self._start_hold(self.config.drop_open_ms, self._release_closing)

    def _release_closing(self) -> None:
        if self.brownout_on_release:
            # Servo inrush on the closing move: reset with the release still open. Boot closes it
            # first, then sends EVENT BOOT - the host never sees OK GATE_CLOSED / OK DROPPED.
            log.debug("virtual ESP32: brown-out as the release closes -> reboot")
            self.boot()
            return
        self._start_travel(False, self._release_closed)

    def _release_closed(self) -> None:
        slot = self._releasing
        self._releasing = None
        self._gate_open = False
        self._ok(Ok.GATE_CLOSED)
        if self.config.drop_sensor and not self._saw_pill:
            self._err(Err.NO_PILL)
        else:
            self._ok(Ok.DROPPED, slot)
        self._enter_ready()

    def _drop_pill(self) -> None:
        """The release just finished opening: one pill falls from the container over the chute."""
        k = self._physical_slot(self._phys % self._rev)
        if k is None or self._pills[k] <= 0:
            return
        self._pills[k] -= 1
        self._pills_dropped += 1
        self._saw_pill = True

    def _close_gate_then(self, after: Callable[[], None]) -> None:
        def done() -> None:
            self._gate_open = False
            self._gate_opened_at = None
            self._ok(Ok.GATE_CLOSED)
            after()

        self._start_travel(False, done)

    # ------------------------------------------------------------------ firmware: motion (§8.3, §8.6)
    def _shortest(self, delta: int) -> int:
        d = delta % self._rev
        if 2 * d > self._rev:
            d -= self._rev
        return d

    def _begin_motion(self, kind: str, slot: int | None, delta: int) -> None:
        c = self.config
        profile = _Profile.plan(abs(delta), c.max_speed_sps, c.accel_sps2)
        expected_ms = math.ceil(profile.total_s * 1000.0)
        self._move = _Move(
            kind=kind,
            slot=slot,
            direction=1 if delta >= 0 else -1,
            profile=profile,
            started_ms=self._time,
            timeout_ms=MOTION_TIMEOUT_FACTOR * expected_ms + MOTION_TIMEOUT_EXTRA_MS,
        )

    def _run_move(self) -> None:
        m = self._move
        assert m is not None
        elapsed = self._time - m.started_ms
        if elapsed > m.timeout_ms:
            self._fault(Err.MOTOR_FAULT)
            return
        want = m.profile.steps_at(elapsed / 1000.0)
        n = want - m.issued
        if n > 0:
            m.issued = want
            self._pos += m.direction * n
            if self._jam:
                m.lost += n
            else:
                self._phys += m.direction * n
        if m.issued >= m.profile.distance and m.lost == 0:
            self._move_done(m)

    def _move_done(self, m: _Move) -> None:
        self._move = None
        self._pos %= self._rev
        if m.kind == "home":
            self._become_homed()
            return
        self._slot = m.slot
        self._ok(Ok.AT_SLOT, m.slot)
        if m.kind == "move":
            self._enter_ready()
        else:
            self._state = DeviceState.AT_TARGET
            self._settle_until = self._time + self.config.settle_ms   # §8.4
            self._settle_kind = m.kind

    # ------------------------------------------------------------------ firmware: homing (§8.5)
    def _start_homing(self) -> None:
        self._abort_motion()
        self._ok(Ok.HOMING)
        self._state = DeviceState.HOMING
        self._homed = False
        self._slot = None
        if not self._has_sensor:
            # Sensorless build: HOME = return to the step counter's zero by dead reckoning.
            self._begin_motion("home", None, self._shortest(-self._pos))
            return
        phase = _OFF if self._sensor_active() else _SEEK
        self._homing = _Homing(phase=phase, started_ms=self._time, phase_ms=self._time)

    def _run_homing(self) -> None:
        h = self._homing
        assert h is not None
        c = self.config
        if self._time - h.started_ms > c.home_timeout_ms or h.travel > self._max_homing_travel:
            self._homing = None
            self._fault(Err.HOME_TIMEOUT)
            return
        due = int((self._time - h.phase_ms) * c.homing_speed_sps / 1000.0) - h.phase_steps
        if h.phase == _OFF:
            for _ in range(due):
                self._homing_step(h, -1)
            if self._sensor_active():
                h.clear_since = None
            elif h.clear_since is None:
                h.clear_since = self._time
            elif self._time - h.clear_since >= SENSOR_DEBOUNCE_MS:
                self._homing_phase(h, _SEEK)
        elif h.phase == _SEEK:
            for _ in range(due):
                self._homing_step(h, +1)
                if not self._sensor_active():
                    h.edge_pos = None
                elif h.edge_pos is None:
                    h.edge_pos, h.edge_ms = self._pos, self._time
            if h.edge_pos is not None:
                if not self._sensor_active():
                    h.edge_pos = None
                elif self._time - h.edge_ms >= SENSOR_DEBOUNCE_MS:
                    self._homing_phase(h, _RETURN)
        else:
            assert h.edge_pos is not None
            back = self._pos - h.edge_pos
            for _ in range(min(due, back)):
                self._homing_step(h, -1)
            if self._pos == h.edge_pos:
                self._homing = None
                self._become_homed()

    def _homing_step(self, h: _Homing, direction: int) -> None:
        self._pos += direction
        h.travel += 1
        h.phase_steps += 1
        if not self._jam:
            self._phys += direction

    def _homing_phase(self, h: _Homing, phase: str) -> None:
        h.phase = phase
        h.phase_ms = self._time
        h.phase_steps = 0
        h.clear_since = None

    # ------------------------------------------------------------------ physics helpers
    def _sensor_active(self) -> bool:
        return (
            self._has_sensor
            and self._sensor_ok
            and (self._phys % self._rev) < self.config.sensor_zone_steps
        )

    def _physical_slot(self, p: int) -> int | None:
        for k, target in enumerate(self._targets):
            d = abs(p - target)
            if min(d, self._rev - d) <= self._slot_tolerance:
                return k
        return None

    # ------------------------------------------------------------------ output
    def _emit(self, line: str) -> None:
        self._out.append(line)

    def _ok(self, code: Ok, *args: object) -> None:
        self._emit(format_message(MessageKind.OK, code, *args))

    def _err(self, code: Err) -> None:
        self._emit(format_message(MessageKind.ERR, code))


# =========================================================================== conformance target


class ConformanceSimTarget:
    """``ConformanceTarget`` over a fresh :class:`VirtualESP32` (simulated time).

    The default :class:`SimConfig` is the conformance harness (6 slots, drop sensor, 20 pills).
    """

    name = "sim"
    supports_faults = True
    supports_buttons = True
    supports_boot = True

    def __init__(self, config: SimConfig | None = None) -> None:
        self._config = config
        self.esp = VirtualESP32(config)

    def reset(self) -> None:
        self.esp = VirtualESP32(self._config)

    def boot(self, mode: str) -> None:
        self.esp.boot(mode)

    def send(self, line: str) -> None:
        self.esp.feed_line(line)

    def tick(self, ms: int) -> list[str]:
        self.esp.tick(ms)
        return self.esp.drain_output()

    def set_button(self, name: str, pressed: bool) -> None:
        self.esp.set_button(name, pressed)

    def set_sensor(self, mode: str) -> None:
        self.esp.set_sensor(mode)

    def set_jam(self, on: bool) -> None:
        self.esp.set_jam(on)

    def set_pills(self, slot: int, count: int) -> None:
        self.esp.set_pills(slot, count)

    def close(self) -> None:
        return None


# =========================================================================== real-time device


class _SimTransport:
    """In-process byte pipe to a :class:`SimulatedDevice` (implements ``Transport``)."""

    def __init__(self, device: "SimulatedDevice") -> None:
        self.name = SIM_PORT_NAME
        self._device = device
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._broken: str | None = None

    @property
    def is_open(self) -> bool:
        return not self._closed and self._broken is None

    def read(self, size: int = 1024) -> bytes:
        with self._cond:
            if not self._buf and self.is_open:
                self._cond.wait(DEFAULT_READ_TIMEOUT_S)
            self._check()
            if not self._buf:
                return b""
            data = bytes(self._buf[:size])
            del self._buf[:size]
            return data

    def write(self, data: bytes) -> int:
        with self._cond:
            self._check()
        return self._device._host_write(self, bytes(data))

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._device._detach(self)

    # -- device side -----------------------------------------------------------
    def _check(self) -> None:
        if self._closed:
            raise TransportError(f"{self.name} transport is closed")
        if self._broken is not None:
            raise TransportError(self._broken)

    def _deliver(self, data: bytes) -> None:
        with self._cond:
            if not self.is_open:
                return
            self._buf += data
            excess = len(self._buf) - TRANSPORT_BUFFER_BYTES
            if excess > 0:
                del self._buf[:excess]
            self._cond.notify_all()

    def _kill(self, reason: str) -> None:
        with self._cond:
            if self._broken is None:
                self._broken = reason
            self._cond.notify_all()


class SimulatedDevice:
    """A :class:`VirtualESP32` running in (``settings.sim_speed`` x) real time, with faults.

    Ownership: whoever calls :meth:`start` must call :meth:`close` (in ``sim`` hardware
    mode the ``HardwareClient`` created by ``create_hardware`` does both). Without an explicit
    ``config`` the device is :meth:`SimConfig.from_settings` (``settings.num_slots`` containers,
    protocol 1.1, drop sensor, 20 pills each). Faults (:data:`FAULT_NAMES`):

    * ``home_sensor_dead`` - the home sensor never triggers (next homing -> ``ERR HOME_TIMEOUT``);
    * ``motor_jam`` - the carousel stops following the motor (moves -> ``ERR MOTOR_FAULT``);
    * ``unresponsive`` - every byte in both directions is dropped (firmware keeps running);
    * ``brownout_on_gate`` - the MCU resets (``EVENT BOOT`` + re-home) the instant the gate /
      release would start to open; it stays closed and no pill drops;
    * ``brownout_on_release`` - the MCU resets as a ``DROP_SLOT`` release starts to close: the
      pill has dropped after ``OK GATE_OPEN`` but ``OK DROPPED`` never comes (host: UNCERTAIN);
    * ``disconnect`` - USB unplug: the open transport raises ``TransportError`` and new ones
      cannot be opened until the fault is cleared (the firmware keeps running meanwhile).
    """

    def __init__(
        self,
        settings: "Settings",
        bus: "EventBus | None" = None,
        config: SimConfig | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.config = config if config is not None else SimConfig.from_settings(settings)
        self.speed = float(settings.sim_speed)
        self._esp = VirtualESP32(self.config)
        self._lock = threading.RLock()
        self._faults = dict.fromkeys(FAULT_NAMES, False)
        self._transport: _SimTransport | None = None
        self._releases: list[tuple[int, str]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        #: More than this much simulated time behind real time (~0.5 s real) is dropped, not replayed.
        self._max_backlog_ms = max(1000, int(self.speed * 500))
        self._pub_lock = threading.Lock()
        self._last_pub_at = 0.0
        self._last_pub_key: dict[str, Any] | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def started(self) -> bool:
        return self._started

    @property
    def time_ms(self) -> int:
        """Simulated milliseconds since construction."""
        with self._lock:
            return self._esp.time_ms

    def start(self) -> None:
        """Power the device on (boot + auto-home) and start the ``sim-device`` thread. Idempotent."""
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            self._esp.boot()
        self._thread = threading.Thread(target=self._run, name="sim-device", daemon=True)
        self._thread.start()
        log.info("simulated ESP32 started (speed x%g, %d slots)", self.speed, self.config.num_slots)

    def close(self) -> None:
        """Stop the simulation thread and break any open transport. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            transport, self._transport = self._transport, None
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if transport is not None:
            transport._kill("simulated device closed")
        log.info("simulated ESP32 stopped")

    def __enter__(self) -> "SimulatedDevice":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ host link
    def open_transport(self) -> _SimTransport:
        """Open the in-process serial link (a newer link replaces - and breaks - the old one)."""
        with self._lock:
            if self._closed:
                raise TransportError("simulated device is closed")
            if self._faults["disconnect"]:
                raise TransportError(f"{SIM_PORT_NAME} not available (simulated USB disconnect)")
            old = self._transport
            transport = _SimTransport(self)
            self._transport = transport
        if old is not None:
            old._kill("replaced by a newer connection")
        return transport

    def _host_write(self, transport: _SimTransport, data: bytes) -> int:
        with self._lock:
            if transport is not self._transport:
                raise TransportError(f"{SIM_PORT_NAME} link is gone")
            if not self._faults["unresponsive"]:
                self._esp.feed_bytes(data)
        return len(data)

    def _detach(self, transport: _SimTransport) -> None:
        with self._lock:
            if self._transport is transport:
                self._transport = None

    # ------------------------------------------------------------------ demo / test controls
    def set_fault(self, name: str, enabled: bool) -> None:
        key = _fault_name(name)
        on = bool(enabled)
        unplugged: _SimTransport | None = None
        with self._lock:
            self._faults[key] = on
            if key == "home_sensor_dead":
                self._esp.set_sensor("dead" if on else "ok")
            elif key == "motor_jam":
                self._esp.set_jam(on)
            elif key == "brownout_on_gate":
                self._esp.brownout_on_gate = on
            elif key == "brownout_on_release":
                self._esp.brownout_on_release = on
            elif key == "disconnect" and on:
                unplugged, self._transport = self._transport, None
        if unplugged is not None:
            unplugged._kill("simulated USB disconnect")
        log.info("simulator fault %s = %s", key, on)

    def set_buzzer_fault(self, mode: str | None) -> None:
        """Buzzer fault injection: None (healthy) or one of :data:`BUZZER_FAULTS`."""
        if mode is not None and mode not in BUZZER_FAULTS:
            raise ValueError(f"buzzer fault must be None or one of {BUZZER_FAULTS}, got {mode!r}")
        with self._lock:
            self._esp.buzzer_fault = mode
        log.info("simulator buzzer fault = %s", mode)

    def faults(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._faults)

    def press(self, name: str, duration_ms: int = DEFAULT_PRESS_MS) -> None:
        """Press ``CONFIRM``/``CANCEL`` for ``duration_ms`` simulated milliseconds."""
        button = _button_name(name)
        with self._lock:
            self._esp.set_button(button, True)
            self._releases.append((self._esp.time_ms + max(1, int(duration_ms)), button))

    def reboot(self) -> None:
        """Reset the MCU (as if EN was pulsed): ``EVENT BOOT`` and re-home."""
        with self._lock:
            self._esp.boot()

    def set_pills(self, slot: int, count: int) -> None:
        """Demo panel: set the physical pill count of container ``slot`` (raises ValueError).

        This is the simulated *physical* truth; the database's ``pill_count`` is not changed.
        """
        with self._lock:
            self._esp.set_pills(slot, count)
        log.info("simulator container %s now holds %s pills", slot, count)

    def physical(self) -> dict[str, Any]:
        """``VirtualESP32.physical()`` - incl. ``pills`` (list per slot) and ``pills_dropped``."""
        with self._lock:
            return self._esp.physical()

    # ------------------------------------------------------------------ simulation thread
    def _run(self) -> None:
        anchor_real = time.monotonic()
        with self._lock:
            anchor_sim = self._esp.time_ms
        while not self._stop.is_set():
            now = time.monotonic()
            behind = 0
            try:
                with self._lock:
                    target = anchor_sim + int((now - anchor_real) * 1000.0 * self.speed)
                    behind = target - self._esp.time_ms
                    if behind > self._max_backlog_ms:
                        # Cannot keep up (process stalled or speed too high): drop the backlog.
                        log.debug("simulated ESP32 dropped %d ms of backlog", behind)
                        anchor_real, anchor_sim = now, self._esp.time_ms
                        behind = 0
                    if behind > 0:
                        # Bounded work per lock hold so host writes are never blocked for long.
                        self._advance(min(behind, CATCHUP_CHUNK_MS))
                    lines = self._esp.drain_output()
                    transport = self._transport
                    if self._faults["unresponsive"] or self._faults["disconnect"]:
                        transport = None
                if lines and transport is not None:
                    transport._deliver("".join(f"{line}\r\n" for line in lines).encode("ascii", "replace"))
                self._publish_physical(now)
            except Exception:  # noqa: BLE001 - keep the device alive, but make bugs visible
                log.exception("simulated ESP32 loop error")
            # Caught up: wait for the next millisecond. Behind: just yield so writers get the lock.
            time.sleep(LOOP_SLEEP_S if behind <= CATCHUP_CHUNK_MS else 0)

    def _advance(self, ms: int) -> None:
        esp = self._esp
        end = esp.time_ms + ms
        while esp.time_ms < end:
            nxt = min((t for t, _ in self._releases), default=None)
            if nxt is None or nxt > end:
                esp.tick(end - esp.time_ms)
                return
            if nxt > esp.time_ms:
                esp.tick(nxt - esp.time_ms)
            keep: list[tuple[int, str]] = []
            for at, button in self._releases:
                if at <= esp.time_ms:
                    esp.set_button(button, False)
                else:
                    keep.append((at, button))
            self._releases = keep

    def _publish_physical(self, now: float) -> None:
        if self.bus is None:
            return
        with self._pub_lock:
            if now - self._last_pub_at < PUBLISH_INTERVAL_S:
                return
            self._last_pub_at = now                # checked (and published) at most ~10 Hz
            with self._lock:
                payload = self._esp.physical()
                payload["faults"] = dict(self._faults)
            key = {k: v for k, v in payload.items() if k != "time_ms"}
            if key == self._last_pub_key:
                return
            self._last_pub_key = key
        self.bus.publish(Topic.SIM_PHYSICAL, payload)
