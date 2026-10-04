"""Command-line entry point.

    tactidose-wellbeing demo                 # scripted synthetic scenarios
    tactidose-wellbeing interactive          # type your own answers
    tactidose-wellbeing serve --dev-auth     # REST API on 127.0.0.1 (dev identity)
    tactidose-wellbeing openapi > openapi.json
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from .bootstrap import build_service
from .config import Settings
from .contract import ActionRequest, StartSessionRequest

DEMO_USER = "demo-user-001"


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import SCENARIOS, ServiceTransport, run_all, run_scenario

    with tempfile.TemporaryDirectory() as tmp:
        db = args.db or str(Path(tmp) / "demo.sqlite3")
        service = build_service(replace(Settings.from_env(), db_path=db))
        transport = ServiceTransport(service, DEMO_USER)
        if args.scenario:
            match = [s for s in SCENARIOS if s.key == args.scenario]
            if not match:
                print(f"Unknown scenario. Choose from: {', '.join(s.key for s in SCENARIOS)}")
                return 2
            run_scenario(transport, match[0])
        else:
            run_all(transport)
        print(f"\n(SQLite file used for this demo: {db})")
    return 0


def _cmd_interactive(args: argparse.Namespace) -> int:
    settings = replace(Settings.from_env(), db_path=args.db or Settings.from_env().db_path)
    service = build_service(settings)
    user = args.user
    print(f"Interactive check-in for synthetic user '{user}'. Saved data goes to {settings.db_path}.")
    print("Type your answers. Commands: skip, repeat, cancel, finish. Ctrl-D to quit.\n")
    resp = service.start_session(user, StartSessionRequest(request_id=uuid.uuid4().hex))
    print(f"TTS < {resp.speech_text}")
    while not resp.session_status.is_terminal:
        try:
            text = input("YOU > ").strip()
        except EOFError:
            print()
            return 0
        if not text:
            continue
        resp = service.handle_action(
            user,
            resp.session_id,
            ActionRequest(request_id=uuid.uuid4().hex, action="answer", answer=text),
        )
        print(f"TTS < {resp.speech_text}")
        for event in resp.events:
            print(f"      (event: {event.type})")
    print(f"\nTTS < {service.get_history(user).speech_text}")
    return 0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .bootstrap import build_app

    settings = Settings.from_env()
    host = args.host or settings.host
    port = args.port or settings.port
    if args.dev_auth:
        settings = replace(settings, auth_mode="dev")
    if settings.auth_mode == "dev" and not _is_loopback(host):
        print("Refusing to start: dev identity mode may only bind to a loopback address.", file=sys.stderr)
        return 2
    if settings.auth_mode == "unconfigured":
        print(
            "WARNING: authentication is not configured; user-scoped endpoints will return 503. "
            "Use --dev-auth for local development.",
            file=sys.stderr,
        )
    uvicorn.run(build_app(settings), host=host, port=port, log_level="info")
    return 0


def _cmd_openapi(args: argparse.Namespace) -> int:
    from .api.app import create_app
    from .api.auth import UnconfiguredIdentityProvider

    service = build_service(replace(Settings(), db_path=":memory:"))
    spec = create_app(service, UnconfiguredIdentityProvider()).openapi()
    text = json.dumps(spec, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tactidose-wellbeing", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="Run scripted synthetic scenarios")
    p.add_argument("--scenario", help="Run one scenario by key")
    p.add_argument("--db", help="SQLite path (default: a temporary file)")
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("interactive", help="Type answers to a check-in")
    p.add_argument("--user", default=DEMO_USER, help="Synthetic local user id")
    p.add_argument("--db", help="SQLite path (default from settings)")
    p.set_defaults(func=_cmd_interactive)

    p = sub.add_parser("serve", help="Run the REST API (binds to 127.0.0.1 by default)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--dev-auth", action="store_true",
                   help="LOCAL DEVELOPMENT ONLY: trust the X-Dev-User-Id header from localhost")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("openapi", help="Print or write the OpenAPI document")
    p.add_argument("--output")
    p.set_defaults(func=_cmd_openapi)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
