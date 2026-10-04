from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tactidose_wellbeing.api import DEV_USER_HEADER, DevHeaderIdentityProvider, create_app
from tactidose_wellbeing.contract import ActionRequest, StartSessionRequest
from tactidose_wellbeing.service import WellbeingService
from tactidose_wellbeing.storage import InMemorySessionStore, SQLiteCheckinRepository


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def make_service(repo=None, clock=None, **kwargs) -> WellbeingService:
    counter = itertools.count(1)
    return WellbeingService(
        repo if repo is not None else SQLiteCheckinRepository(":memory:"),
        InMemorySessionStore(),
        clock=clock or FakeClock(),
        id_factory=lambda: f"{next(counter):04d}",
        **kwargs,
    )


class Driver:
    """Drives one session through the service with auto-generated request ids."""

    def __init__(self, service: WellbeingService, user_id: str = "user-a") -> None:
        self.service = service
        self.user_id = user_id
        self._ids = itertools.count(1)
        self.last = None
        self.session_id = None

    def start(self):
        self.last = self.service.start_session(
            self.user_id, StartSessionRequest(request_id=f"{self.user_id}-start-{next(self._ids)}")
        )
        self.session_id = self.last.session_id
        return self.last

    def act(self, action: str, text: str | None = None, **extra):
        kwargs = dict(extra)
        if action == "answer":
            kwargs["answer"] = text
        elif action == "add_note":
            kwargs["note_text"] = text
        req = ActionRequest(request_id=f"{self.user_id}-{next(self._ids)}", action=action, **kwargs)
        self.last = self.service.handle_action(self.user_id, self.session_id, req)
        return self.last

    def say(self, *texts: str):
        for t in texts:
            self.act("answer", t)
        return self.last


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo() -> SQLiteCheckinRepository:
    return SQLiteCheckinRepository(":memory:")


@pytest.fixture
def service(repo, clock) -> WellbeingService:
    return make_service(repo, clock)


@pytest.fixture
def driver(service) -> Driver:
    d = Driver(service)
    d.start()
    return d


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service, DevHeaderIdentityProvider(require_loopback=False)))


def dev_headers(user_id: str) -> dict[str, str]:
    return {DEV_USER_HEADER: user_id}
