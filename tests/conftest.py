"""Shared pytest fixtures. Tests are hermetic: no .env, no real API keys, temp data dir.

* ``settings`` / ``db`` — v1-era fixtures (6 slots) used by the wave-1 module tests.
* ``settings_v2`` / ``db_v2`` — the v2 product: 3 containers, global cooldown, rule-based agent,
  no AI report summary, no SMTP. Seed with ``tests.fakes.seed_v2``.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from tactidose.config import Settings
from tactidose.core.bus import EventBus
from tactidose.core.clock import Clock
from tactidose.db.session import Database
from tests.fakes import FakeDropHardware, FakeExtractor, FakeHardware, FakeSpeaker

TEST_TZ = "America/Vancouver"
#: Monday 5 Oct 2026, 07:55 local — five minutes before the seeded 08:00 dose.
TEST_NOW_LOCAL = datetime(2026, 10, 5, 7, 55)

_ENV_PREFIXES = (
    "TACTIDOSE_", "TIDB_", "SNOWFLAKE_", "GEMINI_", "ELEVENLABS_", "GOOGLE_API_KEY",
    "DATABASE_URL", "CA_PATH", "SMTP_", "AGENT_MODEL",
)
#: Developer tooling variables that must survive the scrub (native firmware harness location).
_ENV_KEEP_PREFIXES = ("TACTIDOSE_NATIVE_HARNESS_", "TACTIDOSE_GCC_IMAGE")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(_ENV_PREFIXES) and not upper.startswith(_ENV_KEEP_PREFIXES):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        hardware_mode="none",
        num_slots=6,
        voice_enabled=False,
        tts_provider="none",
        label_extractor="fake",
        timezone=TEST_TZ,
        demo_mode=True,
        hw_boot_wait_s=0,
    )


@pytest.fixture
def settings_v2(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        hardware_mode="none",
        num_slots=3,
        manual_cooldown_minutes=60,
        auto_drop_enabled=True,
        voice_enabled=False,
        tts_provider="none",
        label_extractor="fake",
        agent_provider="rules",
        report_ai_summary=False,
        timezone=TEST_TZ,
        demo_mode=True,
        hw_boot_wait_s=0,
        session_ttl_hours=12,
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
def db_v2(settings_v2: Settings):
    database = Database(settings_v2)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def fake_hw() -> FakeHardware:
    return FakeHardware()


@pytest.fixture
def fake_drop_hw() -> FakeDropHardware:
    return FakeDropHardware()


@pytest.fixture
def fake_speaker() -> FakeSpeaker:
    return FakeSpeaker()


@pytest.fixture
def fake_extractor() -> FakeExtractor:
    return FakeExtractor()
