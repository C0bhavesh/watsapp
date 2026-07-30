"""Admin auth primitives: signed expiring session token + constant-time password check."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime


def _sign(secret: str, payload: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode()


def issue_token(secret: str, now: datetime, ttl_hours: int = 12) -> str:
    """Token format: ``<unix_exp>.<base64url_hmac_sha256>``; *now* injected for tests."""
    if not secret:
        raise ValueError("signing secret must not be empty")
    ts = int(now.replace(tzinfo=now.tzinfo or UTC).timestamp())
    exp = ts + ttl_hours * 3600
    payload = str(exp)
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(secret: str, token: str, now: datetime) -> bool:
    """False (never raises) on empty secret, malformed token, bad signature, or expiry."""
    if not secret:
        return False
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return False
    try:
        exp = int(payload)
    except ValueError:
        return False
    now_ts = int(now.replace(tzinfo=now.tzinfo or UTC).timestamp())
    return now_ts < exp


def check_password(supplied: str, expected: str) -> bool:
    """Constant-time compare; empty *expected* never grants access (fail closed)."""
    if not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())
