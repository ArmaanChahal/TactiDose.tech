"""Transactional outbox -> Snowflake analytics sync (handoff §20).

Domain code writes de-identified rows into ``analytics_outbox`` in the same transaction
as every dose-state change (``db/outbox.py``). :class:`SnowflakeSync` drains that outbox
on the ``analytics-sync`` thread:

1. Load up to 500 unsent rows (kinds ``adherence`` and ``device_event``) by ``outbox_id``.
2. Collapse adherence rows to the latest payload per ``dedupe_key`` (latest =
   highest ``recorded_at``, ties broken by ``outbox_id``; the same rule the MERGE uses).
3. Load them into session-scoped temporary staging tables (one bulk ``executemany``) and
   ``MERGE`` into ``ADHERENCE_EVENTS`` on ``EVENT_UID`` (a matched row is only updated
   when ``source.RECORDED_AT >= target.RECORDED_AT``) and into ``DEVICE_EVENTS`` on
   ``EVENT_KEY`` (insert-only). Re-sending a batch is therefore harmless.
4. Only after Snowflake succeeded are the outbox rows stamped ``sent_at``. On failure
   they stay pending (``attempts + 1``, truncated ``last_error``) and the worker backs off
   exponentially (``analytics_sync_interval_s`` doubling, capped at 10 minutes).

Nothing on the dispensing path depends on this module: when Snowflake (or the internet)
is unavailable, rows simply wait in the outbox. Every public method is safe to call from
any thread; ``sync_once``/``status``/``report``/``start``/``close`` never raise.

Tables (``ensure_schema``): column names are the upper-cased keys of
``db.outbox.adherence_payload`` (+ ``SYNCED_AT``); every ``*_AT`` column is a
``TIMESTAMP_NTZ`` holding UTC. Named analytics queries live in
``analytics/snowflake_queries.sql`` (blocks introduced by ``-- name:`` and
``-- description:``) and are run by :meth:`SnowflakeSync.report`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import delete, func, select, update

from tactidose.config import Settings
from tactidose.core.bus import EventBus
from tactidose.core.clock import Clock
from tactidose.db.models import AnalyticsOutbox
from tactidose.db.outbox import KIND_ADHERENCE, KIND_DEVICE_EVENT
from tactidose.db.session import Database

log = logging.getLogger(__name__)

__all__ = [
    "SnowflakeSync", "SfColumn", "NamedQuery", "PreparedBatch",
    "ADHERENCE_COLUMNS", "DEVICE_EVENT_COLUMNS", "ADHERENCE_TABLE", "DEVICE_EVENTS_TABLE",
    "ADHERENCE_STAGE", "DEVICE_EVENTS_STAGE", "TOPIC_SYNC",
    "snowflake_auth_kwargs", "auth_mode", "describe_config",
    "adherence_table_ddl", "device_events_table_ddl", "adherence_stage_ddl",
    "device_events_stage_ddl", "adherence_stage_insert_sql", "device_events_stage_insert_sql",
    "adherence_merge_sql", "device_events_merge_sql", "prepare_batch",
    "parse_named_queries", "load_named_queries", "default_queries_path",
]

#: Bus topic published after every sync attempt (payload = the sync report dict).
TOPIC_SYNC = "analytics.sync"

BATCH_SIZE = 500
MAX_BACKOFF_S = 600.0
PRUNE_AFTER_DAYS = 7
PRUNE_EVERY_S = 3600.0
LAST_ERROR_CHARS = 500
REPORT_MAX_ROWS = 200
LOGIN_TIMEOUT_S = 15
NETWORK_TIMEOUT_S = 30
#: How long a manual sync_once() (HTTP "sync now") waits for a sync already in progress.
SYNC_LOCK_WAIT_S = 60.0
APPLICATION = "TactiDose"
SUPPORTED_KINDS = (KIND_ADHERENCE, KIND_DEVICE_EVENT)

ADHERENCE_TABLE = "ADHERENCE_EVENTS"
DEVICE_EVENTS_TABLE = "DEVICE_EVENTS"
ADHERENCE_STAGE = "TD_STAGE_ADHERENCE"
DEVICE_EVENTS_STAGE = "TD_STAGE_DEVICE_EVENTS"

_TOKEN_AUTHENTICATORS = frozenset({"PROGRAMMATIC_ACCESS_TOKEN", "OAUTH", "PAT_WITH_EXTERNAL_SESSION"})
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


# =========================================================================== columns


@dataclass(frozen=True)
class SfColumn:
    """One Snowflake column fed from one payload key."""

    key: str                 # payload key
    name: str                # Snowflake column name
    sql_type: str            # target column type
    kind: str                # converter: str | int | float | bool | ts | date | json
    max_len: int = 0         # str truncation (0 = no limit)
    comment: str = ""

    @property
    def stage_name(self) -> str:
        return f"{self.name}_JSON" if self.kind == "json" else self.name

    @property
    def stage_type(self) -> str:
        return "VARCHAR" if self.kind == "json" else self.sql_type


#: Exactly the keys of ``db.outbox.adherence_payload`` (a test enforces this).
ADHERENCE_COLUMNS: tuple[SfColumn, ...] = (
    SfColumn("event_uid", "EVENT_UID", "VARCHAR", "str", 128, "device_id:event_id (MERGE key)"),
    SfColumn("device_id", "DEVICE_ID", "VARCHAR", "str", 64),
    SfColumn("user_hash", "USER_HASH", "VARCHAR", "str", 64, "keyed pseudonym of the user"),
    SfColumn("schedule_hash", "SCHEDULE_HASH", "VARCHAR", "str", 64, "keyed pseudonym of the schedule"),
    SfColumn("scheduled_at", "SCHEDULED_AT", "TIMESTAMP_NTZ", "ts", comment="UTC"),
    SfColumn("scheduled_local_date", "SCHEDULED_LOCAL_DATE", "DATE", "date", comment="device-local date"),
    SfColumn("scheduled_local_hour", "SCHEDULED_LOCAL_HOUR", "INTEGER", "int", comment="device-local hour"),
    SfColumn("scheduled_local_dow", "SCHEDULED_LOCAL_DOW", "VARCHAR", "str", 3, "MON..SUN"),
    SfColumn("time_window", "TIME_WINDOW", "VARCHAR", "str", 16, "morning|afternoon|evening|night"),
    SfColumn("dispensed_at", "DISPENSED_AT", "TIMESTAMP_NTZ", "ts", comment="UTC"),
    SfColumn("confirmed_taken_at", "CONFIRMED_TAKEN_AT", "TIMESTAMP_NTZ", "ts", comment="UTC"),
    SfColumn("dispense_delay_minutes", "DISPENSE_DELAY_MINUTES", "FLOAT", "float"),
    SfColumn("confirm_delay_minutes", "CONFIRM_DELAY_MINUTES", "FLOAT", "float"),
    SfColumn("delay_minutes", "DELAY_MINUTES", "FLOAT", "float"),
    SfColumn("missed", "MISSED", "BOOLEAN", "bool"),
    SfColumn("taken", "TAKEN", "BOOLEAN", "bool"),
    SfColumn("final_status", "FINAL_STATUS", "VARCHAR", "str", 32, "dose status when recorded"),
    SfColumn("hardware_result", "HARDWARE_RESULT", "VARCHAR", "str", 255),
    SfColumn("needs_review", "NEEDS_REVIEW", "BOOLEAN", "bool"),
    SfColumn("attempts", "ATTEMPTS", "INTEGER", "int"),
    SfColumn("slot_number", "SLOT_NUMBER", "INTEGER", "int"),
    SfColumn("recorded_at", "RECORDED_AT", "TIMESTAMP_NTZ", "ts", comment="UTC; latest state wins"),
)

#: ``EVENT_KEY`` is the outbox ``dedupe_key``; the rest are ``enqueue_device_event`` payload keys.
DEVICE_EVENT_COLUMNS: tuple[SfColumn, ...] = (
    SfColumn("event_key", "EVENT_KEY", "VARCHAR", "str", 128, "outbox dedupe key (MERGE key)"),
    SfColumn("device_id", "DEVICE_ID", "VARCHAR", "str", 64),
    SfColumn("event_type", "EVENT_TYPE", "VARCHAR", "str", 64),
    SfColumn("code", "CODE", "VARCHAR", "str", 64),
    SfColumn("occurred_at", "OCCURRED_AT", "TIMESTAMP_NTZ", "ts", comment="UTC"),
    SfColumn("detail", "DETAIL", "VARIANT", "json", comment="no personal data"),
)


# =========================================================================== SQL builders


def _column_defs(columns: Sequence[SfColumn], *, key: str) -> str:
    parts = []
    for c in columns:
        # Documented order: <name> <type> [COMMENT '...'] [NOT NULL]
        comment = f" COMMENT '{c.comment}'" if c.comment else ""
        not_null = " NOT NULL" if c.name == key else ""
        parts.append(f"    {c.name} {c.sql_type}{comment}{not_null}")
    return ",\n".join(parts)


def adherence_table_ddl() -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {ADHERENCE_TABLE} (\n"
        f"{_column_defs(ADHERENCE_COLUMNS, key='EVENT_UID')},\n"
        "    SYNCED_AT TIMESTAMP_NTZ COMMENT 'UTC time of the last MERGE',\n"
        f"    CONSTRAINT PK_{ADHERENCE_TABLE} PRIMARY KEY (EVENT_UID)\n"
        ") COMMENT = 'TactiDose de-identified dose events, latest state per EVENT_UID. Timestamps are UTC.'"
    )


def device_events_table_ddl() -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {DEVICE_EVENTS_TABLE} (\n"
        f"{_column_defs(DEVICE_EVENT_COLUMNS, key='EVENT_KEY')},\n"
        "    SYNCED_AT TIMESTAMP_NTZ COMMENT 'UTC time of the MERGE',\n"
        f"    CONSTRAINT PK_{DEVICE_EVENTS_TABLE} PRIMARY KEY (EVENT_KEY)\n"
        ") COMMENT = 'TactiDose hardware faults, resets and disconnects. Timestamps are UTC.'"
    )


def _stage_ddl(table: str, columns: Sequence[SfColumn]) -> str:
    cols = ",\n".join(f"    {c.stage_name} {c.stage_type}" for c in columns)
    return f"CREATE OR REPLACE TEMPORARY TABLE {table} (\n{cols},\n    SOURCE_SEQ INTEGER\n)"


def adherence_stage_ddl() -> str:
    return _stage_ddl(ADHERENCE_STAGE, ADHERENCE_COLUMNS)


def device_events_stage_ddl() -> str:
    return _stage_ddl(DEVICE_EVENTS_STAGE, DEVICE_EVENT_COLUMNS)


def _stage_insert(table: str, columns: Sequence[SfColumn]) -> str:
    names = [c.stage_name for c in columns] + ["SOURCE_SEQ"]
    placeholders = ", ".join(["%s"] * len(names))
    return f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"


def adherence_stage_insert_sql() -> str:
    """pyformat ``executemany`` statement (the connector rewrites it into one multi-row INSERT)."""
    return _stage_insert(ADHERENCE_STAGE, ADHERENCE_COLUMNS)


def device_events_stage_insert_sql() -> str:
    return _stage_insert(DEVICE_EVENTS_STAGE, DEVICE_EVENT_COLUMNS)


def adherence_merge_sql() -> str:
    names = [c.name for c in ADHERENCE_COLUMNS]
    select_list = ",\n        ".join(names + ["SOURCE_SEQ", "SYSDATE() AS SYNCED_AT"])
    updates = ",\n        ".join(f"{n} = s.{n}" for n in names[1:] + ["SYNCED_AT"])
    insert_cols = ", ".join(names + ["SYNCED_AT"])
    insert_vals = ", ".join(f"s.{n}" for n in names + ["SYNCED_AT"])
    return (
        f"MERGE INTO {ADHERENCE_TABLE} AS t\n"
        "USING (\n"
        f"    SELECT\n        {select_list}\n"
        f"    FROM {ADHERENCE_STAGE}\n"
        "    QUALIFY ROW_NUMBER() OVER (\n"
        "        PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SOURCE_SEQ DESC) = 1\n"
        ") AS s\n"
        "ON t.EVENT_UID = s.EVENT_UID\n"
        "WHEN MATCHED AND (t.RECORDED_AT IS NULL OR s.RECORDED_AT >= t.RECORDED_AT) THEN UPDATE SET\n"
        f"        {updates}\n"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols})\n"
        f"    VALUES ({insert_vals})"
    )


def device_events_merge_sql() -> str:
    names = [c.name for c in DEVICE_EVENT_COLUMNS]
    select_parts = [
        f"TRY_PARSE_JSON({c.stage_name}) AS {c.name}" if c.kind == "json" else c.name
        for c in DEVICE_EVENT_COLUMNS
    ]
    select_list = ",\n        ".join(select_parts + ["SOURCE_SEQ", "SYSDATE() AS SYNCED_AT"])
    insert_cols = ", ".join(names + ["SYNCED_AT"])
    insert_vals = ", ".join(f"s.{n}" for n in names + ["SYNCED_AT"])
    return (
        f"MERGE INTO {DEVICE_EVENTS_TABLE} AS t\n"
        "USING (\n"
        f"    SELECT\n        {select_list}\n"
        f"    FROM {DEVICE_EVENTS_STAGE}\n"
        "    QUALIFY ROW_NUMBER() OVER (PARTITION BY EVENT_KEY ORDER BY SOURCE_SEQ DESC) = 1\n"
        ") AS s\n"
        "ON t.EVENT_KEY = s.EVENT_KEY\n"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols})\n"
        f"    VALUES ({insert_vals})"
    )


def _ident(name: str) -> str:
    """Snowflake identifier: plain names unquoted (case-insensitive), anything else quoted."""
    if _IDENT_RE.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


# =========================================================================== value conversion


def _parse_ts(value: Any) -> datetime | None:
    """Payload timestamp -> naive UTC ``datetime`` (``TIMESTAMP_NTZ``), ``None`` if invalid."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _convert(col: SfColumn, value: Any) -> Any:
    if value is None:
        return None
    kind = col.kind
    if kind == "str":
        text = value if isinstance(value, str) else (
            json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value))
        return text[: col.max_len] if col.max_len else text
    if kind == "int":
        if isinstance(value, bool):
            return int(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if math.isfinite(number) else None
    if kind == "float":
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "t", "y"):
                return True
            if low in ("false", "0", "no", "f", "n"):
                return False
        return None
    if kind == "ts":
        return _parse_ts(value)
    if kind == "date":
        return _parse_date(value)
    if kind == "json":
        text = json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
        return text if len(text) <= 16000 else json.dumps({"truncated": True})
    raise ValueError(f"unknown column kind {kind!r}")


# =========================================================================== batch preparation


@dataclass(frozen=True)
class _OutboxRow:
    outbox_id: int
    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    created_at: datetime | None


@dataclass
class PreparedBatch:
    """Stage rows ready for ``executemany`` (column order + ``SOURCE_SEQ``)."""

    outbox_ids: list[int] = field(default_factory=list)
    adherence: list[tuple[Any, ...]] = field(default_factory=list)
    device_events: list[tuple[Any, ...]] = field(default_factory=list)
    superseded: int = 0


_MIN_TS = datetime.min


def _adherence_row(row: _OutboxRow) -> tuple[Any, ...]:
    payload = dict(row.payload)
    if not payload.get("event_uid"):
        payload["event_uid"] = row.dedupe_key.split(":", 1)[-1]
    if not _parse_ts(payload.get("recorded_at")) and row.created_at is not None:
        payload["recorded_at"] = row.created_at.isoformat()
    values = tuple(_convert(c, payload.get(c.key)) for c in ADHERENCE_COLUMNS)
    return values + (row.outbox_id,)


def _device_event_row(row: _OutboxRow) -> tuple[Any, ...]:
    payload = dict(row.payload)
    payload["event_key"] = row.dedupe_key
    if not _parse_ts(payload.get("occurred_at")) and row.created_at is not None:
        payload["occurred_at"] = row.created_at.isoformat()
    if payload.get("detail") is None:
        payload["detail"] = {}
    values = tuple(_convert(c, payload.get(c.key)) for c in DEVICE_EVENT_COLUMNS)
    return values + (row.outbox_id,)


def prepare_batch(rows: Iterable[_OutboxRow]) -> PreparedBatch:
    """Collapse to one stage row per key (latest wins) and convert values for Snowflake.

    Adherence: latest = highest ``recorded_at`` then highest ``outbox_id`` — consistent
    with the MERGE condition, so the outcome does not depend on batch boundaries.
    Device events: one row per ``dedupe_key`` (identical events), highest ``outbox_id``.
    """
    batch = PreparedBatch()
    recorded_idx = [c.key for c in ADHERENCE_COLUMNS].index("recorded_at")
    adherence: dict[str, tuple[tuple[datetime, int], tuple[Any, ...]]] = {}
    device: dict[str, tuple[int, tuple[Any, ...]]] = {}
    total = 0
    for row in rows:
        batch.outbox_ids.append(row.outbox_id)
        if row.kind == KIND_ADHERENCE:
            total += 1
            values = _adherence_row(row)
            sort_key = (values[recorded_idx] or _MIN_TS, row.outbox_id)
            current = adherence.get(row.dedupe_key)
            if current is None or sort_key >= current[0]:
                adherence[row.dedupe_key] = (sort_key, values)
        elif row.kind == KIND_DEVICE_EVENT:
            total += 1
            current_dev = device.get(row.dedupe_key)
            if current_dev is None or row.outbox_id >= current_dev[0]:
                device[row.dedupe_key] = (row.outbox_id, _device_event_row(row))
    batch.adherence = [v for _, v in sorted(adherence.values(), key=lambda item: item[1][-1])]
    batch.device_events = [v for _, v in sorted(device.values(), key=lambda item: item[1][-1])]
    batch.superseded = total - len(batch.adherence) - len(batch.device_events)
    return batch


# =========================================================================== named queries


@dataclass(frozen=True)
class NamedQuery:
    name: str
    description: str
    sql: str


_NAME_RE = re.compile(r"^\s*--\s*name\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
_DESC_RE = re.compile(r"^\s*--\s*description\s*:\s*(.*?)\s*$", re.IGNORECASE)


def _has_sql(lines: Sequence[str]) -> bool:
    return any(line.strip() and not line.strip().startswith("--") for line in lines)


def parse_named_queries(text: str) -> list[NamedQuery]:
    """Split a SQL file into blocks introduced by ``-- name: X`` (+ ``-- description: Y``).

    Text before the first ``-- name:`` is a file header and ignored. Several
    ``-- description:`` lines before the SQL are joined with spaces. Blocks without any
    SQL are skipped; trailing semicolons are removed.
    """
    queries: list[NamedQuery] = []
    name: str | None = None
    desc: list[str] = []
    body: list[str] = []

    def flush() -> None:
        if name is None or not _has_sql(body):
            return
        sql = "\n".join(body).strip()
        while sql.endswith(";"):
            sql = sql[:-1].rstrip()
        queries.append(NamedQuery(name=name, description=" ".join(d for d in desc if d), sql=sql))

    for line in text.splitlines():
        m = _NAME_RE.match(line)
        if m:
            flush()
            name, desc, body = m.group(1), [], []
            continue
        if name is None:
            continue
        d = _DESC_RE.match(line)
        if d and not _has_sql(body):
            desc.append(d.group(1))
            continue
        body.append(line)
    flush()
    return queries


def default_queries_path() -> Path | None:
    """``analytics/snowflake_queries.sql`` next to the package (repo checkout) or in the CWD."""
    candidates = (
        Path(__file__).resolve().parents[2] / "analytics" / "snowflake_queries.sql",
        Path.cwd() / "analytics" / "snowflake_queries.sql",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_named_queries(path: Path | None = None) -> list[NamedQuery]:
    target = path or default_queries_path()
    if target is None:
        raise FileNotFoundError("analytics/snowflake_queries.sql not found")
    return parse_named_queries(Path(target).read_text(encoding="utf-8"))


# =========================================================================== auth / config


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    text = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    return text or None


def snowflake_auth_kwargs(settings: Settings) -> dict[str, Any]:
    """Authentication kwargs for ``snowflake.connector.connect``.

    Inferred (no ``SNOWFLAKE_AUTHENTICATOR``): token -> ``PROGRAMMATIC_ACCESS_TOKEN``;
    private key file -> ``SNOWFLAKE_JWT``; else password. An explicit authenticator wins
    and only the credential that authenticator uses is passed (the connector silently
    switches to key-pair auth whenever a key file is present, so it is only passed for
    ``SNOWFLAKE_JWT``).
    """
    token = _secret(settings.snowflake_token)
    password = _secret(settings.snowflake_password)
    key_file = (settings.snowflake_private_key_file or "").strip() or None
    key_pwd = _secret(settings.snowflake_private_key_file_pwd)

    def key_pair() -> dict[str, Any]:
        kw: dict[str, Any] = {}
        if key_file:
            kw["private_key_file"] = key_file
            if key_pwd:
                kw["private_key_file_pwd"] = key_pwd
        return kw

    explicit = (settings.snowflake_authenticator or "").strip()
    if explicit:
        kw: dict[str, Any] = {"authenticator": explicit}
        upper = explicit.upper()
        if upper == "SNOWFLAKE_JWT":
            kw.update(key_pair())
        elif upper in _TOKEN_AUTHENTICATORS:
            if token:
                kw["token"] = token
        elif password or token:
            kw["password"] = password or token   # a PAT also works in place of a password
        return kw
    if token:
        return {"authenticator": "PROGRAMMATIC_ACCESS_TOKEN", "token": token}
    if key_file:
        return {"authenticator": "SNOWFLAKE_JWT", **key_pair()}
    if password:
        return {"password": password}
    return {}


def auth_mode(settings: Settings) -> str:
    """``pat`` | ``key_pair`` | ``password`` | ``explicit:<AUTHENTICATOR>`` | ``none``."""
    explicit = (settings.snowflake_authenticator or "").strip()
    if explicit:
        return f"explicit:{explicit.upper()}"
    if _secret(settings.snowflake_token):
        return "pat"
    if (settings.snowflake_private_key_file or "").strip():
        return "key_pair"
    if _secret(settings.snowflake_password):
        return "password"
    return "none"


def describe_config(settings: Settings) -> dict[str, Any]:
    """Non-secret view of the Snowflake settings (doctor command / health pages)."""
    return {
        "configured": settings.snowflake_configured,
        "account": settings.snowflake_account,
        "user_configured": bool(settings.snowflake_user),
        "auth": auth_mode(settings),
        "warehouse": settings.snowflake_warehouse,
        "database": settings.snowflake_database,
        "schema": settings.snowflake_schema,
        "role": settings.snowflake_role,
        "sync_interval_s": settings.analytics_sync_interval_s,
    }


def _default_connect(**kwargs: Any) -> Any:
    import snowflake.connector  # optional dependency: tactidose[snowflake]

    return snowflake.connector.connect(**kwargs)


def _short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# =========================================================================== sync worker


class SnowflakeSync:
    """Drains ``analytics_outbox`` into Snowflake (see module docstring).

    ``connect`` replaces ``snowflake.connector.connect`` (tests pass a fake); it is called
    with keyword arguments only. ``queries_path`` overrides the location of
    ``snowflake_queries.sql``; ``initial_delay_s`` is the delay before the first sync
    after :meth:`start`.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        clock: Clock,
        bus: EventBus | None = None,
        connect: Callable[..., Any] | None = None,
        *,
        queries_path: Path | None = None,
        initial_delay_s: float = 5.0,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._db = db
        self._settings = settings
        self._clock = clock
        self._bus = bus
        self._connect = connect or _default_connect
        self._queries_path = queries_path
        self._initial_delay_s = max(0.0, float(initial_delay_s))
        self._batch_size = max(1, int(batch_size))
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._schema_ready = False
        self._last_sync: datetime | None = None
        self._last_attempt: datetime | None = None
        self._last_error: str | None = None
        self._failures = 0
        self._last_prune_mono: float | None = None
        self._secrets = tuple(s for s in (
            _secret(settings.snowflake_password),
            _secret(settings.snowflake_token),
            _secret(settings.snowflake_private_key_file_pwd),
        ) if s)

    # ------------------------------------------------------------------ config
    @property
    def configured(self) -> bool:
        return self._settings.snowflake_configured

    def connection_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``snowflake.connector.connect`` (contains secrets)."""
        s = self._settings
        kw: dict[str, Any] = {
            "account": s.snowflake_account,
            "user": s.snowflake_user,
            "database": s.snowflake_database,
            "schema": s.snowflake_schema,
            "login_timeout": LOGIN_TIMEOUT_S,
            "network_timeout": NETWORK_TIMEOUT_S,
            "application": APPLICATION,
            "paramstyle": "pyformat",
        }
        if s.snowflake_warehouse:
            kw["warehouse"] = s.snowflake_warehouse
        if s.snowflake_role:
            kw["role"] = s.snowflake_role
        kw.update(snowflake_auth_kwargs(s))
        return kw

    def backoff_delay(self, failures: int) -> float:
        """Seconds until the next attempt after ``failures`` consecutive failures."""
        interval = float(self._settings.analytics_sync_interval_s)
        if failures <= 0:
            return interval
        return max(interval, min(MAX_BACKOFF_S, interval * (2 ** min(failures - 1, 20))))

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the ``analytics-sync`` daemon thread (idempotent; no-op if not configured)."""
        try:
            with self._lifecycle_lock:
                if not self.configured:
                    log.info("Snowflake not configured; analytics stay in the local outbox")
                    return
                if self._thread is not None and self._thread.is_alive():
                    return
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="analytics-sync", daemon=True)
                self._thread.start()
                log.info("analytics-sync started (every %.0f s)", self._settings.analytics_sync_interval_s)
        except Exception:  # noqa: BLE001 - start() must not raise
            log.exception("could not start analytics-sync")

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker (idempotent). Pending rows stay in the outbox for next time."""
        try:
            with self._lifecycle_lock:
                self._stop.set()
                thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout)
                if thread.is_alive():
                    log.warning("analytics-sync still busy after %.1f s; leaving daemon thread", timeout)
        except Exception:  # noqa: BLE001 - close() must not raise
            log.exception("error while stopping analytics-sync")

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def _run(self) -> None:
        delay = self._initial_delay_s
        while not self._stop.wait(delay):
            report = self.sync_once()
            self._maybe_prune()
            if report.get("ok"):
                full = report.get("selected", 0) >= self._batch_size
                delay = 1.0 if full else float(self._settings.analytics_sync_interval_s)
            else:
                with self._state_lock:
                    failures = self._failures
                delay = self.backoff_delay(max(failures, 1))

    # ------------------------------------------------------------------ sync
    def sync_once(self) -> dict[str, Any]:
        """Push one batch of pending outbox rows. Never raises; returns a report dict:

        ``{ok, configured, selected, adherence, device_events, superseded, sent, error, at,
        duration_s}`` — ``selected`` outbox rows were read, ``adherence``/``device_events``
        unique rows were merged, ``superseded`` older duplicates were folded into newer
        ones, ``sent`` rows were stamped ``sent_at``.
        """
        started = time.monotonic()
        report: dict[str, Any] = {
            "ok": False, "configured": self.configured, "selected": 0, "adherence": 0,
            "device_events": 0, "superseded": 0, "sent": 0, "error": None,
            "at": _iso(self._now()), "duration_s": 0.0,
        }
        try:
            if not self.configured:
                report["error"] = "not_configured"
                return report
            if not self._sync_lock.acquire(timeout=SYNC_LOCK_WAIT_S):
                report["error"] = "busy: another sync is still running"
                return report
            try:
                self._sync_locked(report)
            finally:
                self._sync_lock.release()
        except Exception as exc:  # noqa: BLE001 - defensive: never raise
            log.exception("analytics sync crashed")
            report["ok"] = False
            report["error"] = self._redact(_short_error(exc))[:LAST_ERROR_CHARS]
        finally:
            report["duration_s"] = round(time.monotonic() - started, 3)
        if report["selected"] or report["error"]:
            self._publish(report)
        return report

    def _sync_locked(self, report: dict[str, Any]) -> None:
        with self._state_lock:
            self._last_attempt = self._now()
        try:
            rows = self._load_batch()
        except Exception as exc:  # noqa: BLE001
            err = f"local db: {_short_error(exc)}"[:LAST_ERROR_CHARS]
            self._note_failure(err)
            report["error"] = err
            log.warning("analytics sync could not read the outbox: %s", err)
            return
        report["selected"] = len(rows)
        if not rows:
            report["ok"] = True
            return
        batch = prepare_batch(rows)
        report["adherence"] = len(batch.adherence)
        report["device_events"] = len(batch.device_events)
        report["superseded"] = batch.superseded
        try:
            self._push(batch)
        except Exception as exc:  # noqa: BLE001
            err = self._redact(_short_error(exc))[:LAST_ERROR_CHARS]
            self._schema_ready = False   # re-check DDL next time (tables may have been dropped)
            try:
                self._mark_failed(batch.outbox_ids, err)
            except Exception as db_exc:  # noqa: BLE001
                log.warning("could not record the sync failure locally: %s", _short_error(db_exc))
            self._note_failure(err)
            report["error"] = err
            log.warning("Snowflake sync failed; %d outbox rows stay pending: %s", len(rows), err)
            return
        try:
            report["sent"] = self._mark_sent(batch.outbox_ids)
        except Exception as exc:  # noqa: BLE001
            # Snowflake has the rows; they will be re-sent and the MERGE makes that harmless.
            err = f"local db: {_short_error(exc)}"[:LAST_ERROR_CHARS]
            self._note_failure(err)
            report["error"] = err
            log.warning("Snowflake sync succeeded but marking rows sent failed: %s", err)
            return
        report["ok"] = True
        with self._state_lock:
            self._failures = 0
            self._last_error = None
            self._last_sync = self._now()
        log.info("Snowflake sync: %d outbox rows -> %d adherence + %d device events",
                 len(rows), len(batch.adherence), len(batch.device_events))

    def _push(self, batch: PreparedBatch) -> None:
        conn = self._connect(**self.connection_kwargs())
        try:
            cur = conn.cursor()
            try:
                if not self._schema_ready:
                    self._ensure_schema_on(cur)
                    self._schema_ready = True
                if batch.adherence:
                    cur.execute(adherence_stage_ddl())
                    cur.executemany(adherence_stage_insert_sql(), batch.adherence)
                    cur.execute(adherence_merge_sql())
                if batch.device_events:
                    cur.execute(device_events_stage_ddl())
                    cur.executemany(device_events_stage_insert_sql(), batch.device_events)
                    cur.execute(device_events_merge_sql())
            finally:
                _quiet_close(cur)
        finally:
            _quiet_close(conn)

    def _load_batch(self) -> list[_OutboxRow]:
        with self._db.session() as s:
            result = s.execute(
                select(
                    AnalyticsOutbox.outbox_id, AnalyticsOutbox.kind, AnalyticsOutbox.dedupe_key,
                    AnalyticsOutbox.payload, AnalyticsOutbox.created_at,
                )
                .where(AnalyticsOutbox.sent_at.is_(None), AnalyticsOutbox.kind.in_(SUPPORTED_KINDS))
                .order_by(AnalyticsOutbox.outbox_id)
                .limit(self._batch_size)
            ).all()
        return [
            _OutboxRow(r.outbox_id, r.kind, r.dedupe_key,
                       r.payload if isinstance(r.payload, dict) else {}, r.created_at)
            for r in result
        ]

    def _mark_sent(self, ids: Sequence[int]) -> int:
        now = self._now()
        with self._db.session() as s:
            res = s.execute(
                update(AnalyticsOutbox)
                .where(AnalyticsOutbox.outbox_id.in_(list(ids)), AnalyticsOutbox.sent_at.is_(None))
                .values(sent_at=now)
                .execution_options(synchronize_session=False)
            )
            count = res.rowcount
        return int(count) if count is not None and count >= 0 else len(ids)

    def _mark_failed(self, ids: Sequence[int], error: str) -> None:
        with self._db.session() as s:
            s.execute(
                update(AnalyticsOutbox)
                .where(AnalyticsOutbox.outbox_id.in_(list(ids)), AnalyticsOutbox.sent_at.is_(None))
                .values(attempts=func.coalesce(AnalyticsOutbox.attempts, 0) + 1, last_error=error)
                .execution_options(synchronize_session=False)
            )

    def _note_failure(self, error: str) -> None:
        with self._state_lock:
            self._failures += 1
            self._last_error = error

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> dict[str, Any]:
        """Create database/schema (best effort) and both tables if missing.

        Returns ``{ok, error}``; never raises.
        """
        if not self.configured:
            return {"ok": False, "error": "not_configured"}
        try:
            conn = self._connect(**self.connection_kwargs())
            try:
                cur = conn.cursor()
                try:
                    self._ensure_schema_on(cur)
                finally:
                    _quiet_close(cur)
            finally:
                _quiet_close(conn)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": self._redact(_short_error(exc))[:LAST_ERROR_CHARS]}
        self._schema_ready = True
        return {"ok": True, "error": None}

    def _ensure_schema_on(self, cur: Any) -> None:
        database = _ident(self._settings.snowflake_database)
        schema = _ident(self._settings.snowflake_schema)
        # Best effort: a least-privilege role may not be allowed to create these, which is
        # fine as long as they already exist (the USE statements below verify that).
        self._best_effort(cur, f"CREATE DATABASE IF NOT EXISTS {database}")
        cur.execute(f"USE DATABASE {database}")
        self._best_effort(cur, f"CREATE SCHEMA IF NOT EXISTS {schema}")
        cur.execute(f"USE SCHEMA {schema}")
        cur.execute(adherence_table_ddl())
        cur.execute(device_events_table_ddl())

    def _best_effort(self, cur: Any, stmt: str) -> None:
        try:
            cur.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            log.info("Snowflake: '%s' skipped (%s)", stmt, self._redact(_short_error(exc))[:200])

    # ------------------------------------------------------------------ status / report
    def status(self) -> dict[str, Any]:
        """``{configured, last_sync, pending, sent, last_error}`` plus diagnostics. Never raises."""
        pending: int | None = None
        sent: int | None = None
        db_error: str | None = None
        try:
            with self._db.session() as s:
                pending = int(s.scalar(
                    select(func.count()).select_from(AnalyticsOutbox).where(
                        AnalyticsOutbox.sent_at.is_(None), AnalyticsOutbox.kind.in_(SUPPORTED_KINDS))
                ) or 0)
                sent = int(s.scalar(
                    select(func.count()).select_from(AnalyticsOutbox).where(AnalyticsOutbox.sent_at.is_not(None))
                ) or 0)
        except Exception as exc:  # noqa: BLE001
            db_error = f"local db: {_short_error(exc)}"[:LAST_ERROR_CHARS]
        with self._state_lock:
            return {
                "configured": self.configured,
                "last_sync": _iso(self._last_sync),
                "pending": pending,
                "sent": sent,
                "last_error": self._last_error or db_error,
                "last_attempt": _iso(self._last_attempt),
                "consecutive_failures": self._failures,
                "running": self.running,
            }

    def report(self) -> dict[str, Any]:
        """Run every named query in ``snowflake_queries.sql``. Never raises.

        ``{configured, queries: [{name, description, columns, rows, error}], error}``;
        rows are JSON-friendly (Decimal -> int/float, dates -> ISO strings), at most
        200 per query. A failing query only sets its own ``error``.
        """
        out: dict[str, Any] = {"configured": self.configured, "queries": [], "error": None}
        if not self.configured:
            out["error"] = "Snowflake is not configured"
            return out
        try:
            queries = load_named_queries(self._queries_path)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"could not load analytics queries: {_short_error(exc)}"[:LAST_ERROR_CHARS]
            return out
        try:
            conn = self._connect(**self.connection_kwargs())
        except Exception as exc:  # noqa: BLE001
            out["error"] = self._redact(_short_error(exc))[:LAST_ERROR_CHARS]
            return out
        try:
            cur = conn.cursor()
            try:
                if not self._schema_ready:
                    try:
                        self._ensure_schema_on(cur)
                        self._schema_ready = True
                    except Exception as exc:  # noqa: BLE001 - queries will report their own errors
                        log.info("Snowflake schema check before report failed: %s",
                                 self._redact(_short_error(exc))[:200])
                for q in queries:
                    out["queries"].append(self._run_query(cur, q))
            finally:
                _quiet_close(cur)
        except Exception as exc:  # noqa: BLE001
            out["error"] = self._redact(_short_error(exc))[:LAST_ERROR_CHARS]
        finally:
            _quiet_close(conn)
        return out

    def _run_query(self, cur: Any, q: NamedQuery) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": q.name, "description": q.description,
                                 "columns": [], "rows": [], "error": None}
        try:
            cur.execute(q.sql)
            entry["columns"] = [str(d[0]) for d in (cur.description or [])]
            fetchmany = getattr(cur, "fetchmany", None)
            rows = fetchmany(REPORT_MAX_ROWS) if callable(fetchmany) else cur.fetchall()[:REPORT_MAX_ROWS]
            entry["rows"] = [[_json_value(v) for v in row] for row in rows or []]
        except Exception as exc:  # noqa: BLE001
            entry["error"] = self._redact(_short_error(exc))[:LAST_ERROR_CHARS]
        return entry

    # ------------------------------------------------------------------ maintenance
    def prune(self, older_than_days: int = PRUNE_AFTER_DAYS) -> int:
        """Delete outbox rows that were sent and were created more than N days ago.

        Unsent rows are never deleted. Returns the number of rows removed (0 on error).
        Note: local analytics read device events from the outbox, so they only see
        what the retention keeps.
        """
        cutoff = self._retention_now() - timedelta(days=max(0, older_than_days))
        try:
            with self._db.session() as s:
                res = s.execute(
                    delete(AnalyticsOutbox)
                    .where(AnalyticsOutbox.sent_at.is_not(None), AnalyticsOutbox.created_at < cutoff)
                    .execution_options(synchronize_session=False)
                )
                count = res.rowcount
            removed = int(count) if count and count > 0 else 0
            if removed:
                log.info("pruned %d sent analytics outbox rows older than %d days", removed, older_than_days)
            return removed
        except Exception as exc:  # noqa: BLE001
            log.warning("outbox prune failed: %s", _short_error(exc))
            return 0

    def _maybe_prune(self) -> None:
        now = time.monotonic()
        if self._last_prune_mono is not None and now - self._last_prune_mono < PRUNE_EVERY_S:
            return
        self._last_prune_mono = now
        self.prune()

    def _retention_now(self) -> datetime:
        # ``created_at`` is wall-clock UTC; never let demo time travel prune extra rows.
        return min(self._clock.now(), datetime.now(timezone.utc))

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        return self._clock.now()

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _publish(self, report: dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(TOPIC_SYNC, dict(report))
        except Exception:  # noqa: BLE001
            log.debug("could not publish analytics sync report", exc_info=True)


def _quiet_close(obj: Any) -> None:
    try:
        obj.close()
    except Exception:  # noqa: BLE001
        log.debug("close() failed", exc_info=True)
