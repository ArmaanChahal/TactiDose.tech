"""``tactidose/app.py``: service wiring, lifespan order, guarded startup, the scheduler loop,
pages, static files and ``/api/health``."""

from __future__ import annotations

import mimetypes
import sys
import threading
import time
import types
from typing import Any

import pytest

import tactidose.app as app_module
from tactidose.api.common import call_supported, supported_kwargs
from tactidose.app import SchedulerLoop, build_services, create_app
from tests import test_api_support as support
from tests.fakes import FakeDropHardware, wait_until
from tests.test_api_support import TestClient

# pytest fixtures shared by the API tests
api = support.api
api_settings = support.api_settings
make_api = support.make_api


# --------------------------------------------------------------------------- helpers


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.lock = threading.Lock()

    def add(self, name: str) -> None:
        with self.lock:
            self.events.append(name)


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> None:
    monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs))


class _Ticker:
    """Scheduler + DropService stand-ins for the loop."""

    def __init__(self, rec: _Recorder | None = None, *, fail_tick: bool = False) -> None:
        self.rec = rec or _Recorder()
        self.fail_tick = fail_tick
        self.ticks = 0
        self.runs = 0

    def tick(self) -> int:
        self.ticks += 1
        self.rec.add("tick")
        if self.fail_tick:
            raise RuntimeError("db down")
        return 2

    def run_scheduled_drops(self) -> int:
        self.runs += 1
        self.rec.add("drops")
        return 1


# --------------------------------------------------------------------------- wiring


def test_call_supported_filters_keyword_arguments():
    class A:
        def __init__(self, db: Any, settings: Any, *, bus: Any = None) -> None:
            self.args = (db, settings, bus)

    class B:
        def __init__(self, db: Any, **kw: Any) -> None:
            self.kw = kw

    a = call_supported(A, 1, 2, bus=3, auth=4, notifications=5)
    assert a.args == (1, 2, 3)
    assert call_supported(B, 1, auth=4).kw == {"auth": 4}
    assert supported_kwargs(lambda x, *, y=1: None, {"y": 2, "z": 3}) == {"y": 2}


def test_build_services_keeps_injected_fakes_and_builds_wave1_services(api_settings, db_v2, clock, bus):
    drops, notes, auth = object(), object(), object()
    s = build_services(api_settings, clock=clock, bus=bus, db=db_v2, hardware=FakeDropHardware(),
                       drops=drops, notifications=notes, auth=auth, agent=object(), reports=object())
    assert s.drops is drops and s.notifications is notes and s.auth is auth
    assert type(s.compartments).__name__ == "CompartmentService"
    assert type(s.catalog).__name__ == "MedicationCatalog"
    assert type(s.scheduler).__name__ == "Scheduler"
    assert type(s.onboarding).__name__ == "OnboardingService"
    assert s.extractor is not None            # label_extractor="fake" in the test settings
    assert s.analytics_sync is None           # Snowflake not configured
    assert isinstance(s.scheduler_loop, SchedulerLoop) and s.scheduler_loop.interval_s == 600.0
    assert s.owns_db is False and s.sim is None


def test_optional_services_failing_to_build_are_recorded(api_settings, db_v2, clock, bus, monkeypatch):
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("agent exploded")

    _fake_module(monkeypatch, "tactidose.agent.service", AgentService=broken)
    _fake_module(monkeypatch, "tactidose.reports.service", ReportService=broken)
    s = build_services(api_settings, clock=clock, bus=bus, db=db_v2, hardware=FakeDropHardware(),
                       drops=object(), notifications=object(), auth=object())
    assert s.agent is None and s.reports is None
    assert set(s.startup_errors) == {"agent", "reports"}


# --------------------------------------------------------------------------- lifespan


def test_lifespan_order_and_shutdown(api_settings, make_api, monkeypatch):
    rec = _Recorder()
    settings = api_settings.model_copy(update={"seed_demo_accounts": True, "voice_enabled": True})

    def seed_demo(db: Any, settings: Any, clock: Any, *, auth: Any = None) -> dict[str, Any]:
        rec.add("seed")
        return {"ok": True}

    class VoiceLoop:
        def __init__(self, *, settings: Any, agent: Any, hardware: Any, patient_id: int | None = None) -> None:
            self.patient_id = patient_id
            rec.add("voice:init")

        def start(self) -> bool:
            rec.add("voice:start")
            return True

        def close(self) -> None:
            rec.add("voice:close")

    class Sync:
        configured = True

        def start(self) -> None:
            rec.add("sync:start")

        def close(self) -> None:
            rec.add("sync:close")

        def status(self) -> dict[str, Any]:
            return {"configured": True}

    _fake_module(monkeypatch, "tactidose.db.seed", seed_demo=seed_demo)
    _fake_module(monkeypatch, "tactidose.agent.voice_loop", VoiceLoop=VoiceLoop)

    class Hardware(FakeDropHardware):
        def start(self) -> None:
            rec.add("hardware:start")
            super().start()

        def close(self) -> None:
            rec.add("hardware:close")
            super().close()

    h = make_api(settings, hardware=Hardware(), analytics_sync=Sync())
    s = h.services
    original_create_all = s.db.create_all
    monkeypatch.setattr(s.db, "create_all", lambda: (rec.add("create_all"), original_create_all())[1])
    original_recover = s.drops.recover_on_startup
    monkeypatch.setattr(s.drops, "recover_on_startup", lambda: (rec.add("recover"), original_recover())[1])
    app = create_app(services=s)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert wait_until(lambda: "voice:start" in rec.events, timeout=10)
        assert wait_until(lambda: s.drops.run_calls >= 1, timeout=10)
        assert s.scheduler_loop.running
        startup = [e for e in rec.events if e not in ("tick", "drops")]
        assert startup[:4] == ["create_all", "seed", "recover", "hardware:start"]
        assert set(startup[4:]) == {"voice:init", "voice:start", "sync:start"}
        assert s.startup_log[:4] == ["create_all", "seed_demo", "recover_on_startup", "scheduler_loop"]
        assert s.voice_loop.patient_id == h.pid
        assert s.startup_errors == {}
    assert s.stopping.is_set() and not s.scheduler_loop.running
    tail = [e for e in rec.events if e.endswith(":close")]
    assert tail == ["voice:close", "sync:close", "hardware:close"]


def test_failing_steps_never_prevent_startup(api_settings, make_api, monkeypatch):
    settings = api_settings.model_copy(update={"seed_demo_accounts": True, "voice_enabled": True})

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("nope")

    _fake_module(monkeypatch, "tactidose.db.seed", seed_demo=boom)
    _fake_module(monkeypatch, "tactidose.agent.voice_loop", VoiceLoop=boom)

    class BadHardware(FakeDropHardware):
        def start(self) -> None:
            raise RuntimeError("serial port busy")

    h = make_api(settings, hardware=BadHardware())
    monkeypatch.setattr(h.services.drops, "recover_on_startup", boom)
    with TestClient(create_app(services=h.services)) as client:
        assert wait_until(lambda: "voice" in h.services.startup_errors, timeout=10)
        health = client.get("/api/health").json()
        assert health["ok"] is True
        assert set(health["degraded"]) >= {"seed_demo", "recover_on_startup", "hardware", "voice"}
        assert client.get(f"/api/patients/{h.pid}/status", headers=h.h("patient")).status_code == 200


# --------------------------------------------------------------------------- scheduler loop


def test_loop_runs_tick_then_drops_and_survives_errors():
    rec = _Recorder()
    t = _Ticker(rec, fail_tick=True)
    loop = SchedulerLoop(t, t, interval_s=600)
    out = loop.run_once()
    assert rec.events == ["tick", "drops"] and out["drops"] == 1 and out["errors"] == ["tick: RuntimeError"]
    assert loop.cycles == 1 and loop.last_error == "tick: RuntimeError"
    ok = _Ticker()
    assert SchedulerLoop(ok, ok, interval_s=600).tick_now() == 2 and ok.runs == 0


def test_loop_trigger_wakes_it_immediately():
    t = _Ticker()
    loop = SchedulerLoop(t, t, interval_s=600)
    assert loop.start() is True and loop.start() is False
    try:
        assert wait_until(lambda: t.runs == 1, timeout=10)
        loop.trigger()
        assert wait_until(lambda: t.runs == 2, timeout=10)
        assert loop.status()["running"] is True and loop.status()["cycles"] >= 2
    finally:
        loop.close()
    assert not loop.running


def test_loop_waits_for_the_hardware_handshake_first():
    t = _Ticker()
    hw = FakeDropHardware(connected=False)
    loop = SchedulerLoop(t, t, interval_s=600, hardware=hw, hardware_grace_s=10)
    loop.start()
    try:
        time.sleep(0.3)
        assert t.runs == 0
        hw.set_state(connected=True)
        assert wait_until(lambda: t.runs == 1, timeout=10)
    finally:
        loop.close()


def test_loop_exclusive_blocks_cycles():
    t = _Ticker()
    loop = SchedulerLoop(t, t, interval_s=600)
    done = threading.Event()
    with loop.exclusive():
        worker = threading.Thread(target=lambda: (loop.run_once(), done.set()))
        worker.start()
        assert not done.wait(0.2)
    assert done.wait(5)
    worker.join(5)


# --------------------------------------------------------------------------- pages & static


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    d = tmp_path / "static"
    for name in ("login.html", "patient.html", "care.html", "kiosk.html", "demo.html"):
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(f"<!doctype html><title>{name}</title>", encoding="utf-8")
    (d / "js").mkdir()
    (d / "js" / "app.js").write_text("export const x = 1;", encoding="utf-8")
    (d / "css").mkdir()
    (d / "css" / "a.css").write_text("body{}", encoding="utf-8")
    (d / "img").mkdir()
    (d / "img" / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC_DIR", d)
    return d


def test_root_redirects_by_session(api):
    c = api.client
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/", headers=api.h("patient"), follow_redirects=False).headers["location"] == "/patient"
    assert c.get("/", headers=api.h("doctor"), follow_redirects=False).headers["location"] == "/care"


def test_pages_are_served_with_role_redirects(api, pages_dir):
    c = api.client
    r = c.get("/login")
    assert r.status_code == 200 and "login.html" in r.text and r.headers["content-type"].startswith("text/html")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-cache"
    anon = c.get("/patient", follow_redirects=False)
    assert anon.status_code == 303 and anon.headers["location"] == "/login?next=/patient"
    assert c.get("/patient", headers=api.h("patient")).text.endswith("<title>patient.html</title>")
    assert c.get("/patient", headers=api.h("family"), follow_redirects=False).headers["location"] == "/care"
    assert c.get("/care", headers=api.h("patient"), follow_redirects=False).headers["location"] == "/patient"
    assert "care.html" in c.get("/care", headers=api.h("doctor")).text
    assert "kiosk.html" in c.get("/kiosk", headers=api.h("patient")).text
    assert "demo.html" in c.get("/demo").text
    assert c.get("/favicon.ico").headers["content-type"].startswith("image/svg+xml")


def test_missing_page_is_a_clean_404(api, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path / "nothing-here")
    r = api.client.get("/login")
    assert r.status_code == 404 and r.json()["detail"].startswith("Page not available")
    assert api.client.get("/favicon.ico").status_code == 204


def test_static_mount_uses_module_mimetypes(api_settings, make_api, pages_dir):
    h = make_api(api_settings)
    app = create_app(services=h.services)
    assert mimetypes.guess_type("x.js")[0] == "text/javascript"
    assert mimetypes.guess_type("x.css")[0] == "text/css"
    assert mimetypes.guess_type("x.svg")[0] == "image/svg+xml"
    c = TestClient(app)
    assert c.get("/static/js/app.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/static/css/a.css").headers["content-type"].startswith("text/css")
    assert c.get("/static/img/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    assert c.get("/static/../app.py").status_code == 404


# --------------------------------------------------------------------------- health


def test_health_needs_no_auth_and_leaks_no_secrets(api_settings, make_api):
    settings = api_settings.model_copy(update={
        "gemini_api_key": "sk-gemini-SECRET", "smtp_password": "smtp-SECRET", "smtp_host": "smtp.example.com",
        "smtp_from": "td@example.com"})
    h = make_api(settings)
    r = h.client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "version", "db", "hardware", "agent", "tts", "smtp", "time"}
    assert body["ok"] is True and body["db"] == {"ok": True, "backend": "sqlite", "error": None}
    assert body["hardware"]["connected"] is True and body["smtp"]["configured"] is True
    assert body["time"]["tz"] == "America/Vancouver"
    assert "SECRET" not in r.text


def test_uvicorn_shutdown_ends_event_streams_first(api):
    import asyncio

    import uvicorn

    assert app_module.install_server_shutdown_hook() is True and app_module.install_server_shutdown_hook() is True
    stopping = threading.Event()
    app_module._LIVE_STOP_EVENTS.add(stopping)
    try:
        server = uvicorn.Server(uvicorn.Config(api.app, lifespan="off"))
        server.servers = []                           # nothing was started: no sockets,
        server.force_exit = True                      # and no lifespan step
        asyncio.run(server.shutdown())
        assert stopping.is_set()
    finally:
        app_module._LIVE_STOP_EVENTS.discard(stopping)


def test_unknown_routes_are_json_404(api):
    r = api.client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_openapi_schema_lists_the_v2_routes(api):
    paths = api.client.get("/openapi.json").json()["paths"]
    assert {"/api/patients/{pid}/drops", "/api/events", "/api/agent/audio/{audio_id}.wav",
            "/api/reports/{rid}/pdf", "/api/demo/jump-to-next-dose"} <= set(paths)
    assert "/patient" not in paths                     # pages stay out of the API docs
