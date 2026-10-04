"""The ARCHITECTURE §9 permission matrix across every protected endpoint and every role.

Rules: ``view`` (patient themself + linked doctor/family), ``edit`` (linked doctor/family only),
``self`` (the patient only). Anonymous callers always get 401; everyone else who is not allowed
gets 403. For each endpoint the denied actors run first (they must not change anything), then the
allowed ones (the first must succeed; later ones may hit a 4xx *domain* answer such as 409 when the
first already changed the state, but never 401/403).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests import test_api_support as support

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api


VIEW = ("patient", "family", "doctor")
EDIT = ("family", "doctor")
SELF = ("patient",)
EVERYONE = ("patient", "family", "doctor", "other", "stranger")

PATIENT_ENDPOINTS: list[tuple[tuple[str, ...], str, str, Any]] = [
    (VIEW, "GET", "/api/patients/{pid}/status", None),
    (VIEW, "GET", "/api/patients/{pid}/containers", None),
    (VIEW, "GET", "/api/patients/{pid}/medications", None),
    (VIEW, "GET", "/api/patients/{pid}/schedules", None),
    (VIEW, "GET", "/api/patients/{pid}/settings", None),
    (VIEW, "GET", "/api/patients/{pid}/drops", None),
    (VIEW, "GET", "/api/patients/{pid}/doses?date=2026-10-05", None),
    (VIEW, "GET", "/api/patients/{pid}/conversations", None),
    (VIEW, "GET", "/api/patients/{pid}/conversations/{conversation_id}/messages", None),
    (VIEW, "GET", "/api/patients/{pid}/reports", None),
    (VIEW, "POST", "/api/patients/{pid}/reports", {"days": 7}),
    (EDIT, "PUT", "/api/patients/{pid}/containers/0", {"pill_count": 10}),
    (EDIT, "POST", "/api/patients/{pid}/containers/1/refill", {"add": 2}),
    (EDIT, "POST", "/api/patients/{pid}/medications", {"name": "Zinc (demo token)", "confirmed": True}),
    (EDIT, "PATCH", "/api/patients/{pid}/medications/{med0}", {"strength": "2 pieces", "confirmed": True}),
    (EDIT, "DELETE", "/api/patients/{pid}/medications/{med2}", None),
    (EDIT, "POST", "/api/patients/{pid}/schedules", {"medication_id": "{med0}", "time_of_day": "09:30"}),
    (EDIT, "PATCH", "/api/patients/{pid}/schedules/{sched0}", {"time_of_day": "08:15"}),
    (EDIT, "DELETE", "/api/patients/{pid}/schedules/{sched2}", None),
    (EDIT, "PATCH", "/api/patients/{pid}/settings", {"manual_cooldown_minutes": 30}),
    (EDIT, "POST", "/api/patients/{pid}/drops/{drop_id}/resolve", {"dropped": False, "note": "checked"}),
    (EDIT, "POST", "/api/patients/{pid}/doses/{event_id}/skip", {"note": "travelling"}),
    (EDIT, "POST", "/api/patients/{pid}/scans/{scan_id}/reject", None),
    (EDIT, "POST", "/api/patients/{pid}/scans/{scan_id}/confirm", {"name": "Vitamin C", "confirmed": True}),
    (SELF, "POST", "/api/patients/{pid}/drops", {"slot": 0}),
]


def _fill(h: Any, template: Any) -> Any:
    values = {
        "pid": h.pid, "conversation_id": h.ids["conversation_id"], "drop_id": h.ids["drop_id"],
        "event_id": h.ids["event_id"], "scan_id": h.ids["scan_id"],
        "med0": h.ids["med_ids"][0], "med2": h.ids["med_ids"][2],
        "sched0": h.ids["schedule_ids"][0], "sched2": h.ids["schedule_ids"][2],
    }
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template[1:-1] in values:
            return values[template[1:-1]]
        return template.format(**values)
    if isinstance(template, dict):
        return {k: _fill(h, v) for k, v in template.items()}
    return template


def check_matrix(h: Any, allowed: tuple[str, ...], method: str, path: str, body: Any) -> None:
    url = _fill(h, path)
    payload = _fill(h, body)
    kw = {"json": payload} if payload is not None else {}
    anon = h.request(method, url, actor=None, **kw)
    assert anon.status_code == 401, (url, anon.status_code, anon.text)
    for actor in EVERYONE:
        if actor not in allowed:
            r = h.request(method, url, actor=actor, **kw)
            assert r.status_code == 403, (actor, method, url, r.status_code, r.text)
            assert isinstance(r.json()["detail"], str)
    for i, actor in enumerate(allowed):
        r = h.request(method, url, actor=actor, **kw)
        if i == 0:
            assert r.status_code < 300, (actor, method, url, r.status_code, r.text)
        else:
            assert r.status_code not in (401, 403) and r.status_code < 500, (actor, url, r.status_code, r.text)


@pytest.mark.parametrize("allowed,method,path,body", PATIENT_ENDPOINTS,
                         ids=[f"{m} {p}" for _a, m, p, _b in PATIENT_ENDPOINTS])
def test_patient_endpoint_matrix(api, allowed, method, path, body):
    check_matrix(api, allowed, method, path, body)


def test_denied_actors_change_nothing(api):
    """403s happen before any service call."""
    s = api.services
    for actor in ("patient", "other", "stranger"):
        api.request("PATCH", f"/api/patients/{api.pid}/settings", actor=actor, json={"manual_cooldown_minutes": 1})
        api.request("DELETE", f"/api/patients/{api.pid}/medications/{api.ids['med_ids'][0]}", actor=actor)
    for actor in ("family", "doctor", "other", "stranger"):
        api.post(f"/api/patients/{api.pid}/drops", actor=actor, json={"slot": 0})
    assert s.drops.requests == [] and s.catalog.calls == []
    assert not [c for c in s.drops.calls if c[0] == "update_settings"]


def test_other_patient_reaches_only_their_own_data(api):
    other = api.uid("other")
    assert api.get(f"/api/patients/{other}/status", actor="other").status_code == 200
    assert api.get(f"/api/patients/{other}/containers", actor="other").json() == []
    assert api.get(f"/api/patients/{other}/settings", actor="other").status_code == 404  # no device
    # a conversation id of another patient is not found through one's own pid
    r = api.get(f"/api/patients/{other}/conversations/{api.ids['conversation_id']}/messages", actor="other")
    assert r.status_code == 404
    r = api.get(f"/api/patients/{api.pid}/conversations/{api.ids['other_conversation_id']}/messages")
    assert r.status_code == 404


def test_cross_patient_record_ids_are_404_for_linked_caregivers(api):
    """A caregiver linked to Alex cannot reach another patient's records through Alex's pid."""
    other = api.uid("other")
    with api.services.db.session() as s:
        from tactidose.db.models import Medication

        m = Medication(user_id=other, name="Other med", confirmed_by_user=True)
        s.add(m)
        s.flush()
        mid = m.medication_id
    r = api.request("PATCH", f"/api/patients/{api.pid}/medications/{mid}", actor="doctor",
                    json={"strength": "x", "confirmed": True})
    assert r.status_code == 404
    r = api.request("DELETE", f"/api/patients/{api.pid}/medications/{mid}", actor="doctor")
    assert r.status_code == 404
    r = api.post(f"/api/patients/{api.pid}/schedules", actor="doctor", json={"medication_id": mid, "time_of_day": "08:00"})
    assert r.status_code == 404
    assert api.services.catalog.calls == [] and api.services.scheduler.calls == []


def _make_report(h: Any) -> int:
    r = h.post(f"/api/patients/{h.pid}/reports", json={"days": 7})
    assert r.status_code == 201, r.text
    return r.json()["report_id"]


def test_report_endpoints_follow_the_reports_patient(api):
    rid = _make_report(api)
    for method, suffix, body in (("GET", "", None), ("GET", "/pdf", None),
                                 ("POST", "/send", {"to_email": "doc@example.com"})):
        check_matrix(api, VIEW, method, f"/api/reports/{rid}{suffix}", body)


def test_unknown_report_is_404(api):
    assert api.get("/api/reports/424242").status_code == 404
    assert api.get("/api/reports/424242", actor="stranger").status_code == 404


def test_device_endpoints(api):
    for method, path, allowed in (("GET", "/api/device", VIEW), ("POST", "/api/device/stop", VIEW),
                                  ("POST", "/api/device/home", EDIT), ("POST", "/api/device/reconnect", EDIT)):
        check_matrix(api, allowed, method, path, None)


def test_agent_endpoints_are_patient_only(api):
    for actor in ("family", "doctor", "stranger"):
        assert api.post("/api/agent/chat", actor=actor, json={"text": "hello"}).status_code == 403
        assert api.post("/api/agent/transcribe", actor=actor, content=b"\x00\x00",
                        headers={"Content-Type": "application/octet-stream"}).status_code == 403
        assert api.get("/api/agent/audio/a1.wav", actor=actor).status_code == 403
    assert api.post("/api/agent/chat", actor=None, json={"text": "hello"}).status_code == 401
    r = api.post("/api/agent/chat", actor="other", json={"text": "hello"})
    assert r.status_code == 200
    assert api.services.agent.chats[-1]["patient_id"] == api.uid("other")  # never Alex


def test_caregiver_and_any_user_endpoints(api):
    for actor in ("family", "doctor", "stranger"):
        assert api.get("/api/care/patients", actor=actor).status_code == 200
    for actor in ("patient", "other"):
        assert api.get("/api/care/patients", actor=actor).status_code == 403
        assert api.post("/api/care/links", actor=actor, json={"patient_id": 1, "link_code": "X"}).status_code == 403
    for actor in EVERYONE:
        assert api.get("/api/notifications", actor=actor).status_code == 200
        assert api.post("/api/notifications/read", actor=actor, json={}).status_code == 200
        assert api.get("/api/auth/me", actor=actor).status_code == 200
    for path in ("/api/notifications", "/api/auth/me", "/api/care/patients", "/api/events"):
        assert api.get(path, actor=None).status_code == 401


def test_analytics_summary_follows_view_rule(api):
    url = f"/api/analytics/summary?patient_id={api.pid}&days=7"
    assert api.get(url, actor=None).status_code == 401
    for actor in ("other", "stranger"):
        assert api.get(url, actor=actor).status_code == 403
    for actor in VIEW:
        r = api.get(url, actor=actor)
        assert r.status_code == 200, r.text
        assert r.json()["patient_id"] == api.pid and "totals" in r.json()
    assert api.get("/api/analytics/summary", actor="patient").json()["patient_id"] == api.pid
    assert api.get("/api/analytics/summary", actor="doctor").json()["patient_id"] == api.pid
    assert api.get("/api/analytics/summary", actor="stranger").status_code == 422
    assert api.get(f"/api/analytics/summary?patient_id={api.uid('other')}", actor="other").status_code == 404


DEMO_ENDPOINTS = [
    ("GET", "/api/demo/clock", None),
    ("POST", "/api/demo/clock", {"local_time": "08:00"}),
    ("POST", "/api/demo/jump-to-next-dose", None),
    ("GET", "/api/demo/simulator", None),
    ("POST", "/api/demo/simulator", {"reboot": True}),
    ("POST", "/api/demo/command", {"line": "STATUS"}),
]


def test_demo_endpoints_need_a_session_in_demo_mode(api):
    for method, path, body in DEMO_ENDPOINTS:
        kw = {"json": body} if body is not None else {}
        assert api.request(method, path, actor=None, **kw).status_code == 401, path
        # The device's care team only; the raw console is for linked doctor/family.
        allowed = ("family", "doctor") if path == "/api/demo/command" else ("patient", "family", "doctor")
        for actor in EVERYONE:
            r = api.request(method, path, actor=actor, **kw)
            assert r.status_code == (200 if actor in allowed else 403), (actor, path, r.text)


def test_demo_endpoints_are_403_when_demo_mode_is_off(api_settings, make_api):
    h = make_api(api_settings.model_copy(update={"demo_mode": False}))
    for method, path, body in DEMO_ENDPOINTS + [("POST", "/api/demo/reset", {"reseed": True})]:
        kw = {"json": body} if body is not None else {}
        for actor in (None, "patient", "doctor"):
            r = h.request(method, path, actor=actor, **kw)
            assert r.status_code == 403, (actor, path, r.status_code)
    assert h.client.get("/demo").status_code == 404
    assert h.services.sim.reboots == 0 and h.services.hardware.commands() == []
