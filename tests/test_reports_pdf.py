"""reports.pdf: the rendered PDF parses (pypdf) and carries the facts; Unicode and font fallbacks."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta

import pypdf
import pytest

from tactidose.reports.data import MessageRow, PersonRow, gather_report_data
from tactidose.reports.narrative import Narrative, rules_narrative, select_excerpts
from tactidose.reports.pdf import (
    DISCLAIMER,
    FontFile,
    FontPaths,
    _FontKit,
    find_system_fonts,
    render_report_pdf,
)
from tactidose.reports.stats import compute_stats
from tests.fakes import seed_v2
from tests.test_reports_support import HEADACHE, seed_report_scenario


def _text(pdf: bytes) -> tuple[pypdf.PdfReader, str]:
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    raw = "\n".join(page.extract_text() for page in reader.pages)
    return reader, " ".join(raw.split())


def _render(data, *, fonts=None, narrative=None):
    stats = compute_stats(data)
    narrative = narrative or Narrative(rules_narrative(data, stats), "rules", fallback_reason="disabled")
    return render_report_pdf(data, stats, narrative, select_excerpts(data), fonts=fonts)


@pytest.fixture
def data(db_v2, settings_v2, clock):
    ids = seed_report_scenario(db_v2, settings_v2, clock)
    now = clock.now()
    return gather_report_data(db_v2, clock, settings_v2, patient_id=ids["patient_id"], days=7,
                              period_start=now - timedelta(days=7), period_end=now,
                              created_by_user_id=ids["doctor_id"])


def test_pdf_contains_the_facts(data):
    pdf = _render(data)
    assert pdf.startswith(b"%PDF-")
    reader, text = _text(pdf)
    assert "Alex Rivera" in text and "Patient ID" in text
    assert "Period: 28 Sep 2026, 7:55 AM – 5 Oct 2026, 7:55 AM (America/Vancouver) · last 7 days" in text
    assert "Generated: 5 Oct 2026, 7:55 AM by Dr. Lee (doctor)" in text
    assert "Adherence 83%" in text and "5 of 6 scheduled doses dropped" in text
    assert HEADACHE in text                                    # conversation quote
    assert "Pill request refused (cooldown after a recent drop)" in text
    assert "Sun 4 Oct, 8:19 AM — Patient (voice)" in text
    for heading in ("Summary", "Adherence by day", "Medications", "Missed, failed and uncertain",
                    "Refused requests", "Inventory now", "Conversation summary", "Conversation excerpts",
                    "About this report"):
        assert heading in text, heading
    assert "Drop uncertain" in text and "Drop failed" in text and "Missed dose" in text
    assert "Low stock" in text and "Empty" in text            # state written as words
    n = len(reader.pages)
    assert n >= 2 and text.count(DISCLAIMER) >= n and f"Page 1 of {n}" in text and f"Page {n} of {n}" in text
    assert reader.metadata.title == "TactiDose report — Alex Rivera — last 7 days"
    assert [o.title for o in reader.outline][:2] == ["Summary", "Adherence by day"]


def test_pdf_gemini_narrative_is_labelled(data):
    narrative = Narrative("- The patient asked for a pill twice.", "gemini", model="gemini-test")
    _, text = _text(_render(data, narrative=narrative))
    assert "Written by Gemini (gemini-test)" in text and "The patient asked for a pill twice." in text


def test_pdf_unicode_and_emoji(data):
    data = replace(
        data,
        patient=PersonRow(data.patient.user_id, "Zo\u00eb \u00d1\u00fa\u00f1ez \U0001f600", "patient"),
        messages=data.messages + (
            MessageRow(99001, 99, "user", "I feel \U0001f922 today \u2014 \u201cmuch\u201d better\u2026 \u2713 \u041f\u0440\u0438\u0432\u0435\u0442 \U0001f44d\U0001f3fd\u2764\ufe0f\u200d",
                       data.period_end - timedelta(minutes=5), input_mode="text"),
        ),
    )
    pdf = _render(data)
    _, text = _text(pdf)
    assert "Zo\u00eb \u00d1\u00fa\u00f1ez" in text and "I feel" in text and "\u201cmuch\u201d better\u2026" in text
    assert "\U0001f600" in text or "[grinning face]" in text   # glyph from a fallback font, or its name
    assert "\ufe0f" not in text and "\u200d" not in text and "\U0001f3fd" not in text   # dropped


def test_pdf_core_font_fallback_sanitises(data):
    data = replace(data, patient=PersonRow(data.patient.user_id, "Zoë 李 😀", "patient"),
                   messages=data.messages + (MessageRow(99001, 99, "user", "Pain ≤ 3 → ok ✓",
                                                        data.period_end - timedelta(minutes=5)),))
    pdf = _render(data, fonts=FontPaths())
    _, text = _text(pdf)
    assert "Zoë ? [grinning face]" in text                     # cp1252 kept, CJK -> ?, emoji -> name
    assert "Pain <= 3 -> ok [check mark]" in text
    assert DISCLAIMER in text and "83%" in text


def test_pdf_falls_back_when_the_font_is_unusable(data, tmp_path, caplog):
    bad = tmp_path / "broken.ttf"
    bad.write_bytes(b"this is not a font")
    pdf = _render(data, fonts=FontPaths(regular=FontFile(bad), bold=None))
    assert pdf.startswith(b"%PDF-") and "Alex Rivera" in _text(pdf)[1]
    assert "retrying with the built-in font" in caplog.text


def test_pdf_empty_period(db_v2, settings_v2, clock):
    ids = seed_v2(db_v2, settings_v2)
    now = clock.now()
    empty = gather_report_data(db_v2, clock, settings_v2, patient_id=ids["patient_id"], days=1,
                               period_start=now - timedelta(days=1), period_end=now)
    _, text = _text(_render(empty))
    assert "No scheduled doses decided yet" in text and "No conversations with the assistant in this period." in text
    assert "No missed doses and no failed or uncertain drops." in text
    assert "No drop request was refused in this period." in text and "last 1 day" in text


def test_pdf_long_period_paginates(db_v2, settings_v2, clock):
    ids = seed_v2(db_v2, settings_v2)
    now = clock.now()
    long = gather_report_data(db_v2, clock, settings_v2, patient_id=ids["patient_id"], days=90,
                              period_start=now - timedelta(days=90), period_end=now)
    reader, text = _text(_render(long))
    assert len(reader.pages) >= 3 and "Tue 7 Jul" in text and "Mon 5 Oct" in text


def test_font_kit_cleaning_core_mode():
    from fpdf import FPDF

    kit = _FontKit(FPDF(), FontPaths())
    assert kit.clean("a\tb\x00c\r\nd e") == "a bc\nd\ne"
    assert kit.clean("café — ‘x’ €") == "café — ‘x’ €"           # cp1252 superset of latin-1
    assert kit.clean("łódź") == "?ódz"                            # NFKD strips the accent, else '?'
    assert kit.clean("\u2764\ufe0f") == "[heavy black heart]" and kit.clean(None) == ""


def test_system_font_discovery_is_cached():
    first = find_system_fonts()
    assert find_system_fonts() is first
    if first.regular is not None:
        assert first.regular.path.is_file()


@pytest.mark.parametrize("platform,layout,regular,bold", [
    ("linux", ["truetype/dejavu/DejaVuSans.ttf", "truetype/dejavu/DejaVuSans-Bold.ttf",
               "opentype/noto/NotoSansCJK-Regular.ttc"], "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("linux", ["truetype/liberation/LiberationSans-Regular.ttf"], "LiberationSans-Regular.ttf", None),
    ("darwin", ["Arial.ttf", "Arial Bold.ttf", "Arial Unicode.ttf"], "Arial.ttf", "Arial Bold.ttf"),
    ("win32", ["segoeui.ttf", "segoeuib.ttf", "seguisym.ttf"], "segoeui.ttf", "segoeuib.ttf"),
    ("linux", [], None, None),
])
def test_font_discovery_per_platform(monkeypatch, tmp_path, platform, layout, regular, bold):
    import tactidose.reports.pdf as pdfmod

    for rel in layout:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")
    monkeypatch.setattr(pdfmod.sys, "platform", platform)
    for fn in ("_win_dirs", "_linux_dirs", "_mac_dirs"):
        monkeypatch.setattr(pdfmod, fn, lambda: [tmp_path])
    found = pdfmod.find_system_fonts.__wrapped__()
    assert (found.regular.path.name if found.regular else None) == regular
    assert (found.bold.path.name if found.bold else None) == bold
    assert all(f.path.is_file() for f in found.fallbacks)
    if platform == "darwin":
        assert [f.path.name for f in found.fallbacks] == ["Arial Unicode.ttf"]


def test_running_header_is_fitted_to_the_page():
    from fpdf import FPDF

    from tactidose.reports.pdf import _fit_text

    pdf = FPDF()
    pdf.core_fonts_encoding = "windows-1252"   # as the renderer configures the built-in font
    pdf.set_font("helvetica", "", 9)
    text = "TactiDose report - " + "Maximilian-Alexander " * 20
    fitted = _fit_text(pdf, text, 100)
    assert fitted.endswith("…") and pdf.get_string_width(fitted) <= 100 < pdf.get_string_width(text)
    assert _fit_text(pdf, "short", 100) == "short"
