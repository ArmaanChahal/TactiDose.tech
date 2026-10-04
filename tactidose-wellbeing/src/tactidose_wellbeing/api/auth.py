"""Identity providers for the REST API.

The REST API never trusts a ``user_id`` in a request body. The authenticated
user comes from an :class:`IdentityProvider`:

* ``UnconfiguredIdentityProvider`` (default) - refuses every user-scoped call
  with 503 ``auth_not_configured``. Deploying without configuring auth fails safe.
* ``DevHeaderIdentityProvider`` - **LOCAL DEVELOPMENT ONLY.** Trusts the
  ``X-Dev-User-Id`` header and, by default, only from loopback clients.
* ``StaticTokenIdentityProvider`` - prototype bearer tokens mapped to user ids
  from a JSON file. Replace with the host's real auth (e.g. OIDC/JWT) in production.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from pathlib import Path
from typing import Protocol

from fastapi import Request

from ..config import Settings
from ..contract import ID_PATTERN
from ..service import ServiceError

logger = logging.getLogger("tactidose_wellbeing.auth")

DEV_USER_HEADER = "X-Dev-User-Id"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ID_RE = re.compile(ID_PATTERN)


class IdentityProvider(Protocol):
    mode: str
    configured: bool

    def authenticate(self, request: Request) -> str:
        """Return the authenticated user id or raise :class:`ServiceError`."""


class UnconfiguredIdentityProvider:
    mode = "unconfigured"
    configured = False

    def authenticate(self, request: Request) -> str:
        raise ServiceError(
            "auth_not_configured",
            "Authentication is not configured for this service. Refusing user-scoped requests.",
            503,
        )


class DevHeaderIdentityProvider:
    """LOCAL DEVELOPMENT ONLY - anyone who can reach the server can claim any user."""

    mode = "dev"
    configured = True

    def __init__(self, require_loopback: bool = True) -> None:
        self.require_loopback = require_loopback
        logger.warning(
            "DEV identity mode enabled: the %s header is trusted. Never use this in production.",
            DEV_USER_HEADER,
        )

    def authenticate(self, request: Request) -> str:
        if self.require_loopback:
            client = request.client.host if request.client else None
            if client not in LOOPBACK_HOSTS:
                raise ServiceError("unauthenticated", "Dev identity is only accepted from localhost.", 401)
        user_id = request.headers.get(DEV_USER_HEADER)
        if not user_id or not _ID_RE.match(user_id):
            raise ServiceError("unauthenticated", f"Missing or invalid {DEV_USER_HEADER} header.", 401)
        return user_id


class StaticTokenIdentityProvider:
    mode = "static_tokens"
    configured = True

    def __init__(self, tokens: dict[str, str]) -> None:
        if not tokens:
            raise ValueError("static_tokens auth mode needs at least one token")
        self._tokens = dict(tokens)

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticTokenIdentityProvider":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def authenticate(self, request: Request) -> str:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ServiceError("unauthenticated", "Missing bearer token.", 401)
        for known, user_id in self._tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                return user_id
        raise ServiceError("unauthenticated", "Invalid bearer token.", 401)


def identity_provider_from_settings(settings: Settings) -> IdentityProvider:
    if settings.auth_mode == "dev":
        return DevHeaderIdentityProvider(require_loopback=True)
    if settings.auth_mode == "static_tokens":
        if not settings.api_tokens_file:
            raise ValueError("static_tokens auth mode requires TACTIDOSE_WELLBEING_API_TOKENS_FILE")
        return StaticTokenIdentityProvider.from_file(settings.api_tokens_file)
    return UnconfiguredIdentityProvider()
