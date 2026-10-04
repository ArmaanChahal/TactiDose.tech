"""Composition root: wires settings, storage and the service together."""

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI

from .api.app import create_app
from .api.auth import identity_provider_from_settings
from .config import Settings, load_safety_config
from .service import WellbeingService
from .storage.base import CheckinRepository
from .storage.memory import InMemoryCheckinRepository, InMemorySessionStore
from .storage.sqlite import SQLiteCheckinRepository


def build_service(
    settings: Settings | None = None, *, repository: CheckinRepository | None = None
) -> WellbeingService:
    settings = settings or Settings.from_env()
    if repository is None:
        repository = (
            InMemoryCheckinRepository()
            if settings.db_path == ":memory:"
            else SQLiteCheckinRepository(settings.db_path)
        )
    return WellbeingService(
        repository,
        InMemorySessionStore(),
        safety=load_safety_config(settings.config_file),
        session_ttl=timedelta(seconds=settings.session_ttl_seconds),
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    """Uvicorn factory: ``uvicorn tactidose_wellbeing.bootstrap:build_app --factory``."""
    settings = settings or Settings.from_env()
    return create_app(build_service(settings), identity_provider_from_settings(settings))
