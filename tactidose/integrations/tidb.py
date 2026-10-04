"""TiDB helpers: configuration summary, connection check and database bootstrap.

The operational database itself is owned by ``db/session.py`` (TiDB over TLS when
``TIDB_HOST`` is set, SQLite otherwise). This module adds the operator-facing bits used by
the ``doctor`` / ``init-db`` commands and the health page:

* :func:`describe` - non-secret summary (host, port, database, TLS, CA source).
* :func:`check_connection` - ``SELECT VERSION()`` (``sqlite_version()`` on SQLite) through
  the same :class:`~tactidose.db.session.Database` the app uses.
* :func:`ensure_database` - best-effort ``CREATE DATABASE IF NOT EXISTS`` over a
  server-level PyMySQL connection with the same TLS arguments as ``db/session.py``.
  TiDB Cloud Starter clusters ship with a ``test`` database; ours defaults to
  ``tactidose``, which has to exist before the engine (bound to that database) connects.

Nothing here raises for connection problems: results are ``(ok, message, ...)`` tuples and
messages never contain the password.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import make_url

from tactidose.config import Settings
from tactidose.db.session import Database, _tidb_connect_args

log = logging.getLogger(__name__)

__all__ = ["describe", "check_connection", "is_tidb_version", "ensure_database", "quote_identifier"]

_MAX_MESSAGE = 300


def _password(settings: Settings) -> str | None:
    if settings.tidb_password is None:
        return None
    return settings.tidb_password.get_secret_value() or None


def _redact(message: str, settings: Settings) -> str:
    secrets = [_password(settings)]
    if settings.database_url:
        try:
            secrets.append(make_url(settings.database_url).password)
        except Exception:  # noqa: BLE001 - an unparseable URL has nothing to redact
            pass
    for secret in secrets:
        if secret:
            message = message.replace(str(secret), "***")
    return message


def _short_error(exc: BaseException, settings: Settings) -> str:
    inner = getattr(exc, "orig", None) or exc       # SQLAlchemy wraps DBAPI errors
    detail = " ".join(str(inner).split())
    msg = f"{type(inner).__name__}: {detail}" if detail else type(inner).__name__
    return _redact(msg, settings)[:_MAX_MESSAGE]


def _ca_source(settings: Settings) -> tuple[str, str | None]:
    from tactidose.db.session import tidb_ssl_ca  # the CA the connection really uses

    return tidb_ssl_ca(settings)


def describe(settings: Settings) -> dict[str, Any]:
    """Non-secret description of the operational database configuration.

    ``backend`` is ``tidb`` (``TIDB_HOST``), ``sqlite`` (default) or the SQLAlchemy backend
    name of ``TACTIDOSE_DATABASE_URL`` (which overrides ``TIDB_*``). Passwords and user
    names are never included, only whether they are set.
    """
    if settings.database_url:
        info: dict[str, Any] = {"backend": "custom", "source": "TACTIDOSE_DATABASE_URL",
                                "host": None, "port": None, "database": None,
                                "user_configured": False, "password_configured": False,
                                "tls": None, "ca_source": None, "ca_path": None}
        try:
            url = make_url(settings.database_url)
            info.update(backend=url.get_backend_name(), host=url.host, port=url.port,
                        database=url.database, user_configured=bool(url.username),
                        password_configured=bool(url.password))
        except Exception:  # noqa: BLE001
            info["backend"] = "invalid-url"
        info["tidb_configured"] = False
        return info
    if settings.tidb_host:
        ca_source, ca_path = _ca_source(settings)
        return {
            "backend": "tidb",
            "source": "TIDB_*",
            "tidb_configured": True,
            "host": settings.tidb_host,
            "port": settings.tidb_port,
            "database": settings.tidb_database,
            "user_configured": bool(settings.tidb_user),
            "password_configured": bool(_password(settings)),
            "tls": bool(settings.tidb_ssl),
            "ca_source": ca_source,
            "ca_path": ca_path,
        }
    return {
        "backend": "sqlite",
        "source": "default",
        "tidb_configured": False,
        "host": None,
        "port": None,
        "database": str(Path(settings.sqlite_path)),
        "user_configured": False,
        "password_configured": False,
        "tls": None,
        "ca_source": None,
        "ca_path": None,
    }


def is_tidb_version(version: str | None) -> bool:
    """True for TiDB ``VERSION()`` strings, e.g. ``8.0.11-TiDB-v7.5.2-serverless``."""
    return bool(version) and "tidb" in str(version).lower()


def check_connection(
    settings: Settings, *, database: Database | None = None
) -> tuple[bool, str, str | None]:
    """Connect and read the server version: ``(ok, message, server_version)``.

    Uses ``database`` if given, else a temporary :class:`Database` built from ``settings``
    (disposed afterwards). Never raises.
    """
    owned = database is None
    db: Database | None = database
    try:
        if db is None:
            db = Database(settings)
        is_sqlite = db.backend == "sqlite"
        query = "SELECT sqlite_version()" if is_sqlite else "SELECT VERSION()"
        with db.engine.connect() as conn:
            version = conn.execute(text(query)).scalar()
        version_text = None if version is None else str(version)
        if is_sqlite:
            kind = "SQLite"
        elif is_tidb_version(version_text):
            kind = "TiDB"
        else:
            kind = f"{db.backend} (not TiDB)"
        where = _where(settings, db)
        return True, f"Connected to {kind}{where}", version_text
    except Exception as exc:  # noqa: BLE001
        msg = _short_error(exc, settings)
        if "1049" in msg or "unknown database" in msg.lower():
            msg += (f" - create it first: ensure_database() / `python -m tactidose init-db`"
                    f" (database '{settings.tidb_database}')")
        log.warning("database connection check failed: %s", msg)
        return False, msg, None
    finally:
        if owned and db is not None:
            try:
                db.dispose()
            except Exception:  # noqa: BLE001
                log.debug("dispose failed", exc_info=True)


def _where(settings: Settings, db: Database) -> str:
    try:
        url = db.engine.url
        if url.get_backend_name() == "sqlite":
            return f" at {url.database}" if url.database else ""
        host = url.host or settings.tidb_host
        port = url.port or settings.tidb_port
        return f" at {host}:{port}/{url.database or ''}"
    except Exception:  # noqa: BLE001
        return ""


def quote_identifier(name: str) -> str:
    """MySQL/TiDB identifier quoting (backticks, embedded backticks doubled)."""
    if not name or len(name) > 64 or "\x00" in name:
        raise ValueError("invalid database name")
    return "`" + name.replace("`", "``") + "`"


def ensure_database(
    settings: Settings, *, connect: Callable[..., Any] | None = None
) -> tuple[bool, str]:
    """Best-effort ``CREATE DATABASE IF NOT EXISTS <TIDB_DATABASE>``: ``(ok, message)``.

    Only for ``TIDB_*`` configuration (returns ``(False, ...)`` without connecting when
    ``TIDB_HOST`` is unset or ``TACTIDOSE_DATABASE_URL`` overrides it). ``connect``
    replaces ``pymysql.connect`` (tests). Never raises.
    """
    if settings.database_url:
        return False, "skipped: TACTIDOSE_DATABASE_URL overrides the TIDB_* settings"
    if not settings.tidb_host:
        return False, "TiDB is not configured (TIDB_HOST is empty); using SQLite"
    name = settings.tidb_database
    try:
        stmt = f"CREATE DATABASE IF NOT EXISTS {quote_identifier(name)}"
    except ValueError as exc:
        return False, f"invalid TIDB_DATABASE: {exc}"
    if connect is None:
        try:
            import pymysql
        except ImportError:
            return False, "pymysql is not installed (pip install 'tactidose[tidb]')"
        connect = pymysql.connect
    kwargs: dict[str, Any] = {
        "host": settings.tidb_host,
        "port": settings.tidb_port,
        "user": settings.tidb_user,
        "password": _password(settings) or "",
        "charset": "utf8mb4",
        "autocommit": True,
        **_tidb_connect_args(settings),
    }
    conn = None
    try:
        conn = connect(**kwargs)
        cur = conn.cursor()
        try:
            cur.execute(stmt)
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        msg = f"database '{name}' is ready on {settings.tidb_host}:{settings.tidb_port}"
        log.info("TiDB: %s", msg)
        return True, msg
    except Exception as exc:  # noqa: BLE001
        msg = _short_error(exc, settings)
        log.warning("TiDB ensure_database failed: %s", msg)
        return False, msg
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                log.debug("close failed", exc_info=True)
