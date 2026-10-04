"""Shared scenario and fakes for the reports tests (no tests in this module).

``seed_report_scenario`` builds on ``tests.fakes.seed_v2`` and inserts, relative to the frozen
test clock (Mon 5 Oct 2026 07:55 America/Vancouver; 7-day period = Mon 28 Sep 07:55 -> now):

Doses (local)                     status            counted
Mon 28 Sep 07:50 Vitamin C        MISSED            no (before the period)
Mon 28 Sep 08:00 Vitamin C        DISPENSED 08:01   on time
Sat  3 Oct 08:00 Vitamin C        DISPENSED 08:02   on time
Sat  3 Oct 13:00 Calcium          DISPENSED 13:40   late
Sat  3 Oct 20:00 Omega-3          MISSED            missed
Sun  4 Oct 08:00 Vitamin C        TAKEN (07:50)     on time (early)
Sun  4 Oct 13:00 Calcium          CANCELLED         excluded
Sun  4 Oct 20:00 Omega-3          HARDWARE_ERROR    open, needs review
Mon  5 Oct 08:00 Vitamin C        DISPENSED 07:50   on time (future but satisfied early)
Mon  5 Oct 13:00 Calcium          SCHEDULED         no (future)

=> scheduled 7, dispensed 5 (4 on time, 1 late), missed 1, adherence 5/6.

Drops: Sep 27 manual DROPPED (outside); Oct 3 08:02 schedule DROPPED; Oct 3 13:40 schedule DROPPED;
Oct 3 20:00 schedule FAILED MOTOR_FAULT; Oct 4 07:50 manual DROPPED; Oct 4 08:00 schedule DENIED
ALREADY_SATISFIED; Oct 4 08:20 agent DENIED COOLDOWN; Oct 4 09:30 manual DENIED COOLDOWN (Calcium);
Oct 4 20:00 schedule UNCERTAIN (needs review); Oct 4 20:30 manual DENIED NEEDS_REVIEW; Oct 5 07:50
agent DROPPED.

Conversations: #1 Oct 4 08:19 (voice, headache, refused request), #2 Oct 5 07:49 (text, dropped);
one older message outside the period. Notifications: LOW_STOCK and MISSED_DOSE for 3 recipients each
(1 alert each after de-duplication), PILL_DROPPED (not an alert kind), an old LOW_STOCK (outside).
Inventory: 17 / 2 (low) / 0 (empty) pills; Calcium also has a WEEKLY Mon/Wed/Fri schedule; Omega-3
has an extra inactive schedule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from sqlalchemy import select

from tactidose.core.clock import Clock
from tactidose.db.models import (
    Compartment,
    Conversation,
    ConversationMessage,
    DoseEvent,
    Notification,
    PillDrop,
    Schedule,
)
from tests.fakes import seed_v2

HEADACHE = "Can I have another pill? My head hurts a little."
COOLDOWN_MSG = "You can have another pill at 8:50 AM."
AGENT_REPLY = ("Your Vitamin C dropped at 7:50 AM, so I can't give another one until 8:50 AM. "
               "For the headache, please contact your doctor.")


def local(clock: Clock, y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    """Aware UTC instant of a local wall-clock time in the test zone."""
    return datetime(y, mo, d, h, mi, tzinfo=clock.tz).astimezone(timezone.utc)


def seed_report_scenario(db, settings, clock: Clock) -> dict[str, Any]:
    ids = seed_v2(db, settings, now=clock.now() - timedelta(days=30))
    pid, dev = ids["patient_id"], ids["device_id"]
    m_vit, m_cal, m_omg = ids["med_ids"]
    s_vit, s_cal, s_omg = ids["schedule_ids"]
    t = lambda *a: local(clock, *a)

    with db.session() as s:
        weekly = Schedule(medication_id=m_cal, time_of_day="18:00", frequency="WEEKLY", days_of_week="MON,WED,FRI",
                          created_at=clock.now() - timedelta(days=30))
        inactive = Schedule(medication_id=m_omg, time_of_day="09:00", active=False,
                            created_at=clock.now() - timedelta(days=30))
        s.add_all([weekly, inactive])
        comps = {c.slot_number: c for c in s.scalars(select(Compartment)).all()}
        comps[0].pill_count, comps[1].pill_count, comps[2].pill_count = 17, 2, 0

        def dose(sched: int, med: int, slot: int, at: datetime, status: str, **kw: Any) -> None:
            s.add(DoseEvent(schedule_id=sched, medication_id=med, user_id=pid, device_id=dev, scheduled_at=at,
                            slot_number=slot, status=status, **kw))

        dose(s_vit, m_vit, 0, t(2026, 9, 28, 7, 50), "MISSED", missed_at=t(2026, 9, 28, 9, 50))
        dose(s_vit, m_vit, 0, t(2026, 9, 28, 8, 0), "DISPENSED", dispensed_at=t(2026, 9, 28, 8, 1))
        dose(s_vit, m_vit, 0, t(2026, 10, 3, 8, 0), "DISPENSED", dispensed_at=t(2026, 10, 3, 8, 2))
        dose(s_cal, m_cal, 1, t(2026, 10, 3, 13, 0), "DISPENSED", dispensed_at=t(2026, 10, 3, 13, 40))
        dose(s_omg, m_omg, 2, t(2026, 10, 3, 20, 0), "MISSED", missed_at=t(2026, 10, 3, 22, 0),
             hardware_result="ERR MOTOR_FAULT")
        dose(s_vit, m_vit, 0, t(2026, 10, 4, 8, 0), "TAKEN", dispensed_at=t(2026, 10, 4, 7, 50),
             confirmed_taken_at=t(2026, 10, 4, 8, 5))
        dose(s_cal, m_cal, 1, t(2026, 10, 4, 13, 0), "CANCELLED", cancelled_at=t(2026, 10, 4, 9, 0))
        dose(s_omg, m_omg, 2, t(2026, 10, 4, 20, 0), "HARDWARE_ERROR", needs_review=True,
             hardware_result="UNCERTAIN TIMEOUT")
        dose(s_vit, m_vit, 0, t(2026, 10, 5, 8, 0), "DISPENSED", dispensed_at=t(2026, 10, 5, 7, 50))
        dose(s_cal, m_cal, 1, t(2026, 10, 5, 13, 0), "SCHEDULED")

        def drop(at: datetime, source: str, status: str, slot: int, med: int, name: str, **kw: Any) -> None:
            s.add(PillDrop(patient_id=pid, device_id=dev, slot_number=slot, medication_id=med, medication_name=name,
                           source=source, status=status, requested_at=at,
                           completed_at=kw.pop("completed_at", at), **kw))

        vit, cal, omg = "Vitamin C (demo candy)", "Calcium (demo token)", "Omega-3 (demo candy)"
        drop(t(2026, 9, 27, 10, 0), "manual", "DROPPED", 0, m_vit, vit)
        drop(t(2026, 10, 3, 8, 2), "schedule", "DROPPED", 0, m_vit, vit, hardware_result="OK DROPPED")
        drop(t(2026, 10, 3, 13, 40), "schedule", "DROPPED", 1, m_cal, cal, hardware_result="OK DROPPED")
        drop(t(2026, 10, 3, 20, 0), "schedule", "FAILED", 2, m_omg, omg, reason="MOTOR_FAULT",
             hardware_result="ERR MOTOR_FAULT")
        drop(t(2026, 10, 4, 7, 50), "manual", "DROPPED", 0, m_vit, vit, requested_by_user_id=pid)
        drop(t(2026, 10, 4, 8, 0), "schedule", "DENIED", 0, m_vit, vit, reason="ALREADY_SATISFIED")
        drop(t(2026, 10, 4, 8, 20), "agent", "DENIED", 0, m_vit, vit, reason="COOLDOWN", requested_by_user_id=pid)
        drop(t(2026, 10, 4, 9, 30), "manual", "DENIED", 1, m_cal, cal, reason="COOLDOWN", requested_by_user_id=pid)
        drop(t(2026, 10, 4, 20, 0), "schedule", "UNCERTAIN", 2, m_omg, omg, reason="TIMEOUT", needs_review=True,
             completed_at=t(2026, 10, 4, 20, 1))
        drop(t(2026, 10, 4, 20, 30), "manual", "DENIED", 2, m_omg, omg, reason="NEEDS_REVIEW",
             requested_by_user_id=pid)
        drop(t(2026, 10, 5, 7, 50), "agent", "DROPPED", 0, m_vit, vit, requested_by_user_id=pid)

        old = Conversation(patient_id=pid, channel="text", started_at=t(2026, 9, 20, 9, 0),
                           last_message_at=t(2026, 9, 20, 9, 0))
        c1 = Conversation(patient_id=pid, channel="voice", started_at=t(2026, 10, 4, 8, 19),
                          last_message_at=t(2026, 10, 4, 8, 20))
        c2 = Conversation(patient_id=pid, channel="text", started_at=t(2026, 10, 5, 7, 49),
                          last_message_at=t(2026, 10, 5, 7, 50))
        s.add_all([old, c1, c2])
        s.flush()

        def msg(conv: Conversation, at: datetime, role: str, content: str = "", **kw: Any) -> None:
            s.add(ConversationMessage(conversation_id=conv.conversation_id, patient_id=pid, role=role,
                                      content=content, created_at=at, **kw))

        msg(old, t(2026, 9, 20, 9, 0), "user", "This old message is outside the period.", input_mode="text")
        base = t(2026, 10, 4, 8, 19)
        msg(c1, base, "user", HEADACHE, input_mode="voice")
        msg(c1, base + timedelta(seconds=2), "tool", tool_name="get_patient_status", tool_args={},
            tool_result={"cooldown_remaining_s": 1800})
        msg(c1, base + timedelta(seconds=3), "tool", tool_name="request_pill", tool_args={"container_number": 1},
            tool_result={"status": "DENIED", "reason": "COOLDOWN", "message": COOLDOWN_MSG})
        msg(c1, base + timedelta(seconds=4), "assistant", AGENT_REPLY, model="rules")
        base2 = t(2026, 10, 5, 7, 49)
        msg(c2, base2, "user", "Drop my vitamin please", input_mode="text")
        msg(c2, base2 + timedelta(seconds=30), "tool", tool_name="request_pill",
            tool_args={"medication_name": "vitamin"},
            tool_result={"status": "DROPPED", "message": "Vitamin C (demo candy) dropped from container 1."})
        msg(c2, base2 + timedelta(seconds=31), "assistant", "Done. Your Vitamin C dropped from container 1.",
            model="rules")

        for uid in (pid, ids["family_id"], ids["doctor_id"]):
            s.add(Notification(user_id=uid, patient_id=pid, kind="LOW_STOCK", title="Low stock",
                               body="Container 2 has 2 pills left.", data={"slot": 1},
                               created_at=t(2026, 10, 3, 14, 0)))
            s.add(Notification(user_id=uid, patient_id=pid, kind="MISSED_DOSE", title="Missed dose",
                               body="Omega-3 was not dropped.", created_at=t(2026, 10, 3, 22, 0)))
            s.add(Notification(user_id=uid, patient_id=pid, kind="PILL_DROPPED", title="Pill dropped",
                               created_at=t(2026, 10, 4, 7, 50)))
        s.add(Notification(user_id=pid, patient_id=pid, kind="LOW_STOCK", title="Low stock",
                           body="Old alert.", created_at=t(2026, 9, 1, 9, 0)))
    ids.update(conversation_ids=[c1.conversation_id, c2.conversation_id])
    return ids


# --------------------------------------------------------------------------- fakes

#: Minimal stand-in PDF for service tests that do not inspect the rendering.
FAKE_PDF = b"%PDF-1.4\n% fake report for tests\n%%EOF\n"


def fast_renderer(data: Any, stats: Any, narrative: Any, excerpts: Any) -> bytes:
    return FAKE_PDF


class FakeAPIError(Exception):
    """Shape of ``google.genai.errors.APIError`` that the narrator relies on (``code``, ``status``)."""

    def __init__(self, code: int, status: str, message: str = "") -> None:
        super().__init__(f"{code} {status}. {message}")
        self.code = code
        self.status = status


class FakeNotifications:
    """NotificationServiceAPI double that records calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def notify(self, **kw: Any) -> list[int]:
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("notification store down")
        return [len(self.calls)]

    def list_for_user(self, user_id: int, *, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int:
        return 0


class FakeGenaiResponse:
    def __init__(self, text: str | None = None, *, finish: str | None = "STOP", block: str | None = None) -> None:
        self._text = text
        self.candidates = [type("Cand", (), {"finish_reason": finish})()] if finish else []
        self.prompt_feedback = type("FB", (), {"block_reason": block})() if block else None

    @property
    def text(self) -> str | None:
        return self._text


class FakeGenaiClient:
    """Mimics ``google.genai.Client``: ``client.models.generate_content(model=, contents=, config=)``."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results) or [FakeGenaiResponse("- The patient asked for a pill once.")]
        self.calls: list[dict[str, Any]] = []
        self.models = self

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeSMTP:
    """Records the SMTP conversation; ``fail_on`` = method name that raises ``error``."""

    instances: ClassVar[list[FakeSMTP]] = []
    fail_on: ClassVar[str | None] = None
    error: ClassVar[BaseException | None] = None
    refused: ClassVar[dict[str, Any]] = {}

    def __init__(self, host: str, port: int, timeout: float | None = None, context: Any = None, **kw: Any) -> None:
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[Any] = []
        self.sent: Any = None
        type(self).instances.append(self)
        self._maybe_fail("__init__")

    def _maybe_fail(self, name: str) -> None:
        if type(self).fail_on == name and type(self).error is not None:
            raise type(self).error

    def ehlo(self, *a: Any) -> tuple[int, bytes]:
        self.calls.append("ehlo")
        return 250, b"ok"

    def starttls(self, context: Any = None) -> tuple[int, bytes]:
        self.calls.append("starttls")
        self.tls_context = context
        self._maybe_fail("starttls")
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append(("login", user, password))
        self._maybe_fail("login")
        return 235, b"ok"

    def send_message(self, msg: Any) -> dict[str, Any]:
        self.calls.append("send_message")
        self.sent = msg
        self._maybe_fail("send_message")
        return dict(type(self).refused)

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


def fake_smtp_classes() -> tuple[type, type]:
    """Fresh FakeSMTP / FakeSMTP_SSL subclasses (class-level state is per test)."""
    plain = type("FakeSMTPPlain", (FakeSMTP,), {"instances": [], "fail_on": None, "error": None, "refused": {}})
    ssl_cls = type("FakeSMTPSSL", (FakeSMTP,), {"instances": [], "fail_on": None, "error": None, "refused": {}})
    return plain, ssl_cls
