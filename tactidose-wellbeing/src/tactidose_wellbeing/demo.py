"""Synthetic demo scenarios, shared by the CLI demo, the mock host (REST) and the
mock orchestrator (agent adapter). All data is synthetic."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .contract import SCHEMA_VERSION


@dataclass(frozen=True)
class Step:
    action: str
    text: str | None = None
    retry: bool = False  # resend the previous request with the SAME request_id


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    steps: tuple[Step, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)


def _a(text: str) -> Step:
    return Step("answer", text)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "completed",
        "1. Completed and saved check-in (with a confirmed note)",
        (
            _a("yes"),
            _a("low"),
            _a("Work deadlines this week"),
            _a("yes"),
            _a("medium"), _a("no"),
            _a("good"), _a("no"),
            _a("no"),
            Step("finish"),
        ),
    ),
    Scenario(
        "skipped",
        "2. Skipped question, ambiguous answer, and a corrected note",
        (
            _a("yes"),
            _a("skip"),
            _a("not great honestly"),       # unclear -> clarification
            _a("high"),
            _a("yes"),
            Step("add_note", "Too many appointments"),
            _a("change"),
            Step("add_note", "Too many appointments this week"),
            Step("confirm"),
            _a("okay"), Step("skip"),
            _a("no"),
            Step("finish"),
        ),
    ),
    Scenario(
        "cancelled",
        "3. Cancellation",
        (_a("yes"), _a("good"), _a("no"), _a("cancel")),
    ),
    Scenario(
        "session_only",
        "4. Session-only check-in (storage declined; nothing persisted)",
        (
            _a("no"),
            _a("okay"),
            _a("Quiet day at home"),
            _a("no"),                      # leave the note out
            _a("low"), _a("no"),
            _a("poor"), _a("no"),
            _a("no"),
            Step("finish"),
        ),
    ),
    Scenario(
        "support",
        "5. Support request (host-controlled handoff; nobody is contacted)",
        (
            _a("yes"),
            _a("sad"),                     # candidate -> needs confirmation
            _a("yes"),
            _a("no"),
            Step("skip"),
            Step("skip"),
            _a("yes"),
            Step("finish"),
        ),
    ),
    Scenario(
        "retry",
        "6. Repeated requests handled without duplication",
        (
            _a("yes"),
            _a("low"),
            Step("answer", "low", retry=True),
            _a("no"),
            Step("finish"),
            Step("finish", retry=True),
        ),
    ),
)


class Transport(Protocol):
    def start(self, request_id: str) -> dict[str, Any]: ...

    def act(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def history(self) -> dict[str, Any]: ...


def action_payload(step: Step, request_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "action": step.action,
    }
    if step.action == "answer":
        payload["answer"] = step.text
    elif step.action == "add_note":
        payload["note_text"] = step.text
    return payload


def run_scenario(
    transport: Transport, scenario: Scenario, out: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    out(f"\n=== {scenario.title} ===")
    responses = []
    resp = transport.start(f"{scenario.key}-start")
    responses.append(resp)
    out(f"  TTS  < {resp['speech_text']}")
    session_id = resp["session_id"]
    counter = 0
    previous: dict[str, Any] | None = None
    for step in scenario.steps:
        if step.retry and previous is not None:
            payload = previous
            label = f"(retry of {payload['request_id']})"
        else:
            counter += 1
            payload = action_payload(step, f"{scenario.key}-{counter}")
            label = f"[{step.action}]"
        spoken = step.text if step.text is not None else step.action
        out(f"  USER > {spoken} {label}")
        resp = transport.act(session_id, payload)
        responses.append(resp)
        out(f"  TTS  < {resp['speech_text']}")
        extras = []
        if resp.get("idempotent_replay"):
            extras.append("idempotent replay: nothing re-applied")
        if resp.get("events"):
            extras.append("events: " + ", ".join(e["type"] for e in resp["events"]))
        if resp.get("error"):
            extras.append(f"error: {resp['error']['code']}")
        if extras:
            out("         (" + "; ".join(extras) + ")")
        previous = payload
    out(f"  -> final status: {resp['session_status']}")
    return responses


def run_all(transport: Transport, out: Callable[[str], None] = print) -> None:
    for scenario in SCENARIOS:
        run_scenario(transport, scenario, out)
    history = transport.history()
    records = history.get("records", [])
    out(f"\n=== Saved history for the demo user: {len(records)} record(s) ===")
    out(f"  TTS  < {history.get('speech_text', '')}")
    out("  (Scenarios 3 and 4 saved nothing; scenario 6 saved exactly one record.)")


class ServiceTransport:
    """Calls the application service directly (Python import integration)."""

    def __init__(self, service: Any, user_id: str) -> None:
        from .contract import ActionRequest, StartSessionRequest

        self._service, self._user = service, user_id
        self._start_req, self._action_req = StartSessionRequest, ActionRequest

    def start(self, request_id: str) -> dict[str, Any]:
        return self._service.start_session(
            self._user, self._start_req(request_id=request_id)
        ).model_dump(mode="json")

    def act(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._service.handle_action(
            self._user, session_id, self._action_req.model_validate(payload)
        ).model_dump(mode="json")

    def history(self) -> dict[str, Any]:
        return self._service.get_history(self._user).model_dump(mode="json")
