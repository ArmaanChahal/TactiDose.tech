"""Operational data model (TiDB in the cloud, SQLite locally).

Follows handoff §19 with a few additions needed for safety and integration:

* ``Device`` – a physical unit (compartments belong to a device).
* ``LabelScan`` – the *UNCONFIRMED* record produced by Gemini. Only a human
  confirmation turns it into a ``Medication`` (handoff §18 activation rule:
  "Only confirmed information is saved").
* ``DeviceLog`` – append-only audit trail (hardware lines, intents, safety refusals).
* ``AnalyticsOutbox`` – transactional outbox drained into Snowflake.

Conventions
-----------
* Every datetime column is :class:`UTCDateTime`: aware UTC in Python, naive UTC in SQL.
* Integer surrogate keys (AUTO_INCREMENT on TiDB). ``event_id`` is presented as
  ``dose_<id>`` in speech/logs.
* Status columns are plain strings holding enum values (portable across dialects).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tactidose.db.types import UTCDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- enums


class DoseStatus(str, Enum):
    SCHEDULED = "SCHEDULED"            # generated, window not open yet
    DUE = "DUE"                        # inside the dispensing window, not accessed
    DISPENSING = "DISPENSING"          # claimed; hardware command in flight
    DISPENSED = "DISPENSED"            # gate opened -> dose was accessible
    TAKEN = "TAKEN"                    # user confirmed
    MISSED = "MISSED"                  # window closed without access
    CANCELLED = "CANCELLED"            # skipped by caregiver / schedule removed
    HARDWARE_ERROR = "HARDWARE_ERROR"  # hardware failed; see needs_review


#: Statuses from which a (re)dispense may be attempted (HARDWARE_ERROR only if not needs_review).
DISPENSABLE_STATUSES = frozenset({DoseStatus.SCHEDULED, DoseStatus.DUE, DoseStatus.HARDWARE_ERROR})
#: Statuses meaning the compartment was opened for this dose.
ACCESSED_STATUSES = frozenset({DoseStatus.DISPENSED, DoseStatus.TAKEN})
#: Statuses that never change again (except caregiver corrections).
FINAL_STATUSES = frozenset({DoseStatus.TAKEN, DoseStatus.MISSED, DoseStatus.CANCELLED})


class ScanStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"  # UNCONFIRMED extraction awaiting a human
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"                  # could not reliably read label


class MedicationSource(str, Enum):
    MANUAL = "manual"
    LABEL_SCAN = "label_scan"
    DEMO_SEED = "demo_seed"


class Frequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"   # on the days listed in days_of_week


ALL_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class LogCategory(str, Enum):
    HARDWARE = "HARDWARE"
    INTENT = "INTENT"
    SPEECH = "SPEECH"
    SAFETY = "SAFETY"
    DOSE = "DOSE"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


# --------------------------------------------------------------------------- tables


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(120))
    accessibility_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    voice_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="user")
    medications: Mapped[list["Medication"]] = relationship(back_populates="user")


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    name: Mapped[str] = mapped_column(String(120), default="TactiDose")
    num_slots: Mapped[int] = mapped_column(Integer, default=6)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    user: Mapped[User] = relationship(back_populates="devices")
    compartments: Mapped[list["Compartment"]] = relationship(
        back_populates="device", order_by="Compartment.slot_number"
    )


class LabelScan(Base):
    """Gemini extraction = the UNCONFIRMED record. Never used for scheduling."""

    __tablename__ = "label_scans"

    scan_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    status: Mapped[str] = mapped_column(String(20), default=ScanStatus.PENDING_REVIEW.value, index=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: LabelExtraction as returned by the model (verbatim, for audit).
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Medication created from this scan (plain column: avoids a circular FK).
    medication_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Medication(Base):
    """A *confirmed* medication record (only humans create these)."""

    __tablename__ = "medications"

    medication_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    strength: Mapped[str | None] = mapped_column(String(120), nullable=True)
    instructions_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(32), default=MedicationSource.MANUAL.value)
    #: Must be True for the medication to be scheduled or dispensed.
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    scan_id: Mapped[int | None] = mapped_column(ForeignKey("label_scans.scan_id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="medications")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="medication")


class Compartment(Base):
    """One row per (device, slot). ``medication_id`` NULL = empty slot."""

    __tablename__ = "compartments"
    __table_args__ = (UniqueConstraint("device_id", "slot_number", name="uq_compartment_slot"),)

    compartment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    #: Protocol slot index 0..N-1 (user-facing "compartment slot_number+1").
    slot_number: Mapped[int] = mapped_column(Integer)
    medication_id: Mapped[int | None] = mapped_column(
        ForeignKey("medications.medication_id"), nullable=True, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    loaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    device: Mapped[Device] = relationship(back_populates="compartments")
    medication: Mapped[Medication | None] = relationship()


class Schedule(Base):
    __tablename__ = "schedules"

    schedule_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    medication_id: Mapped[int] = mapped_column(ForeignKey("medications.medication_id"), index=True)
    #: Local wall-clock time "HH:MM" (24h) in the device timezone.
    time_of_day: Mapped[str] = mapped_column(String(5))
    frequency: Mapped[str] = mapped_column(String(16), default=Frequency.DAILY.value)
    #: Comma list of MON..SUN; used when frequency == WEEKLY.
    days_of_week: Mapped[str] = mapped_column(String(32), default=",".join(ALL_DAYS))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    medication: Mapped[Medication] = relationship(back_populates="schedules")


class DoseEvent(Base):
    __tablename__ = "dose_events"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_at", name="uq_dose_schedule_time"),
        Index("ix_dose_device_time", "device_id", "scheduled_at"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.schedule_id"), index=True)
    medication_id: Mapped[int] = mapped_column(ForeignKey("medications.medication_id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime())
    #: Compartment resolved when the dose was (last) dispensed — re-resolved at dispense time.
    compartment_id: Mapped[int | None] = mapped_column(ForeignKey("compartments.compartment_id"), nullable=True)
    slot_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DoseStatus.SCHEDULED.value, index=True)
    dispensed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    confirmed_taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    missed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: Last hardware outcome, e.g. "OK GATE_OPEN", "ERR NOT_HOMED", "UNCERTAIN TIMEOUT".
    hardware_result: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: True when the outcome is uncertain (gate may have opened) or attempts exhausted.
    #: Such doses are never re-dispensed automatically (fail closed).
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dispense_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirm_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    schedule: Mapped[Schedule] = relationship()
    medication: Mapped[Medication] = relationship()

    @property
    def label(self) -> str:
        return f"dose_{self.event_id}"


class DeviceLog(Base):
    __tablename__ = "device_log"
    __table_args__ = (Index("ix_devlog_device_time", "device_id", "created_at"),)

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    category: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AnalyticsOutbox(Base):
    """Transactional outbox; rows are upserted into Snowflake by ``dedupe_key``."""

    __tablename__ = "analytics_outbox"
    __table_args__ = (Index("ix_outbox_pending", "sent_at", "outbox_id"),)

    outbox_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))          # "adherence" | "device_event"
    dedupe_key: Mapped[str] = mapped_column(String(128))   # e.g. "adherence:tactidose-001:184"
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
