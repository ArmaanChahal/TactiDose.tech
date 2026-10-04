"""``/api/auth/*`` and ``/api/care/*``: registration, login sessions, care links (docs/API.md v2).

Login and registration set the HttpOnly session cookie (SameSite=Lax, ``Secure`` when
``settings.cookie_secure``, ``max_age`` = session TTL) *and* return the token for API clients.
Logout revokes the session and clears the cookie.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from tactidose.api import views
from tactidose.api.common import TactiRoute
from tactidose.auth.deps import CaregiverUser, CurrentUser, ServicesDep, session_token
from tactidose.core.interfaces import AuthUser

log = logging.getLogger(__name__)

router = APIRouter(route_class=TactiRoute, tags=["auth"])


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["patient", "doctor", "family"]
    phone: str | None = Field(None, max_length=32)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: StrictInt = Field(ge=1)
    link_code: str = Field(min_length=1, max_length=32)


# --------------------------------------------------------------------------- cookie


def set_session_cookie(response: Response, settings: Any, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=int(settings.session_ttl_hours) * 3600,
        path="/",
        httponly=True,
        samesite="lax",
        secure=bool(settings.cookie_secure),
    )


def clear_session_cookie(response: Response, settings: Any) -> None:
    response.delete_cookie(
        settings.session_cookie_name, path="/", httponly=True, samesite="lax",
        secure=bool(settings.cookie_secure),
    )


def _user_out(services: Any, user: AuthUser) -> dict[str, Any]:
    """API.md ``User`` (``AuthService.user_to_dict`` when available, else read from ``users``)."""
    fn = getattr(services.auth, "user_to_dict", None)
    if callable(fn):
        return fn(user)
    return views.user_view(services.db, user.user_id) or {**user.to_dict(), "phone": None, "created_at": None}


def _patient_profile(services: Any, patient_id: int) -> dict[str, Any]:
    fn = getattr(services.auth, "patient_profile", None)
    return fn(patient_id) if callable(fn) else views.patient_profile(services.db, patient_id)


def _links(services: Any, caregiver: AuthUser) -> list[dict[str, Any]]:
    """``[{patient_id, display_name, relationship}]`` for a caregiver."""
    fn = getattr(services.auth, "care_patients", None)
    return list(fn(caregiver)) if callable(fn) else views.care_links(services.db, caregiver.user_id)


def _open_session(services: Any, user: AuthUser, body: "RegisterBody", user_agent: str | None) -> str:
    """A session right after registering (``start_session`` avoids a second password hash)."""
    start = getattr(services.auth, "start_session", None)
    if callable(start):
        return start(user, user_agent=user_agent)
    return services.auth.login(body.email, body.password, user_agent=user_agent)[1]


# --------------------------------------------------------------------------- care patients


def care_patient(services: Any, caregiver_id: int, link: dict[str, Any]) -> dict[str, Any]:
    """API.md ``CarePatient`` for one link ``{patient_id, display_name, relationship}``."""
    pid = int(link["patient_id"])
    last_drop = None
    try:
        last_drop = services.drops.patient_status(pid).last_drop
    except Exception:  # noqa: BLE001 - one patient's status must not break the list
        log.exception("care list: status of patient %s failed", pid)
    return {
        "patient_id": pid,
        "display_name": link.get("display_name"),
        "relationship": link.get("relationship"),
        "last_drop": last_drop,
        "unread_alerts": views.unread_alerts(services.db, caregiver_id, pid),
        "adherence_7d": views.adherence(services.db, services.clock.now(), pid, days=7),
    }


def care_patients(services: Any, caregiver: AuthUser) -> list[dict[str, Any]]:
    return [care_patient(services, caregiver.user_id, link) for link in _links(services, caregiver)]


# --------------------------------------------------------------------------- auth


@router.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterBody, request: Request, response: Response, services: ServicesDep) -> dict[str, Any]:
    settings = services.settings
    if not settings.allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is disabled on this TactiDose.")
    user = services.auth.register(
        email=body.email, password=body.password, display_name=body.display_name.strip(),
        role=body.role, phone=(body.phone or None),
    )
    token = _open_session(services, user, body, _user_agent(request))
    set_session_cookie(response, settings, token)
    out: dict[str, Any] = {"user": _user_out(services, user), "token": token}
    if user.is_patient:
        profile = _patient_profile(services, user.user_id)
        out["patient"] = {"patient_id": user.user_id, "link_code": profile.get("link_code")}
    log.info("registered %s account %s", user.role, user.user_id)
    return out


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, services: ServicesDep) -> dict[str, Any]:
    user, token = services.auth.login(body.email, body.password, user_agent=_user_agent(request))
    set_session_cookie(response, services.settings, token)
    return {"user": _user_out(services, user), "token": token}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, services: ServicesDep) -> Any:
    settings = services.settings
    token = session_token(request, settings)
    user = services.auth.resolve(token) if token else None
    if user is None:
        out = JSONResponse({"detail": "Please sign in."}, status_code=status.HTTP_401_UNAUTHORIZED)
        clear_session_cookie(out, settings)
        return out
    services.auth.logout(token)
    clear_session_cookie(response, settings)
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: CurrentUser, services: ServicesDep) -> dict[str, Any]:
    out: dict[str, Any] = {"user": _user_out(services, user)}
    if user.is_patient:
        out["patient"] = _patient_profile(services, user.user_id)
    elif user.is_caregiver:
        out["patients"] = care_patients(services, user)
    return out


# --------------------------------------------------------------------------- care links


@router.get("/api/care/patients")
def list_care_patients(user: CaregiverUser, services: ServicesDep) -> list[dict[str, Any]]:
    return care_patients(services, user)


@router.post("/api/care/links", status_code=status.HTTP_201_CREATED)
def link_patient(body: LinkBody, user: CaregiverUser, services: ServicesDep) -> dict[str, Any]:
    link = services.auth.link_patient(caregiver=user, patient_id=body.patient_id, link_code=body.link_code.strip())
    if not isinstance(link, dict) or "patient_id" not in link:
        link = next((x for x in _links(services, user) if x.get("patient_id") == body.patient_id), None)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient not found.")
    log.info("caregiver %s linked to patient %s", user.user_id, body.patient_id)
    return care_patient(services, user.user_id, link)


@router.delete("/api/care/links/{patient_id}")
def unlink_patient(patient_id: int, user: CaregiverUser, services: ServicesDep) -> dict[str, Any]:
    services.auth.unlink_patient(caregiver=user, patient_id=patient_id)
    return {"ok": True}


def _user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:255] if ua else None
