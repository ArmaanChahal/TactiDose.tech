"""reports.narrative: rules summary, excerpt selection and the (faked) Gemini summariser."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import SecretStr

from tactidose.reports.data import gather_report_data
from tactidose.reports.narrative import (
    SYSTEM_INSTRUCTION,
    ReportNarrator,
    build_prompt,
    clean_ai_text,
    conversation_turns,
    rules_narrative,
    select_excerpts,
)
from tactidose.reports.stats import compute_stats
from tests.fakes import seed_v2
from tests.test_reports_support import (
    AGENT_REPLY,
    COOLDOWN_MSG,
    HEADACHE,
    FakeAPIError,
    FakeGenaiClient,
    FakeGenaiResponse,
    seed_report_scenario,
)


@pytest.fixture
def scenario(db_v2, settings_v2, clock):
    ids = seed_report_scenario(db_v2, settings_v2, clock)
    now = clock.now()
    data = gather_report_data(db_v2, clock, settings_v2, patient_id=ids["patient_id"], days=7,
                              period_start=now - timedelta(days=7), period_end=now)
    return data, compute_stats(data)


@pytest.fixture
def ai_settings(settings_v2):
    return settings_v2.model_copy(update={"report_ai_summary": True, "gemini_api_key": SecretStr("test-key-123")})


def test_rules_narrative_bullets(scenario):
    data, stats = scenario
    text = rules_narrative(data, stats)
    lines = text.splitlines()
    assert lines[0] == "- Scheduled doses: 7. Dropped: 5 (4 on time, 1 late). Missed: 1. Still open: 1."
    assert "- Adherence for scheduled doses: 83% (5 of 6)." in lines
    assert "- Pills dropped on request: 2 (1 via the app button, 1 via the assistant)." in lines
    assert ("- Refused requests: 3 (cooldown after a recent drop: 2; "
            "an uncertain drop is waiting for review: 1).") in lines
    assert "- Drop problems: 1 failed, 1 uncertain (1 still to be reviewed)." in lines
    assert "- Alerts sent: 1 low-stock, 1 missed-dose." in lines
    assert "- Pill requests through the assistant: 2 (1 refused, 1 dropped)." in lines
    # the patient's words are quoted verbatim with the local time, not interpreted
    assert f'  - Sun 4 Oct, 8:19 AM: "{HEADACHE}"' in lines
    assert '  - Mon 5 Oct, 7:49 AM: "Drop my vitamin please"' in lines
    assert "diagnos" not in text.lower() and "recommend" not in text.lower()


def test_rules_narrative_without_conversations(db_v2, settings_v2, clock):
    ids = seed_v2(db_v2, settings_v2)
    now = clock.now()
    data = gather_report_data(db_v2, clock, settings_v2, patient_id=ids["patient_id"], days=3,
                              period_start=now - timedelta(days=3), period_end=now)
    text = rules_narrative(data, compute_stats(data))
    assert text.splitlines() == ["- No scheduled doses in this period.",
                                 "- No conversations with the assistant in this period."]


def test_turns_and_excerpts(scenario):
    data, _ = scenario
    turns = conversation_turns(data)
    assert len(turns) == 2 and turns[0].concern and turns[0].refused and turns[1].asks_for_pill
    ex = select_excerpts(data)
    assert [(e.speaker, e.text) for e in ex] == [
        ("patient", HEADACHE),
        ("dispenser", f"Pill request refused (cooldown after a recent drop): {COOLDOWN_MSG}"),
        ("assistant", AGENT_REPLY),
        ("patient", "Drop my vitamin please"),
        ("dispenser", "Pill request dropped: Vitamin C (demo candy) dropped from container 1."),
        ("assistant", "Done. Your Vitamin C dropped from container 1."),
    ]
    assert ex[0].input_mode == "voice" and ex[0].notable
    assert [e.at for e in ex] == sorted(e.at for e in ex)
    only_one = select_excerpts(data, max_turns=1)
    assert [e.speaker for e in only_one] == ["patient", "dispenser", "assistant"]
    assert only_one[0].text == "Drop my vitamin please"   # newest notable turn is kept


def test_narrator_disabled_never_calls_gemini(scenario, settings_v2):
    data, stats = scenario
    client = FakeGenaiClient()
    n = ReportNarrator(settings_v2, client=client).build(data, stats)
    assert n.source == "rules" and n.fallback_reason == "disabled" and client.calls == []
    assert n.text == rules_narrative(data, stats)


def test_narrator_not_configured(scenario, settings_v2):
    data, stats = scenario
    n = ReportNarrator(settings_v2.model_copy(update={"report_ai_summary": True})).build(data, stats)
    assert n.source == "rules" and n.fallback_reason == "not_configured"


def test_narrator_gemini_success(scenario, ai_settings):
    data, stats = scenario
    client = FakeGenaiClient(FakeGenaiResponse(
        "## Summary\n**The patient** asked for a pill twice.\n* On Sun 4 Oct they said \"My head hurts a little.\"\n"
        "* One request was refused because of the cooldown."))
    n = ReportNarrator(ai_settings, client=client).build(data, stats)
    assert n.source == "gemini" and n.model == ai_settings.gemini_model and n.fallback_reason is None
    assert n.text == ("Summary\nThe patient asked for a pill twice.\n"
                      "- On Sun 4 Oct they said \"My head hurts a little.\"\n"
                      "- One request was refused because of the cooldown.")
    call = client.calls[0]
    assert call["model"] == ai_settings.gemini_model
    config = call["config"]
    assert config["system_instruction"] == SYSTEM_INSTRUCTION and "temperature" not in config
    assert "Do not diagnose" in SYSTEM_INSTRUCTION and "200 words" in SYSTEM_INSTRUCTION
    prompt = call["contents"]
    assert HEADACHE in prompt and "Patient (voice)" in prompt and COOLDOWN_MSG in prompt
    assert "refused (cooldown after a recent drop)" in prompt
    assert "Alex Rivera" not in prompt and "alex@test.tactidose" not in prompt  # no identifiers sent
    assert "This old message is outside the period." not in prompt


def test_narrator_caps_words(scenario, ai_settings):
    data, stats = scenario
    client = FakeGenaiClient(FakeGenaiResponse(" ".join(f"word{i}" for i in range(300))))
    n = ReportNarrator(ai_settings, client=client).build(data, stats)
    assert n.source == "gemini" and len(n.text.split()) == 200 and n.text.endswith("word199…")


@pytest.mark.parametrize("result,reason", [
    (TimeoutError("read timed out"), "timeout"),
    (ConnectionError("no route"), "network"),
    (FakeGenaiResponse("", finish="STOP"), "empty"),
    (FakeGenaiResponse(None, finish="SAFETY"), "blocked"),
    (FakeGenaiResponse("partial", finish="MAX_TOKENS"), "truncated"),
    (FakeGenaiResponse("ok", block="SAFETY"), "blocked"),
    (FakeGenaiResponse("The patient probably has a migraine. I recommend a higher dose."), "unsafe_output"),
])
def test_narrator_falls_back_to_rules(scenario, ai_settings, result, reason):
    data, stats = scenario
    n = ReportNarrator(ai_settings, client=FakeGenaiClient(result)).build(data, stats)
    assert n.source == "rules" and n.fallback_reason == reason
    assert n.text == rules_narrative(data, stats)


def test_narrator_api_error_and_quotes_are_allowed(scenario, ai_settings, caplog):
    data, stats = scenario
    err = FakeAPIError(503, "UNAVAILABLE", "overloaded test-key-123")
    with caplog.at_level("WARNING"):
        n = ReportNarrator(ai_settings, client=FakeGenaiClient(err)).build(data, stats)
    assert n.source == "rules" and n.fallback_reason == "api_error:503"
    assert "api_error:503" in caplog.text and "test-key-123" not in caplog.text   # key redacted
    quoted = FakeGenaiResponse('The patient said "I was diagnosed with migraines, should I take more?"')
    n = ReportNarrator(ai_settings, client=FakeGenaiClient(quoted)).build(data, stats)
    assert n.source == "gemini"   # prescriptive words inside the patient's quote are not the model's advice


def test_narrator_retries_fallback_model_on_404(scenario, ai_settings):
    data, stats = scenario
    missing = FakeAPIError(404, "NOT_FOUND", "models/x is not found")
    client = FakeGenaiClient(missing, FakeGenaiResponse("- The patient asked for a pill twice."))
    narrator = ReportNarrator(ai_settings, client=client)
    n = narrator.build(data, stats)
    assert n.source == "gemini" and n.model == ai_settings.gemini_fallback_model
    assert [c["model"] for c in client.calls] == [ai_settings.gemini_model, ai_settings.gemini_fallback_model]
    assert narrator.model == ai_settings.gemini_fallback_model


def test_narrator_skips_gemini_without_conversations(db_v2, settings_v2, clock, ai_settings):
    ids = seed_v2(db_v2, settings_v2)
    now = clock.now()
    data = gather_report_data(db_v2, clock, settings_v2, patient_id=ids["patient_id"], days=2,
                              period_start=now - timedelta(days=2), period_end=now)
    client = FakeGenaiClient()
    n = ReportNarrator(ai_settings, client=client).build(data, compute_stats(data))
    assert n.source == "rules" and n.fallback_reason == "no_conversations" and client.calls == []


def test_narrator_without_sdk_client_or_key(scenario, settings_v2, monkeypatch):
    data, stats = scenario
    import builtins

    real_import = builtins.__import__

    def no_genai(name, *a, **kw):
        if name.startswith("google"):
            raise ImportError("no google-genai")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_genai)
    keyed = settings_v2.model_copy(update={"report_ai_summary": True, "gemini_api_key": SecretStr("k")})
    n = ReportNarrator(keyed).build(data, stats)
    assert n.source == "rules" and n.fallback_reason == "sdk_missing"


def test_narrator_real_client_never_follows_redirects(scenario, ai_settings, caplog):
    import httpx

    data, stats = scenario
    narrator = ReportNarrator(ai_settings)
    client = narrator._get_client()          # a real genai.Client: construction makes no network call
    try:
        http = client._api_client._httpx_client
        assert http.follow_redirects is False
        seen: list[httpx.Request] = []

        def filter_proxy(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "sso.example.com":
                return httpx.Response(200, text="<html>sign in</html>")
            return httpx.Response(307, headers={"location": "https://sso.example.com/login?user=alex"})

        http._mounts = {}                    # no proxy mounts, no sockets: everything goes to the mock
        http._transport = httpx.MockTransport(filter_proxy)
        with caplog.at_level("WARNING"):
            n = narrator.build(data, stats)
        assert n.source == "rules" and n.fallback_reason == "api_error:307"
        assert [r.url.host for r in seen] == ["generativelanguage.googleapis.com"]   # 307 not followed
        assert "api_error:307; BLOCKED_BY_NETWORK: the network redirected the request" in caplog.text
        assert "alex" not in caplog.text and "test-key-123" not in caplog.text
    finally:
        client.close()


def test_narrator_logs_netsafe_messages_not_raw_exception_text(scenario, ai_settings, caplog):
    data, stats = scenario
    err = ConnectionError("cannot reach https://generativelanguage.googleapis.com/v1beta/models/x?trace=abc")
    with caplog.at_level("WARNING"):
        n = ReportNarrator(ai_settings, client=FakeGenaiClient(err)).build(data, stats)
    assert n.fallback_reason == "network"
    assert "network; NETWORK_ERROR: could not connect" in caplog.text
    assert "googleapis.com" not in caplog.text and "trace=abc" not in caplog.text


def test_build_prompt_is_bounded(scenario):
    data, stats = scenario
    prompt = build_prompt(data, stats)
    assert prompt.startswith("Report period: 28 Sep 2026, 7:55 AM to 5 Oct 2026, 7:55 AM (America/Vancouver).")
    assert "Refused pill requests in the dispenser log: 3." in prompt
    assert prompt.index(HEADACHE) < prompt.index("Drop my vitamin please")
    assert "get_patient_status" not in prompt   # internal tool chatter is not sent


def test_clean_ai_text():
    assert clean_ai_text("```text\n# Title\n\n\n1. one\n• two\n**bold**  text\n```") == \
        "Title\n\n- one\n- two\nbold text"
    assert clean_ai_text("  ") == ""
