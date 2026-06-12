"""Security & resilience layer — Phase 8.

Pure functions / small classes that the FastAPI server wires in:
  - `require_master_key`: dependency that checks `Authorization: Bearer <key>`.
  - `TokenBucket`: in-memory rate limiter per client IP / API key.
  - `is_transient_error`: classifies LiteLLM exceptions for retry decision.
  - `with_retry`: synchronous exponential backoff wrapper for the LLM call.

Kept separate from the server so the server module stays thin and these
utilities are independently testable.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


# --- Auth ---

def get_master_key() -> str:
    return os.environ.get("ROUTER_MASTER_KEY", "")


def require_master_key(authorization: str | None) -> None:
    """Validate `Authorization: Bearer <key>`.

    If ROUTER_MASTER_KEY is unset, auth is DISABLED (dev mode).
    If set, the header MUST match (constant-time comparison is not strictly
    needed here since the key is in env, but it's good hygiene).
    """
    expected = get_master_key()
    if not expected:
        return  # auth disabled
    if not authorization or not authorization.startswith("Bearer "):
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if provided != expected:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


# --- Rate limiting ---

@dataclass
class TokenBucket:
    """Simple token bucket per key. Thread-safe."""
    capacity: float
    refill_rate: float  # tokens per second

    def __post_init__(self) -> None:
        # Initialize with `capacity` so the FIRST allow() has a full budget
        # to work with, even if no time has passed since construction.
        # This avoids subtle timing bugs in tests.
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _ensure(self, key: str) -> None:
        """Lazily initialize a fresh key with full capacity at NOW."""
        if key not in self._tokens:
            self._tokens[key] = self.capacity
            self._last[key] = time.monotonic()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        with self._lock:
            self._ensure(key)
            now = time.monotonic()
            elapsed = now - self._last[key]
            self._tokens[key] = min(
                self.capacity,
                self._tokens[key] + elapsed * self.refill_rate,
            )
            if self._tokens[key] >= cost:
                self._tokens[key] -= cost
                self._last[key] = now
                return True
            self._last[key] = now
            return False

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._tokens.clear()
                self._last.clear()
            else:
                self._tokens.pop(key, None)
                self._last.pop(key, None)


# --- Error classification ---

# Structured exception classification (iter 15 hardening). Previously the
# router used a string-substring match on the error message, which had
# false positives like "Connection error from billing" (intentional 4xx)
# triggering a retry. We now use the actual litellm exception classes
# (with safe fallbacks for tests / minimal installs).

try:
    import litellm.exceptions as _litellm_exceptions  # noqa: F401
    _RETRYABLE_EXC_TYPES: tuple[type, ...] = (
        _litellm_exceptions.APIConnectionError,
        _litellm_exceptions.Timeout,
        _litellm_exceptions.ServiceUnavailableError,
        _litellm_exceptions.InternalServerError,
        _litellm_exceptions.RateLimitError,
    )
    _API_ERROR_TYPE = _litellm_exceptions.APIError
    _LITELLM_EXCEPTIONS_AVAILABLE = True
except ImportError:  # pragma: no cover — litellm is optional in tests
    _RETRYABLE_EXC_TYPES = ()
    _API_ERROR_TYPE = None
    _LITELLM_EXCEPTIONS_AVAILABLE = False

# HTTP status codes that are safe to retry.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})


def is_transient_error(exc: Exception) -> bool:
    """True if the error is worth retrying (network, rate limit, 5xx).

    iter 15: classify via litellm exception types when available, else
    fall back to a curated list of substrings (kept narrow to avoid the
    previous false-positives like "Connection error from billing").
    """
    # 1. Structured: litellm exception types (preferred path).
    if _LITELLM_EXCEPTIONS_AVAILABLE and _RETRYABLE_EXC_TYPES:
        if isinstance(exc, _RETRYABLE_EXC_TYPES):
            return True
        if _API_ERROR_TYPE is not None and isinstance(exc, _API_ERROR_TYPE):
            # Some APIError carry .status_code; only retry on 5xx / 408 / 429.
            status = getattr(exc, "status_code", None)
            if status in _RETRYABLE_STATUSES:
                return True
        # httpx-style errors often surface as HTTPError; check by type name
        # to avoid importing httpx at module top.
        cls_name = type(exc).__name__
        if cls_name in {"ConnectError", "ReadTimeout", "WriteTimeout", "PoolTimeout"}:
            return True

    # 2. Fallback: narrow substring match (replaces the old broad match).
    msg = (str(exc) or "").lower()
    return any(
        kw in msg
        for kw in (
            "rate limit", "rate-limit", "ratelimit",
            " 429", "429 ", "http 429",
            " 500", "500 ", "http 500",
            " 502", "502 ", "http 502",
            " 503", "503 ", "http 503",
            " 504", "504 ", "http 504",
            "service unavailable", "temporarily unavailable",
            "timeout", "timed out", "connection reset", "connection aborted",
        )
    )


# --- Retry with exponential backoff ---

def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn` with exponential backoff on transient errors.

    Non-transient errors (auth, validation, etc.) raise immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if not is_transient_error(e) or attempt == max_attempts:
                raise
            last_exc = e
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            sleep(delay)
    # Should be unreachable, but type checkers want this:
    assert last_exc is not None
    raise last_exc
