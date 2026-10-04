"""Agent chat / transcribe / audio, notifications and report endpoints."""

from __future__ import annotations

from typing import Any

from tests import test_api_support as support
from tests.test_api_support import AgentUnavailable

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api


REPLY_KEYS = {"conversation_id", "text", "model", "actions", "messages", "audio_url"}
PCM = {"Content-Type": "application/octet-stream"}


# --------------------------------------------------------------------------- chat


def test_chat_uses_the_session_patient_and_may_request_a_pill(api):
    r = api.post("/api/agent/chat", json={"text": "  Can I have my vitamin C?  ", "input_mode": "voice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == REPLY_KEYS and body["model"] == "rules" and body["audio_url"] is None
    assert api.services.agent.chats == [{"patient_id": api.pid, "text": "Can I have my vitamin C?",
                                         "input_mode": "voice", "conversation_id": None}]
    [req] = api.services.drops.requests
    assert req["source"] == "agent" and req["patient_id"] == api.pid
    assert body["actions"][0]["status"] == "DROPPED"
    follow = api.post("/api/agent/chat", json={"text": "thanks", "conversation_id": body["conversation_id"]}).json()
    assert follow["conversation_id"] == body["conversation_id"]


def test_chat_speak_returns_an_audio_url_for_this_patient_only(api):
    body = api.post("/api/agent/chat", json={"text": "hello", "speak": True}).json()
    assert body["audio_url"] == "/api/agent/audio/a1.wav"
    r = api.get(body["audio_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")
    assert api.get(body["audio_url"], actor="other").status_code == 404
    assert api.get("/api/agent/audio/../../x.wav").status_code == 404
    assert api.get("/api/agent/audio/zzz.wav").status_code == 404


def test_chat_tts_failure_still_answers(api, monkeypatch):
    def broken(*a: Any) -> Any:
        raise OSError("no audio device")

    monkeypatch.setattr(api.services.agent, "speak", broken)
    r = api.post("/api/agent/chat", json={"text": "hello", "speak": True})
    assert r.status_code == 200 and r.json()["audio_url"] is None


def test_chat_validation(api):
    for body in ({}, {"text": ""}, {"text": "   "}, {"text": "x" * 2001}, {"text": "hi", "input_mode": "telepathy"},
                 {"text": "hi", "conversation_id": "1"}, {"text": "hi", "patient_id": 2}):
        assert api.post("/api/agent/chat", json=body).status_code == 422, body
    assert api.services.agent.chats == []


def test_chat_without_agent_is_503(api_settings, make_api):
    h = make_api(api_settings)
    h.services.agent = None
    assert h.post("/api/agent/chat", json={"text": "hi"}).status_code == 503
    assert h.get(f"/api/patients/{h.pid}/conversations").json() == []


# --------------------------------------------------------------------------- transcribe


def test_transcribe_success(api):
    pcm = b"\x01\x00" * 16000
    r = api.post("/api/agent/transcribe", content=pcm, headers=PCM)
    assert r.status_code == 200 and r.json() == {"text": "drop my vitamin c", "confidence": 0.91, "engine": "vosk"}
    assert api.services.agent.transcribed == [pcm]


def test_transcribe_validation(api):
    for size, headers, status in ((2, {"Content-Type": "audio/wav"}, 415), (0, PCM, 422), (3, PCM, 422),
                                  (960_002, PCM, 413)):
        r = api.post("/api/agent/transcribe", content=b"\x00" * size, headers=headers)
        assert r.status_code == status, (size, r.status_code)
    assert api.services.agent.transcribed == []


def test_transcribe_accepts_exactly_30_seconds(api):
    assert api.post("/api/agent/transcribe", content=b"\x00" * 960_000, headers=PCM).status_code == 200


def test_transcribe_unavailable_is_503(api, monkeypatch):
    api.services.agent.transcribe_result = AgentUnavailable("Vosk model not found at C:/secret/path")
    r = api.post("/api/agent/transcribe", content=b"\x00\x00", headers=PCM)
    assert r.status_code == 503 and "secret" not in r.text and "type" in r.json()["detail"].lower()
    monkeypatch.delattr(type(api.services.agent), "transcribe")
    assert api.post("/api/agent/transcribe", content=b"\x00\x00", headers=PCM).status_code == 503


# --------------------------------------------------------------------------- notifications


def test_notifications_are_per_user(api):
    mine = api.get("/api/notifications").json()
    assert [n["kind"] for n in mine] == ["PILL_DROPPED"] and mine[0]["user_id"] == api.pid
    doc = api.get("/api/notifications?unread=true&limit=10", actor="doctor").json()
    assert {n["kind"] for n in doc} == {"LOW_STOCK", "PILL_DROPPED"}
    # the doctor cannot mark the patient's notification as read
    assert api.post("/api/notifications/read", actor="doctor", json={"ids": [mine[0]["notification_id"]]}).json() == \
        {"updated": 0}
    assert api.post("/api/notifications/read", json={"ids": [mine[0]["notification_id"]]}).json() == {"updated": 1}
    assert api.get("/api/notifications?unread=true").json() == []
    assert api.post("/api/notifications/read", actor="doctor").json() == {"updated": 2}
    assert api.post("/api/notifications/read", actor="doctor", json={"ids": ["x"]}).status_code == 422
    assert api.get("/api/notifications?limit=0").status_code == 422


# --------------------------------------------------------------------------- reports


def _report(h: Any, actor: str = "patient", days: int = 7) -> dict[str, Any]:
    r = h.post(f"/api/patients/{h.pid}/reports", actor=actor, json={"days": days})
    assert r.status_code == 201, r.text
    return r.json()


def test_report_meta_pdf_and_send(api):
    meta = _report(api, "doctor")
    rid = meta["report_id"]
    got = api.get(f"/api/reports/{rid}", actor="family").json()
    assert got["report_id"] == rid and got["pdf_url"] == f"/api/reports/{rid}/pdf" and got["deliveries"] == []
    pdf = api.get(f"/api/reports/{rid}/pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and pdf.headers["cache-control"] == "no-store"
    assert pdf.headers["content-disposition"] == f'inline; filename="tactidose-report-{rid}.pdf"'
    dl = api.get(f"/api/reports/{rid}/pdf?download=1")
    assert dl.headers["content-disposition"].startswith("attachment;")
    sent = api.post(f"/api/reports/{rid}/send", json={})
    assert sent.status_code == 200 and sent.json()["deliveries"][0]["status"] == "SAVED"
    assert api.services.reports.sent[-1] == {"report_id": rid, "sent_by_user_id": api.pid, "to_email": None}
    api.post(f"/api/reports/{rid}/send", actor="doctor", json={"to_email": " clinic@example.com "})
    assert api.services.reports.sent[-1]["to_email"] == "clinic@example.com"
    assert len(api.get(f"/api/reports/{rid}").json()["deliveries"]) == 2
    assert api.post(f"/api/reports/{rid}/send", json={"to": "x"}).status_code == 422


def test_reports_unavailable(api_settings, make_api):
    h = make_api(api_settings)
    h.services.reports = None
    assert h.get(f"/api/patients/{h.pid}/reports").json() == []
    assert h.post(f"/api/patients/{h.pid}/reports", json={"days": 7}).status_code == 503
    assert h.get("/api/reports/1").status_code == 503
