"""Patient-data endpoints: shapes per docs/API.md v2, how requests reach the services, error
mapping, input validation and the scheduler trigger after edits."""

from __future__ import annotations

from typing import Any

import pytest

from tactidose.core.interfaces import DropOutcome
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tests import test_api_support as support

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings


CONTAINER_KEYS = {"slot", "container_number", "compartment_id", "medication_id", "medication_name", "strength",
                  "pill_count", "capacity", "low_stock_threshold", "low_stock", "empty", "loaded_at"}
DROP_OUTCOME_KEYS = {"status", "reason", "message", "drop_id", "slot", "container_number", "medication_id",
                     "medication_name", "source", "pill_count_after", "cooldown_remaining_s", "next_allowed_at",
                     "hardware"}
STATUS_KEYS = {"patient_id", "display_name", "now_local", "containers", "cooldown_minutes", "cooldown_remaining_s",
               "next_manual_allowed_at", "last_drop", "today", "next_scheduled", "auto_drop_enabled", "device",
               "alerts"}


def url(h: Any, suffix: str) -> str:
    return f"/api/patients/{h.pid}{suffix}"


# --------------------------------------------------------------------------- reads


def test_status_and_containers_shapes(api):
    st = api.get(url(api, "/status")).json()
    assert set(st) == STATUS_KEYS and st["patient_id"] == api.pid
    containers = api.get(url(api, "/containers")).json()
    assert [c["slot"] for c in containers] == [0, 1, 2]
    assert all(set(c) == CONTAINER_KEYS for c in containers)
    assert containers[0]["container_number"] == 1 and containers[0]["pill_count"] == 20


def test_medications_schedules_settings(api):
    meds = api.get(url(api, "/medications")).json()
    assert [m["name"] for m in meds][:1] == ["Vitamin C (demo candy)"] and len(meds) == 3
    scheds = api.get(url(api, "/schedules")).json()
    assert sorted(s["time_of_day"] for s in scheds) == ["08:00", "13:00", "20:00"]
    st = api.get(url(api, "/settings")).json()
    assert st == {"manual_cooldown_minutes": 60, "auto_drop_enabled": True, "device_id": api.ids["device_id"],
                  "num_slots": 3}


def test_drops_listing_and_status_filter(api):
    rows = api.get(url(api, "/drops?days=7")).json()
    assert [r["drop_id"] for r in rows] == [api.ids["drop_id"]]
    assert api.get(url(api, "/drops?status=uncertain")).json()[0]["status"] == "UNCERTAIN"
    assert api.get(url(api, "/drops?status=DROPPED")).json() == []
    assert api.get(url(api, "/drops?status=BOGUS")).status_code == 422
    assert api.get(url(api, "/drops?days=0")).status_code == 422
    assert ("recent_drops", {"patient_id": api.pid, "days": 7, "limit": 200, "status": None}) in api.services.drops.calls
    assert ("recent_drops", {"patient_id": api.pid, "days": 7, "limit": 200, "status": "UNCERTAIN"}) in \
        api.services.drops.calls
    both = api.get(url(api, "/drops?status=DROPPED,UNCERTAIN")).json()
    assert [r["drop_id"] for r in both] == [api.ids["drop_id"]]


def test_doses_default_to_today_local(api):
    today = api.get(url(api, "/doses")).json()
    # the seeded dose is 2026-10-05 20:00 UTC = 13:00 local on the frozen "today" (2026-10-05)
    assert [d["event_id"] for d in today] == [api.ids["event_id"]]
    call = [c for c in api.services.drops.calls if c[0] == "list_doses"][-1][1]
    assert str(call["local_date"]) == "2026-10-05"
    assert api.get(url(api, "/doses?date=2026-10-06")).json() == []
    assert api.get(url(api, "/doses?date=05/10/2026")).status_code == 422


def test_conversations_and_messages(api):
    convs = api.get(url(api, "/conversations?limit=10")).json()
    assert convs[0]["conversation_id"] == api.ids["conversation_id"]
    msgs = api.get(url(api, f"/conversations/{api.ids['conversation_id']}/messages"), actor="family").json()
    assert msgs[0]["content"] == "Can I have my pill?" and msgs[0]["role"] == "user"
    assert api.get(url(api, "/conversations/99999/messages")).status_code == 404
    assert api.get(url(api, "/conversations?limit=0")).status_code == 422


# --------------------------------------------------------------------------- manual drops


def test_manual_drop_goes_through_drop_service(api):
    r = api.post(url(api, "/drops"), json={"slot": 1})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == DROP_OUTCOME_KEYS and body["status"] == "DROPPED"
    assert api.services.drops.requests == [dict(
        patient_id=api.pid, source="manual", slot=1, medication_id=None, requested_by_user_id=api.pid,
        conversation_id=None, dose_event_id=None)]
    assert api.services.hardware.commands() == []   # the API never touches the hardware for a drop


def test_denied_drop_is_still_http_200(api):
    api.services.drops.outcomes.append(DropOutcome(
        status="DENIED", reason="COOLDOWN", source="manual", message="You can have another pill at 9:05 AM.",
        cooldown_remaining_s=1800))
    r = api.post(url(api, "/drops"), json={"medication_id": api.ids["med_ids"][0]})
    assert r.status_code == 200 and r.json()["status"] == "DENIED" and r.json()["reason"] == "COOLDOWN"
    assert r.json()["cooldown_remaining_s"] == 1800


def test_drop_body_validation(api):
    for body in ({}, {"slot": 0, "medication_id": 1}, {"slot": "0"}, {"slot": True}, {"slot": -1},
                 {"slot": 0, "dose_event_id": 3}):
        r = api.post(url(api, "/drops"), json=body)
        assert r.status_code == 422 and isinstance(r.json()["detail"], str), body
    assert api.services.drops.requests == []


# --------------------------------------------------------------------------- caregiver edits


def test_container_update_and_refill(api):
    r = api.request("PUT", url(api, "/containers/0"), actor="doctor",
                    json={"medication_id": None, "capacity": 25, "low_stock_threshold": 5})
    assert r.status_code == 200, r.text
    assert set(r.json()) == CONTAINER_KEYS and r.json()["capacity"] == 25 and r.json()["medication_id"] is None
    [(name, call)] = api.services.compartments.calls
    assert name == "update" and call["patient_id"] == api.pid and call["by_user_id"] == api.uid("doctor")
    assert call["medication_id"] is None and call["pill_count"] is None
    r = api.post(url(api, "/containers/2/refill"), actor="family", json={"set": 25})
    assert r.status_code == 200 and r.json()["pill_count"] == 25
    r = api.post(url(api, "/containers/2/refill"), actor="family", json={"add": 10})
    assert r.status_code == 422 and "at most" in r.json()["detail"]   # ValidationError from the service


def test_container_body_validation(api):
    for path, body in (
        ("/containers/0", {}),
        ("/containers/0", {"pill_count": -1}),
        ("/containers/0", {"pill_count": None}),
        ("/containers/0", {"color": "red"}),
        ("/containers/0/refill", {}),
        ("/containers/0/refill", {"set": 1, "add": 1}),
        ("/containers/0/refill", {"add": 0}),
    ):
        method = "PUT" if path.endswith("/0") else "POST"
        r = api.request(method, url(api, path), actor="doctor", json=body)
        assert r.status_code == 422, (path, body, r.text)
    assert api.services.compartments.calls == []


def test_unknown_container_slot_is_404(api):
    assert api.request("PUT", url(api, "/containers/3"), actor="doctor", json={"pill_count": 1}).status_code == 404
    assert api.post(url(api, "/containers/7/refill"), actor="doctor", json={"add": 1}).status_code == 404


def test_medication_crud(api):
    created = api.post(url(api, "/medications"), actor="doctor",
                       json={"name": "Zinc (demo token)", "strength": "1 token", "warnings": ["Demo"],
                             "confirmed": True, "confirmed_by": "someone else"})
    assert created.status_code == 201, created.text
    name, call = api.services.catalog.calls[-1]
    assert name == "create" and call["patient_id"] == api.pid and call["confirmed"] is True
    assert call["confirmed_by"] == "Dr. Lee"            # the signed-in caregiver, not the body
    assert call["fields"] == {"name": "Zinc (demo token)", "strength": "1 token", "warnings": ["Demo"]}
    mid = created.json()["medication_id"]
    unconfirmed = api.post(url(api, "/medications"), actor="doctor", json={"name": "X"})
    assert unconfirmed.status_code == 422
    patched = api.request("PATCH", url(api, f"/medications/{mid}"), actor="family",
                          json={"strength": "2 tokens", "confirmed": True})
    assert patched.status_code == 200 and patched.json()["strength"] == "2 tokens"
    assert api.request("PATCH", url(api, f"/medications/{mid}"), actor="family",
                       json={"strength": "3"}).status_code == 422
    assert api.request("DELETE", url(api, f"/medications/{mid}"), actor="family").json() == {"ok": True}
    assert api.request("DELETE", url(api, "/medications/77777"), actor="family").status_code == 404


def test_schedule_crud_triggers_scheduler(api):
    loop = api.services.scheduler_loop
    before = loop.triggers
    med = api.ids["med_ids"][1]
    r = api.post(url(api, "/schedules"), actor="doctor",
                 json={"medication_id": med, "time_of_day": "21:30", "frequency": "WEEKLY", "days_of_week": ["MON"]})
    assert r.status_code == 201, r.text
    name, call = api.services.scheduler.calls[-1]
    assert call == {"medication_id": med, "time_of_day": "21:30", "frequency": "WEEKLY", "days_of_week": ["MON"],
                    "created_by_user_id": api.uid("doctor"), "patient_id": api.pid}
    sid = r.json()["schedule_id"]
    assert api.request("PATCH", url(api, f"/schedules/{sid}"), actor="doctor",
                       json={"active": False}).json()["active"] is False
    assert api.request("PATCH", url(api, f"/schedules/{sid}"), actor="doctor", json={}).status_code == 422
    assert api.request("DELETE", url(api, f"/schedules/{sid}"), actor="doctor").json() == {"ok": True}
    assert loop.triggers == before + 3
    bad = api.post(url(api, "/schedules"), actor="doctor", json={"medication_id": med, "time_of_day": "25:00"})
    assert bad.status_code == 422 and "HH:MM" in bad.json()["detail"]


def test_settings_patch_validates_and_triggers(api):
    loop = api.services.scheduler_loop
    before = loop.triggers
    r = api.request("PATCH", url(api, "/settings"), actor="doctor",
                    json={"manual_cooldown_minutes": 0, "auto_drop_enabled": False})
    assert r.status_code == 200 and r.json()["manual_cooldown_minutes"] == 0 and r.json()["auto_drop_enabled"] is False
    assert loop.triggers == before + 1
    call = [c for c in api.services.drops.calls if c[0] == "update_settings"][-1][1]
    assert call["by_user_id"] == api.uid("doctor")
    for body in ({"manual_cooldown_minutes": 1441}, {"manual_cooldown_minutes": -1}, {"auto_drop_enabled": "no"},
                 {}, {"manual_cooldown_minutes": 5.5}):
        assert api.request("PATCH", url(api, "/settings"), actor="doctor", json=body).status_code == 422, body
    assert loop.triggers == before + 1


def test_resolve_and_skip(api):
    r = api.post(url(api, f"/drops/{api.ids['drop_id']}/resolve"), actor="doctor", json={"dropped": True, "note": "saw it"})
    assert r.status_code == 200 and r.json()["status"] == "DROPPED" and r.json()["needs_review"] is False
    again = api.post(url(api, f"/drops/{api.ids['drop_id']}/resolve"), actor="doctor", json={"dropped": True})
    assert again.status_code == 409 and again.json()["detail"] == "This drop does not need a review."
    assert api.post(url(api, f"/drops/{api.ids['drop_id']}/resolve"), actor="doctor", json={}).status_code == 422
    assert api.post(url(api, "/drops/5555/resolve"), actor="doctor", json={"dropped": False}).status_code == 404
    skipped = api.post(url(api, f"/doses/{api.ids['event_id']}/skip"), actor="family")
    assert skipped.status_code == 200 and skipped.json()["status"] == "CANCELLED"
    assert api.post(url(api, "/doses/5555/skip"), actor="family", json={}).status_code == 404


def test_reports_list_and_generate(api):
    assert api.get(url(api, "/reports")).json() == []
    r = api.post(url(api, "/reports"), actor="doctor", json={"days": 14})
    assert r.status_code == 201 and r.json()["days"] == 14 and r.json()["created_by_user_id"] == api.uid("doctor")
    assert [m["report_id"] for m in api.get(url(api, "/reports")).json()] == [r.json()["report_id"]]
    for days in (0, 91, "7"):
        assert api.post(url(api, "/reports"), json={"days": days}).status_code == 422


# --------------------------------------------------------------------------- error mapping


def test_domain_errors_are_mapped(api, monkeypatch):
    for exc, status in ((ValidationError("bad input"), 422), (NotFoundError("no such thing"), 404),
                        (ConflictError("state changed"), 409)):
        def boom(*a: Any, _exc: Exception = exc, **k: Any) -> Any:
            raise _exc

        monkeypatch.setattr(api.services.drops, "get_settings", boom)
        r = api.get(url(api, "/settings"))
        assert r.status_code == status and r.json() == {"detail": str(exc)}


def test_auth_errors_are_mapped(api, monkeypatch):
    errors = pytest.importorskip("tactidose.auth.errors")

    def denied(*a: Any, **k: Any) -> Any:
        raise errors.PermissionDenied("nope")

    monkeypatch.setattr(api.services.drops, "get_settings", denied)
    assert api.get(url(api, "/settings")).status_code == 403

    def unauth(*a: Any, **k: Any) -> Any:
        raise errors.AuthError("sign in again")

    monkeypatch.setattr(api.services.drops, "get_settings", unauth)
    r = api.get(url(api, "/settings"))
    assert r.status_code == 401 and r.json() == {"detail": "sign in again"}
    if hasattr(errors, "TooManyAttempts"):
        def locked(*a: Any, **k: Any) -> Any:
            raise errors.TooManyAttempts("wait", retry_after_s=30)

        monkeypatch.setattr(api.services.drops, "get_settings", locked)
        r = api.get(url(api, "/settings"))
        assert r.status_code == 429 and r.headers["retry-after"] == "30"


def test_unexpected_errors_are_json_500_and_db_errors_503(api, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def bug(*a: Any, **k: Any) -> Any:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(api.services.drops, "patient_status", bug)
    r = api.get(url(api, "/status"))
    assert r.status_code == 500 and "secret" not in r.text and r.json()["detail"]

    def db_down(*a: Any, **k: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(api.services.drops, "patient_status", db_down)
    r = api.get(url(api, "/status"))
    assert r.status_code == 503 and "database" in r.json()["detail"].lower()


def test_validation_errors_are_readable(api):
    r = api.request("PATCH", url(api, "/settings"), actor="doctor", json={"manual_cooldown_minutes": "abc"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "manual_cooldown_minutes" in detail
    assert api.get("/api/patients/abc/status").status_code in (401, 422)
