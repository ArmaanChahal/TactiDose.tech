"""AuthService: accounts, login sessions, care links and access checks (ARCHITECTURE v2 §9).

Implements :class:`tactidose.core.interfaces.AuthServiceAPI`.

Accounts
    :meth:`AuthService.register` is self-service registration (honours ``allow_registration``);
    :meth:`AuthService.create_user` is the same without that check (CLI, demo seed). Emails are
    trimmed, lower-cased, validated and unique. Patients get an 8-character link code from an
    unambiguous alphabet (no 0/O, 1/I/L). When the configured device (``settings.device_id``)
    has no real patient yet - no device row, or its owner has no email (the legacy placeholder)
    or is not a patient account - it is bound to the new patient (:func:`bind_device_in_session`).

Sessions
    Bearer tokens from ``secrets.token_urlsafe(32)``; only their SHA-256 is stored. Sliding
    expiry: ``session_ttl_hours`` after the last request (``last_seen_at`` is written at most
    once a minute). Session and lockout times use the clock *without* its demo travel offset
    (:meth:`AuthService.now`), so demo time travel never logs anyone out; a frozen clock (tests)
    is honoured.

Login
    One message for unknown email, wrong password and disabled accounts. After
    :data:`MAX_FAILURES` failures for one email (counted before the password is checked, so
    parallel guesses cannot bypass it) further attempts are refused for :data:`LOCKOUT_SECONDS`
    (:class:`~tactidose.auth.errors.TooManyAttempts`). Linking with wrong codes is limited the
    same way per caregiver.

Access
    :meth:`can_view` / :meth:`can_edit` / :meth:`can_drop` / :meth:`permissions` implement the
    §9 permission matrix from ``care_links``; links are re-read on every call, so unlinking takes
    effect immediately.

Thread-safe: every call uses its own DB session; the in-memory lockout table has a lock.
Raises auth errors (401/403/429) and ``medication.errors`` (422/404/409). Database errors
propagate (the caller answers 5xx) - except the best-effort session refresh in :meth:`resolve`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
import threading
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from tactidose.auth import passwords
from tactidose.auth.errors import AuthError, PermissionDenied, TooManyAttempts
from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import AuthUser
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    AuthSession,
    CareLink,
    Compartment,
    Device,
    DoseEvent,
    DoseStatus,
    LabelScan,
    LogCategory,
    Medication,
    Role,
    User,
)
from tactidose.db.outbox import enqueue_adherence
from tactidose.db.session import Database
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError

log = logging.getLogger(__name__)

__all__ = [
    "LINK_CODE_ALPHABET",
    "LINK_CODE_LENGTH",
    "LOCKOUT_SECONDS",
    "MAX_FAILURES",
    "AuthService",
    "DeviceBinding",
    "bind_device_in_session",
    "clean_display_name",
    "clean_phone",
    "generate_link_code",
    "is_placeholder_owner",
    "is_unclaimed_owner",
    "normalize_email",
    "normalize_link_code",
    "parse_role",
    "token_hash",
    "user_to_dict",
]

#: No 0/O, 1/I/L: easy to read aloud and to type from a screen or a phone call.
LINK_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
LINK_CODE_LENGTH = 8
MAX_FAILURES = 5
LOCKOUT_SECONDS = 30
#: Failures older than this no longer count towards a lockout.
FAILURE_WINDOW = timedelta(minutes=15)
#: ``last_seen_at`` / sliding expiry are written at most this often per session.
SESSION_TOUCH_INTERVAL = timedelta(seconds=60)
PURGE_INTERVAL = timedelta(minutes=10)
#: Bounds the lockout table (it is keyed by whatever email strings clients send).
MAX_TRACKED_KEYS = 10_000

MAX_EMAIL_LENGTH = 254
MAX_NAME_LENGTH = 120
MAX_PHONE_LENGTH = 32
MAX_TOKEN_LENGTH = 512

MSG_INVALID_LOGIN = "The email or password is not correct."
MSG_LOCKED_NOW = (
    "The email or password is not correct. Too many failed attempts: "
    f"please wait {LOCKOUT_SECONDS} seconds and try again."
)
MSG_LOCKED = "Too many failed attempts. Please wait {seconds} seconds and try again."
MSG_LINK_LOCKED = "Too many wrong link codes. Please wait {seconds} seconds and try again."
MSG_DUPLICATE_EMAIL = "An account with this email address already exists. Please sign in instead."
MSG_REGISTRATION_OFF = (
    "New accounts cannot be created here. Please ask the person who set up TactiDose to create one."
)
MSG_NOT_CAREGIVER = "Only doctor or family accounts can link to a patient."
MSG_NO_PATIENT = "No patient account has that ID. Please check the patient ID."
MSG_BAD_CODE = "That link code does not match. Please check the code shown in the patient's portal."

_PATIENT = Role.PATIENT.value
_CAREGIVER_VALUES = (Role.DOCTOR.value, Role.FAMILY.value)
_OPEN_DOSE_STATUSES = (DoseStatus.SCHEDULED.value, DoseStatus.DUE.value, DoseStatus.HARDWARE_ERROR.value)

_LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PHONE_RE = re.compile(r"^[0-9+().\- ]+$")


# =========================================================================== input normalisation


def normalize_email(value: object) -> str:
    """Trimmed, lower-cased email address. Raises ``ValidationError`` if it is not plausible."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Please enter your email address.")
    email = unicodedata.normalize("NFKC", value).strip().lower()
    invalid = ValidationError("Please enter a valid email address, for example name@example.com.")
    if len(email) > MAX_EMAIL_LENGTH or email.count("@") != 1:
        raise invalid
    local, domain = email.split("@")
    labels = domain.split(".")
    if (
        not local
        or len(local) > 64
        or not _LOCAL_RE.match(local)
        or len(labels) < 2
        or not all(_LABEL_RE.match(label) for label in labels)
    ):
        raise invalid
    return email


def parse_role(value: object) -> Role:
    """``Role`` from an enum or a case-insensitive string. Raises ``ValidationError``."""
    if isinstance(value, Role):
        return value
    if isinstance(value, str):
        try:
            return Role(value.strip().lower())
        except ValueError:
            pass
    raise ValidationError("The account type must be patient, doctor or family.")


def clean_display_name(value: object) -> str:
    """Single-spaced name without control characters, 1..120 characters."""
    if not isinstance(value, str):
        raise ValidationError("Please enter your name.")
    text = "".join(ch for ch in unicodedata.normalize("NFKC", value)
                   if not unicodedata.category(ch).startswith("C") or ch in "\t\n\r")
    name = " ".join(text.split())
    if not name:
        raise ValidationError("Please enter your name.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"The name must be at most {MAX_NAME_LENGTH} characters long.")
    return name


def clean_phone(value: object) -> str | None:
    """Optional phone number: digits, spaces, ``+ - ( ) .``; blank means none."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("Please enter the phone number as text.")
    phone = " ".join(value.split())
    if not phone:
        return None
    digits = sum(ch.isdigit() for ch in phone)
    if len(phone) > MAX_PHONE_LENGTH or not phone.isascii() or not _PHONE_RE.match(phone) or digits < 3:
        raise ValidationError("A phone number may contain digits, spaces, +, -, ( and ).")
    return phone


def normalize_link_code(value: object) -> str:
    """Upper-case code without spaces or dashes (people often type ``abcd-efgh``)."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if not ch.isspace() and ch not in "-_").upper()[:64]


def generate_link_code() -> str:
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH))


def token_hash(token: str) -> str:
    """Hex SHA-256 of a session token (what ``auth_sessions.token_hash`` stores)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_token_hash(token: object) -> str | None:
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    try:
        return token_hash(token)
    except UnicodeError:
        return None


def _login_key(email: object) -> str:
    if not isinstance(email, str):
        return ""
    return unicodedata.normalize("NFKC", email).strip().lower()[:MAX_EMAIL_LENGTH]


def _codes_match(stored: str, provided: str) -> bool:
    expected = normalize_link_code(stored)
    return bool(expected) and hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _clean_user_agent(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch for ch in value if ch.isprintable()).split())
    return text[:255] or None


def _is_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _auth_user(row: User) -> AuthUser:
    return AuthUser(user_id=row.user_id, display_name=row.display_name, role=row.role, email=row.email)


def user_to_dict(user: User) -> dict[str, Any]:
    """The API ``User`` object (docs/API.md) for an ORM row. Never includes secrets."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "phone": user.phone,
        "created_at": _iso(user.created_at),
    }


# =========================================================================== device binding


def is_placeholder_owner(owner: User | None) -> bool:
    """The legacy default device user (no email, so nobody can sign in as it)."""
    return owner is not None and not owner.email


def is_unclaimed_owner(owner: User | None) -> bool:
    """True when a device has no *real* patient: no owner, a placeholder, or a non-patient account."""
    return owner is None or not owner.email or owner.role != _PATIENT


@dataclass(frozen=True)
class DeviceBinding:
    """Outcome of :func:`bind_device_in_session`."""

    device_id: str
    patient_id: int | None             # owner after the call
    previous_owner_id: int | None
    created: bool = False              # the device row was created
    changed: bool = False              # the owner changed (or the device was created)
    adopted: bool = False              # a placeholder's medications/doses moved to the new owner
    released_containers: int = 0       # containers cleared because they held someone else's medication
    cancelled_doses: int = 0           # open doses of the previous owner cancelled

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def bind_device_in_session(
    s: Session,
    settings: Settings,
    patient: User,
    *,
    now: datetime,
    device_id: str | None = None,
    force: bool = False,
    tz: tzinfo | None = None,
) -> DeviceBinding:
    """Bind ``device_id`` (default ``settings.device_id``) to ``patient`` inside the caller's session.

    * No device row: it is created (with ``settings.num_slots`` empty containers, the default
      cooldown and auto-drop setting).
    * Owner is not a real patient (:func:`is_unclaimed_owner`) or ``force``: the owner changes
      (compare-and-set, so two concurrent registrations cannot both win). A placeholder owner's
      medication setup (medications, label scans, this device's dose events) moves to the new
      patient. Otherwise the previous owner's setup is released, failing closed: containers holding
      a medication that does not belong to the new patient are cleared (no medication, 0 pills) and
      the previous owner's open doses on this device are cancelled.
    * Owned by another real patient and not ``force``: nothing changes.

    Flushes, never commits. ``now`` should be the domain clock (``Clock.now()``).
    """
    did = device_id or settings.device_id
    dev = s.get(Device, did)
    if dev is None:
        dev = Device(
            device_id=did,
            user_id=patient.user_id,
            name=settings.device_name,
            num_slots=settings.num_slots,
            manual_cooldown_minutes=settings.manual_cooldown_minutes,
            auto_drop_enabled=settings.auto_drop_enabled,
            created_at=now,
            updated_at=now,
        )
        s.add(dev)
        s.flush()
        _ensure_compartments(s, dev, settings, now)
        log_event(s, did, LogCategory.AUTH, "DEVICE_BOUND",
                  {"patient_id": patient.user_id, "previous_owner_id": None, "created": True}, at=now)
        log.info("device %s created for patient #%s", did, patient.user_id)
        return DeviceBinding(did, patient.user_id, None, created=True, changed=True)

    if dev.user_id == patient.user_id:
        _ensure_compartments(s, dev, settings, now)
        return DeviceBinding(did, patient.user_id, patient.user_id)

    owner = s.get(User, dev.user_id)
    if not (force or is_unclaimed_owner(owner)):
        return DeviceBinding(did, dev.user_id, dev.user_id)

    previous = dev.user_id
    result = s.execute(
        update(Device)
        .where(Device.device_id == did, Device.user_id == previous)
        .values(user_id=patient.user_id, updated_at=now)
    )
    if result.rowcount != 1:
        s.refresh(dev)
        return DeviceBinding(did, dev.user_id, dev.user_id)

    adopted = is_placeholder_owner(owner)
    released = cancelled = 0
    if adopted:
        _adopt_placeholder_setup(s, did, previous, patient.user_id)
    else:
        released, cancelled = _release_previous_setup(s, settings, did, previous, patient.user_id, now, tz)
    _ensure_compartments(s, dev, settings, now)
    log_event(s, did, LogCategory.AUTH, "DEVICE_BOUND", {
        "patient_id": patient.user_id, "previous_owner_id": previous, "created": False,
        "adopted": adopted, "forced": force, "released_containers": released, "cancelled_doses": cancelled,
    }, at=now)
    log.info("device %s bound to patient #%s (was #%s, adopted=%s)", did, patient.user_id, previous, adopted)
    return DeviceBinding(did, patient.user_id, previous, changed=True, adopted=adopted,
                         released_containers=released, cancelled_doses=cancelled)


def _ensure_compartments(s: Session, dev: Device, settings: Settings, now: datetime) -> int:
    existing = set(s.scalars(select(Compartment.slot_number).where(Compartment.device_id == dev.device_id)))
    added = 0
    for slot in range(settings.num_slots):
        if slot not in existing:
            s.add(Compartment(
                device_id=dev.device_id, slot_number=slot, active=True, pill_count=0,
                capacity=settings.default_container_capacity,
                low_stock_threshold=settings.default_low_stock_threshold, updated_at=now,
            ))
            added += 1
    if added:
        s.flush()
    return added


def _adopt_placeholder_setup(s: Session, device_id: str, old_owner: int, new_owner: int) -> None:
    s.execute(update(Medication).where(Medication.user_id == old_owner).values(user_id=new_owner))
    s.execute(update(LabelScan).where(LabelScan.user_id == old_owner).values(user_id=new_owner))
    s.execute(
        update(DoseEvent)
        .where(DoseEvent.user_id == old_owner, DoseEvent.device_id == device_id)
        .values(user_id=new_owner)
    )


def _release_previous_setup(
    s: Session, settings: Settings, device_id: str, old_owner: int, new_owner: int,
    now: datetime, tz: tzinfo | None,
) -> tuple[int, int]:
    foreign = s.scalars(
        select(Compartment)
        .join(Medication, Compartment.medication_id == Medication.medication_id)
        .where(Compartment.device_id == device_id, Medication.user_id != new_owner)
    ).all()
    for comp in foreign:
        comp.medication_id = None
        comp.pill_count = 0
        comp.updated_at = now
    events = s.scalars(
        select(DoseEvent).where(
            DoseEvent.device_id == device_id,
            DoseEvent.user_id == old_owner,
            DoseEvent.status.in_(_OPEN_DOSE_STATUSES),
        )
    ).all()
    salt = settings.analytics_salt.get_secret_value()
    for ev in events:
        ev.status = DoseStatus.CANCELLED.value
        ev.cancelled_at = now
        ev.next_attempt_at = None
        ev.review_note = "Device reassigned to another patient"
        enqueue_adherence(s, ev, salt=salt, tz=tz)
    s.flush()
    return len(foreign), len(events)


# =========================================================================== lockout bookkeeping


@dataclass
class _Attempts:
    failures: deque[datetime] = field(default_factory=deque)
    locked_until: datetime | None = None


# =========================================================================== service


class AuthService:
    """Accounts, sessions, care links and the §9 permission matrix (implements ``AuthServiceAPI``)."""

    def __init__(self, db: Database, settings: Settings, clock: Clock, bus: EventBus | None = None) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.bus = bus
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempts] = {}
        self._last_purge: datetime | None = None

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        """Security time (aware UTC): the clock minus its demo travel offset."""
        return self.clock.now() - self.clock.offset

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(hours=self.settings.session_ttl_hours)

    # ------------------------------------------------------------------ accounts
    def register(self, *, email: str, password: str, display_name: str, role: str,
                 phone: str | None = None) -> AuthUser:
        """Self-service registration (``POST /api/auth/register``). Does not create a session:
        call :meth:`start_session` (no second password hash) or :meth:`login`."""
        if not self.settings.allow_registration:
            raise PermissionDenied(MSG_REGISTRATION_OFF)
        return self.create_user(email=email, password=password, display_name=display_name,
                                role=role, phone=phone)

    def create_user(self, *, email: str, password: str, display_name: str, role: str | Role,
                    phone: str | None = None, bind_device: bool = True) -> AuthUser:
        """Create an account (ignores ``allow_registration``; used by the CLI and the demo seed)."""
        email_n = normalize_email(email)
        role_n = parse_role(role)
        name = clean_display_name(display_name)
        phone_n = clean_phone(phone)
        problem = passwords.password_problem(password)
        if problem:
            raise ValidationError(problem)
        pw_hash = passwords.hash_password(password)   # slow: outside the transaction
        now = self.now()
        try:
            with self.db.session() as s:
                if s.scalar(select(User.user_id).where(func.lower(User.email) == email_n)) is not None:
                    raise ConflictError(MSG_DUPLICATE_EMAIL)
                user = User(display_name=name, role=role_n.value, email=email_n, password_hash=pw_hash,
                            phone=phone_n, is_active=True, accessibility_preferences={},
                            voice_enabled=True, created_at=now)
                if role_n is Role.PATIENT:
                    user.link_code = self.new_link_code(s)
                s.add(user)
                s.flush()
                log_event(s, self.settings.device_id, LogCategory.AUTH, "USER_CREATED",
                          {"user_id": user.user_id, "role": user.role}, at=self.clock.now())
                created = _auth_user(user)
        except IntegrityError as exc:   # same email registered concurrently
            raise ConflictError(MSG_DUPLICATE_EMAIL) from exc
        log.info("created %s account #%s", created.role, created.user_id)
        if role_n is Role.PATIENT and bind_device:
            try:
                self.bind_device(created.user_id)
            except Exception:   # the account exists; the binding can be redone (CLI bind-device)
                log.warning("could not bind device %s to new patient #%s",
                            self.settings.device_id, created.user_id, exc_info=True)
            if self.settings.effective_shared_device:
                self._ensure_shared_device(created.user_id)
        return created

    def _ensure_shared_device(self, patient_id: int) -> None:
        """Shared dispenser (one ESP32 for everyone): give a new patient their own device record."""
        from tactidose.medication.compartments import ensure_patient_device

        try:
            with self.db.session() as s:
                patient = s.get(User, patient_id)
                if patient is not None:
                    dev, made = ensure_patient_device(s, self.settings, patient, now=self.clock.now())
                    if made:
                        log.info("patient #%s gets device record %s on the shared dispenser", patient_id, dev.device_id)
        except Exception:   # the account exists; the device can be added later
            log.warning("could not create a shared-dispenser device for patient #%s", patient_id, exc_info=True)

    def new_link_code(self, s: Session) -> str:
        """A link code not used by any other account (call inside a session)."""
        for _ in range(20):
            code = generate_link_code()
            if s.scalar(select(User.user_id).where(User.link_code == code)) is None:
                return code
        raise ConflictError("Could not create a unique link code. Please try again.")

    def get_user(self, user_id: int) -> AuthUser | None:
        if not _is_id(user_id):
            return None
        with self.db.session() as s:
            row = s.get(User, user_id)
            return _auth_user(row) if row is not None else None

    def get_user_by_email(self, email: str) -> AuthUser | None:
        try:
            key = normalize_email(email)
        except ValidationError:
            return None
        with self.db.session() as s:
            row = s.scalars(select(User).where(func.lower(User.email) == key)).first()
            return _auth_user(row) if row is not None else None

    def user_to_dict(self, user: AuthUser | User | int) -> dict[str, Any]:
        """API ``User`` object for an ``AuthUser``, an ORM row or a user id."""
        if isinstance(user, User):
            return user_to_dict(user)
        uid = user if _is_id(user) else getattr(user, "user_id", None)
        row = None
        if _is_id(uid):
            with self.db.session() as s:
                row = s.get(User, uid)
        if row is not None:
            return user_to_dict(row)
        if isinstance(user, AuthUser):
            return {"user_id": user.user_id, "email": user.email, "display_name": user.display_name,
                    "role": user.role, "phone": None, "created_at": None}
        raise NotFoundError("No account has that ID.")

    def patient_profile(self, patient_id: int) -> dict[str, Any]:
        """``{patient_id, link_code, device_id}`` for the patient portal (``GET /api/auth/me``).
        Assigns a link code to legacy patients that have none."""
        with self.db.session() as s:
            row = s.get(User, patient_id) if _is_id(patient_id) else None
            if row is None or row.role != _PATIENT:
                raise NotFoundError(MSG_NO_PATIENT)
            if not row.link_code:
                row.link_code = self.new_link_code(s)
            devices = list(s.scalars(select(Device.device_id).where(Device.user_id == patient_id)
                                     .order_by(Device.device_id)))
            device_id = self.settings.device_id if self.settings.device_id in devices else (
                devices[0] if devices else None)
            return {"patient_id": row.user_id, "link_code": row.link_code, "device_id": device_id}

    def bind_device(self, patient_id: int, *, device_id: str | None = None, force: bool = False) -> dict[str, Any]:
        """Bind the configured device to ``patient_id`` (see :func:`bind_device_in_session`).
        ``force`` takes it from another real patient (CLI ``bind-device``, demo reset)."""
        for attempt in range(2):
            try:
                with self.db.session() as s:
                    patient = s.get(User, patient_id) if _is_id(patient_id) else None
                    if patient is None:
                        raise NotFoundError("No account has that ID.")
                    if patient.role != _PATIENT:
                        raise ValidationError("Only patient accounts can have a device.")
                    if not patient.is_active:
                        raise ValidationError("That patient account is disabled.")
                    binding = bind_device_in_session(s, self.settings, patient, now=self.clock.now(),
                                                     device_id=device_id, force=force, tz=self.clock.tz)
                break
            except IntegrityError:
                if attempt:
                    raise
                log.debug("device %s created concurrently; retrying the binding", device_id, exc_info=True)
        if binding.changed:
            self._publish_status(binding.patient_id, "device_bound")
            if binding.previous_owner_id and binding.previous_owner_id != binding.patient_id:
                self._publish_status(binding.previous_owner_id, "device_unbound")
        return binding.to_dict()

    # ------------------------------------------------------------------ sessions
    def login(self, email: str, password: str, *, user_agent: str | None = None) -> tuple[AuthUser, str]:
        """Check the credentials and open a session: ``(user, token)``.

        Raises ``AuthError`` (same message for unknown email / wrong password / disabled account)
        or ``TooManyAttempts`` (locked for :data:`LOCKOUT_SECONDS`)."""
        key_email = _login_key(email)
        key = f"login:{key_email}"
        now = self.now()
        self._begin_attempt(key, now, MSG_LOCKED)
        try:
            row: User | None = None
            if key_email:
                with self.db.session() as s:
                    row = s.scalars(select(User).where(func.lower(User.email) == key_email)).first()
            stored = row.password_hash if row is not None else None
            ok = passwords.verify_password(password, stored) if stored else passwords.dummy_verify(password)
            ok = ok and row is not None and bool(row.is_active)
        except Exception:
            self._abort_attempt(key, now)
            raise
        if not ok:
            lock_s = self._attempt_failed(key, now)
            log.info("sign-in failed (%s)", f"user #{row.user_id}" if row is not None else "unknown email")
            if lock_s:
                raise TooManyAttempts(MSG_LOCKED_NOW, retry_after_s=lock_s)
            raise AuthError(MSG_INVALID_LOGIN)
        self._attempt_succeeded(key)
        new_hash: str | None = None
        if passwords.needs_rehash(stored):
            try:
                new_hash = passwords.hash_password(password)
            except ValueError:      # e.g. a legacy password shorter than today's minimum
                new_hash = None
        with self.db.session() as s:
            user = s.get(User, row.user_id)
            if user is None or not user.is_active:
                raise AuthError(MSG_INVALID_LOGIN)
            if new_hash:
                user.password_hash = new_hash
            token = self._insert_session(s, user, user_agent, now)
            auth_user = _auth_user(user)
        self._maybe_purge(now)
        log.info("user #%s signed in", auth_user.user_id)
        return auth_user, token

    def start_session(self, user: AuthUser | int, *, user_agent: str | None = None) -> str:
        """Open a session without checking a password (right after :meth:`register`)."""
        uid = user if _is_id(user) else getattr(user, "user_id", None)
        now = self.now()
        with self.db.session() as s:
            row = s.get(User, uid) if _is_id(uid) else None
            if row is None or not row.is_active or not row.email:
                raise AuthError("This account cannot sign in.")
            token = self._insert_session(s, row, user_agent, now)
        self._maybe_purge(now)
        return token

    def resolve(self, token: str) -> AuthUser | None:
        """The signed-in user for a bearer token, or ``None`` (unknown, expired, revoked, disabled).
        Extends the session (sliding expiry); a failed extension is logged, never raised."""
        digest = _safe_token_hash(token)
        if digest is None:
            return None
        now = self.now()
        with self.db.session() as s:
            row = s.execute(
                select(AuthSession.session_id, AuthSession.expires_at, AuthSession.revoked_at,
                       AuthSession.last_seen_at, User)
                .join(User, User.user_id == AuthSession.user_id)
                .where(AuthSession.token_hash == digest)
            ).first()
        if row is None:
            return None
        session_id, expires_at, revoked_at, last_seen, user = row
        if revoked_at is not None or expires_at <= now or not user.is_active:
            return None
        if last_seen is None or now - last_seen >= SESSION_TOUCH_INTERVAL:
            self._touch(session_id, now)
        return _auth_user(user)

    def logout(self, token: str) -> None:
        """Revoke the session (unknown tokens are ignored)."""
        digest = _safe_token_hash(token)
        if digest is None:
            return
        with self.db.session() as s:
            s.execute(update(AuthSession)
                      .where(AuthSession.token_hash == digest, AuthSession.revoked_at.is_(None))
                      .values(revoked_at=self.now()))

    def revoke_sessions(self, user_id: int) -> int:
        """Sign a user out everywhere; returns the number of sessions revoked."""
        with self.db.session() as s:
            result = s.execute(update(AuthSession)
                               .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
                               .values(revoked_at=self.now()))
            return int(result.rowcount or 0)

    def purge_expired_sessions(self) -> int:
        """Delete expired and revoked sessions; returns how many were removed."""
        now = self.now()
        with self.db.session() as s:
            result = s.execute(delete(AuthSession).where(
                or_(AuthSession.expires_at <= now, AuthSession.revoked_at.is_not(None))))
            removed = int(result.rowcount or 0)
        self._last_purge = now
        if removed:
            log.debug("purged %d expired/revoked session(s)", removed)
        return removed

    def clear_lockouts(self) -> None:
        with self._lock:
            self._attempts.clear()

    # ------------------------------------------------------------------ care links
    def link_patient(self, *, caregiver: AuthUser, patient_id: int, link_code: str) -> dict[str, Any]:
        """Link a doctor/family account to a patient with the patient ID + link code.

        Returns ``{patient_id, display_name, relationship}`` (idempotent when already linked).
        Raises ``PermissionDenied`` (not a caregiver / wrong code), ``NotFoundError`` (no such
        patient) or ``TooManyAttempts`` (5 wrong tries -> 30 s pause)."""
        self._require_caregiver(caregiver)
        key = f"link:{caregiver.user_id}"
        now = self.now()
        self._begin_attempt(key, now, MSG_LINK_LOCKED)
        code = normalize_link_code(link_code)
        failure: Exception | None = None
        view: dict[str, Any] | None = None
        created = False
        try:
            with self.db.session() as s:
                carer = s.get(User, caregiver.user_id)
                if carer is None or not carer.is_active or carer.role not in _CAREGIVER_VALUES:
                    raise PermissionDenied(MSG_NOT_CAREGIVER)
                patient = s.get(User, patient_id) if _is_id(patient_id) else None
                if patient is None or patient.role != _PATIENT or not patient.is_active:
                    failure = NotFoundError(MSG_NO_PATIENT)
                elif not patient.link_code or not _codes_match(patient.link_code, code):
                    failure = PermissionDenied(MSG_BAD_CODE)
                else:
                    view, created = self._ensure_link(s, carer, patient, now)
        except IntegrityError:          # linked concurrently by another request
            view, created = self._link_view(caregiver.user_id, patient_id), False
        except Exception:
            self._abort_attempt(key, now)
            raise
        if failure is not None or view is None:
            lock_s = self._attempt_failed(key, now)
            log.info("link attempt by #%s for patient %r failed: %s", caregiver.user_id, patient_id,
                     type(failure).__name__)
            if lock_s:
                raise TooManyAttempts(MSG_LINK_LOCKED.format(seconds=lock_s), retry_after_s=lock_s)
            raise failure or NotFoundError(MSG_NO_PATIENT)
        self._attempt_succeeded(key)
        if created:
            self._publish_status(patient_id, "care_link_added")
        return view

    def admin_link(self, *, caregiver_id: int, patient_id: int) -> dict[str, Any]:
        """Operator link without a code (CLI ``link``). Validates both account types."""
        now = self.now()
        try:
            with self.db.session() as s:
                carer = s.get(User, caregiver_id) if _is_id(caregiver_id) else None
                if carer is None:
                    raise NotFoundError("No account has that ID.")
                if carer.role not in _CAREGIVER_VALUES:
                    raise ValidationError("Only doctor or family accounts can be linked to a patient.")
                patient = s.get(User, patient_id) if _is_id(patient_id) else None
                if patient is None or patient.role != _PATIENT:
                    raise NotFoundError(MSG_NO_PATIENT)
                view, created = self._ensure_link(s, carer, patient, now)
        except IntegrityError:
            return self._link_view(caregiver_id, patient_id)
        if created:
            self._publish_status(patient_id, "care_link_added")
        return view

    def unlink_patient(self, *, caregiver: AuthUser, patient_id: int) -> None:
        """Remove the caller's link to ``patient_id``; ``NotFoundError`` if there is none."""
        self._require_caregiver(caregiver)
        with self.db.session() as s:
            result = s.execute(delete(CareLink).where(CareLink.caregiver_id == caregiver.user_id,
                                                      CareLink.patient_id == patient_id))
            if not result.rowcount:
                raise NotFoundError("You are not linked to that patient.")
            log_event(s, self.settings.device_id, LogCategory.AUTH, "CARE_LINK_REMOVED",
                      {"caregiver_id": caregiver.user_id, "patient_id": patient_id}, at=self.clock.now())
        log.info("caregiver #%s unlinked from patient #%s", caregiver.user_id, patient_id)
        self._publish_status(patient_id, "care_link_removed")

    # ------------------------------------------------------------------ access (§9 matrix)
    def can_view(self, user: AuthUser | None, patient_id: int) -> bool:
        """Patient themself, or a doctor/family account linked to the patient."""
        if user is None or not _is_id(patient_id):
            return False
        if user.is_patient:
            return user.user_id == patient_id
        if user.is_caregiver:
            return self._has_link(user.user_id, patient_id)
        return False

    def can_edit(self, user: AuthUser | None, patient_id: int) -> bool:
        """Schedules, cooldown, containers/refills, medications, reviews, skips: linked caregivers only."""
        return user is not None and user.is_caregiver and _is_id(patient_id) and self._has_link(
            user.user_id, patient_id)

    def can_drop(self, user: AuthUser | None, patient_id: int) -> bool:
        """Manual drop and agent chat: only the patient themself."""
        return user is not None and user.is_patient and _is_id(patient_id) and user.user_id == patient_id

    def permissions(self, user: AuthUser | None, patient_id: int) -> dict[str, bool]:
        """Every §9 matrix row for ``user`` on ``patient_id`` (e.g. for the portals' UI)."""
        view = self.can_view(user, patient_id)
        edit = view and user is not None and user.is_caregiver
        own = self.can_drop(user, patient_id)
        return {
            "view": view,
            "drop": own,
            "chat": own,
            "edit": edit,
            "report": view,
            "device_home": edit,
            "device_reconnect": edit,
            "device_stop": view,
        }

    def linked_patient_ids(self, user: AuthUser | None) -> list[int]:
        """Patients ``user`` may view: ``[own id]`` for a patient, linked patients for a caregiver."""
        if user is None:
            return []
        if user.is_patient:
            return [user.user_id]
        if not user.is_caregiver:
            return []
        patient = aliased(User)
        with self.db.session() as s:
            return list(s.scalars(
                select(CareLink.patient_id)
                .join(patient, patient.user_id == CareLink.patient_id)
                .where(CareLink.caregiver_id == user.user_id, patient.role == _PATIENT,
                       patient.is_active.is_(True))
                .order_by(CareLink.patient_id)
            ))

    def caregiver_ids(self, patient_id: int) -> list[int]:
        """Active doctor/family accounts linked to ``patient_id`` (notification recipients)."""
        return [c["user_id"] for c in self.caregivers(patient_id)]

    def caregivers(self, patient_id: int, *, relationship: str | None = None) -> list[dict[str, Any]]:
        """``[{user_id, display_name, email, role, relationship}]`` linked to the patient, e.g.
        ``relationship="doctor"`` for the report e-mail recipients."""
        if not _is_id(patient_id):
            return []
        query = (
            select(User, CareLink.relationship_kind)
            .join(CareLink, CareLink.caregiver_id == User.user_id)
            .where(CareLink.patient_id == patient_id, User.is_active.is_(True),
                   User.role.in_(_CAREGIVER_VALUES))
            .order_by(User.user_id)
        )
        if relationship:
            query = query.where(CareLink.relationship_kind == relationship)
        with self.db.session() as s:
            return [
                {"user_id": u.user_id, "display_name": u.display_name, "email": u.email,
                 "role": u.role, "relationship": rel}
                for u, rel in s.execute(query).all()
            ]

    def care_patients(self, caregiver: AuthUser | None) -> list[dict[str, Any]]:
        """``[{patient_id, display_name, relationship}]`` for a caregiver (``[]`` otherwise)."""
        if caregiver is None or not caregiver.is_caregiver:
            return []
        with self.db.session() as s:
            rows = s.execute(
                select(User.user_id, User.display_name, CareLink.relationship_kind)
                .join(CareLink, CareLink.patient_id == User.user_id)
                .where(CareLink.caregiver_id == caregiver.user_id, User.role == _PATIENT,
                       User.is_active.is_(True))
                .order_by(func.lower(User.display_name), User.user_id)
            ).all()
        return [{"patient_id": pid, "display_name": name, "relationship": rel} for pid, name, rel in rows]

    # ------------------------------------------------------------------ internals: links
    def _require_caregiver(self, user: AuthUser | None) -> None:
        if user is None or not user.is_caregiver:
            raise PermissionDenied(MSG_NOT_CAREGIVER)

    def _ensure_link(self, s: Session, carer: User, patient: User, now: datetime) -> tuple[dict[str, Any], bool]:
        link = s.scalars(select(CareLink).where(CareLink.caregiver_id == carer.user_id,
                                                CareLink.patient_id == patient.user_id)).first()
        created = link is None
        if link is None:
            link = CareLink(caregiver_id=carer.user_id, patient_id=patient.user_id,
                            relationship_kind=carer.role, created_at=now)
            s.add(link)
            s.flush()
            log_event(s, self.settings.device_id, LogCategory.AUTH, "CARE_LINK_ADDED",
                      {"caregiver_id": carer.user_id, "patient_id": patient.user_id,
                       "relationship": carer.role}, at=self.clock.now())
            log.info("caregiver #%s (%s) linked to patient #%s", carer.user_id, carer.role, patient.user_id)
        view = {"patient_id": patient.user_id, "display_name": patient.display_name,
                "relationship": link.relationship_kind}
        return view, created

    def _link_view(self, caregiver_id: int, patient_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            row = s.execute(
                select(User.display_name, CareLink.relationship_kind)
                .join(CareLink, CareLink.patient_id == User.user_id)
                .where(CareLink.caregiver_id == caregiver_id, CareLink.patient_id == patient_id)
            ).first()
        if row is None:
            raise NotFoundError(MSG_NO_PATIENT)
        return {"patient_id": patient_id, "display_name": row[0], "relationship": row[1]}

    def _has_link(self, caregiver_id: int, patient_id: int) -> bool:
        carer = aliased(User)
        patient = aliased(User)
        with self.db.session() as s:
            return s.scalar(
                select(CareLink.link_id)
                .join(carer, carer.user_id == CareLink.caregiver_id)
                .join(patient, patient.user_id == CareLink.patient_id)
                .where(CareLink.caregiver_id == caregiver_id, CareLink.patient_id == patient_id,
                       carer.is_active.is_(True), carer.role.in_(_CAREGIVER_VALUES),
                       patient.role == _PATIENT)
                .limit(1)
            ) is not None

    # ------------------------------------------------------------------ internals: sessions
    def _insert_session(self, s: Session, user: User, user_agent: object, now: datetime) -> str:
        token = secrets.token_urlsafe(32)
        s.add(AuthSession(token_hash=token_hash(token), user_id=user.user_id, created_at=now,
                          expires_at=now + self.session_ttl, last_seen_at=now,
                          user_agent=_clean_user_agent(user_agent)))
        user.last_login_at = now
        return token

    def _touch(self, session_id: int, now: datetime) -> None:
        try:
            with self.db.session() as s:
                s.execute(update(AuthSession)
                          .where(AuthSession.session_id == session_id, AuthSession.revoked_at.is_(None))
                          .values(last_seen_at=now, expires_at=now + self.session_ttl))
        except SQLAlchemyError:
            log.warning("could not extend session %s", session_id, exc_info=True)

    def _maybe_purge(self, now: datetime) -> None:
        last = self._last_purge
        if last is not None and timedelta(0) <= now - last < PURGE_INTERVAL:
            return
        try:
            self.purge_expired_sessions()
        except SQLAlchemyError:
            log.warning("could not purge expired sessions", exc_info=True)

    # ------------------------------------------------------------------ internals: lockout
    def _begin_attempt(self, key: str, now: datetime, locked_message: str) -> None:
        """Count an attempt before the secret is checked, so parallel guesses share the limit."""
        with self._lock:
            entry = self._attempts.get(key)
            if entry is None:
                if len(self._attempts) >= MAX_TRACKED_KEYS:
                    self._attempts.pop(next(iter(self._attempts)))
                entry = self._attempts[key] = _Attempts()
            if entry.locked_until is not None:
                if now < entry.locked_until:
                    wait = max(1, math.ceil((entry.locked_until - now).total_seconds()))
                    raise TooManyAttempts(locked_message.format(seconds=wait), retry_after_s=wait)
                entry.locked_until = None
            while entry.failures and entry.failures[0] <= now - FAILURE_WINDOW:
                entry.failures.popleft()
            if len(entry.failures) >= MAX_FAILURES:
                entry.failures.clear()
                entry.locked_until = now + timedelta(seconds=LOCKOUT_SECONDS)
                raise TooManyAttempts(locked_message.format(seconds=LOCKOUT_SECONDS),
                                      retry_after_s=LOCKOUT_SECONDS)
            entry.failures.append(now)

    def _attempt_failed(self, key: str, now: datetime) -> int | None:
        """Lock the key once :data:`MAX_FAILURES` attempts failed; returns the lock length."""
        with self._lock:
            entry = self._attempts.get(key)
            if entry is None or entry.locked_until is not None or len(entry.failures) < MAX_FAILURES:
                return None
            entry.failures.clear()
            entry.locked_until = now + timedelta(seconds=LOCKOUT_SECONDS)
        log.warning("too many failed attempts (%s): locked for %d s", key.split(":", 1)[0], LOCKOUT_SECONDS)
        return LOCKOUT_SECONDS

    def _attempt_succeeded(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def _abort_attempt(self, key: str, now: datetime) -> None:
        """An unexpected error (e.g. database down) is not a wrong guess."""
        with self._lock:
            entry = self._attempts.get(key)
            if entry is not None:
                try:
                    entry.failures.remove(now)
                except ValueError:
                    pass

    # ------------------------------------------------------------------ internals: events
    def _publish_status(self, patient_id: int | None, reason: str) -> None:
        if self.bus is not None and patient_id is not None:
            self.bus.publish(Topic.PATIENT_STATUS, {"patient_id": patient_id, "reason": reason})
