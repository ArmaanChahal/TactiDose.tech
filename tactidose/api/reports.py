"""``/api/reports/{rid}…`` — report metadata, the PDF itself and "send to doctor".

Access follows the report's patient: the patient themself or a linked caregiver (403 otherwise,
404 for unknown ids). PDFs carry health information: ``Cache-Control: no-store``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from tactidose.api.common import TactiRoute, require_service
from tactidose.auth.deps import CurrentUser, ServicesDep, check_view

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", route_class=TactiRoute, tags=["reports"])

REPORTS = "Reports"


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_email: StrictStr | None = Field(None, max_length=254)


def _report_for(services: Any, user: Any, rid: int) -> dict[str, Any]:
    reports = require_service(services, "reports", REPORTS)
    meta = reports.get(rid)
    if not meta:
        raise HTTPException(404, f"Report {rid} not found.")
    check_view(services, user, meta.get("patient_id"))
    return meta


def _check_report_access(services: Any, user: Any, rid: int) -> None:
    """Access check without loading the report (``ReportService.patient_of`` when available)."""
    reports = require_service(services, "reports", REPORTS)
    patient_of = getattr(reports, "patient_of", None)
    if callable(patient_of):
        check_view(services, user, patient_of(rid))
    else:
        _report_for(services, user, rid)


@router.get("/{rid}")
def get_report(rid: int, user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    return _report_for(services, user, rid)


@router.get("/{rid}/pdf")
def get_pdf(rid: int, user: CurrentUser, services: ServicesDep, download: bool = False) -> Response:
    _check_report_access(services, user, rid)
    pdf = services.reports.pdf_bytes(rid)
    if not pdf:
        raise HTTPException(404, "This report has no PDF.")
    disposition = "attachment" if download else "inline"
    return Response(
        content=bytes(pdf), media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="tactidose-report-{rid}.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{rid}/send")
def send_report(rid: int, user: CurrentUser, services: ServicesDep, body: SendBody | None = None) -> dict[str, Any]:
    _check_report_access(services, user, rid)
    to_email = (body.to_email or "").strip() if body else ""
    out = services.reports.send(rid, sent_by_user_id=user.user_id, to_email=to_email or None)
    log.info("report %s sent by user %s (%d deliveries)", rid, user.user_id, len(out.get("deliveries", [])))
    return out
