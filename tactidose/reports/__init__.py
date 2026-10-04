"""TactiDose reports (v2, ARCHITECTURE §8): PDF reports for the last N days, stored in the DB,
viewable in both portals and emailable to the doctor.

Modules:

* :mod:`~tactidose.reports.data` - read-only gathering of one patient's period into plain rows
  (+ shared date/percent formatting).
* :mod:`~tactidose.reports.stats` - statistics stored in ``reports.stats`` (adherence, drops,
  refusals, per day / per medication, inventory and days of supply).
* :mod:`~tactidose.reports.narrative` - Gemini factual conversation summary with a rules
  fallback, and the conversation excerpts printed in the PDF.
* :mod:`~tactidose.reports.pdf` - fpdf2 rendering (Unicode-safe, offline).
* :mod:`~tactidose.reports.mailer` - SMTP delivery with the PDF attached, ``.eml`` in the
  outbox when SMTP is not configured.
* :mod:`~tactidose.reports.service` - :class:`ReportService` (implements ``ReportServiceAPI``).

Importing this package is cheap: submodules (fpdf2, google-genai) load on first use.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MailResult", "ReportAccessError", "ReportService", "render_report_pdf", "send_report_email"]


def __getattr__(name: str) -> Any:
    if name in ("ReportService", "ReportAccessError"):
        from tactidose.reports import service

        return getattr(service, name)
    if name in ("MailResult", "send_report_email"):
        from tactidose.reports import mailer

        return getattr(mailer, name)
    if name == "render_report_pdf":
        from tactidose.reports.pdf import render_report_pdf

        return render_report_pdf
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
