"""``/api/patients/{pid}/…``: status, containers, medications, schedules, settings, drops,
doses, conversations and reports (docs/API.md v2, permission matrix ARCHITECTURE §9).

* view (patient themself or a linked caregiver): every ``GET``, ``POST …/reports``;
* caregiver (linked doctor/family only): containers/refills, medications, schedules, settings,
  drop reviews, skipping doses;
* patient only: ``POST …/drops`` — the request goes to ``DropService.request_drop`` with
  source ``manual``; the deterministic rules there decide (HTTP 200 for DENIED/FAILED too).

Edits wake the scheduler loop (a new schedule, a refill or a resolved review can make a dose
droppable now). The domain services publish the ``patient.status`` refetch hints themselves.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from tactidose.api import domain
from tactidose.api.common import TactiRoute, to_dict, trigger_scheduler
from tactidose.auth.deps import PatientEditor, PatientSelf, PatientViewer, ServicesDep
from tactidose.db.models import DropSource, DropStatus

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/patients/{pid}", route_class=TactiRoute, tags=["patients"])

_MAX_COUNT = 10_000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ContainerBody(_Body):
    medication_id: StrictInt | None = Field(None, ge=1)
    pill_count: StrictInt | None = Field(None, ge=0, le=_MAX_COUNT)
    capacity: StrictInt | None = Field(None, ge=1, le=_MAX_COUNT)
    low_stock_threshold: StrictInt | None = Field(None, ge=0, le=_MAX_COUNT)


class RefillBody(_Body):
    set_to: StrictInt | None = Field(None, alias="set", ge=0, le=_MAX_COUNT)
    add: StrictInt | None = Field(None, ge=1, le=_MAX_COUNT)


class MedicationBody(_Body):
    name: StrictStr | None = None
    strength: StrictStr | None = None
    instructions_text: StrictStr | None = None
    instructions: StrictStr | None = None
    warnings: list[StrictStr] | None = None
    confirmed: StrictBool | None = None
    #: Accepted for compatibility; the signed-in caregiver is recorded instead.
    confirmed_by: StrictStr | None = None


class ScheduleBody(_Body):
    medication_id: StrictInt = Field(ge=1)
    time_of_day: StrictStr
    frequency: StrictStr | None = None
    days_of_week: list[StrictStr] | None = None


class SchedulePatch(_Body):
    time_of_day: StrictStr | None = None
    frequency: StrictStr | None = None
    days_of_week: list[StrictStr] | None = None
    active: StrictBool | None = None


class SettingsPatch(_Body):
    manual_cooldown_minutes: StrictInt | None = Field(None, ge=0, le=1440)
    auto_drop_enabled: StrictBool | None = None


class DropBody(_Body):
    slot: StrictInt | None = Field(None, ge=0)
    medication_id: StrictInt | None = Field(None, ge=1)


class ResolveBody(_Body):
    dropped: StrictBool
    note: StrictStr | None = Field(None, max_length=255)


class SkipBody(_Body):
    note: StrictStr | None = Field(None, max_length=255)


class ReportBody(_Body):
    days: StrictInt = Field(ge=1)


def _given(body: BaseModel) -> dict[str, Any]:
    """Fields present in the request (``null`` included), by field name."""
    return {name: getattr(body, name) for name in body.model_fields_set}


def _changed(services: Any, pid: int, reason: str) -> None:
    log.debug("patient %s changed (%s): waking the scheduler loop", pid, reason)
    trigger_scheduler(services)


def _who(user: Any) -> str:
    return (user.display_name or user.email or f"user {user.user_id}")[:120]


# --------------------------------------------------------------------------- status & containers


@router.get("/status")
def get_status(pid: int, user: PatientViewer, services: ServicesDep) -> dict[str, Any]:
    return to_dict(services.drops.patient_status(pid))


@router.get("/containers")
def get_containers(pid: int, user: PatientViewer, services: ServicesDep) -> list[dict[str, Any]]:
    return domain.list_containers(services, pid)


@router.put("/containers/{slot}")
def put_container(pid: int, slot: int, body: ContainerBody, user: PatientEditor,
                  services: ServicesDep) -> dict[str, Any]:
    fields = _given(body)
    if not fields:
        raise HTTPException(422, "Send at least one of medication_id, pill_count, capacity, low_stock_threshold.")
    for key in ("pill_count", "capacity", "low_stock_threshold"):
        if key in fields and fields[key] is None:
            raise HTTPException(422, f"{key} must be a whole number.")
    _check_slot(services, slot)
    out = domain.update_container(services, pid, slot, fields, by_user_id=user.user_id)
    _changed(services, pid, "container")
    return out


@router.post("/containers/{slot}/refill")
def refill_container(pid: int, slot: int, body: RefillBody, user: PatientEditor,
                     services: ServicesDep) -> dict[str, Any]:
    if (body.set_to is None) == (body.add is None):
        raise HTTPException(422, "Send either {\"set\": n} or {\"add\": n}.")
    _check_slot(services, slot)
    out = domain.refill_container(services, pid, slot, set_to=body.set_to, add=body.add, by_user_id=user.user_id)
    _changed(services, pid, "refill")
    return out


def _check_slot(services: Any, slot: int) -> None:
    n = int(services.settings.num_slots)
    if not 0 <= slot < n:
        raise HTTPException(404, f"Container {slot + 1} does not exist; this device has {n} containers.")


# --------------------------------------------------------------------------- medications


def _medication_fields(body: MedicationBody) -> dict[str, Any]:
    return {k: v for k, v in _given(body).items() if k not in ("confirmed", "confirmed_by")}


@router.get("/medications")
def get_medications(pid: int, user: PatientViewer, services: ServicesDep,
                    include_inactive: bool = False) -> list[dict[str, Any]]:
    return domain.list_medications(services, pid, include_inactive=include_inactive)


@router.post("/medications", status_code=status.HTTP_201_CREATED)
def post_medication(pid: int, body: MedicationBody, user: PatientEditor, services: ServicesDep) -> dict[str, Any]:
    if body.confirmed is not True:
        raise HTTPException(422, "Please confirm the medication information is correct (confirmed: true).")
    out = domain.create_medication(services, pid, _medication_fields(body), confirmed_by=_who(user))
    _changed(services, pid, "medication")
    return out


@router.patch("/medications/{mid}")
def patch_medication(pid: int, mid: int, body: MedicationBody, user: PatientEditor,
                     services: ServicesDep) -> dict[str, Any]:
    fields = _medication_fields(body)
    if fields and body.confirmed is not True:
        raise HTTPException(422, "Changed medication information must be confirmed (confirmed: true).")
    out = domain.update_medication(services, pid, mid, fields, confirmed=body.confirmed is True,
                                   confirmed_by=_who(user))
    _changed(services, pid, "medication")
    return out


@router.delete("/medications/{mid}")
def delete_medication(pid: int, mid: int, user: PatientEditor, services: ServicesDep) -> dict[str, Any]:
    domain.archive_medication(services, pid, mid)
    _changed(services, pid, "medication")
    return {"ok": True}


# --------------------------------------------------------------------------- schedules


@router.get("/schedules")
def get_schedules(pid: int, user: PatientViewer, services: ServicesDep,
                  include_inactive: bool = False) -> list[dict[str, Any]]:
    return domain.list_schedules(services, pid, include_inactive=include_inactive)


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
def post_schedule(pid: int, body: ScheduleBody, user: PatientEditor, services: ServicesDep) -> dict[str, Any]:
    out = domain.create_schedule(
        services, pid, medication_id=body.medication_id, time_of_day=body.time_of_day,
        frequency=body.frequency, days_of_week=body.days_of_week, created_by_user_id=user.user_id,
    )
    _changed(services, pid, "schedule")
    return out


@router.patch("/schedules/{sid}")
def patch_schedule(pid: int, sid: int, body: SchedulePatch, user: PatientEditor,
                   services: ServicesDep) -> dict[str, Any]:
    fields = _given(body)
    if not fields:
        raise HTTPException(422, "Send at least one of time_of_day, frequency, days_of_week, active.")
    if "active" in fields and fields["active"] is None:
        raise HTTPException(422, "active must be true or false.")
    out = domain.update_schedule(services, pid, sid, fields, by_user_id=user.user_id)
    _changed(services, pid, "schedule")
    return out


@router.delete("/schedules/{sid}")
def delete_schedule(pid: int, sid: int, user: PatientEditor, services: ServicesDep) -> dict[str, Any]:
    domain.delete_schedule(services, pid, sid, by_user_id=user.user_id)
    _changed(services, pid, "schedule")
    return {"ok": True}


# --------------------------------------------------------------------------- settings


@router.get("/settings")
def get_settings(pid: int, user: PatientViewer, services: ServicesDep) -> dict[str, Any]:
    return domain.get_settings(services, pid)


@router.patch("/settings")
def patch_settings(pid: int, body: SettingsPatch, user: PatientEditor, services: ServicesDep) -> dict[str, Any]:
    fields = {k: v for k, v in _given(body).items() if v is not None}
    if not fields:
        raise HTTPException(422, "Send manual_cooldown_minutes (0-1440) and/or auto_drop_enabled.")
    out = domain.update_settings(services, pid, fields, by_user_id=user.user_id)
    log.info("settings of patient %s changed by user %s: %s", pid, user.user_id, fields)
    _changed(services, pid, "settings")
    return out


# --------------------------------------------------------------------------- drops


@router.post("/drops")
def post_drop(pid: int, body: DropBody, user: PatientSelf, services: ServicesDep) -> dict[str, Any]:
    if (body.slot is None) == (body.medication_id is None):
        raise HTTPException(422, "Send either {\"slot\": n} or {\"medication_id\": id}.")
    outcome = services.drops.request_drop(
        patient_id=pid, source=DropSource.MANUAL.value, slot=body.slot, medication_id=body.medication_id,
        requested_by_user_id=user.user_id,
    )
    return to_dict(outcome)


_DROP_STATUSES = {s.value for s in DropStatus}


@router.get("/drops")
def get_drops(pid: int, user: PatientViewer, services: ServicesDep,
              days: int = Query(7, ge=1, le=366), status_filter: str | None = Query(None, alias="status"),
              limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    wanted: set[str] | None = None
    if status_filter:
        wanted = {p.strip().upper() for p in status_filter.split(",") if p.strip()}
        unknown = wanted - _DROP_STATUSES
        if unknown:
            raise HTTPException(422, f"status must be one of {', '.join(sorted(_DROP_STATUSES))}.")
    return domain.recent_drops(services, pid, days=days, limit=limit, statuses=wanted)


@router.post("/drops/{drop_id}/resolve")
def resolve_drop(pid: int, drop_id: int, body: ResolveBody, user: PatientEditor,
                 services: ServicesDep) -> dict[str, Any]:
    out = domain.resolve_drop(services, pid, drop_id, dropped=body.dropped, note=body.note,
                              by_user_id=user.user_id)
    _changed(services, pid, "review")
    return out


# --------------------------------------------------------------------------- doses


@router.get("/doses")
def get_doses(pid: int, user: PatientViewer, services: ServicesDep,
              date_: str | None = Query(None, alias="date")) -> list[dict[str, Any]]:
    if date_:
        try:
            local_date = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(422, "date must be YYYY-MM-DD.") from None
    else:
        local_date = services.clock.today_local()
    return domain.list_doses(services, pid, local_date)


@router.post("/doses/{event_id}/skip")
def skip_dose(pid: int, event_id: int, user: PatientEditor, services: ServicesDep,
              body: SkipBody | None = None) -> dict[str, Any]:
    out = domain.skip_dose(services, pid, event_id, note=body.note if body else None, by_user_id=user.user_id)
    _changed(services, pid, "dose")
    return out


# --------------------------------------------------------------------------- conversations


@router.get("/conversations")
def get_conversations(pid: int, user: PatientViewer, services: ServicesDep,
                      limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    agent = services.agent
    if agent is None:
        return []
    return agent.conversations(pid, limit=limit)


@router.get("/conversations/{cid}/messages")
def get_messages(pid: int, cid: int, user: PatientViewer, services: ServicesDep) -> list[dict[str, Any]]:
    domain.require_owned(services, "conversation", cid, pid, "Conversation")
    agent = services.agent
    if agent is None:
        raise HTTPException(503, "The assistant is not available right now.")
    return agent.messages(pid, cid)


# --------------------------------------------------------------------------- reports


@router.get("/reports")
def get_reports(pid: int, user: PatientViewer, services: ServicesDep) -> list[dict[str, Any]]:
    reports = services.reports
    if reports is None:
        return []
    return reports.list(pid)


@router.post("/reports", status_code=status.HTTP_201_CREATED)
def post_report(pid: int, body: ReportBody, user: PatientViewer, services: ServicesDep) -> dict[str, Any]:
    max_days = int(services.settings.report_max_days)
    if body.days > max_days:
        raise HTTPException(422, f"days must be between 1 and {max_days}.")
    reports = services.reports
    if reports is None:
        raise HTTPException(503, "Reports are not available right now.")
    out = reports.generate(patient_id=pid, days=body.days, created_by_user_id=user.user_id)
    log.info("report %s generated for patient %s by user %s", out.get("report_id"), pid, user.user_id)
    return out


__all__ = ["router"]
