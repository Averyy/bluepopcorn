"""HTTP middleware for authentication."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request


def hash_api_key(api_key: str) -> str:
    """Hash API key for storage (avoid storing raw key in memory)."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def timing_safe_compare(a: str | bytes, b: str | bytes) -> bool:
    """Timing-safe comparison to prevent timing attacks."""
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


# Per-IP auth failure tracking: {ip: (failure_count, last_failure_time)}
_auth_failures: dict[str, tuple[int, float]] = {}
_AUTH_MAX_FAILURES = 5
_AUTH_COOLDOWN = 60  # seconds


def verify_bearer_auth(
    request: Request,
    api_key_hashes: set[str],
) -> str | None:
    """Verify Bearer token authentication.

    Returns error message if auth fails, None if successful.
    Applies per-IP rate limiting after repeated failures.
    """
    client_ip = get_client_ip(request)

    # Check rate limit
    if client_ip in _auth_failures:
        count, last_time = _auth_failures[client_ip]
        if count >= _AUTH_MAX_FAILURES and time.time() - last_time < _AUTH_COOLDOWN:
            return "Too many auth failures — try again later"
        if time.time() - last_time >= _AUTH_COOLDOWN:
            del _auth_failures[client_ip]

    auth_header = request.headers.get("authorization")
    if not auth_header:
        _record_auth_failure(client_ip)
        return "Missing Authorization header"

    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        _record_auth_failure(client_ip)
        return "Invalid Authorization header format"

    token = parts[1]
    token_hash = hash_api_key(token)
    # Evaluate all keys to prevent timing leak via short-circuit
    results = [timing_safe_compare(token_hash, h) for h in api_key_hashes]
    if any(results):
        # Clear failures on success
        _auth_failures.pop(client_ip, None)
        return None

    _record_auth_failure(client_ip)
    return "Invalid Bearer token"


def _record_auth_failure(ip: str) -> None:
    """Record an auth failure for rate limiting."""
    count, _ = _auth_failures.get(ip, (0, 0.0))
    _auth_failures[ip] = (count + 1, time.time())


def get_client_ip(request: Request) -> str:
    """Get client IP from request, sanitized for safe logging."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        # Sanitize: remove control chars to prevent log injection
        return ip.translate(str.maketrans("", "", "\r\n\t\x00"))
    return request.client.host if request.client else "unknown"
