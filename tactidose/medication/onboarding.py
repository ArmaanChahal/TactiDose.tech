"""Label onboarding: photo -> AI extraction -> UNCONFIRMED ``LabelScan`` -> human review.

Handoff §18 activation rule::

    Gemini extraction -> UNCONFIRMED record -> Human review -> CONFIRMED record -> Eligible for scheduling

* :meth:`OnboardingService.scan` stores the image (``<scans_dir>/<sha256>.<ext>``) and
  the extractor's transcription as a ``LabelScan`` in ``PENDING_REVIEW`` — or ``FAILED``
  with a user message when the label could not be reliably read (handoff §30). It
  **never** creates a ``Medication``.
* :meth:`OnboardingService.confirm_scan` creates the medication from the fields the
  human supplied in the review form only (never from the extracted values) and marks
  the scan ``CONFIRMED`` in the same transaction.

The extractor is any ``LabelExtractor`` (Gemini, the deterministic fake, or None
when label scanning is disabled); its output is data, never an instruction.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.core.interfaces import ExtractionResult, LabelExtractor
from tactidose.db.devlog import log_event
from tactidose.db.models import LabelScan, LogCategory, MedicationSource, ScanStatus
from tactidose.db.session import Database
from tactidose.medication.catalog import MedicationCatalog, medication_to_dict
from tactidose.medication.compartments import ensure_device_rows, get_device, iso, log_device_changes
from tactidose.medication.errors import ConflictError, NotFoundError, ValidationError
from tactidose.medication.scheduler import is_id

log = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_MIME_TYPES",
    "ERROR_NOT_CONFIGURED",
    "NOT_AVAILABLE_MESSAGE",
    "OnboardingService",
    "REVIEW_MESSAGE",
    "normalize_mime",
    "scan_to_dict",
]

#: Accepted upload types -> file extension used when the image is stored.
ALLOWED_MIME_TYPES: dict[str, str] = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}

NOT_AVAILABLE_MESSAGE = (
    "Label scanning is not available (no Gemini API key). Please enter the information manually."
)
REVIEW_MESSAGE = "I detected a new medication label. Please review it before saving."
ERROR_NOT_CONFIGURED = "not_configured"


def normalize_mime(mime_type: object) -> str:
    """``"Image/JPEG; charset=x"`` -> ``"image/jpeg"``; ValidationError for unsupported types."""
    if not isinstance(mime_type, str):
        raise ValidationError("The image type is missing; upload a JPEG, PNG or WebP photo.")
    mime = mime_type.split(";", 1)[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if mime not in ALLOWED_MIME_TYPES:
        raise ValidationError(f"Unsupported image type {mime_type!r}; upload a JPEG, PNG or WebP photo.")
    return mime


def _user_message(scan: LabelScan) -> str | None:
    # There is no user_message column: it is derived from status + error code.
    if scan.status == ScanStatus.FAILED.value:
        if scan.error == ERROR_NOT_CONFIGURED:
            return NOT_AVAILABLE_MESSAGE
        return ExtractionResult.COULD_NOT_READ
    if scan.status == ScanStatus.PENDING_REVIEW.value:
        return REVIEW_MESSAGE
    return None


def scan_to_dict(scan: LabelScan) -> dict[str, Any]:
    """API.md ``LabelScan`` shape."""
    return {
        "scan_id": scan.scan_id,
        "status": scan.status,
        "extracted": scan.extracted,
        "model": scan.model,
        "error": scan.error,
        "user_message": _user_message(scan),
        "created_at": iso(scan.created_at),
        "reviewed_at": iso(scan.reviewed_at),
        "medication_id": scan.medication_id,
    }


class OnboardingService:
    def __init__(
        self,
        db: Database,
        extractor: LabelExtractor | None,
        catalog: MedicationCatalog,
        settings: Settings,
        clock: Clock,
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.extractor = extractor
        self.catalog = catalog
        self.settings = settings
        self.clock = clock
        self.bus = bus

    # ------------------------------------------------------------------ scan
    def scan(self, image: bytes, mime_type: str) -> dict[str, Any]:
        """Extract a label photo into an UNCONFIRMED scan. Never creates a Medication."""
        mime = normalize_mime(mime_type)
        if not isinstance(image, (bytes, bytearray, memoryview)):
            raise ValidationError("The uploaded image is not binary data.")
        data = bytes(image)
        if not data:
            raise ValidationError("The uploaded image is empty.")
        limit = self.settings.max_label_image_bytes
        if len(data) > limit:
            raise ValidationError(f"The image is too large ({len(data)} bytes); the limit is {limit} bytes.")
        sha = hashlib.sha256(data).hexdigest()
        image_path = self._save_image(sha, ALLOWED_MIME_TYPES[mime], data)

        status, extracted, model, error = self._extract(data, mime)
        with self.db.session() as s:
            dev, changes = ensure_device_rows(s, self.settings)
            log_device_changes(s, self.settings.device_id, changes)
            row = LabelScan(
                user_id=dev.user_id,
                status=status,
                model=(model or None) and model[:64],
                extracted=extracted,
                error=error,
                image_sha256=sha,
                image_path=image_path,
                created_at=self.clock.now(),
            )
            s.add(row)
            s.flush()
            log_event(s, self.settings.device_id, LogCategory.ADMIN, "LABEL_SCANNED",
                      {"scan_id": row.scan_id, "status": status, "model": row.model, "error": error,
                       "bytes": len(data), "mime": mime})
            out = scan_to_dict(row)
        log.info("label scan %s -> %s (%s)", out["scan_id"], status, error or "ok")
        self._publish(out["scan_id"])
        return out

    def _extract(self, data: bytes, mime: str) -> tuple[str, dict[str, Any] | None, str | None, str | None]:
        """-> (status, extracted, model, error). Any doubt -> FAILED (handoff §30 Gemini failure)."""
        failed = ScanStatus.FAILED.value
        if self.extractor is None:
            return failed, None, None, ERROR_NOT_CONFIGURED
        name = getattr(self.extractor, "name", None) or type(self.extractor).__name__
        try:
            result = self.extractor.extract(data, mime)
        except Exception as exc:  # noqa: BLE001 - extractor bugs/network errors must not break onboarding
            log.exception("label extractor %s raised", name)
            return failed, None, name, f"exception: {type(exc).__name__}"
        model = getattr(result, "model", None) or name
        if not getattr(result, "ok", False) or getattr(result, "data", None) is None:
            return failed, None, model, (getattr(result, "error", None) or "extraction_failed")
        if not result.data.legible:
            return failed, None, model, "not_legible"
        if not (result.data.medication_name or "").strip():
            return failed, None, model, "no_medication_name"
        return ScanStatus.PENDING_REVIEW.value, result.data.model_dump(), model, None

    def _save_image(self, sha: str, ext: str, data: bytes) -> str | None:
        try:
            directory = self.settings.scans_dir
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{sha}.{ext}"
            if not path.exists():
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
        except OSError:
            log.warning("could not store label image %s", sha[:12], exc_info=True)
            return None
        text = path.as_posix()
        return text if len(text) <= 255 else Path(path.name).as_posix()

    # ------------------------------------------------------------------ queries
    def list_scans(self, status: str | None = None) -> list[dict[str, Any]]:
        """Scans for this device's user, newest first (optionally one status)."""
        wanted: str | None = None
        if status is not None:
            raw = status.value if isinstance(status, ScanStatus) else status
            if not isinstance(raw, str) or raw.strip().upper() not in {s.value for s in ScanStatus}:
                raise ValidationError(
                    f"status must be one of {', '.join(s.value for s in ScanStatus)}."
                )
            wanted = raw.strip().upper()
        with self.db.session() as s:
            q = select(LabelScan).order_by(LabelScan.created_at.desc(), LabelScan.scan_id.desc())
            dev = get_device(s, self.settings)
            if dev is not None:
                q = q.where(LabelScan.user_id == dev.user_id)
            if wanted is not None:
                q = q.where(LabelScan.status == wanted)
            return [scan_to_dict(r) for r in s.scalars(q).all()]

    def get_scan(self, scan_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            return scan_to_dict(self._get(s, scan_id))

    # ------------------------------------------------------------------ review
    def confirm_scan(
        self,
        scan_id: int,
        fields: dict[str, Any],
        *,
        confirmed: bool,
        confirmed_by: str | None = None,
    ) -> dict[str, Any]:
        """Human-reviewed values -> confirmed Medication (the extraction itself is never copied)."""
        now = self.clock.now()
        with self.db.session() as s:
            scan = self._get(s, scan_id)
            if scan.status != ScanStatus.PENDING_REVIEW.value:
                raise ConflictError(f"Label scan {scan_id} is {scan.status}; only scans awaiting review can be confirmed.")
            if confirmed is not True:
                raise ValidationError(
                    "The scanned information must be reviewed and explicitly confirmed (confirmed: true)."
                )
            med = self.catalog.create_record(
                s, fields, confirmed=True, confirmed_by=confirmed_by,
                source=MedicationSource.LABEL_SCAN.value, scan_id=scan.scan_id,
            )
            # CAS on the scan so a double submit can never create two medications.
            res = s.execute(
                update(LabelScan)
                .where(LabelScan.scan_id == scan.scan_id, LabelScan.status == ScanStatus.PENDING_REVIEW.value)
                .values(status=ScanStatus.CONFIRMED.value, reviewed_at=now, reviewed_by=med.confirmed_by,
                        medication_id=med.medication_id)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:
                raise ConflictError(f"Label scan {scan_id} was reviewed concurrently.")
            log_event(s, self.settings.device_id, LogCategory.ADMIN, "LABEL_SCAN_CONFIRMED",
                      {"scan_id": scan.scan_id, "medication_id": med.medication_id, "by": med.confirmed_by})
            out = medication_to_dict(s, med, self.settings)
        self._publish(scan_id)
        if self.bus is not None:
            self.bus.publish(Topic.DATA_CHANGED, {"entity": "medication", "id": out["medication_id"]})
        return out

    def reject_scan(self, scan_id: int, *, by: str | None = None) -> dict[str, Any]:
        """PENDING_REVIEW / FAILED -> REJECTED (idempotent for REJECTED; CONFIRMED -> 409)."""
        reviewer = by.strip()[:120] if isinstance(by, str) and by.strip() else None
        with self.db.session() as s:
            scan = self._get(s, scan_id)
            if scan.status == ScanStatus.REJECTED.value:
                return scan_to_dict(scan)
            if scan.status == ScanStatus.CONFIRMED.value:
                raise ConflictError(f"Label scan {scan_id} was already confirmed; archive the medication instead.")
            res = s.execute(
                update(LabelScan)
                .where(
                    LabelScan.scan_id == scan.scan_id,
                    LabelScan.status.in_((ScanStatus.PENDING_REVIEW.value, ScanStatus.FAILED.value)),
                )
                .values(status=ScanStatus.REJECTED.value, reviewed_at=self.clock.now(), reviewed_by=reviewer)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:
                raise ConflictError(f"Label scan {scan_id} was reviewed concurrently.")
            log_event(s, self.settings.device_id, LogCategory.ADMIN, "LABEL_SCAN_REJECTED",
                      {"scan_id": scan.scan_id, "by": reviewer})
            out = scan_to_dict(s.get(LabelScan, scan.scan_id, populate_existing=True))
        self._publish(scan_id)
        return out

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _get(s: Session, scan_id: int) -> LabelScan:
        scan = s.get(LabelScan, scan_id) if is_id(scan_id) else None
        if scan is None:
            raise NotFoundError(f"Label scan {scan_id} not found.")
        return scan

    def _publish(self, scan_id: int | None) -> None:
        if self.bus is not None:
            self.bus.publish(Topic.DATA_CHANGED, {"entity": "scan", "id": scan_id})
