"""HTTP API v2 (docs/API.md): one router per area, combined by :func:`api_router`."""

from __future__ import annotations

from fastapi import APIRouter


def api_router() -> APIRouter:
    """Every ``/api/*`` route. Imported lazily so ``import tactidose.api`` stays cheap."""
    from tactidose.api import (
        accounts,
        chat,
        checkins,
        device,
        events,
        extras,
        guided,
        health,
        notifications,
        patients,
        reports,
    )

    root = APIRouter()
    for module in (health, accounts, patients, checkins, chat, reports, notifications, events, device, guided, extras):
        root.include_router(module.router)
    return root


__all__ = ["api_router"]
