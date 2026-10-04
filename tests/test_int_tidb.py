"""Tests for tactidose.integrations.tidb: SQLite fallback for real, TiDB paths with fakes /
a mocked ``pymysql.connect`` (no network)."""

from __future__ import annotations

import json
from typing import Any

import certifi
import pymysql
import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from tactidose.config import Settings
from tactidose.db.session import CA_BUNDLE_ENV_VARS, Database, tidb_connect_args, tidb_ssl_ca
from tactidose.integrations import tidb

PASSWORD = "tidb-PASS-secret-1"


@pytest.fixture(autouse=True)
def _no_ca_bundle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Machines behind a TLS-inspecting proxy set these; the CA tests below set them explicitly."""
    for name in CA_BUNDLE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tidb_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={
        "tidb_host": "gateway01.us-west-2.prod.aws.tidbcloud.com",
        "tidb_port": 4000,
        "tidb_user": "2abc3def.root",
        "tidb_password": SecretStr(PASSWORD),
        "tidb_database": "tactidose",
    })


# --------------------------------------------------------------------------- fakes


class FakeResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar(self) -> Any:
        return self.value


class FakeConn:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, stmt: Any) -> FakeResult:
        self.engine.sql.append(str(stmt))
        return FakeResult(self.engine.version)


class FakeEngine:
    def __init__(self, url: str, version: str | None = None, exc: Exception | None = None) -> None:
        self.url = make_url(url)
        self.version = version
        self.exc = exc
        self.sql: list[str] = []

    def connect(self) -> FakeConn:
        if self.exc is not None:
            raise self.exc
        return FakeConn(self)


class FakeDatabase:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.disposed = False

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    def dispose(self) -> None:
        self.disposed = True


class FakePyMySQL:
    """Stands in for ``pymysql.connect``."""

    def __init__(self, exc: Exception | None = None, fail_execute: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sql: list[str] = []
        self.closed = 0
        self.exc = exc
        self.fail_execute = fail_execute

    def connect(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        fake = self

        class Cursor:
            def execute(self, sql: str) -> int:
                fake.sql.append(sql)
                if fake.fail_execute is not None:
                    raise fake.fail_execute
                return 1

            def close(self) -> None:
                pass

        class Conn:
            def cursor(self) -> Cursor:
                return Cursor()

            def close(self) -> None:
                fake.closed += 1

        return Conn()


TIDB_URL = "mysql+pymysql://2abc3def.root@gateway01.us-west-2.prod.aws.tidbcloud.com:4000/tactidose"


# --------------------------------------------------------------------------- describe


def test_describe_sqlite_default(settings):
    d = tidb.describe(settings)
    assert d["backend"] == "sqlite" and d["tidb_configured"] is False
    assert d["database"].endswith("tactidose.db") and d["tls"] is None


def test_describe_tidb_without_secrets(tidb_settings):
    d = tidb.describe(tidb_settings)
    assert d == {
        "backend": "tidb", "source": "TIDB_*", "tidb_configured": True,
        "host": "gateway01.us-west-2.prod.aws.tidbcloud.com", "port": 4000, "database": "tactidose",
        "user_configured": True, "password_configured": True, "tls": True,
        "ca_source": "certifi", "ca_path": certifi.where(),
    }
    text = json.dumps(d)
    assert PASSWORD not in text and "2abc3def.root" not in text


def test_describe_tls_variants(tidb_settings):
    custom = tidb.describe(tidb_settings.model_copy(update={"tidb_ssl_ca": "C:/certs/isrgrootx1.pem"}))
    assert custom["ca_source"] == "TIDB_SSL_CA" and custom["ca_path"] == "C:/certs/isrgrootx1.pem"
    off = tidb.describe(tidb_settings.model_copy(update={"tidb_ssl": False}))
    assert off["tls"] is False and off["ca_source"] == "disabled" and off["ca_path"] is None


def test_describe_database_url_hides_password(settings):
    s = settings.model_copy(update={"database_url": f"mysql+pymysql://app:{PASSWORD}@db.local:3306/td"})
    d = tidb.describe(s)
    assert d["backend"] == "mysql" and d["host"] == "db.local" and d["port"] == 3306
    assert d["database"] == "td" and d["password_configured"] is True and d["tidb_configured"] is False
    assert PASSWORD not in json.dumps(d)


# --------------------------------------------------------------------------- version / connection


@pytest.mark.parametrize("version,expected", [
    ("8.0.11-TiDB-v7.5.2-serverless", True),
    ("5.7.25-TiDB-v6.5.0", True),
    ("8.0.36", False),
    ("10.11.6-MariaDB", False),
    ("", False),
    (None, False),
])
def test_is_tidb_version(version, expected):
    assert tidb.is_tidb_version(version) is expected


def test_check_connection_sqlite_fallback(db, settings):
    ok, msg, version = tidb.check_connection(settings, database=db)
    assert ok and msg.startswith("Connected to SQLite")
    assert version and version[0].isdigit() and version.count(".") >= 1


def test_check_connection_builds_and_disposes_its_own_database(settings):
    ok, msg, version = tidb.check_connection(settings)
    assert ok and "SQLite" in msg and version


def test_check_connection_tidb(tidb_settings):
    db = FakeDatabase(FakeEngine(TIDB_URL, version="8.0.11-TiDB-v7.5.2-serverless"))
    ok, msg, version = tidb.check_connection(tidb_settings, database=db)  # type: ignore[arg-type]
    assert ok and version == "8.0.11-TiDB-v7.5.2-serverless"
    assert msg == "Connected to TiDB at gateway01.us-west-2.prod.aws.tidbcloud.com:4000/tactidose"
    assert db.engine.sql == ["SELECT VERSION()"]
    assert db.disposed is False                       # caller-owned database is left alone


def test_check_connection_plain_mysql(tidb_settings):
    db = FakeDatabase(FakeEngine(TIDB_URL, version="8.0.36"))
    ok, msg, version = tidb.check_connection(tidb_settings, database=db)  # type: ignore[arg-type]
    assert ok and "not TiDB" in msg and version == "8.0.36"


def test_check_connection_failure_is_redacted_with_hint(tidb_settings):
    orig = pymysql.err.OperationalError(1049, f"Unknown database 'tactidose' (pw {PASSWORD})")
    exc = OperationalError("SELECT VERSION()", {}, orig)
    db = FakeDatabase(FakeEngine(TIDB_URL, exc=exc))
    ok, msg, version = tidb.check_connection(tidb_settings, database=db)  # type: ignore[arg-type]
    assert ok is False and version is None
    assert msg.startswith("OperationalError: (1049")
    assert PASSWORD not in msg and "***" in msg
    assert "ensure_database()" in msg and "'tactidose'" in msg


def test_check_connection_never_raises_on_bad_url(settings):
    bad = settings.model_copy(update={"database_url": "nosuchdriver+x://u@h/db"})
    ok, msg, version = tidb.check_connection(bad)
    assert ok is False and version is None and msg


# --------------------------------------------------------------------------- ensure_database


def test_ensure_database_uses_session_tls_args(tidb_settings, monkeypatch):
    fake = FakePyMySQL()
    monkeypatch.setattr(pymysql, "connect", fake.connect)
    ok, msg = tidb.ensure_database(tidb_settings)
    assert ok and "tactidose" in msg and PASSWORD not in msg
    (kw,) = fake.calls
    assert kw == {
        "host": "gateway01.us-west-2.prod.aws.tidbcloud.com", "port": 4000, "user": "2abc3def.root",
        "password": PASSWORD, "charset": "utf8mb4", "autocommit": True,
        "connect_timeout": 10, "read_timeout": 15, "write_timeout": 15,
        "ssl_verify_cert": True, "ssl_verify_identity": True, "ssl_ca": certifi.where(),
    }
    assert "database" not in kw and "db" not in kw         # server-level connection
    assert fake.sql == ["CREATE DATABASE IF NOT EXISTS `tactidose`"]
    assert fake.closed == 1


def test_ensure_database_custom_ca_and_no_tls(tidb_settings):
    fake = FakePyMySQL()
    tidb.ensure_database(tidb_settings.model_copy(update={"tidb_ssl_ca": "C:/certs/ca.pem"}), connect=fake.connect)
    assert fake.calls[-1]["ssl_ca"] == "C:/certs/ca.pem"
    tidb.ensure_database(tidb_settings.model_copy(update={"tidb_ssl": False}), connect=fake.connect)
    assert not any(k.startswith("ssl") for k in fake.calls[-1])


# --------------------------------------------------------------------------- CA bundle


def _bundle(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_text("-----BEGIN CERTIFICATE-----\nnot a real certificate\n-----END CERTIFICATE-----\n")
    return str(path)


def test_ca_bundle_env_order_and_certifi_default(tidb_settings, tmp_path, monkeypatch):
    assert CA_BUNDLE_ENV_VARS == ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
    assert tidb_ssl_ca(tidb_settings) == ("certifi", certifi.where())
    requests_ca = _bundle(tmp_path, "requests.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", requests_ca)
    assert tidb_ssl_ca(tidb_settings) == ("REQUESTS_CA_BUNDLE", requests_ca)
    ssl_cert = _bundle(tmp_path, "proxy bundle.pem")         # first existing file wins
    monkeypatch.setenv("SSL_CERT_FILE", f'  "{ssl_cert}" ')   # quotes / spaces from a hand-set variable
    assert tidb_ssl_ca(tidb_settings) == ("SSL_CERT_FILE", ssl_cert)
    assert tidb_connect_args(tidb_settings)["ssl_ca"] == ssl_cert


@pytest.mark.parametrize("value", ["", "   ", "missing.pem", "dir"])
def test_ca_bundle_env_ignores_blank_missing_or_directory(tidb_settings, tmp_path, monkeypatch, value):
    (tmp_path / "dir").mkdir()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / value) if value.strip() else value)
    requests_ca = _bundle(tmp_path, "requests.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", requests_ca)
    assert tidb_ssl_ca(tidb_settings) == ("REQUESTS_CA_BUNDLE", requests_ca)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "gone.pem"))
    assert tidb_ssl_ca(tidb_settings) == ("certifi", certifi.where())


def test_tidb_ssl_ca_setting_wins_and_tls_off_ignores_env(tidb_settings, tmp_path, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", _bundle(tmp_path, "proxy.pem"))
    custom = tidb_settings.model_copy(update={"tidb_ssl_ca": "C:/certs/isrgrootx1.pem"})
    assert tidb_ssl_ca(custom) == ("TIDB_SSL_CA", "C:/certs/isrgrootx1.pem")
    assert tidb_connect_args(custom)["ssl_ca"] == "C:/certs/isrgrootx1.pem"
    off = tidb_settings.model_copy(update={"tidb_ssl": False})
    assert tidb_ssl_ca(off) == ("disabled", None)
    assert not any(k.startswith("ssl") for k in tidb_connect_args(off))


def test_ensure_database_uses_the_env_ca_bundle(tidb_settings, tmp_path, monkeypatch):
    """The bootstrap connection (init-db) and the engine share the CA choice."""
    proxy_ca = _bundle(tmp_path, "proxy.pem")
    monkeypatch.setenv("SSL_CERT_FILE", proxy_ca)
    fake = FakePyMySQL()
    ok, _ = tidb.ensure_database(tidb_settings, connect=fake.connect)
    assert ok and fake.calls[-1]["ssl_ca"] == proxy_ca
    assert fake.calls[-1]["ssl_verify_cert"] is True and fake.calls[-1]["ssl_verify_identity"] is True


def test_ensure_database_quotes_name(tidb_settings):
    fake = FakePyMySQL()
    ok, _ = tidb.ensure_database(tidb_settings.model_copy(update={"tidb_database": "td`x"}), connect=fake.connect)
    assert ok and fake.sql == ["CREATE DATABASE IF NOT EXISTS `td``x`"]
    for bad in ("", "x" * 65, "a\x00b"):
        ok, msg = tidb.ensure_database(tidb_settings.model_copy(update={"tidb_database": bad}), connect=fake.connect)
        assert ok is False and msg.startswith("invalid TIDB_DATABASE")
    assert len(fake.calls) == 1


def test_ensure_database_not_configured_or_overridden(settings, tidb_settings):
    fake = FakePyMySQL()
    ok, msg = tidb.ensure_database(settings, connect=fake.connect)
    assert ok is False and "not configured" in msg
    overridden = tidb_settings.model_copy(update={"database_url": "sqlite:///x.db"})
    ok, msg = tidb.ensure_database(overridden, connect=fake.connect)
    assert ok is False and msg.startswith("skipped")
    assert fake.calls == []


@pytest.mark.parametrize("kind", ["connect", "execute"])
def test_ensure_database_errors_are_redacted(tidb_settings, kind):
    err = pymysql.err.OperationalError(2003, f"Can't connect to MySQL server (password={PASSWORD})")
    fake = FakePyMySQL(exc=err) if kind == "connect" else FakePyMySQL(fail_execute=err)
    ok, msg = tidb.ensure_database(tidb_settings, connect=fake.connect)
    assert ok is False and msg.startswith("OperationalError: (2003")
    assert PASSWORD not in msg and "***" in msg
    assert fake.closed == (0 if kind == "connect" else 1)


def test_quote_identifier():
    assert tidb.quote_identifier("tactidose") == "`tactidose`"
    assert tidb.quote_identifier("a`b") == "`a``b`"
    with pytest.raises(ValueError):
        tidb.quote_identifier("")


def test_database_session_still_creates_sqlite_schema(settings):
    """Sanity: the helpers use the same Database class the app uses (SQLite fallback)."""
    db = Database(settings)
    try:
        db.create_all()
        assert db.healthcheck() == (True, "sqlite")
        ok, _, _ = tidb.check_connection(settings, database=db)
        assert ok
    finally:
        db.dispose()


def test_describe_reports_a_ca_bundle_from_the_environment(tidb_settings, monkeypatch, tmp_path):
    bundle = tmp_path / "proxy-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    d = tidb.describe(tidb_settings)
    assert d["ca_source"] == "SSL_CERT_FILE" and d["ca_path"] == str(bundle)
