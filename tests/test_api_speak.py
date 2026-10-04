"""POST /api/agent/speak: the page's own announcements ("pill dropped") in the server voice
(ElevenLabs -> cache -> offline OS voice) instead of the browser's built-in one."""

from __future__ import annotations

from typing import Any

import pytest

from tactidose.app import build_services, create_app
from tactidose.config import Settings
from tests.test_api_support import TestClient


@pytest.fixture
def app(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", hardware_mode="none", voice_enabled=False,
                        tts_provider="none", agent_provider="rules", demo_mode=True, scheduler_tick_s=600)
    services = build_services(settings)
    with TestClient(create_app(services=services)) as client:
        def login(email: str) -> dict[str, str]:
            r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
            return {"Authorization": f"Bearer {r.json()['token']}"}
        yield client, services, login


def test_speak_returns_the_rendered_audio(app, monkeypatch):
    client, services, login = app
    alex = login("alex@demo.tactidose")
    said: list[Any] = []
    monkeypatch.setattr(services.agent, "speak", lambda pid, text: said.append((pid, text)) or "a1b2c3")
    r = client.post("/api/agent/speak", headers=alex, json={"text": "  Vitamin C dropped   from container 1. "})
    assert r.status_code == 200 and r.json() == {"audio_url": "/api/agent/audio/a1b2c3.wav"}
    assert said[0][1] == "Vitamin C dropped from container 1."


def test_speak_without_a_server_voice_lets_the_browser_speak(app):
    client, _services, login = app
    r = client.post("/api/agent/speak", headers=login("alex@demo.tactidose"), json={"text": "Pill dropped."})
    assert r.status_code == 200 and r.json() == {"audio_url": None}      # tts_provider none


def test_speak_is_for_the_patient_and_bounded(app):
    client, _services, login = app
    assert client.post("/api/agent/speak", headers=login("sam@demo.tactidose"), json={"text": "hi"}).status_code == 403
    alex = login("alex@demo.tactidose")
    assert client.post("/api/agent/speak", headers=alex, json={"text": ""}).status_code == 422
    assert client.post("/api/agent/speak", headers=alex, json={"text": "x" * 601}).status_code == 422
