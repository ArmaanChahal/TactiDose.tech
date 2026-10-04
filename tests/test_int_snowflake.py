"""Tests for tactidose.integrations.snowflake and analytics/snowflake_queries.sql.

A fake connector records every connect() kwarg, SQL statement and parameter and emulates
the staging-table + MERGE semantics the sync relies on. No network; the optional live test
runs only when SNOWFLAKE_ACCOUNT (+ credentials) is set in the environment.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from tactidose.config import Settings
from tactidose.core.bus import EventBus
from tactidose.db.models import AnalyticsOutbox, DoseEvent, DoseStatus
from tactidose.db.outbox import (
    KIND_ADHERENCE,
    adherence_payload,
    enqueue,
    enqueue_adherence,
    enqueue_device_event,
)
from tactidose.integrations import snowflake as sf
from tactidose.integrations.snowflake import SnowflakeSync
from tests.conftest import TEST_TZ
from tests.fakes import seed_minimal, wait_until

# Captured at import time: the autouse fixture strips SNOWFLAKE_* before each test runs.
_LIVE_ENV = {k: v for k, v in os.environ.items() if k.upper().startswith("SNOWFLAKE_")}

PASSWORD = "pw-SUPER-secret-42"
TOKEN = "pat-TOKEN-secret-99"
KEY_PWD = "key-PASS-secret-7"
TZ = ZoneInfo(TEST_TZ)


# =========================================================================== fake connector


class FakeSnowflakeError(Exception):
    pass


_INSERT_RE = re.compile(r"INSERT INTO (\w+) \(([^)]*)\) VALUES", re.IGNORECASE)
_TEMP_RE = re.compile(r"CREATE OR REPLACE TEMPORARY TABLE (\w+)", re.IGNORECASE)


class FakeSnowflake:
    """Callable used as ``connect=``; holds the emulated warehouse state."""

    def __init__(self) -> None:
        self.connects: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.many: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.fail_on: str | None = None
        self.fail_message = "simulated Snowflake failure"
        self.connect_error: Exception | None = None
        self.adherence: dict[str, dict[str, Any]] = {}
        self.device_events: dict[str, dict[str, Any]] = {}
        self.stages: dict[str, list[dict[str, Any]]] = {}
        self.results: list[tuple[str, list[str], list[tuple[Any, ...]]]] = []
        self.connections_closed = 0
        self.cursors_closed = 0

    def __call__(self, **kwargs: Any) -> "FakeConnection":
        self.connects.append(kwargs)
        if self.connect_error is not None:
            raise self.connect_error
        return FakeConnection(self)

    def staged(self, table: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sql, params in self.many:
            m = _INSERT_RE.match(sql)
            if m and m.group(1) == table:
                cols = [c.strip() for c in m.group(2).split(",")]
                rows.extend(dict(zip(cols, p)) for p in params)
        return rows

    def executed(self, prefix: str) -> list[str]:
        return [s for s in self.statements if s.startswith(prefix)]


class FakeConnection:
    def __init__(self, fake: FakeSnowflake) -> None:
        self.fake = fake

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self.fake)

    def close(self) -> None:
        self.fake.connections_closed += 1


class FakeCursor:
    def __init__(self, fake: FakeSnowflake) -> None:
        self.fake = fake
        self.description: list[tuple[Any, ...]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def _maybe_fail(self, sql: str) -> None:
        if self.fake.fail_on and self.fake.fail_on in sql:
            raise FakeSnowflakeError(self.fake.fail_message)

    def execute(self, sql: str, params: Any = None) -> "FakeCursor":
        assert params is None, "statements are expected to be parameter-free"
        self.fake.statements.append(sql)
        self._maybe_fail(sql)
        self.description, self._rows = None, []
        m = _TEMP_RE.match(sql)
        if m:
            self.fake.stages[m.group(1)] = []
        elif sql.startswith(f"MERGE INTO {sf.ADHERENCE_TABLE}"):
            self._merge_adherence()
        elif sql.startswith(f"MERGE INTO {sf.DEVICE_EVENTS_TABLE}"):
            self._merge_device_events()
        else:
            for marker, columns, rows in self.fake.results:
                if marker in sql:
                    self.description = [(c, None) for c in columns]
                    self._rows = list(rows)
                    break
            else:
                code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
                if code.lstrip().upper().startswith(("WITH", "SELECT")):
                    self.description, self._rows = [("N", None)], []
        return self

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> "FakeCursor":
        self.fake.many.append((sql, list(seq)))
        self._maybe_fail(sql)
        m = _INSERT_RE.match(sql)
        assert m, sql
        cols = [c.strip() for c in m.group(2).split(",")]
        assert all(len(p) == len(cols) for p in seq)
        self.fake.stages.setdefault(m.group(1), []).extend(dict(zip(cols, p)) for p in seq)
        return self

    def _merge_adherence(self) -> None:
        best: dict[str, tuple[tuple[datetime, int], dict[str, Any]]] = {}
        for r in self.fake.stages.get(sf.ADHERENCE_STAGE, []):
            key = (r["RECORDED_AT"] or datetime.min, r["SOURCE_SEQ"])
            if r["EVENT_UID"] not in best or key > best[r["EVENT_UID"]][0]:
                best[r["EVENT_UID"]] = (key, r)
        for uid, (_, r) in best.items():
            row = {c.name: r[c.name] for c in sf.ADHERENCE_COLUMNS}
            target = self.fake.adherence.get(uid)
            if target is None or target["RECORDED_AT"] is None or row["RECORDED_AT"] >= target["RECORDED_AT"]:
                self.fake.adherence[uid] = row

    def _merge_device_events(self) -> None:
        for r in self.fake.stages.get(sf.DEVICE_EVENTS_STAGE, []):
            self.fake.device_events.setdefault(r["EVENT_KEY"], dict(r))

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        return self._rows[:n]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.fake.cursors_closed += 1


# =========================================================================== helpers


@pytest.fixture
def sf_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={
        "snowflake_account": "acme-test",
        "snowflake_user": "TACTIDOSE_SVC",
        "snowflake_password": SecretStr(PASSWORD),
        "snowflake_warehouse": "COMPUTE_WH",
        "snowflake_role": "TACTIDOSE_ROLE",
        "analytics_sync_interval_s": 30.0,
    })


@pytest.fixture
def fake() -> FakeSnowflake:
    return FakeSnowflake()


@pytest.fixture
def ids(db, settings) -> dict[str, Any]:
    return seed_minimal(db, settings)


def make_sync(db, sf_settings, clock, fake, **kw: Any) -> SnowflakeSync:
    return SnowflakeSync(db, sf_settings, clock, connect=fake, **kw)


def add_event(db, ids, at: datetime, *, sched: int = 0) -> int:
    with db.session() as s:
        ev = DoseEvent(schedule_id=ids["schedule_ids"][sched], medication_id=ids["med_ids"][0],
                       user_id=ids["user_id"], device_id=ids["device_id"], scheduled_at=at,
                       status=DoseStatus.DUE.value)
        s.add(ev)
        s.flush()
        return ev.event_id


def record_state(db, settings, event_id: int, status: DoseStatus, recorded_at: datetime, **fields: Any) -> None:
    with db.session() as s:
        ev = s.get(DoseEvent, event_id)
        ev.status = status.value
        for k, v in fields.items():
            setattr(ev, k, v)
        enqueue_adherence(s, ev, salt=settings.analytics_salt.get_secret_value(), tz=TZ,
                          recorded_at=recorded_at)


def outbox_rows(db) -> list[AnalyticsOutbox]:
    with db.session() as s:
        return list(s.scalars(select(AnalyticsOutbox).order_by(AnalyticsOutbox.outbox_id)))


def naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# =========================================================================== connection kwargs


def test_connection_kwargs_password(db, sf_settings, clock, fake):
    kw = make_sync(db, sf_settings, clock, fake).connection_kwargs()
    assert kw == {
        "account": "acme-test", "user": "TACTIDOSE_SVC", "warehouse": "COMPUTE_WH",
        "database": "TACTIDOSE", "schema": "ANALYTICS", "role": "TACTIDOSE_ROLE",
        "login_timeout": 15, "network_timeout": 30, "application": "TactiDose",
        "paramstyle": "pyformat", "password": PASSWORD,
    }
    assert sf.auth_mode(sf_settings) == "password"


def test_auth_token_wins(sf_settings):
    s = sf_settings.model_copy(update={"snowflake_token": SecretStr(TOKEN),
                                       "snowflake_private_key_file": "C:/keys/rsa_key.p8"})
    assert sf.snowflake_auth_kwargs(s) == {"authenticator": "PROGRAMMATIC_ACCESS_TOKEN", "token": TOKEN}
    assert sf.auth_mode(s) == "pat"


def test_auth_key_pair(sf_settings):
    s = sf_settings.model_copy(update={"snowflake_password": None,
                                       "snowflake_private_key_file": "C:/keys/rsa_key.p8",
                                       "snowflake_private_key_file_pwd": SecretStr(KEY_PWD)})
    assert sf.snowflake_auth_kwargs(s) == {"authenticator": "SNOWFLAKE_JWT",
                                           "private_key_file": "C:/keys/rsa_key.p8",
                                           "private_key_file_pwd": KEY_PWD}
    no_pwd = s.model_copy(update={"snowflake_private_key_file_pwd": None})
    assert sf.snowflake_auth_kwargs(no_pwd) == {"authenticator": "SNOWFLAKE_JWT",
                                                "private_key_file": "C:/keys/rsa_key.p8"}
    assert sf.auth_mode(s) == "key_pair"


@pytest.mark.parametrize("authenticator,extra,expected", [
    ("username_password_mfa", {}, {"authenticator": "username_password_mfa", "password": PASSWORD}),
    ("SNOWFLAKE_JWT", {"snowflake_token": SecretStr(TOKEN)},
     {"authenticator": "SNOWFLAKE_JWT", "private_key_file": "C:/k.p8"}),
    ("PROGRAMMATIC_ACCESS_TOKEN", {"snowflake_token": SecretStr(TOKEN)},
     {"authenticator": "PROGRAMMATIC_ACCESS_TOKEN", "token": TOKEN}),
    ("oauth", {"snowflake_token": SecretStr(TOKEN)}, {"authenticator": "oauth", "token": TOKEN}),
    ("snowflake", {"snowflake_password": None, "snowflake_token": SecretStr(TOKEN)},
     {"authenticator": "snowflake", "password": TOKEN}),      # a PAT works as a password
    ("externalbrowser", {"snowflake_password": None}, {"authenticator": "externalbrowser"}),
])
def test_explicit_authenticator_overrides(sf_settings, authenticator, extra, expected):
    s = sf_settings.model_copy(update={"snowflake_authenticator": authenticator,
                                       "snowflake_private_key_file": "C:/k.p8", **extra})
    assert sf.snowflake_auth_kwargs(s) == expected
    assert sf.auth_mode(s) == f"explicit:{authenticator.upper()}"


def test_describe_config_has_no_secrets(sf_settings):
    s = sf_settings.model_copy(update={"snowflake_token": SecretStr(TOKEN)})
    d = sf.describe_config(s)
    assert d["configured"] and d["auth"] == "pat" and d["account"] == "acme-test"
    assert PASSWORD not in repr(d) and TOKEN not in repr(d) and "TACTIDOSE_SVC" not in repr(d)


# =========================================================================== schema / SQL shape


def test_columns_match_outbox_payloads():
    ev = DoseEvent(event_id=1, schedule_id=1, medication_id=1, user_id=1, device_id="d",
                   scheduled_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc), status="DUE", attempts=0)
    assert [c.key for c in sf.ADHERENCE_COLUMNS] == list(adherence_payload(ev, salt="s"))
    row = enqueue_device_event(SimpleNamespace(add=lambda r: None), device_id="d", event_type="FAULT")
    assert {c.key for c in sf.DEVICE_EVENT_COLUMNS} - {"event_key"} == set(row.payload)
    for c in sf.ADHERENCE_COLUMNS + sf.DEVICE_EVENT_COLUMNS:
        assert c.name == c.key.upper()
        if c.key.endswith("_at"):
            assert c.sql_type == "TIMESTAMP_NTZ" and c.kind == "ts"


def test_ddl_and_merge_shape():
    ddl = sf.adherence_table_ddl()
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS ADHERENCE_EVENTS (")
    for c in sf.ADHERENCE_COLUMNS:
        assert f"    {c.name} {c.sql_type}" in ddl
    assert "SYNCED_AT TIMESTAMP_NTZ" in ddl and "PRIMARY KEY (EVENT_UID)" in ddl
    assert "DETAIL VARIANT" in sf.device_events_table_ddl()
    stage = sf.adherence_stage_ddl()
    assert stage.startswith("CREATE OR REPLACE TEMPORARY TABLE TD_STAGE_ADHERENCE")
    assert "SOURCE_SEQ INTEGER" in stage
    assert "DETAIL_JSON VARCHAR" in sf.device_events_stage_ddl()

    merge = sf.adherence_merge_sql()
    assert merge.startswith("MERGE INTO ADHERENCE_EVENTS AS t\nUSING (")
    assert "FROM TD_STAGE_ADHERENCE" in merge
    assert "QUALIFY ROW_NUMBER() OVER (" in merge
    assert "PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SOURCE_SEQ DESC) = 1" in merge
    assert "ON t.EVENT_UID = s.EVENT_UID" in merge
    assert ("WHEN MATCHED AND (t.RECORDED_AT IS NULL OR s.RECORDED_AT >= t.RECORDED_AT) "
            "THEN UPDATE SET") in merge
    assert "EVENT_UID = s.EVENT_UID," not in merge          # the key is never updated
    insert_cols = re.search(r"WHEN NOT MATCHED THEN INSERT \(([^)]*)\)", merge).group(1).split(", ")
    assert insert_cols == [c.name for c in sf.ADHERENCE_COLUMNS] + ["SYNCED_AT"]

    dmerge = sf.device_events_merge_sql()
    assert "ON t.EVENT_KEY = s.EVENT_KEY" in dmerge
    assert "TRY_PARSE_JSON(DETAIL_JSON) AS DETAIL" in dmerge
    assert "WHEN MATCHED" not in dmerge                      # device events are insert-only

    insert = sf.adherence_stage_insert_sql()
    assert insert.startswith("INSERT INTO TD_STAGE_ADHERENCE (EVENT_UID, ")
    assert insert.count("%s") == len(sf.ADHERENCE_COLUMNS) + 1


def test_ensure_schema_statements_and_permission_errors(db, sf_settings, clock, fake):
    sync = make_sync(db, sf_settings, clock, fake)
    assert sync.ensure_schema() == {"ok": True, "error": None}
    assert [s.split(" (")[0] for s in fake.statements] == [
        "CREATE DATABASE IF NOT EXISTS TACTIDOSE", "USE DATABASE TACTIDOSE",
        "CREATE SCHEMA IF NOT EXISTS ANALYTICS", "USE SCHEMA ANALYTICS",
        "CREATE TABLE IF NOT EXISTS ADHERENCE_EVENTS", "CREATE TABLE IF NOT EXISTS DEVICE_EVENTS",
    ]
    assert fake.connections_closed == 1 and fake.cursors_closed == 1

    fake.statements.clear()
    fake.fail_on, fake.fail_message = "CREATE DATABASE", "Insufficient privileges to operate on account"
    assert sync.ensure_schema()["ok"] is True                 # permission error ignored
    assert len(fake.statements) == 6
    fake.fail_on = "CREATE SCHEMA"
    assert sync.ensure_schema()["ok"] is True
    fake.fail_on, fake.fail_message = "CREATE TABLE IF NOT EXISTS ADHERENCE_EVENTS", "no CREATE TABLE privilege"
    res = sync.ensure_schema()
    assert res == {"ok": False, "error": "FakeSnowflakeError: no CREATE TABLE privilege"}


def test_identifiers_are_quoted_when_needed(db, sf_settings, clock, fake):
    s = sf_settings.model_copy(update={"snowflake_database": "my-db", "snowflake_schema": 'we"ird'})
    make_sync(db, s, clock, fake).ensure_schema()
    assert 'CREATE DATABASE IF NOT EXISTS "my-db"' in fake.statements
    assert 'USE SCHEMA "we""ird"' in fake.statements


def test_not_configured_is_inert(db, settings, clock, fake):
    sync = SnowflakeSync(db, settings, clock, connect=fake)
    assert not sync.configured
    assert sync.sync_once()["error"] == "not_configured"
    sync.start()
    assert not sync.running
    sync.close()
    rep = sync.report()
    assert rep == {"configured": False, "queries": [], "error": "Snowflake is not configured"}
    assert sync.ensure_schema() == {"ok": False, "error": "not_configured"}
    assert fake.connects == []
    assert sync.status()["configured"] is False


# =========================================================================== sync behaviour


def test_sync_merges_and_marks_sent(db, settings, sf_settings, clock, fake, ids):
    bus = EventBus()
    sub = bus.subscribe([sf.TOPIC_SYNC])
    at = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    eid = add_event(db, ids, at)
    t0 = clock.now()
    record_state(db, settings, eid, DoseStatus.DISPENSED, t0, dispensed_at=at + timedelta(minutes=3), attempts=1)
    record_state(db, settings, eid, DoseStatus.TAKEN, t0 + timedelta(minutes=2),
                 confirmed_taken_at=at + timedelta(minutes=5))
    with db.session() as s:
        enqueue_device_event(s, device_id=ids["device_id"], event_type="FAULT", code="MOTOR_FAULT",
                             at=t0, detail={"state": "FAULT"})

    sync = SnowflakeSync(db, sf_settings, clock, bus=bus, connect=fake)
    report = sync.sync_once()
    assert report["ok"] and report["error"] is None
    assert (report["selected"], report["adherence"], report["device_events"], report["superseded"],
            report["sent"]) == (3, 1, 1, 1, 3)

    assert fake.connects == [sync.connection_kwargs()]
    assert [s.split("\n")[0].split(" (")[0] for s in fake.statements] == [
        "CREATE DATABASE IF NOT EXISTS TACTIDOSE", "USE DATABASE TACTIDOSE",
        "CREATE SCHEMA IF NOT EXISTS ANALYTICS", "USE SCHEMA ANALYTICS",
        "CREATE TABLE IF NOT EXISTS ADHERENCE_EVENTS", "CREATE TABLE IF NOT EXISTS DEVICE_EVENTS",
        "CREATE OR REPLACE TEMPORARY TABLE TD_STAGE_ADHERENCE",
        "MERGE INTO ADHERENCE_EVENTS AS t",
        "CREATE OR REPLACE TEMPORARY TABLE TD_STAGE_DEVICE_EVENTS",
        "MERGE INTO DEVICE_EVENTS AS t",
    ]
    assert [sql for sql, _ in fake.many] == [sf.adherence_stage_insert_sql(), sf.device_events_stage_insert_sql()]
    assert fake.connections_closed == 1 and fake.cursors_closed == 1

    (staged,) = fake.staged(sf.ADHERENCE_STAGE)            # collapsed to the latest payload
    uid = f"{ids['device_id']}:{eid}"
    assert staged["EVENT_UID"] == uid and staged["FINAL_STATUS"] == "TAKEN" and staged["TAKEN"] is True
    assert staged["SCHEDULED_AT"] == datetime(2026, 10, 5, 15, 0)          # naive UTC
    assert staged["CONFIRMED_TAKEN_AT"] == datetime(2026, 10, 5, 15, 5)
    assert staged["SCHEDULED_LOCAL_DATE"] == date(2026, 10, 5)
    assert staged["SCHEDULED_LOCAL_HOUR"] == 8 and staged["TIME_WINDOW"] == "morning"
    assert staged["CONFIRM_DELAY_MINUTES"] == 5.0 and staged["ATTEMPTS"] == 1
    assert staged["RECORDED_AT"] == naive_utc(t0 + timedelta(minutes=2))
    assert len(staged["USER_HASH"]) == 16
    (dev,) = fake.staged(sf.DEVICE_EVENTS_STAGE)
    assert dev["CODE"] == "MOTOR_FAULT" and dev["DETAIL_JSON"] == '{"state": "FAULT"}'
    assert dev["EVENT_KEY"].startswith(f"device_event:{ids['device_id']}:FAULT:")

    assert fake.adherence[uid]["FINAL_STATUS"] == "TAKEN" and len(fake.adherence) == 1
    assert all(r.sent_at is not None for r in outbox_rows(db))
    st = sync.status()
    assert st["pending"] == 0 and st["sent"] == 3 and st["last_error"] is None
    assert st["last_sync"] is not None and st["configured"] is True
    (ev,) = sub.drain()
    assert ev.data["ok"] is True and ev.data["sent"] == 3

    # nothing pending: no connection is opened at all
    assert sync.sync_once()["selected"] == 0 and len(fake.connects) == 1


def test_idempotent_resend_and_stale_payloads(db, settings, sf_settings, clock, fake, ids):
    at = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    eid = add_event(db, ids, at)
    t0 = clock.now()
    record_state(db, settings, eid, DoseStatus.DISPENSED, t0)
    sync = make_sync(db, sf_settings, clock, fake)
    assert sync.sync_once()["ok"]
    uid = f"{ids['device_id']}:{eid}"
    assert fake.adherence[uid]["FINAL_STATUS"] == "DISPENSED"

    record_state(db, settings, eid, DoseStatus.TAKEN, t0 + timedelta(minutes=1))
    assert sync.sync_once()["ok"]
    assert fake.adherence[uid]["FINAL_STATUS"] == "TAKEN"

    # a stale snapshot (older recorded_at) arriving later must not overwrite the newer state
    record_state(db, settings, eid, DoseStatus.DISPENSED, t0 - timedelta(minutes=10))
    assert sync.sync_once()["ok"]
    assert fake.adherence[uid]["FINAL_STATUS"] == "TAKEN" and len(fake.adherence) == 1

    # re-sending everything (e.g. marking sent failed earlier) changes nothing
    with db.session() as s:
        for row in s.scalars(select(AnalyticsOutbox)):
            row.sent_at = None
    rep = sync.sync_once()
    assert rep["ok"] and rep["selected"] == 3 and rep["adherence"] == 1 and rep["superseded"] == 2
    assert fake.adherence[uid]["FINAL_STATUS"] == "TAKEN" and len(fake.adherence) == 1


def test_same_recorded_at_later_outbox_row_wins(db, settings, sf_settings, clock, fake, ids):
    eid = add_event(db, ids, datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
    t0 = clock.now()                                   # frozen clock: identical recorded_at
    record_state(db, settings, eid, DoseStatus.DISPENSED, t0)
    record_state(db, settings, eid, DoseStatus.TAKEN, t0)
    make_sync(db, sf_settings, clock, fake).sync_once()
    (staged,) = fake.staged(sf.ADHERENCE_STAGE)
    assert staged["FINAL_STATUS"] == "TAKEN"


def test_failure_keeps_rows_pending_and_backs_off(db, settings, sf_settings, clock, fake, ids):
    eid = add_event(db, ids, datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
    record_state(db, settings, eid, DoseStatus.MISSED, clock.now())
    fake.fail_on = "MERGE INTO ADHERENCE_EVENTS"
    fake.fail_message = "Warehouse 'COMPUTE_WH' is suspended " + "x" * 2000
    sync = make_sync(db, sf_settings, clock, fake)

    r1 = sync.sync_once()
    assert r1["ok"] is False and r1["sent"] == 0
    assert r1["error"].startswith("FakeSnowflakeError: Warehouse 'COMPUTE_WH' is suspended")
    assert len(r1["error"]) <= sf.LAST_ERROR_CHARS
    (row,) = outbox_rows(db)
    assert row.sent_at is None and row.attempts == 1
    assert row.last_error and len(row.last_error) <= sf.LAST_ERROR_CHARS
    st = sync.status()
    assert st["pending"] == 1 and st["sent"] == 0 and st["last_error"] == r1["error"]
    assert st["consecutive_failures"] == 1 and st["last_sync"] is None

    assert sync.sync_once()["ok"] is False
    (row,) = outbox_rows(db)
    assert row.attempts == 2 and sync.status()["consecutive_failures"] == 2
    ddl_runs = len(fake.executed("CREATE TABLE IF NOT EXISTS ADHERENCE_EVENTS"))
    assert ddl_runs == 2                     # schema re-checked after a failure

    fake.fail_on = None
    ok = sync.sync_once()
    assert ok["ok"] and ok["sent"] == 1
    (row,) = outbox_rows(db)
    assert row.sent_at is not None and row.attempts == 2
    st = sync.status()
    assert st["consecutive_failures"] == 0 and st["last_error"] is None and st["last_sync"]


def test_backoff_schedule(db, sf_settings, clock, fake):
    sync = make_sync(db, sf_settings, clock, fake)
    assert [sync.backoff_delay(n) for n in range(0, 8)] == [30, 30, 60, 120, 240, 480, 600, 600]
    assert sync.backoff_delay(10_000) == sf.MAX_BACKOFF_S == 600
    slow = make_sync(db, sf_settings.model_copy(update={"analytics_sync_interval_s": 3600.0}), clock, fake)
    assert slow.backoff_delay(3) == 3600      # never shorter than the normal interval


def test_connect_failure_is_redacted(db, settings, sf_settings, clock, fake, ids):
    eid = add_event(db, ids, datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
    record_state(db, settings, eid, DoseStatus.MISSED, clock.now())
    fake.connect_error = FakeSnowflakeError(f"250001: Could not connect (password={PASSWORD})")
    sync = make_sync(db, sf_settings, clock, fake)
    rep = sync.sync_once()
    assert rep["ok"] is False and PASSWORD not in rep["error"] and "***" in rep["error"]
    (row,) = outbox_rows(db)
    assert row.sent_at is None and PASSWORD not in row.last_error
    assert PASSWORD not in repr(sync.status())


def test_local_db_failure_is_reported(sf_settings, clock, fake):
    class BrokenDb:
        def session(self):
            raise RuntimeError("database is locked")

    sync = SnowflakeSync(BrokenDb(), sf_settings, clock, connect=fake)   # type: ignore[arg-type]
    rep = sync.sync_once()
    assert rep["ok"] is False and rep["error"].startswith("local db: RuntimeError")
    st = sync.status()
    assert st["pending"] is None and "database is locked" in st["last_error"]
    assert fake.connects == []


def test_batch_limit_and_order(db, sf_settings, clock, fake):
    with db.session() as s:
        for i in range(600):
            enqueue(s, KIND_ADHERENCE, f"adherence:dev:{i}", {"event_uid": f"dev:{i}", "device_id": "dev",
                                                             "final_status": "MISSED"})
        enqueue(s, "something_else", "x:1", {"a": 1})          # unsupported kind: ignored
    sync = make_sync(db, sf_settings, clock, fake)
    first = sync.sync_once()
    assert first["selected"] == 500 and first["sent"] == 500
    assert [r["EVENT_UID"] for r in fake.staged(sf.ADHERENCE_STAGE)][:3] == ["dev:0", "dev:1", "dev:2"]
    assert sync.status()["pending"] == 100
    second = sync.sync_once()
    assert second["selected"] == 100 and sync.status()["pending"] == 0
    assert len(fake.adherence) == 600
    other = [r for r in outbox_rows(db) if r.kind == "something_else"]
    assert other[0].sent_at is None


def test_malformed_payloads_still_sync(db, sf_settings, clock, fake):
    with db.session() as s:
        row = enqueue(s, KIND_ADHERENCE, "adherence:dev:77", {
            "scheduled_at": "not a date", "attempts": "three", "missed": "yes",
            "delay_minutes": float("nan"), "hardware_result": "E" * 1000, "slot_number": True})
        created = row
    rep = make_sync(db, sf_settings, clock, fake).sync_once()
    assert rep["ok"]
    (staged,) = fake.staged(sf.ADHERENCE_STAGE)
    assert staged["EVENT_UID"] == "dev:77"                      # derived from the dedupe key
    assert staged["SCHEDULED_AT"] is None and staged["ATTEMPTS"] is None
    assert staged["MISSED"] is True and staged["DELAY_MINUTES"] is None
    assert len(staged["HARDWARE_RESULT"]) == 255 and staged["SLOT_NUMBER"] == 1
    assert staged["RECORDED_AT"] == naive_utc(created.created_at)   # falls back to created_at


def test_prune_only_old_sent_rows(db, sf_settings, clock, fake):
    real_now = datetime.now(timezone.utc)
    with db.session() as s:
        for key, created, sent in [
            ("old-sent", real_now - timedelta(days=10), real_now - timedelta(days=9)),
            ("new-sent", real_now - timedelta(days=1), real_now - timedelta(hours=1)),
            ("old-pending", real_now - timedelta(days=10), None),
        ]:
            s.add(AnalyticsOutbox(kind=KIND_ADHERENCE, dedupe_key=key, payload={}, created_at=created, sent_at=sent))
    sync = make_sync(db, sf_settings, clock, fake)
    assert sync.prune() == 1
    assert sorted(r.dedupe_key for r in outbox_rows(db)) == ["new-sent", "old-pending"]
    assert sync.prune() == 0


def test_worker_thread_lifecycle(db, settings, sf_settings, clock, fake, ids):
    eid = add_event(db, ids, datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
    record_state(db, settings, eid, DoseStatus.TAKEN, clock.now())
    sync = make_sync(db, sf_settings, clock, fake, initial_delay_s=0.01)
    sync.start()
    sync.start()                                                # idempotent
    names = [t.name for t in threading.enumerate() if t.name == "analytics-sync"]
    assert names == ["analytics-sync"] and sync.running
    assert wait_until(lambda: sync.status()["pending"] == 0, timeout=5)
    assert len(fake.connects) == 1
    sync.close()
    sync.close()
    assert not sync.running
    assert not [t for t in threading.enumerate() if t.name == "analytics-sync"]


# =========================================================================== named queries / report


SAMPLE_SQL = """\
-- header text that is ignored
-- name: first_query
-- description: The first query.
-- description: Continued description.
-- an explanatory comment that stays with the SQL
SELECT 1 AS A FROM T1;

-- name: second_query
-- description: Second.
WITH x AS (SELECT 2 AS B FROM T2)
SELECT * FROM x
;
-- name: empty_block
-- description: Only comments, skipped.
-- nothing here
-- name: failing_query
SELECT * FROM T3
"""


def test_parse_named_queries():
    qs = sf.parse_named_queries(SAMPLE_SQL)
    assert [q.name for q in qs] == ["first_query", "second_query", "failing_query"]
    assert qs[0].description == "The first query. Continued description."
    assert qs[0].sql == "-- an explanatory comment that stays with the SQL\nSELECT 1 AS A FROM T1"
    assert qs[1].sql == "WITH x AS (SELECT 2 AS B FROM T2)\nSELECT * FROM x"
    assert qs[2].description == ""


def test_shipped_queries_file():
    path = sf.default_queries_path()
    assert path is not None and path.name == "snowflake_queries.sql"
    qs = sf.load_named_queries()
    names = [q.name for q in qs]
    assert names == ["adherence_overview", "missed_by_time_window", "average_delays",
                     "adherence_trend_daily", "device_errors_by_code", "device_errors_by_day",
                     "hardware_error_rate_by_device"]
    assert len(set(names)) == len(names)
    for q in qs:
        assert q.description and len(q.description) > 20, q.name
        assert "QUALIFY ROW_NUMBER() OVER" in q.sql, q.name
        assert not q.sql.rstrip().endswith(";")
        assert ("ADHERENCE_EVENTS" in q.sql) or ("DEVICE_EVENTS" in q.sql)
        assert "%s" not in q.sql and "%(" not in q.sql    # executed without parameters
    trend = next(q for q in qs if q.name == "adherence_trend_daily")
    assert "GENERATOR(ROWCOUNT => 30)" in trend.sql


def test_report_runs_named_queries(db, sf_settings, clock, fake, tmp_path):
    qfile = tmp_path / "queries.sql"
    qfile.write_text(SAMPLE_SQL, encoding="utf-8")
    fake.results = [
        ("FROM T1", ["A", "WHEN_UTC"], [(Decimal("2.50"), datetime(2026, 10, 5, 15, 0))]),
        ("FROM T2", ["B", "DAY", "N"], [(Decimal("3"), date(2026, 10, 4), None),
                                         (Decimal("NaN"), date(2026, 10, 5), float("inf"))]),
    ]
    fake.fail_on, fake.fail_message = "FROM T3", f"SQL compilation error (pw {PASSWORD})"
    sync = make_sync(db, sf_settings, clock, fake, queries_path=qfile)
    rep = sync.report()
    assert rep["configured"] is True and rep["error"] is None
    q1, q2, q3 = rep["queries"]
    assert q1 == {"name": "first_query", "description": "The first query. Continued description.",
                  "columns": ["A", "WHEN_UTC"], "rows": [[2.5, "2026-10-05T15:00:00"]], "error": None}
    assert q2["rows"] == [[3, "2026-10-04", None], [None, "2026-10-05", None]]
    assert q3["rows"] == [] and "SQL compilation error" in q3["error"] and PASSWORD not in q3["error"]
    assert fake.executed("CREATE TABLE IF NOT EXISTS ADHERENCE_EVENTS")      # schema ensured first
    assert fake.connections_closed == 1


def test_report_with_shipped_queries_and_errors(db, sf_settings, clock, fake, tmp_path):
    sync = make_sync(db, sf_settings, clock, fake)
    rep = sync.report()
    assert rep["error"] is None and len(rep["queries"]) == 7
    assert all(q["error"] is None and q["columns"] == ["N"] for q in rep["queries"])
    fake.connect_error = FakeSnowflakeError(f"login failed for token {PASSWORD}")
    bad = sync.report()
    assert bad["queries"] == [] and PASSWORD not in bad["error"]
    missing = make_sync(db, sf_settings, clock, fake, queries_path=tmp_path / "nope.sql").report()
    assert missing["error"].startswith("could not load analytics queries")


def test_generated_and_shipped_sql_parses_as_snowflake():
    sqlglot = pytest.importorskip("sqlglot")
    statements = [
        sf.adherence_table_ddl(), sf.device_events_table_ddl(), sf.adherence_stage_ddl(),
        sf.device_events_stage_ddl(), sf.adherence_merge_sql(), sf.device_events_merge_sql(),
        sf.adherence_stage_insert_sql().replace("%s", "NULL"),
        sf.device_events_stage_insert_sql().replace("%s", "NULL"),
    ] + [q.sql for q in sf.load_named_queries()]
    for sql in statements:
        assert len(sqlglot.parse(sql, read="snowflake")) == 1


# =========================================================================== live (optional)


def _live_settings(base: Settings) -> Settings:
    env = _LIVE_ENV
    secret = lambda name: SecretStr(env[name]) if env.get(name) else None  # noqa: E731
    return base.model_copy(update={
        "snowflake_account": env.get("SNOWFLAKE_ACCOUNT"),
        "snowflake_user": env.get("SNOWFLAKE_USER"),
        "snowflake_password": secret("SNOWFLAKE_PASSWORD"),
        "snowflake_token": secret("SNOWFLAKE_TOKEN") or secret("SNOWFLAKE_PAT"),
        "snowflake_private_key_file": env.get("SNOWFLAKE_PRIVATE_KEY_FILE"),
        "snowflake_private_key_file_pwd": secret("SNOWFLAKE_PRIVATE_KEY_FILE_PWD"),
        "snowflake_authenticator": env.get("SNOWFLAKE_AUTHENTICATOR"),
        "snowflake_warehouse": env.get("SNOWFLAKE_WAREHOUSE"),
        "snowflake_database": env.get("SNOWFLAKE_DATABASE", "TACTIDOSE"),
        "snowflake_schema": env.get("SNOWFLAKE_TEST_SCHEMA", "TACTIDOSE_PYTEST"),
        "snowflake_role": env.get("SNOWFLAKE_ROLE"),
        "device_id": f"pytest-{uuid.uuid4().hex[:8]}",
    })


@pytest.mark.skipif(not _LIVE_ENV.get("SNOWFLAKE_ACCOUNT"),
                    reason="live Snowflake test: set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and a credential")
@pytest.mark.timeout(300)
def test_live_snowflake_roundtrip(tmp_path, clock):
    from tactidose.db.session import Database

    base = Settings(_env_file=None, data_dir=tmp_path / "live", timezone=TEST_TZ)
    live = _live_settings(base)
    assert live.snowflake_configured, "SNOWFLAKE_USER and a password/token/key file are required"
    db = Database(live)
    db.create_all()
    try:
        ids = seed_minimal(db, live)
        eid = add_event(db, ids, datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
        record_state(db, live, eid, DoseStatus.DISPENSED, clock.now())
        record_state(db, live, eid, DoseStatus.TAKEN, clock.now() + timedelta(minutes=1))
        sync = SnowflakeSync(db, live, clock)
        assert sync.ensure_schema()["ok"]
        first = sync.sync_once()
        assert first["ok"], first
        record_state(db, live, eid, DoseStatus.TAKEN, clock.now() + timedelta(minutes=2))
        assert sync.sync_once()["ok"]
        report = sync.report()
        assert report["error"] is None, report
        assert all(q["error"] is None for q in report["queries"]), report
        import snowflake.connector

        conn = snowflake.connector.connect(**sync.connection_kwargs())
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*), MAX(FINAL_STATUS) FROM ADHERENCE_EVENTS WHERE DEVICE_ID = '{live.device_id}'")
            assert cur.fetchone() == (1, "TAKEN")
            cur.execute(f"DELETE FROM ADHERENCE_EVENTS WHERE DEVICE_ID = '{live.device_id}'")
        finally:
            conn.close()
    finally:
        db.dispose()
