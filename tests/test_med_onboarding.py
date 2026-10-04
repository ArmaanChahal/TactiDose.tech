"""OnboardingService (optional extra): an extraction never becomes a Medication without human
confirmation; scans belong to one patient."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from tactidose.core.bus import Topic
from tactidose.core.interfaces import ExtractionResult, LabelExtraction
from tactidose.db.models import LabelScan, Medication, User
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tactidose.medication.onboarding import (
    NOT_AVAILABLE_MESSAGE,
    REVIEW_MESSAGE,
    OnboardingService,
    normalize_mime,
)
from tests.fakes import FakeExtractor
from tests.test_med_support import Env, env, env_template  # noqa: F401 - fixtures

JPEG = b"\xff\xd8\xff\xe0" + b"demo-label" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"demo-label" * 20
SCAN_KEYS = {"scan_id", "status", "extracted", "model", "error", "user_message", "created_at",
             "reviewed_at", "medication_id"}


def _service(e: Env, extractor=None, **overrides) -> OnboardingService:
    st = e.settings.model_copy(update=overrides) if overrides else e.settings
    return OnboardingService(e.db, extractor, e.catalog, st, e.clock, bus=e.bus)


@pytest.fixture
def ob(env: Env, fake_extractor: FakeExtractor) -> OnboardingService:
    return _service(env, fake_extractor)


def _medications(e: Env) -> int:
    with e.db.session() as s:
        return s.scalar(select(func.count()).select_from(Medication))


def _scan_row(e: Env, scan_id: int) -> LabelScan:
    with e.db.session() as s:
        return s.get(LabelScan, scan_id)


def _other_patient(e: Env) -> int:
    with e.db.session() as s:
        u = User(display_name="Pat Two", role="patient", email="pat2@test.tactidose")
        s.add(u)
        s.flush()
        return u.user_id


def test_scan_is_pending_review_and_never_creates_medication(env: Env, ob, fake_extractor):
    before = _medications(env)
    sub = env.subscribe(Topic.DATA_CHANGED)
    out = ob.scan(JPEG, "image/jpeg", patient_id=env.patient)
    assert set(out) == SCAN_KEYS
    assert out["status"] == "PENDING_REVIEW" and out["user_message"] == REVIEW_MESSAGE and out["error"] is None
    assert out["extracted"]["medication_name"] == "Vitamin C (demo candy)" and out["extracted"]["legible"] is True
    assert out["model"] == "fake" and out["medication_id"] is None and out["reviewed_at"] is None
    assert out["created_at"] == env.clock.now().isoformat()
    assert _medications(env) == before                                   # extraction is data, not a record
    assert fake_extractor.calls == [(len(JPEG), "image/jpeg")]
    sha = hashlib.sha256(JPEG).hexdigest()
    assert (env.settings.scans_dir / f"{sha}.jpg").read_bytes() == JPEG
    row = _scan_row(env, out["scan_id"])
    assert row.image_sha256 == sha and row.image_path.endswith(f"{sha}.jpg") and row.user_id == env.patient
    assert {"entity": "scan", "id": out["scan_id"]} in [e.data for e in sub.drain()]


def test_scan_without_extractor_explains_manual_entry(env: Env):
    out = _service(env, None).scan(PNG, "image/png")
    assert out["status"] == "FAILED" and out["user_message"] == NOT_AVAILABLE_MESSAGE
    assert out["extracted"] is None and out["error"] == "not_configured"


def test_scan_extractor_exception_fails_closed(env: Env):
    class Boom:
        name = "boom"

        def extract(self, image: bytes, mime_type: str) -> ExtractionResult:
            raise TimeoutError("gemini took too long")

    out = _service(env, Boom()).scan(JPEG, "image/jpeg")
    assert out["status"] == "FAILED" and out["user_message"] == ExtractionResult.COULD_NOT_READ
    assert out["error"] == "exception: TimeoutError" and out["model"] == "boom"


@pytest.mark.parametrize("result,error", [
    (ExtractionResult(ok=False, model="gemini-x", error="timeout", user_message="Network unavailable."), "timeout"),
    (ExtractionResult(ok=True, model="gemini-x", data=LabelExtraction(medication_name="Zinc", legible=False)),
     "not_legible"),
    (ExtractionResult(ok=True, model="gemini-x", data=LabelExtraction(medication_name="   ")), "no_medication_name"),
    (ExtractionResult(ok=True, model="gemini-x", data=None), "extraction_failed"),
])
def test_unreliable_extraction_is_failed(env: Env, result, error):
    before = _medications(env)
    out = _service(env, FakeExtractor(result)).scan(JPEG, "image/jpeg")
    assert out["status"] == "FAILED" and out["error"] == error and out["extracted"] is None
    assert out["user_message"] == ExtractionResult.COULD_NOT_READ and out["model"] == "gemini-x"
    assert _medications(env) == before


def test_scan_validates_type_size_and_patient(env: Env, ob, fake_extractor):
    for mime in ("image/gif", "text/plain", "", None):
        with pytest.raises(ValidationError):
            ob.scan(JPEG, mime)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ob.scan(b"", "image/jpeg")
    with pytest.raises(ValidationError):
        ob.scan("not bytes", "image/jpeg")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        ob.scan(JPEG, "image/jpeg", patient_id=4242)
    with pytest.raises(ValidationError):
        ob.scan(JPEG, "image/jpeg", patient_id=env.doctor)
    small = _service(env, fake_extractor, max_label_image_bytes=100)
    with pytest.raises(ValidationError):
        small.scan(b"x" * 101, "image/jpeg")
    assert small.scan(b"x" * 100, "image/jpeg")["status"] == "PENDING_REVIEW"
    assert normalize_mime("image/jpg") == "image/jpeg" and normalize_mime(" IMAGE/PNG; q=1 ") == "image/png"
    out = ob.scan(PNG, "IMAGE/PNG; q=1")
    assert _scan_row(env, out["scan_id"]).image_path.endswith(".png")
    assert ob.scan(b"RIFF....WEBPVP8 ", "image/webp")["status"] == "PENDING_REVIEW"
    assert len(fake_extractor.calls) == 3                                 # rejected uploads never reach the AI


def test_confirm_uses_only_human_supplied_fields(env: Env, ob):
    scan = ob.scan(JPEG, "image/jpeg", patient_id=env.patient)
    env.advance(minutes=3)
    out = ob.confirm_scan(scan["scan_id"], {"name": "Vitamin C chewable (demo)", "strength": "1 piece"},
                          confirmed=True, confirmed_by="Dr. Lee", patient_id=env.patient)
    assert out["name"] == "Vitamin C chewable (demo)" and out["source"] == "label_scan"
    assert out["instructions_text"] is None and out["warnings"] == []     # nothing copied from the extraction
    assert out["confirmed_by_user"] is True and out["confirmed_by"] == "Dr. Lee" and out["patient_id"] == env.patient
    assert env.medication(out["medication_id"]).scan_id == scan["scan_id"]
    row = _scan_row(env, scan["scan_id"])
    assert row.status == "CONFIRMED" and row.medication_id == out["medication_id"]
    assert row.reviewed_by == "Dr. Lee" and row.reviewed_at == env.clock.now()
    assert ob.list_scans()[0]["status"] == "CONFIRMED" and ob.list_scans()[0]["user_message"] is None
    # The confirmed medication is now a normal record: assignable and schedulable.
    env.compartments.assign(0, out["medication_id"], patient_id=env.patient, pill_count=5)
    assert env.scheduler.create_schedule(out["medication_id"], "21:00", patient_id=env.patient)["active"] is True


def test_confirm_requires_confirmation_and_a_name(env: Env, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    before = _medications(env)
    with pytest.raises(ValidationError):
        ob.confirm_scan(scan["scan_id"], {"name": "Zinc"}, confirmed=False)
    with pytest.raises(ValidationError):
        ob.confirm_scan(scan["scan_id"], {"strength": "1 piece"}, confirmed=True)   # name required
    assert _medications(env) == before and _scan_row(env, scan["scan_id"]).status == "PENDING_REVIEW"


def test_confirm_only_pending_scans(env: Env, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    ob.confirm_scan(scan["scan_id"], {"name": "Zinc"}, confirmed=True)
    with pytest.raises(ConflictError):
        ob.confirm_scan(scan["scan_id"], {"name": "Zinc again"}, confirmed=True)     # no double records
    failed = _service(env, None).scan(JPEG, "image/jpeg")
    with pytest.raises(ConflictError):
        ob.confirm_scan(failed["scan_id"], {"name": "Zinc"}, confirmed=True)
    with pytest.raises(NotFoundError):
        ob.confirm_scan(777, {"name": "Zinc"}, confirmed=True)


def test_reject_scan(env: Env, ob):
    scan = ob.scan(JPEG, "image/jpeg")
    out = ob.reject_scan(scan["scan_id"], by="  caregiver ")
    assert out["status"] == "REJECTED" and out["reviewed_at"] == env.clock.now().isoformat()
    assert _scan_row(env, scan["scan_id"]).reviewed_by == "caregiver"
    assert ob.reject_scan(scan["scan_id"])["status"] == "REJECTED"                   # idempotent
    with pytest.raises(ConflictError):
        ob.confirm_scan(scan["scan_id"], {"name": "Zinc"}, confirmed=True)
    failed = _service(env, None).scan(JPEG, "image/jpeg")
    assert ob.reject_scan(failed["scan_id"])["status"] == "REJECTED"                 # dismiss a failed scan
    confirmed = ob.scan(PNG, "image/png")
    ob.confirm_scan(confirmed["scan_id"], {"name": "Zinc"}, confirmed=True)
    with pytest.raises(ConflictError):
        ob.reject_scan(confirmed["scan_id"])
    with pytest.raises(NotFoundError):
        ob.reject_scan(31337)


def test_list_scans_newest_first_and_filtered(env: Env, ob):
    ids = []
    for _ in range(3):
        ids.append(ob.scan(JPEG, "image/jpeg")["scan_id"])
        env.advance(seconds=30)
    ob.reject_scan(ids[0])
    assert [s["scan_id"] for s in ob.list_scans()] == list(reversed(ids))
    assert [s["scan_id"] for s in ob.list_scans("pending_review")] == [ids[2], ids[1]]
    assert [s["scan_id"] for s in ob.list_scans("REJECTED")] == [ids[0]]
    with pytest.raises(ValidationError):
        ob.list_scans("APPROVED")
    assert ob.get_scan(ids[1])["status"] == "PENDING_REVIEW"
    with pytest.raises(NotFoundError):
        ob.get_scan(999)


def test_scans_are_scoped_to_one_patient(env: Env, ob):
    other = _other_patient(env)
    mine = ob.scan(JPEG, "image/jpeg", patient_id=env.patient)
    theirs = ob.scan(PNG, "image/png", patient_id=other)
    assert [s["scan_id"] for s in ob.list_scans(patient_id=other)] == [theirs["scan_id"]]
    assert [s["scan_id"] for s in ob.list_scans(patient_id=env.patient)] == [mine["scan_id"]]
    with pytest.raises(NotFoundError):
        ob.get_scan(mine["scan_id"], patient_id=other)
    with pytest.raises(NotFoundError):
        ob.confirm_scan(mine["scan_id"], {"name": "Zinc"}, confirmed=True, patient_id=other)
    with pytest.raises(NotFoundError):
        ob.reject_scan(mine["scan_id"], patient_id=other)
    med = ob.confirm_scan(theirs["scan_id"], {"name": "Pat's zinc"}, confirmed=True, patient_id=other)
    assert med["patient_id"] == other                                       # created for the scan's patient
    assert _scan_row(env, mine["scan_id"]).status == "PENDING_REVIEW"
