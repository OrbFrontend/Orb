"""Hash the access password and check what browsers send against it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from ...database.models import AccessPasswordRow

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def new_lock(password: str) -> AccessPasswordRow:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return {
        "password_hash": "$".join(("scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt), _b64(digest))),
        "session_key": secrets.token_urlsafe(32),
    }


def password_matches(lock: AccessPasswordRow, password: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = lock["password_hash"].split("$")
        expected = base64.b64decode(digest, validate=True)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt, validate=True), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except ValueError:
        return False
    return scheme == "scrypt" and hmac.compare_digest(actual, expected)


def session_valid(lock: AccessPasswordRow, cookie: str | None) -> bool:
    return cookie is not None and hmac.compare_digest(cookie.encode(), lock["session_key"].encode())


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()
