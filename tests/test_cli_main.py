"""tactidose/__main__.py: argument parsing, every command (heavy work monkeypatched), exit codes."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import inspect, select

import tactidose.__main__ as cli
from tactidose.auth import passwords
from tactidose.auth.service import AuthService
from tactidose.config import Settings
from tactidose.core.clock import Clock
from tactidose.db.models import AuthSession, Device, User
from tactidose.db.session import Database
from tactidose.medication.errors import NotFoundError, ValidationError

#: Keys the ``run`` command writes straight into ``os.environ``.
RUN_ENV_KEYS = ("TACTIDOSE_HARDWARE_MODE", "TACTIDOSE_SERIAL_PORT", "TACTIDOSE_VOICE_ENABLED",
                "TACTIDOSE_HOST", "TACTIDOSE_PORT")


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic CLI environment: temp data dir, no .env, no hardware/voice, fast hashing."""
    monkeypatch.chdir(tmp_path)
    for key in RUN_ENV_KEYS:              # registered so monkeypatch restores what `run` writes
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    for key, value in {
        "TACTIDOSE_DATA_DIR": str(tmp_path / "data"),
        "TACTIDOSE_HARDWARE_MODE": "none",
        "TACTIDOSE_TIMEZONE": "America/Vancouver",
        "TACTIDOSE_VOICE_ENABLED": "false",
        "TACTIDOSE_TTS_PROVIDER": "none",
        "TACTIDOSE_LABEL_EXTRACTOR": "disabled",
        "TACTIDOSE_AGENT_PROVIDER": "rules",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(passwords, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


class _Db:
    """The CLI's database, opened the same way the CLI does (from the environment)."""

    def __enter__(self) -> tuple[Settings, Database, AuthService]:
        self.settings = Settings(_env_file=None)
        self.db = Database(self.settings)
        self.db.create_all()
        return self.settings, self.db, AuthService(self.db, self.settings, Clock(self.settings.timezone))

    def __exit__(self, *exc: object) -> None:
        self.db.dispose()


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _create(capsys, monkeypatch, email: str, role: str, name: str = "Some One", *extra: str) -> int:
    _stdin(monkeypatch, "pass-word-123\n")
    code, out, err = run(capsys, "create-user", "--email", email, "--name", name, "--role", role, *extra)
    assert code == 0, err
    return int(out.split("#", 1)[1].split(":", 1)[0])


# --------------------------------------------------------------------------- parsing & errors


def test_no_command_prints_help(cli_env, capsys) -> None:
    code, _, err = run(capsys)
    assert code == 2 and "<command>" in err and "seed-demo" in err


def test_help_and_version(cli_env, capsys) -> None:
    assert run(capsys, "--help")[0] == 0
    code, out, _ = run(capsys, "--version")
    assert code == 0 and out.strip() == f"tactidose {cli.__version__}"
    code, out, _ = run(capsys, "create-user", "--help")
    assert code == 0 and "--password-stdin" in out and "--password " not in out


@pytest.mark.parametrize("argv", [
    ["frobnicate"], ["run", "--sim", "--serial", "COM5"], ["run", "--port", "0"], ["run", "--port", "x"],
    ["hw-test", "--repeat", "0"], ["generate-report", "--patient-id", "1", "--days", "0"],
    ["create-user", "--email", "a@b.co", "--name", "A", "--role", "admin"], ["link", "--patient-id", "1"],
    ["seed-demo", "--unexpected"],
])
def test_usage_errors_exit_2(cli_env, capsys, argv) -> None:
    code, _, err = run(capsys, *argv)
    assert code == 2 and "usage:" in err


def test_configuration_problems_exit_2_without_echoing_values(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TACTIDOSE_NUM_SLOTS", "99")
    monkeypatch.setenv("SMTP_PORT", "not-a-port-xyz")
    code, out, err = run(capsys, "seed-demo")
    assert code == 2 and "Configuration problem" in err
    assert "TACTIDOSE_NUM_SLOTS" in err and "SMTP_PORT" in err and "not-a-port-xyz" not in err + out


def test_split_passthrough() -> None:
    assert cli._split_passthrough(["-v", "conformance", "--target", "sim"]) == (["-v", "conformance"],
                                                                              ["--target", "sim"])
    assert cli._split_passthrough(["run", "--sim"]) == (["run", "--sim"], None)
    assert cli._split_passthrough([]) == ([], None)


def test_env_names_for_settings_errors() -> None:
    assert cli._env_name(("num_slots",)) == "TACTIDOSE_NUM_SLOTS"
    assert cli._env_name(("smtp_port",)) == "SMTP_PORT"
    assert cli._env_name(("database_url",)) == "TACTIDOSE_DATABASE_URL"


def test_unexpected_errors_are_redacted(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret-987654")

    def boom(args):
        cli._settings()
        raise RuntimeError("login failed with smtp-secret-987654")

    monkeypatch.setattr(cli, "_cmd_ports", boom)
    code, out, err = run(capsys, "ports")
    assert code == 1 and "RuntimeError" in err and "***" in err and "smtp-secret-987654" not in out + err


@pytest.mark.parametrize("exc,expected", [
    (ValidationError("bad input"), 2), (NotFoundError("missing"), 1), (KeyboardInterrupt(), 1),
    (cli.CliError("custom", 7), 7),
])
def test_exception_mapping(cli_env, capsys, monkeypatch, exc, expected) -> None:
    def fail(args):
        raise exc

    monkeypatch.setattr(cli, "_cmd_ports", fail)
    code, _, err = run(capsys, "ports")
    assert code == expected and err.strip()


def test_python_dash_m_entry_point(cli_env) -> None:
    proc = subprocess.run([sys.executable, "-m", "tactidose", "--version"], capture_output=True, text=True,
                          timeout=120, cwd=cli_env, check=False)
    assert proc.returncode == 0 and proc.stdout.strip() == f"tactidose {cli.__version__}"


# --------------------------------------------------------------------------- run


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list[tuple[tuple, dict]]:
    import uvicorn

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(cli, "_app_available", lambda: True)
    return calls


def test_run_starts_uvicorn_with_the_app_factory(cli_env, capsys, uvicorn_calls) -> None:
    code, out, err = run(capsys, "run", "--sim", "--port", "8123", "--no-voice")
    assert code == 0
    ((args, kwargs),) = uvicorn_calls
    assert args == ("tactidose.app:create_app",)
    assert kwargs["factory"] is True and kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8123
    assert kwargs["lifespan"] == "on" and kwargs["log_level"] == "info"
    assert os.environ["TACTIDOSE_HARDWARE_MODE"] == "sim" and os.environ["TACTIDOSE_VOICE_ENABLED"] == "false"
    assert os.environ["TACTIDOSE_PORT"] == "8123"
    assert "http://127.0.0.1:8123" in out and "hardware: sim" in out and "not a medical device" in out
    assert "Warning" not in err


def test_run_serial_on_the_network_warns_in_demo_mode(cli_env, capsys, uvicorn_calls, monkeypatch) -> None:
    monkeypatch.setenv("TACTIDOSE_LOG_LEVEL", "DEBUG")
    code, out, err = run(capsys, "run", "--serial", "COM7", "--host", "0.0.0.0")
    assert code == 0 and os.environ["TACTIDOSE_SERIAL_PORT"] == "COM7"
    assert os.environ["TACTIDOSE_HARDWARE_MODE"] == "serial" and "hardware: serial on COM7" in out
    assert uvicorn_calls[0][1]["host"] == "0.0.0.0" and uvicorn_calls[0][1]["log_level"] == "debug"
    assert "Warning: demo mode is on" in err


def test_run_without_hardware_uses_settings_defaults(cli_env, capsys, uvicorn_calls, monkeypatch) -> None:
    monkeypatch.setenv("TACTIDOSE_PORT", "9001")
    monkeypatch.setenv("TACTIDOSE_LOG_LEVEL", "LOUD")
    assert run(capsys, "run", "--no-hardware")[0] == 0
    assert os.environ["TACTIDOSE_HARDWARE_MODE"] == "none"
    assert uvicorn_calls[0][1]["port"] == 9001 and uvicorn_calls[0][1]["log_level"] == "info"


def test_run_reports_a_missing_app_module(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_app_available", lambda: False)
    code, _, err = run(capsys, "run")
    assert code == 1 and "tactidose/app.py" in err


# --------------------------------------------------------------------------- database & demo


def test_init_db(cli_env, capsys) -> None:
    code, out, _ = run(capsys, "init-db")
    assert code == 0 and "Tables are ready" in out and "SQLite" in out
    with _Db() as (_, db, _auth):
        assert {"users", "auth_sessions", "pill_drops", "reports"} <= set(inspect(db.engine).get_table_names())


def test_init_db_failure_exits_1(cli_env, capsys, monkeypatch) -> None:
    bad = (cli_env / "missing" / "deeper" / "x.db").as_posix()
    monkeypatch.setenv("TACTIDOSE_DATABASE_URL", f"sqlite:///{bad}")
    code, _, err = run(capsys, "init-db")
    assert code == 1 and "Database problem" in err


def test_seed_demo_command(cli_env, capsys) -> None:
    code, out, err = run(capsys, "seed-demo")
    assert code == 0, err
    for text in ("Alex Rivera", "sam@demo.tactidose", "dr.lee@demo.tactidose", "Password: demo1234",
                 "Container 3: Omega-3 (demo candy), 3 pills (low)", "Schedules: 08:00, 13:00, 20:00 every day",
                 "Cooldown: 60 minutes"):
        assert text in out, text
    with _Db() as (_, _db, auth):
        alex = auth.get_user_by_email("alex@demo.tactidose")
        link_code = auth.patient_profile(alex.user_id)["link_code"]
        assert auth.login("alex@demo.tactidose", "demo1234")
    assert link_code not in out + err
    code, out, _ = run(capsys, "seed-demo")
    assert code == 0 and "Created now: 0 users, 0 links, 0 medications, 0 schedules." in out


def test_seed_demo_never_prints_a_custom_password(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TACTIDOSE_DEMO_PASSWORD", "custom-demo-pass")
    monkeypatch.setenv("TACTIDOSE_DEMO_MODE", "false")
    code, out, err = run(capsys, "seed-demo")
    assert code == 0 and "custom-demo-pass" not in out + err
    assert "the value of TACTIDOSE_DEMO_PASSWORD" in out and "TACTIDOSE_DEMO_MODE is false" in err


def test_seed_demo_with_a_short_password_is_a_config_problem(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TACTIDOSE_DEMO_PASSWORD", "tiny")
    code, _, err = run(capsys, "seed-demo")
    assert code == 2 and "TACTIDOSE_DEMO_PASSWORD" in err and "tiny" not in err


def test_seed_demo_warns_when_another_patient_owns_the_device(cli_env, capsys, monkeypatch) -> None:
    _create(capsys, monkeypatch, "real@example.com", "patient", "Real Patient")
    code, _, err = run(capsys, "seed-demo")
    assert code == 0 and "belongs to another patient" in err and "reset-demo --yes" in err


def test_reset_demo_requires_confirmation(cli_env, capsys, monkeypatch) -> None:
    assert run(capsys, "seed-demo")[0] == 0
    code, _, err = run(capsys, "reset-demo")
    assert code == 2 and "--yes" in err
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    answers = iter(["no"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, out, _ = run(capsys, "reset-demo")
    assert code == 1 and "Cancelled" in out

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run(capsys, "reset-demo")[0] == 2
    monkeypatch.setattr("builtins.input", lambda prompt="": "RESET ")
    code, out, _ = run(capsys, "reset-demo")
    assert code == 0 and "Demo data reset" in out


def test_reset_demo_wipes_sessions_unless_kept(cli_env, capsys) -> None:
    assert run(capsys, "seed-demo")[0] == 0
    with _Db() as (_, db, auth):
        _, token = auth.login("alex@demo.tactidose", "demo1234")
    code, out, _ = run(capsys, "reset-demo", "--yes", "--keep-sessions")
    assert code == 0 and "Deleted: nothing." in out
    with _Db() as (_, db, auth):
        assert auth.resolve(token) is not None
    code, out, _ = run(capsys, "reset-demo", "--yes")
    assert code == 0 and "1 auth_sessions" in out and "restart it" in out
    with _Db() as (_, db, auth):
        assert auth.resolve(token) is None
        with db.session() as s:
            assert s.scalars(select(AuthSession)).all() == []


def test_reset_demo_only_in_demo_mode(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TACTIDOSE_DEMO_MODE", "false")
    code, _, err = run(capsys, "reset-demo", "--yes")
    assert code == 2 and "demo mode" in err


# --------------------------------------------------------------------------- accounts


def test_create_user_reads_the_password_from_stdin(cli_env, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "s3cret-pass-word\n")
    code, out, err = run(capsys, "create-user", "--email", "Doc@Example.com", "--name", "Dr. Who", "--role", "Doctor")
    assert code == 0 and "Created doctor account #1: Dr. Who <doc@example.com>." in out
    assert "s3cret-pass-word" not in out + err and "standard input" in err
    with _Db() as (_, _db, auth):
        assert auth.login("doc@example.com", "s3cret-pass-word")[0].role == "doctor"


def test_create_user_prompts_twice_on_a_terminal(cli_env, capsys, monkeypatch) -> None:
    import getpass

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    answers = iter(["typed-pass-1", "typed-pass-1", "first-pass-1", "other-pass-2"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    code, out, _ = run(capsys, "create-user", "--email", "pat@example.com", "--name", "Pat", "--role", "patient")
    assert code == 0 and "Device tactidose-001 is bound to this patient." in out
    code, _, err = run(capsys, "create-user", "--email", "sam@example.com", "--name", "Sam", "--role", "family")
    assert code == 2 and "do not match" in err
    _stdin(monkeypatch, "piped-pass-123\n")
    code, _, _ = run(capsys, "create-user", "--email", "sam@example.com", "--name", "Sam", "--role", "family",
                     "--password-stdin")
    assert code == 0


def test_create_user_refuses_passwords_on_the_command_line(cli_env, capsys) -> None:
    code, out, err = run(capsys, "create-user", "--email", "a@example.com", "--name", "A", "--role", "doctor",
                         "--password", "visible-secret")
    assert code == 2 and "not accepted on the command line" in err and "visible-secret" not in out + err
    with _Db() as (_, db, _auth), db.session() as s:
        assert s.scalars(select(User)).all() == []


@pytest.mark.parametrize("stdin,argv,expected", [
    ("pass-word-123\n", ["--email", "not-an-email"], 2),
    ("short\n", ["--email", "a@example.com"], 2),
    ("\n", ["--email", "a@example.com"], 2),
])
def test_create_user_input_problems(cli_env, capsys, monkeypatch, stdin, argv, expected) -> None:
    _stdin(monkeypatch, stdin)
    code, _, err = run(capsys, "create-user", "--name", "A", "--role", "doctor", *argv)
    assert code == expected and err.strip()


def test_create_user_duplicate_and_device_rules(cli_env, capsys, monkeypatch) -> None:
    first = _create(capsys, monkeypatch, "pat@example.com", "patient", "Pat")
    _stdin(monkeypatch, "pass-word-123\n")
    code, _, err = run(capsys, "create-user", "--email", "PAT@example.com", "--name", "P", "--role", "doctor")
    assert code == 1 and "already exists" in err
    _stdin(monkeypatch, "pass-word-123\n")
    code, out, _ = run(capsys, "create-user", "--email", "two@example.com", "--name", "Two", "--role", "patient")
    assert code == 0 and "No device is bound to this patient" in out
    with _Db() as (settings, db, _auth), db.session() as s:
        assert s.get(Device, settings.device_id).user_id == first


def test_create_patient_without_device(cli_env, capsys, monkeypatch) -> None:
    _create(capsys, monkeypatch, "pat@example.com", "patient", "Pat", "--no-device")
    with _Db() as (settings, db, _auth), db.session() as s:
        assert s.get(Device, settings.device_id) is None


def test_link_command(cli_env, capsys, monkeypatch) -> None:
    assert run(capsys, "seed-demo")[0] == 0
    doctor = _create(capsys, monkeypatch, "newdoc@example.com", "doctor", "New Doc")
    code, out, _ = run(capsys, "link", "--caregiver-email", "NewDoc@example.com", "--patient-id", "1")
    assert code == 0 and out.strip() == "Linked New Doc (doctor) to patient #1 Alex Rivera."
    with _Db() as (_, _db, auth):
        assert auth.can_edit(auth.get_user(doctor), 1)
    assert run(capsys, "link", "--caregiver-email", "nobody@example.com", "--patient-id", "1")[0] == 1
    assert run(capsys, "link", "--caregiver-email", "newdoc@example.com", "--patient-id", "3")[0] == 1
    assert run(capsys, "link", "--caregiver-email", "alex@demo.tactidose", "--patient-id", "1")[0] == 2


def test_bind_device_command(cli_env, capsys, monkeypatch) -> None:
    assert run(capsys, "seed-demo")[0] == 0
    newcomer = _create(capsys, monkeypatch, "new@example.com", "patient", "New Patient")
    code, out, _ = run(capsys, "bind-device", "--patient-id", str(newcomer))
    assert code == 0 and f"now bound to patient #{newcomer} (it belonged to patient #1)" in out
    assert "3 container(s) held another patient's medication" in out
    code, out, _ = run(capsys, "bind-device", "--patient-id", str(newcomer))
    assert code == 0 and "already bound" in out
    assert run(capsys, "bind-device", "--patient-id", "999")[0] == 1
    assert run(capsys, "bind-device", "--patient-id", "3")[0] == 2      # Dr. Lee is not a patient


# --------------------------------------------------------------------------- hardware commands


def test_simulator_command(cli_env, capsys, monkeypatch) -> None:
    from tactidose.hardware import transports

    seen: dict = {}

    def fake(settings, host, port, stop_event, *, on_ready=None, device=None):
        seen.update(host=host, port=port, stop=stop_event, slots=settings.num_slots)
        on_ready(host, 7788)

    monkeypatch.setattr(transports, "serve_simulator_tcp", fake)
    code, out, _ = run(capsys, "simulator", "--port", "7788")
    assert code == 0 and "socket://127.0.0.1:7788" in out and "TACTIDOSE_SERIAL_PORT=socket://" in out
    assert seen["port"] == 7788 and seen["host"] == "127.0.0.1" and seen["stop"].is_set()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(transports, "serve_simulator_tcp", interrupted)
    code, out, _ = run(capsys, "simulator")
    assert code == 0 and "Simulator stopped." in out

    def in_use(*args, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(transports, "serve_simulator_tcp", in_use)
    code, _, err = run(capsys, "simulator", "--host", "0.0.0.0", "--port", "0")
    assert code == 1 and "address already in use" in err


def test_hw_test_command(cli_env, capsys, monkeypatch) -> None:
    from tactidose.hardware import selftest

    seen: dict = {}

    def fake(settings, *, port=None, repeat=1, interactive=False, **kwargs):
        seen.update(port=port, repeat=repeat, interactive=interactive)
        return 2

    monkeypatch.setattr(selftest, "run_hw_test", fake)
    assert run(capsys, "hw-test", "--port", "COM9", "--repeat", "3", "--interactive")[0] == 2
    assert seen == {"port": "COM9", "repeat": 3, "interactive": True}
    monkeypatch.setattr(selftest, "run_hw_test", lambda settings, **kw: 0)
    assert run(capsys, "hw-test")[0] == 0


def test_conformance_passes_arguments_through(cli_env, capsys, monkeypatch) -> None:
    from tactidose.hardware import conformance

    code, out, _ = run(capsys, "conformance", "--help")      # the real runner's own parser
    assert code == 0 and "--target" in out
    seen: list = []
    monkeypatch.setattr(conformance, "main", lambda argv=None: seen.append(argv) or 1)
    assert run(capsys, "conformance", "--target", "sim", "--scenario", "drop_ok", "--no-slow")[0] == 1
    assert run(capsys, "-v", "conformance")[0] == 1
    assert seen == [["--target", "sim", "--scenario", "drop_ok", "--no-slow"], []]

    def exits(argv=None):
        raise SystemExit(2)

    monkeypatch.setattr(conformance, "main", exits)
    assert run(capsys, "conformance", "--bogus")[0] == 2


def test_ports_command(cli_env, capsys, monkeypatch) -> None:
    from tactidose.hardware import ports

    monkeypatch.setattr(ports, "format_ports", lambda ports=None: "* COM5  10C4:EA60  CP2102 USB to UART")
    code, out, _ = run(capsys, "ports")
    assert code == 0 and "COM5" in out


# --------------------------------------------------------------------------- doctor


@pytest.fixture
def doctor_fakes(cli_env, monkeypatch):
    from tactidose.hardware import conformance_native, ports

    monkeypatch.setattr(ports, "format_ports", lambda ports=None: "  COM3  -  Intel(R) AMT SOL")
    monkeypatch.setattr(ports, "describe_ports", lambda ports=None: [{"device": "COM3", "auto_selected": False}])
    devices = [{"name": "Desk Mic", "max_input_channels": 1, "max_output_channels": 0},
               {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2}]

    def query_devices(device=None, kind=None):
        return {"input": devices[0], "output": devices[1]}.get(kind, devices)

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(query_devices=query_devices))
    harness = cli_env / "harness"
    harness.write_bytes(b"\x7fELF")
    monkeypatch.setattr(conformance_native, "resolve_binary", lambda binary=None: harness)
    monkeypatch.setattr(conformance_native, "harness_is_stale", lambda binary=None: False)
    monkeypatch.setattr(conformance_native, "docker_unavailable_reason", lambda **kw: None)
    return harness


def test_doctor_reports_without_secrets(doctor_fakes, capsys, monkeypatch) -> None:
    secrets = {"GEMINI_API_KEY": "gem-secret-123456", "SMTP_PASSWORD": "smtp-secret-987654",
               "ELEVENLABS_API_KEY": "eleven-secret-555", "SNOWFLAKE_PASSWORD": "snow-secret-777"}
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "tactidose@example.com")
    code, out, err = run(capsys, "doctor", "--docker")
    assert code == 0, out + err
    for text in ("[OK  ] Database: Connected to SQLite", "device_id: tactidose-001", "Serial ports: hardware mode none",
                 "COM3", "[INFO] Voice model: no Vosk model", "download-voice-model",
                 "1 input (microphone) and 1 output (speaker) device(s)", "default input: Desk Mic",
                 "Gemini: configured", "SMTP e-mail: configured (smtp.example.com:587, STARTTLS)",
                 "ElevenLabs voice: configured", "TiDB: not configured", "up to date", "docker: ok",
                 "test the keys live on this network (tiny real requests): python -m tactidose check-apis",
                 "All essential checks passed."):
        assert text in out, text
    for value in secrets.values():
        assert value not in out + err


def test_doctor_fails_when_the_database_is_unreachable(doctor_fakes, capsys, monkeypatch) -> None:
    from tactidose.integrations import tidb

    monkeypatch.setattr(tidb, "check_connection", lambda settings, **kw: (False, "Connection refused", None))
    code, out, _ = run(capsys, "doctor")
    assert code == 1 and "[FAIL] Database: Connection refused" in out and "1 essential check(s) failed." in out


def test_doctor_warnings(doctor_fakes, capsys, monkeypatch) -> None:
    from tactidose.hardware import conformance_native

    monkeypatch.setenv("TACTIDOSE_HARDWARE_MODE", "serial")
    monkeypatch.setenv("TACTIDOSE_VOICE_ENABLED", "true")
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setattr(conformance_native, "harness_is_stale", lambda binary=None: True)
    code, out, _ = run(capsys, "doctor")
    assert code == 0
    for text in ("[WARN] Serial ports", "no ESP32 found", "[WARN] Voice model", "[WARN] Audio devices",
                 "sounddevice is unavailable", "[WARN] Native firmware harness", "rebuild it"):
        assert text in out, text


def test_doctor_survives_a_crashing_check(doctor_fakes, capsys, monkeypatch) -> None:
    def broken(settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_check_voice_model", broken)
    code, out, _ = run(capsys, "doctor")
    assert code == 1 and "[FAIL] Voice model: check failed: RuntimeError: boom" in out
    assert "[INFO] Services" in out


def test_doctor_native_harness_not_built(doctor_fakes, capsys, monkeypatch) -> None:
    from tactidose.hardware import conformance_native

    monkeypatch.setattr(conformance_native, "resolve_binary", lambda binary=None: doctor_fakes.parent / "nope")
    code, out, _ = run(capsys, "doctor")
    assert code == 0 and "[INFO] Native firmware harness: not built" in out


# --------------------------------------------------------------------------- voice / speech


def test_download_voice_model(cli_env, capsys, monkeypatch) -> None:
    from tactidose.voice import recognizer

    seen: dict = {}

    def fake(dest, name, url=None, progress=None, *, timeout_s=60.0):
        seen.update(dest=Path(dest), name=name)
        progress(50, 100)
        progress(100, 100)
        return Path(dest) / name

    monkeypatch.setattr(recognizer, "download_model", fake)
    code, out, err = run(capsys, "download-voice-model", "--dest", str(cli_env / "elsewhere"))
    assert code == 0 and seen == {"dest": cli_env / "elsewhere", "name": "vosk-model-small-en-us-0.15"}
    assert "50%" in err and "100%" in err and "TACTIDOSE_VOSK_MODEL_PATH=" in out
    code, out, _ = run(capsys, "download-voice-model")
    assert code == 0 and seen["dest"] == Path("models") and "TACTIDOSE_VOSK_MODEL_PATH" not in out


def test_download_voice_model_already_installed_or_failing(cli_env, capsys, monkeypatch) -> None:
    from tactidose.voice import recognizer

    def must_not_download(*args, **kwargs):
        raise AssertionError("no download expected")

    monkeypatch.setattr(recognizer, "download_model", must_not_download)
    monkeypatch.setattr(recognizer, "looks_like_vosk_model", lambda path: True)
    code, out, _ = run(capsys, "download-voice-model")
    assert code == 0 and "already installed" in out

    def failing(*args, **kwargs):
        raise RuntimeError("download failed: HTTP 404")

    monkeypatch.setattr(recognizer, "looks_like_vosk_model", lambda path: False)
    monkeypatch.setattr(recognizer, "download_model", failing)
    code, _, err = run(capsys, "download-voice-model")
    assert code == 1 and "HTTP 404" in err


class _FakeSpeaker:
    instances: ClassVar[list[_FakeSpeaker]] = []
    result: ClassVar[dict] = {}

    def __init__(self, settings, bus, **kwargs):
        self.closed = False
        _FakeSpeaker.instances.append(self)

    def warm_cache(self, texts=None):
        return dict(_FakeSpeaker.result)

    def close(self) -> None:
        self.closed = True


def test_warm_tts_cache(cli_env, capsys, monkeypatch) -> None:
    from tactidose.audio import speaker

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret-555")
    monkeypatch.setattr(speaker, "SpeakerService", _FakeSpeaker)
    _FakeSpeaker.result = {"rendered": 3, "cached": 2, "failed": 0, "total": 5, "provider": "offline", "errors": []}
    code, out, _ = run(capsys, "warm-tts-cache")
    assert code == 0 and "Speech cache (offline): 3 rendered, 2 already cached, 0 failed, 5 phrases." in out
    assert _FakeSpeaker.instances[-1].closed
    _FakeSpeaker.result = {"rendered": 0, "cached": 0, "failed": 2, "total": 2, "provider": "elevenlabs",
                           "errors": ["ElevenLabs HTTP 401 for key eleven-secret-555"]}
    code, out, _ = run(capsys, "warm-tts-cache")
    assert code == 1 and "HTTP 401" in out and "eleven-secret-555" not in out


# --------------------------------------------------------------------------- reports & e-mail


class _FakeReports:
    made: ClassVar[list[dict]] = []
    meta: ClassVar[dict] = {}

    def __init__(self, db, clock, settings, *, auth=None, notifications=None, bus=None):
        self.calls: list[dict] = []
        _FakeReports.made.append({"auth": auth, "notifications": notifications, "bus": bus, "self": self})

    def generate(self, *, patient_id, days, created_by_user_id):
        self.calls.append({"patient_id": patient_id, "days": days, "created_by_user_id": created_by_user_id})
        return dict(_FakeReports.meta, report_id=7)

    def pdf_bytes(self, report_id):
        assert report_id == 7
        return b"%PDF-1.4 fake"


@pytest.fixture
def fake_reports(cli_env, monkeypatch):
    module = types.ModuleType("tactidose.reports.service")
    module.ReportService = _FakeReports
    monkeypatch.setitem(sys.modules, "tactidose.reports.service", module)
    notifications = types.ModuleType("tactidose.medication.notifications")
    notifications.NotificationService = lambda db, settings, clock, bus=None: ("notifications", bus)
    monkeypatch.setitem(sys.modules, "tactidose.medication.notifications", notifications)
    _FakeReports.made = []
    _FakeReports.meta = {"title": "TactiDose report - Alex Rivera - last 7 days", "status": "READY", "pdf_size": 13}
    return _FakeReports


def test_generate_report(fake_reports, cli_env, capsys) -> None:
    assert run(capsys, "seed-demo")[0] == 0
    out_file = cli_env / "reports" / "alex.pdf"
    code, out, err = run(capsys, "generate-report", "--patient-id", "1", "--days", "14", "--out", str(out_file))
    assert code == 0, err
    assert "Report #7 (READY): TactiDose report - Alex Rivera - last 7 days, 13 bytes." in out
    assert out_file.read_bytes() == b"%PDF-1.4 fake"
    made = fake_reports.made[-1]
    assert isinstance(made["auth"], AuthService) and made["notifications"] == ("notifications", None)
    assert made["self"].calls == [{"patient_id": 1, "days": 14, "created_by_user_id": 1}]
    code, _, _ = run(capsys, "generate-report", "--patient-id", "1", "--by-email", "dr.lee@demo.tactidose")
    assert code == 0 and fake_reports.made[-1]["self"].calls[0]["created_by_user_id"] == 3


def test_generate_report_problems(fake_reports, cli_env, capsys, monkeypatch) -> None:
    assert run(capsys, "seed-demo")[0] == 0
    _create(capsys, monkeypatch, "stranger@example.com", "doctor", "Stranger")
    assert run(capsys, "generate-report", "--patient-id", "1", "--days", "91")[0] == 2
    assert run(capsys, "generate-report", "--patient-id", "99")[0] == 1
    assert run(capsys, "generate-report", "--patient-id", "3")[0] == 1          # a doctor, not a patient
    code, _, err = run(capsys, "generate-report", "--patient-id", "1", "--by-email", "stranger@example.com")
    assert code == 1 and "not the patient or a doctor/family member linked" in err
    fake_reports.meta = {"title": "t", "status": "FAILED", "error": "fpdf2 missing", "pdf_size": 0}
    code, _, err = run(capsys, "generate-report", "--patient-id", "1")
    assert code == 1 and "fpdf2 missing" in err
    monkeypatch.setitem(sys.modules, "tactidose.reports.service", None)
    code, _, err = run(capsys, "generate-report", "--patient-id", "1")
    assert code == 1 and "reports module is not available" in err


def _mailer(monkeypatch, **attrs) -> None:
    module = types.ModuleType("tactidose.reports.mailer")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "tactidose.reports.mailer", module)


def test_send_test_email(cli_env, capsys, monkeypatch) -> None:
    seen: list = []

    def send_test_email(settings, to_email):
        seen.append(to_email)
        return {"status": "SAVED", "path": str(settings.outbox_dir / "test-1.eml")}

    _mailer(monkeypatch, send_test_email=send_test_email)
    code, out, _ = run(capsys, "send-test-email", "--to", " Dr.Lee@Example.com ")
    assert code == 0 and seen == ["dr.lee@example.com"]
    assert "Test e-mail to dr.lee@example.com: SAVED" in out and "test-1.eml" in out and "saved as an .eml" in out


def test_send_test_email_variants(cli_env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret-987654")
    _mailer(monkeypatch, send_test_email=lambda settings, to: types.SimpleNamespace(
        status="failed", path=None, error="535 auth failed for smtp-secret-987654"))
    code, out, _ = run(capsys, "send-test-email", "--to", "a@example.com")
    assert code == 1 and "FAILED" in out and "smtp-secret-987654" not in out

    sent: list[dict] = []

    def send_report_email(settings, *, to, subject, body, pdf_bytes, filename, **kwargs):
        sent.append({"to": to, "subject": subject, "body": body, "pdf": pdf_bytes, "filename": filename})
        return types.SimpleNamespace(status=types.SimpleNamespace(value="sent"), path=None, error=None)

    _mailer(monkeypatch, send_report_email=send_report_email)
    code, out, _ = run(capsys, "send-test-email", "--to", "a@example.com")
    assert code == 0 and "SENT" in out
    (mail,) = sent
    assert mail["to"] == "a@example.com" and mail["subject"] == "TactiDose test e-mail"
    assert mail["pdf"].startswith(b"%PDF") and mail["filename"].endswith(".pdf")
    from pypdf import PdfReader

    pages = PdfReader(io.BytesIO(mail["pdf"])).pages
    assert len(pages) == 1 and "TactiDose test e-mail" in pages[0].extract_text()
    assert "not a medical device" in mail["body"]
    _mailer(monkeypatch)
    code, _, err = run(capsys, "send-test-email", "--to", "a@example.com")
    assert code == 1 and "send_report_email" in err
    assert run(capsys, "send-test-email", "--to", "not-an-address")[0] == 2
    monkeypatch.setitem(sys.modules, "tactidose.reports.mailer", None)
    code, _, err = run(capsys, "send-test-email", "--to", "a@example.com")
    assert code == 1 and "mailer is not available" in err
