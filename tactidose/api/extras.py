"""Optional extras, off the main flow (docs/API.md "Optional extras").

* Label scanning (caregiver): photo -> Gemini transcription -> UNCONFIRMED ``LabelScan``; only a
  person's confirmation creates a ``Medication`` (``medication/onboarding.py``).
* ``GET /api/analytics/summary`` — local adherence analytics for the device's patient.
* ``GET /api/analytics/snowflake`` / ``POST …/sync`` — Snowflake outbox status / one sync.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from tactidose.api import domain
from tactidose.api.common import TactiRoute, require_service, trigger_scheduler
from tactidose.auth.deps import CaregiverUser, CurrentUser, PatientEditor, ServicesDep, check_view

log = logging.getLogger(__name__)

router = APIRouter(route_class=TactiRoute, tags=["extras"])

SCANNING = "Label scanning"


class ConfirmScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr | None = None
    strength: StrictStr | None = None
    instructions_text: StrictStr | None = None
    instructions: StrictStr | None = None
    warnings: list[StrictStr] | None = None
    confirmed: StrictBool | None = None
    confirmed_by: StrictStr | None = None


def _who(user: Any) -> str:
    return (user.display_name or user.email or f"user {user.user_id}")[:120]


# --------------------------------------------------------------------------- label scans


@router.post("/api/patients/{pid}/scans")
def scan_label(pid: int, user: PatientEditor, services: ServicesDep,
               image: Annotated[UploadFile, File()]) -> dict[str, Any]:
    require_service(services, "onboarding", SCANNING)
    limit = int(services.settings.max_label_image_bytes)
    data = image.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"The image is too large; the limit is {limit // (1024 * 1024)} MB.")
    return domain.scan_label(services, pid, data, image.content_type or "")


@router.post("/api/patients/{pid}/scans/{scan_id}/confirm", status_code=status.HTTP_201_CREATED)
def confirm_scan(pid: int, scan_id: int, body: ConfirmScanBody, user: PatientEditor,
                 services: ServicesDep) -> dict[str, Any]:
    require_service(services, "onboarding", SCANNING)
    if body.confirmed is not True:
        raise HTTPException(422, "Please review the information and confirm it is correct (confirmed: true).")
    fields = {k: getattr(body, k) for k in body.model_fields_set if k not in ("confirmed", "confirmed_by")}
    out = domain.confirm_scan(services, pid, scan_id, fields, confirmed_by=_who(user))
    trigger_scheduler(services)
    return out


@router.post("/api/patients/{pid}/scans/{scan_id}/reject")
def reject_scan(pid: int, scan_id: int, user: PatientEditor, services: ServicesDep) -> dict[str, Any]:
    require_service(services, "onboarding", SCANNING)
    return domain.reject_scan(services, pid, scan_id, by=_who(user))


# --------------------------------------------------------------------------- analytics


@router.get("/api/analytics/summary")
def analytics_summary(user: CurrentUser, services: ServicesDep, patient_id: int | None = None,
                      days: int = Query(7, ge=1, le=366)) -> dict[str, Any]:
    pid = patient_id
    if pid is None:
        if user.is_patient:
            pid = user.user_id
        else:
            linked = list(services.auth.linked_patient_ids(user))
            if len(linked) != 1:
                raise HTTPException(422, "patient_id is required.")
            pid = int(linked[0])
    check_view(services, user, pid)
    if domain.device_patient(services) != pid:
        raise HTTPException(404, "There is no device data for this patient yet.")
    from tactidose.medication.analytics import local_summary

    out = local_summary(services.db, services.clock, services.settings, days)
    out["patient_id"] = pid
    return out


def _snowflake_status(services: Any) -> dict[str, Any]:
    sync = getattr(services, "analytics_sync", None)
    if sync is None:
        return {"configured": False, "last_sync": None, "pending": None, "sent": None, "last_error": None}
    return sync.status()


@router.get("/api/analytics/snowflake")
def snowflake_status(user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    return _snowflake_status(services)


@router.post("/api/analytics/snowflake/sync")
def snowflake_sync(user: CaregiverUser, services: ServicesDep) -> dict[str, Any]:
    sync = getattr(services, "analytics_sync", None)
    if sync is None or not getattr(sync, "configured", False):
        raise HTTPException(409, "Snowflake is not configured.")
    report = sync.sync_once()
    out = _snowflake_status(services)
    out["last_report"] = report
    return out
