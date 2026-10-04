"""Shared builders and fixtures for the medication-domain tests (``test_med_*``), v2.

Not a test module by itself (it defines no tests); other ``test_med_*`` files import the
fixtures from here. Everything runs on the frozen test clock (Mon 5 Oct 2026, 07:55
America/Vancouver) against a temp SQLite DB (copied from a session template for speed) and
``FakeDropHardware`` (3 containers, 20 pills each, drop sensor, protocol 1.1).

Seed (``tests.fakes.seed_v2``, created one hour before "now", so no past doses exist):
patient Alex (+ family Sam, doctor Lee, both linked), device bound to Alex, containers
0/1/2 = Vitamin C / Calcium / Omega-3 with 20 pills each (capacity 30, low-stock 3), daily
schedules 08:00 (slot 0), 13:00 (slot 1), 20:00 (slot 2). The ``env`` fixture has also run one
scheduler tick at 07:55: today's 08:00 dose is DUE, the others SCHEDULED.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Subscription
from tactidose.core.clock import Clock
from tactidose.db.models import (
    AnalyticsOutbox,
    Compartment,
    Device,
    DeviceLog,
    DoseEvent,
    DoseStatus,
    Medication,
    Notification,
    PillDrop,
    Schedule,
)
from tactidose.db.outbox import KIND_ADHERENCE, KIND_DEVICE_EVENT
from tactidose.db.session import Database
from tactidose.medication.catalog import MedicationCatalog
from tactidose.medication.compartments import CompartmentService
from tactidose.medication.drops import DropService
from tactidose.medication.notifications import NotificationService
from tactidose.medication.scheduler import Scheduler
from tests.conftest import _ENV_PREFIXES, TEST_NOW_LOCAL, TEST_TZ
from tests.fakes import FakeDropHardware, seed_v2

__all__ = [
    "CANCELLED", "DISPENSED", "DISPENSING", "DUE", "Env", "FlakyDB", "HARDWARE_ERROR", "KIND_ADHERENCE",
    "KIND_DEVICE_EVENT", "MISSED", "SCHEDULED", "TAKEN", "background", "build", "env", "env_template",
    "env_template_unticked", "env_unticked", "flaky", "from_template",
]

DUE = DoseStatus.DUE.value
SCHEDULED = DoseStatus.SCHEDULED.value
DISPENSING = DoseStatus.DISPENSING.value
DISPENSED = DoseStatus.DISPENSED.value
TAKEN = DoseStatus.TAKEN.value
MISSED = DoseStatus.MISSED.value
CANCELLED = DoseStatus.CANCELLED.value
HARDWARE_ERROR = DoseStatus.HARDWARE_ERROR.value


def v2_settings(data_dir: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        _env_file=None, data_dir=data_dir, hardware_mode="none", num_slots=3, manual_cooldown_minutes=60,
        auto_drop_enabled=True, voice_enabled=False, tts_provider="none", label_extractor="fake",
        agent_provider="rules", report_ai_summary=False, timezone=TEST_TZ, demo_mode=True, hw_boot_wait_s=0,
    )
    base.update(overrides)
    return Settings(**base)


@dataclass
class Env:
    settings: Settings
    clock: Clock
    db: Database
    bus: EventBus
    hw: FakeDropHardware
    notifications: NotificationService
    compartments: CompartmentService
    catalog: MedicationCatalog
    scheduler: Scheduler
    drops: DropService
    ids: dict[str, Any]

    # ------------------------------------------------------------------ ids
    @property
    def patient(self) -> int:
        return self.ids["patient_id"]

    @property
    def family(self) -> int:
        return self.ids["family_id"]

    @property
    def doctor(self) -> int:
        return self.ids["doctor_id"]

    def med(self, i: int) -> int:
        """Medication in container ``i`` of the seed (0 Vitamin C 08:00, 1 Calcium 13:00, 2 Omega-3 20:00)."""
        return self.ids["med_ids"][i]

    def sched(self, i: int) -> int:
        return self.ids["schedule_ids"][i]

    # ------------------------------------------------------------------ time
    def at(self, hhmm: str, day: date | None = None) -> datetime:
        """Aware UTC for local ``HH:MM`` on ``day`` (default: today, local)."""
        h, m = (int(x) for x in hhmm.split(":"))
        return self.clock.local_to_utc(datetime.combine(day or self.clock.today_local(), time(h, m)))

    def travel(self, hhmm: str, day: date | None = None, *, seconds: int = 0) -> None:
        h, m = (int(x) for x in hhmm.split(":"))
        self.clock.freeze(datetime.combine(day or self.clock.today_local(), time(h, m, seconds)))

    def advance(self, *, minutes: float = 0, seconds: float = 0) -> None:
        self.clock.advance(timedelta(minutes=minutes, seconds=seconds))

    def tick(self) -> int:
        return self.scheduler.tick()

    # ------------------------------------------------------------------ actions
    def manual(self, slot: int | None = 0, **kw: Any):
        kw.setdefault("requested_by_user_id", self.patient)
        return self.drops.request_drop(patient_id=self.patient, source="manual", slot=slot, **kw)

    def agent(self, slot: int | None = 0, **kw: Any):
        return self.drops.request_drop(patient_id=self.patient, source="agent", slot=slot, **kw)

    def scheduled(self, event_id: int):
        return self.drops.request_drop(patient_id=self.patient, source="schedule", dose_event_id=event_id)

    def set_cooldown(self, minutes: int) -> None:
        self.drops.update_settings(self.patient, manual_cooldown_minutes=minutes)

    # ------------------------------------------------------------------ rows
    def drop_rows(self) -> list[PillDrop]:
        with self.db.session() as s:
            return list(s.scalars(select(PillDrop).order_by(PillDrop.drop_id)).all())

    def drop(self, drop_id: int) -> PillDrop:
        with self.db.session() as s:
            row = s.get(PillDrop, drop_id)
            assert row is not None
            return row

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

    def dose_0800(self) -> DoseEvent:
        ev = self.event_at(self.sched(0), "08:00")
        assert ev is not None
        return ev

    def set_event(self, event_id: int, **values: Any) -> None:
        with self.db.session() as s:
            s.execute(update(DoseEvent).where(DoseEvent.event_id == event_id).values(**values))

    def compartment(self, slot: int) -> Compartment:
        with self.db.session() as s:
            return s.scalars(select(Compartment).where(Compartment.slot_number == slot)).one()

    def set_compartment(self, slot: int, **values: Any) -> None:
        with self.db.session() as s:
            s.execute(update(Compartment).where(Compartment.slot_number == slot).values(**values))

    def set_medication(self, medication_id: int, **values: Any) -> None:
        with self.db.session() as s:
            s.execute(update(Medication).where(Medication.medication_id == medication_id).values(**values))

    def device(self) -> Device:
        with self.db.session() as s:
            dev = s.get(Device, self.settings.device_id)
            assert dev is not None
            return dev

    def notes(self, user_id: int | None = None, kind: str | None = None) -> list[Notification]:
        with self.db.session() as s:
            q = select(Notification).order_by(Notification.notification_id)
            if user_id is not None:
                q = q.where(Notification.user_id == user_id)
            if kind is not None:
                q = q.where(Notification.kind == kind)
            return list(s.scalars(q).all())

    def note_kinds(self, user_id: int | None = None) -> list[str]:
        return [n.kind for n in self.notes(user_id)]

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

    def schedule(self, schedule_id: int) -> Schedule:
        with self.db.session() as s:
            sc = s.get(Schedule, schedule_id)
            assert sc is not None
            return sc

    def medication(self, medication_id: int) -> Medication:
        with self.db.session() as s:
            med = s.get(Medication, medication_id)
            assert med is not None
            return med

    def subscribe(self, *topics: str) -> Subscription:
        return self.bus.subscribe(list(topics) or None)

    def drop_commands(self) -> list[str]:
        return [c for c in self.hw.sent if c.startswith("DROP_SLOT")]


def build(
    settings: Settings,
    clock: Clock,
    db: Database,
    bus: EventBus,
    hw: FakeDropHardware,
    *,
    seed: bool = True,
    tick: bool = True,
    ids: dict[str, Any] | None = None,
    with_notifications: bool = True,
    **overrides: Any,
) -> Env:
    """Services wired like ARCHITECTURE §11 (``overrides`` applied to the settings).

    ``ids`` = the DB was copied from an already seeded template: do not seed/tick.
    """
    st = settings.model_copy(update=overrides) if overrides else settings
    if ids is not None:
        seed = tick = False
    else:
        ids = {}
    if seed:
        ids = seed_v2(db, st, now=clock.now() - timedelta(hours=1))
    notifications = NotificationService(db, st, clock, bus=bus)
    notes = notifications if with_notifications else None
    drops = DropService(db, hw, clock, st, notifications=notes, bus=bus)
    drops.db_retry_delays_s = (0.0, 0.0)
    drops.poll_interval_s = 0.01
    drops.busy_wait_s = 0.2
    env = Env(
        settings=st, clock=clock, db=db, bus=bus, hw=hw, notifications=notifications,
        compartments=CompartmentService(db, st, bus=bus, clock=clock),
        catalog=MedicationCatalog(db, st, clock, bus=bus),
        scheduler=Scheduler(db, clock, st, bus=bus, notifications=notes),
        drops=drops, ids=ids,
    )
    if seed and tick:
        env.tick()
    return env


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
        st = v2_settings(root / "data")
        database = Database(st)
        database.create_all()
        e = build(st, Clock(TEST_TZ, frozen_at=TEST_NOW_LOCAL), database, EventBus(), FakeDropHardware(), tick=tick)
        with database.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        database.dispose()
    return st.sqlite_path, dict(e.ids)


@pytest.fixture(scope="session")
def env_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    return _template(tmp_path_factory, "med-v2-ticked", tick=True)


@pytest.fixture(scope="session")
def env_template_unticked(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    return _template(tmp_path_factory, "med-v2-seeded", tick=False)


def from_template(template: tuple[Path, dict[str, Any]], settings: Settings, clock: Clock, bus: EventBus,
                  hw: FakeDropHardware, **overrides: Any) -> Env:
    path, ids = template
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, settings.sqlite_path)
    return build(settings, clock, Database(settings), bus, hw, ids=dict(ids), **overrides)


@pytest.fixture
def env(settings_v2: Settings, clock: Clock, bus: EventBus, fake_drop_hw: FakeDropHardware,
        env_template: tuple[Path, dict[str, Any]]):
    """Seeded and ticked at Mon 07:55 (08:00 dose DUE). Do not combine with the ``db_v2`` fixture."""
    e = from_template(env_template, settings_v2, clock, bus, fake_drop_hw)
    yield e
    e.db.dispose()


@pytest.fixture
def env_unticked(settings_v2: Settings, clock: Clock, bus: EventBus, fake_drop_hw: FakeDropHardware,
                 env_template_unticked: tuple[Path, dict[str, Any]]):
    """Seeded like ``env`` but without dose events (tests add their own)."""
    e = from_template(env_template_unticked, settings_v2, clock, bus, fake_drop_hw)
    yield e
    e.db.dispose()


@pytest.fixture
def flaky(env: Env, monkeypatch: pytest.MonkeyPatch) -> FlakyDB:
    f = FlakyDB(env.db)
    monkeypatch.setattr(env.db, "session", f.session)
    return f


def background(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    """Run ``fn`` on a daemon thread; ``out["value"]`` / ``out["error"]`` after join."""
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["value"] = fn()
        except BaseException as exc:  # pragma: no cover - surfaced by the test
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out
