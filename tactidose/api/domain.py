"""Adapter between the routers and the domain services for operations outside the frozen
Protocols (container inventory, medications, schedules, device settings, dose views, reviews).

Every name the HTTP layer needs from ``medication/`` lives here, so a renamed domain method is a
one-line fix. The v2 domain services are patient-scoped (``patient_id=`` keyword: a record of
another patient is "not found"); on top of that :func:`require_owned` checks ownership in the
HTTP layer before anything is changed, so ``/api/patients/{pid}/…`` can never reach another
patient's records even if a service forgot to scope a query.

Caregiver operations raise ``medication.errors`` (mapped to 422/404/409) and are only called
after ``auth.deps`` has checked the caller.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import HTTPException

from tactidose.api import views
from tactidose.api.common import to_dict

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- ownership


def device_patient(services: Any) -> int | None:
    """The patient the configured device dispenses for."""
    return views.device_owner_id(services.db, services.settings)


def require_owned(services: Any, kind: str, record_id: int, pid: int, what: str) -> None:
    """404 unless ``record_id`` exists and belongs to patient ``pid``."""
    if views.owner_of(services.db, kind, record_id) != pid:
        raise HTTPException(404, f"{what} {record_id} not found.")


# --------------------------------------------------------------------------- containers


def list_containers(services: Any, pid: int) -> list[dict[str, Any]]:
    """API.md ``[ContainerInfo]`` (all slots, ordered; empty for a patient without a device)."""
    return [to_dict(c) for c in services.compartments.list(patient_id=pid)]


def update_container(services: Any, pid: int, slot: int, fields: dict[str, Any], *,
                     by_user_id: int) -> dict[str, Any]:
    """``fields`` ⊆ {medication_id (None = empty the container), pill_count, capacity,
    low_stock_threshold}, applied atomically by ``CompartmentService.update``."""
    return to_dict(services.compartments.update(slot, patient_id=pid, by_user_id=by_user_id, **fields))


def refill_container(services: Any, pid: int, slot: int, *, set_to: int | None, add: int | None,
                     by_user_id: int) -> dict[str, Any]:
    return to_dict(services.compartments.refill(slot, set=set_to, add=add, patient_id=pid, by_user_id=by_user_id))


# --------------------------------------------------------------------------- medications


def list_medications(services: Any, pid: int, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    return services.catalog.list(include_inactive, patient_id=pid)


def create_medication(services: Any, pid: int, fields: dict[str, Any], *, confirmed_by: str) -> dict[str, Any]:
    return services.catalog.create(fields, confirmed=True, confirmed_by=confirmed_by, patient_id=pid)


def update_medication(services: Any, pid: int, medication_id: int, fields: dict[str, Any], *,
                      confirmed: bool, confirmed_by: str) -> dict[str, Any]:
    require_owned(services, "medication", medication_id, pid, "Medication")
    return services.catalog.update(medication_id, fields, confirmed=confirmed, confirmed_by=confirmed_by,
                                   patient_id=pid)


def archive_medication(services: Any, pid: int, medication_id: int) -> None:
    require_owned(services, "medication", medication_id, pid, "Medication")
    services.catalog.archive(medication_id, patient_id=pid)


# --------------------------------------------------------------------------- schedules


def list_schedules(services: Any, pid: int, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    return services.scheduler.list_schedules(include_inactive, patient_id=pid)


def create_schedule(services: Any, pid: int, *, medication_id: int, time_of_day: str,
                    frequency: str | None, days_of_week: Any, created_by_user_id: int) -> dict[str, Any]:
    require_owned(services, "medication", medication_id, pid, "Medication")
    return services.scheduler.create_schedule(
        medication_id, time_of_day, frequency or "DAILY", days_of_week,
        created_by_user_id=created_by_user_id, patient_id=pid,
    )


def update_schedule(services: Any, pid: int, schedule_id: int, fields: dict[str, Any], *,
                    by_user_id: int) -> dict[str, Any]:
    require_owned(services, "schedule", schedule_id, pid, "Schedule")
    return services.scheduler.update_schedule(schedule_id, patient_id=pid, by_user_id=by_user_id, **fields)


def delete_schedule(services: Any, pid: int, schedule_id: int, *, by_user_id: int) -> None:
    """Soft delete (deactivation): dose history keeps its schedule."""
    require_owned(services, "schedule", schedule_id, pid, "Schedule")
    services.scheduler.delete_schedule(schedule_id, patient_id=pid, by_user_id=by_user_id)


# --------------------------------------------------------------------------- device settings


def get_settings(services: Any, pid: int) -> dict[str, Any]:
    """``{manual_cooldown_minutes, auto_drop_enabled, device_id, num_slots}``."""
    return to_dict(services.drops.get_settings(pid))


def update_settings(services: Any, pid: int, fields: dict[str, Any], *, by_user_id: int) -> dict[str, Any]:
    return to_dict(services.drops.update_settings(pid, by_user_id=by_user_id, **fields))


# --------------------------------------------------------------------------- drops & doses


def recent_drops(services: Any, pid: int, *, days: int, limit: int, statuses: set[str] | None) -> list[dict[str, Any]]:
    """PillDropViews newest first; one status is filtered by the service, several here."""
    if statuses and len(statuses) == 1:
        return services.drops.recent_drops(pid, days=days, limit=limit, status=next(iter(statuses)))
    rows = services.drops.recent_drops(pid, days=days, limit=limit)
    return [r for r in rows if r.get("status") in statuses] if statuses else rows


def next_dose(services: Any, pid: int) -> dict[str, Any] | None:
    """The next open scheduled dose (``DropService.next_scheduled_dose``, else from the status)."""
    fn = getattr(services.drops, "next_scheduled_dose", None)
    if callable(fn):
        return fn(pid)
    return services.drops.patient_status(pid).next_scheduled


def resolve_drop(services: Any, pid: int, drop_id: int, *, dropped: bool, note: str | None,
                 by_user_id: int) -> dict[str, Any]:
    require_owned(services, "drop", drop_id, pid, "Drop")
    return to_dict(services.drops.resolve_drop(drop_id, dropped=dropped, note=note, by_user_id=by_user_id,
                                               patient_id=pid))


def list_doses(services: Any, pid: int, local_date: date) -> list[dict[str, Any]]:
    return [to_dict(d) for d in services.drops.list_doses(pid, local_date)]


def skip_dose(services: Any, pid: int, event_id: int, *, note: str | None, by_user_id: int) -> dict[str, Any]:
    require_owned(services, "dose", event_id, pid, "Dose")
    return to_dict(services.drops.skip_dose(event_id, note=note, by_user_id=by_user_id, patient_id=pid))


# --------------------------------------------------------------------------- label scans (optional extra)


def scan_label(services: Any, pid: int, image: bytes, mime_type: str) -> dict[str, Any]:
    return services.onboarding.scan(image, mime_type, patient_id=pid)


def confirm_scan(services: Any, pid: int, scan_id: int, fields: dict[str, Any], *, confirmed_by: str) -> dict[str, Any]:
    require_owned(services, "scan", scan_id, pid, "Label scan")
    return services.onboarding.confirm_scan(scan_id, fields, confirmed=True, confirmed_by=confirmed_by,
                                            patient_id=pid)


def reject_scan(services: Any, pid: int, scan_id: int, *, by: str) -> dict[str, Any]:
    require_owned(services, "scan", scan_id, pid, "Label scan")
    return services.onboarding.reject_scan(scan_id, by=by, patient_id=pid)
