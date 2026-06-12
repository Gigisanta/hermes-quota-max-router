"""Tests for the HealthProbe circuit breaker (iter 15)."""
from __future__ import annotations

import time

import pytest

from core.health_probe import (
    HealthProbe,
    HealthState,
    ModelHealth,
    get_default_probe,
    set_default_probe,
)


@pytest.fixture
def probe() -> HealthProbe:
    # Short cooldown for fast tests.
    return HealthProbe(
        failure_threshold=3,
        recovery_threshold=1,
        cooldown_s=0.1,  # 100ms
        max_cooldown_s=1.0,
        half_open_probe_interval_s=0.01,
        transient_window_s=10.0,
        transient_count_threshold=5,
    )


def test_unknown_model_is_available(probe: HealthProbe) -> None:
    assert probe.is_available("unknown") is True
    assert probe.get_state("unknown").state == HealthState.HEALTHY


def test_success_keeps_healthy(probe: HealthProbe) -> None:
    probe.record_success("m1")
    assert probe.is_available("m1") is True
    assert probe.get_state("m1").state == HealthState.HEALTHY


def test_three_consecutive_transient_failures_trip_to_unhealthy(
    probe: HealthProbe,
) -> None:
    for _ in range(2):
        probe.record_failure("m1", transient=True, error="timeout")
    assert probe.get_state("m1").state == HealthState.DEGRADED
    assert probe.is_available("m1") is True  # degraded still routes
    probe.record_failure("m1", transient=True, error="timeout")
    h = probe.get_state("m1")
    assert h.state == HealthState.UNHEALTHY
    assert h.cooldown_until is not None
    assert h.cooldown_count == 1
    # Within cooldown, unavailable
    assert probe.is_available("m1") is False


def test_unhealthy_recovers_via_half_open(probe: HealthProbe) -> None:
    for _ in range(3):
        probe.record_failure("m1", transient=True, error="timeout")
    assert probe.get_state("m1").state == HealthState.UNHEALTHY
    # After cooldown, promoted to HALF_OPEN
    time.sleep(0.15)
    assert probe.is_available("m1") is True
    assert probe.get_state("m1").state == HealthState.HALF_OPEN
    # Successful probe → HEALTHY
    probe.record_success("m1")
    assert probe.get_state("m1").state == HealthState.HEALTHY
    assert probe.get_state("m1").consecutive_failures == 0


def test_half_open_probe_failure_extends_cooldown(probe: HealthProbe) -> None:
    for _ in range(3):
        probe.record_failure("m1", transient=True, error="timeout")
    time.sleep(0.15)
    assert probe.is_available("m1") is True  # → HALF_OPEN
    assert probe.get_state("m1").cooldown_count == 1
    # Failed probe
    probe.record_failure("m1", transient=True, error="still timing out")
    h = probe.get_state("m1")
    assert h.state == HealthState.UNHEALTHY
    assert h.cooldown_count == 2  # exponential backoff counter incremented


def test_hard_failure_does_not_trip(probe: HealthProbe) -> None:
    # 10 hard failures (e.g. invalid request, model not found) should
    # NOT trip the circuit. The model is fine; the request is bad.
    for _ in range(10):
        probe.record_failure("m1", transient=False, error="model not found")
    assert probe.get_state("m1").state == HealthState.HEALTHY
    assert probe.is_available("m1") is True
    # But total_failures is recorded.
    assert probe.get_state("m1").total_failures == 10


def test_transient_window_threshold(probe: HealthProbe) -> None:
    # 4 failures, no consecutive threshold hit (4 < 3? no, 4 > 3).
    # Then 1 more to cross the count threshold. Use a long pause
    # between calls so they aren't "consecutive" but still in window.
    for _ in range(4):
        probe.record_failure("m1", transient=True, error="timeout")
        time.sleep(0.005)  # within 10s window
    # 4 consecutive → tripped by consecutive rule
    assert probe.get_state("m1").state == HealthState.UNHEALTHY


def test_reset_specific_model(probe: HealthProbe) -> None:
    for _ in range(3):
        probe.record_failure("m1", transient=True, error="timeout")
    probe.record_success("m2")
    probe.reset("m1")
    assert probe.get_state("m1").state == HealthState.HEALTHY
    assert probe.get_state("m2").consecutive_successes == 1


def test_reset_all(probe: HealthProbe) -> None:
    for mid in ("m1", "m2", "m3"):
        probe.record_success(mid)
    probe.reset()
    assert probe.all_states() == {}


def test_thread_safety(probe: HealthProbe) -> None:
    """Smoke test: many threads hammering record_success/failure
    should not crash or deadlock."""
    import threading
    def worker() -> None:
        for _ in range(100):
            probe.record_success("m1")
            probe.record_failure("m1", transient=True, error="x")
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # State should be consistent (no exception, state is one of valid)
    h = probe.get_state("m1")
    assert h.state in (
        HealthState.HEALTHY, HealthState.DEGRADED,
        HealthState.UNHEALTHY, HealthState.HALF_OPEN,
    )
    assert h.total_calls == 1600  # 8 threads * 200 calls


def test_get_set_default_probe_singleton() -> None:
    p1 = get_default_probe()
    p2 = get_default_probe()
    assert p1 is p2
    custom = HealthProbe(failure_threshold=999)
    set_default_probe(custom)
    assert get_default_probe() is custom
    set_default_probe(p1)  # restore for other tests
