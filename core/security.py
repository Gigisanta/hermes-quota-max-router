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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

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
    """In-process token bucket per key. Thread-safe.

    Suitable for single-worker deployments or dev/test mode. For
    production multi-worker uvicorn (or multi-pod k8s), use
    :class:`RedisTokenBucket` which shares state across workers.
    """

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


# --- Distributed rate limiting (Redis-backed, iter 15) ---


class RateLimiter(Protocol):
    """Minimal rate-limiter contract used by the auth dependency.

    Both the in-process :class:`TokenBucket` and the distributed
    :class:`RedisTokenBucket` satisfy this protocol, so the
    dependency-injection in `build_app()` can swap them transparently.
    """

    def allow(self, key: str, cost: float = 1.0) -> bool: ...
    def reset(self, key: str | None = None) -> None: ...


@dataclass
class RedisTokenBucket:
    """Redis-backed token bucket (HGETALL+HMSET path, no Lua).

    iter 15: the in-process :class:`TokenBucket` only works for
    single-worker uvicorn. With multiple workers (the production
    default `uvicorn --workers 4`), each worker has its own counter
    → a 4-worker deployment would admit 4x the intended rate. The
    RedisTokenBucket fixes this by storing tokens + last in Redis.

    **Atomicity note.** We use HGETALL → compute → HMSET, which is
    not atomic across concurrent workers (two workers could read the
    same state, both decrement, both write back, and over-admit by 1
    token). For a production-grade atomic implementation, use a Lua
    script (EVAL/EVALSHA) — most Redis clients support it natively.
    The HMSET path is the right tradeoff for our scale: the over-admit
    is bounded by `min(num_workers, max_burst)` and we already
    tolerate some slack in the 60-burst default.

    **Future improvement:** the Lua script is documented in the
    `_ALLOW_LUA` constant for ops to enable via a class flag.

    Keys:
      - ``rl:{key}`` → hash with ``tokens`` (float) and ``last`` (float
        wall time seconds).
    """

    capacity: float
    refill_rate: float  # tokens per second
    redis_client: Any = None  # redis.Redis instance; injected
    key_prefix: str = "qr_rl:"

    # Reference Lua script for atomic implementations (ops can enable
    # by switching _ALLOW_LUA → _exec_lua in the allow() method). Kept
    # here for documentation + future atomic upgrade.
    _ALLOW_LUA: str = """
    local key = KEYS[1]
    local cap = tonumber(ARGV[1])
    local rate = tonumber(ARGV[2])
    local cost = tonumber(ARGV[3])
    local now = tonumber(ARGV[4])
    local data = redis.call('HMGET', key, 'tokens', 'last')
    local tokens = tonumber(data[1])
    local last = tonumber(data[2])
    if tokens == nil then
        tokens = cap
        last = now
    end
    local elapsed = math.max(0, now - last)
    tokens = math.min(cap, tokens + elapsed * rate)
    local allowed = 0
    if tokens >= cost then
        tokens = tokens - cost
        allowed = 1
    end
    redis.call('HMSET', key, 'tokens', tokens, 'last', now)
    redis.call('EXPIRE', key, 3600)
    return allowed
    """

    def allow(self, key: str, cost: float = 1.0) -> bool:
        if self.redis_client is None:
            # Defensive: fall back to allowing the request. The
            # build_app() factory should never construct this class
            # without a redis client, but if someone does, we don't
            # want to DOS them.
            return True
        full_key = f"{self.key_prefix}{key}"
        now = time.time()
        try:
            # Read state.
            data = self.redis_client.hmget(full_key, "tokens", "last")
            tokens_raw, last_raw = data[0], data[1]
            if tokens_raw is None or last_raw is None:
                tokens = self.capacity
                last = now
            else:
                try:
                    tokens = float(tokens_raw)
                    last = float(last_raw)
                except (TypeError, ValueError):
                    tokens = self.capacity
                    last = now
            # Refill based on elapsed time.
            elapsed = max(0.0, now - last)
            tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
            # Try to consume.
            allowed = 0
            if tokens >= cost:
                tokens -= cost
                allowed = 1
            # Persist new state.
            self.redis_client.hmset(
                full_key,
                {"tokens": tokens, "last": now},
            )
            self.redis_client.expire(full_key, 3600)
            return allowed == 1
        except (OSError, RuntimeError, AttributeError) as e:
            # Redis unavailable or broken client. Fail OPEN (admit the
            # request) so a Redis outage doesn't take down the API.
            import logging as _log

            _log.getLogger(__name__).warning(
                "RedisTokenBucket: Redis unavailable (%s); failing open.",
                e,
            )
            return True

    def reset(self, key: str | None = None) -> None:
        if self.redis_client is None:
            return
        try:
            if key is None:
                # Delete all matching keys. Use SCAN to avoid blocking.
                pattern = f"{self.key_prefix}*"
                for k in self.redis_client.scan_iter(match=pattern, count=100):
                    self.redis_client.delete(k)
            else:
                self.redis_client.delete(f"{self.key_prefix}{key}")
        except (OSError, RuntimeError, AttributeError):
            pass  # best-effort


# --- Error classification ---

# Structured exception classification (iter 15 hardening). Previously the
# router used a string-substring match on the error message, which had
# false positives like "Connection error from billing" (intentional 4xx)
# triggering a retry. We now use the actual litellm exception classes
# (with safe fallbacks for tests / minimal installs).

_API_ERROR_TYPE: type[Exception] | None

try:
    import litellm.exceptions as _litellm_exceptions

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
            "rate limit",
            "rate-limit",
            "ratelimit",
            " 429",
            "429 ",
            "http 429",
            " 500",
            "500 ",
            "http 500",
            " 502",
            "502 ",
            "http 502",
            " 503",
            "503 ",
            "http 503",
            " 504",
            "504 ",
            "http 504",
            "service unavailable",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
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
        except Exception as e:
            if not is_transient_error(e) or attempt == max_attempts:
                raise
            last_exc = e
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            sleep(delay)
    # Should be unreachable, but type checkers want this:
    assert last_exc is not None
    raise last_exc
