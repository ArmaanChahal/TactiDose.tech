"""ReportService: generate / list / get / pdf_bytes / send per ARCHITECTURE §8 and the ReportMeta shape."""

from __future__ import annotations

import email
import io
import re
import smtplib
import ssl
from datetime import timedelta
from email import policy

import pypdf
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, event, select

from tactidose.core.bus import Topic
from tactidose.core.interfaces import ReportServiceAPI
from tactidose.db.models import CareLink, DeviceLog, Report, ReportDelivery, User
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tactidose.reports.mailer import MailResult
from tactidose.reports.service import ReportAccessError, ReportService
from tests.test_reports_support import (
    HEADACHE,
    FakeGenaiClient,
    FakeGenaiResponse,
    FakeNotifications,
    fake_smtp_classes,
    fast_renderer,
    seed_report_scenario,
)

META_KEYS = {"report_id", "patient_id", "title", "days", "period_start", "period_end", "status", "pdf_size",
             "created_at", "created_by_user_id", "stats", "narrative", "narrative_source", "pdf_url", "deliveries"}


@pytest.fixture
def ids(db_v2, settings_v2, clock):
    return seed_report_scenario(db_v2, settings_v2, clock)


@pytest.fixture
def notes():
    return FakeNotifications()


@pytest.fixture
def service(db_v2, clock, settings_v2, bus, notes):
    """Fast service (stand-in PDF); tests that inspect the PDF build their own with the real renderer."""
    return ReportService(db_v2, clock, settings_v2, notifications=notes, bus=bus, renderer=fast_renderer)


def test_conforms_to_protocol_and_accepts_wiring_kwargs(db_v2, clock, settings_v2, bus):
    svc = ReportService(db_v2, clock, settings_v2, auth=object(), notifications=None, bus=bus)
    assert isinstance(svc, ReportServiceAPI)


def test_generate_stores_a_ready_report(ids, bus, notes, db_v2, clock, settings_v2):
    service = ReportService(db_v2, clock, settings_v2, notifications=notes, bus=bus)   # real PDF renderer
    sub = bus.subscribe([Topic.REPORT])
    meta = service.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    assert META_KEYS <= set(meta)
    rid = meta["report_id"]
    assert meta["status"] == "READY" and meta["error"] is None and meta["deliveries"] == []
    assert meta["title"] == "TactiDose report — Alex Rivera — last 7 days" and meta["days"] == 7
    assert meta["period_end"] == clock.now().isoformat()
    assert meta["period_start"] == (clock.now() - timedelta(days=7)).isoformat()
    assert meta["period_start_local"] == "2026-09-28T07:55:00-07:00"
    assert meta["created_by_user_id"] == ids["doctor_id"] and meta["pdf_url"] == f"/api/reports/{rid}/pdf"
    assert meta["narrative_source"] == "rules" and meta["narrative"].startswith("- Scheduled doses: 7.")
    assert meta["stats"]["adherence_rate"] == pytest.approx(5 / 6, abs=1e-4)
    assert meta["stats"]["narrative"] == {"source": "rules", "model": None, "fallback_reason": "disabled"}
    pdf = service.pdf_bytes(rid)
    assert pdf.startswith(b"%PDF-") and len(pdf) == meta["pdf_size"]
    text = " ".join("\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages).split())
    assert "Alex Rivera" in text and "83%" in text and HEADACHE in text
    assert [e.data for e in sub.drain()] == [{"patient_id": ids["patient_id"], "report_id": rid, "status": "READY"}]
    (call,) = notes.calls
    assert call["kind"] == "REPORT_READY" and call["patient_id"] == ids["patient_id"]
    assert call["user_ids"] == [ids["doctor_id"]]                            # the creator only
    assert call["to_patient"] is False and call["to_caregivers"] is True      # Protocol-only fallback
    assert call["data"]["report_id"] == rid
    with db_v2.session() as s:
        row = s.get(Report, rid)
        assert row.created_at == clock.now() and row.narrative_source == "rules"
        log = s.scalars(select(DeviceLog).where(DeviceLog.event == "REPORT_GENERATED")).one()
        assert log.category == "REPORT" and log.detail["report_id"] == rid and log.created_at == clock.now()


def test_patient_creator_is_notified_as_patient(service, ids, notes):
    service.generate(patient_id=ids["patient_id"], days=1, created_by_user_id=ids["patient_id"])
    assert notes.calls[0]["to_patient"] is True and notes.calls[0]["to_caregivers"] is False
    assert notes.calls[0]["user_ids"] == [ids["patient_id"]]


def test_protocol_only_notifier_gets_no_user_ids(db_v2, clock, settings_v2, ids):
    class ProtocolNotifier:
        def __init__(self):
            self.calls = []

        def notify(self, *, patient_id, kind, title, body="", data=None, to_patient=True, to_caregivers=True):
            self.calls.append({"kind": kind, "to_patient": to_patient, "to_caregivers": to_caregivers})
            return [1]

        def list_for_user(self, user_id, *, unread_only=False, limit=50):
            return []

        def mark_read(self, user_id, ids=None):
            return 0

    notifier = ProtocolNotifier()
    svc = ReportService(db_v2, clock, settings_v2, notifications=notifier, renderer=fast_renderer)
    svc.generate(patient_id=ids["patient_id"], days=1, created_by_user_id=ids["family_id"])
    assert notifier.calls == [{"kind": "REPORT_READY", "to_patient": False, "to_caregivers": True}]


@pytest.mark.parametrize("days", [0, -1, 91, True, 7.5, "abc", None, "0"])
def test_days_validation(service, ids, days, db_v2):
    with pytest.raises(ValidationError) as err:
        service.generate(patient_id=ids["patient_id"], days=days, created_by_user_id=ids["doctor_id"])
    assert err.value.status_code == 422 and "1 to 90" in str(err.value)
    with db_v2.session() as s:
        assert s.scalars(select(Report)).all() == []


def test_days_limits_follow_settings(db_v2, clock, settings_v2, ids):
    svc = ReportService(db_v2, clock, settings_v2.model_copy(update={"report_max_days": 30}),
                        renderer=fast_renderer)
    with pytest.raises(ValidationError):
        svc.generate(patient_id=ids["patient_id"], days=31, created_by_user_id=ids["patient_id"])
    assert svc.generate(patient_id=ids["patient_id"], days="30", created_by_user_id=ids["patient_id"])["days"] == 30
    assert svc.generate(patient_id=ids["patient_id"], days=1, created_by_user_id=ids["patient_id"])["days"] == 1


def test_access_rules(service, ids, db_v2):
    with db_v2.session() as s:
        stranger = User(display_name="Dr. Stranger", role="doctor", email="stranger@test.tactidose")
        inactive = User(display_name="Old Family", role="family", email="old@test.tactidose", is_active=False)
        s.add_all([stranger, inactive])
        s.flush()
        s.add(CareLink(caregiver_id=inactive.user_id, patient_id=ids["patient_id"], relationship_kind="family"))
        stranger_id, inactive_id = stranger.user_id, inactive.user_id
    for uid in (stranger_id, inactive_id, 999):
        with pytest.raises(ReportAccessError) as err:
            service.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=uid)
        assert err.value.status_code == 403 and isinstance(err.value, PermissionError)
    with pytest.raises(NotFoundError):
        service.generate(patient_id=999, days=7, created_by_user_id=ids["doctor_id"])
    with pytest.raises(NotFoundError):     # a caregiver account is not a patient
        service.generate(patient_id=ids["doctor_id"], days=7, created_by_user_id=ids["doctor_id"])
    meta = service.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["family_id"])
    assert meta["status"] == "READY"


def test_list_is_newest_first_and_never_loads_the_pdf(service, ids, db_v2, clock):
    first = service.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    clock.advance(timedelta(minutes=5))
    second = service.generate(patient_id=ids["patient_id"], days=3, created_by_user_id=ids["patient_id"])
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_v2.engine, "before_cursor_execute", capture)
    try:
        listed = service.list(ids["patient_id"])
        got = service.get(first["report_id"])
    finally:
        event.remove(db_v2.engine, "before_cursor_execute", capture)
    assert [m["report_id"] for m in listed] == [second["report_id"], first["report_id"]]
    assert all(META_KEYS <= set(m) for m in listed) and listed[1]["stats"] == first["stats"]
    assert got["report_id"] == first["report_id"] and got["pdf_size"] == first["pdf_size"]
    selects = [st for st in statements if "FROM reports" in st]
    assert selects and not any(re.search(r"reports\.pdf(?!_size)\b", st) for st in selects), selects
    assert service.list(ids["family_id"]) == []


def test_unknown_report(service):
    for call in (lambda: service.get(4242), lambda: service.pdf_bytes(4242),
                 lambda: service.send(4242, sent_by_user_id=1), lambda: service.patient_of(4242)):
        with pytest.raises(NotFoundError):
            call()


def test_failed_generation_is_stored_as_failed(db_v2, clock, settings_v2, bus, notes, ids):
    def broken(*a, **kw):
        raise RuntimeError("font exploded")

    svc = ReportService(db_v2, clock, settings_v2, notifications=notes, bus=bus, renderer=broken)
    sub = bus.subscribe([Topic.REPORT])
    meta = svc.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    assert meta["status"] == "FAILED" and "RuntimeError: font exploded" in meta["error"]
    assert meta["pdf_size"] == 0 and meta["pdf_url"] is None
    assert meta["stats"]["doses"]["scheduled"] == 7 and meta["narrative_source"] == "rules"
    assert sub.drain()[0].data["status"] == "FAILED" and notes.calls == []
    with pytest.raises(NotFoundError):
        svc.pdf_bytes(meta["report_id"])
    with pytest.raises(ConflictError):
        svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"])
    with db_v2.session() as s:
        assert s.scalars(select(DeviceLog.event).where(DeviceLog.category == "REPORT")).all() == ["REPORT_FAILED"]


def test_renderer_returning_garbage_is_failed(db_v2, clock, settings_v2, ids):
    svc = ReportService(db_v2, clock, settings_v2, renderer=lambda *a, **kw: b"not a pdf")
    meta = svc.generate(patient_id=ids["patient_id"], days=2, created_by_user_id=ids["patient_id"])
    assert meta["status"] == "FAILED" and "no PDF" in meta["error"]


def test_send_to_linked_doctor_saves_eml_without_smtp(service, ids, settings_v2, bus, notes, db_v2, clock):
    meta = service.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["patient_id"])
    sub = bus.subscribe([Topic.REPORT])
    result = service.send(meta["report_id"], sent_by_user_id=ids["patient_id"])
    (d,) = result["deliveries"]
    assert d["to_email"] == "dr.lee@test.tactidose" and d["to_user_id"] == ids["doctor_id"]
    assert d["status"] == "SAVED" and d["error"] is None and d["sent_at"] is None
    assert d["sent_by_user_id"] == ids["patient_id"] and d["created_at"] == clock.now().isoformat()
    emls = list(settings_v2.outbox_dir.glob(f"report-{meta['report_id']}-*.eml"))
    assert len(emls) == 1
    saved = email.message_from_bytes(emls[0].read_bytes(), policy=policy.default)
    assert saved["To"] == "dr.lee@test.tactidose" and saved["Subject"] == meta["title"]
    assert "Attached is the TactiDose report for Alex Rivera" in saved.get_body(("plain",)).get_content()
    assert next(iter(saved.iter_attachments())).get_content() == service.pdf_bytes(meta["report_id"])
    assert service.get(meta["report_id"])["deliveries"] == [d]
    assert sub.drain()[0].data == {"patient_id": ids["patient_id"], "report_id": meta["report_id"], "status": "READY",
                                   "deliveries": [{"delivery_id": d["delivery_id"], "status": "SAVED"}]}
    sent = notes.calls[-1]
    assert sent["kind"] == "REPORT_SENT" and sent["to_patient"] is True and sent["title"] == "Report email saved"
    with db_v2.session() as s:
        assert s.scalars(select(ReportDelivery)).one().status == "SAVED"
        assert "REPORT_SENT" in s.scalars(select(DeviceLog.event)).all()


def test_send_to_explicit_address_uses_the_mailer(db_v2, clock, settings_v2, notes, ids):
    calls = []

    def mailer(settings, **kw):
        calls.append(kw)
        return MailResult("SENT", message_id="<1@x>")

    svc = ReportService(db_v2, clock, settings_v2, notifications=notes, mailer=mailer, renderer=fast_renderer)
    meta = svc.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    result = svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"], to_email=" Sam@Test.Tactidose ")
    (d,) = result["deliveries"]
    assert d["status"] == "SENT" and d["to_email"] == "sam@test.tactidose" and d["to_user_id"] == ids["family_id"]
    assert d["sent_at"] == clock.now().isoformat()
    (kw,) = calls
    assert kw["to"] == "sam@test.tactidose" and kw["pdf_bytes"] == svc.pdf_bytes(meta["report_id"])
    assert kw["subject"] == meta["title"] and kw["report_id"] == meta["report_id"]
    assert kw["filename"] == f"tactidose-report-{meta['report_id']}-20261005.pdf"
    assert "Adherence for scheduled doses: 83% (5 of 6 doses dropped)" in kw["body"]
    assert "Sent by Dr. Lee (doctor)" in kw["body"] and "not a medical device" in kw["body"]
    assert notes.calls[-1]["title"] == "Report sent" and notes.calls[-1]["to_caregivers"] is True
    other = svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"], to_email="someone@example.org")
    assert other["deliveries"][0]["to_user_id"] is None


def test_send_with_smtp_end_to_end(db_v2, clock, settings_v2, ids, monkeypatch):
    plain, _ = fake_smtp_classes()
    monkeypatch.setattr(smtplib, "SMTP", plain)
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: "tls-context")
    settings = settings_v2.model_copy(update={"smtp_host": "smtp.example.com", "smtp_user": "reports@example.com",
                                              "smtp_password": SecretStr("pw"), "smtp_from": "reports@example.com"})
    svc = ReportService(db_v2, clock, settings, renderer=fast_renderer)
    meta = svc.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    result = svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"])
    assert [d["status"] for d in result["deliveries"]] == ["SENT"]
    assert plain.instances[0].tls_context == "tls-context"
    msg = plain.instances[0].sent
    assert msg["To"] == "dr.lee@test.tactidose"
    (att,) = list(msg.iter_attachments())
    assert att.get_content() == svc.pdf_bytes(meta["report_id"])


def test_send_failures_are_recorded_not_raised(db_v2, clock, settings_v2, notes, ids):
    def failing(settings, **kw):
        return MailResult("FAILED", error="SMTPAuthenticationError: (535, b'bad')")

    def raising(settings, **kw):
        raise OSError("disk on fire")

    for mailer, error in ((failing, "SMTPAuthenticationError"), (raising, "OSError: disk on fire")):
        svc = ReportService(db_v2, clock, settings_v2, notifications=notes, mailer=mailer,
                            renderer=fast_renderer)
        meta = svc.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
        (d,) = svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"])["deliveries"]
        assert d["status"] == "FAILED" and error in d["error"]
        assert notes.calls[-1]["title"] == "Report not sent"


def test_send_validation(service, ids, db_v2):
    meta = service.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    rid = meta["report_id"]
    for bad in ("nope", "a@b.com\r\nBcc: x@y.com", "Dr <dr@x.com>"):
        with pytest.raises(ValidationError):
            service.send(rid, sent_by_user_id=ids["doctor_id"], to_email=bad)
    with db_v2.session() as s:
        stranger = User(display_name="Stranger", role="family", email="stranger@test.tactidose")
        s.add(stranger)
        s.flush()
        stranger_id = stranger.user_id
    with pytest.raises(ReportAccessError):
        service.send(rid, sent_by_user_id=stranger_id)
    with db_v2.session() as s:
        s.execute(delete(CareLink).where(CareLink.relationship_kind == "doctor"))
    with pytest.raises(ValidationError) as err:
        service.send(rid, sent_by_user_id=ids["family_id"])
    assert "No doctor" in str(err.value)
    with db_v2.session() as s:
        assert s.scalars(select(ReportDelivery)).all() == []


def test_notification_and_bus_failures_do_not_break_reports(db_v2, clock, settings_v2, ids):
    class BrokenBus:
        def publish(self, *a, **kw):
            raise RuntimeError("bus down")

    svc = ReportService(db_v2, clock, settings_v2, notifications=FakeNotifications(fail=True), bus=BrokenBus(),
                        renderer=fast_renderer)
    meta = svc.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    assert meta["status"] == "READY"
    assert svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"])["deliveries"][0]["status"] == "SAVED"


def test_gemini_narrative_through_the_service(db_v2, clock, settings_v2, ids):
    settings = settings_v2.model_copy(update={"report_ai_summary": True, "gemini_api_key": SecretStr("k")})
    client = FakeGenaiClient(FakeGenaiResponse('- The patient said "My head hurts a little."'))
    svc = ReportService(db_v2, clock, settings, genai_client=client, renderer=fast_renderer)
    meta = svc.generate(patient_id=ids["patient_id"], days=7, created_by_user_id=ids["doctor_id"])
    assert meta["narrative_source"] == "gemini" and meta["narrative"] == '- The patient said "My head hurts a little."'
    assert meta["stats"]["narrative"]["model"] == settings.gemini_model and len(client.calls) == 1


def test_patient_of(service, ids):
    meta = service.generate(patient_id=ids["patient_id"], days=1, created_by_user_id=ids["patient_id"])
    assert service.patient_of(meta["report_id"]) == ids["patient_id"]


def test_mailer_results_are_normalised(db_v2, clock, settings_v2, ids):
    results = iter([{"status": "sent"}, object(), {"status": "LOST", "error": "?"}])
    svc = ReportService(db_v2, clock, settings_v2, renderer=fast_renderer,
                        mailer=lambda settings, **kw: next(results))
    meta = svc.generate(patient_id=ids["patient_id"], days=1, created_by_user_id=ids["doctor_id"])
    statuses = [svc.send(meta["report_id"], sent_by_user_id=ids["doctor_id"], to_email="a@example.com")
                ["deliveries"][0]["status"] for _ in range(3)]
    assert statuses == ["SENT", "FAILED", "FAILED"]
