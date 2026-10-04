"""End-to-end smoke test with the REAL v2 services: simulator hardware at high speed, the demo
seed, the rule-based agent and real PDF reports. Skipped while a parallel module is missing.

Alex logs in -> manual drop (DROPPED) -> second manual drop refused by the global cooldown ->
agent chat "can I have my vitamin c" (refused too: the agent can never bypass the cooldown) ->
report generation -> Dr. Lee logs in and sees (and downloads) the report.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest

for _module in ("tactidose.medication.drops", "tactidose.medication.notifications", "tactidose.auth.service",
                "tactidose.agent.service", "tactidose.reports.service", "tactidose.db.seed"):
    pytest.importorskip(_module)

from tactidose.app import build_services, create_app  # noqa: E402
from tactidose.config import Settings  # noqa: E402
from tactidose.core.bus import Topic  # noqa: E402
from tactidose.core.clock import Clock  # noqa: E402
from tests.test_api_support import TestClient  # noqa: E402

pytestmark = pytest.mark.timeout(120)

TZ = "America/Vancouver"
#: Monday 10:30 local: between the seeded 08:00 and 13:00 doses, so nothing is auto-dropped.
FROZEN_AT = datetime(2026, 10, 5, 10, 30)


def _wait_ready(client: Any, headers: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    device: dict[str, Any] = {}
    while time.monotonic() < deadline:
        device = client.get("/api/device", headers=headers).json()
        if device.get("ready_for_motion"):
            return device
        time.sleep(0.05)
    raise AssertionError(f"simulated device never became ready: {device}")


def _login(client: Any, email: str, password: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_patient_drop_cooldown_agent_report_doctor(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", hardware_mode="sim", sim_speed=50.0, num_slots=3,
        voice_enabled=False, tts_provider="none", label_extractor="disabled", agent_provider="rules",
        report_ai_summary=False, timezone=TZ, demo_mode=True, seed_demo_accounts=True, hw_boot_wait_s=0,
        scheduler_tick_s=600, manual_cooldown_minutes=60,
    )
    services = build_services(settings, clock=Clock(TZ, frozen_at=FROZEN_AT))
    password = settings.demo_password.get_secret_value()
    with TestClient(create_app(services=services)) as client:
        alex = _login(client, "alex@demo.tactidose", password)
        me = client.get("/api/auth/me", headers=alex).json()
        pid = me["user"]["user_id"]
        assert me["patient"]["patient_id"] == pid and me["patient"]["link_code"]
        _wait_ready(client, alex)

        first = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 0})
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "DROPPED", first.json()
        assert first.json()["pill_count_after"] == 19

        second = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 1}).json()
        assert second["status"] == "DENIED" and second["reason"] == "COOLDOWN", second
        assert second["cooldown_remaining_s"] > 0

        reply = client.post("/api/agent/chat", headers=alex, json={"text": "can I have my vitamin c"})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["text"] and body["model"].startswith("rules")
        assert all(a["status"] != "DROPPED" for a in body["actions"]), body["actions"]
        convs = client.get(f"/api/patients/{pid}/conversations", headers=alex).json()
        assert convs and convs[0]["conversation_id"] == body["conversation_id"]

        drops = client.get(f"/api/patients/{pid}/drops?days=1", headers=alex).json()
        assert [d["status"] for d in drops if d["source"] == "manual"] == ["DENIED", "DROPPED"]
        status = client.get(f"/api/patients/{pid}/status", headers=alex).json()
        assert status["containers"][0]["pill_count"] == 19 and status["cooldown_remaining_s"] > 0
        kinds = {n["kind"] for n in client.get("/api/notifications", headers=alex).json()}
        assert "PILL_DROPPED" in kinds

        created = client.post(f"/api/patients/{pid}/reports", headers=alex, json={"days": 7})
        assert created.status_code == 201, created.text
        report = created.json()
        assert report["status"] == "READY" and report["pdf_size"] > 0

        doctor = _login(client, "dr.lee@demo.tactidose", password)
        listed = client.get(f"/api/patients/{pid}/reports", headers=doctor).json()
        assert report["report_id"] in [r["report_id"] for r in listed]
        pdf = client.get(f"/api/reports/{report['report_id']}/pdf", headers=doctor)
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        sent = client.post(f"/api/reports/{report['report_id']}/send", headers=doctor, json={}).json()
        assert sent["deliveries"] and {d["status"] for d in sent["deliveries"]} <= {"SAVED", "SENT"}
        # the doctor may look but not drop
        assert client.post(f"/api/patients/{pid}/drops", headers=doctor, json={"slot": 0}).status_code == 403

        # exactly one pill left the device: the refused requests never reached the hardware
        sent = [e.data.get("line") for e in services.bus.recent(100_000, [Topic.DEVICE_LINE])
                if e.data.get("dir") == "tx"]
        assert [line for line in sent if line.startswith(("DROP_SLOT", "DISPENSE_SLOT"))] == ["DROP_SLOT 0"]
        # the seed pushed the container counts into the simulator: physical truth == database
        pills = services.sim.physical().get("pills")
        db_counts = [c["pill_count"] for c in client.get(f"/api/patients/{pid}/containers", headers=alex).json()]
        assert db_counts == [19, 12, 3]
        if isinstance(pills, (list, tuple)):
            assert list(pills) == db_counts
    assert services.stopping.is_set()


def test_scheduled_auto_drop_after_clock_jump_then_demo_reset(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", hardware_mode="sim", sim_speed=50.0, num_slots=3,
        voice_enabled=False, tts_provider="none", label_extractor="disabled", agent_provider="rules",
        report_ai_summary=False, timezone=TZ, demo_mode=True, seed_demo_accounts=True, hw_boot_wait_s=0,
        scheduler_tick_s=600, manual_cooldown_minutes=60,
    )
    clock = Clock(TZ, frozen_at=FROZEN_AT)
    # "reset" returns the demo clock to the test's frozen time instead of the wall clock
    monkeypatch.setattr(clock, "reset", lambda: clock.freeze(FROZEN_AT))
    services = build_services(settings, clock=clock)
    with TestClient(create_app(services=services)) as client:
        alex = _login(client, "alex@demo.tactidose", settings.demo_password.get_secret_value())
        pid = client.get("/api/auth/me", headers=alex).json()["user"]["user_id"]
        _wait_ready(client, alex)

        jump = client.post("/api/demo/jump-to-next-dose", headers=alex)
        assert jump.status_code == 200, jump.text
        nxt = jump.json()["next"]
        assert nxt is not None and nxt["scheduled_local"].startswith("2026-10-05T13:00")
        assert jump.json()["clock"]["now_local"].startswith("2026-10-05T13:00")

        deadline = time.monotonic() + 30
        auto: list[dict[str, Any]] = []
        while time.monotonic() < deadline and not auto:
            rows = client.get(f"/api/patients/{pid}/drops?days=1", headers=alex).json()
            auto = [d for d in rows if d["source"] == "schedule" and d["status"] == "DROPPED"]
            time.sleep(0.05)
        assert auto, "the scheduler loop did not drop the 13:00 dose"
        assert auto[0]["dose_event_id"] == nxt["event_id"] and auto[0]["slot"] == nxt["slot"]
        doses = client.get(f"/api/patients/{pid}/doses", headers=alex).json()
        assert {d["event_id"]: d["status"] for d in doses}[nxt["event_id"]] == "DISPENSED"

        # the automatic drop started the global cooldown for manual drops of any pill
        manual = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 0}).json()
        assert manual["status"] == "DENIED" and manual["reason"] == "COOLDOWN"

        sam = _login(client, "sam@demo.tactidose", settings.demo_password.get_secret_value())
        reset = client.post("/api/demo/reset", headers=sam, json={})   # reset: doctor/family only
        assert reset.status_code == 200, reset.text
        assert client.get("/api/auth/me", headers=alex).status_code == 200      # still signed in
        assert client.get(f"/api/patients/{pid}/drops?days=1", headers=alex).json() == []
        counts = [c["pill_count"] for c in client.get(f"/api/patients/{pid}/containers", headers=alex).json()]
        assert counts == [20, 12, 3]
        assert client.get("/api/demo/clock", headers=alex).json()["now_local"].startswith("2026-10-05T10:30")
