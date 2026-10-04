"""Shared test doubles. Import as ``from tests.fakes import FakeHardware, ...``.

``FakeHardware`` enforces the same interlocks as the real firmware (docs §7) so
domain logic is exercised against realistic refusals, and can be scripted to
return any outcome (``ERR …``, ``TIMEOUT``, ``DISCONNECTED`` …) or to block a
command until released (concurrency / cancel tests).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable

from tactidose.core.interfaces import (
    DeviceSnapshot,
    ExtractionResult,
    LabelExtraction,
)
from tactidose.hardware.protocol import (
    Command,
    CommandName,
    CommandResult,
    DeviceState,
    Err,
    Ev,
    GateState,
    HostCode,
    Message,
    MessageKind,
    Ok,
    ProtocolError,
    StatusReport,
    parse_command,
)

_OK_CODES = {c.value for c in Ok}
_ERR_CODES = {c.value for c in Err}
_HOST_CODES = {c.value for c in HostCode}


class FakeHardware:
    """In-memory HardwareController with firmware-like interlocks."""

    def __init__(
        self,
        num_slots: int = 6,
        *,
        connected: bool = True,
        state: DeviceState = DeviceState.READY,
        homed: bool = True,
        pills: int = 20,
        drop_sensor: bool = True,
        proto: str | None = "1.1",
    ) -> None:
        self.num_slots = num_slots
        self.sent: list[str] = []
        self.started = False
        self.closed = False
        self.reconnects = 0
        #: v1.1 physical pill counts per container (DROP_SLOT decrements; 0 + sensor -> ERR NO_PILL).
        self.pills: dict[int, int] = {s: pills for s in range(num_slots)}
        self.drop_sensor = drop_sensor
        self._snap = DeviceSnapshot(
            mode="fake",
            port="fake://",
            connected=connected,
            responsive=connected,
            state=state,
            homed=homed,
            slot=0 if homed else None,
            gate=GateState.CLOSED,
            fw_version="fake-1.1",
            num_slots_reported=num_slots,
            proto=proto,
            drop_sensor=drop_sensor,
        )
        self._scripts: dict[CommandName, deque[Any]] = defaultdict(deque)
        self._listeners: list[Callable[[Message], None]] = []
        self._cmd_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._holds: set[CommandName] = set()
        self._release = threading.Event()
        self._stopped_during_hold = threading.Event()
        self.holding = threading.Event()   # set while a held command is waiting

    # ------------------------------------------------------------------ test controls
    def script(self, name: CommandName, code: str | Ok | Err | HostCode, *, times: int = 1) -> None:
        """Next ``times`` calls of ``name`` return ``code`` instead of the default behaviour."""
        value = code.value if hasattr(code, "value") else str(code)
        for _ in range(times):
            self._scripts[name].append(value)

    def script_fn(self, name: CommandName, fn: Callable[[Command], CommandResult]) -> None:
        self._scripts[name].append(fn)

    def hold(self, name: CommandName) -> None:
        """Make the next ``name`` command block until :meth:`release` or :meth:`stop`."""
        self._holds.add(name)
        self._release.clear()
        self._stopped_during_hold.clear()

    def release(self) -> None:
        self._release.set()

    def set_state(self, **fields: Any) -> None:
        with self._state_lock:
            self._snap = replace(self._snap, **fields)

    def emit_event(self, code: Ev | str, *args: str) -> None:
        c = code.value if isinstance(code, Ev) else str(code)
        msg = Message(MessageKind.EVENT, c, tuple(args), raw=" ".join(("EVENT", c, *args)))
        for cb in list(self._listeners):
            cb(msg)

    def commands(self, name: CommandName | None = None) -> list[str]:
        if name is None:
            return list(self.sent)
        return [s for s in self.sent if s.split(" ")[0] == name.value]

    # ------------------------------------------------------------------ HardwareController
    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def snapshot(self) -> DeviceSnapshot:
        with self._state_lock:
            return self._snap

    def add_event_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def ping(self) -> CommandResult:
        return self._run(Command.ping())

    def status(self) -> CommandResult:
        return self._run(Command.status())

    def home(self) -> CommandResult:
        return self._run(Command.home())

    def move_slot(self, slot: int) -> CommandResult:
        return self._build_and_run(lambda: Command.move_slot(slot, self.num_slots), CommandName.MOVE_SLOT)

    def dispense_slot(self, slot: int) -> CommandResult:
        return self._build_and_run(lambda: Command.dispense_slot(slot, self.num_slots), CommandName.DISPENSE_SLOT)

    def open_gate(self) -> CommandResult:
        return self._run(Command.open_gate())

    def close_gate(self) -> CommandResult:
        return self._run(Command.close_gate())

    def drop_slot(self, slot: int) -> CommandResult:
        """v1.1 ``DROP_SLOT n`` (this fake always speaks v1.1, regardless of ``proto``)."""
        return self._build_and_run(lambda: Command.drop_slot(slot, self.num_slots), CommandName.DROP_SLOT)

    def set_pills(self, slot: int, count: int) -> None:
        self.pills[slot] = count

    def reconnect(self) -> bool:
        self.reconnects += 1
        return self.snapshot().mode != "none"

    def send_raw(self, line: str) -> CommandResult:
        parsed = parse_command(line, self.num_slots)
        if parsed.command is None:
            dummy = Command.ping()
            return CommandResult.host_failure(dummy, HostCode.INVALID_ARGUMENT, detail=line)
        return self._run(parsed.command)

    def stop(self) -> CommandResult:
        cmd = Command.stop()
        self.sent.append(cmd.to_line())
        if not self.snapshot().connected:
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED)
        msgs: list[Message] = []
        with self._state_lock:
            if self._snap.gate is GateState.OPEN:
                msgs.append(_msg("OK", "GATE_CLOSED"))
            self._snap = replace(
                self._snap, state=DeviceState.SAFE_STOP, homed=False, gate=GateState.CLOSED,
                target_slot=None,
            )
        if self.holding.is_set():
            self._stopped_during_hold.set()
            self._release.set()
        msgs.append(_msg("OK", "STOPPED"))
        return CommandResult(cmd, True, Ok.STOPPED.value, tuple(msgs))

    # ------------------------------------------------------------------ internals
    def _build_and_run(self, build: Callable[[], Command], name: CommandName) -> CommandResult:
        try:
            cmd = build()
        except ProtocolError as exc:
            return CommandResult.host_failure(Command.ping(), HostCode.INVALID_ARGUMENT, detail=f"{name.value}: {exc}")
        return self._run(cmd)

    def _run(self, cmd: Command) -> CommandResult:
        if not self.snapshot().connected:
            self.sent.append(cmd.to_line())
            return CommandResult.host_failure(cmd, HostCode.NOT_CONNECTED)
        if not self._cmd_lock.acquire(blocking=False):
            return CommandResult.host_failure(cmd, HostCode.BUSY_LOCAL)
        try:
            self.sent.append(cmd.to_line())
            with self._state_lock:
                self._snap = replace(self._snap, in_flight=cmd.to_line())
            if cmd.name in self._holds:
                self._holds.discard(cmd.name)
                if cmd.slot is not None and cmd.name in (
                    CommandName.MOVE_SLOT, CommandName.DISPENSE_SLOT, CommandName.DROP_SLOT,
                ):
                    self.set_state(state=DeviceState.MOVING, target_slot=cmd.slot, slot=None)
                self.holding.set()
                self._release.wait(timeout=30)
                self.holding.clear()
                if self._stopped_during_hold.is_set():
                    return CommandResult(cmd, False, Err.STOPPED.value, (_msg("ERR", "STOPPED"),))
                # restore pre-motion state so default behaviour applies
                if cmd.slot is not None:
                    self.set_state(state=DeviceState.READY, target_slot=None)
            scripted = self._scripts[cmd.name].popleft() if self._scripts[cmd.name] else None
            if callable(scripted):
                return scripted(cmd)
            if scripted is not None:
                return self._scripted_result(cmd, scripted)
            return self._default(cmd)
        finally:
            with self._state_lock:
                self._snap = replace(self._snap, in_flight=None)
            self._cmd_lock.release()

    def _scripted_result(self, cmd: Command, code: str) -> CommandResult:
        if code in _HOST_CODES:
            return CommandResult.host_failure(cmd, HostCode(code))
        if code in _ERR_CODES:
            if code in (Err.MOTOR_FAULT.value, Err.HOME_TIMEOUT.value):
                self.set_state(state=DeviceState.FAULT, homed=False, gate=GateState.CLOSED)
            return CommandResult(cmd, False, code, (_msg("ERR", code),))
        if code in _OK_CODES:
            return CommandResult(cmd, True, code, (_msg("OK", code),))
        raise ValueError(f"unknown scripted code {code!r}")

    def _default(self, cmd: Command) -> CommandResult:
        s = self.snapshot()
        busy = s.state in (DeviceState.HOMING, DeviceState.MOVING, DeviceState.AT_TARGET)
        unhomed = not s.homed or s.state in (DeviceState.BOOT, DeviceState.SAFE_STOP, DeviceState.FAULT)
        n = cmd.name

        def err(code: Err) -> CommandResult:
            return CommandResult(cmd, False, code.value, (_msg("ERR", code.value),))

        if n is CommandName.PING:
            return CommandResult(cmd, True, Ok.PONG.value, (_msg("OK", "PONG"),))
        if n is CommandName.STATUS:
            rep = StatusReport(state=s.state, homed=bool(s.homed), slot=s.slot, gate=s.gate,
                               num_slots=self.num_slots, fw=s.fw_version)
            line = rep.to_line()
            m = Message(MessageKind.OK, "STATUS", tuple(line.split(" ")[2:]), raw=line)
            return CommandResult(cmd, True, Ok.STATUS.value, (m,))
        if n is CommandName.HOME:
            if busy:
                return err(Err.BUSY)
            if s.gate is GateState.OPEN:
                return err(Err.INVALID_STATE)
            self.set_state(state=DeviceState.READY, homed=True, slot=0, gate=GateState.CLOSED)
            return CommandResult(cmd, True, Ok.HOMED.value, (_msg("OK", "HOMING"), _msg("OK", "HOMED")))
        if n in (CommandName.MOVE_SLOT, CommandName.DISPENSE_SLOT):
            if busy:
                return err(Err.BUSY)
            if s.gate is GateState.OPEN:
                return err(Err.INVALID_STATE)
            if unhomed:
                return err(Err.NOT_HOMED)
            slot = cmd.slot
            if n is CommandName.MOVE_SLOT:
                self.set_state(state=DeviceState.READY, slot=slot)
                return CommandResult(cmd, True, Ok.AT_SLOT.value,
                                     (_msg("OK", f"MOVING {slot}"), _msg("OK", f"AT_SLOT {slot}")))
            self.set_state(state=DeviceState.GATE_OPEN, slot=slot, gate=GateState.OPEN)
            return CommandResult(cmd, True, Ok.GATE_OPEN.value,
                                 (_msg("OK", f"MOVING {slot}"), _msg("OK", f"AT_SLOT {slot}"),
                                  _msg("OK", "GATE_OPEN")))
        if n is CommandName.OPEN_GATE:
            if busy:
                return err(Err.BUSY)
            if s.gate is GateState.OPEN:
                return CommandResult(cmd, True, Ok.GATE_OPEN.value, (_msg("OK", "GATE_OPEN"),))
            if unhomed:
                return err(Err.NOT_HOMED)
            self.set_state(state=DeviceState.GATE_OPEN, gate=GateState.OPEN)
            return CommandResult(cmd, True, Ok.GATE_OPEN.value, (_msg("OK", "GATE_OPEN"),))
        if n is CommandName.CLOSE_GATE:
            if busy:
                return err(Err.BUSY)
            was_open = s.gate is GateState.OPEN
            new_state = DeviceState.READY if s.state is DeviceState.GATE_OPEN else s.state
            self.set_state(gate=GateState.CLOSED, state=new_state)
            msgs = [_msg("OK", "GATE_CLOSED")] + ([_msg("OK", "READY")] if was_open else [])
            return CommandResult(cmd, True, Ok.GATE_CLOSED.value, tuple(msgs))
        if n is CommandName.DROP_SLOT:
            if busy:
                return err(Err.BUSY)
            if s.gate is GateState.OPEN:
                return err(Err.INVALID_STATE)
            if unhomed:
                return err(Err.NOT_HOMED)
            slot = int(cmd.slot)  # type: ignore[arg-type]
            self.set_state(state=DeviceState.READY, slot=slot, gate=GateState.CLOSED)
            release = (_msg("OK", f"MOVING {slot}"), _msg("OK", f"AT_SLOT {slot}"),
                       _msg("OK", "GATE_OPEN"), _msg("OK", "GATE_CLOSED"))
            if self.drop_sensor and self.pills.get(slot, 0) <= 0:
                return CommandResult(cmd, False, Err.NO_PILL.value, release + (_msg("ERR", "NO_PILL"),))
            self.pills[slot] = max(0, self.pills.get(slot, 0) - 1)
            return CommandResult(cmd, True, Ok.DROPPED.value, release + (_msg("OK", f"DROPPED {slot}"),))
        raise AssertionError(f"unhandled command {cmd}")


def _msg(kind: str, text: str) -> Message:
    parts = text.split(" ")
    return Message(MessageKind(kind), parts[0], tuple(parts[1:]), raw=f"{kind} {text}")


class FakeSpeaker:
    """Records utterances; never plays audio."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []
        self.meta: list[dict[str, Any]] = []
        self.interrupts = 0
        self.started = False
        self.closed = False
        self.speaking = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def say(self, text: str, *, kind: str = "info", interrupt: bool = False,
            meta: dict[str, Any] | None = None) -> None:
        if interrupt:
            self.interrupts += 1
        self.said.append((text, kind))
        self.meta.append(dict(meta or {}))

    def wait_idle(self, timeout: float | None = None) -> bool:
        return True

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.said]

    def last(self) -> str | None:
        return self.said[-1][0] if self.said else None


class FakeExtractor:
    """LabelExtractor returning a fixed result (default: a legible demo label)."""

    name = "fake"

    def __init__(self, result: ExtractionResult | None = None, *, delay_s: float = 0.0) -> None:
        self.calls: list[tuple[int, str]] = []
        self.delay_s = delay_s
        self.result = result or ExtractionResult(
            ok=True,
            model="fake",
            data=LabelExtraction(
                medication_name="Vitamin C (demo candy)",
                strength="1 piece",
                visible_instructions="Take one piece in the morning. DEMO ONLY - NOT MEDICATION.",
                warnings_visible=["Demo token - not a real medication"],
                confidence_notes="Synthetic test extraction",
                legible=True,
            ),
        )

    def extract(self, image: bytes, mime_type: str) -> ExtractionResult:
        self.calls.append((len(image), mime_type))
        if self.delay_s:
            time.sleep(self.delay_s)
        return self.result


# --------------------------------------------------------------------------- data seeding


def seed_minimal(db, settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Raw-ORM seed independent of service code: 1 user, 1 device, N empty compartments,
    two confirmed medications assigned to slots 2 and 4, daily schedules 08:00 and 20:00
    for the first and 13:00 for the second.

    Returns ids: {"user_id", "device_id", "med_ids": [..], "schedule_ids": [..]}.
    """
    from tactidose.db.models import Compartment, Device, Medication, Schedule, User, utcnow

    created = now or utcnow() - timedelta(days=2)
    with db.session() as s:
        user = User(display_name="Demo User", accessibility_preferences={"voice": True}, created_at=created)
        s.add(user)
        s.flush()
        dev = Device(device_id=settings.device_id, user_id=user.user_id, name="Test unit",
                     num_slots=settings.num_slots, created_at=created)
        s.add(dev)
        s.flush()
        comps = [Compartment(device_id=dev.device_id, slot_number=i, active=True) for i in range(settings.num_slots)]
        s.add_all(comps)
        m1 = Medication(user_id=user.user_id, name="Vitamin C (demo candy)", strength="1 piece",
                        instructions_text="Take one piece.", warnings=[], source="demo_seed",
                        confirmed_by_user=True, confirmed_by="test", confirmed_at=created, created_at=created)
        m2 = Medication(user_id=user.user_id, name="Calcium (demo token)", strength="1 token",
                        instructions_text="Take with water.", warnings=["Demo only"], source="demo_seed",
                        confirmed_by_user=True, confirmed_by="test", confirmed_at=created, created_at=created)
        s.add_all([m1, m2])
        s.flush()
        comps[2].medication_id = m1.medication_id
        comps[4].medication_id = m2.medication_id
        sc1 = Schedule(medication_id=m1.medication_id, time_of_day="08:00", created_at=created)
        sc2 = Schedule(medication_id=m1.medication_id, time_of_day="20:00", created_at=created)
        sc3 = Schedule(medication_id=m2.medication_id, time_of_day="13:00", created_at=created)
        s.add_all([sc1, sc2, sc3])
        s.flush()
        return {
            "user_id": user.user_id,
            "device_id": dev.device_id,
            "med_ids": [m1.medication_id, m2.medication_id],
            "schedule_ids": [sc1.schedule_id, sc2.schedule_id, sc3.schedule_id],
        }


class FakeDropHardware(FakeHardware):
    """v2 default device: 3 containers, 20 pills each, drop sensor, protocol 1.1."""

    def __init__(self, num_slots: int = 3, **kw: Any) -> None:
        super().__init__(num_slots, **kw)


def seed_v2(db, settings, *, now: datetime | None = None, pills: int = 20,
            cooldown_minutes: int = 60) -> dict[str, Any]:
    """Raw-ORM v2 seed (no service code): patient + family + doctor accounts (no passwords),
    care links, a device bound to the patient with ``settings.num_slots`` containers, three
    confirmed demo medications in slots 0, 1, 2 (``pills`` each) and daily schedules
    08:00 (med 0), 13:00 (med 1), 20:00 (med 2), created by the doctor.

    Returns ids: {patient_id, family_id, doctor_id, device_id, med_ids, compartment_ids,
    schedule_ids, link_code}.
    """
    from tactidose.db.models import (
        CareLink, Compartment, Device, Medication, Role, Schedule, User, utcnow,
    )

    created = now or utcnow() - timedelta(days=2)
    catalog = [
        ("Vitamin C (demo candy)", "1 piece", "Take one piece."),
        ("Calcium (demo token)", "1 token", "Take with water."),
        ("Omega-3 (demo candy)", "1 piece", "Take with dinner."),
    ]
    with db.session() as s:
        patient = User(display_name="Alex Rivera", role=Role.PATIENT.value, email="alex@test.tactidose",
                       link_code="ALEX2026", created_at=created)
        family = User(display_name="Sam Rivera", role=Role.FAMILY.value, email="sam@test.tactidose",
                      created_at=created)
        doctor = User(display_name="Dr. Lee", role=Role.DOCTOR.value, email="dr.lee@test.tactidose",
                      created_at=created)
        s.add_all([patient, family, doctor])
        s.flush()
        s.add_all([
            CareLink(caregiver_id=family.user_id, patient_id=patient.user_id, relationship_kind="family"),
            CareLink(caregiver_id=doctor.user_id, patient_id=patient.user_id, relationship_kind="doctor"),
        ])
        dev = Device(device_id=settings.device_id, user_id=patient.user_id, name="Test unit",
                     num_slots=settings.num_slots, manual_cooldown_minutes=cooldown_minutes,
                     created_at=created)
        s.add(dev)
        s.flush()
        comps = [Compartment(device_id=dev.device_id, slot_number=i, active=True, pill_count=pills,
                             capacity=30, low_stock_threshold=3) for i in range(settings.num_slots)]
        s.add_all(comps)
        meds = []
        for name, strength, instr in catalog[: settings.num_slots]:
            m = Medication(user_id=patient.user_id, name=name, strength=strength, instructions_text=instr,
                           warnings=["Demo only"], source="demo_seed", confirmed_by_user=True,
                           confirmed_by="test", confirmed_at=created, created_at=created)
            s.add(m)
            meds.append(m)
        s.flush()
        for comp, med in zip(comps, meds):
            comp.medication_id = med.medication_id
        times = ["08:00", "13:00", "20:00"]
        scheds = [Schedule(medication_id=m.medication_id, time_of_day=t, created_at=created,
                           created_by_user_id=doctor.user_id) for m, t in zip(meds, times)]
        s.add_all(scheds)
        s.flush()
        return {
            "patient_id": patient.user_id,
            "family_id": family.user_id,
            "doctor_id": doctor.user_id,
            "device_id": dev.device_id,
            "med_ids": [m.medication_id for m in meds],
            "compartment_ids": [c.compartment_id for c in comps],
            "schedule_ids": [sc.schedule_id for sc in scheds],
            "link_code": "ALEX2026",
        }


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


__all__ = [
    "FakeHardware", "FakeDropHardware", "FakeSpeaker", "FakeExtractor",
    "seed_minimal", "seed_v2", "wait_until", "dtime",
]
