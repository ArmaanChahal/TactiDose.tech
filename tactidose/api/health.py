"""``GET /api/health`` — liveness and configuration summary (no auth, no secrets)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from tactidose import __version__
from tactidose.api.common import TactiRoute
from tactidose.auth.deps import ServicesDep

log = logging.getLogger(__name__)

router = APIRouter(route_class=TactiRoute, tags=["health"])


def _redact(text: str, settings: Any) -> str:
    secrets: list[str] = []
    for name in ("tidb_password", "smtp_password", "gemini_api_key", "elevenlabs_api_key",
                 "snowflake_password", "snowflake_token"):
        value = getattr(settings, name, None)
        raw = value.get_secret_value() if value is not None and hasattr(value, "get_secret_value") else None
        if raw:
            secrets.append(raw)
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _call_status(obj: Any) -> dict[str, Any] | None:
    fn = getattr(obj, "status", None)
    if not callable(fn):
        return None
    try:
        out = fn()
        return out if isinstance(out, dict) else None
    except Exception:  # noqa: BLE001 - health must always answer
        log.debug("status() of %r failed", obj, exc_info=True)
        return None


def health_payload(services: Any) -> dict[str, Any]:
    settings = services.settings
    db_ok, db_info = services.db.healthcheck()
    try:
        hardware: dict[str, Any] = services.hardware.snapshot().to_dict()
    except Exception as exc:  # noqa: BLE001
        hardware = {"connected": False, "error": type(exc).__name__}
    clock = services.clock
    now = clock.now()
    provider = settings.effective_agent_provider
    loop = getattr(services, "scheduler_loop", None)
    voice_loop = getattr(services, "voice_loop", None)
    return {
        "ok": bool(db_ok),
        "version": __version__,
        "db": {"ok": bool(db_ok), "backend": services.db.backend,
               "error": None if db_ok else _redact(str(db_info), settings)[:300]},
        "hardware": hardware,
        "agent": {"available": services.agent is not None, "provider": provider,
                  "model": settings.effective_agent_model if provider == "gemini" else "rules",
                  **(_call_status(services.agent) or {})},
        "tts": {"provider": settings.tts_provider, "elevenlabs": settings.elevenlabs_configured},
        "smtp": {"configured": settings.smtp_configured,
                 "delivery": "smtp" if settings.smtp_configured else "outbox"},
        "time": {"now_local": clock.to_local(now).isoformat(), "now_utc": now.isoformat(),
                 "tz": clock.tz_name, "travelling": bool(clock.is_travelling)},
        "demo_mode": bool(settings.demo_mode),
        "scheduler": loop.status() if loop is not None else {"running": False},
        "voice": {"enabled": bool(settings.voice_enabled), "running": voice_loop is not None,
                  **(_call_status(voice_loop) or {})},
        "degraded": sorted(getattr(services, "startup_errors", {}) or {}),
    }


@router.get("/api/health")
def health(services: ServicesDep) -> dict[str, Any]:
    return health_payload(services)
