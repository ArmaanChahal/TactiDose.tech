"""The REST API, the agent adapter and direct Python calls behave identically,
because all three call the same application service."""

import pytest
from fastapi.testclient import TestClient

from tactidose_wellbeing.agent import WellbeingAgent
from tactidose_wellbeing.api import DevHeaderIdentityProvider, create_app
from tactidose_wellbeing.demo import SCENARIOS, ServiceTransport, action_payload

from .conftest import dev_headers, make_service


class RestTransport:
    def __init__(self, service, user):
        self.client = TestClient(create_app(service, DevHeaderIdentityProvider(require_loopback=False)))
        self.h = dev_headers(user)

    def start(self, rid):
        return self.client.post("/v1/sessions", json={"request_id": rid}, headers=self.h).json()

    def act(self, sid, payload):
        return self.client.post(f"/v1/sessions/{sid}/actions", json=payload, headers=self.h).json()

    def history(self):
        return self.client.get("/v1/me/history", headers=self.h).json()


class AgentTransport:
    def __init__(self, service, user):
        self.agent, self.ctx = WellbeingAgent(service), {"user_id": user}

    def start(self, rid):
        return self.agent.handle({"request_id": rid, "action": "start"}, self.ctx).model_dump(mode="json")

    def act(self, sid, payload):
        return self.agent.handle({**payload, "session_id": sid}, self.ctx).model_dump(mode="json")

    def history(self):
        return self.agent.handle({"request_id": "h", "action": "get_history"}, self.ctx).model_dump(mode="json")


def _trace(transport, scenario):
    resp = transport.start(f"{scenario.key}-start")
    sid = resp["session_id"]
    trace = [(resp["session_status"], resp["step"], resp["speech_text"])]
    previous = None
    for i, step in enumerate(scenario.steps):
        payload = previous if step.retry else action_payload(step, f"{scenario.key}-{i}")
        resp = transport.act(sid, payload)
        events = [(e["type"], e["event_id"]) for e in resp["events"]]
        answers = [(a["question_id"], a["answer_value"], a["status"]) for a in resp["confirmed_answers"]]
        trace.append((resp["session_status"], resp["step"], resp["speech_text"], events, answers,
                      resp["support_requested"], resp["idempotent_replay"]))
        previous = payload
    return trace


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_api_agent_and_python_are_equivalent(scenario):
    traces, histories = [], []
    for transport_cls in (ServiceTransport, RestTransport, AgentTransport):
        service = make_service()  # identical clock and id sequence for each transport
        t = transport_cls(service, "user-eq")
        traces.append(_trace(t, scenario))
        histories.append([(a["question_id"], a["answer_value"]) for r in t.history()["records"]
                          for a in r["answers"]])
    assert traces[0] == traces[1] == traces[2]
    assert histories[0] == histories[1] == histories[2]
