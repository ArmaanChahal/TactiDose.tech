"""Optional device-side voice + button loop (tactidose/agent/voice_loop.py)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from tactidose.agent import AgentUnavailable
from tactidose.agent.voice_loop import DeviceVoiceLoop
from tactidose.core import phrases
from tactidose.core.bus import Topic
from tactidose.core.interfaces import AgentReply
from tactidose.hardware.protocol import Err, Ev, Message, MessageKind
from tests.fakes import FakeDropHardware, FakeSpeaker
from tests.test_agent_support import FakeDrops, make_service


class FakeAgent:
    def __init__(self, text: str = "Nothing is due right now.") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def chat(self, *, patient_id: int, text: str, input_mode: str = "text",
             conversation_id: int | None = None) -> AgentReply:
        self.calls.append({"patient_id": patient_id, "text": text, "input_mode": input_mode,
                           "conversation_id": conversation_id})
        if self.error is not None:
            raise self.error
        return AgentReply(conversation_id=7, text=self.text, model="rules")

    def conversations(self, patient_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def messages(self, patient_id: int, conversation_id: int) -> list[dict[str, Any]]:
        return []


class FakeRecognizer:
    def __init__(self, on_text, is_muted) -> None:
        self.on_text, self.is_muted = on_text, is_muted
        self.started = threading.Event()
        self.closed = False

    def start(self) -> bool:
        self.started.set()
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def loop_env(settings_v2, clock, bus):
    drops = FakeDrops(clock, patient_id=1, med_ids=[11, 12, 13])
    agent = FakeAgent()
    speaker = FakeSpeaker()
    hw = FakeDropHardware()
    recognizers: list[FakeRecognizer] = []

    def factory(*, on_text, is_muted):
        rec = FakeRecognizer(on_text, is_muted)
        recognizers.append(rec)
        return rec

    loop = DeviceVoiceLoop(settings_v2, agent=agent, drops=drops, clock=clock, hardware=hw, bus=bus,
                           patient_id=1, speaker=speaker, recognizer_factory=factory)
    loop.boot_grace_s = 0.0
    assert loop.start() is True
    yield loop, drops, agent, speaker, hw, recognizers
    loop.close()


def event(code: Ev) -> Message:
    return Message(MessageKind.EVENT, code.value, (), raw=f"EVENT {code.value}")


def test_confirm_button_drops_the_due_dose(loop_env):
    loop, drops, agent, speaker, hw, _ = loop_env
    drops.add_dose(8, 0, slot=1)
    hw.emit_event(Ev.CONFIRM_BUTTON)
    assert loop.wait_idle(5)
    assert drops.requests == [{"patient_id": 1, "source": "button", "slot": None, "medication_id": 12,
                               "requested_by_user_id": 1, "conversation_id": None, "dose_event_id": None}]
    assert speaker.said[-1] == ("Calcium dropped from container 2.", "success")
    assert agent.calls == []


def test_confirm_button_with_nothing_due_speaks_the_status(loop_env):
    loop, drops, agent, speaker, hw, _ = loop_env
    drops.next_scheduled = drops.add_dose(13, 0, slot=1)
    hw.emit_event(Ev.CONFIRM_BUTTON)
    assert loop.wait_idle(5)
    assert drops.requests == []
    assert speaker.last() == "Nothing is due right now. Your next scheduled pill is Calcium at 1:00 PM."


def test_confirm_button_refusal_is_spoken(loop_env):
    loop, drops, agent, speaker, hw, _ = loop_env
    drops.add_dose(8, 0, slot=0)
    drops.set_last_drop(minutes_ago=10)
    hw.emit_event(Ev.CONFIRM_BUTTON)
    assert loop.wait_idle(5)
    assert speaker.said[-1] == ("It's too soon for another pill. The next pill can drop at 8:45 AM, in 50 minutes.",
                                "warning")


def test_cancel_button_only_enqueues_and_drops_queued_work(loop_env):
    loop, drops, agent, speaker, hw, _ = loop_env
    drops.add_dose(8, 0, slot=0)
    gate, entered = threading.Event(), threading.Event()
    original = drops.patient_status

    def slow_status(pid):
        entered.set()
        gate.wait(5)
        return original(pid)

    drops.patient_status = slow_status  # type: ignore[method-assign]
    hw.emit_event(Ev.CONFIRM_BUTTON)          # running (blocked in patient_status)
    assert entered.wait(5)
    hw.emit_event(Ev.CONFIRM_BUTTON)          # queued
    hw.emit_event(Ev.CANCEL_BUTTON)           # drops the queued confirm; no host STOP
    gate.set()
    assert loop.wait_idle(5)
    assert drops.interrupts == 0
    assert len(drops.requests) == 1
    assert (phrases.STOPPED, "info") in speaker.said and speaker.interrupts >= 1


def test_voice_stop_interrupts_immediately(loop_env):
    loop, drops, agent, speaker, hw, recognizers = loop_env
    assert recognizers and recognizers[0].started.wait(5)
    drops.moving = True
    recognizers[0].on_text("stop", 0.4)
    assert drops.interrupts == 1                       # synchronous, before the worker runs
    assert loop.wait_idle(5)
    assert speaker.said[-1] == (phrases.STOPPED, "info") and agent.calls == []


def test_voice_text_goes_to_the_agent_and_is_spoken(loop_env):
    loop, drops, agent, speaker, hw, recognizers = loop_env
    assert recognizers[0].started.wait(5)
    recognizers[0].on_text("what is due", 0.9)
    assert loop.wait_idle(5)
    recognizers[0].on_text("drop my pill", 0.9)
    assert loop.wait_idle(5)
    assert [c["input_mode"] for c in agent.calls] == ["voice", "voice"]
    assert agent.calls[0]["conversation_id"] is None and agent.calls[1]["conversation_id"] == 7
    assert speaker.texts[-1] == "Nothing is due right now."
    assert recognizers[0].is_muted() is False
    speaker.speaking = True
    assert recognizers[0].is_muted() is True


def test_agent_unavailable_is_spoken(loop_env):
    loop, drops, agent, speaker, hw, recognizers = loop_env
    agent.error = AgentUnavailable("db down")
    loop.handle_text("help")
    assert loop.wait_idle(5)
    assert speaker.said[-1] == (phrases.AGENT_ERROR, "error")


def test_restart_and_fault_are_announced_once(loop_env, bus):
    loop, drops, agent, speaker, hw, _ = loop_env
    sub = bus.subscribe([Topic.NOTICE])
    hw.emit_event(Ev.BOOT)
    hw.emit_event(Ev.BOOT)
    assert loop.wait_idle(5)
    loop.on_hardware_event(Message(MessageKind.ERR, Err.MOTOR_FAULT.value, (), raw="ERR MOTOR_FAULT"))
    bus.publish(Topic.DEVICE_STATE, {"state": "FAULT"})
    assert loop.wait_idle(5)
    assert speaker.texts.count(phrases.DEVICE_RESTARTED) == 1
    assert speaker.texts.count(phrases.DEVICE_NEEDS_ATTENTION) == 1
    bus.publish(Topic.DEVICE_STATE, {"state": "READY"})
    bus.publish(Topic.DEVICE_STATE, {"state": "FAULT"})      # a new fault episode
    assert loop.wait_idle(5)
    assert speaker.texts.count(phrases.DEVICE_NEEDS_ATTENTION) == 2
    assert [e.data["code"] for e in sub.drain()] == ["DEVICE_BOOT", "DEVICE_FAULT", "DEVICE_FAULT"]


def test_boot_during_start_up_grace_is_silent(settings_v2, clock, bus):
    speaker = FakeSpeaker()
    loop = DeviceVoiceLoop(settings_v2, agent=FakeAgent(), drops=FakeDrops(clock, patient_id=1, med_ids=[1, 2, 3]),
                           clock=clock, bus=bus, patient_id=1, speaker=speaker)
    assert loop.start()
    loop.on_hardware_event(event(Ev.BOOT))
    assert loop.wait_idle(5)
    assert speaker.said == []
    loop.close()


def test_patient_comes_from_the_device_binding(db_v2, settings_v2, clock, bus):
    svc, drops, ids = make_service(db_v2, settings_v2, clock, bus)
    speaker = FakeSpeaker()
    loop = DeviceVoiceLoop(settings_v2, agent=svc, drops=drops, clock=clock, bus=bus, db=db_v2, speaker=speaker)
    assert loop.start()
    loop.handle_text("drop my vitamin c")
    assert loop.wait_idle(5)
    assert speaker.last() == "Vitamin C dropped from container 1."
    assert loop.status()["patient_id"] == ids["patient_id"] and loop.conversation_id is not None
    assert drops.requests[0]["source"] == "agent"
    assert svc.messages(ids["patient_id"], loop.conversation_id)[0]["input_mode"] == "voice"
    loop.close()


def test_no_patient_means_not_set_up(settings_v2, clock):
    speaker = FakeSpeaker()
    loop = DeviceVoiceLoop(settings_v2, agent=FakeAgent(), drops=FakeDrops(clock, patient_id=1, med_ids=[1, 2, 3]),
                           clock=clock, speaker=speaker)
    assert loop.start()
    loop.on_hardware_event(event(Ev.CONFIRM_BUTTON))
    loop.handle_text("help")
    assert loop.wait_idle(5)
    assert speaker.texts == [phrases.NOT_SET_UP, phrases.NOT_SET_UP]
    loop.close()


def test_lifecycle(loop_env):
    loop, drops, agent, speaker, hw, recognizers = loop_env
    assert recognizers[0].started.wait(5)
    assert loop.start() is True and loop.status()["running"] is True
    loop.close()
    loop.close()
    assert recognizers[0].closed and hw._listeners == []
    assert loop.start() is False
    loop.handle_text("help")
    assert agent.calls == []
    assert not any(t.name == "voice-loop" and t.is_alive() for t in threading.enumerate())


def test_voice_disabled_skips_the_recognizer(settings_v2, clock):
    loop = DeviceVoiceLoop(settings_v2, agent=FakeAgent(), drops=FakeDrops(clock, patient_id=1, med_ids=[1, 2, 3]),
                           clock=clock, patient_id=1, speaker=FakeSpeaker())
    assert settings_v2.voice_enabled is False
    assert loop.start() and loop._start_thread is None
    loop.close()
