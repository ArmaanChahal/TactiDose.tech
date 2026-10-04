"""TactiDose command line: ``python -m tactidose <command>`` (also installed as ``tactidose``).

=====================  ==================================================================
run                    start the web app (uvicorn, factory ``tactidose.app:create_app``)
init-db                create the database (TiDB: ``CREATE DATABASE`` first) and the tables
seed-demo              create the demo accounts, device, containers and schedules (idempotent)
reset-demo             wipe drops, doses, conversations, reports, notifications, sign-ins; re-seed
create-user            create an account (password from a prompt or stdin, never an argument)
link                   link a doctor/family account to a patient (operator; no link code)
bind-device            bind the configured device to a patient
simulator              serve a simulated ESP32 on TCP (``socket://127.0.0.1:7777``)
hw-test                integration checklist on a real board
conformance            protocol scenarios (arguments go to ``tactidose.hardware.conformance``)
ports                  serial ports and the auto-detect choice
doctor                 configuration and environment check (offline)
check-apis             tiny live requests to each configured cloud service (keys, network)
download-voice-model   fetch the offline Vosk speech model
warm-tts-cache         pre-render the critical spoken phrases
guided-demo            headless MORNING / NOON / NIGHT judge demo on the simulator (scripted answers)
buzzer-test            sound the configured buzzer backend for 2 s (wiring check, docs/BUZZER.md)
generate-report        PDF report for a patient (optionally saved to a file)
send-test-email        check the report e-mail set-up
=====================  ==================================================================

Exit codes: 0 ok, 1 failure, 2 usage or configuration problem. Command options that change
the app (``run --sim`` ...) are passed to the app factory as ``TACTIDOSE_*`` environment
variables. Heavy modules are imported lazily, so every command starts quickly and works without
the optional extras. Secrets (passwords, tokens, API keys, link codes) are never printed and are
masked in error messages; the only password ever shown is the documented default demo password.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from tactidose import __version__

if TYPE_CHECKING:
    from tactidose.auth.service import AuthService
    from tactidose.config import Settings
    from tactidose.core.clock import Clock
    from tactidose.db.session import Database

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2
#: Everything after these command names is handed to the delegate unparsed.
PASSTHROUGH_COMMANDS = frozenset({"conformance"})
DISCLAIMER = "Prototype - not a medical device. Demo with candy or tokens only."
DEFAULT_DEMO_PASSWORD = "demo1234"
APP_FACTORY = "tactidose.app:create_app"
_UVICORN_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")

_current_settings: Settings | None = None


class CliError(Exception):
    """Stops a command: ``message`` goes to stderr and ``exit_code`` is returned."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


# =========================================================================== output helpers


def _write(stream: TextIO, text: str) -> None:
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError:      # e.g. a cp1252 console and a name in another script
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), file=stream, flush=True)


def _out(text: str = "") -> None:
    _write(sys.stdout, text)


def _err(text: str) -> None:
    _write(sys.stderr, text)


def _stdin_is_tty() -> bool:
    """True for an interactive terminal. On Windows ``NUL`` claims to be a TTY, so a console
    mode is required as well."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


# =========================================================================== settings / database


def _settings() -> Settings:
    """Settings from the environment and ``.env``; configuration errors exit with code 2."""
    global _current_settings
    from pydantic import ValidationError as SettingsError

    from tactidose.config import Settings, get_settings

    get_settings.cache_clear()
    try:
        settings = Settings()
    except SettingsError as exc:
        problems = "; ".join(
            f"{_env_name(err.get('loc', ()))}: {err.get('msg', 'invalid value')}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise CliError(f"Configuration problem: {problems}", EXIT_USAGE) from None
    _current_settings = settings
    return settings


def _env_name(loc: Sequence[Any]) -> str:
    """Field location -> the environment variable a person would set."""
    from tactidose.config import Settings

    name = str(loc[0]) if loc else "settings"
    field = Settings.model_fields.get(name)
    alias = getattr(field, "validation_alias", None) if field is not None else None
    choices = getattr(alias, "choices", None)
    if choices:
        return str(choices[0])
    if field is not None:
        return f"TACTIDOSE_{name.upper()}"
    return name.upper() if name.isidentifier() else name


def _secret_values(settings: Settings | None) -> list[str]:
    if settings is None:
        return []
    from pydantic import SecretStr

    values: list[str] = []
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            if raw and len(raw) >= 4:
                values.append(raw)
    if settings.database_url:
        try:
            from sqlalchemy.engine import make_url

            password = make_url(settings.database_url).password
            if password:
                values.append(str(password))
        except Exception:  # noqa: BLE001 - an unparseable URL has nothing to redact
            log.debug("database URL could not be parsed for redaction")
    return values


def _redact(text: str, settings: Settings | None = None) -> str:
    for secret in _secret_values(settings if settings is not None else _current_settings):
        text = text.replace(secret, "***")
    return text


def _describe_exc(exc: BaseException) -> str:
    inner = getattr(exc, "orig", None) or exc
    detail = " ".join(str(inner).split())
    text = f"{type(inner).__name__}: {detail}" if detail else type(inner).__name__
    return _redact(text)[:400]


def _open_database(settings: Settings, *, create: bool = True) -> Database:
    from tactidose.db.session import Database

    try:
        db = Database(settings)
        if create:
            db.create_all()
    except Exception as exc:  # noqa: BLE001 - shown to the operator, then exit 1
        raise CliError(f"Database problem: {_describe_exc(exc)}") from None
    return db


def _clock(settings: Settings) -> Clock:
    from tactidose.core.clock import Clock

    return Clock(settings.timezone)


def _auth(db: Database, settings: Settings) -> AuthService:
    from tactidose.auth.service import AuthService

    return AuthService(db, settings, _clock(settings))


# =========================================================================== run / database commands


def _app_available() -> bool:
    return importlib.util.find_spec(APP_FACTORY.split(":", 1)[0]) is not None


def _cmd_run(args: argparse.Namespace) -> int:
    overrides: dict[str, str] = {}
    if args.sim:
        overrides["TACTIDOSE_HARDWARE_MODE"] = "sim"
    elif args.serial is not None:
        overrides["TACTIDOSE_HARDWARE_MODE"] = "serial"
        overrides["TACTIDOSE_SERIAL_PORT"] = args.serial
    elif args.no_hardware:
        overrides["TACTIDOSE_HARDWARE_MODE"] = "none"
    if args.no_voice:
        overrides["TACTIDOSE_VOICE_ENABLED"] = "false"
    if args.demo_pause_seconds is not None:
        overrides["TACTIDOSE_DEMO_PAUSE_SECONDS"] = str(args.demo_pause_seconds)
    if args.host:
        overrides["TACTIDOSE_HOST"] = args.host
    if args.port is not None:
        overrides["TACTIDOSE_PORT"] = str(args.port)
    os.environ.update(overrides)          # create_app() reads its Settings from the environment
    settings = _settings()
    configured_level = logging.getLevelName(settings.log_level.upper())
    if not (args.verbose or args.quiet) and isinstance(configured_level, int):
        logging.getLogger().setLevel(configured_level)
    if not _app_available():
        raise CliError("The web app module (tactidose/app.py) is not installed.")
    try:
        import uvicorn
    except ImportError:
        raise CliError("uvicorn is not installed: pip install uvicorn") from None
    level = settings.log_level.lower()
    hw = settings.hardware_mode + (f" on {settings.serial_port}" if settings.hardware_mode == "serial" else "")
    host_text = f"[{settings.host}]" if ":" in settings.host else settings.host
    _out(f"TactiDose {__version__} on http://{host_text}:{settings.port}  "
         f"(hardware: {hw}, voice: {'on' if settings.voice_enabled else 'off'}, "
         f"demo mode: {'on' if settings.demo_mode else 'off'})")
    _out(DISCLAIMER + " Press Ctrl+C to stop.")
    if settings.demo_mode and settings.host not in _LOOPBACK:
        _err("Warning: demo mode is on and the server is reachable from the network: the demo "
             "endpoints (clock travel, simulator faults, data reset) are too.")
    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=level if level in _UVICORN_LEVELS else "info",
        lifespan="on",
        timeout_graceful_shutdown=5,
    )
    return EXIT_OK


def _cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.integrations import tidb

    if tidb.describe(settings)["backend"] == "tidb":
        ok, message = tidb.ensure_database(settings)
        _out(("" if ok else "Warning: ") + message)
    db = _open_database(settings, create=True)
    try:
        ok, message, version = tidb.check_connection(settings, database=db)
    finally:
        db.dispose()
    if not ok:
        raise CliError(f"Database check failed: {message}")
    _out(f"Tables are ready. {message}" + (f" (version {version})." if version else "."))
    return EXIT_OK


def _cmd_seed_demo(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.db.seed import seed_demo

    if not settings.demo_mode:
        _err("Note: TACTIDOSE_DEMO_MODE is false. The demo accounts use a well-known password.")
    db = _open_database(settings)
    try:
        summary = seed_demo(db, settings, _clock(settings))
    finally:
        db.dispose()
    _print_demo_summary(summary, settings)
    return EXIT_OK


def _cmd_reset_demo(args: argparse.Namespace) -> int:
    settings = _settings()
    if not settings.demo_mode:
        raise CliError("reset-demo only works in demo mode (TACTIDOSE_DEMO_MODE=true).", EXIT_USAGE)
    if not args.yes:
        if not _stdin_is_tty():
            raise CliError("reset-demo deletes data. Add --yes to confirm.", EXIT_USAGE)
        try:
            answer = input("This deletes every pill drop, dose, conversation, report, notification and "
                           "sign-in. Type 'reset' to continue: ")
        except EOFError:
            raise CliError("reset-demo deletes data. Add --yes to confirm.", EXIT_USAGE) from None
        if answer.strip().lower() != "reset":
            _out("Cancelled. Nothing was changed.")
            return EXIT_FAILURE
    from tactidose.db.seed import reset_demo

    db = _open_database(settings)
    try:
        summary = reset_demo(db, settings, _clock(settings), keep_sessions=args.keep_sessions)
    finally:
        db.dispose()
    wiped = ", ".join(f"{count} {table}" for table, count in summary.get("wiped", {}).items() if count)
    _out(f"Demo data reset. Deleted: {wiped or 'nothing'}.")
    _print_demo_summary(summary, settings)
    _out("If the app is running, restart it so the simulator's pill counts match the containers.")
    return EXIT_OK


def _print_demo_summary(summary: dict[str, Any], settings: Settings) -> None:
    owner = summary.get("device_owner_id")
    _out(f"Demo data ready on device {summary['device_id']}"
         + (f" (owned by patient #{owner})." if owner is not None else "."))
    for account in summary.get("accounts", []):
        _out(f"  {account['key'].capitalize():<8} {account['display_name']:<12} {account['email']:<24} "
             f"user id {account['user_id']}")
    if settings.demo_password.get_secret_value() == DEFAULT_DEMO_PASSWORD:
        _out(f"  Password: {DEFAULT_DEMO_PASSWORD} (the default; set TACTIDOSE_DEMO_PASSWORD to change it)")
    else:
        _out("  Password: the value of TACTIDOSE_DEMO_PASSWORD")
    if not summary.get("device_bound_to_patient"):
        _err(f"Warning: device {summary['device_id']} belongs to another patient (#{owner}), so the demo "
             "containers were not loaded. Run 'python -m tactidose reset-demo --yes' to take it back.")
        return
    for c in summary.get("containers", []):
        name = c.get("medication_name") or "empty"
        low = " (low)" if c.get("medication_id") and 0 < c["pill_count"] <= c.get("low_stock_threshold", 0) else ""
        _out(f"  Container {c['container_number']}: {name}, {c['pill_count']} pills{low}")
    times = ", ".join(sc["time_of_day"] for sc in summary.get("schedules", []))
    _out(f"  Schedules: {times or 'none'} every day")
    _out(f"  Cooldown: {summary.get('cooldown_minutes')} minutes after any drop before the next manual drop")
    created = summary.get("created", {})
    _out("Created now: " + ", ".join(f"{created.get(k, 0)} {k}" for k in ("users", "links", "medications", "schedules"))
         + (", the device" if created.get("device") else "") + ".")


# =========================================================================== account commands


def _read_new_password(from_stdin: bool) -> str:
    if from_stdin or not _stdin_is_tty():
        if not from_stdin:
            _err("Reading the password from standard input ...")
        password = sys.stdin.readline().rstrip("\r\n") if sys.stdin is not None else ""
        if not password:
            raise CliError("No password was given on standard input.", EXIT_USAGE)
        return password
    first = getpass.getpass("New password (at least 8 characters): ")
    second = getpass.getpass("Type the password again: ")
    if first != second:
        raise CliError("The two passwords do not match. Nothing was created.", EXIT_USAGE)
    return first


def _cmd_create_user(args: argparse.Namespace) -> int:
    if args.password is not None:
        raise CliError("For safety, passwords are not accepted on the command line (they end up in the "
                       "shell history). Leave out --password to be asked for it, or pipe it with "
                       "--password-stdin.", EXIT_USAGE)
    settings = _settings()
    password = _read_new_password(args.password_stdin)
    db = _open_database(settings)
    try:
        auth = _auth(db, settings)
        user = auth.create_user(email=args.email, password=password, display_name=args.name,
                                role=args.role, phone=args.phone, bind_device=not args.no_device)
        profile = auth.patient_profile(user.user_id) if user.is_patient else None
    finally:
        db.dispose()
    _out(f"Created {user.role} account #{user.user_id}: {user.display_name} <{user.email}>.")
    if profile is not None:
        if profile.get("device_id"):
            _out(f"Device {profile['device_id']} is bound to this patient.")
        else:
            _out("No device is bound to this patient (it belongs to another patient). "
                 "Use 'bind-device --patient-id' to change that.")
        _out("Doctor and family accounts link with the patient ID and the link code shown in the patient portal.")
    return EXIT_OK


def _cmd_link(args: argparse.Namespace) -> int:
    settings = _settings()
    db = _open_database(settings)
    try:
        auth = _auth(db, settings)
        carer = auth.get_user_by_email(args.caregiver_email)
        if carer is None:
            raise CliError(f"No account uses the email address {args.caregiver_email!r}.")
        view = auth.admin_link(caregiver_id=carer.user_id, patient_id=args.patient_id)
    finally:
        db.dispose()
    _out(f"Linked {carer.display_name} ({view['relationship']}) to patient #{view['patient_id']} "
         f"{view['display_name']}.")
    return EXIT_OK


def _cmd_bind_device(args: argparse.Namespace) -> int:
    settings = _settings()
    db = _open_database(settings)
    try:
        binding = _auth(db, settings).bind_device(args.patient_id, device_id=args.device_id, force=True)
    finally:
        db.dispose()
    did, pid, previous = binding["device_id"], binding["patient_id"], binding["previous_owner_id"]
    if not binding["changed"]:
        _out(f"Device {did} was already bound to patient #{pid}.")
        return EXIT_OK
    was = f" (it belonged to patient #{previous})" if previous and previous != pid else ""
    _out(f"Device {did} is now bound to patient #{pid}{was}.")
    if binding.get("adopted"):
        _out("The device's medications and schedules moved to this patient.")
    if binding.get("released_containers"):
        _out(f"{binding['released_containers']} container(s) held another patient's medication and were "
             "emptied. A doctor or family member must load and refill them in the care portal.")
    if binding.get("cancelled_doses"):
        _out(f"{binding['cancelled_doses']} open dose(s) of the previous patient were cancelled.")
    return EXIT_OK


# =========================================================================== hardware commands


def _cmd_simulator(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.hardware.transports import serve_simulator_tcp

    stop = threading.Event()

    def ready(host: str, port: int) -> None:
        _out(f"Simulated ESP32 ({settings.num_slots} containers) on socket://{host}:{port}")
        _out(f"Connect the app with TACTIDOSE_HARDWARE_MODE=serial TACTIDOSE_SERIAL_PORT=socket://{host}:{port}")
        _out("Press Ctrl+C to stop.")

    try:
        serve_simulator_tcp(settings, args.host, args.port, stop, on_ready=ready)
    except KeyboardInterrupt:
        _out("Simulator stopped.")
    except OSError as exc:
        raise CliError(f"Could not serve the simulator on {args.host}:{args.port}: {exc}") from None
    finally:
        stop.set()
    return EXIT_OK


def _cmd_hw_test(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.hardware.selftest import run_hw_test

    return int(run_hw_test(settings, port=args.port, repeat=args.repeat, interactive=args.interactive))


def _cmd_conformance(args: argparse.Namespace) -> int:
    from tactidose.hardware.conformance import main as conformance_main

    try:
        return int(conformance_main(list(getattr(args, "passthrough", []) or [])) or 0)
    except SystemExit as exc:
        return _exit_code(exc)


def _cmd_ports(args: argparse.Namespace) -> int:
    from tactidose.hardware.ports import format_ports

    _out(format_ports())
    return EXIT_OK


# =========================================================================== doctor

Check = Callable[["Settings"], "tuple[str, list[str]]"]


def _check_database(settings: Settings) -> tuple[str, list[str]]:
    from tactidose.integrations.tidb import check_connection, describe

    info = describe(settings)
    ok, message, version = check_connection(settings)
    lines = [message + (f" (version {version})" if version else "")]
    if info.get("backend") == "tidb":
        lines.append(f"TLS: {'on' if info.get('tls') else 'off'}, CA: {info.get('ca_source')}")
    return ("OK" if ok else "FAIL"), lines


def _check_ports(settings: Settings) -> tuple[str, list[str]]:
    from tactidose.hardware.ports import describe_ports, format_ports

    lines = [f"hardware mode {settings.hardware_mode}, serial port setting {settings.serial_port}"]
    lines += format_ports().splitlines()
    if settings.hardware_mode != "serial":
        return "INFO", lines
    if settings.serial_port == "auto" and not any(p.get("auto_selected") for p in describe_ports()):
        lines.append("no ESP32 found: plug the board in or set TACTIDOSE_SERIAL_PORT")
        return "WARN", lines
    return "OK", lines


def _check_voice_model(settings: Settings) -> tuple[str, list[str]]:
    from tactidose.voice.recognizer import looks_like_vosk_model

    path = Path(settings.vosk_model_path)
    if looks_like_vosk_model(path):
        return "OK", [f"Vosk model found at {path}"]
    return ("WARN" if settings.voice_enabled else "INFO"), [
        f"no Vosk model at {path}", "install it with: python -m tactidose download-voice-model"]


def _check_audio(settings: Settings) -> tuple[str, list[str]]:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001 - ImportError or OSError (PortAudio missing)
        return "WARN", [f"sounddevice is unavailable ({type(exc).__name__})",
                        "install the voice extras: pip install 'tactidose[voice]'"]
    try:
        devices = list(sd.query_devices())
    except Exception as exc:  # noqa: BLE001
        return "WARN", [f"cannot list audio devices: {exc}"]
    inputs = [d for d in devices if int(d.get("max_input_channels", 0) or 0) > 0]
    outputs = [d for d in devices if int(d.get("max_output_channels", 0) or 0) > 0]
    lines = [f"{len(inputs)} input (microphone) and {len(outputs)} output (speaker) device(s)"]
    for kind in ("input", "output"):
        try:
            lines.append(f"default {kind}: {sd.query_devices(kind=kind).get('name', '?')}")
        except Exception:  # noqa: BLE001 - no default device of that kind
            lines.append(f"default {kind}: none")
    return ("OK" if inputs and outputs else "WARN"), lines


def _check_services(settings: Settings) -> tuple[str, list[str]]:
    def yes(flag: bool, on: str, off: str) -> str:
        return on if flag else off

    smtp = (f"configured ({settings.smtp_host}:{settings.smtp_port}, "
            f"{'SSL' if settings.smtp_ssl else ('STARTTLS' if settings.smtp_starttls else 'plain')})")
    return "INFO", [
        "Gemini: " + yes(settings.gemini_configured, f"configured (model {settings.gemini_model})",
                         "not configured - the offline rule-based agent answers"),
        f"Agent provider: {settings.effective_agent_provider}",
        "ElevenLabs voice: " + yes(settings.elevenlabs_configured, "configured", "not configured - offline voice"),
        "SMTP e-mail: " + yes(settings.smtp_configured, smtp,
                              f"not configured - report e-mails are saved in {settings.outbox_dir}"),
        "Snowflake analytics: " + yes(settings.snowflake_configured, "configured", "not configured"),
        "TiDB: " + yes(bool(settings.tidb_host), f"configured ({settings.tidb_host})", "not configured - SQLite"),
        "To test the keys live on this network (tiny real requests): python -m tactidose check-apis",
    ]


def _check_native(settings: Settings, *, docker: bool = False) -> tuple[str, list[str]]:
    from tactidose.hardware import conformance_native as native

    binary = native.resolve_binary(native.DEFAULT_BINARY)
    if not binary.is_file():
        return "INFO", ["not built (optional): python -m tactidose.hardware.conformance_native build"]
    stale = native.harness_is_stale(binary)
    lines = [f"{binary} ({'older than the firmware sources - rebuild it' if stale else 'up to date'})"]
    if docker:
        reason = native.docker_unavailable_reason(timeout_s=20.0, attempts=1)
        lines.append("docker: " + ("ok" if reason is None else reason))
    return ("WARN" if stale else "OK"), lines


def _doctor_checks(args: argparse.Namespace) -> list[tuple[str, Check]]:
    return [
        ("Database", _check_database),
        ("Serial ports", _check_ports),
        ("Voice model", _check_voice_model),
        ("Audio devices", _check_audio),
        ("Services", _check_services),
        ("Native firmware harness", lambda s: _check_native(s, docker=args.docker)),
    ]


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings()
    _out(f"TactiDose {__version__} doctor. {DISCLAIMER}")
    _out("Settings:")
    for key, value in settings.public_summary().items():
        _out(f"  {key}: {value}")
    failed = 0
    for title, check in _doctor_checks(args):
        try:
            status, lines = check(settings)
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            status, lines = "FAIL", [f"check failed: {_describe_exc(exc)}"]
        lines = lines or [""]
        _out(f"[{status:<4}] {title}: {_redact(lines[0], settings)}")
        for extra in lines[1:]:
            _out(f"       {_redact(extra, settings)}")
        failed += status == "FAIL"
    _out("All essential checks passed." if not failed else f"{failed} essential check(s) failed.")
    return EXIT_FAILURE if failed else EXIT_OK


# =========================================================================== check-apis

#: ``check-apis --only`` names, in check order (= ``live_check.SERVICES``; kept here so the parser
#: does not import the integrations).
API_SERVICES = ("gemini", "elevenlabs", "snowflake", "tidb", "smtp")


def _api_services(text: str) -> list[str]:
    names = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not names:
        raise argparse.ArgumentTypeError(f"name at least one service: {','.join(API_SERVICES)}")
    unknown = [n for n in names if n not in API_SERVICES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown service {unknown[0]!r} (choose from {', '.join(API_SERVICES)})")
    return names


def _env_file_path() -> Path:
    """The ``.env`` file Settings reads (relative to the working directory)."""
    from tactidose.config import Settings

    configured = Settings.model_config.get("env_file")
    if isinstance(configured, (list, tuple)):
        configured = configured[0] if configured else None
    return Path(configured or ".env").resolve()


def _cmd_check_apis(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.integrations import live_check

    only = set(args.only) if args.only else None
    if args.json:
        results = live_check.run_checks(settings, only=only)
        _out(_redact(json.dumps([r.to_dict() for r in results], indent=2, default=str), settings))
    else:
        _err(f"Contacting the configured services (tiny requests, {live_check.TIMEOUT_S:g} s limit each; "
             "no email is sent) ...")
        results = live_check.run_checks(settings, only=only)
        width = None
        if sys.stdout is not None and sys.stdout.isatty():      # wrap for a terminal, never for a pipe
            width = shutil.get_terminal_size((120, 24)).columns - 1
        _out(_redact(live_check.format_report(results, _env_file_path(), width=width), settings))
    return EXIT_FAILURE if any(r.failed for r in results) else EXIT_OK


# =========================================================================== voice / audio commands


def _cmd_download_voice_model(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.voice.recognizer import download_model, looks_like_vosk_model

    configured = Path(settings.vosk_model_path)
    dest = Path(args.dest) if args.dest else configured.parent
    target = dest / configured.name
    if looks_like_vosk_model(target):
        _out(f"The offline speech model is already installed at {target}.")
        return EXIT_OK
    _out(f"Downloading the offline speech model (about 40 MB) to {target} ...")
    last = {"percent": -10}

    def progress(done: int, total: int | None) -> None:
        if total:
            percent = int(done * 100 / total)
            if percent >= last["percent"] + 10:
                last["percent"] = percent
                _err(f"  {percent}% ({done // 1_000_000} of {total // 1_000_000} MB)")

    try:
        path = download_model(dest, configured.name, progress=progress)
    except (RuntimeError, OSError) as exc:
        raise CliError(f"Download failed: {exc}") from None
    _out(f"Offline speech model ready at {path}.")
    if Path(path).resolve() != configured.resolve():
        _out(f"Set TACTIDOSE_VOSK_MODEL_PATH={path} so the app finds it.")
    return EXIT_OK


def _cmd_warm_tts_cache(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.audio.speaker import SpeakerService

    speaker = SpeakerService(settings, None)
    try:
        result = speaker.warm_cache()
    finally:
        speaker.close()
    _out(f"Speech cache ({result.get('provider', 'none')}): {result.get('rendered', 0)} rendered, "
         f"{result.get('cached', 0)} already cached, {result.get('failed', 0)} failed, "
         f"{result.get('total', 0)} phrases.")
    for message in result.get("errors", [])[:5]:
        _out(f"  {_redact(str(message), settings)}")
    return EXIT_OK if not result.get("failed") else EXIT_FAILURE


# =========================================================================== guided demo

#: Default scripted answers: morning yes/taken, noon unclear -> no, night yes/not taken.
GUIDED_SCRIPT = (
    "yes", "yes", "Pretty good day, no problems.",
    "maybe", "no", "A bit tired and I have a mild headache.",
    "yes please", "not yet", "Okay, a little worried about my sleep.",
)


def _cmd_guided_demo(args: argparse.Namespace) -> int:
    import time as _time

    base = _settings()
    data_dir = Path(args.data_dir) if args.data_dir else base.data_dir / "guided-demo"
    os.environ.update({
        "TACTIDOSE_HARDWARE_MODE": "sim", "TACTIDOSE_DEMO_MODE": "true", "TACTIDOSE_VOICE_ENABLED": "false",
        "TACTIDOSE_DATA_DIR": str(data_dir), "TACTIDOSE_DEMO_PAUSE_SECONDS": str(args.pause),
        "TACTIDOSE_DEMO_BUZZER_SECONDS": "1", "TACTIDOSE_HW_BOOT_WAIT_S": "0",
        **({} if args.speak else {"TACTIDOSE_TTS_PROVIDER": "none"}),
    })
    os.environ.setdefault("TACTIDOSE_SIM_SPEED", "10")
    settings = _settings()
    script = [a.strip() for a in (args.answers.split("|") if args.answers else GUIDED_SCRIPT)]
    from sqlalchemy import select

    from tactidose.api.views import device_owner_id
    from tactidose.app import build_services, shutdown, startup
    from tactidose.core.bus import Topic
    from tactidose.db.models import DoseEvent, GuidedDemoSlot, PillDrop

    services = build_services(settings)
    startup(services)
    try:
        deadline = _time.monotonic() + 30
        while not services.hardware.snapshot().connected and _time.monotonic() < deadline:
            _time.sleep(0.1)
        pid = device_owner_id(services.db, settings)
        if pid is None or services.guided is None:
            raise CliError("The demo patient or the guided demo is not available.")

        def show(ev: Any) -> None:
            d = ev.data or {}
            at = services.clock.local_now().strftime("%a %H:%M")
            if d.get("say"):
                _out(f"[{at}] SAY   ({d.get('step')}) {d['say']}")
            elif d.get("step") in ("buzzer_off",) or (d.get("step") == "dispensed"):
                _out(f"[{at}] {'BUZZER off' if d['step'] == 'buzzer_off' else 'DROP  ' + str((d.get('outcome') or {}).get('outcome'))}")

        services.bus.add_listener(show, [Topic.DEMO_GUIDED])
        services.guided.start(pid, reset=True)
        for answer in script:
            if services.guided.wait_awaiting(pid, settings.demo_answer_timeout_s + 30) is None:
                break
            _out(f"{'':13}HEARD {answer}")
            services.guided.answer(pid, answer)
        services.guided.wait_done(pid, 120)
        state = services.guided.state(pid) or {}
        _out(f"\nRun {state.get('run_id')}: {state.get('state')}")
        with services.db.session() as s:
            _out("\nguided_demo_slots:")
            for r in s.scalars(select(GuidedDemoSlot).where(GuidedDemoSlot.run_id == state.get("run_id"))
                               .order_by(GuidedDemoSlot.slot_index)):
                _out(f"  {r.slot_name:7} answer={r.take_answer:7} outcome={r.outcome:9} drop_id={r.drop_id} "
                     f"dose_event_id={r.dose_event_id} taken={r.taken} mood={r.mood} symptoms={r.symptoms} "
                     f"severity={r.severity} source={r.extraction_source} alert={r.alert}")
            _out("pill_drops:")
            for d in s.scalars(select(PillDrop).where(PillDrop.patient_id == pid).order_by(PillDrop.drop_id)):
                _out(f"  #{d.drop_id} {d.source:8} {d.status:9} {d.medication_name} (container {d.slot_number + 1 if d.slot_number is not None else '-'}) "
                     f"dose_event_id={d.dose_event_id} pills {d.pill_count_before}->{d.pill_count_after}")
            _out("dose_events (demo day):")
            ids = [r.dose_event_id for r in s.scalars(select(GuidedDemoSlot).where(
                GuidedDemoSlot.run_id == state.get("run_id"))) if r.dose_event_id]
            for ev in s.scalars(select(DoseEvent).where(DoseEvent.event_id.in_(ids)).order_by(DoseEvent.scheduled_at)):
                _out(f"  #{ev.event_id} {services.clock.to_local(ev.scheduled_at):%a %H:%M} {ev.status:10} "
                     f"drop_id={ev.drop_id} note={getattr(ev, 'review_note', None)!r}")
        return EXIT_OK if state.get("state") == "finished" else EXIT_FAILURE
    finally:
        shutdown(services)


# =========================================================================== buzzer test


def _cmd_buzzer_test(args: argparse.Namespace) -> int:
    import time as _time

    from tactidose.hardware import buzzer_config
    from tactidose.hardware.buzzer import create_buzzer

    if args.sim:
        os.environ["TACTIDOSE_HARDWARE_MODE"] = "sim"
    elif args.serial is not None:
        os.environ.update({"TACTIDOSE_HARDWARE_MODE": "serial", "TACTIDOSE_SERIAL_PORT": args.serial})
    settings = _settings()
    backend = args.backend or settings.buzzer_backend
    settings = settings.model_copy(update={"buzzer_backend": backend})
    ms = int(args.ms or buzzer_config.TEST_DURATION_MS)
    _out(f"Buzzer test: backend {backend}, {ms} ms (hardware: {settings.hardware_mode}).")
    hardware = None
    if backend in ("serial", "both"):
        from tactidose.hardware.serial_client import create_hardware

        hardware, _sim = create_hardware(settings)
        hardware.start()
        deadline = _time.monotonic() + 15
        while not hardware.snapshot().connected and _time.monotonic() < deadline:
            _time.sleep(0.1)
        if not hardware.snapshot().connected:
            _out("  The device is not connected (check the USB cable / port).")
        else:
            probe = getattr(hardware, "buzzer_query", None)
            reply = probe() if callable(probe) else None
            meaning = {
                "BUZZER": "the firmware has the BUZZER command",
                "UNKNOWN_COMMAND": "the firmware is older than the BUZZER command (flash the new firmware)",
                "NO_BUZZER": "BUZZER_PIN is still -1 in config.h (set the pin and flash again)",
            }
            code = getattr(reply, "code", "no reply")
            _out(f"  Device probe: {code} - {meaning.get(code, 'no usable reply')}.")
    buzzer = create_buzzer(settings, hardware, play_locally=True)
    try:
        sounded = buzzer.on(ms)
        hw_on = buzzer.hardware_active
        _time.sleep(ms / 1000.0)
        buzzer.off()
    finally:
        buzzer.close()
        if hardware is not None:
            hardware.close()
    if backend == "none":
        _out("  Backend 'none': nothing sounds (expected).")
        return EXIT_OK
    if backend in ("serial", "both"):
        if hw_on:
            _out("  OK: the device's buzzer was switched on.")
            return EXIT_OK
        serial_part = getattr(buzzer, "serial", buzzer)
        error = getattr(serial_part, "last_error", None) or "not connected"
        fallback = "the laptop tone played instead" if backend == "serial" else "only the laptop tone played"
        _out(f"  The device's buzzer was NOT used ({error}); {fallback}.")
        return EXIT_FAILURE
    _out("  OK: laptop tone played on this computer's speaker." if sounded else "  Nothing sounded.")
    return EXIT_OK if sounded else EXIT_FAILURE


# =========================================================================== reports / e-mail


def _optional_notifications(db: Database, settings: Settings, clock: Clock) -> Any:
    try:
        from tactidose.medication.notifications import NotificationService

        return NotificationService(db, settings, clock, bus=None)
    except Exception:   # optional: reports work without in-app notifications
        log.debug("notification service unavailable", exc_info=True)
        return None


def _cmd_generate_report(args: argparse.Namespace) -> int:
    settings = _settings()
    if not 1 <= args.days <= settings.report_max_days:
        raise CliError(f"--days must be between 1 and {settings.report_max_days}.", EXIT_USAGE)
    try:
        service_module = importlib.import_module("tactidose.reports.service")
    except ImportError as exc:
        raise CliError(f"The reports module is not available ({exc}).") from None
    db = _open_database(settings)
    try:
        clock = _clock(settings)
        from tactidose.auth.service import AuthService

        auth = AuthService(db, settings, clock)
        patient = auth.get_user(args.patient_id)
        if patient is None or not patient.is_patient:
            raise CliError(f"No patient account has the ID {args.patient_id}.")
        creator = patient
        if args.by_email:
            creator = auth.get_user_by_email(args.by_email)
            if creator is None or not auth.can_view(creator, patient.user_id):
                raise CliError(f"{args.by_email!r} is not the patient or a doctor/family member linked to them.")
        reports = service_module.ReportService(
            db, clock, settings, auth=auth, notifications=_optional_notifications(db, settings, clock), bus=None)
        meta = reports.generate(patient_id=patient.user_id, days=args.days, created_by_user_id=creator.user_id)
        pdf = reports.pdf_bytes(meta["report_id"]) if args.out else None
    finally:
        db.dispose()
    status = str(meta.get("status", "READY"))
    _out(f"Report #{meta['report_id']} ({status}): {meta.get('title', '')}, "
         f"{meta.get('pdf_size', len(pdf or b''))} bytes.")
    if pdf is not None:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(pdf)
        _out(f"Saved to {out}.")
    if status != "READY":
        _err(f"The report could not be generated: {meta.get('error') or 'unknown error'}")
        return EXIT_FAILURE
    return EXIT_OK


TEST_EMAIL_SUBJECT = "TactiDose test e-mail"
TEST_EMAIL_BODY = (
    "This is a test message from TactiDose.\n\n"
    "If you can read it, TactiDose reports will reach this address. The attached PDF is only a "
    "test page.\n\n" + DISCLAIMER + "\n"
)


def _test_pdf() -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.add_page()
    for size, height, text in ((16, 10, "TactiDose test e-mail"),
                               (12, 8, "This page only checks that report e-mails are delivered. " + DISCLAIMER)):
        pdf.set_font("Helvetica", size=size)
        pdf.multi_cell(0, height, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def _call_test_mailer(mailer: Any, settings: Settings, to_email: str) -> Any:
    """``send_test_email(settings, to)`` if the mailer has one, else a test page through
    ``send_report_email`` (the same path real reports take)."""
    test = getattr(mailer, "send_test_email", None)
    if callable(test):
        return test(settings, to_email)
    send = getattr(mailer, "send_report_email", None)
    if callable(send):
        return send(settings, to=to_email, subject=TEST_EMAIL_SUBJECT, body=TEST_EMAIL_BODY,
                    pdf_bytes=_test_pdf(), filename="tactidose-test.pdf")
    raise CliError("tactidose.reports.mailer has neither send_test_email() nor send_report_email().")


def _delivery_view(result: Any) -> tuple[str, str]:
    if isinstance(result, dict):
        status, detail = result.get("status"), result.get("path") or result.get("error") or result.get("detail")
    else:
        status = getattr(result, "status", None)
        detail = getattr(result, "path", None) or getattr(result, "error", None)
    status = getattr(status, "value", status)
    return (str(status).upper() if status else "SENT"), ("" if detail is None else str(detail))


def _cmd_send_test_email(args: argparse.Namespace) -> int:
    settings = _settings()
    from tactidose.auth.service import normalize_email
    from tactidose.medication.errors import ValidationError

    try:
        to_email = normalize_email(args.to)
    except ValidationError as exc:
        raise CliError(exc.message, EXIT_USAGE) from None
    try:
        mailer = importlib.import_module("tactidose.reports.mailer")
    except ImportError as exc:
        raise CliError(f"The reports mailer is not available ({exc}).") from None
    status, detail = _delivery_view(_call_test_mailer(mailer, settings, to_email))
    _out(f"Test e-mail to {to_email}: {status}" + (f" - {_redact(detail, settings)}" if detail else "") + ".")
    if status == "SAVED":
        _out("SMTP is not configured, so the message was saved as an .eml file instead of being sent.")
    return EXIT_OK if status in ("SENT", "SAVED") else EXIT_FAILURE


# =========================================================================== parser


def _int_in(lo: int, hi: int | None, what: str) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{what} must be a whole number") from None
        if value < lo or (hi is not None and value > hi):
            bound = f"between {lo} and {hi}" if hi is not None else f"at least {lo}"
            raise argparse.ArgumentTypeError(f"{what} must be {bound}")
        return value

    return parse


def _float_in(lo: float, hi: float, what: str) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{what} must be a number") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"{what} must be between {lo:g} and {hi:g}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tactidose",
        description=f"TactiDose.tech host software. {DISCLAIMER}",
        epilog="Settings come from TACTIDOSE_* environment variables or a .env file (see .env.example).",
    )
    parser.add_argument("--version", action="version", version=f"tactidose {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="only show warnings and errors")
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="commands")
    port = _int_in(1, 65535, "the port")
    positive = _int_in(1, None, "the value")

    p = sub.add_parser("run", help="start the web app",
                       description="Start the web app (patient portal, care portal, demo panel).")
    p.add_argument("--host", help="listen address (default: TACTIDOSE_HOST or 127.0.0.1)")
    p.add_argument("--port", type=port, help="listen port (default: TACTIDOSE_PORT or 8000)")
    hw = p.add_mutually_exclusive_group()
    hw.add_argument("--sim", action="store_true", help="use the built-in simulated ESP32")
    hw.add_argument("--serial", metavar="PORT", help="use a real ESP32: COM5, /dev/ttyUSB0, socket://HOST:PORT or auto")
    hw.add_argument("--no-hardware", action="store_true", help="no device: every drop is refused")
    p.add_argument("--no-voice", action="store_true", help="turn off the device-side microphone loop")
    p.add_argument("--demo-pause-seconds", type=_float_in(0, 120, "the pause"), metavar="N",
                   help="pause between the guided demo's slots (default: TACTIDOSE_DEMO_PAUSE_SECONDS or 7)")
    p.set_defaults(handler=_cmd_run, default_log_level=logging.INFO)

    p = sub.add_parser("init-db", help="create the database and tables")
    p.set_defaults(handler=_cmd_init_db)

    p = sub.add_parser("seed-demo", help="create the demo accounts and device (idempotent)")
    p.set_defaults(handler=_cmd_seed_demo)

    p = sub.add_parser("reset-demo", help="wipe the demo's dynamic data and re-seed it")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--keep-sessions", action="store_true", help="keep everyone signed in")
    p.set_defaults(handler=_cmd_reset_demo)

    p = sub.add_parser("create-user", help="create an account",
                       description="Create an account. The password is asked for (or read from stdin).")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True, help="display name, e.g. 'Alex Rivera'")
    p.add_argument("--role", required=True, type=str.lower, choices=("patient", "doctor", "family"))
    p.add_argument("--phone")
    p.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    p.add_argument("--no-device", action="store_true", help="patients: do not bind the configured device")
    p.add_argument("--password", help=argparse.SUPPRESS)
    p.set_defaults(handler=_cmd_create_user)

    p = sub.add_parser("link", help="link a doctor/family account to a patient (no link code needed)")
    p.add_argument("--caregiver-email", required=True)
    p.add_argument("--patient-id", required=True, type=positive)
    p.set_defaults(handler=_cmd_link)

    p = sub.add_parser("bind-device", help="bind the configured device to a patient")
    p.add_argument("--patient-id", required=True, type=positive)
    p.add_argument("--device-id", help="default: TACTIDOSE_DEVICE_ID")
    p.set_defaults(handler=_cmd_bind_device)

    p = sub.add_parser("simulator", help="serve a simulated ESP32 over TCP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=_int_in(0, 65535, "the port"), default=7777, help="0 = any free port")
    p.set_defaults(handler=_cmd_simulator, default_log_level=logging.INFO)

    p = sub.add_parser("hw-test", help="integration checklist on a real ESP32 (moves the device)")
    p.add_argument("--port", help="serial port, e.g. COM5 (default: TACTIDOSE_SERIAL_PORT / auto)")
    p.add_argument("--repeat", type=positive, default=1, help="dispense cycles to run")
    p.add_argument("--interactive", action="store_true", help="also test the physical buttons")
    p.set_defaults(handler=_cmd_hw_test, default_log_level=logging.INFO)

    p = sub.add_parser("conformance", help="protocol conformance scenarios",
                       description="Arguments are passed to python -m tactidose.hardware.conformance, "
                                   "e.g. --target sim | native | serial --port COM5.")
    p.set_defaults(handler=_cmd_conformance, passthrough=[])

    p = sub.add_parser("ports", help="list serial ports")
    p.set_defaults(handler=_cmd_ports)

    p = sub.add_parser("doctor", help="check the configuration and environment")
    p.add_argument("--docker", action="store_true", help="also check Docker (slow)")
    p.set_defaults(handler=_cmd_doctor)

    p = sub.add_parser("check-apis", help="test the configured cloud keys with tiny live requests",
                       description="Send one tiny real request to each configured cloud service (Gemini, "
                                   "ElevenLabs, Snowflake, TiDB, SMTP) and report what works from this network. "
                                   "Services without settings are not contacted, no email is sent and keys are "
                                   "never printed. Exit code 1 when a configured service fails.")
    p.add_argument("--only", type=_api_services, action="extend", metavar="NAMES",
                   help=f"comma-separated subset of {','.join(API_SERVICES)} (default: all)")
    p.add_argument("--json", action="store_true", help="print the results as a JSON list")
    p.set_defaults(handler=_cmd_check_apis)

    p = sub.add_parser("download-voice-model", help="download the offline Vosk speech model")
    p.add_argument("--dest", help="folder for the model (default: the folder of TACTIDOSE_VOSK_MODEL_PATH)")
    p.set_defaults(handler=_cmd_download_voice_model)

    p = sub.add_parser("guided-demo", help="run the guided judge demo headless on the simulator",
                       description="Run the MORNING / NOON / NIGHT guided demo on the built-in simulator with "
                                   "scripted answers, print every spoken line, then the stored rows. Uses its "
                                   "own data folder (<data dir>/guided-demo) and re-seeds the demo data there.")
    p.add_argument("--answers", help=f"answers separated by '|' (default: {'|'.join(GUIDED_SCRIPT)!r})")
    p.add_argument("--pause", type=_float_in(0, 120, "the pause"), default=1.0, metavar="N",
                   help="seconds between slots (default 1)")
    p.add_argument("--data-dir", help="data folder (default: <TACTIDOSE_DATA_DIR>/guided-demo)")
    p.add_argument("--speak", action="store_true", help="render speech too (default: captions only)")
    p.set_defaults(handler=_cmd_guided_demo)

    p = sub.add_parser("buzzer-test", help="sound the buzzer for 2 seconds (wiring check)",
                       description="Fire the buzzer backend (TACTIDOSE_BUZZER_BACKEND, or --backend) once, "
                                   "without running the demo, and say what happened. serial/both talk to "
                                   "the device (TACTIDOSE_HARDWARE_MODE / --serial PORT / --sim). Exit 0 when "
                                   "the chosen backend sounded, 1 when it fell back. See docs/BUZZER.md.")
    p.add_argument("--backend", choices=["laptop", "serial", "both", "none"],
                   help="override TACTIDOSE_BUZZER_BACKEND for this test")
    hw_choice = p.add_mutually_exclusive_group()
    hw_choice.add_argument("--serial", metavar="PORT", help="the device's port: COM5, /dev/ttyUSB0 or auto")
    hw_choice.add_argument("--sim", action="store_true", help="use the built-in simulated ESP32")
    p.add_argument("--ms", type=_int_in(1, 65535, "the duration"), default=None,
                   help="how long to sound it (default: buzzer_config.TEST_DURATION_MS = 2000)")
    p.set_defaults(handler=_cmd_buzzer_test)

    p = sub.add_parser("warm-tts-cache", help="pre-render the critical spoken phrases")
    p.set_defaults(handler=_cmd_warm_tts_cache)

    p = sub.add_parser("generate-report", help="generate a PDF report for a patient")
    p.add_argument("--patient-id", required=True, type=positive)
    p.add_argument("--days", type=positive, default=7)
    p.add_argument("--out", help="also save the PDF to this file")
    p.add_argument("--by-email", help="record this linked account as the creator (default: the patient)")
    p.set_defaults(handler=_cmd_generate_report)

    p = sub.add_parser("send-test-email", help="send (or save) a test e-mail")
    p.add_argument("--to", required=True, help="recipient address")
    p.set_defaults(handler=_cmd_send_test_email)
    return parser


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Cut the arguments after a pass-through command, so argparse never sees them."""
    for i, token in enumerate(argv):
        if token.startswith("-"):
            continue
        if token in PASSTHROUGH_COMMANDS:
            return argv[: i + 1], argv[i + 1:]
        break
    return argv, None


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    _err(str(code))
    return EXIT_FAILURE


def _configure_logging(args: argparse.Namespace) -> None:
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    else:
        level = getattr(args, "default_log_level", logging.WARNING)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(level)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code (0 ok, 1 failure, 2 usage/configuration)."""
    parser = build_parser()
    head, passthrough = _split_passthrough(list(sys.argv[1:] if argv is None else argv))
    try:
        args = parser.parse_args(head)
    except SystemExit as exc:          # argparse: usage errors (2) and --help/--version (0)
        return _exit_code(exc)
    if passthrough is not None:
        args.passthrough = passthrough
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    _configure_logging(args)
    try:
        return int(handler(args) or 0)
    except CliError as exc:
        _err(exc.message)
        return exc.exit_code
    except KeyboardInterrupt:
        _err("Interrupted.")
        return EXIT_FAILURE
    except Exception as exc:   # last resort: a readable message, the traceback only with -v
        from tactidose.medication.errors import DomainError, ValidationError

        if isinstance(exc, DomainError):
            _err(exc.message)
            return EXIT_USAGE if isinstance(exc, ValidationError) else EXIT_FAILURE
        log.debug("command failed", exc_info=True)
        _err(f"Error: {_describe_exc(exc)}" + ("" if args.verbose else " (run with -v for details)"))
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
