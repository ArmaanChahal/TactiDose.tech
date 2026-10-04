"""TactiDose v2 application: service wiring (ARCHITECTURE §11), lifespan, scheduler loop, pages.

``python -m tactidose run`` serves ``create_app`` through uvicorn (``factory=True``)::

    app = create_app()                       # Settings() from the environment / .env
    app = create_app(services=build_services(settings, drops=fake_drops, ...))   # tests

Startup (each step guarded — a failing step is logged, listed in ``/api/health`` as
``degraded`` and never prevents the others): ``create_all`` → demo seed (demo mode +
``seed_demo_accounts``) → ``drops.recover_on_startup()`` → the ``scheduler`` thread →
``hardware.start()`` (in sim mode it owns the simulator) → extras (device-side voice loop in a
background thread, Snowflake sync). Shutdown runs in reverse dependency order (see :func:`shutdown`).

Threads started here: ``scheduler`` (:class:`SchedulerLoop`) and, with ``voice_enabled``,
``voice-start`` (builds and starts the optional device-side voice loop) and ``stt-preload``
(loads the Vosk model behind ``/api/agent/transcribe``). The hardware client and the extras own
their threads.
"""

from __future__ import annotations

import importlib
import logging
import mimetypes
import threading
import time
import weakref
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from tactidose import __version__
from tactidose.api import api_router
from tactidose.api.common import call_supported, install_error_handlers, supported_kwargs
from tactidose.api.device import push_pill_counts
from tactidose.api.views import device_owner_id
from tactidose.auth.deps import session_token
from tactidose.config import Settings, get_settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.db.session import Database

log = logging.getLogger(__name__)

__all__ = ["STATIC_DIR", "SchedulerLoop", "Services", "build_services", "create_app", "shutdown", "startup"]

STATIC_DIR = Path(__file__).resolve().parent / "ui" / "static"
#: Page path -> file in ``STATIC_DIR`` and who may open it ("any", "patient", "caregiver", "demo").
PAGES: dict[str, tuple[str, str]] = {
    "/login": ("login.html", "any"),
    "/patient": ("patient.html", "patient"),
    "/care": ("care.html", "caregiver"),
    "/kiosk": ("kiosk.html", "patient"),
    "/demo": ("demo.html", "demo"),
}
#: The scheduler's first cycle waits (at most) this long for the device handshake, so a dose
#: that is due at startup is not refused as DEVICE_UNAVAILABLE just because the link is opening.
HARDWARE_GRACE_S = 10.0
PAGE_HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}
VOICE_JOIN_S = 2.0
#: ``Services.stopping`` of every running app (see :func:`install_server_shutdown_hook`).
_LIVE_STOP_EVENTS: "weakref.WeakSet[threading.Event]" = weakref.WeakSet()


# =========================================================================== scheduler loop


class SchedulerLoop:
    """The ``scheduler`` thread (ARCHITECTURE §4).

    Every ``interval_s`` — and immediately after :meth:`trigger` (schedule/settings edits,
    demo clock travel) — runs ``scheduler.tick()`` then ``drops.run_scheduled_drops()``.
    Cycles never overlap (one lock) and never raise. :meth:`exclusive` holds that lock for
    work that must not interleave with a cycle (demo reset).
    """

    def __init__(
        self,
        scheduler: Any,
        drops: Any,
        *,
        interval_s: float,
        hardware: Any | None = None,
        hardware_grace_s: float = HARDWARE_GRACE_S,
    ) -> None:
        self.scheduler = scheduler
        self.drops = drops
        self.interval_s = max(0.05, float(interval_s))
        self.hardware = hardware
        self.hardware_grace_s = max(0.0, float(hardware_grace_s))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cycles = 0
        self.triggers = 0
        self.last_cycle: dict[str, Any] | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler loop started (every %.0f s)", self.interval_s)
        return True

    def request_stop(self) -> None:
        """No new cycles from now on (a cycle in progress finishes)."""
        self._stop.set()
        self._wake.set()

    def close(self, timeout: float = 5.0) -> None:
        self.request_stop()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
            if t.is_alive():
                log.warning("scheduler loop still busy after %.1f s (a drop in progress?)", timeout)

    def trigger(self) -> None:
        """Run a cycle as soon as possible (no-op before :meth:`start`)."""
        self.triggers += 1
        self._wake.set()

    # ------------------------------------------------------------------ work
    def tick_now(self) -> int:
        """``scheduler.tick()`` on the calling thread (materialise + DUE/MISSED); thread-safe."""
        return int(self.scheduler.tick() or 0)

    def run_once(self) -> dict[str, Any]:
        """One cycle: tick, then scheduled drops. Never raises."""
        with self._lock:
            out: dict[str, Any] = {"changes": 0, "drops": 0, "errors": []}
            try:
                out["changes"] = int(self.scheduler.tick() or 0)
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                log.exception("scheduler tick failed")
                out["errors"].append(f"tick: {type(exc).__name__}")
            try:
                out["drops"] = int(self.drops.run_scheduled_drops() or 0)
            except Exception as exc:  # noqa: BLE001 - DropService should not raise; stay alive anyway
                log.exception("run_scheduled_drops failed")
                out["errors"].append(f"drops: {type(exc).__name__}")
            out["at"] = time.time()
            self.cycles += 1
            self.last_cycle = out
            self.last_error = out["errors"][-1] if out["errors"] else None
            return out

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._lock:
            yield

    def status(self) -> dict[str, Any]:
        last = self.last_cycle or {}
        return {"running": self.running, "interval_s": self.interval_s, "cycles": self.cycles,
                "last_cycle_at": last.get("at"), "last_error": self.last_error}

    def _await_hardware(self) -> None:
        hw = self.hardware
        if hw is None or self.hardware_grace_s <= 0:
            return
        deadline = time.monotonic() + self.hardware_grace_s
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                snap = hw.snapshot()
                if snap.connected or snap.mode == "none":
                    return
            except Exception:  # noqa: BLE001
                return
            self._stop.wait(0.1)

    def _run(self) -> None:
        self._await_hardware()
        while not self._stop.is_set():
            self._wake.clear()
            self.run_once()
            self._wake.wait(self.interval_s)


# =========================================================================== services


@dataclass
class Services:
    """Everything the routes need. Built by :func:`build_services` (tests inject fakes)."""

    settings: Settings
    clock: Clock
    bus: EventBus
    db: Database
    hardware: Any
    sim: Any | None = None
    notifications: Any = None
    compartments: Any = None
    catalog: Any = None
    scheduler: Any = None
    drops: Any = None
    auth: Any = None
    agent: Any = None
    reports: Any = None
    extractor: Any = None
    onboarding: Any = None
    speaker: Any = None
    voice_loop: Any = None
    analytics_sync: Any = None
    #: Optional well-being check-in bridge (tactidose/wellbeing.py); None when off or not installed.
    wellbeing: Any = None
    #: Guided judge demo runner (tactidose/guided/); its endpoints need demo mode.
    guided: Any = None
    scheduler_loop: SchedulerLoop | None = None
    #: Step name -> short error, for steps that failed at build/startup ("degraded" in /api/health).
    startup_errors: dict[str, str] = field(default_factory=dict)
    #: Startup steps in the order they ran (diagnostics/tests).
    startup_log: list[str] = field(default_factory=list)
    #: Set when the app shuts down: open SSE streams end.
    stopping: threading.Event = field(default_factory=threading.Event)
    #: SSE tuning (tests shorten these).
    sse_keepalive_s: float = 15.0
    sse_max_stream_s: float | None = None
    sse_scope_refresh_s: float = 30.0
    #: True when build_services created the Database (then shutdown disposes it).
    owns_db: bool = False
    voice_thread: threading.Thread | None = None

    def device_patient_id(self) -> int | None:
        return device_owner_id(self.db, self.settings)

    def trigger_scheduler(self) -> None:
        if self.scheduler_loop is not None:
            self.scheduler_loop.trigger()


def _note(errors: dict[str, str], name: str, exc: BaseException) -> None:
    errors[name] = f"{type(exc).__name__}: {exc}"[:300]


def _optional(errors: dict[str, str], name: str, build: Callable[[], Any]) -> Any:
    """Build an optional component; failures are logged and recorded, never raised."""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - optional component
        log.exception("could not build %s; continuing without it", name)
        _note(errors, name, exc)
        return None


def build_services(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    db: Database | None = None,
    hardware: Any = None,
    sim: Any = None,
    notifications: Any = None,
    compartments: Any = None,
    catalog: Any = None,
    scheduler: Any = None,
    drops: Any = None,
    auth: Any = None,
    agent: Any = None,
    reports: Any = None,
    extractor: Any = None,
    onboarding: Any = None,
    speaker: Any = None,
    voice_loop: Any = None,
    analytics_sync: Any = None,
    wellbeing: Any = None,
) -> Services:
    """Construct the real implementations (ARCHITECTURE §11) for everything not injected.

    Parallel v2 modules are imported lazily here. ``notifications``, ``drops`` and ``auth`` are
    required (an ImportError propagates); the agent, reports and the extras are optional: if
    they fail to build the app still starts and their endpoints answer 503. The well-being
    check-in is optional too: None when disabled or when ``tactidose-wellbeing`` is not installed.
    """
    settings = settings or get_settings()
    errors: dict[str, str] = {}
    clock = clock or Clock(settings.timezone)
    bus = bus or EventBus()
    owns_db = db is None
    db = db or Database(settings)
    if hardware is None:
        from tactidose.hardware.serial_client import create_hardware

        hardware, sim = create_hardware(settings, bus=bus, clock=clock)

    if notifications is None:
        from tactidose.medication.notifications import NotificationService

        notifications = call_supported(NotificationService, db, settings, clock, bus=bus)
    if compartments is None:
        from tactidose.medication.compartments import CompartmentService

        compartments = call_supported(CompartmentService, db, settings, bus=bus, clock=clock)
    if catalog is None:
        from tactidose.medication.catalog import MedicationCatalog

        catalog = call_supported(MedicationCatalog, db, settings, clock, bus=bus)
    if scheduler is None:
        from tactidose.medication.scheduler import Scheduler

        scheduler = call_supported(Scheduler, db, clock, settings, bus=bus, notifications=notifications)
    if drops is None:
        from tactidose.medication.drops import DropService

        drops = call_supported(DropService, db, hardware, clock, settings, notifications=notifications, bus=bus)
    if auth is None:
        from tactidose.auth.service import AuthService

        auth = call_supported(AuthService, db, settings, clock, bus=bus)

    if agent is None:
        def _agent() -> Any:
            from tactidose.agent.service import AgentService

            return call_supported(AgentService, db, drops, clock, settings, bus=bus)

        agent = _optional(errors, "agent", _agent)
    if reports is None:
        def _reports() -> Any:
            from tactidose.reports.service import ReportService

            return call_supported(ReportService, db, clock, settings, auth=auth, notifications=notifications, bus=bus)

        reports = _optional(errors, "reports", _reports)
    if extractor is None:
        def _extractor() -> Any:
            from tactidose.integrations.gemini import create_label_extractor

            return create_label_extractor(settings)

        extractor = _optional(errors, "extractor", _extractor)
    if onboarding is None:
        def _onboarding() -> Any:
            from tactidose.medication.onboarding import OnboardingService

            return call_supported(OnboardingService, db, extractor, catalog, settings, clock, bus=bus)

        onboarding = _optional(errors, "onboarding", _onboarding)
    if analytics_sync is None and settings.snowflake_configured:
        def _sync() -> Any:
            from tactidose.integrations.snowflake import SnowflakeSync

            return SnowflakeSync(db, settings, clock, bus=bus)

        analytics_sync = _optional(errors, "analytics_sync", _sync)
    if wellbeing is None and settings.wellbeing_enabled:
        def _wellbeing() -> Any:
            from tactidose.wellbeing import build_wellbeing

            return build_wellbeing(settings, db=db, clock=clock, bus=bus)

        wellbeing = _optional(errors, "wellbeing", _wellbeing)

    services = Services(
        settings=settings, clock=clock, bus=bus, db=db, hardware=hardware, sim=sim,
        notifications=notifications, compartments=compartments, catalog=catalog, scheduler=scheduler,
        drops=drops, auth=auth, agent=agent, reports=reports, extractor=extractor, onboarding=onboarding,
        speaker=speaker, voice_loop=voice_loop, analytics_sync=analytics_sync, wellbeing=wellbeing,
        startup_errors=errors, owns_db=owns_db,
    )
    services.scheduler_loop = SchedulerLoop(
        scheduler, drops, interval_s=settings.scheduler_tick_s, hardware=hardware,
    )

    def _guided() -> Any:
        from tactidose.guided import GuidedDemoRunner

        return GuidedDemoRunner(services)

    services.guided = _optional(errors, "guided", _guided)
    return services


# =========================================================================== lifecycle


def _step(services: Services, name: str, fn: Callable[[], Any]) -> Any:
    services.startup_log.append(name)
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one failing step must not prevent the others
        log.exception("startup step %r failed; continuing", name)
        _note(services.startup_errors, name, exc)
        return None


def _seed(services: Services) -> Any:
    from tactidose.db.seed import seed_demo

    summary = call_supported(seed_demo, services.db, services.settings, services.clock, auth=services.auth,
                             bus=services.bus)
    log.info("demo data ready: %s", summary.get("created") if isinstance(summary, dict) else summary)
    if isinstance(summary, dict):
        push_pill_counts(services.sim, summary.get("containers"))
    return summary


def _recover(services: Services) -> Any:
    fn = getattr(services.drops, "recover_on_startup", None)
    if not callable(fn):
        log.warning("DropService has no recover_on_startup(); skipping startup recovery")
        return None
    recovered = fn()
    if recovered:
        log.warning("startup recovery: %s in-flight drop(s) marked UNCERTAIN for review", recovered)
    return recovered


def _build_voice_loop(services: Services) -> Any:
    module = importlib.import_module("tactidose.agent.voice_loop")
    cls = getattr(module, "DeviceVoiceLoop", None) or module.VoiceLoop
    pool = {
        "settings": services.settings, "clock": services.clock, "bus": services.bus, "db": services.db,
        "hardware": services.hardware, "drops": services.drops, "agent": services.agent,
        "notifications": services.notifications, "auth": services.auth, "speaker": services.speaker,
        "patient_id": services.device_patient_id(),
    }
    return cls(**supported_kwargs(cls, pool))


def _start_voice(services: Services) -> None:
    """Build/start the optional device-side voice loop off the startup path (Vosk loads slowly)."""

    def run() -> None:
        try:
            loop = services.voice_loop or _build_voice_loop(services)
            services.voice_loop = loop
            if loop is None or services.stopping.is_set():
                return
            loop.start()
            if services.stopping.is_set():  # shut down while the model was loading
                loop.close()
        except Exception as exc:  # noqa: BLE001 - optional extra
            log.exception("voice loop did not start; continuing without it")
            _note(services.startup_errors, "voice", exc)

    services.voice_thread = threading.Thread(target=run, name="voice-start", daemon=True)
    services.voice_thread.start()


def _preload_speech(services: Services) -> None:
    """Load the server-side Vosk model for ``/api/agent/transcribe`` in the background (1-16 s)."""
    preload = getattr(services.agent, "preload_speech_model", None)
    if not callable(preload):
        return

    def run() -> None:
        try:
            preload()
        except Exception:  # noqa: BLE001 - transcribe then answers 503 / loads lazily
            log.exception("speech model preload failed")

    threading.Thread(target=run, name="stt-preload", daemon=True).start()


def startup(services: Services) -> None:
    """Blocking startup sequence (runs in the threadpool from the lifespan)."""
    s = services.settings
    services.stopping.clear()
    _step(services, "create_all", services.db.create_all)
    if s.demo_mode and s.seed_demo_accounts:
        _step(services, "seed_demo", lambda: _seed(services))
    _step(services, "recover_on_startup", lambda: _recover(services))
    if services.scheduler_loop is not None:
        _step(services, "scheduler_loop", services.scheduler_loop.start)
    _step(services, "hardware", services.hardware.start)
    if s.voice_enabled:
        _step(services, "voice", lambda: _start_voice(services))
    if services.analytics_sync is not None:
        _step(services, "analytics_sync", services.analytics_sync.start)
    if s.voice_enabled and services.agent is not None:
        _step(services, "stt_preload", lambda: _preload_speech(services))
    services.bus.publish(Topic.NOTICE, {"level": "info", "message": "TactiDose started."})
    log.info("TactiDose %s started (demo_mode=%s, hardware=%s, agent=%s)", __version__, s.demo_mode,
             s.hardware_mode, s.effective_agent_provider)


def _close(name: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - keep shutting the rest down
        log.exception("error while stopping %s", name)


def shutdown(services: Services) -> None:
    """Stop in reverse dependency order; every step guarded.

    The scheduler loop is told to stop first (no new cycle starts), the extras are closed, then
    the loop is joined — letting a scheduled drop in progress finish — before the hardware link
    is closed, so shutting down never turns a normal drop into an UNCERTAIN one.
    """
    services.stopping.set()
    loop = services.scheduler_loop
    if loop is not None:
        loop.request_stop()
    t = services.voice_thread
    if t is not None and t.is_alive():
        t.join(VOICE_JOIN_S)
    if services.voice_loop is not None:
        _close("voice loop", services.voice_loop.close)
    if services.analytics_sync is not None:
        _close("analytics sync", services.analytics_sync.close)
    if services.guided is not None:
        _close("guided demo", services.guided.close)
    if services.wellbeing is not None:
        _close("wellbeing", services.wellbeing.close)
    agent_close = getattr(services.agent, "close", None)
    if callable(agent_close):
        _close("agent", agent_close)
    if loop is not None:
        _close("scheduler loop", lambda: loop.close(timeout=float(services.settings.timeout_drop_s) + 5.0))
    _close("hardware", services.hardware.close)
    if services.owns_db:
        _close("database", services.db.dispose)
    log.info("TactiDose stopped")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    services: Services = app.state.services
    await run_in_threadpool(startup, services)
    _LIVE_STOP_EVENTS.add(services.stopping)
    try:
        yield
    finally:
        _LIVE_STOP_EVENTS.discard(services.stopping)
        await run_in_threadpool(shutdown, services)


def install_server_shutdown_hook() -> bool:
    """End open SSE streams as soon as uvicorn starts to shut down.

    uvicorn runs the ASGI lifespan shutdown only after open responses have finished, and an
    event stream never finishes by itself: Ctrl+C would wait ``timeout_graceful_shutdown`` and
    then cancel the stream with a traceback. Wrapping ``uvicorn.Server.shutdown`` to set
    ``Services.stopping`` first lets every stream end within half a second (same approach as
    sse-starlette). Idempotent; returns False when uvicorn is not importable.
    """
    try:
        from uvicorn.server import Server
    except Exception:  # noqa: BLE001 - optional: only matters when served by uvicorn
        return False
    original = Server.shutdown
    if getattr(original, "_tactidose_hook", False):
        return True

    async def shutdown(self: Any, *args: Any, **kwargs: Any) -> Any:
        for stopping in list(_LIVE_STOP_EVENTS):
            stopping.set()
        return await original(self, *args, **kwargs)

    shutdown._tactidose_hook = True  # type: ignore[attr-defined]
    Server.shutdown = shutdown  # type: ignore[method-assign]
    return True


# =========================================================================== pages & static


def register_mimetypes() -> None:
    """Windows registries often map .js to text/plain; browsers then refuse ES modules (§13)."""
    for mime, ext in (
        ("text/javascript", ".js"), ("text/javascript", ".mjs"), ("text/css", ".css"),
        ("image/svg+xml", ".svg"), ("application/json", ".json"), ("application/manifest+json", ".webmanifest"),
        ("font/woff2", ".woff2"), ("audio/wav", ".wav"),
    ):
        mimetypes.add_type(mime, ext)


def _page_user(request: Request, services: Services) -> Any:
    token = session_token(request, services.settings)
    if not token:
        return None
    try:
        return services.auth.resolve(token)
    except Exception:  # noqa: BLE001 - pages degrade to the login screen
        log.exception("session lookup for a page failed")
        return None


def _portal_for(user: Any) -> str:
    return "/patient" if getattr(user, "is_patient", False) else "/care"


def _page_file(name: str) -> Response:
    path = STATIC_DIR / name
    if not path.is_file():
        return JSONResponse({"detail": f"Page not available ({name})."}, status_code=404)
    return FileResponse(path, media_type="text/html", headers=PAGE_HEADERS)


def _add_pages(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    def root(request: Request) -> Response:
        user = _page_user(request, request.app.state.services)
        return RedirectResponse("/login" if user is None else _portal_for(user), status_code=303)

    def make_page(path: str, filename: str, audience: str) -> Callable[[Request], Response]:
        def page(request: Request) -> Response:
            services = request.app.state.services
            if audience == "demo":
                if not services.settings.demo_mode:
                    return JSONResponse({"detail": "Demo mode is off."}, status_code=404)
                return _page_file(filename)
            if audience != "any":
                user = _page_user(request, services)
                if user is None:
                    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
                wants_patient = audience == "patient"
                if bool(getattr(user, "is_patient", False)) != wants_patient:
                    return RedirectResponse(_portal_for(user), status_code=303)
            return _page_file(filename)

        page.__name__ = f"page_{path.strip('/') or 'root'}"
        return page

    for path, (filename, audience) in PAGES.items():
        app.add_api_route(path, make_page(path, filename, audience), methods=["GET"], include_in_schema=False)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        icon = STATIC_DIR / "img" / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)


def _mount_static(app: FastAPI) -> None:
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    else:
        log.warning("UI directory %s is missing; pages and /static are unavailable", STATIC_DIR)


# =========================================================================== factory


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    """The FastAPI app. Without ``services`` the real ones are built from ``settings``
    (default: ``Settings()`` from the environment)."""
    if services is None:
        services = build_services(settings)
    register_mimetypes()
    install_server_shutdown_hook()
    app = FastAPI(
        title="TactiDose",
        version=__version__,
        description="TactiDose v2 HTTP API (hackathon prototype - not a medical device).",
        lifespan=_lifespan,
        redoc_url=None,
    )
    app.state.services = services
    install_error_handlers(app)
    app.include_router(api_router())
    if services.wellbeing is not None:
        from tactidose.wellbeing import mount_wellbeing

        mount_wellbeing(app, services)
    _add_pages(app)
    _mount_static(app)
    return app
