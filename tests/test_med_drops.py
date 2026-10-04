"""DropService rules (ARCHITECTURE v2 §5): every DenyReason, the global cooldown, the double-dose
guard, inventory accounting, the hardware outcome table, review blocking and drop notifications."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import pytest

from tactidose.core.bus import Topic
from tactidose.core.interfaces import DropOutcome, DropServiceAPI, NotificationServiceAPI
from tactidose.db.models import DenyReason, Device, PillDrop, User
from tactidose.db.outbox import KIND_DEVICE_EVENT
from tactidose.hardware.protocol import (
    CommandName,
    CommandResult,
    DeviceState,
    Err,
    GateState,
    HostCode,
    parse_message,
)
from tests.test_med_support import (  # noqa: F401 - fixtures
    DISPENSED,
    DUE,
    HARDWARE_ERROR,
    Env,
    env,
    env_template,
    env_template_unticked,
    env_unticked,
    flaky,
)

R = DenyReason
ONLY_PATIENT = "Only the patient can ask for their pill."


def _msgs(*lines: str):
    return tuple(parse_message(line) for line in lines)


def _dropped(cmd) -> CommandResult:
    return CommandResult(cmd, True, "DROPPED", _msgs(f"OK MOVING {cmd.slot}", f"OK AT_SLOT {cmd.slot}",
                                                     "OK GATE_OPEN", "OK GATE_CLOSED", f"OK DROPPED {cmd.slot}"))


def add_patient(e: Env, name: str = "Pat Two", email: str = "pat2@test.tactidose") -> int:
    with e.db.session() as s:
        u = User(display_name=name, role="patient", email=email)
        s.add(u)
        s.flush()
        return u.user_id


def recipients(e: Env, kind: str) -> set[int]:
    return {n.user_id for n in e.notes(kind=kind)}


# --------------------------------------------------------------------------- contract & happy path


def test_services_implement_the_protocols(env: Env):
    assert isinstance(env.drops, DropServiceAPI)
    assert isinstance(env.notifications, NotificationServiceAPI)


def test_manual_drop_happy_path(env: Env):
    sub = env.subscribe(Topic.DROP, Topic.PATIENT_STATUS, Topic.DOSE_UPDATED, Topic.NOTIFICATION)
    out = env.manual(0)
    assert out.status == "DROPPED" and out.reason is None and out.dropped
    assert out.message == "Vitamin C (demo candy) dropped from container 1."
    assert (out.slot, out.medication_id, out.pill_count_after) == (0, env.med(0), 19)
    assert out.cooldown_remaining_s == 3600 and out.next_allowed_at == env.clock.now() + timedelta(minutes=60)
    assert out.next_allowed_at.utcoffset() == timedelta(hours=-7)          # local offset, as in API.md
    assert out.to_dict()["hardware"] == "OK DROPPED" and out.to_dict()["container_number"] == 1
    assert env.hw.sent == ["DROP_SLOT 0"] and env.hw.pills[0] == 19
    row = env.drop(out.drop_id)
    assert (row.status, row.source, row.pill_count_before, row.pill_count_after) == ("DROPPED", "manual", 20, 19)
    assert row.completed_at == env.clock.now() and row.requested_by_user_id == env.patient
    assert not row.needs_review and row.hardware_result == "OK DROPPED"
    assert env.compartment(0).pill_count == 19
    ev = env.dose_0800()                       # 08:00 window opened at 07:30: this drop satisfies it
    assert (ev.status, ev.drop_id, ev.dispense_source) == (DISPENSED, out.drop_id, "manual")
    assert row.dose_event_id == ev.event_id
    events = sub.drain()
    drop_views = [e.data for e in events if e.topic == Topic.DROP]
    assert [(d["status"], d["in_progress"]) for d in drop_views] == [("UNCERTAIN", True), ("DROPPED", False)]
    assert all(d["patient_id"] == env.patient for d in drop_views)
    assert {"patient_id": env.patient, "reason": "drop"} in [e.data for e in events if e.topic == Topic.PATIENT_STATUS]
    dose_updates = [e.data for e in events if e.topic == Topic.DOSE_UPDATED]
    assert dose_updates[0]["event_id"] == ev.event_id and dose_updates[0]["patient_id"] == env.patient
    pushed = [e.data for e in events if e.topic == Topic.NOTIFICATION]
    assert {(n["user_id"], n["kind"]) for n in pushed} == {
        (env.patient, "PILL_DROPPED"), (env.family, "PILL_DROPPED"), (env.doctor, "PILL_DROPPED")}
    assert pushed[0]["body"] == "Vitamin C (demo candy) dropped from container 1 at 7:55 AM."
    assert pushed[0]["data"]["drop_id"] == out.drop_id and pushed[0]["patient_id"] == env.patient
    assert [r.event for r in env.devlog() if r.event.startswith("DROP_")] == ["DROP_STARTED", "DROP_DROPPED"]
    assert env.devlog("DROP_DROPPED")[0].created_at == env.clock.now()      # audit rows follow the demo clock


def test_in_flight_row_is_written_before_the_hardware_command(env: Env):
    seen: dict[str, Any] = {}

    def inspect(cmd):
        seen["rows"] = [(r.status, r.completed_at, r.needs_review) for r in env.drop_rows()]
        return _dropped(cmd)

    env.hw.script_fn(CommandName.DROP_SLOT, inspect)
    assert env.manual(1).status == "DROPPED"
    assert seen["rows"] == [("UNCERTAIN", None, False)]


def test_drop_by_medication_resolves_its_container_now(env: Env):
    env.compartments.assign(1, env.med(2), patient_id=env.patient, pill_count=5)      # Omega-3 moved
    out = env.manual(None, medication_id=env.med(2))
    assert out.status == "DROPPED" and out.slot == 1 and env.hw.sent == ["DROP_SLOT 1"]
    assert out.pill_count_after == 4


def test_demo_drops_obey_the_cooldown(env: Env):
    env.manual(0)
    out = env.drops.request_drop(patient_id=env.patient, source="demo", slot=1, requested_by_user_id=env.doctor)
    assert (out.status, out.reason) == ("DENIED", "COOLDOWN")
    env.set_cooldown(0)
    out = env.drops.request_drop(patient_id=env.patient, source="demo", slot=1, requested_by_user_id=env.doctor)
    assert out.status == "DROPPED" and out.source == "demo"


# --------------------------------------------------------------------------- every DenyReason


def _deny_cooldown(e: Env) -> DropOutcome:
    e.manual(0)
    return e.agent(1)


def _deny_empty(e: Env) -> DropOutcome:
    e.set_compartment(2, pill_count=0)
    return e.manual(2)


def _deny_no_medication(e: Env) -> DropOutcome:
    e.compartments.assign(1, None, patient_id=e.patient)
    return e.manual(1)


def _deny_unknown(e: Env) -> DropOutcome:
    return e.manual(7)


def _deny_satisfied(e: Env) -> DropOutcome:
    e.manual(0)
    return e.scheduled(e.dose_0800().event_id)


def _deny_in_progress(e: Env) -> DropOutcome:
    assert e.drops._lock.acquire(timeout=1)
    try:
        return e.manual(0)
    finally:
        e.drops._lock.release()


def _deny_device(e: Env) -> DropOutcome:
    e.hw.set_state(connected=False)
    return e.manual(0)


def _deny_review(e: Env) -> DropOutcome:
    e.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    assert e.manual(0).status == "UNCERTAIN"
    return e.manual(1)


def _deny_not_allowed(e: Env) -> DropOutcome:
    return e.drops.request_drop(patient_id=e.patient, source="manual", slot=0, requested_by_user_id=e.family)


def _deny_db(e: Env) -> DropOutcome:
    def broken():
        raise RuntimeError("database is unavailable")

    e.db.session = broken  # type: ignore[method-assign]
    return e.manual(0)


DENY_SCENARIOS: dict[DenyReason, Callable[[Env], DropOutcome]] = {
    R.COOLDOWN: _deny_cooldown,
    R.EMPTY: _deny_empty,
    R.NO_MEDICATION: _deny_no_medication,
    R.UNKNOWN_MEDICATION: _deny_unknown,
    R.ALREADY_SATISFIED: _deny_satisfied,
    R.IN_PROGRESS: _deny_in_progress,
    R.DEVICE_UNAVAILABLE: _deny_device,
    R.NEEDS_REVIEW: _deny_review,
    R.NOT_ALLOWED: _deny_not_allowed,
    R.DB_ERROR: _deny_db,
}


@pytest.mark.parametrize("reason", list(DenyReason), ids=lambda r: r.value)
def test_every_deny_reason_is_enforced_without_a_drop(env: Env, reason: DenyReason):
    out = DENY_SCENARIOS[reason](env)
    assert (out.status, out.reason) == ("DENIED", reason.value)
    assert out.message and not out.dropped
    # Only the scenario's own preparatory drop ever reached the hardware.
    preliminary = 1 if reason in (R.COOLDOWN, R.ALREADY_SATISFIED, R.NEEDS_REVIEW) else 0
    assert len(env.drop_commands()) == preliminary
    if reason is R.DB_ERROR:
        assert out.drop_id is None and env.hw.sent == []          # nothing at all reaches the device
    else:
        row = env.drop(out.drop_id)
        assert (row.status, row.reason, row.completed_at) == ("DENIED", reason.value, env.clock.now())


# --------------------------------------------------------------------------- 1. device


def test_patient_without_a_dispenser_is_denied_and_audited(env: Env):
    other = add_patient(env)
    for pid in (other, 424242):
        out = env.drops.request_drop(patient_id=pid, source="manual", slot=0, requested_by_user_id=pid)
        assert (out.status, out.reason, out.drop_id) == ("DENIED", "DEVICE_UNAVAILABLE", None)
        assert out.message == "No dispenser is linked to this account."
    assert env.drop_rows() == [] and env.hw.sent == []
    assert [r.detail["patient_id"] for r in env.devlog("DROP_DENIED")] == [other, 424242]


def test_a_device_this_system_does_not_control_is_unavailable(env: Env):
    other = add_patient(env)
    with env.db.session() as s:
        s.add(Device(device_id="far-away-unit", user_id=other, name="Elsewhere", num_slots=3))
    out = env.drops.request_drop(patient_id=other, source="manual", slot=0, requested_by_user_id=other)
    assert (out.status, out.reason) == ("DENIED", "DEVICE_UNAVAILABLE")
    assert env.drop(out.drop_id).device_id == "far-away-unit" and env.hw.sent == []


@pytest.mark.parametrize("bad", ["1", True, None, 1.0])
def test_invalid_patient_ids_are_not_allowed(env: Env, bad):
    out = env.drops.request_drop(patient_id=bad, source="manual", slot=0)
    assert (out.status, out.reason, out.drop_id) == ("DENIED", "NOT_ALLOWED", None)
    assert env.hw.sent == []


# --------------------------------------------------------------------------- permission (NOT_ALLOWED)


@pytest.mark.parametrize("make,message", [
    (lambda e: dict(source="kiosk", slot=0), "That kind of drop request is not allowed."),
    (lambda e: dict(source="manual", slot=0, requested_by_user_id=e.family), ONLY_PATIENT),
    (lambda e: dict(source="agent", slot=0, requested_by_user_id=e.doctor), ONLY_PATIENT),
    (lambda e: dict(source="button", slot=0, requested_by_user_id=e.doctor), ONLY_PATIENT),
    (lambda e: dict(source="schedule", dose_event_id=e.dose_0800().event_id, requested_by_user_id=e.patient),
     "Scheduled pills drop automatically."),
    (lambda e: dict(source="manual", slot=0, requested_by_user_id="1"), "That request is not allowed."),
    (lambda e: dict(source="agent", slot=0, conversation_id="chat-7"), "That request is not allowed."),
], ids=["unknown-source", "family-manual", "doctor-agent", "doctor-button", "schedule-by-user", "bad-user-id",
        "bad-conversation-id"])
def test_not_allowed(env: Env, make, message):
    out = env.drops.request_drop(patient_id=env.patient, **make(env))
    assert (out.status, out.reason, out.message) == ("DENIED", "NOT_ALLOWED", message)
    assert env.hw.sent == [] and env.drop(out.drop_id).status == "DENIED"


def test_demo_source_only_in_demo_mode(settings_v2, clock, bus, fake_drop_hw, env_template):
    from tests.test_med_support import from_template

    e = from_template(env_template, settings_v2, clock, bus, fake_drop_hw, demo_mode=False)
    try:
        out = e.drops.request_drop(patient_id=e.patient, source="demo", slot=0)
        assert (out.reason, out.message) == ("NOT_ALLOWED", "Demo drops are turned off.")
        assert e.hw.sent == []
    finally:
        e.db.dispose()


# --------------------------------------------------------------------------- 2. target


@pytest.mark.parametrize("make,message", [
    (lambda e: dict(slot=3), "There is no container 4. The containers are numbered 1 to 3."),
    (lambda e: dict(slot=-1), "There is no container 0. The containers are numbered 1 to 3."),
    (lambda e: dict(slot=True), "There is no such container. The containers are numbered 1 to 3."),
    (lambda e: dict(slot="1"), "There is no such container. The containers are numbered 1 to 3."),
    (lambda e: dict(slot=None), "Please say which container or medication you want."),
    (lambda e: dict(slot=None, medication_id=9999), "I could not find that medication."),
    (lambda e: dict(slot=None, medication_id="2"), "I could not find that medication."),
    (lambda e: dict(slot=0, medication_id=e.med(1)), "Container 1 holds Vitamin C (demo candy), not that medication."),
], ids=["slot-3", "slot-negative", "slot-bool", "slot-str", "no-target", "unknown-med", "med-str", "mismatch"])
def test_unknown_medication(env: Env, make, message):
    out = env.manual(**make(env))
    assert (out.status, out.reason, out.message) == ("DENIED", "UNKNOWN_MEDICATION", message)
    assert env.hw.sent == []


def test_another_patients_medication_is_unknown(env: Env):
    other = add_patient(env)
    foreign = env.catalog.create({"name": "Not yours"}, confirmed=True, patient_id=other)["medication_id"]
    out = env.manual(None, medication_id=foreign)
    assert (out.reason, out.message) == ("UNKNOWN_MEDICATION", "I could not find that medication.")


@pytest.mark.parametrize("setup,slot,message", [
    (lambda e: e.compartments.assign(1, None, patient_id=e.patient), 1, "Container 2 has no medication set up."),
    (lambda e: e.set_medication(e.med(0), active=False), 0, "Container 1 has no medication set up."),
    (lambda e: e.set_medication(e.med(0), confirmed_by_user=False), 0, "Container 1 has no medication set up."),
    (lambda e: e.set_compartment(2, active=False), 2, "Container 3 has no medication set up."),
], ids=["unassigned", "archived", "unconfirmed", "inactive-container"])
def test_no_medication_by_slot(env: Env, setup, slot, message):
    setup(env)
    out = env.manual(slot)
    assert (out.status, out.reason, out.message) == ("DENIED", "NO_MEDICATION", message)
    assert env.hw.sent == []


def test_no_medication_by_medication_id(env: Env):
    loose = env.catalog.create({"name": "Zinc (demo)"}, confirmed=True, patient_id=env.patient)["medication_id"]
    out = env.manual(None, medication_id=loose)
    assert (out.reason, out.message) == ("NO_MEDICATION", "Zinc (demo) is not in a container right now.")
    env.set_medication(env.med(1), active=False)
    out = env.manual(None, medication_id=env.med(1))
    assert (out.reason, out.message) == ("NO_MEDICATION", "Calcium (demo token) is not set up for dropping.")


# --------------------------------------------------------------------------- 3. review / in flight


def test_in_flight_drop_on_the_device_is_in_progress_then_needs_review(env: Env):
    with env.db.session() as s:
        s.add(PillDrop(patient_id=env.patient, device_id=env.settings.device_id, slot_number=2, source="manual",
                       status="UNCERTAIN", requested_at=env.clock.now(), completed_at=None))
    assert env.manual(0).reason == "IN_PROGRESS"
    env.advance(minutes=10)                      # stuck far longer than any drop takes
    out = env.manual(0)
    assert out.reason == "NEEDS_REVIEW" and "container 3 could not be confirmed" in out.message
    assert env.hw.sent == []


# --------------------------------------------------------------------------- 4. cooldown


def test_cooldown_blocks_manual_agent_and_button_for_any_pill(env: Env):
    env.manual(0)
    env.advance(minutes=10)
    for source, slot in (("manual", 1), ("agent", 2), ("button", 0)):
        kw = {"requested_by_user_id": env.patient} if source == "manual" else {}
        out = env.drops.request_drop(patient_id=env.patient, source=source, slot=slot, **kw)
        assert (out.status, out.reason) == ("DENIED", "COOLDOWN"), source
        assert out.cooldown_remaining_s == 50 * 60 and out.next_allowed_at == env.at("08:55")
        assert out.message == "A pill was dropped at 7:55 AM. The next pill can drop at 8:55 AM, in 50 minutes."
    assert env.drop_commands() == ["DROP_SLOT 0"]
    assert env.notes(kind="DROP_DENIED") == []            # denials are shown inline, never stored


def test_cooldown_ends_exactly_after_the_configured_minutes(env: Env):
    env.manual(0)
    env.advance(minutes=59, seconds=59)
    out = env.manual(1)
    assert (out.reason, out.cooldown_remaining_s) == ("COOLDOWN", 1)
    assert out.message.endswith("in less than a minute.")
    env.advance(seconds=1)
    assert env.manual(1).status == "DROPPED"


def test_cooldown_message_names_tomorrow(env: Env):
    env.set_cooldown(1440)
    env.manual(0)
    env.advance(minutes=5)
    out = env.agent(1)
    assert out.message == ("A pill was dropped at 7:55 AM. The next pill can drop tomorrow at 7:55 AM, "
                           "in 23 hours 55 minutes.")


def test_scheduled_drops_ignore_the_cooldown_but_start_it(env: Env):
    env.manual(1)                                            # 07:55 Calcium
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1              # 08:00 Vitamin C drops anyway
    assert env.drop_commands() == ["DROP_SLOT 1", "DROP_SLOT 0"]
    ev = env.dose_0800()
    assert (ev.status, ev.dispense_source) == (DISPENSED, "schedule")
    env.travel("08:56")                                      # 61 min after the manual drop, 56 after the scheduled one
    out = env.manual(2)
    assert out.reason == "COOLDOWN" and out.next_allowed_at == env.at("09:00")


def test_cooldown_zero_disables_the_check(env: Env):
    env.set_cooldown(0)
    assert [env.manual(i).status for i in (0, 1, 2)] == ["DROPPED"] * 3
    st = env.drops.patient_status(env.patient)
    assert st.cooldown_remaining_s == 0 and st.next_manual_allowed_at is None


def test_doctor_cooldown_is_the_only_wait_on_request(env: Env):
    """No fixed per-pill floor: with the doctor/family cooldown at 0 the same pill can drop again."""
    env.set_cooldown(0)
    assert env.manual(0).status == "DROPPED"                  # 07:55 Vitamin C
    env.advance(minutes=5)
    assert env.agent(0).status == "DROPPED"                   # same pill, 5 minutes later
    demo = env.drops.request_drop(patient_id=env.patient, source="demo", slot=0, requested_by_user_id=env.doctor)
    assert demo.status == "DROPPED"
    env.set_cooldown(30)                                      # the doctor's cooldown applies again
    env.advance(minutes=1)
    denied = env.manual(0)
    assert (denied.status, denied.reason) == ("DENIED", "COOLDOWN")
    assert env.drop_commands() == ["DROP_SLOT 0", "DROP_SLOT 0", "DROP_SLOT 0"]


def test_uncertain_drops_count_for_the_cooldown(env: Env):
    with env.db.session() as s:   # an UNCERTAIN drop that is (no longer) flagged for review
        s.add(PillDrop(patient_id=env.patient, device_id=env.settings.device_id, slot_number=2, source="agent",
                       status="UNCERTAIN", needs_review=False, requested_at=env.clock.now() - timedelta(minutes=6),
                       completed_at=env.clock.now() - timedelta(minutes=5)))
    out = env.manual(0)
    assert (out.reason, out.cooldown_remaining_s) == ("COOLDOWN", 55 * 60)


def test_cooldown_runs_from_an_uncertain_drop_awaiting_review(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    out = env.manual(0)
    assert out.status == "UNCERTAIN" and out.cooldown_remaining_s == 3600
    assert env.drops.patient_status(env.patient).cooldown_remaining_s == 3600


def test_failed_and_denied_requests_do_not_start_the_cooldown(env: Env):
    env.hw.script(CommandName.DROP_SLOT, Err.INVALID_STATE)
    failed = env.manual(0)
    assert failed.status == "FAILED" and failed.cooldown_remaining_s == 0 and failed.next_allowed_at is None
    env.set_compartment(1, pill_count=0)
    assert env.manual(1).reason == "EMPTY"
    assert env.manual(0).status == "DROPPED"


# --------------------------------------------------------------------------- 5. double-dose guard


def test_manual_drop_at_0750_then_auto_drop_at_0800_is_already_satisfied(env_unticked: Env):
    e = env_unticked                                  # the 08:00 dose does not exist yet at 07:50
    e.travel("07:50")
    first = e.manual(0)
    assert first.status == "DROPPED" and e.events() == []
    e.travel("08:00")
    e.tick()
    sub = e.subscribe(Topic.DOSE_UPDATED)
    assert e.drops.run_scheduled_drops() == 1
    rows = e.drop_rows()
    assert [(r.source, r.status, r.reason) for r in rows] == [
        ("manual", "DROPPED", None), ("schedule", "DENIED", "ALREADY_SATISFIED")]
    ev = e.dose_0800()
    assert (ev.status, ev.drop_id, ev.dispensed_at, ev.dispense_source) == (
        DISPENSED, first.drop_id, e.at("07:50"), "manual")
    assert rows[0].dose_event_id == ev.event_id and rows[1].dose_event_id == ev.event_id
    assert e.drop_commands() == ["DROP_SLOT 0"]                 # ZERO extra DROP_SLOT
    assert [d.data["change"] for d in sub.drain()] == ["satisfied"]
    assert e.drops.run_scheduled_drops() == 0                   # satisfied doses are never attempted again
    assert e.compartment(0).pill_count == 19


def test_manual_drop_links_the_open_dose_so_no_auto_drop_is_attempted(env: Env):
    env.travel("07:50")
    first = env.manual(0)
    ev = env.dose_0800()
    assert (ev.status, ev.drop_id) == (DISPENSED, first.drop_id)
    env.travel("08:00")
    env.tick()
    assert env.drops.run_scheduled_drops() == 0
    out = env.scheduled(ev.event_id)                             # even an explicit scheduled request
    assert (out.reason, out.message) == (
        "ALREADY_SATISFIED", "The 8:00 AM dose of Vitamin C (demo candy) was already dropped at 7:50 AM.")
    assert env.drop_commands() == ["DROP_SLOT 0"] and env.event(ev.event_id).drop_id == first.drop_id


def test_a_drop_within_the_minimum_interval_before_the_window_satisfies_the_dose(env: Env):
    # The 08:00 window opens at 07:30, but min_dose_interval_minutes (60) reaches back to 07:00:
    # a manual drop at 07:20 must not be followed by a second pill at 08:00.
    env.travel("07:20")
    first = env.manual(0)
    assert first.status == "DROPPED"
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    assert env.drop_commands() == ["DROP_SLOT 0"]                 # no second pill
    rows = env.drop_rows()
    assert (rows[-1].source, rows[-1].status, rows[-1].reason) == ("schedule", "DENIED", "ALREADY_SATISFIED")
    assert env.dose_0800().drop_id == first.drop_id


def test_a_drop_earlier_than_the_minimum_interval_does_not_satisfy_the_dose(env: Env):
    env.travel("06:50")                                          # 70 min before 08:00
    env.manual(0)
    env.travel("08:00")
    assert env.drops.run_scheduled_drops() == 1
    assert env.drop_commands() == ["DROP_SLOT 0", "DROP_SLOT 0"]


def test_a_drop_of_another_medication_does_not_satisfy_the_dose(env: Env):
    env.manual(1)
    assert env.dose_0800().status == DUE


def test_agent_drop_satisfies_the_dose_too(env: Env):
    out = env.agent(0, conversation_id=5)
    ev = env.dose_0800()
    assert (ev.status, ev.dispense_source, ev.drop_id) == (DISPENSED, "agent", out.drop_id)
    assert env.drop(out.drop_id).conversation_id == 5


@pytest.mark.parametrize("status,reason,message", [
    ("MISSED", "NOT_ALLOWED", "The 8:00 AM dose of Vitamin C (demo candy) was missed."),
    ("CANCELLED", "NOT_ALLOWED", "The 8:00 AM dose of Vitamin C (demo candy) was skipped."),
    ("DISPENSING", "IN_PROGRESS", "The 8:00 AM dose of Vitamin C (demo candy) is dropping right now."),
    ("TAKEN", "ALREADY_SATISFIED", "The 8:00 AM dose of Vitamin C (demo candy) was already dropped."),
])
def test_scheduled_requests_for_closed_doses(env: Env, status, reason, message):
    ev = env.dose_0800()
    env.set_event(ev.event_id, status=status)
    out = env.scheduled(ev.event_id)
    assert (out.status, out.reason, out.message) == ("DENIED", reason, message)
    assert env.hw.sent == []


def test_scheduled_request_outside_the_window_or_for_unknown_doses(env: Env):
    tomorrow = env.event_at(env.sched(0), "08:00", env.clock.today_local() + timedelta(days=1))
    out = env.scheduled(tomorrow.event_id)
    assert (out.reason, out.message) == ("NOT_ALLOWED", "The 8:00 AM dose of Vitamin C (demo candy) is not due now.")
    assert env.scheduled(987654).reason == "UNKNOWN_MEDICATION"
    no_id = env.drops.request_drop(patient_id=env.patient, source="schedule")
    assert no_id.reason == "UNKNOWN_MEDICATION" and no_id.drop_id is not None
    mismatch = env.drops.request_drop(patient_id=env.patient, source="schedule",
                                      dose_event_id=env.dose_0800().event_id, medication_id=env.med(2))
    assert mismatch.reason == "UNKNOWN_MEDICATION"
    assert env.hw.sent == []


# --------------------------------------------------------------------------- 6. inventory


def test_inventory_follows_every_outcome(env: Env):
    env.set_cooldown(0)
    env.settings.min_dose_interval_minutes = 0                  # same pill back to back
    assert env.manual(0).pill_count_after == 19
    env.hw.script(CommandName.DROP_SLOT, Err.INVALID_STATE)
    failed = env.manual(0)
    assert (failed.status, failed.pill_count_after) == ("FAILED", 19)
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    unsure = env.manual(0)
    assert (unsure.status, unsure.pill_count_after) == ("UNCERTAIN", 19)
    assert env.compartment(0).pill_count == 19                  # unchanged until reviewed
    view = env.drops.resolve_drop(unsure.drop_id, dropped=True, by_user_id=env.doctor)
    assert view["pill_count_after"] == 18 and env.compartment(0).pill_count == 18
    assert [r.pill_count_before for r in env.drop_rows()] == [20, 19, 19]


def test_empty_container_is_refused_and_notified_once_per_episode(env: Env):
    env.set_compartment(2, pill_count=0)
    out = env.manual(2)
    assert (out.reason, out.message, out.pill_count_after) == (
        "EMPTY", "Container 3 is empty. Please ask your caregiver to refill it.", 0)
    assert recipients(env, "EMPTY") == {env.patient, env.family, env.doctor}
    env.advance(minutes=1)
    assert env.agent(2).reason in ("COOLDOWN", "EMPTY")
    assert env.manual(2).reason == "EMPTY" and len(env.notes(kind="EMPTY")) == 3
    assert env.hw.sent == []


def test_no_pill_from_the_sensor_empties_the_container(env: Env):
    env.set_cooldown(0)
    env.settings.min_dose_interval_minutes = 0                  # same pill back to back
    env.hw.set_pills(0, 0)                                      # physically empty, the database says 20
    out = env.manual(0)
    assert (out.status, out.reason, out.pill_count_after) == ("FAILED", "NO_PILL", 0)
    assert out.message == "No pill came out of container 1. It may be empty. Please ask your caregiver to refill it."
    assert env.compartment(0).pill_count == 0 and env.dose_0800().status == DUE
    assert env.note_kinds(env.patient) == ["EMPTY"]
    assert recipients(env, "EMPTY") == {env.patient, env.family, env.doctor}
    again = env.manual(0)
    assert again.reason == "EMPTY" and env.drop_commands() == ["DROP_SLOT 0"]
    assert len(env.notes(kind="EMPTY")) == 3                    # same empty episode: not repeated
    env.advance(minutes=1)
    env.compartments.refill(0, set=2, patient_id=env.patient)   # refill starts a new episode
    env.hw.set_pills(0, 2)
    assert [env.manual(0).pill_count_after for _ in range(2)] == [1, 0]
    assert len(env.notes(kind="EMPTY")) == 6
    dev_events = [r.payload for r in env.outbox(KIND_DEVICE_EVENT)]
    assert [(p["event_type"], p["code"]) for p in dev_events] == [("drop_failed", "NO_PILL")]


def test_low_stock_is_notified_once_per_crossing(env: Env):
    env.set_cooldown(0)
    env.settings.min_dose_interval_minutes = 0                  # same pill back to back
    env.compartments.refill(0, set=5, patient_id=env.patient)
    outs = [env.manual(0) for _ in range(5)]
    assert [o.pill_count_after for o in outs] == [4, 3, 2, 1, 0]
    assert outs[1].message == "Vitamin C (demo candy) dropped from container 1. Only 3 pills left."
    assert outs[3].message == "Vitamin C (demo candy) dropped from container 1. Only 1 pill left."
    assert outs[4].message == "Vitamin C (demo candy) dropped from container 1. Container 1 is now empty."
    low = env.notes(env.patient, "LOW_STOCK")
    assert len(low) == 1 and low[0].data["pill_count"] == 3
    assert low[0].body == "Container 1 (Vitamin C (demo candy)) has 3 pills left. Please refill it soon."
    assert recipients(env, "LOW_STOCK") == {env.patient, env.family, env.doctor}
    assert len(env.notes(env.patient, "EMPTY")) == 1
    env.advance(minutes=1)
    env.compartments.refill(0, set=6, patient_id=env.patient)
    for _ in range(3):
        env.manual(0)
    assert len(env.notes(env.patient, "LOW_STOCK")) == 2        # a new crossing after the refill


# --------------------------------------------------------------------------- hardware outcome table


@pytest.mark.parametrize("code", [Err.INVALID_STATE, Err.NOT_HOMED, Err.BUSY, Err.MOTOR_FAULT, Err.STOPPED,
                                  HostCode.NOT_CONNECTED, HostCode.DEVICE_RESET, HostCode.INVALID_ARGUMENT])
def test_definite_failures_are_failed_with_the_code(env: Env, code):
    env.hw.script(CommandName.DROP_SLOT, code)
    out = env.manual(0)
    assert (out.status, out.reason, out.pill_count_after) == ("FAILED", code.value, 20)
    assert out.cooldown_remaining_s == 0 and env.compartment(0).pill_count == 20
    row = env.drop(out.drop_id)
    assert row.status == "FAILED" and not row.needs_review and row.completed_at is not None
    kinds = {n.kind for n in env.notes()}
    assert kinds == {"DROP_FAILED"} and recipients(env, "DROP_FAILED") == {env.patient, env.family, env.doctor}
    dev = [r.payload for r in env.outbox(KIND_DEVICE_EVENT)]
    if code is Err.STOPPED:
        assert out.message == "Stopped. No pill was dropped." and dev == []
    else:
        assert out.message == ("The pill did not drop from container 1. "
                               "Please try again, or ask your caregiver for help.")
        assert [(p["event_type"], p["code"]) for p in dev] == [("drop_failed", code.value)]
        assert "Vitamin" not in str(dev)


def _timeout(e: Env) -> None:
    e.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)


def _disconnect(e: Env) -> None:
    e.hw.script(CommandName.DROP_SLOT, HostCode.DISCONNECTED)


def _stopped_after_gate_open(e: Env) -> None:
    e.hw.script_fn(CommandName.DROP_SLOT, lambda cmd: CommandResult(
        cmd, False, "STOPPED", _msgs(f"OK MOVING {cmd.slot}", f"OK AT_SLOT {cmd.slot}", "OK GATE_OPEN", "ERR STOPPED")))


def _reset_after_gate_open(e: Env) -> None:
    e.hw.script_fn(CommandName.DROP_SLOT, lambda cmd: CommandResult.host_failure(
        cmd, HostCode.DEVICE_RESET, messages=_msgs("OK GATE_OPEN")))


def _driver_raises(e: Env) -> None:
    def boom(_cmd):
        raise RuntimeError("driver bug")

    e.hw.script_fn(CommandName.DROP_SLOT, boom)


@pytest.mark.parametrize("script,code", [
    (_timeout, "TIMEOUT"), (_disconnect, "DISCONNECTED"), (_stopped_after_gate_open, "STOPPED"),
    (_reset_after_gate_open, "DEVICE_RESET"), (_driver_raises, "DISCONNECTED"),
], ids=["timeout", "disconnected", "stopped-after-gate", "reset-after-gate", "driver-raises"])
def test_uncertain_outcomes_need_review_and_block_every_later_drop(env: Env, script, code):
    script(env)
    out = env.manual(0)
    assert (out.status, out.reason) == ("UNCERTAIN", code)
    assert out.message == ("I could not confirm whether a pill dropped from container 1. Please check. "
                           "A caregiver must confirm it before the next pill can drop.")
    row = env.drop(out.drop_id)
    assert row.needs_review and row.completed_at == env.clock.now() and row.pill_count_after == 20
    assert env.compartment(0).pill_count == 20 and env.dose_0800().status == DUE     # not satisfied
    assert recipients(env, "DROP_UNCERTAIN") == {env.patient, env.family, env.doctor}
    assert [(p.payload["event_type"], p.payload["code"]) for p in env.outbox(KIND_DEVICE_EVENT)] == [
        ("drop_uncertain", code)]
    env.set_cooldown(0)
    for blocked in (env.manual(1), env.agent(2), env.scheduled(env.dose_0800().event_id)):
        assert (blocked.status, blocked.reason) == ("DENIED", "NEEDS_REVIEW")
        assert blocked.message == ("An earlier drop from container 1 could not be confirmed. "
                                   "A caregiver must check it before another pill can drop.")
    assert len(env.drop_commands()) == 1


def test_resolving_an_uncertain_drop_as_dropped(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    out = env.manual(0)
    env.advance(minutes=3)
    sub = env.subscribe(Topic.DROP, Topic.DOSE_UPDATED, Topic.PATIENT_STATUS)
    view = env.drops.resolve_drop(out.drop_id, dropped=True, note=" Pill was in the cup ", by_user_id=env.family)
    assert (view["status"], view["needs_review"], view["review_note"]) == ("DROPPED", False, "Pill was in the cup")
    assert view["pill_count_after"] == 19 and env.compartment(0).pill_count == 19
    ev = env.dose_0800()                                          # the 07:55 drop satisfies the 08:00 dose
    assert (ev.status, ev.drop_id, ev.dispensed_at) == (DISPENSED, out.drop_id, env.at("07:55"))
    topics = {e.topic for e in sub.drain()}
    assert topics == {Topic.DROP, Topic.DOSE_UPDATED, Topic.PATIENT_STATUS}
    assert env.devlog("DROP_REVIEW_RESOLVED")[-1].detail["by_user_id"] == env.family
    # the drop counts: the cooldown runs from 07:55
    blocked = env.manual(1)
    assert blocked.reason == "COOLDOWN" and blocked.next_allowed_at == env.at("08:55")


def test_resolving_an_uncertain_drop_as_not_dropped_lifts_the_cooldown(env: Env):
    env.hw.script(CommandName.DROP_SLOT, HostCode.TIMEOUT)
    out = env.manual(0)
    view = env.drops.resolve_drop(out.drop_id, dropped=False, by_user_id=env.doctor)
    assert (view["status"], view["needs_review"], view["pill_count_after"]) == ("FAILED", False, 20)
    assert env.compartment(0).pill_count == 20
    assert env.manual(0).status == "DROPPED"


# --------------------------------------------------------------------------- notifications policy


def test_caregivers_are_not_told_about_routine_drops_when_disabled(settings_v2, clock, bus, fake_drop_hw, env_template):
    from tests.test_med_support import from_template

    e = from_template(env_template, settings_v2, clock, bus, fake_drop_hw, notify_caregivers_on_drop=False)
    try:
        e.set_cooldown(0)
        e.compartments.refill(0, set=4, patient_id=e.patient)
        e.manual(0)
        assert recipients(e, "PILL_DROPPED") == {e.patient}
        assert recipients(e, "LOW_STOCK") == {e.patient, e.family, e.doctor}      # problems still go to everyone
    finally:
        e.db.dispose()


def test_inactive_or_unlinked_caregivers_get_nothing(env: Env):
    with env.db.session() as s:
        s.get(User, env.family).is_active = False
        s.add(User(display_name="Stranger", role="doctor", email="stranger@test.tactidose"))
    env.manual(0)
    assert recipients(env, "PILL_DROPPED") == {env.patient, env.doctor}


def test_drops_work_without_a_notification_service(settings_v2, clock, bus, fake_drop_hw, env_template):
    from tests.test_med_support import from_template

    e = from_template(env_template, settings_v2, clock, bus, fake_drop_hw, with_notifications=False)
    try:
        assert e.manual(0).status == "DROPPED" and e.notes() == []
    finally:
        e.db.dispose()


def test_a_foreign_notification_service_is_called_after_the_commit(env: Env):
    calls: list[dict[str, Any]] = []

    class Minimal:
        def notify(self, **kw: Any) -> list[int]:
            calls.append(kw)
            return []

        def list_for_user(self, user_id: int, *, unread_only: bool = False, limit: int = 50) -> list:
            return []

        def mark_read(self, user_id: int, ids: list[int] | None = None) -> int:
            return 0

    env.drops.notifications = Minimal()
    env.manual(0)
    assert [c["kind"] for c in calls] == ["PILL_DROPPED"] and calls[0]["patient_id"] == env.patient
    assert env.notes() == []


# --------------------------------------------------------------------------- hardware preparation (check 8)


@pytest.mark.parametrize("state", [DeviceState.SAFE_STOP, DeviceState.BOOT])
def test_unhomed_device_is_homed_first(env: Env, state):
    env.hw.set_state(state=state, homed=False)
    assert env.manual(0).status == "DROPPED"
    assert env.hw.sent == ["HOME", "DROP_SLOT 0"]


def test_open_gate_is_closed_first(env: Env):
    env.hw.set_state(state=DeviceState.GATE_OPEN, gate=GateState.OPEN)
    assert env.manual(0).status == "DROPPED"
    assert env.hw.sent == ["CLOSE_GATE", "DROP_SLOT 0"]


@pytest.mark.parametrize("setup,sent", [
    (lambda e: e.hw.set_state(connected=False), []),
    (lambda e: e.hw.set_state(state=DeviceState.FAULT, homed=False), []),
    (lambda e: e.hw.set_state(state=DeviceState.MOVING), []),
    (lambda e: (e.hw.set_state(state=DeviceState.SAFE_STOP, homed=False),
                e.hw.script(CommandName.HOME, Err.HOME_TIMEOUT)), ["HOME"]),
    (lambda e: (e.hw.set_state(state=DeviceState.GATE_OPEN, gate=GateState.OPEN),
                e.hw.script(CommandName.CLOSE_GATE, Err.BUSY)), ["CLOSE_GATE"]),
    (lambda e: (e.hw.set_state(state=DeviceState.UNKNOWN, homed=None),
                e.hw.script(CommandName.STATUS, HostCode.TIMEOUT)), ["STATUS"]),
    (lambda e: e.hw.set_state(state=DeviceState.UNKNOWN, homed=None), ["STATUS"]),
], ids=["disconnected", "fault", "busy", "home-fails", "close-fails", "status-fails", "still-unknown"])
def test_hardware_not_ready_is_device_unavailable(env: Env, setup, sent):
    setup(env)
    out = env.manual(0)
    assert (out.status, out.reason) == ("DENIED", "DEVICE_UNAVAILABLE")
    assert env.hw.sent == sent and env.compartment(0).pill_count == 20
    row = env.drop(out.drop_id)
    assert row.status == "DENIED" and row.reason == "DEVICE_UNAVAILABLE"
    if sent in (["HOME"], ["CLOSE_GATE"]):
        assert out.hardware is not None and row.hardware_result.startswith("ERR")


def test_auto_home_disabled_refuses_an_unhomed_device(settings_v2, clock, bus, fake_drop_hw, env_template):
    from tests.test_med_support import from_template

    e = from_template(env_template, settings_v2, clock, bus, fake_drop_hw, hw_auto_home=False)
    try:
        e.hw.set_state(state=DeviceState.SAFE_STOP, homed=False)
        assert e.manual(0).reason == "DEVICE_UNAVAILABLE" and e.hw.sent == []
    finally:
        e.db.dispose()


def test_sources_may_be_given_as_enum_members(env: Env):
    from tactidose.db.models import DropSource

    out = env.drops.request_drop(patient_id=env.patient, source=DropSource.AGENT, slot=2)
    assert (out.status, out.source) == ("DROPPED", "agent") and env.drop(out.drop_id).source == "agent"
    other = env.drops.request_drop(patient_id=env.patient, source=None, slot=1)  # type: ignore[arg-type]
    assert other.reason == "NOT_ALLOWED"
