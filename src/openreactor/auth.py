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


def verify_password(password: str, stored: str) -> bool:
    """True if ``password`` matches ``stored``. A malformed hash never
    matches."""
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        n_, r_, p_ = int(n), int(r), int(p)
        # Costs this module would never write: refuse rather than attempt.
        if scheme != "scrypt" or not (2 <= n_ <= 2**20 and 1 <= r_ <= 32 and 1 <= p_ <= 16):
            return False
        expected = base64.b64decode(key, validate=True)
        actual = _scrypt(password, base64.b64decode(salt, validate=True), n_, r_, p_)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def looks_like_hash(stored: str) -> bool:
    parts = stored.split("$")
    return len(parts) == 6 and parts[0] == "scrypt" and all(parts[1:4]) and parts[1].isdigit()
