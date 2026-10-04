"""Engine/session factory for SQLite (local, offline) and TiDB (cloud, MySQL protocol).

Selection order:
1. ``TACTIDOSE_DATABASE_URL`` / ``DATABASE_URL`` – any SQLAlchemy URL;
2. ``TIDB_HOST`` (+ ``TIDB_USER``/``TIDB_PASSWORD``/``TIDB_DATABASE``) – TiDB over TLS;
3. otherwise SQLite at ``<data_dir>/tactidose.db``.

Failure policy (handoff §30): callers must treat any database exception as
"state unknown" and fail closed — never dispense when the DB cannot be read/written.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from tactidose.config import Settings
from tactidose.db.models import Base

log = logging.getLogger(__name__)


def build_database_url(settings: Settings) -> str | URL:
    if settings.database_url:
        return settings.database_url
    if settings.tidb_host:
        return URL.create(
            drivername="mysql+pymysql",
            username=settings.tidb_user,
            password=settings.tidb_password.get_secret_value() if settings.tidb_password else None,
            host=settings.tidb_host,
            port=settings.tidb_port,
            database=settings.tidb_database,
            query={"charset": "utf8mb4"},
        )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{settings.sqlite_path.resolve().as_posix()}"


def tidb_connect_args(settings: Settings) -> dict[str, object]:
    """PyMySQL connect kwargs (TLS + timeouts) used for TiDB; shared by integrations/tidb.py."""
    return _tidb_connect_args(settings)


def _tidb_connect_args(settings: Settings) -> dict[str, object]:
    args: dict[str, object] = {"connect_timeout": 10, "read_timeout": 15, "write_timeout": 15}
    if settings.tidb_ssl:
        ca = settings.tidb_ssl_ca
        if not ca:
            try:
                import certifi

                ca = certifi.where()
            except ImportError:  # pragma: no cover - certifi ships with requests/httpx
                ca = None
        args.update({"ssl_verify_cert": True, "ssl_verify_identity": True})
        if ca:
            args["ssl_ca"] = ca
    return args


def create_db_engine(settings: Settings, *, echo: bool = False) -> Engine:
    url = build_database_url(settings)
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        engine = create_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return engine

    connect_args: dict[str, object] = {}
    if backend == "mysql" and settings.tidb_host and not settings.database_url:
        connect_args = _tidb_connect_args(settings)
    return create_engine(
        url,
        echo=echo,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
    )


class Database:
    """Owns the engine and session factory. One instance per process."""

    def __init__(self, settings: Settings, *, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or create_db_engine(settings)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    @property
    def is_tidb(self) -> bool:
        return self.backend == "mysql"

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, rollback on any exception."""
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True, self.backend
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:300]

    def dispose(self) -> None:
        self.engine.dispose()
