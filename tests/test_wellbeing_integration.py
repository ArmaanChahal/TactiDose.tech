"""The optional well-being check-in (``tactidose-wellbeing/``) wired into the real v2 app
(tactidose/wellbeing.py): the offer after a pill drops, storage next to the pill history (patient
+ linked caregivers), chat routing, check-in turns kept out of the conversation log, medication
independence, identity on the mounted REST API. Skipped when the package is not installed.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest

pytest.importorskip("tactidose_wellbeing")
for _module in ("tactidose.auth.service", "tactidose.agent.service", "tactidose.db.seed"):
    pytest.importorskip(_module)

from tactidose.app import build_services, create_app  # noqa: E402
from tactidose.config import Settings  # noqa: E402
from tactidose.core.clock import Clock  # noqa: E402
from tactidose.api.events import EventScope  # noqa: E402
from tactidose.core.bus import BusEvent, Topic  # noqa: E402
from tactidose.wellbeing import CANCELLED_ON_STOP, CONSENT_NOTICE, MODEL_NAME, OFFER, STILL_OPEN  # noqa: E402
from tests.test_api_support import TestClient  # noqa: E402

pytestmark = pytest.mark.timeout(60)

TZ = "America/Vancouver"
FROZEN_AT = datetime(2026, 10, 5, 10, 30)
#: What to say for each kind of check-in prompt to walk through a whole check-in.
SAY = {"consent": "yes", "note_offer": "no", "confirm_answer": "yes", "note_confirmation": "yes",
       "finish_review": "yes"}


def _settings(tmp_path: Any, **over: Any) -> Settings:
    base: dict[str, Any] = dict(
        _env_file=None, data_dir=tmp_path / "data", hardware_mode="none", num_slots=3, voice_enabled=False,
        tts_provider="none", label_extractor="disabled", agent_provider="rules", report_ai_summary=False,
        timezone=TZ, demo_mode=True, seed_demo_accounts=True, hw_boot_wait_s=0, scheduler_tick_s=600,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def app_client(tmp_path):
    settings = _settings(tmp_path)
    services = build_services(settings, clock=Clock(TZ, frozen_at=FROZEN_AT))
    with TestClient(create_app(services=services)) as client:
        yield client, services, settings


def _login(client: Any, email: str, password: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _patient(client: Any, settings: Settings) -> tuple[dict[str, str], int]:
    headers = _login(client, "alex@demo.tactidose", settings.demo_password.get_secret_value())
    pid = client.get("/api/auth/me", headers=headers).json()["user"]["user_id"]
    return headers, pid


@pytest.fixture
def sim_client(tmp_path):
    settings = _settings(tmp_path, hardware_mode="sim", sim_speed=50.0, manual_cooldown_minutes=0)
    services = build_services(settings, clock=Clock(TZ, frozen_at=FROZEN_AT))
    with TestClient(create_app(services=services)) as client:
        yield client, services, settings


def _wait_ready(client: Any, headers: dict[str, str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/device", headers=headers).json().get("ready_for_motion"):
            return
        time.sleep(0.05)
    raise AssertionError("simulated device never became ready")


def _walk(client: Any, headers: dict[str, str], reply: dict[str, Any], *, note: str | None = None) -> dict[str, Any]:
    """Answer every prompt (first option; ``note`` at the first note offer) until the check-in ends."""
    for _ in range(30):
        state = reply["wellbeing"]
        if state.get("session_status") == "completed":
            return reply
        nq = state["next_question"]
        text = SAY.get(nq["kind"]) or nq["options"][0]
        if nq["kind"] == "question" and nq["question_id"] == "support":
            text = "no"
        if nq["kind"] == "note_offer" and note:
            text, note = note, None
        reply = _say(client, headers, text)
        assert reply["model"] == MODEL_NAME, reply
    raise AssertionError(f"check-in did not finish: {reply}")


def _say(client: Any, headers: dict[str, str], text: str) -> dict[str, Any]:
    r = client.post("/api/agent/chat", headers=headers, json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def _all_stored_text(client: Any, headers: dict[str, str], pid: int) -> str:
    parts: list[str] = []
    for conv in client.get(f"/api/patients/{pid}/conversations", headers=headers).json():
        msgs = client.get(f"/api/patients/{pid}/conversations/{conv['conversation_id']}/messages",
                          headers=headers).json()
        parts += [str(m.get("content") or "") for m in msgs]
    return "\n".join(parts).lower()


def test_full_checkin_over_chat_is_saved_and_never_stored_as_conversation(app_client):
    client, _services, settings = app_client
    alex, pid = _patient(client, settings)

    reply = _say(client, alex, "Start a well-being check-in")
    assert reply["model"] == MODEL_NAME and reply["actions"] == []
    assert reply["wellbeing"]["next_question"]["kind"] == "consent"
    for _ in range(25):
        state = reply["wellbeing"]
        if state["session_status"] == "completed":
            break
        nq = state["next_question"]
        text = SAY.get(nq["kind"]) or nq["options"][0]
        if nq["kind"] == "question" and nq["question_id"] == "support":
            text = "no"
        reply = _say(client, alex, text)
        assert reply["model"] == MODEL_NAME, reply
    assert reply["wellbeing"]["session_status"] == "completed", reply
    assert reply["wellbeing"]["record_id"]

    history = client.get("/api/wellbeing/v1/me/history", headers=alex)
    assert history.status_code == 200, history.text
    assert len(history.json()["records"]) == 1
    # Nothing from the check-in reached the agent's (caregiver-visible) conversation log.
    assert "well-being" not in _all_stored_text(client, alex, pid)


def test_pill_request_mid_checkin_goes_to_the_agent_and_checkin_stays_open(app_client):
    client, services, settings = app_client
    alex, pid = _patient(client, settings)
    _say(client, alex, "start a check in")

    reply = _say(client, alex, "can I have my pill")
    assert reply["model"].startswith("rules"), reply
    assert reply["text"].endswith(STILL_OPEN)
    assert services.wellbeing.has_open_checkin(pid)

    back = _say(client, alex, "repeat")
    assert back["model"] == MODEL_NAME
    assert back["wellbeing"]["next_question"]["kind"] == "consent"


def test_stop_mid_checkin_reaches_the_agent_and_cancels_the_checkin(app_client):
    client, services, settings = app_client
    alex, pid = _patient(client, settings)
    _say(client, alex, "well-being check")

    reply = _say(client, alex, "stop")
    assert reply["model"].startswith("rules"), reply
    assert reply["text"].endswith(CANCELLED_ON_STOP)
    assert not services.wellbeing.has_open_checkin(pid)


def test_ordinary_chat_is_untouched(app_client):
    client, _services, settings = app_client
    alex, _pid = _patient(client, settings)
    reply = _say(client, alex, "when is my next pill?")
    assert reply["model"].startswith("rules")
    assert "wellbeing" not in reply and STILL_OPEN not in reply["text"]


def test_rest_api_uses_tactidose_sessions_patients_only(app_client):
    client, _services, settings = app_client
    assert client.get("/api/wellbeing/v1/me/history").status_code == 401
    sam = _login(client, "sam@demo.tactidose", settings.demo_password.get_secret_value())
    r = client.get("/api/wellbeing/v1/me/history", headers=sam)
    assert r.status_code == 403 and r.json()["error"]["code"] == "patient_only"

    alex, pid = _patient(client, settings)
    start = client.post("/api/wellbeing/v1/sessions", headers=alex, json={"request_id": "req-1"})
    assert start.status_code == 201, start.text
    assert start.json()["user_id"] == f"tactidose-patient-{pid}"
    health = client.get("/api/wellbeing/v1/health").json()
    assert health["auth_mode"] == "tactidose_session" and health["auth_configured"] is True
    assert client.get("/api/health").json()["wellbeing"]["available"] is True


def test_disabled_checkin_is_not_mounted_and_chat_goes_to_the_agent(tmp_path):
    settings = _settings(tmp_path, wellbeing_enabled=False)
    services = build_services(settings, clock=Clock(TZ, frozen_at=FROZEN_AT))
    assert services.wellbeing is None
    with TestClient(create_app(services=services)) as client:
        alex, _pid = _patient(client, settings)
        assert client.get("/api/wellbeing/v1/health").status_code == 404
        reply = _say(client, alex, "start a check in")
        assert reply["model"].startswith("rules")
        assert client.get("/api/health").json()["wellbeing"] == {"available": False}


# --------------------------------------------------------------------------- after a drop


def test_drop_offers_checkin_saved_next_to_the_drop_and_visible_to_caregivers(sim_client):
    client, services, settings = sim_client
    alex, pid = _patient(client, settings)
    _wait_ready(client, alex)
    prompts = services.bus.subscribe([Topic.WELLBEING_PROMPT])

    drop = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 0}).json()
    assert drop["status"] == "DROPPED", drop
    ev = prompts.get(timeout=5)
    assert ev is not None and ev.data["user_id"] == pid and ev.data["drop_id"] == drop["drop_id"]
    assert ev.data["text"] == OFFER

    reply = _say(client, alex, "yes please")
    assert reply["model"] == MODEL_NAME and reply["wellbeing"]["drop_id"] == drop["drop_id"]
    assert CONSENT_NOTICE in reply["text"]
    _walk(client, alex, reply, note="A busy week at work.")

    sam = _login(client, "sam@demo.tactidose", settings.demo_password.get_secret_value())
    for who in (alex, sam):
        items = client.get(f"/api/patients/{pid}/wellbeing?days=7", headers=who).json()
        assert len(items) == 1, items
        item = items[0]
        assert item["after_drop"]["drop_id"] == drop["drop_id"]
        assert item["after_drop"]["medication_name"] == drop["medication_name"]
        notes = [a["note_text"] for a in item["answers"] if a["note_text"]]
        assert notes == ["A busy week at work."]
    # Still nothing in the agent's conversation log.
    assert "busy week" not in _all_stored_text(client, alex, pid)

    # Only the patient deletes; afterwards the care team no longer sees it.
    rid = items[0]["record_id"]
    assert client.delete(f"/api/wellbeing/v1/me/history/{rid}", headers=sam).status_code == 403
    assert client.delete(f"/api/wellbeing/v1/me/history/{rid}", headers=alex).status_code == 200
    assert client.get(f"/api/patients/{pid}/wellbeing", headers=sam).json() == []


def test_unrelated_message_dismisses_the_offer_and_reaches_the_agent(sim_client):
    client, services, settings = sim_client
    alex, pid = _patient(client, settings)
    _wait_ready(client, alex)
    assert client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 0}).json()["status"] == "DROPPED"
    assert services.wellbeing.has_pending_offer(pid)

    reply = _say(client, alex, "when is my next pill?")
    assert reply["model"].startswith("rules")
    assert not services.wellbeing.has_pending_offer(pid)
    assert not services.wellbeing.has_open_checkin(pid)


def test_no_thanks_declines_and_offers_are_rate_limited(sim_client):
    client, services, settings = sim_client
    alex, pid = _patient(client, settings)
    _wait_ready(client, alex)
    assert client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 0}).json()["status"] == "DROPPED"
    reply = _say(client, alex, "no thanks")
    assert reply["model"] == MODEL_NAME and reply["wellbeing"]["offer_declined"] is True
    assert not services.wellbeing.has_open_checkin(pid)
    # A second drop within wellbeing_after_drop_gap_minutes is not offered again.
    assert services.wellbeing.offer_after_drop(pid, 999_999) is None


def test_agent_drop_reply_carries_the_offer(sim_client):
    client, _services, settings = sim_client
    alex, _pid = _patient(client, settings)
    _wait_ready(client, alex)
    reply = _say(client, alex, "can I have my vitamin c")
    assert [a["status"] for a in reply["actions"]] == ["DROPPED"], reply
    assert reply["text"].endswith(OFFER)
    assert reply["wellbeing"]["kind"] == "offer" and reply["wellbeing"]["offer_id"]
    assert _say(client, alex, "yes")["wellbeing"]["next_question"]["kind"] == "consent"


def test_offer_event_goes_to_the_patient_only():
    ev = BusEvent(seq=1, topic=Topic.WELLBEING_PROMPT, data={"user_id": 1, "patient_id": 1, "text": OFFER})
    assert EventScope(user_id=1, patient_ids=frozenset({1}), device_patient_id=1, demo=True).permits(ev)
    assert not EventScope(user_id=2, patient_ids=frozenset({1}), device_patient_id=1, demo=True).permits(ev)
