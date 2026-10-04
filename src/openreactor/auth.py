"""The shared password: hashed with stdlib scrypt, checked in constant time.

A hash looks like ``scrypt$<n>$<r>$<p>$<salt>$<key>``, base64 for the last
two, so the cost parameters can change later without breaking old hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

# About 32 MiB and a fraction of a second on a Raspberry Pi 4.
N, R, P = 2**15, 8, 1
KEY_BYTES = 32


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=2 * 128 * r * n
    )


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = _scrypt(password, salt, N, R, P)
    b64 = base64.b64encode
    return f"scrypt${N}${R}${P}${b64(salt).decode()}${b64(key).decode()}"


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    """The cost, salt and key of a hash this module would write, or None.
    Costs outside fixed bounds are refused rather than attempted."""
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        cost = int(n), int(r), int(p)
        decoded = base64.b64decode(salt, validate=True), base64.b64decode(key, validate=True)
    except ValueError:
        return None
    n_, r_, p_ = cost
    power_of_two = n_ >= 2 and n_ & (n_ - 1) == 0
    if scheme != "scrypt" or not (power_of_two and n_ <= 2**20 and 1 <= r_ <= 32 and 1 <= p_ <= 16):
        return None
    if not decoded[0] or len(decoded[1]) != KEY_BYTES:
        return None
    return n_, r_, p_, decoded[0], decoded[1]


def verify_password(password: str, stored: str) -> bool:
    """True if ``password`` matches ``stored``. A malformed hash never
    matches."""
    parsed = _parse(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    return hmac.compare_digest(_scrypt(password, salt, n, r, p), expected)


def looks_like_hash(stored: str) -> bool:
    """True if ``stored`` is a hash verify_password can check."""
    return _parse(stored) is not None
