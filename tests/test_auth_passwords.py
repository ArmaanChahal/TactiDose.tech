"""auth/passwords.py: scrypt format, verification, rehash detection and the password policy."""

from __future__ import annotations

import base64
import hmac
import unicodedata

import pytest

from tactidose.auth import passwords as pw


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests use a cheaper cost; the default parameters are checked explicitly below."""
    monkeypatch.setattr(pw, "SCRYPT_N", 2 ** 10)


def test_default_parameters_match_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pw, "SCRYPT_N", 2 ** 14)
    encoded = pw.hash_password("correct horse")
    scheme, n, r, p, salt, key = encoded.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert len(base64.b64decode(salt, validate=True)) == 16
    assert len(base64.b64decode(key, validate=True)) == 32
    assert pw.verify_password("correct horse", encoded)
    assert not pw.needs_rehash(encoded)
    assert (pw.SCRYPT_R, pw.SCRYPT_P, pw.SALT_BYTES, pw.MIN_PASSWORD_LENGTH) == (8, 1, 16, 8)


def test_hashes_are_salted_and_verify() -> None:
    a = pw.hash_password("correct horse")
    b = pw.hash_password("correct horse")
    assert a != b
    assert pw.verify_password("correct horse", a) and pw.verify_password("correct horse", b)
    assert not pw.verify_password("correct horsE", a)
    assert not pw.verify_password("correct horse ", a)
    assert "correct horse" not in a


def test_unicode_is_normalised_before_hashing() -> None:
    composed = unicodedata.normalize("NFC", "café-paris")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert pw.verify_password(decomposed, pw.hash_password(composed))


@pytest.mark.parametrize(
    "password,problem",
    [
        (None, "Please enter a password."),
        ("", "Please enter a password."),
        (12345678, "Please enter a password."),
        ("short", "at least 8 characters"),
        ("1234567", "at least 8 characters"),
        ("x" * (pw.MAX_PASSWORD_LENGTH + 1), "at most"),
        ("valid-start\ud800", "cannot be used"),
    ],
)
def test_password_policy(password: object, problem: str) -> None:
    assert problem in (pw.password_problem(password) or "")
    with pytest.raises(ValueError):
        pw.hash_password(password)  # type: ignore[arg-type]


def test_minimum_length_is_inclusive() -> None:
    assert pw.password_problem("12345678") is None
    assert pw.verify_password("12345678", pw.hash_password("12345678"))


def test_explicit_parameters_and_bounds() -> None:
    encoded = pw.hash_password("correct horse", n=2 ** 11, r=4, p=1)
    assert encoded.split("$")[1:4] == ["2048", "4", "1"]
    assert pw.verify_password("correct horse", encoded)
    for bad in ({"n": 1000}, {"n": 2 ** 20}, {"r": 0}, {"p": 9}):
        with pytest.raises(ValueError):
            pw.hash_password("correct horse", **bad)


def _swap(encoded: str, index: int, value: str) -> str:
    parts = encoded.split("$")
    parts[index] = value
    return "$".join(parts)


def test_verify_never_raises_on_bad_input() -> None:
    good = pw.hash_password("correct horse")
    salt_b64 = good.split("$")[4]
    bad_values = [
        None, 123, b"bytes", "", "scrypt", "scrypt$$$$$", good + "$extra",
        _swap(good, 0, "bcrypt"),
        _swap(good, 1, "abc"),
        _swap(good, 1, "1000"),                    # not a power of two
        _swap(good, 1, str(2 ** 30)),              # absurd memory request
        _swap(good, 1, "١٠٢٤"),  # non-ASCII digits
        _swap(good, 1, "+1024"),
        _swap(good, 2, "0"),
        _swap(good, 3, "99"),
        _swap(good, 4, "!!!not base64!!!"),
        _swap(good, 4, base64.b64encode(b"abc").decode()),   # salt too short
        _swap(good, 5, base64.b64encode(b"short").decode()),  # key too short
        _swap(good, 5, salt_b64 + "é"),
        "x" * 10_000,
    ]
    for encoded in bad_values:
        assert pw.verify_password("correct horse", encoded) is False, encoded
    assert pw.verify_password(None, good) is False
    assert pw.verify_password(b"correct horse", good) is False
    assert pw.verify_password("\ud800 lone surrogate", good) is False


def test_needs_rehash() -> None:
    current = pw.hash_password("correct horse")
    assert not pw.needs_rehash(current)
    assert pw.needs_rehash(pw.hash_password("correct horse", n=2 ** 11))
    assert pw.needs_rehash(pw.hash_password("correct horse", r=4))
    assert pw.needs_rehash(None) and pw.needs_rehash("") and pw.needs_rehash("plain-text")
    short_salt = _swap(current, 4, base64.b64encode(b"8bytes!!").decode())
    assert pw.verify_password("correct horse", short_salt) is False   # different salt, so no match
    assert pw.needs_rehash(short_salt)


def test_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(pw.hmac, "compare_digest", spy)
    encoded = pw.hash_password("correct horse")
    assert pw.verify_password("correct horse", encoded)
    assert not pw.verify_password("wrong horse!", encoded)
    assert len(calls) == 2 and all(isinstance(a, bytes) and len(a) == 32 for a, _ in calls)


def test_dummy_verify_is_always_false() -> None:
    assert pw.dummy_verify("correct horse") is False
    assert pw.dummy_verify(None) is False
