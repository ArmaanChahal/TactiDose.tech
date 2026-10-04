"""NotificationService: recipients per kind (patient / linked caregivers / explicit users), live
push per recipient, per-user listing and read state, transactional staging, wording helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from tactidose.core.bus import Topic
from tactidose.db.models import CareLink, Notification, NotificationKind, User
from tactidose.medication.errors import ValidationError
from tactidose.medication.notifications import (
    KIND_AUDIENCE,
    NotificationService,
    PendingNotifications,
    clock_label,
    duration_label,
    notification_to_dict,
    plural,
)
from tests.test_med_support import Env, env, env_template  # noqa: F401 - fixtures

NOTIFICATION_KEYS = {"notification_id", "user_id", "patient_id", "kind", "title", "body", "data", "created_at",
                     "read_at"}


def add_user(e: Env, name: str, role: str, email: str, *, link_to: int | None = None) -> int:
    with e.db.session() as s:
        u = User(display_name=name, role=role, email=email)
        s.add(u)
        s.flush()
        if link_to is not None:
            s.add(CareLink(caregiver_id=u.user_id, patient_id=link_to, relationship_kind=role))
        return u.user_id


def notify(e: Env, **kw: Any) -> list[int]:
    base: dict[str, Any] = {"patient_id": e.patient, "kind": "LOW_STOCK", "title": "Low stock", "body": "Two left."}
    base.update(kw)
    return e.notifications.notify(**base)


def owners(e: Env, ids: list[int]) -> list[int]:
    with e.db.session() as s:
        return [s.get(Notification, i).user_id for i in ids]


# --------------------------------------------------------------------------- recipients


def test_notify_reaches_the_patient_and_linked_caregivers(env: Env):
    sub = env.subscribe(Topic.NOTIFICATION)
    ids = notify(env, data={"slot": 1})
    assert owners(env, ids) == [env.patient, env.family, env.doctor]
    pushed = [e.data for e in sub.drain()]
    assert [p["user_id"] for p in pushed] == [env.patient, env.family, env.doctor]
    assert all(set(p) == NOTIFICATION_KEYS for p in pushed)
    assert pushed[0] == {
        "notification_id": ids[0], "user_id": env.patient, "patient_id": env.patient, "kind": "LOW_STOCK",
        "title": "Low stock", "body": "Two left.", "data": {"slot": 1},
        "created_at": env.clock.now().isoformat(), "read_at": None,
    }


def test_audience_flags(env: Env):
    assert owners(env, notify(env, to_patient=False)) == [env.family, env.doctor]
    assert owners(env, notify(env, to_caregivers=False)) == [env.patient]
    assert notify(env, to_patient=False, to_caregivers=False) == []


def test_explicit_recipients_stay_within_the_care_circle(env: Env):
    stranger = add_user(env, "Stranger", "doctor", "stranger@test.tactidose")
    ids = notify(env, kind="REPORT_READY", title="Report ready", user_ids=[env.doctor, stranger, env.doctor, True])
    assert owners(env, ids) == [env.doctor]                      # the stranger is never told anything
    assert owners(env, notify(env, kind="REPORT_SENT", title="Report sent", user_ids=[env.patient])) == [env.patient]


def test_inactive_unlinked_and_non_caregiver_accounts_are_skipped(env: Env):
    other_patient = add_user(env, "Pat Two", "patient", "pat2@test.tactidose")
    with env.db.session() as s:
        s.get(User, env.family).is_active = False
        # a (bogus) link from another patient account must not make them a caregiver
        s.add(CareLink(caregiver_id=other_patient, patient_id=env.patient, relationship_kind="family"))
    assert owners(env, notify(env)) == [env.patient, env.doctor]
    assert notify(env, patient_id=987654) == []                  # unknown patient: nobody


@pytest.mark.parametrize("kw", [
    dict(kind="SOMETHING"), dict(kind="DROP_DENIED"), dict(kind=7), dict(title=""), dict(title="   "),
    dict(title=None), dict(body=5), dict(data=["x"]),
])
def test_invalid_notifications_are_rejected(env: Env, kw):
    with pytest.raises(ValidationError):
        notify(env, **kw)
    assert env.notes() == []


def test_long_titles_are_truncated_and_enum_kinds_accepted(env: Env):
    ids = notify(env, kind=NotificationKind.EMPTY, title="T" * 300)
    with env.db.session() as s:
        row = s.get(Notification, ids[0])
        assert row.kind == "EMPTY" and len(row.title) == 200


def test_kind_audience_table_covers_every_kind():
    assert set(KIND_AUDIENCE) == {k.value for k in NotificationKind}


# --------------------------------------------------------------------------- listing & read state


def test_list_for_user_is_newest_first_and_private(env: Env):
    first = notify(env, title="First")
    env.advance(minutes=1)
    second = notify(env, title="Second", to_caregivers=False)
    mine = env.notifications.list_for_user(env.patient)
    assert [n["notification_id"] for n in mine] == [second[0], first[0]]
    assert [n["title"] for n in env.notifications.list_for_user(env.doctor)] == ["First"]
    assert env.notifications.list_for_user(env.patient, limit=1)[0]["title"] == "Second"
    assert len(env.notifications.list_for_user(env.patient, limit=10_000)) == 2      # clamped, not an error
    assert env.notifications.list_for_user(424242) == []
    with pytest.raises(ValidationError):
        env.notifications.list_for_user(env.patient, limit="5")  # type: ignore[arg-type]


def test_unlinked_caregivers_no_longer_see_old_notifications(env: Env):
    notify(env)
    assert len(env.notifications.list_for_user(env.family)) == 1
    with env.db.session() as s:
        s.query(CareLink).filter(CareLink.caregiver_id == env.family).delete()
    assert env.notifications.list_for_user(env.family) == []
    assert env.notifications.unread_count(env.family) == 0


def test_mark_read_and_unread_filter(env: Env):
    a = notify(env, title="A")
    b = notify(env, title="B")
    assert env.notifications.unread_count(env.patient) == 2
    assert env.notifications.unread_count(env.doctor, patient_id=env.patient) == 2
    assert env.notifications.mark_read(env.patient, [a[0], b[1]]) == 1          # b[1] belongs to the family member
    assert [n["title"] for n in env.notifications.list_for_user(env.patient, unread_only=True)] == ["B"]
    assert env.notifications.mark_read(env.patient) == 1
    assert env.notifications.mark_read(env.patient) == 0
    assert env.notifications.mark_read(env.patient, []) == 0
    read = env.notifications.list_for_user(env.patient)
    assert all(n["read_at"] == env.clock.now().isoformat() for n in read)
    assert env.notifications.unread_count(env.family) == 2                      # others are untouched
    for bad in ("1", [1, "2"], [True], 5):
        with pytest.raises(ValidationError):
            env.notifications.mark_read(env.patient, bad)  # type: ignore[arg-type]


def test_list_for_user_filters_by_patient(env: Env):
    other = add_user(env, "Pat Two", "patient", "pat2@test.tactidose")
    with env.db.session() as s:
        s.add(CareLink(caregiver_id=env.doctor, patient_id=other, relationship_kind="doctor"))
    notify(env, title="About Alex")
    notify(env, patient_id=other, title="About Pat")
    assert {n["title"] for n in env.notifications.list_for_user(env.doctor)} == {"About Alex", "About Pat"}
    assert [n["title"] for n in env.notifications.list_for_user(env.doctor, patient_id=other)] == ["About Pat"]
    assert [n["title"] for n in env.notifications.list_for_user(env.patient)] == ["About Alex"]


# --------------------------------------------------------------------------- transactional staging


def test_staged_notifications_commit_with_the_callers_transaction(env: Env):
    sub = env.subscribe(Topic.NOTIFICATION)
    with pytest.raises(RuntimeError):
        with env.db.session() as s:
            env.notifications.stage(s, patient_id=env.patient, kind="EMPTY", title="Container empty")
            raise RuntimeError("the state change failed")
    assert env.notes() == [] and sub.drain() == []               # rolled back together, nothing pushed
    pending = PendingNotifications(env.notifications)
    with env.db.session() as s:
        pending.add(s, patient_id=env.patient, kind="EMPTY", title="Container empty", to_caregivers=False)
        assert len(pending.views) == 1 and sub.drain() == []      # not pushed before the commit
    pending.deliver()
    assert [e.data["user_id"] for e in sub.drain()] == [env.patient]
    pending.deliver()                                             # delivering twice pushes nothing new
    assert sub.drain() == []


def test_pending_notifications_with_a_foreign_service_and_without_one(env: Env):
    calls: list[dict[str, Any]] = []

    class NotifyOnly:
        def notify(self, **kw: Any) -> list[int]:
            calls.append(kw)
            raise RuntimeError("remote service down")            # best effort: must not raise

    pending = PendingNotifications(NotifyOnly())
    with env.db.session() as s:
        pending.add(s, patient_id=env.patient, kind="EMPTY", title="Container empty", user_ids=[1])
    pending.deliver()
    assert calls == [{"patient_id": env.patient, "kind": "EMPTY", "title": "Container empty"}]
    nothing = PendingNotifications(None)
    with env.db.session() as s:
        nothing.add(s, patient_id=env.patient, kind="EMPTY", title="x")
    nothing.deliver()
    assert env.notes() == []


def test_service_without_a_bus_still_stores(env: Env):
    quiet = NotificationService(env.db, env.settings, env.clock)
    assert len(quiet.notify(patient_id=env.patient, kind="DEVICE_ALERT", title="Check the dispenser")) == 3


def test_notification_to_dict_shape(env: Env):
    ids = notify(env)
    with env.db.session() as s:
        d = notification_to_dict(s.get(Notification, ids[0]))
    assert set(d) == NOTIFICATION_KEYS and d["read_at"] is None


# --------------------------------------------------------------------------- wording helpers


@pytest.mark.parametrize("hh,mm,text", [(0, 5, "12:05 AM"), (8, 0, "8:00 AM"), (12, 0, "12:00 PM"),
                                        (13, 30, "1:30 PM"), (23, 59, "11:59 PM")])
def test_clock_label(hh, mm, text):
    assert clock_label(datetime(2026, 10, 5, hh, mm)) == text


@pytest.mark.parametrize("seconds,text", [(0, "less than a minute"), (59, "less than a minute"),
                                          (60, "1 minute"), (61, "2 minutes"), (3000, "50 minutes"),
                                          (3600, "1 hour"), (3660, "1 hour 1 minute"),
                                          (timedelta(hours=23, minutes=55).total_seconds(), "23 hours 55 minutes")])
def test_duration_label(seconds, text):
    assert duration_label(seconds) == text


def test_plural():
    assert (plural(1, "pill"), plural(0, "pill"), plural(2, "pill")) == ("1 pill", "0 pills", "2 pills")
