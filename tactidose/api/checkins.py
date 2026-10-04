"""``GET /api/patients/{pid}/wellbeing`` — saved well-being check-ins next to the pill history.

The patient themself or a linked doctor/family account (``PatientViewer``). Each check-in lists
its ratings, the patient's confirmed notes and the drop it followed (``after_drop``). Read-only:
only the patient deletes check-ins (``DELETE /api/wellbeing/v1/me/history/…``). Check-ins are
informal and non-clinical; they never affect drops. Reading works even when the optional
``tactidose-wellbeing`` package is not installed (the rows live in the main database).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response

from tactidose.api.common import TactiRoute
from tactidose.auth.deps import PatientViewer, ServicesDep
from tactidose.wellbeing import checkin_views

router = APIRouter(route_class=TactiRoute, tags=["wellbeing"])


@router.get("/api/patients/{pid}/wellbeing")
def list_checkins(pid: int, user: PatientViewer, services: ServicesDep, response: Response,
                  days: int = Query(30, ge=1, le=366)) -> list[dict[str, Any]]:
    response.headers["Cache-Control"] = "no-store"
    return checkin_views(services.db, services.clock, pid, days=days)
