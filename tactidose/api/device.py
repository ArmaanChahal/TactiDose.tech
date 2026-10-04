"""``/api/device*`` (device status/home/stop/reconnect) and ``/api/demo/*`` (demo mode only).

Device access follows the patient the configured device dispenses for: view/stop for that patient
and linked caregivers, home/reconnect for linked caregivers only.

Demo controls are 403 unless ``settings.demo_mode`` and need a signed-in user. Pills are only
ever released through ``DropService``: a ``DROP_SLOT n`` typed in the demo console becomes a
recorded drop with source ``demo`` (inventory, ``pill_drops`` and notifications stay correct);
raw ``DISPENSE_SLOT`` / ``OPEN_GATE`` (which would release unrecorded pills) are refused.
Clock travel and demo resets run a scheduler tick immediately and wake the scheduler loop.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from tactidose.api import domain
from tactidose.api.common import (
    TactiRoute,
    call_supported,
    command_result_view,
    device_view,
    to_dict,
    trigger_scheduler,
)
from tactidose.auth.deps import CurrentUser, DemoUser, ServicesDep, check_edit, check_view
from tactidose.core.bus import Topic
from tactidose.core.interfaces import AuthUser
from tactidose.db.models import DropSource
from tactidose.hardware.protocol import MAX_LINE_LENGTH, CommandName, parse_command

log = logging.getLogger(__name__)

router = APIRouter(route_class=TactiRoute, tags=["device"])

NO_PATIENT = "No patient is linked to this device yet."
NOT_ALLOWED_DEVICE = "You do not have access to this dispenser."
RELEASE_REFUSED = (
    "Pills are only released with DROP_SLOT n, so every pill is recorded. "
    "DISPENSE_SLOT and OPEN_GATE are not allowed from the console."
)
NO_SIM = "The simulator is only available when the hardware mode is 'sim'."
_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
MAX_TRAVEL = timedelta(days=30)


# --------------------------------------------------------------------------- helpers


def _device_patient(services: Any, user: Any, *, edit: bool) -> int:
    """The patient whose device ``user`` may use: the configured device's patient, or - with a
    shared dispenser (everyone on the same ESP32) - the user's own / a linked patient that has a
    device record. 403 otherwise."""
    owner = domain.device_patient(services)
    check = check_edit if edit else check_view
    if owner is not None:
        try:
            check(services, user, owner)
            return owner
        except HTTPException:
            if not services.settings.effective_shared_device:
                raise
    if services.settings.effective_shared_device:
        for pid in _shared_candidates(services, user):
            try:
                check(services, user, pid)
                return pid
            except HTTPException:
                continue
    raise HTTPException(403, NO_PATIENT if owner is None else NOT_ALLOWED_DEVICE)


def _shared_candidates(services: Any, user: Any) -> list[int]:
    """Patients with a device record that ``user`` is (patient) or is linked to (caregiver)."""
    from sqlalchemy import select

    from tactidose.db.models import Device

    pids = [user.user_id] if getattr(user, "is_patient", False) else [
        int(p) for p in services.auth.linked_patient_ids(user)]
    if not pids:
        return []
    with services.db.session() as s:
        have = set(s.scalars(select(Device.user_id).where(Device.user_id.in_(pids))))
    return [p for p in pids if p in have]


def _interrupt(services: Any) -> bool:
    """Make DropService abort a drop in progress (it sends STOP itself if motion is in flight)."""
    try:
        return bool(services.drops.interrupt())
    except Exception:  # noqa: BLE001 - STOP below must still be sent
        log.exception("drops.interrupt() failed")
        return False


def _command_out(services: Any, result: Any, **extra: Any) -> dict[str, Any]:
    return {"ok": bool(result.ok) if result is not None else False, "result": command_result_view(result),
            "device": device_view(services.hardware), **extra}


def demo_viewer(user: DemoUser, services: ServicesDep) -> AuthUser:
    """Demo panel (clock, simulator): the device's patient and their linked doctor/family only."""
    _device_patient(services, user, edit=False)
    return user


def demo_operator(user: DemoUser, services: ServicesDep) -> AuthUser:
    """Device console and data reset: doctor/family linked to the device's patient only."""
    owner = domain.device_patient(services)
    if owner is None:
        if not user.is_caregiver:
            raise HTTPException(403, NO_PATIENT)
        return user
    check_edit(services, user, owner)
    return user


DemoViewer = Annotated[AuthUser, Depends(demo_viewer)]
DemoOperator = Annotated[AuthUser, Depends(demo_operator)]


# --------------------------------------------------------------------------- device


def _device_out(hardware: Any) -> dict[str, Any]:
    """DeviceSnapshot + the restocking lid (Wi-Fi ESP32 only): ``lid_supported`` and ``lid``
    (open/closed/None)."""
    out = device_view(hardware)
    out["lid_supported"] = callable(getattr(hardware, "set_lid", None))
    out["lid"] = getattr(hardware, "lid_state", None)
    return out


@router.get("/api/device")
def get_device(user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    _device_patient(services, user, edit=False)
    return _device_out(services.hardware)


class LidBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["open", "close"]


@router.post("/api/device/lid")
def lid(body: LidBody, user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    """Open / close the dispenser lid to restock it (Wi-Fi ESP32: GET /lid?state=open|close).
    Restocking is a doctor/family task (like refills): linked doctor/family only. Never drops a pill;
    dispensing is POST /api/patients/{pid}/drops (drop rules) -> GET /dispense?pill=N."""
    _device_patient(services, user, edit=True)
    set_lid = getattr(services.hardware, "set_lid", None)
    if not callable(set_lid):
        raise HTTPException(409, "This dispenser has no lid control (only the Wi-Fi ESP32 has one).")
    out = set_lid(body.state == "open")
    log.info("lid %s requested by user %s -> %s", body.state, user.user_id, out.get("detail"))
    return {**out, "device": _device_out(services.hardware)}


@router.post("/api/device/home")
def home(user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    _device_patient(services, user, edit=True)
    result = services.hardware.home()
    log.info("HOME requested by user %s -> %s", user.user_id, result.summary)
    return _command_out(services, result)


@router.post("/api/device/stop")
def stop(user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    _device_patient(services, user, edit=False)
    interrupted = _interrupt(services)
    result = services.hardware.stop()
    log.warning("STOP requested by user %s -> %s", user.user_id, result.summary)
    return _command_out(services, result, interrupted=interrupted)


@router.post("/api/device/reconnect")
def reconnect(user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    _device_patient(services, user, edit=True)
    ok = bool(services.hardware.reconnect())
    return {"ok": ok, "device": device_view(services.hardware)}


# --------------------------------------------------------------------------- demo: console


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: StrictStr = Field(max_length=MAX_LINE_LENGTH)


@router.post("/api/demo/command")
def demo_command(body: CommandBody, user: DemoOperator, services: ServicesDep) -> dict[str, Any]:
    hw = services.hardware
    parsed = parse_command(body.line, int(hw.num_slots))
    if parsed.empty:
        raise HTTPException(422, "Type a command, for example STATUS or DROP_SLOT 0.")
    if parsed.command is None:
        reason = parsed.error.value if parsed.error is not None else "INVALID"
        raise HTTPException(422, f"Not a valid command ({reason}).")
    cmd = parsed.command
    if cmd.name is CommandName.DROP_SLOT:
        owner = domain.device_patient(services)
        if owner is None:
            raise HTTPException(409, NO_PATIENT)
        outcome = services.drops.request_drop(
            patient_id=owner, source=DropSource.DEMO.value, slot=cmd.slot, requested_by_user_id=user.user_id,
        )
        return {"ok": bool(outcome.dropped), "result": command_result_view(outcome.hardware),
                "device": device_view(hw), "drop": to_dict(outcome)}
    if cmd.name in (CommandName.DISPENSE_SLOT, CommandName.OPEN_GATE):
        raise HTTPException(409, RELEASE_REFUSED)
    if cmd.name is CommandName.STOP:
        _interrupt(services)
        result = hw.stop()
    else:
        result = hw.send_raw(cmd.to_line())
    return _command_out(services, result)


# --------------------------------------------------------------------------- demo: clock


class ClockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_time: StrictStr | None = None
    local_datetime: StrictStr | None = None
    offset_minutes: StrictInt | None = Field(None, ge=-int(MAX_TRAVEL.total_seconds() // 60),
                                             le=int(MAX_TRAVEL.total_seconds() // 60))
    reset: StrictBool | None = None


def clock_view(clock: Any) -> dict[str, Any]:
    now = clock.now()
    return {
        "now_local": clock.to_local(now).isoformat(),
        "now_utc": now.isoformat(),
        "offset_s": int(round((now - datetime.now(timezone.utc)).total_seconds())),
        "travelling": bool(clock.is_travelling),
        "tz": clock.tz_name,
    }


def _tick_now(services: Any) -> None:
    loop = getattr(services, "scheduler_loop", None)
    try:
        if loop is not None:
            loop.tick_now()
        else:
            services.scheduler.tick()
    except Exception:  # noqa: BLE001 - the loop retries on its next cycle
        log.exception("scheduler tick after clock change failed")


def after_clock_change(services: Any) -> dict[str, Any]:
    _tick_now(services)
    trigger_scheduler(services)
    view = clock_view(services.clock)
    services.bus.publish(Topic.CLOCK_CHANGED, {"now_local": view["now_local"], "offset_s": view["offset_s"],
                                               "travelling": view["travelling"]})
    return view


def _parse_local_datetime(services: Any, text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "local_datetime must look like 2026-10-05T08:00.") from None
    return dt if dt.tzinfo is not None else services.clock.localize(dt)


@router.get("/api/demo/clock")
def get_clock(user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    return clock_view(services.clock)


@router.post("/api/demo/clock")
def set_clock(body: ClockBody, user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    """``{local_time: "08:00"}`` (today, local), ``{local_datetime: "2026-10-05T08:00"}`` (naive =
    local), ``{offset_minutes: 30}`` (absolute offset from the real time) or ``{reset: true}``."""
    given =[k for k in ("local_time", "local_datetime", "offset_minutes", "reset") if getattr(body, k) is not None]
    if len(given) != 1:
        raise HTTPException(422, "Send exactly one of local_time, local_datetime, offset_minutes or reset.")
    clock = services.clock
    real_now = datetime.now(timezone.utc)
    if body.local_time is not None:
        m = _HHMM.match(body.local_time)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise HTTPException(422, "local_time must be HH:MM (24-hour), e.g. 08:00.")
        target = clock.localize(datetime.combine(clock.today_local(), time(int(m.group(1)), int(m.group(2)))))
    elif body.local_datetime is not None:
        target = _parse_local_datetime(services, body.local_datetime)
    elif body.offset_minutes is not None:
        target = real_now + timedelta(minutes=body.offset_minutes)
    elif body.reset:
        target = None
    else:
        raise HTTPException(422, "reset must be true.")
    if target is None:
        clock.reset()
    else:
        if abs(target - real_now) > MAX_TRAVEL:
            raise HTTPException(422, "Clock travel is limited to 30 days from now.")
        clock.travel_to(target)
    log.info("demo clock -> %s (by user %s)", clock.local_now().isoformat(), user.user_id)
    return after_clock_change(services)


@router.post("/api/demo/jump-to-next-dose")
def jump_to_next_dose(user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    owner = domain.device_patient(services)
    if owner is None:
        raise HTTPException(409, NO_PATIENT)
    _tick_now(services)
    nxt = domain.next_dose(services, owner)
    if nxt:
        at = _parse_iso(nxt.get("scheduled_at"))
        if at is not None and at > services.clock.now():
            services.clock.travel_to(at)
            log.info("demo clock jumped to the next dose at %s", at.isoformat())
    return {"clock": after_clock_change(services), "next": nxt}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return value if isinstance(value, datetime) else None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- demo: simulator


class PillsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: StrictInt = Field(ge=0)
    count: StrictInt = Field(ge=0, le=999)


class SimulatorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault: StrictStr | None = None
    enabled: StrictBool | None = None
    press: StrictStr | None = None
    reboot: StrictBool | None = None
    pills: PillsBody | None = None


def simulator_view(sim: Any) -> dict[str, Any]:
    if sim is None:
        return {"available": False, "physical": None, "faults": {}}
    return {"available": True, "physical": sim.physical(), "faults": sim.faults()}


@router.get("/api/demo/simulator")
def get_simulator(user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    return simulator_view(services.sim)


@router.post("/api/demo/simulator")
def post_simulator(body: SimulatorBody, user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    sim = services.sim
    if sim is None:
        raise HTTPException(409, NO_SIM)
    actions = [k for k in ("fault", "press", "reboot", "pills") if getattr(body, k) is not None]
    if len(actions) != 1:
        raise HTTPException(422, "Send exactly one of {fault, enabled}, {press}, {reboot: true} or {pills: {slot, count}}.")
    try:
        if body.fault is not None:
            if body.enabled is None:
                raise HTTPException(422, "Send enabled: true or false with the fault.")
            sim.set_fault(body.fault, body.enabled)
        elif body.press is not None:
            sim.press(body.press)
        elif body.reboot is not None:
            if body.reboot is not True:
                raise HTTPException(422, "reboot must be true.")
            sim.reboot()
        elif body.pills is not None:
            if body.pills.slot >= int(services.settings.num_slots):
                raise HTTPException(422, f"slot must be 0..{int(services.settings.num_slots) - 1}.")
            set_pills = getattr(sim, "set_pills", None)
            if not callable(set_pills):
                raise HTTPException(409, "This simulator cannot change pill counts.")
            set_pills(body.pills.slot, body.pills.count)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    log.info("demo simulator action %s by user %s", actions[0], user.user_id)
    return simulator_view(sim)


# --------------------------------------------------------------------------- demo: reset


def push_pill_counts(sim: Any, containers: Any) -> int:
    """Make the simulator's *physical* pill counts match the containers of a seed/reset summary
    (``[{slot, pill_count}, …]``), so the drop sensor agrees with the database. Returns the count set."""
    set_pills = getattr(sim, "set_pills", None)
    if sim is None or not callable(set_pills) or not isinstance(containers, (list, tuple)):
        return 0
    done = 0
    for c in containers:
        slot, count = (c.get("slot"), c.get("pill_count")) if isinstance(c, dict) else (None, None)
        if isinstance(slot, int) and isinstance(count, int) and not isinstance(count, bool):
            try:
                set_pills(slot, max(0, count))
                done += 1
            except ValueError:
                log.debug("simulator refused pills for slot %r", slot, exc_info=True)
    return done


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reseed: StrictBool | None = None


@router.post("/api/demo/reset")
def reset(user: DemoOperator, services: ServicesDep, body: ResetBody | None = None) -> dict[str, Any]:
    try:
        from tactidose.db.seed import reset_demo
    except ImportError:
        raise HTTPException(503, "Demo reset is not available in this build.") from None
    reseed = True if body is None or body.reseed is None else body.reseed
    loop = getattr(services, "scheduler_loop", None)
    guard = loop.exclusive() if loop is not None else contextlib.nullcontext()
    with guard:
        # Real time first: the re-seeded schedules start "now" (no-backfill rule), so seeding on a
        # travelled clock (say, tomorrow) would suppress the rest of today's doses.
        services.clock.reset()
        # keep_sessions: the operator who pressed "reset" stays signed in (accounts survive a reset).
        summary = call_supported(reset_demo, services.db, services.settings, services.clock,
                                 reseed=reseed, auth=services.auth, bus=services.bus, keep_sessions=True)
    if isinstance(summary, dict):
        push_pill_counts(services.sim, summary.get("containers"))
    after_clock_change(services)
    services.bus.publish(Topic.NOTICE, {"level": "info", "message": "Demo data was reset."})
    log.warning("demo data reset by user %s", user.user_id)
    return {"ok": True, "summary": summary if isinstance(summary, dict) else None}
