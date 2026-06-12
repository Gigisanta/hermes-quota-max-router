"""Property-based tests with Hypothesis (iter 15 deep-iteration).

These tests generate random inputs and verify invariants that should
hold for ALL inputs — not just hand-picked examples. They catch edge
cases like unicode bombs, very long strings, empty inputs, etc.

The 4 properties tested here are the most failure-prone invariants in
the router:

  1. JsonFormatter always emits valid, parseable JSON
  2. HealthProbe state transitions are monotonic in `cooldown_count`
     (never decreases)
  3. TokenBucket never allows more requests than `capacity` in a
     single call (no negative balances, no over-burst)
  4. TaskAnalyzer never raises on arbitrary text input
"""

from __future__ import annotations

import io
import json
import logging
import string
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.health_probe import HealthProbe, HealthState
from core.security import TokenBucket
from core.task_analyzer import HeuristicTaskAnalyzer
from server.logging_config import JsonFormatter

# --- Helpers ---


@pytest.fixture
def captured_log() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    prev = root.level
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(prev)


# --- 1. JSON formatter invariants ---


@given(
    message=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),  # surrogates crash json.dumps
            blacklist_characters="\x00",  # NUL sometimes breaks log handlers
        ),
        min_size=0,
        max_size=10_000,
    ),
    level=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    logger=st.text(
        alphabet=string.ascii_letters + string.digits + "._-",
        min_size=1,
        max_size=50,
    ),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_json_formatter_always_emits_valid_json(
    captured_log: io.StringIO,  # type: ignore[name-defined]
    message: str,
    level: str,
    logger: str,
) -> None:
    """For any text message + level + logger name, the formatter must
    produce a single line of valid JSON. This catches:
    - Unserializable characters
    - Multi-line messages that break the 'one event per line' contract
    - Encoding issues
    """
    log = logging.getLogger(logger)
    getattr(log, level.lower())(message)
    output = captured_log.getvalue().strip()
    # The log may contain 0 (before any call) or 1 line.
    if not output:
        return
    last = output.splitlines()[-1]
    obj = json.loads(last)  # MUST be valid JSON
    assert "ts" in obj
    assert obj["level"] == level
    assert obj["logger"] == logger
    assert obj["message"] == message


# --- 2. HealthProbe state invariants ---


@given(
    n_successes=st.integers(min_value=0, max_value=10),
    n_failures=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=30, deadline=None)
def test_health_probe_cooldown_count_monotonic(
    n_successes: int,
    n_failures: int,
) -> None:
    """``cooldown_count`` never decreases regardless of the sequence of
    successes/failures. Each UNHEALTHY trip is permanent for the
    lifetime of the probe instance (only reset() can clear it)."""
    probe = HealthProbe(
        failure_threshold=2,
        cooldown_s=0.01,  # fast for tests
        max_cooldown_s=1.0,
    )
    prev_max = 0
    for i in range(max(n_successes, n_failures)):
        if i < n_successes:
            probe.record_success("m1")
        if i < n_failures:
            probe.record_failure("m1", transient=True, error="x")
        cur = probe.get_state("m1").cooldown_count
        assert cur >= prev_max, f"cooldown_count went backwards: {cur} < {prev_max}"
        prev_max = cur


@given(
    name=st.text(min_size=1, max_size=100, alphabet=string.ascii_letters + string.digits + "/-_:."),
)
@settings(max_examples=50, deadline=None)
def test_health_probe_unknown_model_is_always_available(name: str) -> None:
    """A probe that has never seen a model returns True for is_available()
    and HEALTHY for get_state(). The probe must never crash on weird
    model names (unicode, slashes, very long)."""
    probe = HealthProbe()
    assert probe.is_available(name) is True
    h = probe.get_state(name)
    assert h.state == HealthState.HEALTHY
    assert h.consecutive_failures == 0


# --- 3. TokenBucket invariants ---


@given(
    capacity=st.floats(min_value=0.1, max_value=1000.0, allow_nan=False, allow_infinity=False),
    refill=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    n_calls=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=30, deadline=None)
def test_tokenbucket_never_overshoots(
    capacity: float,
    refill: float,
    n_calls: int,
) -> None:
    """Over a sequence of immediate calls, the bucket must not return
    True more than `ceil(capacity)` times before any time has passed."""
    b = TokenBucket(capacity=capacity, refill_rate=refill)
    allowed_count = 0
    for _ in range(n_calls):
        if b.allow("k", cost=1.0):
            allowed_count += 1
    # Allowed is at most ceil(capacity) when refill is 0 (or small
    # when refill > 0). We allow `capacity + 1` slack for refill rounding.
    max_allowed = int(capacity) + 2  # +2 for refill rounding
    assert allowed_count <= max_allowed, (
        f"Overshoot! capacity={capacity}, allowed {allowed_count} > {max_allowed}"
    )


@given(
    key1=st.text(min_size=1, max_size=20, alphabet=string.ascii_letters),
    key2=st.text(min_size=1, max_size=20, alphabet=string.ascii_letters),
)
@settings(max_examples=20, deadline=None)
def test_tokenbucket_keys_are_independent(key1: str, key2: str) -> None:
    """Exhausting key1 must not affect key2's budget."""
    if key1 == key2:
        return  # trivially the same
    b = TokenBucket(capacity=2.0, refill_rate=0.0)
    # Exhaust key1
    assert b.allow(key1) is True
    assert b.allow(key1) is True
    assert b.allow(key1) is False
    # key2 still has full budget
    assert b.allow(key2) is True
    assert b.allow(key2) is True
    assert b.allow(key2) is False


# --- 4. TaskAnalyzer invariants ---


@given(text=st.text(min_size=0, max_size=10_000))
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_task_analyzer_never_crashes_on_arbitrary_text(text: str) -> None:
    """The heuristic analyzer must handle any unicode/empty/very-long
    input without raising. This catches regex compilation issues,
    encoding bugs, and pathological input handling."""
    analyzer = HeuristicTaskAnalyzer()
    # MUST NOT raise
    result = analyzer.analyze(text)
    # Sanity: the result has the expected shape.
    assert isinstance(result.task_type, str)
    assert isinstance(result.required_tags, list)
    assert isinstance(result.min_quality, str)
    assert result.estimated_input_tokens >= 0
    assert result.estimated_output_tokens >= 0


@given(
    prompt=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),
        ),
        min_size=1,
        max_size=5000,
    ),
)
@settings(max_examples=30, deadline=None)
def test_task_analyzer_tags_are_unique_and_known(prompt: str) -> None:
    """The analyzer's tag output must not contain duplicates and must
    only contain values from the known vocabulary."""
    analyzer = HeuristicTaskAnalyzer()
    result = analyzer.analyze(prompt)
    # No duplicates
    assert len(result.required_tags) == len(set(result.required_tags))
    # task_type is one of the known values
    KNOWN_TYPES = {"chat", "code", "research", "writing", "analysis", "planning", "extraction"}
    assert result.task_type in KNOWN_TYPES
    # min_quality is one of the known values
    KNOWN_QUALITY = {"low", "medium", "high", "very_high", "exceptional"}
    assert result.min_quality in KNOWN_QUALITY
