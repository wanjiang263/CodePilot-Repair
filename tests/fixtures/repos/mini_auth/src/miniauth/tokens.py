"""令牌签发与校验。"""

from __future__ import annotations

import hashlib
import hmac
import time

SECRET = b"not-a-real-secret"


def _sign(payload: bytes, *, key: bytes = SECRET) -> str:
    """给负载签名。"""
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def issue_token(user: str, ttl: int = 3600) -> str:
    """签发一个令牌。"""
    expiry = int(time.time()) + ttl
    body = f"{user}:{expiry}".encode()
    return f"{body.decode()}:{_sign(body)}"


def verify_token(token: str, *, key: bytes = SECRET) -> str | None:
    """校验令牌，失败返回 None。"""
    parts = token.split(":")
    if len(parts) != 3:
        return None
    user, expiry, signature = parts
    body = f"{user}:{expiry}".encode()
    if not hmac.compare_digest(_sign(body, key=key), signature):
        return None
    return user if int(expiry) > time.time() else None
