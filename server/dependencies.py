"""FastAPI dependencies for the QuotaMax Router.

iter 15: extracted from server/app.py:build_app() (the "god-object"
refactor). The combined auth + rate-limit dependency is the most
touched code path in the app, so it earns its own module.

The two layers (auth and rate-limit) are intentionally combined into
one dependency so that:
  - unauthenticated requests don't consume a rate-limit token (a
    common DOS vector when the two are separate);
  - the test surface is a single ``auth_and_rate_limit(client_ip)``
    function rather than two stacked ``Depends(...)`` calls.

The dependency is built via ``make_auth_and_rate_limit`` so that the
caller can inject the master key + rate limiter + metrics dict (all
of which are normally captured at app-build time).
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import HTTPException, Request

from core.security import RateLimiter


def make_auth_and_rate_limit(
    *,
    master_key: str,
    rate_limiter: RateLimiter,
    on_rate_limited: Callable[[], None] | None = None,
) -> Callable[[Request], str]:
    """Build a FastAPI dependency that authenticates + rate-limits.

    Args:
        master_key: The expected ``Authorization: Bearer *** value. An
            empty string disables auth (the iter 15 hardening in
            ``build_app()`` already refuses to start the server in that
            state unless the operator opts in).
        rate_limiter: Any ``RateLimiter`` implementation used to enforce
            per-IP request rate.
        on_rate_limited: Optional callback invoked when a request is
            rate-limited (e.g. to bump a Prometheus counter).

    Returns:
        A FastAPI dependency that returns the client IP on success.
    """

    def auth_and_rate_limit(request: Request) -> str:
        # 1. Auth (constant-time compare)
        if master_key:
            auth = request.headers.get("authorization")
            if not auth or not auth.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
            provided = auth.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(provided.encode("utf-8"), master_key.encode("utf-8")):
                raise HTTPException(status_code=401, detail="Invalid API key")
        # 2. Rate limit (per IP, falls back to "unknown")
        client_ip = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client_ip, cost=1.0):
            if on_rate_limited is not None:
                on_rate_limited()
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return client_ip

    return auth_and_rate_limit
