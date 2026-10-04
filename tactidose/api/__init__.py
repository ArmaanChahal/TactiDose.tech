"""HTTP API v2 (docs/API.md): one router per area, combined by :func:`api_router`."""

from __future__ import annotations

from fastapi import APIRouter


def api_router() -> APIRouter:
    """Every ``/api/*`` route. Imported lazily so ``import tactidose.api`` stays cheap."""
    from tactidose.api import (
        accounts,
        chat,
        device,
        events,
        extras,
        health,
        notifications,
        patients,
        reports,
    )

    root = APIRouter()
    for module in (health, accounts, patients, chat, reports, notifications, events, device, extras):
        root.include_router(module.router)
    return root


__all__ = ["api_router"]
