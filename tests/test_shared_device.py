"""Shared dispenser (``settings.effective_shared_device``, automatic in Wi-Fi mode): every patient's
device record is served by the one ESP32 (172.20.10.9). The extra demo patients are seeded, each with
their own containers / cooldown / schedules, and all of them dispense through the same mock ESP32.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select

from tactidose.config import Settings
from tactidose.core.clock import Clock
from tactidose.db.models import Compartment, Device, DoseEvent
from tactidose.db.seed import DEMO_EXTRA_PATIENTS
from tests.test_wifi_device import FakeEsp32, _device

TZ = "America/Vancouver"
EXTRA_EMAILS = [a.email for a in DEMO_EXTRA_PATIENTS]


def _settings(tmp_path: Any, **over: Any) -> Settings:
    base: dict[str, Any] = dict(_env_file=None, data_dir=tmp_path / "data", hardware_mode="wifi", num_slots=3,
                                voice_enabled=False, tts_provider="none", agent_provider="rules", demo_mode=True,
                                scheduler_tick_s=600, timezone=TZ, session_ttl_hours=96)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def shared(tmp_path):
    from tactidose.app import build_services, create_app
    from tests.test_api_support import TestClient

    settings = _settings(tmp_path)
    fake = FakeEsp32()
    dev = _device(fake, settings)
    clock = Clock(TZ, frozen_at=datetime(2026, 10, 5, 10, 30))
    services = build_services(settings, hardware=dev, clock=clock)
    with TestClient(create_app(services=services)) as client:
        deadline = time.monotonic() + 10
        while not dev.snapshot().connected and time.monotonic() < deadline:
            time.sleep(0.05)

        def login(email: str) -> tuple[dict[str, str], int]:
            r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['token']}"}
            return headers, client.get("/api/auth/me", headers=headers).json()["user"]["user_id"]

        yield client, services, fake, login, clock


def test_wifi_mode_shares_the_device_and_other_modes_do_not(tmp_path):
    assert _settings(tmp_path).effective_shared_device is True
    assert _settings(tmp_path, hardware_mode="sim").effective_shared_device is False
    assert _settings(tmp_path, hardware_mode="sim", shared_device=True).effective_shared_device is True
    assert _settings(tmp_path, shared_device=False).effective_shared_device is False


def test_extra_test_patients_each_have_their_own_device_record(shared):
    client, services, fake, login, _clock = shared
    with services.db.session() as s:
        devices = {d.device_id: d.user_id for d in s.scalars(select(Device))}
        assert len(devices) == 1 + len(DEMO_EXTRA_PATIENTS)
        for device_id in devices:
            comps = s.scalars(select(Compartment).where(Compartment.device_id == device_id)).all()
            assert len(comps) == 3 and all(c.medication_id is not None for c in comps)
    doctor, _ = login("dr.lee@demo.tactidose")
    linked = {p["patient_id"] for p in client.get("/api/care/patients", headers=doctor).json()}
    assert len(linked) == 1 + len(DEMO_EXTRA_PATIENTS)            # Dr. Lee sees every test patient


def test_every_patient_dispenses_through_the_same_esp32_with_their_own_cooldown(shared):
    client, services, fake, login, _clock = shared
    alex, alex_id = login("alex@demo.tactidose")
    jordan, jordan_id = login(EXTRA_EMAILS[0])
    maria, maria_id = login(EXTRA_EMAILS[1])
    assert client.post(f"/api/patients/{alex_id}/drops", headers=alex, json={"slot": 0}).json()["status"] == "DROPPED"
    assert client.post(f"/api/patients/{jordan_id}/drops", headers=jordan, json={"slot": 1}).json()["status"] == "DROPPED"
    assert client.post(f"/api/patients/{maria_id}/drops", headers=maria, json={"slot": 2}).json()["status"] == "DROPPED"
    assert [r for r in fake.requests if r.startswith("/dispense")] == ["/dispense?pill=1", "/dispense?pill=2",
                                                                       "/dispense?pill=3"]
    again = client.post(f"/api/patients/{jordan_id}/drops", headers=jordan, json={"slot": 0}).json()
    assert (again["status"], again["reason"]) == ("DENIED", "COOLDOWN")      # Jordan's own cooldown
    # Pill counts are per patient: Jordan's container 2 went down, Alex's container 2 did not.
    jordan_c2 = client.get(f"/api/patients/{jordan_id}/containers", headers=jordan).json()[1]["pill_count"]
    alex_c2 = client.get(f"/api/patients/{alex_id}/containers", headers=alex).json()[1]["pill_count"]
    assert jordan_c2 == alex_c2 - 1


def test_test_patients_and_caregivers_reach_the_device(shared):
    client, services, fake, login, _clock = shared
    jordan, _ = login(EXTRA_EMAILS[0])
    device = client.get("/api/device", headers=jordan).json()
    assert device["mode"] == "wifi" and device["connected"] is True
    assert client.post("/api/device/lid", headers=jordan, json={"state": "open"}).status_code == 403   # restock = carers
    doctor, _ = login("dr.lee@demo.tactidose")
    assert client.post("/api/device/lid", headers=doctor, json={"state": "open"}).json()["ok"] is True


def test_scheduled_doses_drop_for_every_patient(shared):
    client, services, fake, login, clock = shared
    clock.freeze(datetime(2026, 10, 6, 8, 0))                 # tomorrow 08:00: everyone's morning dose
    services.scheduler_loop.run_once()
    with services.db.session() as s:
        morning = s.scalars(select(DoseEvent).where(DoseEvent.status == "DISPENSED")).all()
        assert len({ev.device_id for ev in morning}) == 1 + len(DEMO_EXTRA_PATIENTS)
    assert len([r for r in fake.requests if r == "/dispense?pill=1"]) == 1 + len(DEMO_EXTRA_PATIENTS)


def test_new_patients_get_their_own_device_record(shared):
    client, services, fake, login, _clock = shared
    r = client.post("/api/auth/register", json={"email": "new.patient@example.com", "password": "longpassword1",
                                                "display_name": "New Patient", "role": "patient"})
    assert r.status_code in (200, 201), r.text
    with services.db.session() as s:
        from tactidose.db.models import User

        user = s.scalars(select(User).where(User.email == "new.patient@example.com")).one()
        dev = s.scalars(select(Device).where(Device.user_id == user.user_id)).one()
        assert dev.device_id == f"{services.settings.device_id}-p{user.user_id}"
        assert len(s.scalars(select(Compartment).where(Compartment.device_id == dev.device_id)).all()) == 3
