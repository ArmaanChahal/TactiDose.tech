"""ReportService — implements ``ReportServiceAPI`` (ARCHITECTURE v2 §8, ReportMeta in docs/API.md).

``generate(patient_id, days, created_by_user_id)``
    Validates ``days`` (1..``settings.report_max_days``), the patient and the creator (the patient
    themself or a doctor/family account linked through ``care_links`` — defence in depth on top of
    the API's access check), then gathers ``[now - days, now]`` straight from the v2 tables, computes
    the statistics, writes the narrative (Gemini or rules), selects conversation excerpts and renders
    the PDF. One ``reports`` row is stored either way: ``READY`` with the PDF bytes, or ``FAILED``
    with ``error`` (and whatever statistics were computed). After commit: ``Topic.REPORT``
    ``{patient_id, report_id, status}`` and, for READY, a ``REPORT_READY`` notification for the
    creator. Generation runs synchronously (the HTTP layer calls it from the threadpool).

``list`` / ``get``
    ReportMeta dicts (newest first) with their deliveries; the deferred ``reports.pdf`` column is
    never loaded. ``pdf_bytes`` loads only the PDF.

``send(report_id, sent_by_user_id, to_email=None)``
    Recipients: ``to_email`` (validated, one plain address) or every active, linked doctor
    (``care_links.relationship_kind == 'doctor'``) with an email address. One email and one
    ``report_deliveries`` row per recipient (SENT / SAVED / FAILED, see ``reports.mailer``); email
    I/O happens outside any DB transaction. Afterwards ``Topic.REPORT`` (with the delivery
    statuses) and a ``REPORT_SENT`` notification for the sender. Returns ``{"deliveries": [...]}``.

Errors (``tactidose.medication.errors`` hierarchy, ``status_code`` for the HTTP layer):
``ValidationError`` 422 (days, bad email, no doctor to send to), ``NotFoundError`` 404 (patient,
report, PDF), ``ConflictError`` 409 (sending a FAILED report), ``ReportAccessError`` 403 (the user
is neither the patient nor a linked doctor/family account). Database errors propagate.
Notification and bus failures are logged and never undo a stored report or delivery.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import AuthServiceAPI, NotificationServiceAPI
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    CAREGIVER_ROLES,
    CareLink,
    Device,
    LogCategory,
    NotificationKind,
    Report,
    ReportDelivery,
    Role,
    User,
)
from tactidose.db.session import Database
from tactidose.medication.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from tactidose.reports.data import (
    doctor_recipients,
    fmt_date,
    fmt_pct,
    fmt_time,
    gather_report_data,
)
from tactidose.reports.mailer import (
    STATUS_FAILED,
    STATUS_SAVED,
    STATUS_SENT,
    MailResult,
    normalize_email,
    send_report_email,
)
from tactidose.reports.narrative import ReportNarrator, select_excerpts
from tactidose.reports.pdf import DISCLAIMER, render_report_pdf
from tactidose.reports.stats import compute_stats

log = logging.getLogger(__name__)

__all__ = ["FAILED", "READY", "ReportAccessError", "ReportService", "report_title"]

READY = "READY"
FAILED = "FAILED"
_CAREGIVER_ROLE_VALUES = frozenset(r.value for r in CAREGIVER_ROLES)


class ReportAccessError(DomainError, PermissionError):
    """The user may not create or send reports for this patient (HTTP 403)."""

    status_code = 403


def report_title(display_name: str, days: int) -> str:
    """``TactiDose report — Alex Rivera — last 7 days`` (fits ``reports.title``)."""
    name = re.sub(r"\s+", " ", display_name or "").strip()[:120] or "Patient"
    return f"TactiDose report — {name} — last {days} day{'s' if days != 1 else ''}"[:200]


class ReportService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        *,
        auth: AuthServiceAPI | None = None,
        notifications: NotificationServiceAPI | None = None,
        bus: EventBus | None = None,
        narrator: Any | None = None,
        mailer: Callable[..., MailResult] | None = None,
        genai_client: Any | None = None,
        renderer: Callable[..., bytes] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._settings = settings
        # Accepted for the app.py wiring (ARCHITECTURE §11); access is checked against care_links here.
        self._auth = auth
        self._notifications = notifications
        self._notify_user_ids = _accepts_keyword(getattr(notifications, "notify", None), "user_ids")
        self._bus = bus
        self._narrator = narrator if narrator is not None else ReportNarrator(settings, client=genai_client)
        self._mailer = mailer or send_report_email
        self._renderer = renderer or render_report_pdf

    # ------------------------------------------------------------------ ReportServiceAPI
    def generate(self, *, patient_id: int, days: int, created_by_user_id: int) -> dict[str, Any]:
        days = self._validate_days(days)
        pid = _as_id(patient_id, "patient_id")
        uid = _as_id(created_by_user_id, "created_by_user_id")
        now = self._clock.now()
        start = now - timedelta(days=days)
        with self._db.session() as s:
            patient = self._require_patient(s, pid)
            self._require_member(s, pid, uid)
            title = report_title(patient.display_name, days)
            device_id = self._device_id(s, pid)

        stats: dict[str, Any] = {}
        narrative = None
        pdf: bytes | None = None
        error: str | None = None
        try:
            data = gather_report_data(self._db, self._clock, self._settings, patient_id=pid, days=days,
                                      period_start=start, period_end=now, created_by_user_id=uid,
                                      generated_at=now)
            stats = compute_stats(data)
            narrative = self._narrator.build(data, stats)
            stats["narrative"] = narrative.meta()
            excerpts = select_excerpts(data)
            pdf = bytes(self._renderer(data, stats, narrative, excerpts))
            if not pdf.startswith(b"%PDF"):
                raise RuntimeError("the PDF renderer returned no PDF")
        except Exception as exc:  # stored as a FAILED report, never half-written
            log.exception("report generation failed for patient %s (%d days)", pid, days)
            error = f"Report generation failed: {type(exc).__name__}: {exc}"[:500]
            pdf = None
        status = READY if error is None else FAILED

        with self._db.session() as s:
            row = Report(
                patient_id=pid,
                created_by_user_id=uid,
                days=days,
                period_start=start,
                period_end=now,
                title=title,
                status=status,
                stats=stats,
                narrative=narrative.text if narrative is not None else None,
                narrative_source=narrative.source if narrative is not None else None,
                pdf=pdf,
                pdf_size=len(pdf) if pdf else 0,
                error=error,
                created_at=now,
            )
            s.add(row)
            s.flush()
            log_event(s, device_id, LogCategory.REPORT, "REPORT_GENERATED" if status == READY else "REPORT_FAILED",
                      {"report_id": row.report_id, "patient_id": pid, "days": days, "status": status,
                       "created_by_user_id": uid, "pdf_size": row.pdf_size,
                       "narrative_source": row.narrative_source}, at=now)
            meta = self._meta(row, [])
        log.info("report %s for patient %s: %s (%d days, %d bytes, narrative=%s)", meta["report_id"], pid,
                 status, days, meta["pdf_size"], meta["narrative_source"])

        self._publish({"patient_id": pid, "report_id": meta["report_id"], "status": status})
        if status == READY:
            self._notify(pid, uid, NotificationKind.REPORT_READY.value, "Report ready",
                         f"The report for the last {_days(days)} is ready to view.",
                         {"report_id": meta["report_id"], "days": days})
        return meta

    def list(self, patient_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        pid = _as_id(patient_id, "patient_id")
        limit = max(1, min(int(limit), 500))
        with self._db.session() as s:
            rows = s.scalars(
                select(Report)
                .where(Report.patient_id == pid)
                .order_by(Report.created_at.desc(), Report.report_id.desc())
                .limit(limit)
            ).all()
            deliveries = self._deliveries(s, [r.report_id for r in rows])
            return [self._meta(r, deliveries.get(r.report_id, [])) for r in rows]

    def get(self, report_id: int) -> dict[str, Any]:
        rid = _as_id(report_id, "report_id")
        with self._db.session() as s:
            row = s.get(Report, rid)
            if row is None:
                raise NotFoundError("Report not found.")
            return self._meta(row, self._deliveries(s, [rid]).get(rid, []))

    def patient_of(self, report_id: int) -> int:
        """Patient id of a report (cheap access check for ``/api/reports/{rid}…``)."""
        rid = _as_id(report_id, "report_id")
        with self._db.session() as s:
            pid = s.scalar(select(Report.patient_id).where(Report.report_id == rid))
        if pid is None:
            raise NotFoundError("Report not found.")
        return int(pid)

    def pdf_bytes(self, report_id: int) -> bytes:
        rid = _as_id(report_id, "report_id")
        with self._db.session() as s:
            found = s.execute(select(Report.report_id, Report.pdf).where(Report.report_id == rid)).first()
        if found is None:
            raise NotFoundError("Report not found.")
        if not found.pdf:
            raise NotFoundError("This report has no PDF because it could not be generated.")
        return bytes(found.pdf)

    def send(self, report_id: int, *, sent_by_user_id: int, to_email: str | None = None) -> dict[str, Any]:
        rid = _as_id(report_id, "report_id")
        uid = _as_id(sent_by_user_id, "sent_by_user_id")
        explicit: str | None = None
        recipients: list[tuple[str, int | None]] = []
        if to_email is not None and str(to_email).strip():
            explicit = normalize_email(to_email)
            if explicit is None:
                raise ValidationError("Please enter one valid email address, for example doctor@example.com.")
        now = self._clock.now()
        with self._db.session() as s:
            row = s.get(Report, rid)
            if row is None:
                raise NotFoundError("Report not found.")
            pid = row.patient_id
            self._require_member(s, pid, uid)
            if row.status != READY or not row.pdf_size:
                raise ConflictError("This report could not be generated, so it cannot be sent.")
            pdf = s.scalar(select(Report.pdf).where(Report.report_id == rid))
            if not pdf:
                raise ConflictError("This report has no PDF to send.")
            patient = s.get(User, pid)
            patient_name = patient.display_name if patient is not None else f"Patient {pid}"
            sender = s.get(User, uid)
            sender_label = f"{sender.display_name} ({sender.role})" if sender is not None else "TactiDose"
            title, days, stats = row.title, row.days, dict(row.stats or {})
            period_start, period_end, report_status = row.period_start, row.period_end, row.status
            device_id = self._device_id(s, pid)
            if explicit is not None:
                recipients = [(explicit, self._linked_user_id(s, pid, explicit))]
        if explicit is None:
            seen: set[str] = set()
            for doc in doctor_recipients(self._db, pid):
                address = normalize_email(doc.email)
                if address and address not in seen:
                    seen.add(address)
                    recipients.append((address, doc.user_id))
            if not recipients:
                raise ValidationError("No doctor with an email address is linked to this patient. "
                                      "Enter an email address to send the report.")

        end_local = self._clock.to_local(period_end)
        filename = f"tactidose-report-{rid}-{end_local:%Y%m%d}.pdf"
        body = self._email_body(patient_name, days, period_start, period_end, stats, sender_label)
        results: list[tuple[str, int | None, MailResult]] = []
        for address, to_uid in recipients:
            try:
                result = _coerce_result(self._mailer(self._settings, to=address, subject=title, body=body,
                                                     pdf_bytes=pdf, filename=filename, now=now, report_id=rid))
            except Exception as exc:  # noqa: BLE001 - a broken mailer is a FAILED delivery
                log.warning("report mailer raised for report %s: %s", rid, type(exc).__name__)
                result = MailResult(STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"[:300])
            results.append((address, to_uid, result))

        with self._db.session() as s:
            rows = []
            for address, to_uid, result in results:
                status = result.status
                d = ReportDelivery(report_id=rid, to_email=address, to_user_id=to_uid, sent_by_user_id=uid,
                                   status=status, error=(result.error or None) if status == STATUS_FAILED
                                   else None, created_at=now, sent_at=now if status == STATUS_SENT else None)
                s.add(d)
                rows.append(d)
            s.flush()
            deliveries = [_delivery_dict(d) for d in rows]
            log_event(s, device_id, LogCategory.REPORT, "REPORT_SENT",
                      {"report_id": rid, "patient_id": pid, "sent_by_user_id": uid,
                       "deliveries": [{"delivery_id": d["delivery_id"], "status": d["status"]} for d in deliveries]},
                      at=now)
        log.info("report %s delivered: %s", rid, ", ".join(f"{d['status']}" for d in deliveries))

        self._publish({"patient_id": pid, "report_id": rid, "status": report_status,
                       "deliveries": [{"delivery_id": d["delivery_id"], "status": d["status"]} for d in deliveries]})
        title_text, body_text = _sent_notice(deliveries)
        self._notify(pid, uid, NotificationKind.REPORT_SENT.value, title_text, body_text,
                     {"report_id": rid, "delivery_ids": [d["delivery_id"] for d in deliveries]})
        return {"deliveries": deliveries}

    # ------------------------------------------------------------------ checks
    def _validate_days(self, days: Any) -> int:
        maximum = int(self._settings.report_max_days)
        value: Any = days
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValidationError(f"days must be a whole number from 1 to {maximum}.")
        return value

    @staticmethod
    def _require_patient(s: Any, pid: int) -> User:
        patient = s.get(User, pid)
        if patient is None or patient.role != Role.PATIENT.value:
            raise NotFoundError("Patient not found.")
        return patient

    @staticmethod
    def _require_member(s: Any, pid: int, uid: int) -> User:
        user = s.get(User, uid)
        if user is None or not user.is_active:
            raise ReportAccessError("This account cannot use reports for this patient.")
        if uid == pid:
            return user
        if user.role in _CAREGIVER_ROLE_VALUES:
            linked = s.scalar(select(CareLink.link_id).where(CareLink.caregiver_id == uid,
                                                             CareLink.patient_id == pid))
            if linked is not None:
                return user
        raise ReportAccessError("This account cannot use reports for this patient.")

    @staticmethod
    def _linked_user_id(s: Any, pid: int, address: str) -> int | None:
        return s.scalar(
            select(User.user_id)
            .join(CareLink, CareLink.caregiver_id == User.user_id)
            .where(CareLink.patient_id == pid, func.lower(User.email) == address)
            .limit(1)
        )

    @staticmethod
    def _device_id(s: Any, pid: int) -> str:
        found = s.scalar(select(Device.device_id).where(Device.user_id == pid)
                         .order_by(Device.created_at, Device.device_id).limit(1))
        return found or "-"

    # ------------------------------------------------------------------ views
    def _meta(self, row: Report, deliveries: list[dict[str, Any]]) -> dict[str, Any]:
        has_pdf = row.status == READY and bool(row.pdf_size)
        return {
            "report_id": row.report_id,
            "patient_id": row.patient_id,
            "title": row.title,
            "days": row.days,
            "period_start": _iso(row.period_start),
            "period_end": _iso(row.period_end),
            "period_start_local": _iso(self._clock.to_local(row.period_start)) if row.period_start else None,
            "period_end_local": _iso(self._clock.to_local(row.period_end)) if row.period_end else None,
            "status": row.status,
            "pdf_size": int(row.pdf_size or 0),
            "created_at": _iso(row.created_at),
            "created_by_user_id": row.created_by_user_id,
            "stats": row.stats or {},
            "narrative": row.narrative,
            "narrative_source": row.narrative_source,
            "error": row.error,
            "pdf_url": f"/api/reports/{row.report_id}/pdf" if has_pdf else None,
            "deliveries": deliveries,
        }

    @staticmethod
    def _deliveries(s: Any, report_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        out: dict[int, list[dict[str, Any]]] = {}
        if not report_ids:
            return out
        rows = s.scalars(
            select(ReportDelivery)
            .where(ReportDelivery.report_id.in_(report_ids))
            .order_by(ReportDelivery.created_at, ReportDelivery.delivery_id)
        ).all()
        for d in rows:
            out.setdefault(d.report_id, []).append(_delivery_dict(d))
        return out

    def _email_body(self, patient_name: str, days: int, start: datetime, end: datetime,
                    stats: dict[str, Any], sender_label: str) -> str:
        ls, le = self._clock.to_local(start), self._clock.to_local(end)
        doses = stats.get("doses") or {}
        drops = stats.get("drops") or {}
        lines = [
            "Hello,",
            "",
            f"Attached is the TactiDose report for {patient_name} covering the last {_days(days)}",
            f"({fmt_date(ls)}, {fmt_time(ls)} to {fmt_date(le)}, {fmt_time(le)}, {self._clock.tz_name}).",
            "",
        ]
        if doses:
            decided = int(doses.get("dispensed", 0)) + int(doses.get("missed", 0))
            lines += [
                "Summary",
                f"- Adherence for scheduled doses: {fmt_pct(doses.get('adherence_rate'))}"
                + (f" ({doses.get('dispensed', 0)} of {decided} doses dropped)" if decided else ""),
                (f"- On time: {doses.get('on_time', 0)}, late: {doses.get('late', 0)}, "
                 f"missed: {doses.get('missed', 0)}"),
                f"- Pills dropped on request: {drops.get('on_request_drops', 0)}",
                f"- Refused requests: {drops.get('refused_requests', 0)}",
                f"- Drop problems: {drops.get('failed', 0)} failed, {drops.get('uncertain', 0)} uncertain",
                "",
            ]
        lines += [
            f"Sent by {sender_label} from TactiDose.",
            "The full report is in the attached PDF.",
            "",
            DISCLAIMER,
            "This email contains personal health information.",
            "If you received it by mistake, please delete it.",
        ]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ side channels
    def _publish(self, payload: dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(Topic.REPORT, payload)
        except Exception:  # the bus never breaks a stored report
            log.exception("report bus publish failed")

    def _notify(self, pid: int, uid: int, kind: str, title: str, body: str, data: dict[str, Any]) -> None:
        """Notify the acting user only (ARCHITECTURE §10). Uses the ``user_ids`` extension of
        NotificationService.notify when available; with a Protocol-only notifier a caregiver's notice
        reaches every caregiver linked to this patient (never the patient)."""
        if self._notifications is None:
            return
        is_patient = uid == pid
        kwargs: dict[str, Any] = {"to_patient": is_patient, "to_caregivers": not is_patient}
        if self._notify_user_ids:
            kwargs["user_ids"] = [uid]
        try:
            self._notifications.notify(patient_id=pid, kind=kind, title=title, body=body,
                                       data={**data, "by_user_id": uid}, **kwargs)
        except Exception:  # a notification failure never undoes the report
            log.exception("report notification %s failed", kind)


# --------------------------------------------------------------------------- helpers


def _accepts_keyword(fn: Any, name: str) -> bool:
    """True if ``fn`` declares keyword ``name`` (an explicit parameter or ``**kwargs``)."""
    if not callable(fn):
        return False
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _coerce_result(result: Any) -> MailResult:
    """Normalise what an injected mailer returned (MailResult, a similar object or a dict)."""
    if isinstance(result, dict):
        get = result.get
    else:
        def get(key: str) -> Any:
            return getattr(result, key, None)
    raw = get("status")
    status = str(getattr(raw, "value", raw) or "").upper()
    error = get("error")
    if status not in (STATUS_SENT, STATUS_SAVED, STATUS_FAILED):
        return MailResult(STATUS_FAILED, error=str(error or f"unexpected mailer result {result!r}")[:300])
    return MailResult(status, error=None if error is None else str(error)[:300], path=get("path"),
                      message_id=get("message_id"))


def _as_id(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise ValidationError(f"{name} must be a positive whole number.")
    if value <= 0:
        raise ValidationError(f"{name} must be a positive whole number.")
    return value


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _days(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def _delivery_dict(d: ReportDelivery) -> dict[str, Any]:
    return {
        "delivery_id": d.delivery_id,
        "report_id": d.report_id,
        "to_email": d.to_email,
        "to_user_id": d.to_user_id,
        "sent_by_user_id": d.sent_by_user_id,
        "status": d.status,
        "error": d.error,
        "created_at": _iso(d.created_at),
        "sent_at": _iso(d.sent_at),
    }


def _sent_notice(deliveries: list[dict[str, Any]]) -> tuple[str, str]:
    sent = [d["to_email"] for d in deliveries if d["status"] == STATUS_SENT]
    saved = [d["to_email"] for d in deliveries if d["status"] == STATUS_SAVED]
    failed = [d["to_email"] for d in deliveries if d["status"] == STATUS_FAILED]
    parts = []
    if sent:
        parts.append(f"The report was emailed to {', '.join(sent)}.")
    if saved:
        parts.append(f"Email is not set up, so the message for {', '.join(saved)} was saved on the "
                     "TactiDose computer instead.")
    if failed:
        parts.append(f"The report could not be emailed to {', '.join(failed)}.")
    if sent:
        title = "Report sent" if not failed else "Report partly sent"
    elif saved:
        title = "Report email saved"
    else:
        title = "Report not sent"
    return title, " ".join(parts)
