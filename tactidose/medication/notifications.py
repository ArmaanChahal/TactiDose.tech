"""In-app notifications per recipient (ARCHITECTURE v2 §10) — ``NotificationService``.

Implements ``core.interfaces.NotificationServiceAPI``:

* :meth:`NotificationService.notify` stores one ``notifications`` row per recipient in its own
  transaction and publishes ``Topic.NOTIFICATION`` once per recipient after the commit (the
  SSE endpoint forwards each event only to its ``user_id``).
* :meth:`NotificationService.stage` + :meth:`NotificationService.publish` do the same inside a
  caller's transaction, so a notification commits together with the state change it describes
  (``DropService`` and ``Scheduler`` use this; ARCHITECTURE §5 "same transaction").
* Recipients: the patient (``to_patient``) and/or the doctor/family accounts linked to the
  patient through ``care_links`` (``to_caregivers``). ``user_ids`` narrows delivery to explicit
  accounts (e.g. a report's creator) but never reaches anyone outside {patient} ∪ linked
  caregivers (least privilege). Inactive accounts receive nothing.
* :meth:`list_for_user` / :meth:`mark_read` only touch the user's own rows, and hide rows about
  a patient the user is no longer linked to.

Audience policy per kind (applied by the callers; documented here for reference)::

    PILL_DROPPED                      patient; caregivers if settings.notify_caregivers_on_drop
    DROP_FAILED / DROP_UNCERTAIN /
    DEVICE_ALERT / LOW_STOCK / EMPTY /
    MISSED_DOSE / HEALTH_CONCERN      patient + caregivers
    REPORT_READY / REPORT_SENT        the report's creator (user_ids=[creator])
    DROP_DENIED                       shown inline by the caller, never stored

The module also holds the small wording helpers (``clock_label``, ``duration_label``) used for
deterministic, user-facing sentences across the medication package.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.db.models import CAREGIVER_ROLES, CareLink, Notification, NotificationKind, User
from tactidose.db.session import Database
from tactidose.medication.errors import ValidationError

log = logging.getLogger(__name__)

__all__ = [
    "KIND_AUDIENCE",
    "MAX_LIMIT",
    "NotificationService",
    "PendingNotifications",
    "clock_label",
    "duration_label",
    "notification_to_dict",
    "plural",
]

MAX_LIMIT = 200
_MAX_TITLE = 200
_KINDS = frozenset(k.value for k in NotificationKind)
_CAREGIVER_ROLE_VALUES = tuple(r.value for r in CAREGIVER_ROLES)

#: Default audience per kind: (to_patient, to_caregivers). ``None`` = decided by a setting / caller.
KIND_AUDIENCE: dict[str, tuple[bool, bool | None]] = {
    NotificationKind.PILL_DROPPED.value: (True, None),       # caregivers: settings.notify_caregivers_on_drop
    NotificationKind.DROP_FAILED.value: (True, True),
    NotificationKind.DROP_UNCERTAIN.value: (True, True),
    NotificationKind.DEVICE_ALERT.value: (True, True),
    NotificationKind.LOW_STOCK.value: (True, True),
    NotificationKind.EMPTY.value: (True, True),
    NotificationKind.MISSED_DOSE.value: (True, True),
    NotificationKind.HEALTH_CONCERN.value: (True, True),     # emergency/severe wording (guided demo)
    NotificationKind.REPORT_READY.value: (False, False),      # explicit user_ids (the creator)
    NotificationKind.REPORT_SENT.value: (False, False),
    NotificationKind.DROP_DENIED.value: (False, False),       # never stored
}


# --------------------------------------------------------------------------- wording helpers


def clock_label(dt_local: datetime) -> str:
    """``"8:05 AM"`` (portable: no platform-specific strftime flags)."""
    hour = dt_local.hour % 12 or 12
    return f"{hour}:{dt_local.minute:02d} {'AM' if dt_local.hour < 12 else 'PM'}"


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def duration_label(seconds: float) -> str:
    """``"45 minutes"``, ``"1 hour 5 minutes"``, ``"less than a minute"`` (rounded up to minutes)."""
    total = max(0, int(seconds))
    if total < 60:
        return "less than a minute"
    minutes = -(-total // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{plural(hours, 'hour')} {plural(minutes, 'minute')}"
    if hours:
        return plural(hours, "hour")
    return plural(minutes, "minute")


# --------------------------------------------------------------------------- serialisation


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def notification_to_dict(n: Notification) -> dict[str, Any]:
    """API.md ``Notification`` shape plus the recipient ``user_id`` (used for SSE filtering)."""
    return {
        "notification_id": n.notification_id,
        "user_id": n.user_id,
        "patient_id": n.patient_id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body or "",
        "data": dict(n.data or {}),
        "created_at": _iso(n.created_at),
        "read_at": _iso(n.read_at),
    }


def _check_kind(kind: object) -> str:
    value = kind.value if isinstance(kind, NotificationKind) else kind
    if not isinstance(value, str) or value not in _KINDS:
        raise ValidationError(f"Unknown notification kind {kind!r}.")
    if value == NotificationKind.DROP_DENIED.value:
        raise ValidationError("Denied drops are shown inline and never stored as notifications.")
    return value


def _check_text(value: object, field: str, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"Notification {field} must be non-empty text.")
    return value.strip()


# --------------------------------------------------------------------------- service


class NotificationService:
    """Stores and publishes notifications. Thread-safe (no shared mutable state)."""

    def __init__(self, db: Database, settings: Settings, clock: Clock, bus: EventBus | None = None) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.bus = bus

    # ------------------------------------------------------------------ NotificationServiceAPI
    def notify(
        self,
        *,
        patient_id: int,
        kind: str,
        title: str,
        body: str = "",
        data: dict[str, Any] | None = None,
        to_patient: bool = True,
        to_caregivers: bool = True,
        user_ids: Iterable[int] | None = None,
    ) -> list[int]:
        """Store one row per recipient and publish ``Topic.NOTIFICATION`` for each; returns ids.

        ``user_ids`` (extension) = deliver only to these accounts, filtered to the patient and
        the caregivers linked to them. Invalid input raises ``ValidationError``; database errors
        propagate to the caller.
        """
        with self.db.session() as s:
            views = self.stage(
                s, patient_id=patient_id, kind=kind, title=title, body=body, data=data,
                to_patient=to_patient, to_caregivers=to_caregivers, user_ids=user_ids,
            )
        self.publish(views)
        return [v["notification_id"] for v in views]

    def stage(
        self,
        session: Session,
        *,
        patient_id: int,
        kind: str,
        title: str,
        body: str = "",
        data: dict[str, Any] | None = None,
        to_patient: bool = True,
        to_caregivers: bool = True,
        user_ids: Iterable[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Add the rows inside ``session`` (no commit). Returns views for :meth:`publish`,
        which the caller must call only after its transaction committed."""
        kind_value = _check_kind(kind)
        title_text = _check_text(title, "title", required=True)[:_MAX_TITLE]
        body_text = _check_text(body, "body", required=False)
        if data is not None and not isinstance(data, dict):
            raise ValidationError("Notification data must be an object.")
        recipients = self.recipients(
            session, patient_id, to_patient=to_patient, to_caregivers=to_caregivers, user_ids=user_ids
        )
        if not recipients:
            log.debug("notification %s for patient %s has no recipients", kind_value, patient_id)
            return []
        now = self.clock.now()
        rows = [
            Notification(user_id=uid, patient_id=patient_id, kind=kind_value, title=title_text,
                         body=body_text, data=dict(data or {}), created_at=now)
            for uid in recipients
        ]
        session.add_all(rows)
        session.flush()
        return [notification_to_dict(r) for r in rows]

    def publish(self, views: Iterable[dict[str, Any]]) -> None:
        """Publish ``Topic.NOTIFICATION`` for staged rows (after the caller's commit)."""
        if self.bus is None:
            return
        for view in views:
            self.bus.publish(Topic.NOTIFICATION, dict(view))

    def recipients(
        self,
        session: Session,
        patient_id: int,
        *,
        to_patient: bool = True,
        to_caregivers: bool = True,
        user_ids: Iterable[int] | None = None,
    ) -> list[int]:
        """Active recipient ids, patient first, then caregivers by id (no duplicates)."""
        patient = session.get(User, patient_id) if isinstance(patient_id, int) and not isinstance(
            patient_id, bool) else None
        if patient is None:
            return []
        caregivers = self.caregiver_ids(session, patient_id)
        patient_ok = bool(patient.is_active)
        if user_ids is not None:
            permitted = set(caregivers) | ({patient_id} if patient_ok else set())
            wanted = list(dict.fromkeys(u for u in user_ids if isinstance(u, int) and not isinstance(u, bool)))
            refused = [u for u in wanted if u not in permitted]
            if refused:
                log.warning("notification recipients %s are not linked to patient %s; skipped", refused, patient_id)
            return [u for u in wanted if u in permitted]
        out: list[int] = []
        if to_patient and patient_ok:
            out.append(patient_id)
        if to_caregivers:
            out.extend(c for c in caregivers if c not in out)
        return out

    @staticmethod
    def caregiver_ids(session: Session, patient_id: int) -> list[int]:
        """Active doctor/family accounts linked to ``patient_id`` (ordered by id)."""
        return list(session.scalars(
            select(User.user_id)
            .join(CareLink, CareLink.caregiver_id == User.user_id)
            .where(
                CareLink.patient_id == patient_id,
                User.is_active.is_(True),
                User.role.in_(_CAREGIVER_ROLE_VALUES),
            )
            .order_by(User.user_id)
        ).all())

    # ------------------------------------------------------------------ reading
    def list_for_user(
        self,
        user_id: int,
        *,
        unread_only: bool = False,
        limit: int = 50,
        patient_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """The user's own notifications, newest first (``limit`` clamped to 1..200)."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError("limit must be a whole number.")
        limit = max(1, min(MAX_LIMIT, limit))
        with self.db.session() as s:
            q = self._own_rows(s, user_id)
            if q is None:
                return []
            if unread_only:
                q = q.where(Notification.read_at.is_(None))
            if patient_id is not None:
                q = q.where(Notification.patient_id == patient_id)
            rows = s.scalars(
                q.order_by(Notification.created_at.desc(), Notification.notification_id.desc()).limit(limit)
            ).all()
            return [notification_to_dict(r) for r in rows]

    def unread_count(self, user_id: int, *, patient_id: int | None = None) -> int:
        """Unread notifications of ``user_id`` (optionally about one patient) — CarePatient.unread_alerts."""
        with self.db.session() as s:
            q = self._own_rows(s, user_id)
            if q is None:
                return 0
            q = q.where(Notification.read_at.is_(None))
            if patient_id is not None:
                q = q.where(Notification.patient_id == patient_id)
            return int(s.scalar(select(func.count()).select_from(q.subquery())) or 0)

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int:
        """Mark the user's unread notifications read (all, or only ``ids``). Returns the count."""
        if ids is not None:
            if not isinstance(ids, (list, tuple, set)) or any(
                isinstance(i, bool) or not isinstance(i, int) for i in ids
            ):
                raise ValidationError("ids must be a list of notification ids.")
            if not ids:
                return 0
        now = self.clock.now()
        with self.db.session() as s:
            stmt = update(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
            if ids is not None:
                stmt = stmt.where(Notification.notification_id.in_(list(ids)))
            result = s.execute(stmt.values(read_at=now).execution_options(synchronize_session=False))
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _own_rows(s: Session, user_id: int):  # noqa: ANN205 - SQLAlchemy Select
        """Rows addressed to ``user_id`` about patients the user may still see (None = no such user)."""
        user = s.get(User, user_id) if isinstance(user_id, int) and not isinstance(user_id, bool) else None
        if user is None:
            return None
        linked = select(CareLink.patient_id).where(CareLink.caregiver_id == user_id)
        return select(Notification).where(
            Notification.user_id == user_id,
            or_(Notification.patient_id == user_id, Notification.patient_id.in_(linked)),
        )


# --------------------------------------------------------------------------- transactional collector


class PendingNotifications:
    """Collects notifications inside a caller's transaction; delivers them after the commit.

    With a :class:`NotificationService` (anything with ``stage``/``publish``) rows are written in
    the caller's session — they commit or roll back together with the state change — and
    ``Topic.NOTIFICATION`` is published by :meth:`deliver`. Any other ``NotificationServiceAPI``
    implementation gets ``notify(...)`` calls from :meth:`deliver` instead (best effort, after
    the commit). Without a service nothing is stored.
    """

    def __init__(self, service: Any | None) -> None:
        self.service = service
        self._staged: list[dict[str, Any]] = []
        self._deferred: list[dict[str, Any]] = []

    def add(self, session: Session, **kwargs: Any) -> None:
        svc = self.service
        if svc is None:
            return
        stage = getattr(svc, "stage", None)
        if callable(stage) and callable(getattr(svc, "publish", None)):
            self._staged.extend(stage(session, **kwargs))
        else:
            kwargs.pop("user_ids", None)
            self._deferred.append(kwargs)

    def deliver(self) -> None:
        """Call once, after the transaction committed."""
        svc = self.service
        staged, deferred = self._staged, self._deferred
        self._staged, self._deferred = [], []
        if svc is None:
            return
        if staged:
            try:
                svc.publish(staged)
            except Exception:  # noqa: BLE001 - live push is advisory; the rows are stored
                log.exception("publishing %d notification(s) failed", len(staged))
        for kwargs in deferred:
            try:
                svc.notify(**kwargs)
            except Exception:  # noqa: BLE001 - best effort for foreign implementations
                log.exception("notify(%s) failed", kwargs.get("kind"))

    @property
    def views(self) -> list[dict[str, Any]]:
        """Staged (not yet delivered) notification views."""
        return list(self._staged)
