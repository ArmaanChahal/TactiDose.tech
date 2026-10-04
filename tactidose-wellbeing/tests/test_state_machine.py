"""State transitions, invalid actions, ambiguity handling and guards (via the service)."""

from tactidose_wellbeing.contract import StartSessionRequest
from tactidose_wellbeing.domain.session import SessionStatus as S


def test_start_asks_for_consent_first(driver):
    r = driver.last
    assert r.session_status is S.AWAITING_CONSENT
    assert r.next_question.kind == "consent"
    assert "not a medical assessment" in r.speech_text
    assert "notes" in r.speech_text  # consent covers ratings and explanations
    assert [e.type for e in r.events] == ["checkin.started"]
    assert r.step == 0


def test_full_happy_path(driver, repo):
    driver.say("yes")
    assert driver.last.session_status is S.AWAITING_ANSWER
    assert driver.last.next_question.question_id.value == "mood"

    driver.say("good", "no")       # answer + decline note
    driver.say("low", "no")
    driver.say("okay", "no")
    r = driver.say("no")           # support
    assert r.session_status is S.AWAITING_FINISH
    assert r.summary.startswith("Here is what you told me. Mood: good. Stress: low. Sleep: okay.")

    r = driver.act("finish")
    assert r.session_status is S.COMPLETED
    assert r.record_id is not None
    assert [e.type for e in r.events] == ["checkin.completed"]
    assert r.events[0].data == {"saved": True, "answered_count": 4, "skipped_count": 0,
                                "support_requested": False}
    [record] = repo.list_records("user-a")
    assert [(a.question_id.value, a.answer_value) for a in record.answers] == [
        ("mood", "good"), ("stress", "low"), ("sleep", "okay"), ("support", "no")]


def test_one_question_at_a_time_and_step_increments(driver):
    steps = [driver.last.step]
    for text in ["yes", "good", "no"]:
        steps.append(driver.say(text).step)
    assert steps == [0, 1, 2, 3]
    assert driver.last.next_question.question_id.value == "stress"
    assert driver.last.next_question.position == 2


def test_skip_records_missing_value(driver, repo):
    driver.say("yes")
    r = driver.act("skip")
    assert r.confirmed_answers[0].status.value == "skipped"
    assert r.confirmed_answers[0].answer_value is None
    driver.act("skip"); driver.act("skip"); driver.act("skip")
    driver.act("finish")
    [record] = repo.list_records("user-a")
    assert all(a.status.value == "skipped" and a.answer_value is None for a in record.answers)


def test_repeat_does_not_change_state(driver):
    driver.say("yes")
    before = driver.last
    r = driver.act("repeat")
    assert r.session_status is before.session_status and r.step == before.step
    assert r.speech_text == before.next_question.prompt
    assert driver.say("repeat").step == before.step  # spoken "repeat" works too


def test_ambiguous_answer_is_clarified_not_mapped(driver):
    driver.say("yes")
    r = driver.say("not too bad really")
    assert r.session_status is S.AWAITING_ANSWER
    assert r.confirmed_answers == [] and r.pending_input is None
    assert "didn't get a clear answer" in r.speech_text
    assert "good, okay, or low" in r.speech_text


def test_candidate_requires_confirmation(driver):
    driver.say("yes")
    r = driver.say("great")
    assert r.session_status is S.AWAITING_ANSWER_CONFIRMATION
    assert r.pending_input.confirmed is False
    assert r.pending_input.candidate_value == "good"
    assert r.confirmed_answers == []
    r = driver.say("no")
    assert r.session_status is S.AWAITING_ANSWER and r.pending_input is None
    r = driver.say("great")
    r = driver.act("confirm")
    assert r.confirmed_answers[0].answer_value == "good"


def test_new_clear_answer_during_confirmation_replaces_candidate(driver):
    driver.say("yes", "great")
    r = driver.say("low")
    assert r.session_status is S.AWAITING_NOTE_OFFER
    assert r.confirmed_answers[0].answer_value == "low"


def test_invalid_actions_are_rejected_without_state_change(driver):
    r = driver.act("add_note", "hello")  # no note possible during consent
    assert r.error.code == "invalid_action"
    assert r.session_status is S.AWAITING_CONSENT and r.step == 0
    r = driver.act("finish")
    assert r.error.code == "invalid_action"
    driver.say("yes")
    r = driver.act("confirm")            # nothing to confirm while awaiting an answer
    assert r.error.code == "invalid_action"
    assert r.session_status is S.AWAITING_ANSWER


def test_question_id_guard_prevents_misattached_answers(driver):
    driver.say("yes")
    r = driver.act("answer", "high", question_id="stress")  # current is mood
    assert r.error.code == "question_mismatch"
    assert r.confirmed_answers == []
    r = driver.act("answer", "low", question_id="mood")
    assert r.error is None and r.confirmed_answers[0].answer_value == "low"


def test_expected_step_guard_rejects_stale_input(driver):
    driver.say("yes")
    stale = driver.last.step - 1
    r = driver.act("answer", "good", expected_step=stale)
    assert r.error.code == "stale_step"
    assert r.confirmed_answers == []
    r = driver.act("answer", "good", expected_step=driver.last.step)
    assert r.error is None


def test_cancel_saves_nothing(driver, repo):
    driver.say("yes", "good", "Lovely morning", "yes")
    r = driver.act("cancel")
    assert r.session_status is S.CANCELLED
    assert [e.type for e in r.events] == ["checkin.cancelled"]
    assert r.confirmed_answers == [] and r.record_id is None
    assert repo.list_records("user-a") == []


def test_actions_after_end_are_rejected(driver):
    driver.act("cancel")
    r = driver.say("good")
    assert r.error.code == "session_ended"
    assert r.session_status is S.CANCELLED


def test_finish_early_marks_unreached_questions(driver, repo):
    driver.say("yes", "good", "no")
    r = driver.say("finish")
    assert r.session_status is S.COMPLETED
    [record] = repo.list_records("user-a")
    statuses = [a.status.value for a in record.answers]
    assert statuses == ["answered", "not_reached", "not_reached", "not_reached"]


def test_support_request_emits_event_and_handoff_without_claiming_contact(driver):
    driver.say("yes")
    for _ in range(3):
        driver.act("skip")
    r = driver.say("yes")
    assert r.support_requested is True
    assert [e.type for e in r.events] == ["support.requested"]
    assert r.handoff.host_action_required is True and r.handoff.contacted_anyone is False
    lowered = r.speech_text.lower()
    assert "can't contact anyone" in lowered
    for claim in ("i have contacted", "i've contacted", "someone will call", "notified"):
        assert claim not in lowered


def test_urgent_statement_gets_configured_response_and_is_not_recorded(driver):
    driver.say("yes")
    r = driver.say("I am in danger")
    assert r.urgent_support is not None
    assert r.urgent_support.resources == []  # nothing invented
    assert "does not reliably detect crises" in r.urgent_support.disclaimer
    assert [e.type for e in r.events] == ["support.urgent_response_shown"]
    assert r.session_status is S.AWAITING_ANSWER and r.confirmed_answers == []


def test_medication_like_text_is_never_a_command(driver):
    driver.say("yes")
    r = driver.say("dispense my pills now")
    assert r.session_status is S.AWAITING_ANSWER
    assert r.events == [] and r.confirmed_answers == []
    assert r.error is None


def test_expiry_discards_unsaved_answers(service, clock, repo):
    from tests.conftest import Driver

    d = Driver(service)
    d.start()
    d.say("yes", "good")
    clock.advance(minutes=31)
    r = service.get_session("user-a", d.session_id)
    assert r.session_status is S.EXPIRED
    assert r.confirmed_answers == []
    r = d.say("no")
    assert r.error.code == "session_expired"
    assert repo.list_records("user-a") == []


def test_host_consent_notice_is_read_with_the_consent_question():
    from tests.conftest import make_service

    service = make_service(consent_notice="Your care team can see saved answers.")
    r = service.start_session("user-a", StartSessionRequest(request_id="r1"))
    assert r.next_question.kind == "consent"
    assert r.next_question.prompt.endswith("Your care team can see saved answers.")
    assert r.speech_text.endswith("Your care team can see saved answers.")

    plain = make_service().start_session("user-a", StartSessionRequest(request_id="r1"))
    assert "care team" not in plain.speech_text
