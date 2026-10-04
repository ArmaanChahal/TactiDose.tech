"""Operational data model (TiDB in the cloud, SQLite locally) — v2.

v2 (2026-10-03 product structure): pill *drops* from 3 containers with inventory,
scheduled auto-drops, one global manual-drop cooldown, a conversational agent whose
patient conversations are logged, PDF reports stored in the DB (optionally emailed to
the doctor), and two portals (patient vs doctor/family) with login.

Tables
------
* ``users`` — every account (role patient | doctor | family) + login fields.
* ``care_links`` — which doctor/family account may see which patient.
* ``auth_sessions`` — server-side login sessions (hashed tokens).
* ``devices`` — physical unit bound to one patient; holds the global manual-drop cooldown.
* ``compartments`` — one row per (device, slot); medication assignment + pill inventory.
* ``medications`` — confirmed medication records (humans only create these).
* ``label_scans`` — UNCONFIRMED Gemini extractions (optional extra).
* ``schedules`` / ``dose_events`` — editable schedule and its materialised occurrences.
* ``pill_drops`` — every drop request and its outcome (DROPPED / DENIED / FAILED / UNCERTAIN).
* ``conversations`` / ``conversation_messages`` — patient ↔ agent chats (patients only).
* ``reports`` / ``report_deliveries`` — generated PDF reports (bytes in the DB) and emails.
* ``notifications`` — in-app notifications per recipient.
* ``device_log`` / ``analytics_outbox`` — audit trail and Snowflake outbox (optional extra).

Conventions: aware-UTC datetimes via :class:`UTCDateTime`; integer surrogate keys;
status/role columns hold enum *values* as strings (portable across dialects).
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, relationship

from tactidose.db.types import UTCDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


#: PDF bytes: LONGBLOB on MySQL/TiDB (BLOB is only 64 KB), BLOB elsewhere.
PdfBlob = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")


# --------------------------------------------------------------------------- enums


class Role(str, Enum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    FAMILY = "family"


CAREGIVER_ROLES = frozenset({Role.DOCTOR, Role.FAMILY})


class DoseStatus(str, Enum):
    SCHEDULED = "SCHEDULED"            # generated, window not open yet
    DUE = "DUE"                        # window open, not yet dropped
    DISPENSING = "DISPENSING"          # a drop for this dose is in flight
    DISPENSED = "DISPENSED"            # pill dropped (scheduled auto-drop or an earlier manual/agent drop)
    TAKEN = "TAKEN"                    # patient confirmed taking it (optional extra signal)
    MISSED = "MISSED"                  # window closed without a successful drop
    CANCELLED = "CANCELLED"            # skipped by doctor/family or schedule removed
    HARDWARE_ERROR = "HARDWARE_ERROR"  # last drop attempt failed; see needs_review


DISPENSABLE_STATUSES = frozenset({DoseStatus.SCHEDULED, DoseStatus.DUE, DoseStatus.HARDWARE_ERROR})
ACCESSED_STATUSES = frozenset({DoseStatus.DISPENSED, DoseStatus.TAKEN})
FINAL_STATUSES = frozenset({DoseStatus.TAKEN, DoseStatus.MISSED, DoseStatus.CANCELLED})


class DropSource(str, Enum):
    SCHEDULE = "schedule"    # automatic drop at the scheduled time
    MANUAL = "manual"        # patient pressed "Drop pill" in the app
    AGENT = "agent"          # the conversational agent requested it for the patient
    BUTTON = "button"        # physical confirm button on the device (optional)
    DEMO = "demo"            # demo operator panel


#: Sources subject to the global manual-drop cooldown.
COOLDOWN_SOURCES = frozenset({DropSource.MANUAL, DropSource.AGENT, DropSource.BUTTON})


class DropStatus(str, Enum):
    DROPPED = "DROPPED"        # device confirmed the drop
    DENIED = "DENIED"          # refused by a deterministic rule; no hardware command sent
    FAILED = "FAILED"          # hardware reported a definite failure; nothing dropped
    UNCERTAIN = "UNCERTAIN"    # outcome unknown (timeout/disconnect/stop mid-drop) -> fail closed


class DenyReason(str, Enum):
    COOLDOWN = "COOLDOWN"                      # global manual cooldown still running
    EMPTY = "EMPTY"                            # container pill_count is 0
    NO_MEDICATION = "NO_MEDICATION"            # slot has no (active, confirmed) medication
    UNKNOWN_MEDICATION = "UNKNOWN_MEDICATION"  # requested name/slot does not match
    ALREADY_SATISFIED = "ALREADY_SATISFIED"    # scheduled dose already dropped
    IN_PROGRESS = "IN_PROGRESS"                # another drop is running
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"  # disconnected / FAULT / could not home
    NEEDS_REVIEW = "NEEDS_REVIEW"              # an uncertain drop awaits doctor/family review
    NOT_ALLOWED = "NOT_ALLOWED"                # caller's role may not drop for this patient
    DB_ERROR = "DB_ERROR"                      # state unknown -> fail closed


class NotificationKind(str, Enum):
    PILL_DROPPED = "PILL_DROPPED"
    DROP_DENIED = "DROP_DENIED"
    DROP_FAILED = "DROP_FAILED"
    DROP_UNCERTAIN = "DROP_UNCERTAIN"
    LOW_STOCK = "LOW_STOCK"
    EMPTY = "EMPTY"
    MISSED_DOSE = "MISSED_DOSE"
    REPORT_READY = "REPORT_READY"
    REPORT_SENT = "REPORT_SENT"
    DEVICE_ALERT = "DEVICE_ALERT"


class ScanStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class MedicationSource(str, Enum):
    MANUAL = "manual"
    LABEL_SCAN = "label_scan"
    DEMO_SEED = "demo_seed"


class Frequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


ALL_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class LogCategory(str, Enum):
    HARDWARE = "HARDWARE"
    INTENT = "INTENT"
    SPEECH = "SPEECH"
    SAFETY = "SAFETY"
    DOSE = "DOSE"
    ADMIN = "ADMIN"
    AUTH = "AUTH"
    AGENT = "AGENT"
    REPORT = "REPORT"
    SYSTEM = "SYSTEM"


# --------------------------------------------------------------------------- accounts


class User(Base):
    """Every account. ``role`` decides which portal the person sees."""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16), default=Role.PATIENT.value, index=True)
    #: Login identifier (lower-cased). NULL only for the legacy default device user.
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Patients only: secret code a doctor/family member needs (with the patient ID) to link.
    link_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    accessibility_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    voice_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="user")
    medications: Mapped[list["Medication"]] = relationship(back_populates="user")


class CareLink(Base):
    """Doctor/family account ``caregiver_id`` may view (and edit) patient ``patient_id``."""

    __tablename__ = "care_links"
    __table_args__ = (UniqueConstraint("caregiver_id", "patient_id", name="uq_care_link"),)

    link_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    caregiver_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    relationship_kind: Mapped[str] = mapped_column(String(16))  # "doctor" | "family"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuthSession(Base):
    """Server-side session. Only the SHA-256 of the bearer token is stored."""

    __tablename__ = "auth_sessions"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


# --------------------------------------------------------------------------- device & inventory


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The patient this device dispenses for.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    name: Mapped[str] = mapped_column(String(120), default="TactiDose")
    num_slots: Mapped[int] = mapped_column(Integer, default=3)
    #: Global cooldown: after ANY drop, manual/agent drops of ANY pill are refused for this long.
    manual_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    #: Scheduled doses drop automatically even if the patient forgets.
    auto_drop_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="devices")
    compartments: Mapped[list["Compartment"]] = relationship(
        back_populates="device", order_by="Compartment.slot_number"
    )


class LabelScan(Base):
    """Gemini extraction = the UNCONFIRMED record (optional extra). Never used for scheduling."""

    __tablename__ = "label_scans"

    scan_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    status: Mapped[str] = mapped_column(String(20), default=ScanStatus.PENDING_REVIEW.value, index=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    medication_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Medication(Base):
    """A confirmed medication record (only humans create these)."""

    __tablename__ = "medications"

    medication_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    strength: Mapped[str | None] = mapped_column(String(120), nullable=True)
    instructions_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(32), default=MedicationSource.MANUAL.value)
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
    """One row per (device, slot): which medication it holds and how many pills are left."""

    __tablename__ = "compartments"
    __table_args__ = (UniqueConstraint("device_id", "slot_number", name="uq_compartment_slot"),)

    compartment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    #: Protocol slot index 0..N-1 (user-facing "container slot_number+1").
    slot_number: Mapped[int] = mapped_column(Integer)
    medication_id: Mapped[int | None] = mapped_column(
        ForeignKey("medications.medication_id"), nullable=True, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Pills currently in the container (decremented on every DROPPED, set on refill).
    pill_count: Mapped[int] = mapped_column(Integer, default=0)
    capacity: Mapped[int] = mapped_column(Integer, default=30)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=3)
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
    days_of_week: Mapped[str] = mapped_column(String(32), default=",".join(ALL_DAYS))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    compartment_id: Mapped[int | None] = mapped_column(ForeignKey("compartments.compartment_id"), nullable=True)
    slot_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DoseStatus.SCHEDULED.value, index=True)
    #: The pill_drops row that satisfied this dose (auto-drop or an earlier manual/agent drop).
    drop_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispensed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    confirmed_taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    missed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: Next automatic retry after a failed scheduled drop (None = no retry pending).
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    hardware_result: Mapped[str | None] = mapped_column(String(255), nullable=True)
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


class PillDrop(Base):
    """Every drop *request* and its outcome; the source of truth for pills taken."""

    __tablename__ = "pill_drops"
    __table_args__ = (
        Index("ix_drop_patient_time", "patient_id", "requested_at"),
        Index("ix_drop_device_status", "device_id", "status"),
    )

    drop_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    slot_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compartment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    medication_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Snapshot so reports stay readable even if the medication is renamed/archived later.
    medication_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(16))
    requested_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dose_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hardware_result: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pill_count_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pill_count_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: True while an UNCERTAIN drop has not been resolved by doctor/family.
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


# --------------------------------------------------------------------------- conversations


class Conversation(Base):
    """A patient-agent conversation. Only patient conversations are stored."""

    __tablename__ = "conversations"

    conversation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="text")  # text | voice | mixed
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_message_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation", order_by="ConversationMessage.message_id"
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (Index("ix_msg_patient_time", "patient_id", "created_at"),)

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.conversation_id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    #: "user" (patient), "assistant" (agent reply) or "tool" (a tool call and its result).
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    input_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # text | voice
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_args: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# --------------------------------------------------------------------------- reports


class Report(Base):
    """Generated adherence report. The PDF itself lives in the DB (``pdf``, deferred load)."""

    __tablename__ = "reports"
    __table_args__ = (Index("ix_report_patient_time", "patient_id", "created_at"),)

    report_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    days: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(UTCDateTime())
    period_end: Mapped[datetime] = mapped_column(UTCDateTime())
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="READY")  # READY | FAILED
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # gemini | rules
    pdf: Mapped[bytes | None] = deferred(mapped_column(PdfBlob, nullable=True))
    pdf_size: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ReportDelivery(Base):
    __tablename__ = "report_deliveries"

    delivery_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.report_id"), index=True)
    to_email: Mapped[str] = mapped_column(String(254))
    to_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    #: SENT (SMTP accepted), SAVED (no SMTP configured: .eml written to data/outbox), FAILED
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


# --------------------------------------------------------------------------- notifications & audit


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_time", "user_id", "created_at"),)

    notification_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Recipient.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    #: The patient the notification is about.
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


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
    """Transactional outbox; rows are upserted into Snowflake by ``dedupe_key`` (optional extra)."""

    __tablename__ = "analytics_outbox"
    __table_args__ = (Index("ix_outbox_pending", "sent_at", "outbox_id"),)

    outbox_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
