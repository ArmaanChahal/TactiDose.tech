"""Regenerate docs/openapi.json and docs/samples/*.json from the real API.

Uses a fixed clock and deterministic ids so the output is stable.

    python examples/generate_samples.py
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from tactidose_wellbeing.api import DEV_USER_HEADER, DevHeaderIdentityProvider, create_app
from tactidose_wellbeing.service import WellbeingService
from tactidose_wellbeing.storage import InMemoryCheckinRepository, InMemorySessionStore

DOCS = Path(__file__).resolve().parent.parent / "docs"
SAMPLES = DOCS / "samples"


class SteppingClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=5)
        return self.now


def main() -> None:
    ids = itertools.count(1)
    service = WellbeingService(
        InMemoryCheckinRepository(),
        InMemorySessionStore(),
        clock=SteppingClock(),
        id_factory=lambda: f"{next(ids):032x}",
    )
    client = TestClient(create_app(service, DevHeaderIdentityProvider(require_loopback=False)))
    headers = {DEV_USER_HEADER: "synthetic-user-001"}
    SAMPLES.mkdir(parents=True, exist_ok=True)
    (DOCS / "openapi.json").write_text(json.dumps(client.app.openapi(), indent=2) + "\n")

    n = itertools.count(1)

    def save(name: str, method: str, path: str, request: dict | None, response) -> dict:
        body = response.json()
        doc = {"method": method, "path": path, "status": response.status_code,
               "headers": {DEV_USER_HEADER: "synthetic-user-001"}, "request": request, "response": body}
        (SAMPLES / f"{next(n):02d}_{name}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return body

    req = {"schema_version": "1.0", "request_id": "req-0001"}
    sid = save("start_session", "POST", "/v1/sessions", req,
               client.post("/v1/sessions", json=req, headers=headers))["session_id"]

    def act(name: str, body: dict) -> dict:
        path = f"/v1/sessions/{sid}/actions"
        return save(name, "POST", path, body, client.post(path, json=body, headers=headers))

    act("consent_yes", {"schema_version": "1.0", "request_id": "req-0002", "action": "answer", "answer": "yes"})
    act("answer_needs_confirmation", {"schema_version": "1.0", "request_id": "req-0003", "action": "answer",
                                      "answer": "I'm feeling great", "question_id": "mood", "expected_step": 1})
    act("confirm_answer", {"schema_version": "1.0", "request_id": "req-0004", "action": "confirm"})
    act("add_note_unconfirmed", {"schema_version": "1.0", "request_id": "req-0005", "action": "add_note",
                                 "note_text": "Went for a walk in the park"})
    act("confirm_note", {"schema_version": "1.0", "request_id": "req-0006", "action": "confirm"})
    act("ambiguous_answer_clarified", {"schema_version": "1.0", "request_id": "req-0007", "action": "answer",
                                       "answer": "not too bad"})
    act("skip_question", {"schema_version": "1.0", "request_id": "req-0008", "action": "skip"})
    act("answer_sleep", {"schema_version": "1.0", "request_id": "req-0009", "action": "answer", "answer": "poor"})
    act("decline_note", {"schema_version": "1.0", "request_id": "req-0010", "action": "reject"})
    act("support_requested", {"schema_version": "1.0", "request_id": "req-0011", "action": "answer",
                              "answer": "yes"})
    act("invalid_action", {"schema_version": "1.0", "request_id": "req-0012", "action": "add_note",
                           "note_text": "late note"})
    act("finish", {"schema_version": "1.0", "request_id": "req-0013", "action": "finish"})
    act("finish_retry_replayed", {"schema_version": "1.0", "request_id": "req-0013", "action": "finish"})
    act("idempotency_conflict", {"schema_version": "1.0", "request_id": "req-0013", "action": "cancel"})

    save("get_history", "GET", "/v1/me/history", None, client.get("/v1/me/history", headers=headers))
    save("delete_history", "DELETE", "/v1/me/history", None, client.delete("/v1/me/history", headers=headers))
    save("health", "GET", "/v1/health", None, client.get("/v1/health"))
    print(f"Wrote {DOCS / 'openapi.json'} and {len(list(SAMPLES.glob('*.json')))} samples to {SAMPLES}")


if __name__ == "__main__":
    main()
