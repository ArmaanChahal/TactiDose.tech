"""AuthService: registration, login, sessions (frozen clock), lockout and helpers (ARCHITECTURE §9)."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from tactidose.auth import passwords
from tactidose.auth.errors import AuthError, PermissionDenied, TooManyAttempts
from tactidose.auth.service import (
    LINK_CODE_ALPHABET,
    LOCKOUT_SECONDS,
    MSG_INVALID_LOGIN,
    AuthService,
    clean_phone,
    normalize_email,
    token_hash,
)
from tactidose.core.bus import Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import AuthServiceAPI, AuthUser
from tactidose.db.models import AuthSession, Compartment, Device, DeviceLog, User
from tactidose.medication.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from tests.conftest import TEST_TZ

PASSWORD = "correct horse"


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(passwords, "SCRYPT_N", 2 ** 10)


@pytest.fixture
def auth(db_v2, settings_v2, clock, bus) -> AuthService:
    return AuthService(db_v2, settings_v2, clock, bus=bus)


@pytest.fixture
def offline_auth(settings_v2, clock) -> AuthService:
    """No database at all: input checks must reject before any query is made."""
    return AuthService(None, settings_v2, clock)  # type: ignore[arg-type]


def _register(auth: AuthService, email: str, role: str = "patient", name: str = "Test Person") -> AuthUser:
    return auth.register(email=email, password=PASSWORD, display_name=name, role=role)


def _session_row(db, token: str) -> AuthSession:
    with db.session() as s:
        return s.scalars(select(AuthSession).where(AuthSession.token_hash == token_hash(token))).one()


# --------------------------------------------------------------------------- registration


def test_service_implements_the_protocol(auth: AuthService) -> None:
    assert isinstance(auth, AuthServiceAPI)


def test_register_patient_normalises_input_and_binds_the_device(auth, db_v2, settings_v2, bus) -> None:
    sub = bus.subscribe([Topic.PATIENT_STATUS])
    user = auth.register(email="  Alex@Example.COM ", password=PASSWORD,
                         display_name="  Alex \t Rivera ", role=" Patient ", phone=" +1 (604) 555-0100 ")
    assert user == AuthUser(user.user_id, "Alex Rivera", "patient", "alex@example.com")
    with db_v2.session() as s:
        row = s.get(User, user.user_id)
        assert row.phone == "+1 (604) 555-0100" and row.is_active and row.last_login_at is None
        assert passwords.verify_password(PASSWORD, row.password_hash) and PASSWORD not in row.password_hash
        assert len(row.link_code) == 8 and set(row.link_code) <= set(LINK_CODE_ALPHABET)
        dev = s.get(Device, settings_v2.device_id)
        assert dev.user_id == user.user_id and dev.manual_cooldown_minutes == settings_v2.manual_cooldown_minutes
        slots = sorted(c.slot_number for c in s.scalars(select(Compartment)))
        assert slots == [0, 1, 2]
        events = [r.event for r in s.scalars(select(DeviceLog).order_by(DeviceLog.log_id))]
        assert "USER_CREATED" in events and "DEVICE_BOUND" in events
    profile = auth.patient_profile(user.user_id)
    assert profile == {"patient_id": user.user_id, "link_code": profile["link_code"],
                       "device_id": settings_v2.device_id}
    assert [e.data for e in sub.drain()] == [{"patient_id": user.user_id, "reason": "device_bound"}]


def test_link_codes_avoid_ambiguous_characters(auth) -> None:
    codes = {auth.patient_profile(_register(auth, f"p{i}@example.com").user_id)["link_code"] for i in range(6)}
    assert len(codes) == 6
    assert not set("".join(codes)) & set("01ILO")


def test_register_caregivers_get_no_code_and_no_device(auth, db_v2) -> None:
    for role in ("doctor", "family"):
        user = _register(auth, f"{role}@example.com", role=role)
        assert user.is_caregiver and not user.is_patient
        with db_v2.session() as s:
            assert s.get(User, user.user_id).link_code is None
    with db_v2.session() as s:
        assert s.scalars(select(Device)).all() == []
    with pytest.raises(NotFoundError):
        auth.patient_profile(user.user_id)


def test_second_patient_does_not_take_the_device(auth, db_v2, settings_v2) -> None:
    first = _register(auth, "first@example.com")
    second = _register(auth, "second@example.com")
    with db_v2.session() as s:
        assert s.get(Device, settings_v2.device_id).user_id == first.user_id
    assert auth.patient_profile(second.user_id)["device_id"] is None


@pytest.mark.parametrize("email", [
    "", "   ", "plainaddress", "@example.com", "name@", "name@localhost", "two@@example.com",
    "a@b@example.com", "name@exa_mple.com", "name@-example.com", "name@example-.com", ".name@example.com",
    "na..me@example.com", "name.@example.com", "na me@example.com", "name@example..com",
    "x" * 65 + "@example.com", "name@" + "a" * 250 + ".com", "ünïcode@example.com", None, 42,
])
def test_invalid_emails_are_rejected(offline_auth, email) -> None:
    with pytest.raises(ValidationError) as info:
        offline_auth.register(email=email, password=PASSWORD, display_name="X", role="patient")
    assert info.value.status_code == 422


@pytest.mark.parametrize("email,expected", [
    ("Dr.Lee@Demo.TactiDose", "dr.lee@demo.tactidose"),
    ("first.last+tag@sub.example.org", "first.last+tag@sub.example.org"),
    ("ａｌｅｘ@example.com", "alex@example.com"),   # full-width letters
])
def test_email_normalisation(email, expected) -> None:
    assert normalize_email(email) == expected


@pytest.mark.parametrize("field,value", [
    ("role", "admin"), ("role", ""), ("role", None),
    ("password", "short"), ("password", None),
    ("display_name", "   "), ("display_name", "\x00\x01"), ("display_name", "x" * 121), ("display_name", None),
    ("phone", "call me"), ("phone", "12"), ("phone", "+1" + "2" * 40), ("phone", 6045550100),
])
def test_invalid_fields_are_rejected(offline_auth, field, value) -> None:
    data = {"email": "valid@example.com", "password": PASSWORD, "display_name": "Valid Name", "role": "patient"}
    data[field] = value
    with pytest.raises(ValidationError):
        offline_auth.register(**data)


def test_phone_cleaning() -> None:
    assert clean_phone(None) is None and clean_phone("   ") is None
    assert clean_phone("604  555 0100") == "604 555 0100"


def test_duplicate_email_is_a_conflict(auth) -> None:
    _register(auth, "alex@example.com")
    with pytest.raises(ConflictError) as info:
        _register(auth, " ALEX@example.com", role="doctor")
    assert info.value.status_code == 409


def test_concurrent_registrations_of_one_email(auth) -> None:
    barrier = threading.Barrier(4)
    results: list[str] = []

    def worker() -> None:
        barrier.wait(timeout=10)
        try:
            _register(auth, "race@example.com", role="family")
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]


def test_registration_can_be_disabled(db_v2, settings_v2, clock) -> None:
    closed = AuthService(db_v2, settings_v2.model_copy(update={"allow_registration": False}), clock)
    with pytest.raises(PermissionDenied) as info:
        _register(closed, "alex@example.com")
    assert info.value.status_code == 403 and isinstance(info.value, DomainError)
    user = closed.create_user(email="alex@example.com", password=PASSWORD, display_name="Alex", role="patient")
    assert user.is_patient


def test_failed_device_binding_does_not_fail_registration(auth, monkeypatch, caplog) -> None:
    def broken(*args, **kwargs):
        raise OperationalError("UPDATE devices", {}, Exception("disk I/O error"))

    monkeypatch.setattr(auth, "bind_device", broken)
    user = _register(auth, "alex@example.com")
    assert user.is_patient and "could not bind device" in caplog.text


# --------------------------------------------------------------------------- login & sessions


def test_login_creates_a_hashed_session(auth, db_v2) -> None:
    created = _register(auth, "alex@example.com")
    user, token = auth.login(" ALEX@example.com ", PASSWORD, user_agent="Mozilla/5.0\n\x00 (Test)" + "x" * 400)
    assert user == created and len(token) >= 43
    row = _session_row(db_v2, token)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in row.token_hash
    assert row.expires_at == auth.now() + timedelta(hours=12) and row.last_seen_at == auth.now()
    assert row.user_agent.startswith("Mozilla/5.0 (Test)") and len(row.user_agent) == 255
    with db_v2.session() as s:
        assert s.get(User, created.user_id).last_login_at == auth.now()
    assert auth.resolve(token) == created
    _, other_token = auth.login("alex@example.com", PASSWORD)
    assert other_token != token and auth.resolve(other_token) == created


def test_bad_credentials_share_one_message(auth, db_v2) -> None:
    user = _register(auth, "alex@example.com")
    errors = []
    for email, password in [("alex@example.com", "wrong password"), ("nobody@example.com", PASSWORD),
                            ("not-an-email", PASSWORD), (None, PASSWORD), ("alex@example.com", None)]:
        with pytest.raises(AuthError) as info:
            auth.login(email, password)
        errors.append(info.value)
    with db_v2.session() as s:
        s.get(User, user.user_id).is_active = False
    with pytest.raises(AuthError) as info:
        auth.login("alex@example.com", PASSWORD)
    errors.append(info.value)
    assert {e.message for e in errors} == {MSG_INVALID_LOGIN}
    assert {e.status_code for e in errors} == {401} and not any(isinstance(e, TooManyAttempts) for e in errors)


def test_accounts_without_password_cannot_sign_in(auth, db_v2) -> None:
    with db_v2.session() as s:
        s.add(User(display_name="Legacy", email="legacy@example.com", role="patient"))
    with pytest.raises(AuthError):
        auth.login("legacy@example.com", "")
    with pytest.raises(AuthError):
        auth.login("legacy@example.com", PASSWORD)


@pytest.mark.parametrize("token", [None, "", "x" * 600, 12345, "\ud800", b"bytes"])
def test_resolve_rejects_garbage(offline_auth, token) -> None:
    assert offline_auth.resolve(token) is None
    offline_auth.logout(token)   # never raises, and never reaches the database


def test_unknown_tokens_resolve_to_nobody(auth) -> None:
    _register(auth, "alex@example.com")
    assert auth.resolve("abc") is None and auth.resolve("x" * 43) is None
    auth.logout("abc")


def test_logout_revokes_only_that_session(auth, db_v2) -> None:
    _register(auth, "alex@example.com")
    _, first = auth.login("alex@example.com", PASSWORD)
    _, second = auth.login("alex@example.com", PASSWORD)
    auth.logout(first)
    auth.logout(first)
    assert auth.resolve(first) is None and auth.resolve(second) is not None
    assert _session_row(db_v2, first).revoked_at == auth.now()


def test_sliding_expiry_with_the_frozen_clock(auth, clock) -> None:
    _register(auth, "alex@example.com")
    _, token = auth.login("alex@example.com", PASSWORD)
    clock.advance(timedelta(hours=11, minutes=59))
    assert auth.resolve(token) is not None          # used: expiry slides to now + 12 h
    clock.advance(timedelta(hours=11, minutes=59))
    assert auth.resolve(token) is not None
    clock.advance(timedelta(hours=12))
    assert auth.resolve(token) is None              # 12 h without a request


def test_session_expires_without_activity(auth, clock) -> None:
    _register(auth, "alex@example.com")
    _, token = auth.login("alex@example.com", PASSWORD)
    clock.advance(timedelta(hours=12) - timedelta(seconds=1))
    _, fresh = auth.login("alex@example.com", PASSWORD)
    clock.advance(timedelta(seconds=1))
    assert auth.resolve(token) is None and auth.resolve(fresh) is not None


def test_last_seen_is_written_at_most_once_a_minute(auth, db_v2, clock) -> None:
    _register(auth, "alex@example.com")
    _, token = auth.login("alex@example.com", PASSWORD)
    login_time = auth.now()
    clock.advance(timedelta(seconds=30))
    assert auth.resolve(token) is not None
    assert _session_row(db_v2, token).last_seen_at == login_time
    clock.advance(timedelta(seconds=31))
    assert auth.resolve(token) is not None
    row = _session_row(db_v2, token)
    assert row.last_seen_at == auth.now() and row.expires_at == auth.now() + timedelta(hours=12)


def test_failed_session_refresh_still_authenticates(auth, db_v2, clock, monkeypatch, caplog) -> None:
    user = _register(auth, "alex@example.com")
    _, token = auth.login("alex@example.com", PASSWORD)
    clock.advance(timedelta(minutes=5))
    real_session = db_v2.session
    calls = {"n": 0}

    def flaky_session():
        calls["n"] += 1
        if calls["n"] == 2:          # the second session of resolve() is the refresh
            raise OperationalError("UPDATE auth_sessions", {}, Exception("database is locked"))
        return real_session()

    monkeypatch.setattr(db_v2, "session", flaky_session)
    assert auth.resolve(token) == user
    assert "could not extend session" in caplog.text


def test_demo_time_travel_does_not_log_anyone_out(db_v2, settings_v2) -> None:
    live = Clock(TEST_TZ)
    auth = AuthService(db_v2, settings_v2, live)
    _register(auth, "alex@example.com")
    _, token = auth.login("alex@example.com", PASSWORD)
    live.set_offset(timedelta(days=3))
    assert abs((auth.now() - (live.now() - timedelta(days=3))).total_seconds()) < 5
    assert auth.resolve(token) is not None
    live.set_offset(timedelta(days=-3))
    assert auth.resolve(token) is not None


def test_disabled_accounts_lose_their_sessions(auth, db_v2) -> None:
    user = _register(auth, "alex@example.com")
    _, token = auth.login("alex@example.com", PASSWORD)
    with db_v2.session() as s:
        s.get(User, user.user_id).is_active = False
    assert auth.resolve(token) is None


def test_revoke_and_purge_sessions(auth, db_v2, clock) -> None:
    user = _register(auth, "alex@example.com")
    tokens = [auth.login("alex@example.com", PASSWORD)[1] for _ in range(3)]
    assert auth.revoke_sessions(user.user_id) == 3
    assert all(auth.resolve(t) is None for t in tokens)
    keep = auth.start_session(user)
    clock.advance(timedelta(hours=13))
    assert auth.purge_expired_sessions() == 4            # 3 revoked + 1 expired
    late = auth.start_session(user.user_id)
    with db_v2.session() as s:
        assert [r.token_hash for r in s.scalars(select(AuthSession))] == [token_hash(late)]
    assert auth.resolve(keep) is None and auth.resolve(late) == user


def test_login_purges_old_sessions_opportunistically(auth, db_v2, clock) -> None:
    _register(auth, "alex@example.com")
    _, old = auth.login("alex@example.com", PASSWORD)
    clock.advance(timedelta(hours=13))
    auth.login("alex@example.com", PASSWORD)
    with db_v2.session() as s:
        assert token_hash(old) not in {r.token_hash for r in s.scalars(select(AuthSession))}


def test_start_session_requires_an_active_account(auth, db_v2) -> None:
    user = _register(auth, "alex@example.com")
    assert auth.resolve(auth.start_session(user, user_agent="kiosk")) == user
    with db_v2.session() as s:
        s.get(User, user.user_id).is_active = False
    for target in (user, user.user_id, 9999, "1"):
        with pytest.raises(AuthError):
            auth.start_session(target)


def test_login_upgrades_old_hashes(auth, db_v2, monkeypatch) -> None:
    user = _register(auth, "alex@example.com")
    monkeypatch.setattr(passwords, "SCRYPT_N", 2 ** 11)
    auth.login("alex@example.com", PASSWORD)
    with db_v2.session() as s:
        stored = s.get(User, user.user_id).password_hash
    assert stored.split("$")[1] == "2048" and passwords.verify_password(PASSWORD, stored)


# --------------------------------------------------------------------------- lockout


def test_five_failures_lock_the_email_for_30_seconds(auth, clock) -> None:
    _register(auth, "alex@example.com")
    for _ in range(4):
        with pytest.raises(AuthError) as info:
            auth.login("alex@example.com", "wrong password")
        assert not isinstance(info.value, TooManyAttempts)
    with pytest.raises(TooManyAttempts) as info:
        auth.login("alex@example.com", "wrong password")
    assert info.value.status_code == 429 and info.value.retry_after_s == LOCKOUT_SECONDS
    clock.advance(timedelta(seconds=10))
    with pytest.raises(TooManyAttempts) as info:
        auth.login("Alex@Example.com", PASSWORD)          # even the right password waits
    assert info.value.retry_after_s == 20 and "20 seconds" in info.value.message
    clock.advance(timedelta(seconds=21))
    user, token = auth.login("alex@example.com", PASSWORD)
    assert auth.resolve(token) == user


def test_unknown_emails_lock_the_same_way(auth) -> None:
    for _ in range(4):
        with pytest.raises(AuthError):
            auth.login("ghost@example.com", "whatever1")
    with pytest.raises(TooManyAttempts):
        auth.login("ghost@example.com", "whatever1")


def test_lockout_is_per_email_and_reset_by_success(auth, clock) -> None:
    _register(auth, "alex@example.com")
    _register(auth, "sam@example.com", role="family")
    for _ in range(4):
        with pytest.raises(AuthError):
            auth.login("alex@example.com", "wrong password")
    auth.login("sam@example.com", PASSWORD)
    auth.login("alex@example.com", PASSWORD)              # success clears the counter
    for _ in range(4):
        with pytest.raises(AuthError) as info:
            auth.login("alex@example.com", "wrong password")
        assert not isinstance(info.value, TooManyAttempts)


def test_old_failures_stop_counting(auth, clock) -> None:
    _register(auth, "alex@example.com")
    for _ in range(4):
        with pytest.raises(AuthError):
            auth.login("alex@example.com", "wrong password")
    clock.advance(timedelta(minutes=16))
    for _ in range(4):
        with pytest.raises(AuthError) as info:
            auth.login("alex@example.com", "wrong password")
        assert not isinstance(info.value, TooManyAttempts)


def test_database_errors_are_not_counted_as_guesses(auth, db_v2, monkeypatch) -> None:
    _register(auth, "alex@example.com")
    real_session = db_v2.session

    def down():
        raise OperationalError("SELECT", {}, Exception("server has gone away"))

    monkeypatch.setattr(db_v2, "session", down)
    for _ in range(6):
        with pytest.raises(OperationalError):
            auth.login("alex@example.com", PASSWORD)
    monkeypatch.setattr(db_v2, "session", real_session)
    assert auth.login("alex@example.com", PASSWORD)[0].email == "alex@example.com"


def test_parallel_guesses_share_the_limit(auth, monkeypatch) -> None:
    _register(auth, "alex@example.com")
    entered = threading.Semaphore(0)
    release = threading.Event()

    def slow_verify(password, encoded):
        entered.release()
        release.wait(10)
        return False

    monkeypatch.setattr(passwords, "verify_password", slow_verify)
    outcomes: list[str] = []

    def guess() -> None:
        try:
            auth.login("alex@example.com", "guess-guess")
        except TooManyAttempts:
            outcomes.append("locked")
        except AuthError:
            outcomes.append("wrong")

    threads = [threading.Thread(target=guess) for _ in range(5)]
    for t in threads:
        t.start()
    for _ in threads:
        assert entered.acquire(timeout=10)
    with pytest.raises(TooManyAttempts):
        auth.login("alex@example.com", PASSWORD)          # refused before any password check
    release.set()
    for t in threads:
        t.join(timeout=10)
    assert sorted(outcomes) == ["wrong"] * 5


def test_clear_lockouts(auth) -> None:
    _register(auth, "alex@example.com")
    for _ in range(5):
        with pytest.raises(AuthError):
            auth.login("alex@example.com", "wrong password")
    auth.clear_lockouts()
    assert auth.login("alex@example.com", PASSWORD)


# --------------------------------------------------------------------------- helpers for the API


def test_get_user_and_user_to_dict(auth, db_v2) -> None:
    user = auth.register(email="alex@example.com", password=PASSWORD, display_name="Alex",
                         role="patient", phone="604 555 0100")
    assert auth.get_user(user.user_id) == user and auth.get_user(9999) is None
    assert auth.get_user(True) is None and auth.get_user("1") is None   # type: ignore[arg-type]
    assert auth.get_user_by_email(" ALEX@example.com") == user
    assert auth.get_user_by_email("nobody@example.com") is None and auth.get_user_by_email("junk") is None
    view = auth.user_to_dict(user)
    assert set(view) == {"user_id", "email", "display_name", "role", "phone", "created_at"}
    assert view["phone"] == "604 555 0100" and view["created_at"].endswith("+00:00")
    assert auth.user_to_dict(user.user_id) == view
    with db_v2.session() as s:
        assert auth.user_to_dict(s.get(User, user.user_id)) == view
    text = json.dumps(view)
    assert "scrypt" not in text and "link_code" not in text
    ghost = AuthUser(4242, "Ghost", "doctor", "ghost@example.com")
    assert auth.user_to_dict(ghost)["created_at"] is None
    with pytest.raises(NotFoundError):
        auth.user_to_dict(4242)


def test_patient_profile_assigns_codes_to_legacy_patients(auth, db_v2) -> None:
    with db_v2.session() as s:
        legacy = User(display_name="Legacy", email="legacy@example.com", role="patient")
        s.add(legacy)
        s.flush()
        legacy_id = legacy.user_id
    profile = auth.patient_profile(legacy_id)
    assert len(profile["link_code"]) == 8 and profile["device_id"] is None
    assert auth.patient_profile(legacy_id)["link_code"] == profile["link_code"]
    for bad in (9999, None, True):
        with pytest.raises(NotFoundError):
            auth.patient_profile(bad)  # type: ignore[arg-type]
