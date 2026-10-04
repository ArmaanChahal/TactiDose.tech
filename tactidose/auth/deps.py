"""FastAPI dependencies for sessions and the v2 permission matrix (ARCHITECTURE §9).

A session token comes from ``Authorization: Bearer <token>`` (API clients) or the HttpOnly
cookie ``settings.session_cookie_name`` (browsers). ``AuthServiceAPI.resolve`` turns it into
an :class:`~tactidose.core.interfaces.AuthUser`; every decision below is made from that user
and ``care_links`` (through ``can_view`` / ``can_edit``), never from data the client sends.

Dependencies (the ones that hit the DB — :func:`current_user`, :func:`require_patient_view`,
:func:`require_patient_edit` — are synchronous, so FastAPI runs them in the threadpool; the pure
checks are ``async`` and cost no thread hop):

* :func:`current_user` – the user or ``None``; :func:`require_user` – 401 without a session.
* :func:`require_caregiver` / :func:`require_patient_role` – role checks (403).
* :func:`require_patient_view` / :func:`require_patient_edit` / :func:`require_patient_self` –
  for ``/api/patients/{pid}/…``: the patient themself or a linked caregiver / a linked
  doctor-or-family account only / the patient themself only (403 otherwise).
* :func:`demo_only` (403 unless ``settings.demo_mode``) and :func:`require_demo_user`
  (demo mode *and* a session).

The ``check_*`` functions apply the same rules where the patient id is not in the path
(e.g. ``/api/reports/{rid}`` follows the report's patient).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from tactidose.core.interfaces import AuthUser

log = logging.getLogger(__name__)

__all__ = [
    "CaregiverUser",
    "CurrentUser",
    "DemoUser",
    "OptionalUser",
    "PatientEditor",
    "PatientSelf",
    "PatientUser",
    "PatientViewer",
    "ServicesDep",
    "bearer_token",
    "check_edit",
    "check_self",
    "check_view",
    "current_user",
    "demo_only",
    "get_services",
    "require_caregiver",
    "require_demo_user",
    "require_patient_edit",
    "require_patient_role",
    "require_patient_self",
    "require_patient_view",
    "require_user",
    "session_token",
]

NOT_SIGNED_IN = "Please sign in."
NOT_ALLOWED = "You do not have access to this patient."
CAREGIVER_ONLY = "Only a linked doctor or family member can change this."
PATIENT_ONLY = "Only the patient can do this."
DEMO_ONLY = "Demo mode is off."


async def get_services(request: Request) -> Any:
    """The application's ``tactidose.app.Services`` (set by ``create_app``)."""
    return request.app.state.services


ServicesDep = Annotated[Any, Depends(get_services)]


# --------------------------------------------------------------------------- tokens


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def session_token(request: Request, settings: Any) -> str | None:
    """Bearer header first (explicit), then the session cookie."""
    token = bearer_token(request)
    if token:
        return token
    cookie = request.cookies.get(settings.session_cookie_name)
    return cookie.strip() if cookie and cookie.strip() else None


# --------------------------------------------------------------------------- users


def current_user(request: Request, services: ServicesDep) -> AuthUser | None:
    """Resolve the session (cached per request in ``request.state.user``)."""
    if hasattr(request.state, "user"):
        return request.state.user
    token = session_token(request, services.settings)
    user = services.auth.resolve(token) if token else None
    request.state.user = user
    request.state.token = token if user is not None else None
    return user


OptionalUser = Annotated[AuthUser | None, Depends(current_user)]


async def require_user(user: OptionalUser) -> AuthUser:
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, NOT_SIGNED_IN, headers={"WWW-Authenticate": "Bearer"}
        )
    return user


CurrentUser = Annotated[AuthUser, Depends(require_user)]


async def require_caregiver(user: CurrentUser) -> AuthUser:
    if not user.is_caregiver:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This is only for doctor and family accounts.")
    return user


async def require_patient_role(user: CurrentUser) -> AuthUser:
    if not user.is_patient:
        raise HTTPException(status.HTTP_403_FORBIDDEN, PATIENT_ONLY)
    return user


CaregiverUser = Annotated[AuthUser, Depends(require_caregiver)]
PatientUser = Annotated[AuthUser, Depends(require_patient_role)]


# --------------------------------------------------------------------------- patient access


def check_view(services: Any, user: AuthUser, patient_id: Any) -> None:
    """403 unless ``user`` is the patient or a caregiver linked to them."""
    if not isinstance(patient_id, int) or isinstance(patient_id, bool) or patient_id < 1:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ALLOWED)
    if user.is_patient and user.user_id == patient_id:
        return
    if not services.auth.can_view(user, patient_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_ALLOWED)


def check_edit(services: Any, user: AuthUser, patient_id: Any) -> None:
    """403 unless ``user`` is a doctor/family account linked to the patient."""
    check_view(services, user, patient_id)
    if not user.is_caregiver or not services.auth.can_edit(user, patient_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, CAREGIVER_ONLY)


def check_self(user: AuthUser, patient_id: Any) -> None:
    """403 unless ``user`` is this patient."""
    if not (user.is_patient and user.user_id == patient_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, PATIENT_ONLY)


def require_patient_view(pid: int, user: CurrentUser, services: ServicesDep) -> AuthUser:
    check_view(services, user, pid)
    return user


def require_patient_edit(pid: int, user: CurrentUser, services: ServicesDep) -> AuthUser:
    check_edit(services, user, pid)
    return user


async def require_patient_self(pid: int, user: CurrentUser) -> AuthUser:
    check_self(user, pid)
    return user


PatientViewer = Annotated[AuthUser, Depends(require_patient_view)]
PatientEditor = Annotated[AuthUser, Depends(require_patient_edit)]
PatientSelf = Annotated[AuthUser, Depends(require_patient_self)]


# --------------------------------------------------------------------------- demo


async def demo_only(services: ServicesDep) -> None:
    if not services.settings.demo_mode:
        raise HTTPException(status.HTTP_403_FORBIDDEN, DEMO_ONLY)


async def require_demo_user(_demo: Annotated[None, Depends(demo_only)], user: CurrentUser) -> AuthUser:
    """Demo mode first (403 even without a session), then a signed-in user (401)."""
    return user


DemoUser = Annotated[AuthUser, Depends(require_demo_user)]
