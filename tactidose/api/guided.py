"""``/api/demo/guided/*`` — the guided, voice-first judge demo (demo mode only).

Who: the device's patient and their linked doctor/family (``DemoViewer``, as the demo panel).
``reset: true`` re-seeds the demo data first, so it needs a doctor/family account linked to the
device's patient (the same rule as ``POST /api/demo/reset``). Progress arrives on the event stream
as ``demo.guided``; the runner itself is ``tactidose/guided/runner.py``.

* ``POST /api/demo/guided/start`` ``{reset?: bool}`` -> run state (409 while one is running)
* ``POST /api/demo/guided/answer`` ``{text, input_mode?: "text"|"voice"}`` -> ``{accepted}``
* ``POST /api/demo/guided/stop`` -> ``{stopped}`` (also stops the dispenser)
* ``GET /api/demo/guided`` -> run state or ``{state: "idle"}``
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from tactidose.api import domain
from tactidose.api.common import TactiRoute
from tactidose.api.device import DemoViewer, NO_PATIENT
from tactidose.auth.deps import ServicesDep, check_edit

router = APIRouter(prefix="/api/demo/guided", route_class=TactiRoute, tags=["demo"])


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reset: StrictBool = False


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(min_length=1, max_length=1000)
    input_mode: Literal["text", "voice"] = "text"


def _runner(services: Any) -> Any:
    runner = getattr(services, "guided", None)
    if runner is None:
        raise HTTPException(503, "The guided demo is not available right now.")
    return runner


def _patient(services: Any) -> int:
    pid = domain.device_patient(services)
    if pid is None:
        raise HTTPException(409, NO_PATIENT)
    return pid


@router.post("/start")
def start(user: DemoViewer, services: ServicesDep, body: StartBody | None = None) -> dict[str, Any]:
    from tactidose.guided import GuidedDemoError

    pid = _patient(services)
    reset = bool(body and body.reset)
    if reset:
        check_edit(services, user, pid)   # re-seeding demo data: doctor/family only
    try:
        return _runner(services).start(pid, started_by=user.user_id, reset=reset)
    except GuidedDemoError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/answer")
def answer(body: AnswerBody, user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Please say or type an answer.")
    return {"accepted": _runner(services).answer(_patient(services), text, input_mode=body.input_mode)}


@router.post("/stop")
def stop(user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    return {"stopped": _runner(services).stop(_patient(services))}


@router.get("")
def get_state(user: DemoViewer, services: ServicesDep) -> dict[str, Any]:
    return _runner(services).state(_patient(services)) or {"state": "idle"}
