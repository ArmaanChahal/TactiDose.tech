"""Medication catalog — confirmed records only (handoff §18 activation rule, §3).

There is no such thing as an unconfirmed ``Medication``: :meth:`MedicationCatalog.create`
refuses anything not explicitly confirmed by a person, and every change to the
label information (name / strength / instructions / warnings) must be
re-confirmed, which re-stamps ``confirmed_at`` / ``confirmed_by``. Unconfirmed,
machine-extracted data lives only in ``LabelScan`` (see ``onboarding``).

The catalog never decides dosage: it stores exactly the text a person entered.
Archiving is the only delete: the medication becomes inactive, its compartment
is cleared, its schedules are deactivated and its open (not yet accessed)
SCHEDULED/DUE dose events are cancelled.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from tactidose.config import Settings
from tactidose.core.bus import EventBus, Topic
from tactidose.core.clock import Clock
from tactidose.db.devlog import log_event
from tactidose.db.models import (
    Compartment,
    DoseEvent,
    DoseStatus,
    LabelScan,
    LogCategory,
    Medication,
    MedicationSource,
    Schedule,
)
from tactidose.db.session import Database
from tactidose.hardware.protocol import compartment_number
from tactidose.medication.compartments import (
    assigned_slots,
    ensure_device_rows,
    get_device,
    iso,
    log_device_changes,
)
from tactidose.medication.errors import NotFoundError, ValidationError
from tactidose.medication.scheduler import (
    cas_transition,
    dose_update_payload,
    is_id,
    publish_all,
    record_dose_change,
    schedule_to_dict,
)

log = logging.getLogger(__name__)

__all__ = [
    "MAX_INSTRUCTIONS",
    "MAX_NAME",
    "MAX_STRENGTH",
    "MAX_WARNING_LENGTH",
    "MAX_WARNINGS",
    "MedicationCatalog",
    "medication_to_dict",
    "validate_medication_fields",
]

MAX_NAME = 200
MAX_STRENGTH = 120
MAX_INSTRUCTIONS = 2000
MAX_WARNINGS = 20
MAX_WARNING_LENGTH = 500
MAX_CONFIRMED_BY = 120

_CONTENT_FIELDS = ("name", "strength", "instructions_text", "warnings")
#: Keys an API body may carry alongside the content fields; they are handled by keyword args.
_META_FIELDS = frozenset({"confirmed", "confirmed_by"})


# --------------------------------------------------------------------------- validation


def _clean_text(value: object, field: str, max_len: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValidationError(f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.")
    text = value.strip()
    if not text:
        if required:
            raise ValidationError(f"{field} must not be empty.")
        return None
    if len(text) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters.")
    return text


def _clean_warnings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValidationError("warnings must be a list of text items.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("Each warning must be text.")
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_WARNING_LENGTH:
            raise ValidationError(f"Each warning must be at most {MAX_WARNING_LENGTH} characters.")
        out.append(text)
    if len(out) > MAX_WARNINGS:
        raise ValidationError(f"At most {MAX_WARNINGS} warnings are allowed.")
    return out


def validate_medication_fields(fields: object, *, partial: bool) -> dict[str, Any]:
    """Validate/normalise label fields. ``partial`` = only the keys present (PATCH).

    ``instructions`` is accepted as an alias of ``instructions_text``; the keys
    ``confirmed`` / ``confirmed_by`` are ignored here (they are explicit arguments).
    """
    if not isinstance(fields, Mapping):
        raise ValidationError("Medication fields must be an object.")
    data = dict(fields)
    if "instructions" in data:
        alias = data.pop("instructions")
        data.setdefault("instructions_text", alias)
    unknown = set(data) - set(_CONTENT_FIELDS) - _META_FIELDS
    if unknown:
        raise ValidationError(f"Unknown medication field(s): {', '.join(sorted(unknown))}.")
    out: dict[str, Any] = {}
    if not partial or "name" in data:
        out["name"] = _clean_text(data.get("name"), "name", MAX_NAME, required=True)
    if not partial or "strength" in data:
        out["strength"] = _clean_text(data.get("strength"), "strength", MAX_STRENGTH, required=False)
    if not partial or "instructions_text" in data:
        out["instructions_text"] = _clean_text(
            data.get("instructions_text"), "instructions_text", MAX_INSTRUCTIONS, required=False
        )
    if not partial or "warnings" in data:
        out["warnings"] = _clean_warnings(data.get("warnings"))
    return out


def _require_confirmed(confirmed: object) -> None:
    if confirmed is not True:
        raise ValidationError(
            "Medication information must be explicitly confirmed by a person (confirmed: true) before it is saved."
        )


def _clean_confirmed_by(value: object) -> str | None:
    return _clean_text(value, "confirmed_by", MAX_CONFIRMED_BY, required=False)


# --------------------------------------------------------------------------- serialisation


def medication_to_dict(
    session: Session, med: Medication, settings: Settings, slots: dict[int, tuple[int, int]] | None = None
) -> dict[str, Any]:
    """API.md ``Medication`` shape (``schedules`` lists the active schedules)."""
    slot_map = slots if slots is not None else assigned_slots(session, settings)
    resolved = slot_map.get(med.medication_id)
    slot = resolved[0] if resolved is not None else None
    schedules = sorted((sc for sc in med.schedules if sc.active), key=lambda sc: (sc.time_of_day, sc.schedule_id))
    return {
        "medication_id": med.medication_id,
        "name": med.name,
        "strength": med.strength,
        "instructions_text": med.instructions_text,
        "warnings": list(med.warnings or []),
        "source": med.source,
        "confirmed_by_user": bool(med.confirmed_by_user),
        "confirmed_by": med.confirmed_by,
        "confirmed_at": iso(med.confirmed_at),
        "active": bool(med.active),
        "slot": slot,
        "compartment_number": compartment_number(slot) if slot is not None else None,
        "schedules": [schedule_to_dict(sc) for sc in schedules],
    }


# --------------------------------------------------------------------------- service


class MedicationCatalog:
    def __init__(self, db: Database, settings: Settings, clock: Clock, bus: EventBus | None = None) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.bus = bus

    # ------------------------------------------------------------------ queries
    def list(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as s:
            dev = get_device(s, self.settings)
            if dev is None:
                return []
            q = (
                select(Medication)
                .options(selectinload(Medication.schedules))
                .where(Medication.user_id == dev.user_id)
                .order_by(func.lower(Medication.name), Medication.medication_id)
            )
            if not include_inactive:
                q = q.where(Medication.active.is_(True))
            slots = assigned_slots(s, self.settings)
            return [medication_to_dict(s, m, self.settings, slots) for m in s.scalars(q).all()]

    def get(self, medication_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            med = s.get(Medication, medication_id) if is_id(medication_id) else None
            if med is None:
                raise NotFoundError(f"Medication {medication_id} not found.")
            return medication_to_dict(s, med, self.settings)

    # ------------------------------------------------------------------ commands
    def create(
        self,
        fields: dict[str, Any],
        *,
        confirmed: bool,
        confirmed_by: str | None = None,
        source: str = "manual",
        scan_id: int | None = None,
    ) -> dict[str, Any]:
        with self.db.session() as s:
            med = self.create_record(
                s, fields, confirmed=confirmed, confirmed_by=confirmed_by, source=source, scan_id=scan_id
            )
            out = medication_to_dict(s, med, self.settings)
        self._publish_data("medication", out["medication_id"])
        return out

    def create_record(
        self,
        session: Session,
        fields: dict[str, Any],
        *,
        confirmed: bool,
        confirmed_by: str | None = None,
        source: str = "manual",
        scan_id: int | None = None,
    ) -> Medication:
        """Insert a confirmed medication inside the caller's transaction (no commit, no publish).

        Used by :meth:`create` and by onboarding, which must mark the scan CONFIRMED
        in the same transaction.
        """
        _require_confirmed(confirmed)
        values = validate_medication_fields(fields, partial=False)
        by = _clean_confirmed_by(confirmed_by)
        valid_sources = {m.value for m in MedicationSource}
        src = source.value if isinstance(source, MedicationSource) else source
        if src not in valid_sources:
            raise ValidationError(f"source must be one of {', '.join(sorted(valid_sources))}.")
        if scan_id is not None and (not is_id(scan_id) or session.get(LabelScan, scan_id) is None):
            raise NotFoundError(f"Label scan {scan_id} not found.")
        dev, changes = ensure_device_rows(session, self.settings)
        log_device_changes(session, self.settings.device_id, changes)
        now = self.clock.now()
        med = Medication(
            user_id=dev.user_id,
            name=values["name"],
            strength=values["strength"],
            instructions_text=values["instructions_text"],
            warnings=values["warnings"],
            source=src,
            confirmed_by_user=True,
            confirmed_by=by,
            confirmed_at=now,
            scan_id=scan_id,
            active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(med)
        session.flush()
        log_event(session, self.settings.device_id, LogCategory.ADMIN, "MEDICATION_CREATED",
                  {"medication_id": med.medication_id, "source": src, "scan_id": scan_id, "confirmed_by": by})
        log.info("medication %s created (source=%s, confirmed by %s)", med.medication_id, src, by)
        return med

    def update(
        self,
        medication_id: int,
        fields: dict[str, Any],
        *,
        confirmed: bool,
        confirmed_by: str | None = None,
    ) -> dict[str, Any]:
        values = validate_medication_fields(fields, partial=True)
        with self.db.session() as s:
            med = s.get(Medication, medication_id) if is_id(medication_id) else None
            if med is None:
                raise NotFoundError(f"Medication {medication_id} not found.")
            if not values:
                return medication_to_dict(s, med, self.settings)
            # Any change to label information is a new human confirmation (handoff §18).
            _require_confirmed(confirmed)
            now = self.clock.now()
            for key, value in values.items():
                setattr(med, key, value)
            med.confirmed_by_user = True
            med.confirmed_by = _clean_confirmed_by(confirmed_by)
            med.confirmed_at = now
            med.updated_at = now
            s.flush()
            log_event(s, self.settings.device_id, LogCategory.ADMIN, "MEDICATION_UPDATED",
                      {"medication_id": med.medication_id, "fields": sorted(values), "confirmed_by": med.confirmed_by})
            out = medication_to_dict(s, med, self.settings)
        self._publish_data("medication", medication_id)
        return out

    def archive(self, medication_id: int) -> None:
        """Deactivate; clear its compartment; deactivate schedules; cancel open doses."""
        now = self.clock.now()
        payloads: list[dict[str, Any]] = []
        with self.db.session() as s:
            med = s.get(Medication, medication_id) if is_id(medication_id) else None
            if med is None:
                raise NotFoundError(f"Medication {medication_id} not found.")
            med.active = False
            med.updated_at = now
            cleared = []
            for comp in s.scalars(select(Compartment).where(Compartment.medication_id == med.medication_id)).all():
                comp.medication_id = None
                comp.loaded_at = None
                cleared.append(comp.slot_number)
            deactivated = []
            for sched in s.scalars(
                select(Schedule).where(Schedule.medication_id == med.medication_id, Schedule.active.is_(True))
            ).all():
                sched.active = False
                sched.updated_at = now
                deactivated.append(sched.schedule_id)
            s.flush()
            open_events = s.scalars(
                select(DoseEvent)
                .options(selectinload(DoseEvent.medication))
                .where(
                    DoseEvent.medication_id == med.medication_id,
                    DoseEvent.status.in_((DoseStatus.SCHEDULED.value, DoseStatus.DUE.value)),
                )
                .order_by(DoseEvent.scheduled_at)
            ).all()
            for ev in open_events:
                previous = ev.status
                changed = cas_transition(
                    s, ev.event_id, previous,
                    {"status": DoseStatus.CANCELLED.value, "cancelled_at": now, "review_note": "medication archived"},
                )
                if changed is None:
                    continue
                record_dose_change(s, changed, settings=self.settings, clock=self.clock, action="DOSE_CANCELLED",
                                   detail={"previous": previous, "why": "medication archived"})
                payloads.append(dose_update_payload(changed, self.clock, previous=previous, action="cancelled"))
            log_event(s, self.settings.device_id, LogCategory.ADMIN, "MEDICATION_ARCHIVED",
                      {"medication_id": med.medication_id, "cleared_slots": cleared,
                       "deactivated_schedules": deactivated, "cancelled_events": len(payloads)})
        log.info("medication %s archived (slots %s cleared, %d dose(s) cancelled)", medication_id, cleared, len(payloads))
        publish_all(self.bus, Topic.DOSE_UPDATED, payloads)
        self._publish_data("medication", medication_id)
        if cleared:
            self._publish_data("compartment", None)
        if deactivated:
            self._publish_data("schedule", None)

    # ------------------------------------------------------------------ internals
    def _publish_data(self, entity: str, entity_id: int | None) -> None:
        if self.bus is not None:
            self.bus.publish(Topic.DATA_CHANGED, {"entity": entity, "id": entity_id})
