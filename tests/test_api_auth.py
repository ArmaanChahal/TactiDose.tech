"""Auth & care-link endpoints: register/login/logout/me, the session cookie, bearer tokens."""

from __future__ import annotations

import pytest

from tests import test_api_support as support
from tests.test_api_support import PASSWORD

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _cookie_header(resp) -> str:
    return "; ".join(v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie")


def test_login_sets_httponly_cookie_and_returns_token(api):
    r = api.client.post("/api/auth/login", json={"email": "alex@test.tactidose", "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] and body["user"]["user_id"] == api.pid and body["user"]["role"] == "patient"
    assert set(body["user"]) >= {"user_id", "email", "display_name", "role", "phone", "created_at"}
    cookie = _cookie_header(r)
    assert cookie.startswith("td_session=") and "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower() and "Max-Age=43200" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie
    # the cookie alone authenticates
    me = api.client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"]["user_id"] == api.pid


def test_cookie_is_secure_when_configured(api_settings, make_api):
    h = make_api(api_settings.model_copy(update={"cookie_secure": True, "session_ttl_hours": 2}))
    r = h.client.post("/api/auth/login", json={"email": "sam@test.tactidose", "password": PASSWORD})
    cookie = _cookie_header(r)
    assert "Secure" in cookie and "Max-Age=7200" in cookie


def test_bad_credentials_are_401_with_the_same_message(api):
    wrong = api.client.post("/api/auth/login", json={"email": "alex@test.tactidose", "password": "nope-nope"})
    unknown = api.client.post("/api/auth/login", json={"email": "nobody@test.tactidose", "password": "nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert "td_session" not in _cookie_header(wrong)


def test_bearer_token_and_missing_session(api):
    assert api.get("/api/auth/me", actor=None).status_code == 401
    r = api.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"}, actor=None)
    assert r.status_code == 401 and r.json()["detail"]
    assert r.headers.get("www-authenticate") == "Bearer"
    assert api.get("/api/auth/me", actor="doctor").json()["user"]["role"] == "doctor"


def test_bearer_header_wins_over_cookie(api):
    api.client.cookies.set("td_session", api.tokens["patient"])
    r = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {api.tokens['doctor']}"})
    assert r.json()["user"]["user_id"] == api.uid("doctor")
    api.client.cookies.clear()


def test_me_for_patient_and_caregiver(api):
    me = api.get("/api/auth/me").json()
    assert me["patient"] == {"patient_id": api.pid, "link_code": "ALEX2026", "device_id": api.ids["device_id"]}
    assert "patients" not in me
    cg = api.get("/api/auth/me", actor="doctor").json()
    assert "patient" not in cg
    [p] = cg["patients"]
    assert p["patient_id"] == api.pid and p["relationship"] == "doctor" and p["display_name"] == "Alex Rivera"
    assert set(p) == {"patient_id", "display_name", "relationship", "last_drop", "unread_alerts", "adherence_7d"}
    assert p["unread_alerts"] == 1  # LOW_STOCK counts, PILL_DROPPED does not
    assert p["last_drop"]["drop_id"] == api.ids["drop_id"]   # the seeded UNCERTAIN drop
    assert p["adherence_7d"] is None


def test_register_patient_logs_in_and_returns_link_code(api):
    r = api.client.post("/api/auth/register", json={
        "email": "New@Test.Tactidose", "password": "longenough", "display_name": "New Patient", "role": "patient"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token"] and body["user"]["role"] == "patient"
    assert body["patient"] == {"patient_id": body["user"]["user_id"], "link_code": "NEWCODE1"}
    assert "td_session=" in _cookie_header(r)
    assert api.client.get("/api/auth/me").json()["user"]["email"] == "new@test.tactidose"


def test_register_caregiver_has_no_patient_block(api):
    r = api.client.post("/api/auth/register", json={
        "email": "fam2@test.tactidose", "password": "longenough", "display_name": "Fam", "role": "family",
        "phone": "+1 604 555 0100"})
    assert r.status_code == 201 and "patient" not in r.json()


REGISTER_ERRORS = [
    ({"email": "alex@test.tactidose", "password": "longenough", "display_name": "Dup", "role": "patient"}, 409),
    ({"email": "short@test.tactidose", "password": "short", "display_name": "S", "role": "patient"}, 422),
    ({"email": "x@test.tactidose", "password": "longenough", "display_name": "X", "role": "admin"}, 422),
    ({"email": "x@test.tactidose", "password": "longenough", "role": "patient"}, 422),
]


def test_register_errors(api):
    for body, status in REGISTER_ERRORS:
        r = api.client.post("/api/auth/register", json=body)
        assert r.status_code == status, (body, r.text)
        assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_register_disabled_is_403(api_settings, make_api):
    h = make_api(api_settings.model_copy(update={"allow_registration": False}))
    r = h.client.post("/api/auth/register", json={
        "email": "n@test.tactidose", "password": "longenough", "display_name": "N", "role": "patient"})
    assert r.status_code == 403


def test_logout_revokes_and_clears_cookie(api):
    login = api.client.post("/api/auth/login", json={"email": "sam@test.tactidose", "password": PASSWORD})
    token = login.json()["token"]
    r = api.client.post("/api/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    cookie = _cookie_header(r)
    assert "td_session=" in cookie and ("Max-Age=0" in cookie or "expires=" in cookie.lower())
    assert token not in api.services.auth.tokens
    assert api.client.get("/api/auth/me").status_code == 401
    again = api.client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 401 and "td_session=" in _cookie_header(again)


def test_care_patients_and_links(api):
    assert api.get("/api/care/patients", actor="patient").status_code == 403
    assert api.get("/api/care/patients", actor="stranger").json() == []
    bad = api.post("/api/care/links", actor="stranger", json={"patient_id": api.pid, "link_code": "WRONG"})
    assert bad.status_code == 403
    missing = api.post("/api/care/links", actor="stranger", json={"patient_id": 9999, "link_code": "ALEX2026"})
    assert missing.status_code == 404
    ok = api.post("/api/care/links", actor="stranger", json={"patient_id": api.pid, "link_code": "alex2026"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["patient_id"] == api.pid and ok.json()["relationship"] == "doctor"
    assert "unread_alerts" in ok.json() and "adherence_7d" in ok.json()
    assert [p["patient_id"] for p in api.get("/api/care/patients", actor="stranger").json()] == [api.pid]
    # now the stranger may view the patient
    assert api.get(f"/api/patients/{api.pid}/status", actor="stranger").status_code == 200
    gone = api.request("DELETE", f"/api/care/links/{api.pid}", actor="stranger")
    assert gone.status_code == 200 and gone.json() == {"ok": True}
    assert api.get(f"/api/patients/{api.pid}/status", actor="stranger").status_code == 403
    assert api.request("DELETE", f"/api/care/links/{api.pid}", actor="stranger").status_code == 404
    assert api.post("/api/care/links", actor="patient", json={"patient_id": api.pid, "link_code": "ALEX2026"}).status_code == 403


def test_link_body_validation(api):
    r = api.post("/api/care/links", actor="doctor", json={"patient_id": "1", "link_code": "X"})
    assert r.status_code == 422 and "patient_id" in r.json()["detail"]
    r = api.post("/api/care/links", actor="doctor", json={"patient_id": 1, "link_code": "X", "extra": 1})
    assert r.status_code == 422
