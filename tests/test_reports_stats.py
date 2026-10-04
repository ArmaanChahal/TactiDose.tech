"""reports.data + reports.stats: gathering boundaries, privacy scope and every statistic."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tactidose.db.models import DoseEvent, PillDrop, User
from tactidose.reports.data import fmt_datetime, fmt_pct, fmt_time, gather_report_data
from tactidose.reports.stats import compute_stats, schedule_label
from tests.fakes import seed_v2
from tests.test_reports_support import local, seed_report_scenario


def _gather(db, clock, settings, pid, days=7, **kw):
    now = clock.now()
    return gather_report_data(db, clock, settings, patient_id=pid, days=days,
                              period_start=now - timedelta(days=days), period_end=now, **kw)


@pytest.fixture
def scenario(db_v2, settings_v2, clock):
    ids = seed_report_scenario(db_v2, settings_v2, clock)
    data = _gather(db_v2, clock, settings_v2, ids["patient_id"], created_by_user_id=ids["doctor_id"])
    return ids, data, compute_stats(data)


def test_gather_respects_period_and_patient(scenario, clock):
    ids, data, _ = scenario
    assert data.patient.display_name == "Alex Rivera" and data.creator.display_name == "Dr. Lee"
    assert data.device.device_id == ids["device_id"] and data.device.manual_cooldown_minutes == 60
    assert len(data.doses) == 8                       # 07:50 (before) and 13:00 Oct 5 (future) excluded
    assert data.doses[0].scheduled_at == local(clock, 2026, 9, 28, 8, 0)
    assert data.doses[-1].scheduled_at == local(clock, 2026, 10, 5, 8, 0)   # future but satisfied early
    assert len(data.drops) == 10                      # Sep 27 excluded
    assert {m.content for m in data.messages if m.role == "user"} == {
        "Can I have another pill? My head hurts a little.", "Drop my vitamin please"}
    assert len(data.conversations) == 2
    assert [a.kind for a in data.alerts] == ["LOW_STOCK", "MISSED_DOSE"]   # 3 recipients -> 1 alert each
    assert data.timezone == "America/Vancouver" and data.truncated == ()


def test_gather_never_reads_other_patients(db_v2, settings_v2, clock):
    ids = seed_report_scenario(db_v2, settings_v2, clock)
    with db_v2.session() as s:
        other = User(display_name="Other Patient", role="patient", email="other@test.tactidose")
        s.add(other)
        s.flush()
        s.add(PillDrop(patient_id=other.user_id, device_id=ids["device_id"], slot_number=0, source="manual",
                       status="DROPPED", requested_at=clock.now() - timedelta(hours=1)))
        s.add(DoseEvent(schedule_id=ids["schedule_ids"][0], medication_id=ids["med_ids"][0], user_id=other.user_id,
                        device_id=ids["device_id"], scheduled_at=clock.now() - timedelta(hours=3), status="MISSED"))
        other_id = other.user_id
    mine = _gather(db_v2, clock, settings_v2, ids["patient_id"])
    theirs = _gather(db_v2, clock, settings_v2, other_id)
    assert len(mine.drops) == 10 and len(theirs.drops) == 1
    assert theirs.medications == () and theirs.containers == () and theirs.messages == ()
    assert len(theirs.doses) == 1 and theirs.doses[0].medication_name.startswith("Medication ")


def test_gather_unknown_patient(db_v2, settings_v2, clock):
    with pytest.raises(LookupError):
        _gather(db_v2, clock, settings_v2, 999)


def test_dose_statistics(scenario):
    _, _, stats = scenario
    d = stats["doses"]
    assert d["scheduled"] == 7 and d["cancelled"] == 1
    assert d["dispensed"] == 5 and d["on_time"] == 4 and d["late"] == 1 and d["timing_unknown"] == 0
    assert d["missed"] == 1 and d["hardware_errors"] == 1 and d["pending"] == 0
    assert d["taken_confirmed"] == 1 and d["needs_review"] == 1
    assert d["adherence_rate"] == pytest.approx(5 / 6, abs=1e-4)
    assert stats["adherence_rate"] == d["adherence_rate"]
    assert d["on_time_rate"] == pytest.approx(4 / 5, abs=1e-4)
    assert stats["on_time_minutes"] == 15


def test_drop_statistics(scenario):
    _, _, stats = scenario
    dr = stats["drops"]
    assert dr["requests"] == 10 and dr["dropped"] == 4 and dr["denied"] == 4
    assert dr["failed"] == 1 and dr["uncertain"] == 1 and dr["needs_review"] == 1
    assert dr["scheduled_drops"] == 2 and dr["manual_drops"] == 1 and dr["agent_drops"] == 1
    assert dr["button_drops"] == 0 and dr["on_request_drops"] == 2
    assert dr["refused_requests"] == 3
    assert dr["refused_by_reason"] == {"COOLDOWN": 2, "NEEDS_REVIEW": 1}
    assert dr["denied_by_reason"] == {"COOLDOWN": 2, "ALREADY_SATISFIED": 1, "NEEDS_REVIEW": 1}
    assert dr["failed_by_reason"] == {"MOTOR_FAULT": 1}
    assert dr["by_source"]["agent"] == {"requests": 2, "dropped": 1, "denied": 1, "failed": 0, "uncertain": 0}
    assert dr["by_source"]["schedule"]["requests"] == 5
    assert dr["by_status"] == {"DROPPED": 4, "DENIED": 4, "FAILED": 1, "UNCERTAIN": 1}


def test_conversation_and_alert_statistics(scenario):
    _, _, stats = scenario
    cv = stats["conversations"]
    assert cv["conversations"] == 2 and cv["patient_messages"] == 2 and cv["agent_messages"] == 2
    assert cv["tool_messages"] == 3 and cv["voice_messages"] == 1 and cv["text_messages"] == 1
    assert cv["tool_calls"] == {"request_pill": 2, "get_patient_status": 1}
    assert cv["agent_pill_requests"] == 2
    assert cv["agent_pill_requests_by_status"] == {"DENIED": 1, "DROPPED": 1}
    assert stats["alerts"] == {"LOW_STOCK": 1, "EMPTY": 0, "MISSED_DOSE": 1, "DEVICE_ALERT": 0}


def test_per_day_table(scenario):
    _, _, stats = scenario
    days = {r["date"]: r for r in stats["per_day"]}
    assert list(days) == [f"2026-09-{d}" for d in (28, 29, 30)] + [f"2026-10-0{d}" for d in range(1, 6)]
    assert days["2026-09-28"]["weekday"] == "Mon" and days["2026-09-28"]["adherence_rate"] == 1.0
    assert days["2026-09-29"]["scheduled"] == 0 and days["2026-09-29"]["adherence_rate"] is None
    oct3 = days["2026-10-03"]
    assert (oct3["scheduled"], oct3["dispensed"], oct3["on_time"], oct3["late"], oct3["missed"]) == (3, 2, 1, 1, 1)
    assert oct3["adherence_rate"] == pytest.approx(2 / 3, abs=1e-4) and oct3["drops"] == 2 and oct3["failed"] == 1
    oct4 = days["2026-10-04"]
    assert oct4["scheduled"] == 2 and oct4["cancelled"] == 1 and oct4["pending"] == 1
    assert oct4["denied"] == 4 and oct4["refused"] == 3 and oct4["uncertain"] == 1 and oct4["patient_messages"] == 1
    oct5 = days["2026-10-05"]
    assert oct5["dispensed"] == 1 and oct5["drops"] == 1 and oct5["on_request_drops"] == 1


def test_per_medication_and_inventory(scenario):
    _, _, stats = scenario
    meds = {m["name"]: m for m in stats["per_medication"]}
    vit, cal, omg = meds["Vitamin C (demo candy)"], meds["Calcium (demo token)"], meds["Omega-3 (demo candy)"]
    assert [m["name"] for m in stats["per_medication"]] == [
        "Vitamin C (demo candy)", "Calcium (demo token)", "Omega-3 (demo candy)"]
    assert vit["containers"] == [1] and vit["scheduled"] == 4 and vit["on_time"] == 4 and vit["adherence_rate"] == 1.0
    assert vit["drops"] == 3 and vit["drops_by_source"] == {"schedule": 1, "manual": 1, "agent": 1}
    assert vit["on_request_drops"] == 2 and vit["denied"] == 2 and vit["schedule"] == ["8:00 AM daily"]
    assert vit["pill_count"] == 17 and vit["days_of_supply"] == 17.0
    assert cal["late"] == 1 and cal["scheduled"] == 1 and cal["denied"] == 1
    assert cal["schedule"] == ["1:00 PM daily", "6:00 PM Mon, Wed, Fri"]
    assert cal["doses_per_day"] == pytest.approx(1 + 3 / 7, abs=1e-3) and cal["days_of_supply"] == 1.4
    assert omg["missed"] == 1 and omg["pending"] == 1 and omg["adherence_rate"] == 0.0
    assert omg["failed"] == 1 and omg["uncertain"] == 1 and omg["doses_per_day"] == 1.0  # inactive schedule ignored
    assert omg["days_of_supply"] == 0.0
    inv = stats["inventory"]
    assert [(c["container_number"], c["pill_count"], c["low_stock"], c["empty"]) for c in inv] == [
        (1, 17, False, False), (2, 2, True, False), (3, 0, False, True)]
    assert inv[1]["days_of_supply"] == 1.4 and inv[0]["capacity"] == 30


def test_issues_list(scenario):
    _, _, stats = scenario
    kinds = [(i["kind"], i["medication_name"]) for i in stats["issues"]]
    assert kinds == [("FAILED", "Omega-3 (demo candy)"), ("MISSED", "Omega-3 (demo candy)"),
                     ("UNCERTAIN", "Omega-3 (demo candy)")]
    assert stats["issues_total"] == 3
    assert stats["issues"][2]["needs_review"] is True and stats["issues"][0]["reason"] == "MOTOR_FAULT"
    assert stats["issues"][1]["at_local"].startswith("2026-10-03T20:00:00-07:00")


def test_stats_are_json_and_period(scenario):
    _, _, stats = scenario
    json.dumps(stats)  # stored in a JSON column
    p = stats["period"]
    assert p["days"] == 7 and p["start_local"].startswith("2026-09-28T07:55") and p["end_date"] == "2026-10-05"
    assert stats["device"]["cooldown_minutes"] == 60 and stats["version"] == 1


def test_empty_period(db_v2, settings_v2, clock):
    ids = seed_v2(db_v2, settings_v2, now=clock.now() - timedelta(days=3))
    data = _gather(db_v2, clock, settings_v2, ids["patient_id"], days=1)
    stats = compute_stats(data)
    assert stats["doses"]["scheduled"] == 0 and stats["adherence_rate"] is None
    assert stats["drops"]["requests"] == 0 and stats["issues"] == [] and stats["conversations"]["conversations"] == 0
    assert [r["date"] for r in stats["per_day"]] == ["2026-10-04", "2026-10-05"]
    assert all(r["adherence_rate"] is None for r in stats["per_day"])
    assert [c["days_of_supply"] for c in stats["inventory"]] == [20.0, 20.0, 20.0]


def test_patient_without_device(db_v2, settings_v2, clock):
    with db_v2.session() as s:
        u = User(display_name="No Device", role="patient", email="nodev@test.tactidose")
        s.add(u)
        s.flush()
        pid = u.user_id
    stats = compute_stats(_gather(db_v2, clock, settings_v2, pid))
    assert stats["device"] is None and stats["inventory"] == [] and stats["per_medication"] == []


def test_formatting_helpers(clock):
    at = clock.to_local(local(clock, 2026, 10, 4, 0, 5))
    assert fmt_time(at) == "12:05 AM" and fmt_datetime(at) == "Sun 4 Oct, 12:05 AM"
    assert fmt_time(clock.to_local(local(clock, 2026, 10, 4, 13, 0))) == "1:00 PM"
    assert fmt_pct(None) == "–" and fmt_pct(0) == "0%" and fmt_pct(1) == "100%"
    assert fmt_pct(0.999) == "99%" and fmt_pct(0.001) == "1%" and fmt_pct(5 / 6) == "83%"


def test_schedule_label_formats():
    from tactidose.reports.data import ScheduleRow

    assert schedule_label(ScheduleRow(1, 1, "08:00")) == "8:00 AM daily"
    assert schedule_label(ScheduleRow(1, 1, "20:30", "WEEKLY", ("MON", "THU"))) == "8:30 PM Mon, Thu"
    assert schedule_label(ScheduleRow(1, 1, "bogus")) == "bogus daily"
    assert ScheduleRow(1, 1, "08:00", active=False).doses_per_day == 0.0
