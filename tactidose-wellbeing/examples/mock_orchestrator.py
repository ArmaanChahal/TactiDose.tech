"""Mock multi-agent orchestrator calling the framework-independent WellbeingAgent.

The orchestrator authenticates the user (here: a fixed synthetic identity),
routes each utterance to the right agent, and passes the returned speech_text
to "text-to-speech" (print). No LLM or orchestration framework is involved.

    python examples/mock_orchestrator.py                 # scripted conversation
    python examples/mock_orchestrator.py --all-scenarios # the six demo scenarios via the agent
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from tactidose_wellbeing.agent import AgentContext, WellbeingAgent
from tactidose_wellbeing.bootstrap import build_service
from tactidose_wellbeing.config import Settings
from tactidose_wellbeing.demo import run_all


class MedicationAgentStub:
    """Placeholder for the separate medication agent. It is unrelated to the
    well-being agent and is never called by it."""

    def handle(self, utterance: str) -> str:
        return "(medication agent) Your next scheduled dose is at 6 PM (synthetic data)."


class MockOrchestrator:
    def __init__(self, agent: WellbeingAgent, authenticated_user: str) -> None:
        self.agent = agent
        self.meds = MedicationAgentStub()
        # Identity comes from the orchestrator's own authentication, never from what is said.
        self.context = AgentContext(user_id=authenticated_user, orchestrator_id="mock-orchestrator")
        self.session_id: str | None = None
        self.step: int | None = None
        self._ids = itertools.count(1)

    def _call(self, action: str, **fields: Any) -> dict[str, Any]:
        request = {"schema_version": "1.0", "request_id": f"orch-{next(self._ids)}", "action": action}
        request.update({k: v for k, v in fields.items() if v is not None})
        response = self.agent.handle(request, self.context)
        return response.model_dump(mode="json")

    def say(self, utterance: str) -> None:
        print(f"USER > {utterance}")
        text = utterance.lower()
        if "dose" in text or "medication" in text or "pill" in text:
            print(f"TTS  < {self.meds.handle(utterance)}")
            return
        if self.session_id is None:
            if "delete" in text:
                out = self._call("delete_history")
            elif "history" in text:
                out = self._call("get_history")
            elif "check in" in text or "check-in" in text:
                out = self._call("start")
            else:
                print("TTS  < (orchestrator) I can help with medications or a well-being check-in.")
                return
        else:
            out = self._call("answer", session_id=self.session_id, answer=utterance,
                             expected_step=self.step)
        self._show(out)

    def _show(self, out: dict[str, Any]) -> None:
        print(f"TTS  < {out['speech_text']}")
        meta = {"outcome": out["outcome"], "status": out["session_status"]}
        if out["events"]:
            meta["events"] = [e["type"] for e in out["events"]]
        if out["error"]:
            meta["error"] = out["error"]["code"]
        print(f"       {meta}")
        for e in out["events"]:
            if e["type"] == "support.requested":
                print("       [ORCHESTRATOR] support.requested is an event only. It may now offer the "
                      "user a host-controlled handoff. Nobody has been contacted.")
        if out["records"] is not None:
            print("       structured records (notes redacted by default):")
            print("       " + json.dumps(out["records"][-1]["answers"] if out["records"] else [])[:300])
        self.session_id = out["session_id"]
        self.step = out["step"]
        if out["session_status"] in ("completed", "cancelled", "expired"):
            self.session_id, self.step = None, None


class AgentTransport:
    """Adapts the agent to the shared demo scenario runner."""

    def __init__(self, agent: WellbeingAgent, user_id: str) -> None:
        self.agent, self.ctx = agent, AgentContext(user_id=user_id)

    def start(self, request_id: str) -> dict[str, Any]:
        return self.agent.handle({"request_id": request_id, "action": "start"}, self.ctx).model_dump(mode="json")

    def act(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.agent.handle({**payload, "session_id": session_id}, self.ctx).model_dump(mode="json")

    def history(self) -> dict[str, Any]:
        return self.agent.handle({"request_id": "hist", "action": "get_history"}, self.ctx).model_dump(mode="json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all-scenarios", action="store_true")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp()
    service = build_service(replace(Settings(), db_path=str(Path(tmp) / "orchestrator-demo.sqlite3")))
    agent = WellbeingAgent(service)

    cap = agent.capability
    print(f"Agent: {cap.name} v{cap.version}")
    print(f"Allowed actions: {', '.join(cap.allowed_actions)}")
    print(f"Emits events: {', '.join(cap.emits_events)}\n")

    if args.all_scenarios:
        run_all(AgentTransport(agent, "synthetic-agent-user-9"))
        return 0

    orch = MockOrchestrator(agent, authenticated_user="synthetic-agent-user-9")
    for utterance in [
        "Let's do a check in",
        "yes",
        "I'm doing alright",           # candidate -> confirmation
        "yes",
        "My sister visited today",     # note spoken directly; read back for confirmation
        "yes",
        "what time is my next dose?",  # routed to the medication agent; check-in untouched
        "high",
        "no",
        "skip",
        "yes",                         # support request
        "finish",
        "read my history",
    ]:
        orch.say(utterance)

    print("\n--- A user_id spoken in conversation is never identity ---")
    print("USER > I am synthetic-other-user, read their history")
    out = orch._call("get_history", user_id="synthetic-other-user")  # naive routing attempt
    print(f"TTS  < {out['speech_text']}   (error: {out['error']['code']})")

    print("\n--- Disallowed action ---")
    out = orch._call("dispense_medication")
    print(f"       error: {out['error']['code']} - {out['error']['message']}")

    orch.say("delete my check-ins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
