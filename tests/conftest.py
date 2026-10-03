"""Shared pytest fixtures. Tests are hermetic: no .env, no real API keys, temp data dir."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from tactidose.config import Settings
from tactidose.core.bus import EventBus
from tactidose.core.clock import Clock
from tactidose.db.session import Database
from tests.fakes import FakeExtractor, FakeHardware, FakeSpeaker

TEST_TZ = "America/Vancouver"
#: Monday 5 Oct 2026, 07:55 local — five minutes before the seeded 08:00 dose.
TEST_NOW_LOCAL = datetime(2026, 10, 5, 7, 55)

_ENV_PREFIXES = (
    "TACTIDOSE_", "TIDB_", "SNOWFLAKE_", "GEMINI_", "ELEVENLABS_", "GOOGLE_API_KEY",
    "DATABASE_URL", "CA_PATH",
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        hardware_mode="none",
        voice_enabled=False,
        tts_provider="none",
        label_extractor="fake",
        timezone=TEST_TZ,
        demo_mode=True,
        hw_boot_wait_s=0,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock(TEST_TZ, frozen_at=TEST_NOW_LOCAL)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def db(settings: Settings):
    database = Database(settings)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def fake_hw() -> FakeHardware:
    return FakeHardware()


@pytest.fixture
def fake_speaker() -> FakeSpeaker:
    return FakeSpeaker()


@pytest.fixture
def fake_extractor() -> FakeExtractor:
    return FakeExtractor()
