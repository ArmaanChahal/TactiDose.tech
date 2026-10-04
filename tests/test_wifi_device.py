"""The Wi-Fi ESP32 (hardware_mode "wifi", tactidose/hardware/wifi_device.py) against a mock of its
three endpoints (GET /lid?state=open|close, GET /dispense?pill=N, plus GET / for reachability):
the driver's outcomes (dropped / failed / never sent / uncertain), the lid, and the whole website
path - patient Drop pill -> DropService rules -> /dispense - and the lid buttons' API.
"""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tactidose.config import Settings
from tactidose.hardware import protocol as p
from tactidose.hardware import wifi_config
from tactidose.hardware.serial_client import create_hardware
from tactidose.hardware.wifi_device import HTTP_ERROR, LID_ERROR, WifiDispenser

TZ = "America/Vancouver"


class FakeEsp32:
    """Mock ESP32 web server: records every request; ``mode`` changes how /dispense behaves."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.mode = "ok"            # ok | error | refuse | hang
        self.lid = "closed"
        self.lid_status = 200       # HTTP status of /lid

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        self.requests.append(path)
        if self.mode == "refuse":
            raise httpx.ConnectError("connection refused", request=request)
        if path.startswith("/dispense"):
            if self.mode == "hang":
                raise httpx.ReadTimeout("no answer", request=request)
            if self.mode == "error":
                return httpx.Response(500, text="motor error")
            return httpx.Response(200, text="dispensed")
        if path.startswith("/lid"):
            if self.lid_status != 200:
                return httpx.Response(self.lid_status, text="lid jammed")
            self.lid = "open" if path.endswith("open") else "closed"
            return httpx.Response(200, text=f"lid {self.lid}")
        return httpx.Response(200, text="TactiDose ESP32")


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = dict(_env_file=None, hardware_mode="wifi", num_slots=3)
    base.update(over)
    return Settings(**base)


def _config(**over: Any) -> SimpleNamespace:
    """wifi_config with some values changed (e.g. a short lid delay for the tests)."""
    values = {k: getattr(wifi_config, k) for k in dir(wifi_config) if k.isupper()}
    values.update(over)
    return SimpleNamespace(**values)


def _device(fake: FakeEsp32, settings: Settings | None = None, *, bus: Any = None,
            config: Any = wifi_config) -> WifiDispenser:
    client = httpx.Client(transport=httpx.MockTransport(fake), follow_redirects=False)
    return WifiDispenser(settings or _settings(), bus=bus, client=client, config=config)


def _wait_for(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- the driver


def test_static_ip_default_and_override():
    hw, sim = create_hardware(_settings())
    assert isinstance(hw, WifiDispenser) and sim is None
    assert hw.base_url == wifi_config.ESP32_BASE_URL == "http://172.20.10.9"
    hw.close()
    other, _ = create_hardware(_settings(esp32_url="http://10.0.0.7/"))
    assert other.base_url == "http://10.0.0.7"
    other.close()


def test_dispense_maps_container_to_pill_number_and_reports_dropped():
    fake = FakeEsp32()
    dev = _device(fake)
    assert dev.reconnect() and dev.snapshot().ready_for_motion
    result = dev.drop_slot(1)                                  # container 2
    assert fake.requests[-1] == "/dispense?pill=2"
    assert result.ok and result.code == "DROPPED"
    assert p.drop_certainty(result) is p.DropCertainty.DROPPED


@pytest.mark.parametrize(("mode", "code", "certainty"), [
    ("error", HTTP_ERROR, p.DropCertainty.NOT_DROPPED),            # the ESP32 said no
    ("refuse", "NOT_CONNECTED", p.DropCertainty.NOT_DROPPED),      # never reached it: nothing sent
    ("hang", "TIMEOUT", p.DropCertainty.UNCERTAIN),                # sent, no answer: maybe dropped
])
def test_dispense_failures_are_classified_fail_closed(mode, code, certainty):
    fake = FakeEsp32()
    dev = _device(fake)
    dev.reconnect()
    fake.mode = mode
    result = dev.drop_slot(0)
    assert not result.ok and result.code == code
    assert p.drop_certainty(result) is certainty
    if mode == "refuse":
        assert not dev.snapshot().connected                    # offline: further drops refused


def test_lid_open_close_and_unsupported_commands():
    fake = FakeEsp32()
    dev = _device(fake)
    assert dev.set_lid(True) == {"ok": True, "lid": "open", "detail": "GET /lid?state=open -> HTTP 200"}
    assert dev.set_lid(False)["lid"] == "closed" and fake.requests[-1] == "/lid?state=close"
    fake.mode = "refuse"
    out = dev.set_lid(True)
    assert out["ok"] is False and out["lid"] == "closed"         # unchanged when it failed
    before = len(fake.requests)
    for call in (dev.stop, dev.open_gate, dev.close_gate, lambda: dev.send_raw("DROP_SLOT 1")):
        assert call().ok is False
    assert len(fake.requests) == before                         # nothing sent for unsupported calls


def test_health_check_tracks_online_offline():
    fake = FakeEsp32()
    dev = _device(fake)
    assert dev.reconnect() is True and dev.snapshot().connected
    fake.mode = "refuse"
    assert dev.reconnect() is False and not dev.snapshot().connected
    assert dev.snapshot().ready_for_motion is False


def test_dispense_opens_the_lid_first_and_closes_it_after_the_delay():
    fake = FakeEsp32()
    dev = _device(fake, config=_config(LID_CLOSE_AFTER_S=0.2))
    result = dev.drop_slot(0)
    assert result.code == "DROPPED"
    assert fake.requests == ["/lid?state=open", "/dispense?pill=1"]       # result returns before the close
    assert dev.lid_state == "open"
    assert _wait_for(lambda: fake.requests[-1] == "/lid?state=close")
    assert dev.lid_state == "closed" and fake.requests.count("/lid?state=close") == 1


def test_default_lid_delay_is_five_seconds():
    assert wifi_config.OPEN_LID_FOR_DISPENSE is True and wifi_config.LID_CLOSE_AFTER_S == 5.0


def test_lid_closes_even_when_the_dispense_fails():
    fake = FakeEsp32()
    dev = _device(fake, config=_config(LID_CLOSE_AFTER_S=0.1))
    fake.mode = "error"
    assert dev.drop_slot(2).code == HTTP_ERROR
    assert _wait_for(lambda: fake.requests[-1] == "/lid?state=close")


def test_lid_that_does_not_open_means_nothing_is_dispensed():
    fake = FakeEsp32()
    dev = _device(fake, config=_config(LID_CLOSE_AFTER_S=0.1))
    fake.lid_status = 500
    result = dev.drop_slot(1)
    assert not result.ok and result.code == LID_ERROR and result.definitive
    assert p.drop_certainty(result) is p.DropCertainty.NOT_DROPPED
    time.sleep(0.2)
    assert not any(r.startswith("/dispense") for r in fake.requests)


def test_lid_sequence_can_be_switched_off():
    fake = FakeEsp32()
    dev = _device(fake, config=_config(OPEN_LID_FOR_DISPENSE=False))
    assert dev.drop_slot(0).code == "DROPPED"
    time.sleep(0.1)
    assert fake.requests == ["/dispense?pill=1"]


def test_new_dispense_cancels_the_pending_close():
    fake = FakeEsp32()
    dev = _device(fake, config=_config(LID_CLOSE_AFTER_S=0.3))
    dev.drop_slot(0)
    dev.drop_slot(1)                                     # within the delay: one close, at the end
    assert _wait_for(lambda: fake.requests[-1] == "/lid?state=close")
    time.sleep(0.4)
    assert fake.requests.count("/lid?state=close") == 1


# --------------------------------------------------------------------------- the website


@pytest.fixture
def site(tmp_path):
    from tactidose.app import build_services, create_app
    from tactidose.core.clock import Clock
    from tests.test_api_support import TestClient

    settings = _settings(data_dir=tmp_path / "data", voice_enabled=False, tts_provider="none",
                         agent_provider="rules", demo_mode=True, scheduler_tick_s=600, timezone=TZ)
    fake = FakeEsp32()
    dev = _device(fake, settings)
    services = build_services(settings, hardware=dev, clock=Clock(TZ, frozen_at=datetime(2026, 10, 5, 10, 30)))
    with TestClient(create_app(services=services)) as client:
        deadline = time.monotonic() + 10
        while not dev.snapshot().connected and time.monotonic() < deadline:
            time.sleep(0.05)

        def login(email: str) -> dict[str, str]:
            r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
            return {"Authorization": f"Bearer {r.json()['token']}"}

        yield client, services, fake, login


def test_drop_pill_button_goes_through_the_rules_to_the_esp32(site):
    client, services, fake, login = site
    alex = login("alex@demo.tactidose")
    pid = services.device_patient_id()
    first = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 0}).json()
    assert first["status"] == "DROPPED", first
    assert fake.requests[-2:] == ["/lid?state=open", "/dispense?pill=1"]       # lid first, then the pill
    sent = len([r for r in fake.requests if r.startswith("/dispense")])
    second = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 2}).json()
    assert second["status"] == "DENIED" and second["reason"] == "COOLDOWN"
    assert len([r for r in fake.requests if r.startswith("/dispense")]) == sent      # rules stopped it


def test_no_answer_marks_the_drop_uncertain_for_review(site):
    client, services, fake, login = site
    alex = login("alex@demo.tactidose")
    pid = services.device_patient_id()
    fake.mode = "hang"
    out = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 1}).json()
    assert out["status"] == "UNCERTAIN", out
    drops = client.get(f"/api/patients/{pid}/drops?days=1", headers=alex).json()
    assert drops[0]["needs_review"] is True                    # a caregiver must check it first
    fake.mode = "ok"
    again = client.post(f"/api/patients/{pid}/drops", headers=alex, json={"slot": 2}).json()
    assert again["status"] == "DENIED"                         # no further drop until reviewed


def test_lid_buttons_api(site):
    client, services, fake, login = site
    alex, sam = login("alex@demo.tactidose"), login("sam@demo.tactidose")
    device = client.get("/api/device", headers=alex).json()
    assert device["lid_supported"] is True and device["mode"] == "wifi"
    r = client.post("/api/device/lid", headers=alex, json={"state": "open"}).json()
    assert r["ok"] and r["lid"] == "open" and fake.requests[-1] == "/lid?state=open"
    r = client.post("/api/device/lid", headers=sam, json={"state": "close"}).json()   # caregiver too
    assert r["lid"] == "closed" and r["device"]["lid"] == "closed"
    assert client.post("/api/device/lid", headers=alex, json={"state": "half"}).status_code == 422
    assert not any(q.startswith("/dispense") for q in fake.requests)                  # the lid never dispenses


def test_lid_api_refuses_on_other_devices(tmp_path):
    from tactidose.app import build_services, create_app
    from tests.test_api_support import TestClient

    settings = _settings(data_dir=tmp_path / "data", hardware_mode="none", voice_enabled=False,
                         tts_provider="none", demo_mode=True, scheduler_tick_s=600)
    with TestClient(create_app(services=build_services(settings))) as client:
        r = client.post("/api/auth/login", json={"email": "alex@demo.tactidose", "password": "demo1234"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        assert client.get("/api/device", headers=h).json()["lid_supported"] is False
        assert client.post("/api/device/lid", headers=h, json={"state": "open"}).status_code == 409


def test_cli_run_wifi_flag():
    from tactidose.__main__ import build_parser

    assert build_parser().parse_args(["run", "--wifi"]).wifi == ""
    assert build_parser().parse_args(["run", "--wifi", "http://172.20.10.9"]).wifi == "http://172.20.10.9"
