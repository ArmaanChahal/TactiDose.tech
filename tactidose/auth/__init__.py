"""Accounts, login sessions and access control (ARCHITECTURE v2 §9).

* :mod:`tactidose.auth.passwords` - scrypt password hashing (stdlib only).
* :mod:`tactidose.auth.service` - :class:`AuthService` (implements ``AuthServiceAPI``).
* :mod:`tactidose.auth.errors` - ``AuthError`` (401), ``TooManyAttempts`` (429), ``PermissionDenied`` (403).
* ``tactidose.auth.deps`` - FastAPI dependencies (owned by the API layer).
"""

from __future__ import annotations

from tactidose.auth.errors import AuthError, PermissionDenied, TooManyAttempts
from tactidose.auth.passwords import hash_password, needs_rehash, verify_password
from tactidose.auth.service import AuthService

__all__ = [
    "AuthError",
    "AuthService",
    "PermissionDenied",
    "TooManyAttempts",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
