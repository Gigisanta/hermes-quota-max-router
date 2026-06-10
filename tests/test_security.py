"""Tests for the security & resilience layer (Phase 8)."""
import os
import time

import pytest
from fastapi import HTTPException

from core.security import (
    TokenBucket,
    is_transient_error,
    require_master_key,
    with_retry,
)


# --- Auth ---

def test_require_master_key_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROUTER_MASTER_KEY", raising=False)
    # Should NOT raise even with no auth header
    require_master_key(None)
    require_master_key("Bearer anything")


def test_require_master_key_blocks_without_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTER_MASTER_KEY", "secret-123")
    with pytest.raises(HTTPException) as exc:
        require_master_key(None)
    assert exc.value.status_code == 401


def test_require_master_key_blocks_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTER_MASTER_KEY", "secret-123")
    with pytest.raises(HTTPException) as exc:
        require_master_key("Bearer wrong")
    assert exc.value.status_code == 401


def test_require_master_key_accepts_correct_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTER_MASTER_KEY", "secret-123")
    require_master_key("Bearer secret-123")  # no exception


def test_require_master_key_rejects_malformed_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTER_MASTER_KEY", "secret-123")
    with pytest.raises(HTTPException) as exc:
        require_master_key("Basic secret-123")
    assert exc.value.status_code == 401


# --- Rate limiting ---

def test_token_bucket_allows_up_to_capacity() -> None:
    b = TokenBucket(capacity=5.0, refill_rate=0.0)  # no refill
    for _ in range(5):
        assert b.allow("client-1", cost=1.0) is True
    assert b.allow("client-1", cost=1.0) is False


def test_token_bucket_isolates_clients() -> None:
    b = TokenBucket(capacity=2.0, refill_rate=0.0)
    assert b.allow("a") is True
    assert b.allow("a") is True
    assert b.allow("a") is False
    # b is independent
    assert b.allow("b") is True
    assert b.allow("b") is True
    assert b.allow("b") is False


def test_token_bucket_refills_over_time() -> None:
    b = TokenBucket(capacity=1.0, refill_rate=10.0)  # 10 tokens/sec
    assert b.allow("x") is True
    assert b.allow("x") is False
    time.sleep(0.15)  # 0.15s * 10/s = 1.5 tokens refilled
    assert b.allow("x") is True


def test_token_bucket_resets() -> None:
    b = TokenBucket(capacity=2.0, refill_rate=0.0)
    b.allow("a")
    b.allow("a")
    assert b.allow("a") is False
    b.reset("a")
    assert b.allow("a") is True


# --- Error classification ---

def test_is_transient_error_classifies_correctly() -> None:
    assert is_transient_error(Exception("Connection timeout")) is True
    assert is_transient_error(Exception("429 rate limit exceeded")) is True
    assert is_transient_error(Exception("502 Bad Gateway")) is True
    assert is_transient_error(Exception("Service temporarily unavailable")) is True
    assert is_transient_error(Exception("Authentication failed: invalid API key")) is False
    assert is_transient_error(Exception("Invalid request: model not found")) is False


def test_is_transient_error_handles_empty_message() -> None:
    assert is_transient_error(Exception("")) is False


# --- Retry ---

def test_with_retry_succeeds_first_try() -> None:
    calls = []
    def f() -> int:
        calls.append(1)
        return 42
    assert with_retry(f, max_attempts=3) == 42
    assert len(calls) == 1


def test_with_retry_retries_transient_errors() -> None:
    calls = []
    def f() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise Exception("Connection timeout")
        return "ok"
    # Use a no-op sleep to keep tests fast
    assert with_retry(f, max_attempts=3, sleep=lambda _s: None) == "ok"
    assert len(calls) == 3


def test_with_retry_does_not_retry_non_transient() -> None:
    calls = []
    def f() -> None:
        calls.append(1)
        raise ValueError("auth failed: invalid key")
    with pytest.raises(ValueError):
        with_retry(f, max_attempts=5, sleep=lambda _s: None)
    assert len(calls) == 1


def test_with_retry_exhausts_attempts() -> None:
    calls = []
    def f() -> None:
        calls.append(1)
        raise Exception("timeout")
    with pytest.raises(Exception):
        with_retry(f, max_attempts=3, sleep=lambda _s: None)
    assert len(calls) == 3


def test_with_retry_exponential_backoff_delays() -> None:
    delays: list[float] = []
    def f() -> None:
        raise Exception("network error")
    with pytest.raises(Exception):
        with_retry(f, max_attempts=4, base_delay_s=0.5, sleep=delays.append)
    # delays should be 0.5, 1.0, 2.0 (4 attempts → 3 sleeps)
    assert delays == [0.5, 1.0, 2.0]


def test_with_retry_caps_at_max_delay() -> None:
    delays: list[float] = []
    def f() -> None:
        raise Exception("timeout")
    with pytest.raises(Exception):
        with_retry(f, max_attempts=6, base_delay_s=0.5, max_delay_s=1.0,
                   sleep=delays.append)
    # 0.5, 1.0, 1.0, 1.0, 1.0 — caps after 2nd attempt
    assert all(d <= 1.0 for d in delays)
    assert delays[0] == 0.5
    assert delays[1] == 1.0
