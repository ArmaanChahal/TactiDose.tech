"""Shared doubles for the agent tests (imported by tests/test_agent_*.py).

* :class:`FakeDrops` - scriptable ``DropServiceAPI``: in-memory containers with pill counts, the
  global cooldown for manual/agent/button sources, configurable today's doses, scripted outcomes.
* :class:`FakeGenai` - scripted stand-in for ``google.genai.Client`` (``client.models.generate_content``)
  that records every call; responses are real ``google.genai.types`` objects.
* :func:`fake_vosk_module` / :func:`make_model_dir` - an in-memory ``vosk`` module and a model folder.
* :func:`make_service` - an ``AgentService`` on the seeded v2 DB with a ``FakeDrops``.
"""

from __future__ import annotations

import json
import types as pytypes
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from tactidose.core.interfaces import (
    ContainerInfo,
    DropOutcome,
    DropServiceAPI,
    PatientStatus,
)
from tests.fakes import seed_v2

COOLDOWN_SOURCES = ("manual", "agent", "button")
MEDS = ("Vitamin C (demo candy)", "Calcium (demo token)", "Omega-3 (demo candy)")


class FakeDrops:
    """In-memory DropServiceAPI with the deterministic rules the agent relies on."""

    def __init__(self, clock: Any, *, patient_id: int, med_ids: list[int], names: tuple[str, ...] = MEDS,
                 pills: int = 20, cooldown_minutes: int = 60, display_name: str = "Alex Rivera") -> None:
        self.clock = clock
        self.patient_id = patient_id
        self.display_name = display_name
        self.cooldown_minutes = cooldown_minutes
        self.containers: dict[int, ContainerInfo] = {
            slot: ContainerInfo(slot=slot, compartment_id=slot + 1, medication_id=mid, medication_name=name,
                                strength="1 piece", pill_count=pills, capacity=30, low_stock_threshold=3)
            for slot, (mid, name) in enumerate(zip(med_ids, names))
        }
        self.requests: list[dict[str, Any]] = []
        self.status_calls: list[int] = []
        self.scripted: deque[Any] = deque()
        self.today: list[dict[str, Any]] = []
        self.next_scheduled: dict[str, Any] | None = None
        self.last_drop_at: datetime | None = None
        self.log: list[dict[str, Any]] = []
        self.interrupts = 0
        self.moving = False
        self.fail_status = False
        self.raise_on_drop = False
        self.auto_drop_enabled = True
        self.events: list[str] = []

    # ------------------------------------------------------------------ helpers for tests
    def set_last_drop(self, minutes_ago: float, slot: int = 0, status: str = "DROPPED") -> None:
        at = self.clock.now() - timedelta(minutes=minutes_ago)
        self.last_drop_at = at
        c = self.containers[slot]
        self.log.append(self._view(len(self.log) + 1, c, "manual", status, None, at))

    def add_dose(self, hour: int, minute: int = 0, *, slot: int = 0, status: str = "SCHEDULED",
                 event_id: int | None = None, **extra: Any) -> dict[str, Any]:
        local = self.clock.local_now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        c = self.containers[slot]
        dose = {"event_id": event_id or len(self.today) + 1, "schedule_id": slot + 1,
                "medication_id": c.medication_id, "medication_name": c.medication_name, "slot": slot,
                "container_number": slot + 1, "scheduled_at": local.astimezone(timezone.utc).isoformat(),
                "scheduled_local": local.isoformat(), "status": status, "drop_id": None, "dispensed_at": None,
                "needs_review": False, **extra}
        self.today.append(dose)
        return dose

    def set_pills(self, slot: int, count: int) -> None:
        self.containers[slot] = replace(self.containers[slot], pill_count=count)

    def script(self, *outcomes: Any) -> None:
        self.scripted.extend(outcomes)

    def outcome(self, status: str, reason: str | None = None, *, slot: int = 0, source: str = "agent",
                message: str = "", **kw: Any) -> DropOutcome:
        c = self.containers[slot]
        return DropOutcome(status=status, source=source, message=message or f"{status} {reason or ''}".strip(),
                           reason=reason, drop_id=kw.pop("drop_id", 99), slot=slot,
                           medication_id=c.medication_id, medication_name=c.medication_name, **kw)

    # ------------------------------------------------------------------ DropServiceAPI
    def request_drop(self, *, patient_id: int, source: str, slot: int | None = None,
                     medication_id: int | None = None, requested_by_user_id: int | None = None,
                     conversation_id: int | None = None, dose_event_id: int | None = None) -> DropOutcome:
        self.events.append("request_drop")
        self.requests.append({"patient_id": patient_id, "source": source, "slot": slot,
                              "medication_id": medication_id, "requested_by_user_id": requested_by_user_id,
                              "conversation_id": conversation_id, "dose_event_id": dose_event_id})
        if self.raise_on_drop:
            raise RuntimeError("drop service bug")
        if self.scripted:
            item = self.scripted.popleft()
            return item() if callable(item) else item
        now = self.clock.now()
        if slot is None and medication_id is not None:
            slot = next((s for s, c in self.containers.items() if c.medication_id == medication_id), None)
        if slot is None or slot not in self.containers:
            return DropOutcome(status="DENIED", source=source, reason="UNKNOWN_MEDICATION",
                               message="Unknown medication.")
        c = self.containers[slot]
        if source in COOLDOWN_SOURCES and self.last_drop_at is not None and self.cooldown_minutes:
            nxt = self.last_drop_at + timedelta(minutes=self.cooldown_minutes)
            if now < nxt:
                remaining = int((nxt - now).total_seconds())
                return DropOutcome(status="DENIED", source=source, reason="COOLDOWN",
                                   message="Too soon for another pill.", slot=slot,
                                   medication_id=c.medication_id, medication_name=c.medication_name,
                                   cooldown_remaining_s=remaining, next_allowed_at=nxt)
        if c.pill_count <= 0:
            return DropOutcome(status="DENIED", source=source, reason="EMPTY", message="Container empty.",
                               slot=slot, medication_id=c.medication_id, medication_name=c.medication_name,
                               pill_count_after=0)
        self.containers[slot] = replace(c, pill_count=c.pill_count - 1)
        self.last_drop_at = now
        drop_id = len(self.log) + 1
        self.log.append(self._view(drop_id, self.containers[slot], source, "DROPPED", conversation_id, now))
        return DropOutcome(status="DROPPED", source=source, message=f"{c.medication_name} dropped.",
                           drop_id=drop_id, slot=slot, medication_id=c.medication_id,
                           medication_name=c.medication_name, pill_count_after=c.pill_count - 1)

    def patient_status(self, patient_id: int) -> PatientStatus:
        self.events.append("patient_status")
        self.status_calls.append(patient_id)
        if self.fail_status:
            raise RuntimeError("database is locked")
        now = self.clock.now()
        remaining, nxt = 0, None
        if self.last_drop_at is not None and self.cooldown_minutes:
            nxt = self.last_drop_at + timedelta(minutes=self.cooldown_minutes)
            remaining = max(0, int((nxt - now).total_seconds()))
            nxt = nxt if remaining else None
        last = next((d for d in reversed(self.log) if d["status"] in ("DROPPED", "UNCERTAIN")), None)
        return PatientStatus(
            patient_id=patient_id, display_name=self.display_name, now_local=self.clock.local_now(),
            containers=tuple(self.containers[s] for s in sorted(self.containers)),
            cooldown_minutes=self.cooldown_minutes, cooldown_remaining_s=remaining, next_manual_allowed_at=nxt,
            last_drop=last, today=tuple(self.today), next_scheduled=self.next_scheduled,
            auto_drop_enabled=self.auto_drop_enabled, device={"connected": True, "state": "READY"},
        )

    def recent_drops(self, patient_id: int, *, days: int = 7, limit: int = 200) -> list[dict[str, Any]]:
        return list(reversed(self.log))[:limit]

    def run_scheduled_drops(self) -> int:
        return 0

    def interrupt(self) -> bool:
        self.interrupts += 1
        return self.moving

    def _view(self, drop_id: int, c: ContainerInfo, source: str, status: str, conversation_id: int | None,
              at: datetime) -> dict[str, Any]:
        return {"drop_id": drop_id, "requested_at": at.isoformat(), "completed_at": at.isoformat(),
                "slot": c.slot, "container_number": c.container_number, "medication_id": c.medication_id,
                "medication_name": c.medication_name, "source": source, "status": status, "reason": None,
                "conversation_id": conversation_id, "needs_review": status == "UNCERTAIN"}


# --------------------------------------------------------------------------- fake Gemini


def fc(name: str, call_id: str | None = None, **args: Any) -> types.Part:
    """A model function-call part."""
    return types.Part(function_call=types.FunctionCall(name=name, args=args, id=call_id or f"id-{name}"))


def text(value: str) -> types.Part:
    return types.Part.from_text(text=value)


def response(*parts: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[
        types.Candidate(content=types.Content(role="model", parts=list(parts)), finish_reason="STOP")])


class FakeModels:
    def __init__(self, script: list[Any]) -> None:
        self.script: deque[Any] = deque(script)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: list[Any], config: Any) -> Any:
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        if not self.script:
            raise AssertionError("unexpected Gemini call")
        step = self.script.popleft()
        if callable(step) and not isinstance(step, BaseException):
            step = step(model=model, contents=contents, config=config)
        if isinstance(step, BaseException):
            raise step
        return step


class FakeGenai:
    """Scripted ``genai.Client`` stand-in: each script item is a response, an exception or a callable."""

    def __init__(self, *script: Any) -> None:
        self.models = FakeModels(list(script))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.models.calls

    def add(self, *script: Any) -> None:
        self.models.script.extend(script)


# --------------------------------------------------------------------------- fake vosk


def make_model_dir(root: Path) -> Path:
    model = root / "vosk-model"
    (model / "am").mkdir(parents=True)
    (model / "am" / "final.mdl").write_bytes(b"x")
    (model / "conf").mkdir()
    return model


def fake_vosk_module(results: list[dict[str, Any]], final: dict[str, Any] | None = None) -> pytypes.ModuleType:
    """``vosk`` replacement: each AcceptWaveform call returns True while ``results`` remain."""
    mod = pytypes.ModuleType("vosk")
    mod.loaded = []  # type: ignore[attr-defined]
    mod.recognizers = []  # type: ignore[attr-defined]

    class Model:
        def __init__(self, path: str) -> None:
            mod.loaded.append(path)  # type: ignore[attr-defined]

    class KaldiRecognizer:
        def __init__(self, model: Any, rate: float, *grammar: Any) -> None:
            self.rate, self.grammar, self.fed, self.words = rate, grammar, [], False
            self._pending = deque(json.dumps(r) for r in results)
            mod.recognizers.append(self)  # type: ignore[attr-defined]

        def SetWords(self, on: bool) -> None:  # noqa: N802 - Vosk API
            self.words = on

        def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802
            self.fed.append(len(data))
            return bool(self._pending)

        def Result(self) -> str:  # noqa: N802
            return self._pending.popleft()

        def FinalResult(self) -> str:  # noqa: N802
            return json.dumps(final or {"text": ""})

    mod.Model = Model  # type: ignore[attr-defined]
    mod.KaldiRecognizer = KaldiRecognizer  # type: ignore[attr-defined]
    mod.SetLogLevel = lambda level: None  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------- service factory


def make_service(db: Any, settings: Any, clock: Any, bus: Any = None, *, genai: Any = None, tts: Any = None,
                 pills: int = 20, cooldown_minutes: int = 60, **overrides: Any) -> tuple[Any, FakeDrops, dict]:
    """Seed the v2 DB, build a FakeDrops for the seeded patient and an AgentService."""
    from tactidose.agent.service import AgentService

    ids = seed_v2(db, settings, now=clock.now() - timedelta(days=2), pills=pills, cooldown_minutes=cooldown_minutes)
    if overrides:
        settings = settings.model_copy(update=overrides)
    drops = FakeDrops(clock, patient_id=ids["patient_id"], med_ids=ids["med_ids"], pills=pills,
                      cooldown_minutes=cooldown_minutes)
    svc = AgentService(db, drops, clock, settings, bus=bus, genai_client=genai, tts=tts)
    return svc, drops, ids


def roles(reply: Any) -> list[str]:
    return [m["role"] for m in reply.messages]


def tool_names(reply: Any) -> list[str]:
    return [m["tool_name"] for m in reply.messages if m["role"] == "tool"]


# --------------------------------------------------------------------------- self-checks


def test_fake_drops_follows_the_cooldown_and_empty_rules(clock):
    drops = FakeDrops(clock, patient_id=1, med_ids=[1, 2, 3], pills=1)
    assert isinstance(drops, DropServiceAPI)
    first = drops.request_drop(patient_id=1, source="agent", slot=0)
    assert first.dropped and first.pill_count_after == 0
    again = drops.request_drop(patient_id=1, source="agent", slot=1)
    assert again.status == "DENIED" and again.reason == "COOLDOWN" and again.cooldown_remaining_s == 3600
    clock.advance(timedelta(minutes=61))
    assert drops.request_drop(patient_id=1, source="manual", slot=0).reason == "EMPTY"


def test_fake_genai_records_calls_and_replays_script():
    client = FakeGenai(response(text("hi")), RuntimeError("boom"))
    assert client.models.generate_content(model="m", contents=[], config=None).candidates[0].content.parts[0].text == "hi"
    with pytest.raises(RuntimeError):
        client.models.generate_content(model="m", contents=[], config=None)
    assert [c["model"] for c in client.calls] == ["m", "m"]


def test_fake_vosk_module_shape(tmp_path):
    mod = fake_vosk_module([{"text": "drop my pill"}])
    rec = mod.KaldiRecognizer(mod.Model(str(make_model_dir(tmp_path))), 16000)
    assert rec.AcceptWaveform(b"\x00\x00") and json.loads(rec.Result())["text"] == "drop my pill"
    assert not rec.AcceptWaveform(b"\x00\x00") and json.loads(rec.FinalResult()) == {"text": ""}

