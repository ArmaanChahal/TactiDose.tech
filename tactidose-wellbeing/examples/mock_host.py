"""Mock TactiDose host that integrates with the well-being module over REST.

It stands in for the real TactiDose app, which owns the microphone, speech
recognition, text-to-speech and UI. Here, "speech recognition" is a list of
strings and "text-to-speech" is print().

Run in-process (no server needed):
    python examples/mock_host.py

Or against a running server started with `tactidose-wellbeing serve --dev-auth`:
    python examples/mock_host.py --base-url http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from tactidose_wellbeing.api import DEV_USER_HEADER, DevHeaderIdentityProvider, create_app
from tactidose_wellbeing.bootstrap import build_service
from tactidose_wellbeing.config import Settings
from tactidose_wellbeing.demo import run_all

USER_ID = "synthetic-host-user-7"  # synthetic identity, as if from the host's own login


class FakeMedicationFeature:
    """Stand-in for the host's own medication features. The well-being module
    is never consulted for, and never blocks, medication access."""

    def handle(self, utterance: str) -> str:
        return "[medication feature] Your next scheduled dose is at 6 PM (synthetic data)."


class RestHost:
    def __init__(self, client: Any) -> None:
        self.http = client
        self.headers = {DEV_USER_HEADER: USER_ID}  # LOCAL DEV identity only

    # -- Transport protocol used by the shared demo scenarios ---------------
    def start(self, request_id: str) -> dict[str, Any]:
        r = self.http.post("/v1/sessions", json={"schema_version": "1.0", "request_id": request_id},
                           headers=self.headers)
        return self._handle(r)

    def act(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = self.http.post(f"/v1/sessions/{session_id}/actions", json=payload, headers=self.headers)
        return self._handle(r)

    def history(self) -> dict[str, Any]:
        return self.http.get("/v1/me/history", headers=self.headers).json()

    # -- Host-side reactions to the contract ---------------------------------
    def _handle(self, r: Any) -> dict[str, Any]:
        body = r.json()
        if "error" in body and "session_id" not in body:
            raise RuntimeError(f"HTTP {r.status_code}: {body['error']}")
        seen = getattr(self, "_seen_events", set())
        for event in body.get("events", []):
            if event["event_id"] in seen:
                continue  # deduplicate retried events by event_id
            seen.add(event["event_id"])
            if event["type"] == "support.requested":
                print("         [HOST] support.requested received. The host would now OFFER "
                      "handoff options to the user. Nobody has been contacted.")
            if event["type"] == "support.urgent_response_shown":
                print("         [HOST] urgent support text shown; host may offer its configured options.")
        self._seen_events = seen
        return body


def medication_independence_demo(host: RestHost) -> None:
    print("\n=== Medication access is never blocked by a check-in ===")
    meds = FakeMedicationFeature()
    resp = host.start("meds-demo-start")
    sid = resp["session_id"]
    resp = host.act(sid, {"schema_version": "1.0", "request_id": "meds-demo-1", "action": "answer",
                          "answer": "yes"})
    print(f"  TTS  < {resp['speech_text']}")
    print("  USER > what time is my next dose?   (host routes this to ITS OWN medication feature)")
    print(f"  TTS  < {meds.handle('what time is my next dose?')}")
    state = host.http.get(f"/v1/sessions/{sid}", headers=host.headers).json()
    print(f"  (check-in untouched: status={state['session_status']}, step={state['step']})")
    print("  USER > dispense my pills   (if mistakenly sent to the check-in, it is not a command)")
    resp = host.act(sid, {"schema_version": "1.0", "request_id": "meds-demo-2", "action": "answer",
                          "answer": "dispense my pills"})
    print(f"  TTS  < {resp['speech_text']}")
    host.act(sid, {"schema_version": "1.0", "request_id": "meds-demo-3", "action": "cancel"})


def privacy_demo(host: RestHost) -> None:
    print("\n=== User reviews and deletes their saved data ===")
    history = host.history()
    print(f"  TTS  < {history['speech_text']}")
    with_note = next((r for r in history["records"] if any(a["note_text"] for a in r["answers"])), None)
    if with_note:
        q = next(a["question_id"] for a in with_note["answers"] if a["note_text"])
        r = host.http.delete(f"/v1/me/history/{with_note['record_id']}/notes/{q}", headers=host.headers)
        print(f"  USER > delete the note on {q}")
        print(f"  TTS  < {r.json()['speech_text']}")
    r = host.http.delete("/v1/me/history", headers=host.headers)
    print("  USER > delete all my check-ins")
    print(f"  TTS  < {r.json()['speech_text']}")
    print(f"  TTS  < {host.history()['speech_text']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="Use a running server instead of an in-process app")
    args = parser.parse_args()

    if args.base_url:
        import httpx

        client = httpx.Client(base_url=args.base_url, timeout=10)
        print("NOTE: against a shared server, rerunning reuses request_ids and returns replays.")
    else:
        from fastapi.testclient import TestClient

        tmp = tempfile.mkdtemp()
        service = build_service(replace(Settings(), db_path=str(Path(tmp) / "host-demo.sqlite3")))
        # TestClient is not a loopback socket, so the loopback check is disabled here only.
        app = create_app(service, DevHeaderIdentityProvider(require_loopback=False))
        client = TestClient(app)

    health = client.get("/v1/health").json()
    print(f"Health: {health}")
    host = RestHost(client)
    run_all(host)
    medication_independence_demo(host)
    privacy_demo(host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
