"""HTTP middlewares for the QuotaMax Router FastAPI app.

iter 15: extracted from server/app.py:build_app() (the "god-object"
refactor). Each middleware is a small factory so that the app factory
stays thin and the headers/policies are easy to test in isolation.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

# Headers applied to EVERY response. Order is irrelevant — `setdefault`
# means an endpoint can override (useful for file downloads that need
# `Content-Disposition`).
_DEFAULT_SECURITY_HEADERS: dict[str, str] = {
    # Phase 8 baseline
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # iter 15: HSTS, CSP, Permissions-Policy.
    # HSTS only makes sense behind TLS, but we set it anyway — proxies
    # can strip or override. CSP is strict because the JSON API is
    # never rendered as HTML; we explicitly deny all sources.
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def make_security_headers_middleware(app: ASGIApp) -> BaseHTTPMiddleware:
    """Return a Starlette middleware that adds the standard security headers.

    The middleware is a thin factory around the standard FastAPI
    ``@app.middleware("http")`` decorator, but exposed as a class so
    that unit tests can instantiate it directly without spinning up the
    full app.
    """

    class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            response: Response = await call_next(request)
            for header, value in _DEFAULT_SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            return response

    return _SecurityHeadersMiddleware(app)
