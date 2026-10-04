"""Domain exceptions for the medication package.

Caregiver/admin operations (catalog, compartments, schedules, onboarding, dose
review) raise these for invalid input or conflicting state. The HTTP layer maps
them to status codes via :attr:`DomainError.status_code`::

    ValidationError -> 422    NotFoundError -> 404    ConflictError -> 409

The kiosk/assistant path (``DoseServiceAPI``) never raises them for expected
failures: it reports outcomes through status enums instead (fail closed).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

__all__ = ["DomainError", "ValidationError", "NotFoundError", "ConflictError"]


class DomainError(Exception):
    """Base class. ``message`` is human readable and safe to show in the UI."""

    status_code: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class ValidationError(DomainError):
    """Input is malformed or violates a domain rule (HTTP 422)."""

    status_code = 422


class NotFoundError(DomainError):
    """The referenced record does not exist (HTTP 404)."""

    status_code = 404


class ConflictError(DomainError):
    """The record exists but its current state does not allow the operation (HTTP 409)."""

    status_code = 409
