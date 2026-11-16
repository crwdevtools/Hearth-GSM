"""Password hashing, session tokens and login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
SESSION_COOKIE = "hearth_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                                p=int(p), maxmem=64 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# A precomputed hash so unknown usernames cost the same time as wrong passwords.
DUMMY_HASH = hash_password(secrets.token_hex(8))


def issue_session(secret: str, user_id: int, version: int, hours: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "ver": version, "iat": now, "exp": now + timedelta(hours=hours)}
    return jwt.encode(payload, secret, algorithm="HS256")


def read_session(secret: str, token: str) -> tuple[int, int] | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
        return int(payload["sub"]), int(payload.get("ver", 0))
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


def new_node_token() -> str:
    return secrets.token_urlsafe(36)


class LoginThrottle:
    """Allow `limit` failed attempts per key per `window` seconds."""

    def __init__(self, limit: int = 8, window: float = 300.0) -> None:
        self.limit, self.window = limit, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> float:
        """Seconds until the key may try again; 0 when allowed."""
        now = time.monotonic()
        with self._lock:
            q = self._trim(key, now)
            if len(q) < self.limit:
                return 0.0
            return max(self.window - (now - q[0]), 1.0)

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


# ----- two-factor (TOTP, RFC 6238) ------------------------------------------------------------

import struct  # noqa: E402


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // 30))


def match_totp(secret: str, code: str, at: float | None = None) -> int | None:
    """The 30-second step a code belongs to (current, or one either side for clock drift)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    step = int((at if at is not None else time.time()) // 30)
    for d in (-1, 0, 1):
        if hmac.compare_digest(_hotp(secret, step + d), code):
            return step + d
    return None


def verify_totp(secret: str, code: str, at: float | None = None) -> bool:
    return match_totp(secret, code, at) is not None


def totp_uri(secret: str, username: str, issuer: str = "Hearth") -> str:
    from urllib.parse import quote

    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}"


# ----- API tokens -----------------------------------------------------------------------------

API_TOKEN_PREFIX = "hearth_"


def new_api_token() -> tuple[str, str, str]:
    """Returns (full token shown once, lookup prefix, hash to store)."""
    prefix = secrets.token_hex(4)
    token = f"{API_TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    return token, prefix, hashlib.sha256(token.encode()).hexdigest()


def api_token_prefix(token: str) -> str | None:
    if not token.startswith(API_TOKEN_PREFIX):
        return None
    parts = token[len(API_TOKEN_PREFIX):].split("_", 1)
    return parts[0] if len(parts) == 2 else None


def api_token_matches(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), stored_hash)
