"""Guided judge demo (tactidose/guided/): the MORNING / NOON / NIGHT state machine on the real v2
services with the simulated ESP32 and a frozen demo clock. Covers the yes path, the no path,
unclear -> no, a denied drop, Gemini failure -> rules, never two drops per slot, emergency
wording mid check-in, stop, and the HTTP endpoints.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import func, select

for _module in ("tactidose.medication.drops", "tactidose.auth.service", "tactidose.db.seed"):
    pytest.importorskip(_module)

from tactidose.api.events import EventScope  # noqa: E402
from tactidose.app import build_services, create_app  # noqa: E402
from tactidose.config import Settings  # noqa: E402
from tactidose.core import phrases  # noqa: E402
from tactidose.core.bus import BusEvent, Topic  # noqa: E402
from tactidose.core.clock import Clock  # noqa: E402
from tactidose.db.models import Compartment, DoseEvent, GuidedDemoSlot, Notification, PillDrop  # noqa: E402
from tactidose.guided import classify_yes_no  # noqa: E402
from tactidose.guided.checkin import GeminiCheckinExtractor, rules_extract  # noqa: E402
from tests.test_api_support import TestClient  # noqa: E402

pytestmark = pytest.mark.timeout(120)

TZ = "America/Vancouver"
FROZEN_AT = datetime(2026, 10, 5, 10, 30)


def _settings(tmp_path: Any, **over: Any) -> Settings:
    base: dict[str, Any] = dict(
        _env_file=None, data_dir=tmp_path / "data", hardware_mode="sim", sim_speed=50.0, num_slots=3,
        voice_enabled=False, tts_provider="none", label_extractor="disabled", agent_provider="rules",
        report_ai_summary=False, timezone=TZ, demo_mode=True, seed_demo_accounts=True, hw_boot_wait_s=0,
        scheduler_tick_s=600, demo_pause_seconds=0, demo_buzzer_seconds=0, demo_answer_timeout_s=5,
        # The frozen test clock has no travel offset to subtract, so sessions see the demo jumps.
        session_ttl_hours=96,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def env(tmp_path):
    settings = _settings(tmp_path)
    services = build_services(settings, clock=Clock(TZ, frozen_at=FROZEN_AT))
    with TestClient(create_app(services=services)) as client:
        deadline = time.monotonic() + 30
        while not services.hardware.snapshot().connected and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = services.device_patient_id()
        yield client, services, pid
        services.guided.stop(pid)
        services.guided.wait_done(pid, 10)


def _run(services: Any, pid: int, answers: list[str], *, reset: bool = True) -> dict[str, Any]:
    runner = services.guided
    said: list[str] = []
    stop = services.bus.add_listener(lambda ev: said.append(ev.data["say"]) if ev.data.get("say") else None,
                                     [Topic.DEMO_GUIDED])
    runner.start(pid, reset=reset)
    for answer in answers:
        assert runner.wait_awaiting(pid, 30) is not None, runner.state(pid)
        assert runner.answer(pid, answer)
    assert runner.wait_done(pid, 60), runner.state(pid)
    stop()
    return {**runner.state(pid), "said": said}


def _said(state: dict[str, Any]) -> list[str]:
    """Every spoken line (``_run`` collects them from the bus; the state keeps only the last 20)."""
    return state.get("said") or [t["text"] for t in state["transcript"] if t["who"] == "assistant"]


def _drops_per_event(services: Any) -> dict[int, int]:
    with services.db.session() as s:
        rows = s.execute(select(PillDrop.dose_event_id, func.count()).where(PillDrop.dose_event_id.is_not(None))
                         .group_by(PillDrop.dose_event_id)).all()
    return {int(e): int(n) for e, n in rows}


FULL = ["yes", "yes", "Pretty good day, no problems.",
        "no", "A bit tired and I have a mild headache.",
        "yes please", "not yet", "Okay, a little worried about my sleep."]


# --------------------------------------------------------------------------- whole run


def test_three_slots_yes_no_and_summary(env):
    _client, services, pid = env
    state = _run(services, pid, FULL)
    assert state["state"] == "finished"
    morning, noon, night = state["results"]
    assert (morning["outcome"], morning["taken"], morning["take_answer"]) == ("DROPPED", True, "yes")
    assert (noon["outcome"], noon["take_answer"], noon["drop_id"]) == ("DECLINED", "no", None)
    assert (night["outcome"], night["taken"]) == ("DROPPED", False)
    assert noon["extraction"]["symptoms"] == ["headache", "fatigue"] and noon["extraction"]["source"] == "rules"
    assert phrases.DEMO_BUZZER in _said(state)
    assert _said(state)[-1].startswith(phrases.DEMO_GOODBYE) and "Noon: you skipped it." in _said(state)[-1]

    with services.db.session() as s:
        rows = s.scalars(select(GuidedDemoSlot).order_by(GuidedDemoSlot.slot_index)).all()
        assert [(r.slot_name, r.outcome) for r in rows] == [("morning", "DROPPED"), ("noon", "DECLINED"),
                                                             ("night", "DROPPED")]
        events = {r.slot_name: s.get(DoseEvent, r.dose_event_id) for r in rows}
        assert events["morning"].status == "TAKEN"                      # confirm_pill_taken path
        assert events["noon"].status == "CANCELLED" and "Declined" in (events["noon"].review_note or "")
        assert events["night"].status == "DISPENSED"
        drops = s.scalars(select(PillDrop)).all()
        assert {d.source for d in drops} == {"schedule"} and len(drops) == 2
    # Never two drops for one slot's dose; DropService refuses another one anyway.
    per_event = _drops_per_event(services)
    assert all(n == 1 for n in per_event.values())
    again = services.drops.request_drop(patient_id=pid, source="schedule", dose_event_id=morning["dose_event_id"])
    assert again.to_dict()["status"] == "DENIED"


def test_unclear_twice_is_no(env):
    _client, services, pid = env
    state = _run(services, pid, ["maybe", "banana", "fine"] + ["no", "fine"] * 2)
    first = state["results"][0]
    assert (first["take_answer"], first["outcome"]) == ("unclear", "DECLINED")
    assert _said(state).count(phrases.DEMO_REASK_YES_NO) == 1
    assert _drops_per_event(services) == {}


def test_no_answer_times_out_as_unclear(tmp_path):
    settings = _settings(tmp_path, demo_answer_timeout_s=1)
    services = build_services(settings, clock=Clock(TZ, frozen_at=FROZEN_AT))
    with TestClient(create_app(services=services)):
        pid = services.device_patient_id()
        services.guided.start(pid, reset=True)
        assert services.guided.wait_done(pid, 60)
        state = services.guided.state(pid)
    assert state["state"] == "finished"
    assert [r["outcome"] for r in state["results"]] == ["DECLINED"] * 3
    assert all(r["checkin_text"] is None for r in state["results"])


def test_denied_drop_is_said_honestly_and_the_checkin_still_happens(env):
    _client, services, pid = env
    with services.db.session() as s:   # container 1 empty: DropService refuses (EMPTY)
        s.scalars(select(Compartment).where(Compartment.slot_number == 0)).one().pill_count = 0
    state = _run(services, pid, ["yes", "All good.", "no", "fine", "no", "fine"], reset=False)
    morning = state["results"][0]
    assert (morning["outcome"], morning["reason"]) == ("DENIED", "EMPTY")
    assert morning["taken"] is None and morning["checkin_text"] == "All good."
    said = _said(state)
    assert phrases.DEMO_ASK_TAKEN not in said                       # never asks after a refused drop
    assert any("empty" in t.lower() for t in said)
    assert not any("dropped from container 1" in t for t in said)


def test_emergency_wording_mid_checkin_alerts_and_ends_the_run(env):
    _client, services, pid = env
    state = _run(services, pid, ["no", "I have chest pain and I can't breathe"])
    assert state["state"] == "alerted"
    assert len(state["results"]) == 1
    said = _said(state)
    assert phrases.EMERGENCY in said and phrases.DEMO_ALERT_SENT in said
    with services.db.session() as s:
        row = s.scalars(select(GuidedDemoSlot)).one()
        assert row.alert is True and row.severity == "severe"
        kinds = s.scalars(select(Notification.kind).where(Notification.kind == "HEALTH_CONCERN")).all()
        assert len(kinds) >= 2                                         # patient + linked caregivers


def test_stop_ends_the_run(env):
    _client, services, pid = env
    services.guided.start(pid, reset=True)
    assert services.guided.wait_awaiting(pid, 30) == "yes_no"
    assert services.guided.stop(pid)
    assert services.guided.wait_done(pid, 30)
    state = services.guided.state(pid)
    assert state["state"] == "stopped" and _said(state)[-1] == phrases.DEMO_STOPPED
    assert _drops_per_event(services) == {}


def test_spoken_stop_ends_the_run(env):
    _client, services, pid = env
    state = _run(services, pid, ["stop"])
    assert state["state"] == "stopped"


# --------------------------------------------------------------------------- check-in extraction


class _Raising:
    class models:  # noqa: N801 - mimics client.models
        @staticmethod
        def generate_content(**_kw: Any) -> Any:
            raise TimeoutError("network down")


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.candidates: list[Any] = []


class _Inventing:
    calls = 0

    class models:  # noqa: N801
        @staticmethod
        def generate_content(**_kw: Any) -> Any:
            _Inventing.calls += 1
            return _Reply('{"mood": "low", "symptoms": ["headache", "kidney failure"], '
                          '"concerns": null, "severity": "none"}')


def test_gemini_failure_falls_back_to_rules_and_opens_the_breaker(tmp_path):
    settings = _settings(tmp_path, agent_retry_after_s=60)
    ai = GeminiCheckinExtractor(settings, client=_Raising())
    text = "I have a headache"
    first = ai.extract(text, rules_extract(text))
    assert first.source == "rules" and first.symptoms == ["headache"] and first.fallback_reason
    second = ai.extract(text, rules_extract(text))
    assert second.fallback_reason == "circuit_open"


def test_gemini_output_is_validated(tmp_path):
    ai = GeminiCheckinExtractor(_settings(tmp_path), client=_Inventing())
    text = "Not great, a bad headache since lunch"
    out = ai.extract(text, rules_extract(text))
    assert out.source == "gemini"
    assert out.symptoms == ["headache"]                    # the invented symptom is dropped
    assert out.severity == rules_extract(text).severity    # never below the patient's own words
    assert out.alert is False                              # the model cannot raise or silence alerts


def test_rules_extraction_and_yes_no():
    assert rules_extract("Pretty good day, no problems").to_dict()["concerns"] is None
    assert rules_extract("not bad, a bit tired").mood == "okay"
    assert rules_extract("I have chest pain").alert is True
    assert rules_extract("no headache today").symptoms == []
    table = {"yes": "yes", "yes please": "yes", "I took it": "yes", "no": "no", "not yet": "no",
             "I didn't": "no", "maybe": "unclear", "": "unclear", "stop": "stop",
             "I'm having a heart attack": "emergency"}
    assert {t: classify_yes_no(t) for t in table} == table


# --------------------------------------------------------------------------- HTTP


def _login(client: Any, email: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_http_endpoints_and_permissions(env):
    client, services, pid = env
    alex, sam = _login(client, "alex@demo.tactidose"), _login(client, "sam@demo.tactidose")
    assert client.get("/api/demo/guided", headers=alex).json() == {"state": "idle"}
    assert client.post("/api/demo/guided/start", headers=alex, json={"reset": True}).status_code == 403
    started = client.post("/api/demo/guided/start", headers=sam, json={"reset": True})
    assert started.status_code == 200, started.text
    assert client.post("/api/demo/guided/start", headers=alex, json={}).status_code == 409
    assert services.guided.wait_awaiting(pid, 30) == "yes_no"
    assert client.post("/api/demo/guided/answer", headers=alex, json={"text": "no"}).json() == {"accepted": True}
    assert client.post("/api/demo/guided/stop", headers=alex).json() == {"stopped": True}
    assert services.guided.wait_done(pid, 30)
    assert client.get("/api/demo/guided", headers=alex).json()["state"] == "stopped"
    client.cookies.clear()   # the logins above set the session cookie
    assert client.post("/api/demo/guided/answer", json={"text": "yes"}).status_code == 401


def test_progress_events_go_to_device_linked_users_in_demo_mode():
    ev = BusEvent(seq=1, topic=Topic.DEMO_GUIDED, data={"patient_id": 1, "say": "hi"})
    assert EventScope(user_id=1, patient_ids=frozenset({1}), device_patient_id=1, demo=True).permits(ev)
    assert not EventScope(user_id=1, patient_ids=frozenset({1}), device_patient_id=1, demo=False).permits(ev)
    assert not EventScope(user_id=9, patient_ids=frozenset(), device_patient_id=1, demo=True).permits(ev)
