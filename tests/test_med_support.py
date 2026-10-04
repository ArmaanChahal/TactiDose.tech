"""Shared builders and fixtures for the medication-domain tests (``test_med_*``).

Not a test module by itself (it defines no tests); other ``test_med_*`` files import
the fixtures from here. Everything runs on the frozen test clock (Mon 5 Oct 2026,
07:55 America/Vancouver) against a temp SQLite DB and ``FakeHardware``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Subscription
from tactidose.core.clock import Clock
from tactidose.db.models import (
    AnalyticsOutbox,
    Compartment,
    DeviceLog,
    DoseEvent,
    DoseStatus,
    Medication,
    Schedule,
)
from tactidose.db.outbox import KIND_ADHERENCE, KIND_DEVICE_EVENT
from tactidose.db.session import Database
from tactidose.medication.catalog import MedicationCatalog
from tactidose.medication.compartments import CompartmentService
from tactidose.medication.dispense import DoseService
from tactidose.medication.scheduler import Scheduler
from tests.conftest import _ENV_PREFIXES, TEST_NOW_LOCAL, TEST_TZ
from tests.fakes import FakeHardware, seed_minimal

__all__ = [
    "FlakyDB", "Med", "build", "flaky", "from_template", "med", "med_template", "med_template_unticked",
    "med_unticked", "KIND_ADHERENCE", "KIND_DEVICE_EVENT",
]


@dataclass
class Med:
    settings: Settings
    clock: Clock
    db: Database
    bus: EventBus
    hw: FakeHardware
    compartments: CompartmentService
    catalog: MedicationCatalog
    scheduler: Scheduler
    dose: DoseService
    ids: dict[str, Any]

    # ------------------------------------------------------------------ time
    def at(self, hhmm: str, day: date | None = None) -> datetime:
        """Aware UTC for local ``HH:MM`` on ``day`` (default: today, local)."""
        h, m = (int(x) for x in hhmm.split(":"))
        return self.clock.local_to_utc(datetime.combine(day or self.clock.today_local(), time(h, m)))

    def travel(self, hhmm: str, day: date | None = None, *, seconds: int = 0) -> None:
        h, m = (int(x) for x in hhmm.split(":"))
        self.clock.freeze(datetime.combine(day or self.clock.today_local(), time(h, m, seconds)))

    def tick(self) -> int:
        return self.scheduler.tick()

    # ------------------------------------------------------------------ rows
    def event(self, event_id: int) -> DoseEvent:
        with self.db.session() as s:
            ev = s.get(DoseEvent, event_id)
            assert ev is not None
            return ev

    def events(self) -> list[DoseEvent]:
        with self.db.session() as s:
            return list(s.scalars(select(DoseEvent).order_by(DoseEvent.scheduled_at, DoseEvent.event_id)).all())

    def event_at(self, schedule_id: int, hhmm: str, day: date | None = None) -> DoseEvent | None:
        at = self.at(hhmm, day)
        with self.db.session() as s:
            return s.scalars(select(DoseEvent).where(
                DoseEvent.schedule_id == schedule_id, DoseEvent.scheduled_at == at)).first()

    def set_event(self, event_id: int, **values: Any) -> None:
        with self.db.session() as s:
            s.execute(update(DoseEvent).where(DoseEvent.event_id == event_id).values(**values))

    def outbox(self, kind: str | None = None) -> list[AnalyticsOutbox]:
        with self.db.session() as s:
            q = select(AnalyticsOutbox).order_by(AnalyticsOutbox.outbox_id)
            if kind:
                q = q.where(AnalyticsOutbox.kind == kind)
            return list(s.scalars(q).all())

    def adherence_statuses(self, event_id: int) -> list[str]:
        uid = f"{self.settings.device_id}:{event_id}"
        return [r.payload["final_status"] for r in self.outbox(KIND_ADHERENCE) if r.payload["event_uid"] == uid]

    def devlog(self, event: str | None = None) -> list[DeviceLog]:
        with self.db.session() as s:
            q = select(DeviceLog).order_by(DeviceLog.log_id)
            if event:
                q = q.where(DeviceLog.event == event)
            return list(s.scalars(q).all())

    def compartment(self, slot: int) -> Compartment:
        with self.db.session() as s:
            return s.scalars(select(Compartment).where(Compartment.slot_number == slot)).one()

    def medication(self, medication_id: int) -> Medication:
        with self.db.session() as s:
            med = s.get(Medication, medication_id)
            assert med is not None
            return med

    def schedule(self, schedule_id: int) -> Schedule:
        with self.db.session() as s:
            sc = s.get(Schedule, schedule_id)
            assert sc is not None
            return sc

    def subscribe(self, *topics: str) -> Subscription:
        return self.bus.subscribe(list(topics) or None)

    # ------------------------------------------------------------------ convenience
    @property
    def med1(self) -> int:
        """Vitamin C (slot 2): schedules 08:00 and 20:00."""
        return self.ids["med_ids"][0]

    @property
    def med2(self) -> int:
        """Calcium (slot 4): schedule 13:00."""
        return self.ids["med_ids"][1]

    @property
    def sched_0800(self) -> int:
        return self.ids["schedule_ids"][0]

    @property
    def sched_2000(self) -> int:
        return self.ids["schedule_ids"][1]

    @property
    def sched_1300(self) -> int:
        return self.ids["schedule_ids"][2]

    def due_0800(self) -> DoseEvent:
        ev = self.event_at(self.sched_0800, "08:00")
        assert ev is not None
        return ev


def build(
    settings: Settings,
    clock: Clock,
    db: Database,
    bus: EventBus,
    hw: FakeHardware,
    *,
    seed: bool = True,
    tick: bool = True,
    ids: dict[str, Any] | None = None,
    **overrides: Any,
) -> Med:
    """Services wired like ``app.py`` (with ``overrides`` applied to the settings).

    ``ids`` = the DB was copied from a template that is already seeded: do not seed/tick.
    """
    st = settings.model_copy(update=overrides) if overrides else settings
    if ids is not None:
        seed = tick = False
    else:
        ids = {}
    if seed:
        ids = seed_minimal(db, st, now=clock.now() - timedelta(days=2))
    compartments = CompartmentService(db, st, bus=bus)
    compartments.ensure_device()
    dose = DoseService(db, hw, clock, st, bus=bus)
    dose.db_retry_delays_s = (0.0, 0.0)
    dose.poll_interval_s = 0.01
    m = Med(
        settings=st, clock=clock, db=db, bus=bus, hw=hw, compartments=compartments,
        catalog=MedicationCatalog(db, st, clock, bus=bus),
        scheduler=Scheduler(db, clock, st, bus=bus),
        dose=dose, ids=ids,
    )
    if seed and tick:
        m.tick()
    return m


class FlakyDB:
    """Replaces ``Database.session`` so tests can make the database fail on demand."""

    def __init__(self, db: Database) -> None:
        self._real = db.session
        self.fail = False
        self.calls = 0

    def session(self):  # noqa: ANN201 - context manager
        self.calls += 1
        if self.fail:
            raise OperationalError("SELECT 1", {}, RuntimeError("database is unavailable"))
        return self._real()


def _template(tmp_path_factory: pytest.TempPathFactory, name: str, *, tick: bool) -> tuple[Path, dict[str, Any]]:
    """Build a seeded DB once per session; tests get a file copy (much faster than create_all+seed+tick)."""
    root = tmp_path_factory.mktemp(name)
    with pytest.MonkeyPatch.context() as mp:   # session fixtures run before the autouse env cleaner
        for key in list(os.environ):
            if key.upper().startswith(_ENV_PREFIXES):
                mp.delenv(key, raising=False)
        st = Settings(_env_file=None, data_dir=root / "data", hardware_mode="none", voice_enabled=False,
                      tts_provider="none", label_extractor="fake", timezone=TEST_TZ, demo_mode=True,
                      hw_boot_wait_s=0, num_slots=6)
        database = Database(st)
        database.create_all()
        m = build(st, Clock(TEST_TZ, frozen_at=TEST_NOW_LOCAL), database, EventBus(), FakeHardware(), tick=tick)
        with database.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        database.dispose()
    return st.sqlite_path, dict(m.ids)


@pytest.fixture(scope="session")
def med_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    return _template(tmp_path_factory, "med-ticked", tick=True)


@pytest.fixture(scope="session")
def med_template_unticked(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    return _template(tmp_path_factory, "med-seeded", tick=False)


def from_template(template: tuple[Path, dict[str, Any]], settings: Settings, clock: Clock, bus: EventBus,
                  hw: FakeHardware) -> Med:
    path, ids = template
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, settings.sqlite_path)
    return build(settings, clock, Database(settings), bus, hw, ids=dict(ids))


@pytest.fixture
def med(settings: Settings, clock: Clock, bus: EventBus, fake_hw: FakeHardware,
        med_template: tuple[Path, dict[str, Any]]):
    """Seeded (2 meds in slots 2/4, schedules 08:00/20:00/13:00) and ticked at 07:55.

    Do not combine with the conftest ``db`` fixture (both would own the same file)."""
    m = from_template(med_template, settings, clock, bus, fake_hw)
    yield m
    m.db.dispose()


@pytest.fixture
def med_unticked(settings: Settings, clock: Clock, bus: EventBus, fake_hw: FakeHardware,
                 med_template_unticked: tuple[Path, dict[str, Any]]):
    """Seeded like ``med`` but without any dose events (tests add their own rows)."""
    m = from_template(med_template_unticked, settings, clock, bus, fake_hw)
    yield m
    m.db.dispose()


@pytest.fixture
def flaky(med: Med, monkeypatch: pytest.MonkeyPatch) -> FlakyDB:
    f = FlakyDB(med.db)
    monkeypatch.setattr(med.db, "session", f.session)
    return f


def due_count(m: Med) -> int:
    return len(m.dose.check_due().due)


DUE = DoseStatus.DUE.value
SCHEDULED = DoseStatus.SCHEDULED.value
DISPENSING = DoseStatus.DISPENSING.value
DISPENSED = DoseStatus.DISPENSED.value
TAKEN = DoseStatus.TAKEN.value
MISSED = DoseStatus.MISSED.value
CANCELLED = DoseStatus.CANCELLED.value
HARDWARE_ERROR = DoseStatus.HARDWARE_ERROR.value

