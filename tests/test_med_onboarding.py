"""OnboardingService: extraction never becomes a Medication without human confirmation."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tactidose.core.bus import Topic
from tactidose.core.interfaces import ExtractionResult, LabelExtraction
from tactidose.db.models import LabelScan, Medication
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tactidose.medication.onboarding import (
    NOT_AVAILABLE_MESSAGE,
    REVIEW_MESSAGE,
    OnboardingService,
    normalize_mime,
)
from tests.fakes import FakeExtractor
from tests.test_med_support import Med, med, med_template  # noqa: F401 - fixtures

JPEG = b"\xff\xd8\xff\xe0" + b"demo-label" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"demo-label" * 20
SCAN_KEYS = {"scan_id", "status", "extracted", "model", "error", "user_message", "created_at",
             "reviewed_at", "medication_id"}


def _service(m: Med, extractor=None, **overrides) -> OnboardingService:
    st = m.settings.model_copy(update=overrides) if overrides else m.settings
    return OnboardingService(m.db, extractor, m.catalog, st, m.clock, bus=m.bus)


@pytest.fixture
def ob(med: Med, fake_extractor: FakeExtractor) -> OnboardingService:
    return _service(med, fake_extractor)


def _medications(m: Med) -> int:
    with m.db.session() as s:
        return s.scalar(select(func.count()).select_from(Medication))


def _scan_row(m: Med, scan_id: int) -> LabelScan:
    with m.db.session() as s:
        return s.get(LabelScan, scan_id)


def test_scan_is_pending_review_and_never_creates_medication(med: Med, ob, fake_extractor):
    before = _medications(med)
    sub = med.subscribe(Topic.DATA_CHANGED)
    out = ob.scan(JPEG, "image/jpeg")
    assert set(out) == SCAN_KEYS
    assert out["status"] == "PENDING_REVIEW" and out["user_message"] == REVIEW_MESSAGE and out["error"] is None
    assert out["extracted"]["medication_name"] == "Vitamin C (demo candy)" and out["extracted"]["legible"] is True
    assert out["model"] == "fake" and out["medication_id"] is None and out["reviewed_at"] is None
    assert out["created_at"] == med.clock.now().isoformat()
    assert _medications(med) == before                                   # extraction is data, not a record
    assert fake_extractor.calls == [(len(JPEG), "image/jpeg")]
    sha = hashlib.sha256(JPEG).hexdigest()
    stored = med.settings.scans_dir / f"{sha}.jpg"
    assert stored.read_bytes() == JPEG
    row = _scan_row(med, out["scan_id"])
    assert row.image_sha256 == sha and row.image_path.endswith(f"{sha}.jpg")
    assert {"entity": "scan", "id": out["scan_id"]} in [e.data for e in sub.drain()]


def test_scan_without_extractor_explains_manual_entry(med: Med):
    out = _service(med, None).scan(PNG, "image/png")
    assert out["status"] == "FAILED" and out["user_message"] == NOT_AVAILABLE_MESSAGE
    assert out["extracted"] is None and out["error"] == "not_configured"


def test_scan_extractor_exception_fails_closed(med: Med):
    class Boom:
        name = "boom"

        def extract(self, image: bytes, mime_type: str) -> ExtractionResult:
            raise TimeoutError("gemini took too long")

    out = _service(med, Boom()).scan(JPEG, "image/jpeg")
    assert out["status"] == "FAILED" and out["user_message"] == ExtractionResult.COULD_NOT_READ
    assert out["error"] == "exception: TimeoutError" and out["model"] == "boom"


@pytest.mark.parametrize("result,error", [
    (ExtractionResult(ok=False, model="gemini-x", error="timeout", user_message="Network unavailable."), "timeout"),
    (ExtractionResult(ok=True, model="gemini-x", data=LabelExtraction(medication_name="Zinc", legible=False)),
     "not_legible"),
    (ExtractionResult(ok=True, model="gemini-x", data=LabelExtraction(medication_name="   ")), "no_medication_name"),
    (ExtractionResult(ok=True, model="gemini-x", data=None), "extraction_failed"),
])
def test_unreliable_extraction_is_failed(med: Med, result, error):
    before = _medications(med)
    out = _service(med, FakeExtractor(result)).scan(JPEG, "image/jpeg")
    assert out["status"] == "FAILED" and out["error"] == error and out["extracted"] is None
    assert out["user_message"] == ExtractionResult.COULD_NOT_READ and out["model"] == "gemini-x"
    assert _medications(med) == before


def test_scan_validates_type_and_size(med: Med, ob, fake_extractor):
    for mime in ("image/gif", "text/plain", "", None):
        with pytest.raises(ValidationError):
            ob.scan(JPEG, mime)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ob.scan(b"", "image/jpeg")
    with pytest.raises(ValidationError):
        ob.scan("not bytes", "image/jpeg")  # type: ignore[arg-type]
    small = _service(med, fake_extractor, max_label_image_bytes=100)
    with pytest.raises(ValidationError):
        small.scan(b"x" * 101, "image/jpeg")
    assert small.scan(b"x" * 100, "image/jpeg")["status"] == "PENDING_REVIEW"
    assert normalize_mime("image/jpg") == "image/jpeg" and normalize_mime(" IMAGE/PNG; q=1 ") == "image/png"
    out = ob.scan(PNG, "IMAGE/PNG; q=1")
    assert _scan_row(med, out["scan_id"]).image_path.endswith(".png")
    assert ob.scan(b"RIFF....WEBPVP8 ", "image/webp")["status"] == "PENDING_REVIEW"
    assert len(fake_extractor.calls) == 3                                 # rejected uploads never reach the AI


def test_confirm_uses_only_human_supplied_fields(med: Med, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    med.clock.advance(timedelta(minutes=3))
    out = ob.confirm_scan(scan["scan_id"], {"name": "Vitamin C chewable (demo)", "strength": "1 piece"},
                          confirmed=True, confirmed_by="caregiver")
    assert out["name"] == "Vitamin C chewable (demo)" and out["source"] == "label_scan"
    assert out["instructions_text"] is None and out["warnings"] == []     # nothing copied from the extraction
    assert out["confirmed_by_user"] is True and out["confirmed_by"] == "caregiver"
    assert med.medication(out["medication_id"]).scan_id == scan["scan_id"]
    row = _scan_row(med, scan["scan_id"])
    assert row.status == "CONFIRMED" and row.medication_id == out["medication_id"]
    assert row.reviewed_by == "caregiver" and row.reviewed_at == med.clock.now()
    assert ob.list_scans()[0]["status"] == "CONFIRMED" and ob.list_scans()[0]["user_message"] is None
    # The confirmed medication is now a normal record: assignable and schedulable.
    med.compartments.assign(0, out["medication_id"])
    assert med.scheduler.create_schedule(out["medication_id"], "21:00")["active"] is True


def test_confirm_requires_confirmation_and_a_name(med: Med, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    before = _medications(med)
    with pytest.raises(ValidationError):
        ob.confirm_scan(scan["scan_id"], {"name": "Zinc"}, confirmed=False)
    with pytest.raises(ValidationError):
        ob.confirm_scan(scan["scan_id"], {"strength": "1 piece"}, confirmed=True)   # name required
    assert _medications(med) == before and _scan_row(med, scan["scan_id"]).status == "PENDING_REVIEW"


def test_confirm_only_pending_scans(med: Med, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    ob.confirm_scan(scan["scan_id"], {"name": "Zinc"}, confirmed=True)
    with pytest.raises(ConflictError):
        ob.confirm_scan(scan["scan_id"], {"name": "Zinc again"}, confirmed=True)     # no double records
    failed = _service(med, None).scan(JPEG, "image/jpeg")
    with pytest.raises(ConflictError):
        ob.confirm_scan(failed["scan_id"], {"name": "Zinc"}, confirmed=True)
    with pytest.raises(NotFoundError):
        ob.confirm_scan(777, {"name": "Zinc"}, confirmed=True)


def test_reject_scan(med: Med, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    out = ob.reject_scan(scan["scan_id"], by="  caregiver ")
    assert out["status"] == "REJECTED" and out["reviewed_at"] == med.clock.now().isoformat()
    assert _scan_row(med, scan["scan_id"]).reviewed_by == "caregiver"
    assert ob.reject_scan(scan["scan_id"])["status"] == "REJECTED"                   # idempotent
    with pytest.raises(ConflictError):
        ob.confirm_scan(scan["scan_id"], {"name": "Zinc"}, confirmed=True)
    failed = _service(med, None).scan(JPEG, "image/jpeg")
    assert ob.reject_scan(failed["scan_id"])["status"] == "REJECTED"                 # dismiss a failed scan
    confirmed = ob.scan(PNG, "image/png")
    ob.confirm_scan(confirmed["scan_id"], {"name": "Zinc"}, confirmed=True)
    with pytest.raises(ConflictError):
        ob.reject_scan(confirmed["scan_id"])
    with pytest.raises(NotFoundError):
        ob.reject_scan(31337)


def test_list_scans_newest_first_and_filtered(med: Med, ob):
    ids = []
    for _ in range(3):
        ids.append(ob.scan(JPEG, "image/jpeg")["scan_id"])
        med.clock.advance(timedelta(seconds=30))
    ob.reject_scan(ids[0])
    assert [s["scan_id"] for s in ob.list_scans()] == list(reversed(ids))
    assert [s["scan_id"] for s in ob.list_scans("pending_review")] == [ids[2], ids[1]]
    assert [s["scan_id"] for s in ob.list_scans("REJECTED")] == [ids[0]]
    with pytest.raises(ValidationError):
        ob.list_scans("APPROVED")
    assert ob.get_scan(ids[1])["status"] == "PENDING_REVIEW"
    with pytest.raises(NotFoundError):
        ob.get_scan(999)
