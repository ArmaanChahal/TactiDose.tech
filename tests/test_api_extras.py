"""Optional extras: label-scan upload / review and the Snowflake status / sync endpoints."""

from __future__ import annotations

from typing import Any

from tests import test_api_support as support

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_scan_upload_is_caregiver_only_and_patient_scoped(api):
    url = f"/api/patients/{api.pid}/scans"
    files = {"image": ("label.png", PNG, "image/png")}
    assert api.post(url, actor=None, files=files).status_code == 401
    for actor in ("patient", "other", "stranger"):
        assert api.post(url, actor=actor, files=files).status_code == 403
    r = api.post(url, actor="doctor", files=files)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING_REVIEW"
    [(name, call)] = api.services.onboarding.calls
    assert name == "scan" and call == {"bytes": len(PNG), "mime": "image/png", "patient_id": api.pid}
    assert api.post(url, actor="doctor").status_code == 422          # no image part


def test_scan_upload_size_limit(api_settings, make_api):
    h = make_api(api_settings.model_copy(update={"max_label_image_bytes": 32}))
    r = h.post(f"/api/patients/{h.pid}/scans", actor="family", files={"image": ("big.png", PNG, "image/png")})
    assert r.status_code == 413 and h.services.onboarding.calls == []


def test_scan_confirm_needs_explicit_confirmation(api):
    url = f"/api/patients/{api.pid}/scans/{api.ids['scan_id']}/confirm"
    assert api.post(url, actor="doctor", json={"name": "Vitamin C"}).status_code == 422
    r = api.post(url, actor="doctor", json={"name": "Vitamin C", "strength": "1 piece", "confirmed": True})
    assert r.status_code == 201 and r.json()["confirmed_by"] == "Dr. Lee"
    name, call = api.services.onboarding.calls[-1]
    assert name == "confirm" and call["fields"] == {"name": "Vitamin C", "strength": "1 piece"}
    assert call["patient_id"] == api.pid
    assert api.post(f"/api/patients/{api.pid}/scans/9999/confirm", actor="doctor",
                    json={"name": "x", "confirmed": True}).status_code == 404


def test_scans_without_onboarding_are_503(api):
    api.services.onboarding = None
    r = api.post(f"/api/patients/{api.pid}/scans", actor="doctor", files={"image": ("l.png", PNG, "image/png")})
    assert r.status_code == 503


class _Sync:
    configured = True

    def __init__(self) -> None:
        self.synced = 0

    def status(self) -> dict[str, Any]:
        return {"configured": True, "last_sync": None, "pending": 4 - self.synced, "sent": self.synced,
                "last_error": None}

    def sync_once(self) -> dict[str, Any]:
        self.synced += 1
        return {"ok": True, "selected": 1, "sent": 1}


def test_snowflake_status_and_sync(api):
    assert api.get("/api/analytics/snowflake", actor=None).status_code == 401
    assert api.get("/api/analytics/snowflake").json()["configured"] is False
    assert api.post("/api/analytics/snowflake/sync", actor="doctor").status_code == 409
    api.services.analytics_sync = _Sync()
    assert api.get("/api/analytics/snowflake", actor="family").json()["pending"] == 4
    assert api.post("/api/analytics/snowflake/sync", actor="patient").status_code == 403
    r = api.post("/api/analytics/snowflake/sync", actor="doctor").json()
    assert r["sent"] == 1 and r["last_report"] == {"ok": True, "selected": 1, "sent": 1}
