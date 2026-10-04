"""Exceptions raised by the auth package.

They extend :class:`tactidose.medication.errors.DomainError`, so an HTTP layer that maps
``DomainError.status_code`` handles them without extra code::

    AuthError -> 401    TooManyAttempts -> 429    PermissionDenied -> 403

Input and state problems reuse the domain classes: ``ValidationError`` (422),
``NotFoundError`` (404) and ``ConflictError`` (409), re-exported here for convenience.
Every ``message`` is a short, human-readable sentence that is safe to show (and speak)
to the user; none of them reveal whether an email address has an account.
"""

from __future__ import annotations

import logging

from tactidose.medication.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)

log = logging.getLogger(__name__)

__all__ = [
    "AuthError",
    "ConflictError",
    "DomainError",
    "NotFoundError",
    "PermissionDenied",
    "TooManyAttempts",
    "ValidationError",
]


class AuthError(DomainError):
    """Not signed in, or the credentials are wrong (HTTP 401)."""

    status_code = 401


class TooManyAttempts(AuthError):
    """Too many failed attempts in a short time; retry after ``retry_after_s`` (HTTP 429)."""

    status_code = 429

    def __init__(self, message: str, *, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = max(1, int(retry_after_s))


class PermissionDenied(DomainError):
    """Signed in, but this account may not do that (HTTP 403)."""

    status_code = 403
