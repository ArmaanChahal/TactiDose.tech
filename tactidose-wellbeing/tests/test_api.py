"""REST API: endpoints, auth fail-safe, status codes, ownership and OpenAPI."""

from fastapi.testclient import TestClient

from tactidose_wellbeing.api import StaticTokenIdentityProvider, UnconfiguredIdentityProvider, create_app
from tactidose_wellbeing.cli import main as cli_main

from .conftest import dev_headers

A = dev_headers("user-a")
B = dev_headers("user-b")


def _start(client, headers=A, rid="s-1"):
    r = client.post("/v1/sessions", json={"schema_version": "1.0", "request_id": rid}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _act(client, sid, rid, action, headers=A, **kw):
    body = {"schema_version": "1.0", "request_id": rid, "action": action, **kw}
    return client.post(f"/v1/sessions/{sid}/actions", json=body, headers=headers)


def test_health(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["auth_mode"] == "dev" and r.json()["schema_version"] == "1.0"


def test_full_flow_with_notes_history_and_deletion(client):
    sid = _start(client)["session_id"]
    assert _act(client, sid, "1", "answer", answer="yes").status_code == 200
    r = _act(client, sid, "2", "answer", answer="low", question_id="mood", expected_step=1).json()
    assert r["next_question"]["kind"] == "note_offer"
    r = _act(client, sid, "3", "add_note", note_text="Synthetic note text").json()
    assert r["pending_input"] == {"confirmed": False, "kind": "note_draft", "question_id": "mood",
                                  "candidate_value": None, "note_text": "Synthetic note text"}
    _act(client, sid, "4", "confirm")
    r = _act(client, sid, "5", "finish").json()
    assert r["session_status"] == "completed"

    hist = client.get("/v1/me/history", headers=A).json()
    rec = hist["records"][0]
    assert rec["answers"][0]["note_text"] == "Synthetic note text"
    r = client.delete(f"/v1/me/history/{rec['record_id']}/notes/mood", headers=A)
    assert r.status_code == 200 and r.json()["deleted_notes"] == 1
    assert client.get("/v1/me/history", headers=A).json()["records"][0]["answers"][0]["note_text"] is None
    r = client.delete("/v1/me/history", headers=A)
    assert r.json()["deleted_records"] == 1
    assert client.get("/v1/me/history", headers=A).json()["records"] == []


def test_get_session_state(client):
    sid = _start(client)["session_id"]
    r = client.get(f"/v1/sessions/{sid}", headers=A)
    assert r.status_code == 200 and r.json()["session_status"] == "awaiting_consent"


def test_state_errors_use_409_with_full_envelope(client):
    sid = _start(client)["session_id"]
    r = _act(client, sid, "1", "confirm")
    assert r.status_code == 200  # confirm = consent yes
    r = _act(client, sid, "2", "confirm")  # nothing to confirm now
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "invalid_action"
    assert body["next_question"]["question_id"] == "mood"


def test_idempotency_over_http(client):
    sid = _start(client)["session_id"]
    _act(client, sid, "1", "answer", answer="yes")
    a = _act(client, sid, "2", "answer", answer="good").json()
    b = _act(client, sid, "2", "answer", answer="good").json()
    assert b["idempotent_replay"] is True and a["step"] == b["step"]
    c = _act(client, sid, "2", "answer", answer="low")
    assert c.status_code == 409 and c.json()["error"]["code"] == "idempotency_conflict"
    assert c.json()["request_id"] == "2"
    assert _start(client)["session_id"] == sid  # start retry


def test_ownership_over_http(client):
    sid = _start(client)["session_id"]
    assert _act(client, sid, "1", "answer", headers=B, answer="yes").status_code == 404
    assert client.get(f"/v1/sessions/{sid}", headers=B).status_code == 404
    r = client.post("/v1/sessions", json={"request_id": "x", "user_id": "user-b"}, headers=A)
    assert r.status_code == 403 and r.json()["error"]["code"] == "user_mismatch"
    assert client.delete("/v1/me/history/rec_0001", headers=B).status_code == 404


def test_missing_identity_is_401(client):
    r = client.post("/v1/sessions", json={"request_id": "x"})
    assert r.status_code == 401


def test_unconfigured_auth_fails_safe(service):
    client = TestClient(create_app(service, UnconfiguredIdentityProvider()))
    assert client.get("/v1/health").json()["auth_configured"] is False
    for method, path in [("post", "/v1/sessions"), ("get", "/v1/me/history"), ("delete", "/v1/me/history")]:
        r = client.request(method, path, json={"request_id": "x"}, headers=A)
        assert r.status_code == 503 and r.json()["error"]["code"] == "auth_not_configured"


def test_dev_identity_requires_loopback_by_default(service):
    from tactidose_wellbeing.api import DevHeaderIdentityProvider

    client = TestClient(create_app(service, DevHeaderIdentityProvider()))  # client host = "testclient"
    assert client.post("/v1/sessions", json={"request_id": "x"}, headers=A).status_code == 401


def test_static_token_auth(service):
    client = TestClient(create_app(service, StaticTokenIdentityProvider({"tok-123": "user-t"})))
    h = {"Authorization": "Bearer tok-123"}
    assert client.post("/v1/sessions", json={"request_id": "x"}, headers=h).json()["user_id"] == "user-t"
    bad = client.post("/v1/sessions", json={"request_id": "y"}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    # The dev header is ignored in token mode.
    assert client.post("/v1/sessions", json={"request_id": "z"}, headers=A).status_code == 401


def test_validation_errors_do_not_echo_input(client):
    sid = _start(client)["session_id"]
    secret = "synthetic secret " * 100  # too long
    r = _act(client, sid, "1", "add_note", note_text=secret)
    assert r.status_code == 422
    assert "synthetic secret" not in r.text
    r = _act(client, sid, "2", "answer")  # missing answer
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = _act(client, sid, "3", "skip", answer="yes")
    assert r.status_code == 422
    r = _act(client, sid, "4", "dispense")
    assert r.status_code == 422


def test_sharing_endpoints(client):
    sid = _start(client)["session_id"]
    for i, (action, kw) in enumerate([("answer", {"answer": "yes"}), ("answer", {"answer": "low"}),
                                      ("add_note", {"note_text": "n"}), ("confirm", {}), ("finish", {})]):
        _act(client, sid, str(i), action, **kw)
    rid = client.get("/v1/me/history", headers=A).json()["records"][0]["record_id"]
    assert client.get(f"/v1/me/history/{rid}/shareable", headers=A).json()["answers"] == []
    r = client.put(f"/v1/me/history/{rid}/sharing", json={"share_answers": True}, headers=A)
    assert r.json()["sharing"] == {"share_answers": True, "share_notes": False}
    shared = client.get(f"/v1/me/history/{rid}/shareable", headers=A).json()["answers"]
    assert shared[0]["answer_value"] == "low" and shared[0]["note_text"] is None


def test_openapi_document(client, tmp_path):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    for p in ["/v1/health", "/v1/sessions", "/v1/sessions/{session_id}/actions",
              "/v1/sessions/{session_id}", "/v1/me/history", "/v1/me/history/{record_id}",
              "/v1/me/history/{record_id}/notes/{question_id}"]:
        assert p in paths
    assert "CheckinResponse" in spec["components"]["schemas"]
    out = tmp_path / "openapi.json"
    assert cli_main(["openapi", "--output", str(out)]) == 0
    assert out.read_text().startswith("{")


def test_cli_serve_refuses_dev_auth_on_public_interface(capsys):
    assert cli_main(["serve", "--dev-auth", "--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err
