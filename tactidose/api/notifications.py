"""``/api/notifications`` — the signed-in user's own notifications (one row per recipient)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from tactidose.api.common import TactiRoute
from tactidose.auth.deps import CurrentUser, ServicesDep

router = APIRouter(prefix="/api/notifications", route_class=TactiRoute, tags=["notifications"])


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[StrictInt] | None = Field(None, max_length=1000)


@router.get("")
def list_notifications(user: CurrentUser, services: ServicesDep, unread: bool = False,
                       limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return services.notifications.list_for_user(user.user_id, unread_only=unread, limit=limit)


@router.post("/read")
def mark_read(user: CurrentUser, services: ServicesDep, body: ReadBody | None = None) -> dict[str, Any]:
    ids = body.ids if body is not None else None
    updated = services.notifications.mark_read(user.user_id, ids)
    return {"updated": int(updated or 0)}
