"""Password hashing with the standard library's scrypt (ARCHITECTURE v2 §9).

Stored format::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

Standard base64, a random 16-byte salt and a 32-byte derived key. The cost parameters
travel with every hash, so verification keeps working after the defaults change and
:func:`needs_rehash` tells the login code when to upgrade a stored hash.

* Passwords are NFKC-normalised before hashing, so the same password typed on another
  keyboard or platform (composed vs decomposed accents) still verifies.
* Policy: at least :data:`MIN_PASSWORD_LENGTH` characters; at most
  :data:`MAX_PASSWORD_LENGTH` (bounds the work an attacker can request). No composition rules.
* :func:`verify_password` never raises and compares in constant time. Parameters read
  back from storage are bounded, so a corrupt row cannot exhaust memory or CPU.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import threading
import unicodedata
from typing import NamedTuple

log = logging.getLogger(__name__)

__all__ = [
    "KEY_BYTES",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "SALT_BYTES",
    "SCHEME",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "dummy_verify",
    "hash_password",
    "needs_rehash",
    "password_problem",
    "verify_password",
]

SCHEME = "scrypt"
#: Cost parameters for new hashes (read at call time, so tests may lower them).
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024

#: OpenSSL's default limit (32 MiB) is too tight for n=2**15; 128*r*n is the real need.
_MAXMEM = 64 * 1024 * 1024
_MAX_N = 2 ** 16
_MAX_R = 16
_MAX_P = 4
_MIN_SALT = 8
_MAX_FIELD = 512

_dummy_lock = threading.Lock()
_dummy_hash: str | None = None


class _Parsed(NamedTuple):
    n: int
    r: int
    p: int
    salt: bytes
    key: bytes


def password_problem(password: object) -> str | None:
    """Human-readable reason why ``password`` is not acceptable, or ``None`` if it is."""
    if not isinstance(password, str) or not password:
        return "Please enter a password."
    length = len(_normalise(password))
    if length < MIN_PASSWORD_LENGTH:
        return f"Your password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if length > MAX_PASSWORD_LENGTH:
        return f"Your password must be at most {MAX_PASSWORD_LENGTH} characters long."
    try:
        _encode(password)
    except UnicodeError:
        return "Your password contains characters that cannot be used."
    return None


def hash_password(password: str, *, n: int | None = None, r: int | None = None, p: int | None = None) -> str:
    """Hash ``password`` for storage. Raises ``ValueError`` if it violates the policy."""
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    n = SCRYPT_N if n is None else n
    r = SCRYPT_R if r is None else r
    p = SCRYPT_P if p is None else p
    if not _params_ok(n, r, p):
        raise ValueError("invalid scrypt parameters")
    salt = os.urandom(SALT_BYTES)
    key = _derive(password, salt, n, r, p, KEY_BYTES)
    return "$".join((SCHEME, str(n), str(r), str(p), _b64(salt), _b64(key)))


def verify_password(password: object, encoded: object) -> bool:
    """True if ``password`` matches the stored ``encoded`` hash. Never raises."""
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    parsed = _parse(encoded)
    if parsed is None or len(password) > MAX_PASSWORD_LENGTH * 4:
        return False
    try:
        candidate = _derive(password, parsed.salt, parsed.n, parsed.r, parsed.p, len(parsed.key))
    except (ValueError, MemoryError, UnicodeError):
        return False
    return hmac.compare_digest(candidate, parsed.key)


def needs_rehash(encoded: object) -> bool:
    """True if ``encoded`` is malformed or was made with other parameters than the current ones."""
    if not isinstance(encoded, str):
        return True
    parsed = _parse(encoded)
    if parsed is None:
        return True
    return (
        (parsed.n, parsed.r, parsed.p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
        or len(parsed.salt) < SALT_BYTES
        or len(parsed.key) != KEY_BYTES
    )


def dummy_verify(password: object) -> bool:
    """Spend the same time as a real check (unknown accounts); always returns False."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None or needs_rehash(_dummy_hash):
            _dummy_hash = hash_password("dummy-password-for-timing")
        dummy = _dummy_hash
    verify_password(password if isinstance(password, str) else "", dummy)
    return False


# --------------------------------------------------------------------------- internals


def _normalise(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def _encode(password: str) -> bytes:
    return _normalise(password).encode("utf-8")


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(_encode(password), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=dklen)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _params_ok(n: int, r: int, p: int) -> bool:
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (n, r, p)):
        return False
    if n < 2 or n > _MAX_N or n & (n - 1):
        return False
    if not (1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        return False
    return 128 * r * n * p <= _MAXMEM


def _parse(encoded: str) -> _Parsed | None:
    if len(encoded) > _MAX_FIELD:
        return None
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    if not all(x.isascii() and x.isdigit() and len(x) <= 8 for x in parts[1:4]):
        return None
    n, r, p = (int(x) for x in parts[1:4])
    try:
        salt = base64.b64decode(parts[4], validate=True)
        key = base64.b64decode(parts[5], validate=True)
    except (ValueError, binascii.Error):
        return None
    if not _params_ok(n, r, p) or len(salt) < _MIN_SALT or not 16 <= len(key) <= 64:
        return None
    return _Parsed(n, r, p, salt, key)
