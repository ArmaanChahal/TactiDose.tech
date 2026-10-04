"""reports.mailer: SMTP (mocked: STARTTLS / SSL / login / attachment), .eml outbox, never raises."""

from __future__ import annotations

import email
import smtplib
import ssl
from datetime import datetime, timezone
from email import policy
from pathlib import Path

import pytest
from pydantic import SecretStr

from tactidose.reports.mailer import (
    build_message,
    normalize_email,
    send_report_email,
    send_test_email,
)
from tests.test_reports_support import fake_smtp_classes

PDF = b"%PDF-1.7\n% fake report\n"
TLS_CONTEXT = object()      # stands in for ssl.create_default_context() (verified certificates)
NOW = datetime(2026, 10, 5, 14, 55, tzinfo=timezone.utc)


@pytest.fixture
def smtp(monkeypatch):
    plain, ssl_cls = fake_smtp_classes()
    monkeypatch.setattr(smtplib, "SMTP", plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", ssl_cls)
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: TLS_CONTEXT)
    return plain, ssl_cls


def _smtp_settings(settings_v2, **kw):
    base = {"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_user": "reports@example.com",
            "smtp_password": SecretStr("s3cret-pw"), "smtp_from": "TactiDose <reports@example.com>",
            "smtp_timeout_s": 7.5}
    base.update(kw)
    return settings_v2.model_copy(update=base)


def _send(settings, **kw):
    args = {"to": "dr.lee@example.com", "subject": "TactiDose report — Alex Rivera — last 7 days",
            "body": "Report attached.\n", "pdf_bytes": PDF, "filename": "tactidose-report-3-20261005.pdf",
            "now": NOW, "report_id": 3}
    args.update(kw)
    return send_report_email(settings, **args)


def test_starttls_login_and_pdf_attachment(settings_v2, smtp):
    plain, ssl_cls = smtp
    result = _send(_smtp_settings(settings_v2))
    assert result.status == "SENT" and result.ok and result.error is None and result.message_id
    (conn,) = plain.instances
    assert ssl_cls.instances == []
    assert (conn.host, conn.port, conn.timeout) == ("smtp.example.com", 587, 7.5)
    assert conn.calls == ["ehlo", "starttls", "ehlo", ("login", "reports@example.com", "s3cret-pw"),
                          "send_message", "quit"]
    assert conn.tls_context is TLS_CONTEXT                       # verified-certificate context
    msg = conn.sent
    assert msg["To"] == "dr.lee@example.com" and msg["From"] == "TactiDose <reports@example.com>"
    assert msg["Subject"] == "TactiDose report — Alex Rivera — last 7 days" and msg["Date"]
    assert msg["Message-ID"].endswith("@example.com>")
    parts = list(msg.iter_attachments())
    assert len(parts) == 1 and parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_filename() == "tactidose-report-3-20261005.pdf" and parts[0].get_content() == PDF
    assert msg.get_body(("plain",)).get_content() == "Report attached.\n"


def test_implicit_ssl(settings_v2, smtp):
    plain, ssl_cls = smtp
    result = _send(_smtp_settings(settings_v2, smtp_ssl=True, smtp_port=465))
    assert result.status == "SENT"
    (conn,) = ssl_cls.instances
    assert plain.instances == [] and conn.port == 465 and conn.context is TLS_CONTEXT
    assert "starttls" not in conn.calls and ("login", "reports@example.com", "s3cret-pw") in conn.calls


def test_no_login_without_user_and_plain_relay(settings_v2, smtp):
    plain, _ = smtp
    result = _send(_smtp_settings(settings_v2, smtp_user=None, smtp_password=None, smtp_starttls=False,
                                  smtp_port=1025))
    assert result.status == "SENT"
    assert plain.instances[0].calls == ["ehlo", "send_message", "quit"]


@pytest.mark.parametrize("fail_on,error", [
    ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials for s3cret-pw")),
    ("starttls", smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")),
    ("send_message", smtplib.SMTPServerDisconnected("Connection unexpectedly closed")),
    ("__init__", ConnectionRefusedError(10061, "No connection could be made")),
    ("__init__", TimeoutError("timed out")),
])
def test_smtp_errors_become_failed(settings_v2, smtp, fail_on, error):
    plain, _ = smtp
    plain.fail_on, plain.error = fail_on, error
    result = _send(_smtp_settings(settings_v2))
    assert result.status == "FAILED" and not result.ok
    assert type(error).__name__ in result.error and "s3cret-pw" not in result.error
    conn = plain.instances[0]
    if fail_on != "__init__":
        assert conn.calls[-1] == "quit"                          # connection always closed
    if fail_on == "starttls":
        assert "send_message" not in conn.calls                  # never falls back to plain text


def test_refused_recipient_is_failed(settings_v2, smtp):
    plain, _ = smtp
    plain.refused = {"dr.lee@example.com": (550, b"no such user")}
    result = _send(_smtp_settings(settings_v2))
    assert result.status == "FAILED" and "recipient refused" in result.error


def test_saves_eml_when_smtp_not_configured(settings_v2, smtp):
    plain, ssl_cls = smtp
    assert not settings_v2.smtp_configured
    first = _send(settings_v2)
    second = _send(settings_v2)
    assert first.status == "SAVED" and first.ok and plain.instances == [] and ssl_cls.instances == []
    path = settings_v2.outbox_dir / "report-3-20261005T145500Z.eml"
    assert first.path == str(path) and second.path.endswith("report-3-20261005T145500Z-2.eml")
    raw = path.read_bytes()
    assert b"\r\n" in raw
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["To"] == "dr.lee@example.com" and msg["X-Unsent"] == "1"
    assert msg["From"] == "TactiDose <no-reply@tactidose.local>"
    att = next(iter(msg.iter_attachments()))
    assert att.get_content() == PDF and att.get_filename() == "tactidose-report-3-20261005.pdf"


def test_save_failure_is_failed(settings_v2, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    settings = settings_v2.model_copy(update={"data_dir": blocker})
    result = _send(settings)
    assert result.status == "FAILED" and "could not save the email" in result.error


@pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", "dr@example.com\r\nBcc: evil@example.com",
                                 "Dr <dr@example.com>", "a@example.com, b@example.com", None, 42])
def test_invalid_recipient_is_rejected_before_any_io(settings_v2, smtp, bad):
    plain, _ = smtp
    result = _send(_smtp_settings(settings_v2), to=bad)
    assert result.status == "FAILED" and result.error == "invalid recipient address" and plain.instances == []


def test_headers_cannot_be_injected(settings_v2):
    msg = build_message(settings_v2, to="dr@example.com", subject="Report\r\nBcc: evil@example.com", body="x",
                        pdf_bytes=PDF, filename="../../etc/passwd", now=NOW)
    assert msg["Bcc"] is None and msg["Subject"] == "Report Bcc: evil@example.com"
    assert next(iter(msg.iter_attachments())).get_filename() == "etc-passwd.pdf"


def test_normalize_email():
    assert normalize_email("  Dr.Lee@Example.COM ") == "dr.lee@example.com"
    assert normalize_email("dr.lee@test.tactidose") == "dr.lee@test.tactidose"
    assert normalize_email("a..b@example.com") is None and normalize_email("x" * 65 + "@example.com") is None


def test_send_test_email(settings_v2, smtp):
    plain, _ = smtp
    saved = send_test_email(settings_v2, "dr.lee@example.com", now=NOW)
    assert saved.status == "SAVED" and saved.path.endswith("test-20261005T145500Z.eml")
    msg = email.message_from_bytes(Path(saved.path).read_bytes(), policy=policy.default)
    assert msg["Subject"] == "TactiDose test email" and list(msg.iter_attachments()) == []
    sent = send_test_email(_smtp_settings(settings_v2), "dr.lee@example.com")
    assert sent.status == "SENT" and plain.instances[0].sent["To"] == "dr.lee@example.com"
    assert send_test_email(settings_v2, "bad address").status == "FAILED"
    assert _send(settings_v2, report_id=None).path.endswith("report-20261005T145500Z.eml")
