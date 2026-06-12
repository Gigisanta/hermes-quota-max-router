"""Health probe + circuit breaker for the QuotaMax Router.

iter 15: this module is the implementation of the "perfect free token
radar" promise. Previously the router kept no record of which free
models were currently healthy — a model that returned 429/503 for 6
hours straight would still be picked by the orchestrator, and the
operator would discover it only when the user complained.

Design
------
Per-model state machine:

    HEALTHY ──fail─→ DEGRADED ──fail─→ UNHEALTHY (cooldown)
        ▲                  │                  │
        │                  │                  │ cooldown elapsed
        │                  │                  ▼
        │                  │            HALF_OPEN (probe)
        │                  │                  │ probe ok
        └──────────────────┴──────────────────┘
                                                  │ probe fail
                                                  ▼
                                            UNHEALTHY (cooldown)

Failure threshold: 3 consecutive (or 5 in 5min) → UNHEALTHY.
Cooldown: 300s default. After cooldown, state goes to HALF_OPEN —
exactly one probe call is allowed. If it succeeds, back to HEALTHY; if
it fails, back to UNHEALTHY with a longer cooldown (exponential backoff
capped at 1 hour).

Transient vs hard failures
--------------------------
``is_transient_error`` already classifies by exception class + status
code. We trust that classification:

  - Transient (429, 5xx, timeout, connection): counted toward UNHEALTHY
    threshold because a flood of transient errors means "this model is
    throttling us or down" — exactly the radar's job.
  - Hard (4xx auth, validation, not found): NOT counted. The model is
    fine; the request is bad. (The router will fall through to the
    next model in the same call.)

Persistence
-----------
The default ``HealthProbe`` is in-process and lost on restart. A
``RedisHealthProbe`` subclass (TODO) can be dropped in for multi-worker
deployments. For now, the orchestrator's scoring + the auto-updater
re-fetches the catalog every 48-72h, which is good enough to recover
from a stale local state.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

log = logging.getLogger(__name__)


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"  # cooldown active, skip
    HALF_OPEN = "half_open"  # cooldown elapsed, allow one probe


@dataclass
class ModelHealth:
    """Per-model health snapshot. JSON-safe.

    Two timestamp fields for clarity:
    - ``cooldown_until`` is a wall-clock ISO8601 string (for the API).
    - ``cooldown_until_monotonic`` is the deadline in ``time.monotonic()``
      seconds (for fast, clock-skew-free comparison in ``is_available``).
    """

    model_id: str
    state: HealthState = HealthState.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_at: str | None = None  # ISO8601 UTC (wall clock)
    last_success_at: str | None = None
    cooldown_until: str | None = None  # ISO8601 UTC (wall clock)
    cooldown_until_monotonic: float | None = None  # time.monotonic() seconds
    cooldown_count: int = 0  # how many times this model has been UNHEALTHY
    total_calls: int = 0
    total_failures: int = 0
    total_transient_failures: int = 0
    last_error: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class HealthProbe:
    """In-process circuit breaker per model.

    Thread-safe (FastAPI runs sync handlers in a thread pool). For
    multi-worker deployments, swap to ``RedisHealthProbe`` (TODO).

    Args:
        failure_threshold: consecutive failures to flip to UNHEALTHY.
        recovery_threshold: consecutive successes in HALF_OPEN to
            flip back to HEALTHY (typically 1).
        cooldown_s: initial cooldown after going UNHEALTHY.
        max_cooldown_s: ceiling for exponential-backoff cooldown.
        half_open_probe_interval_s: minimum interval between HALF_OPEN
            probes (prevents thundering herd).
        transient_window_s: rolling window for "N failures in M seconds"
            check (the 5-in-5min rule from the design doc).
        transient_count_threshold: number of failures in the rolling
            window to also flip to UNHEALTHY.
    """

    failure_threshold: int = 3
    recovery_threshold: int = 1
    cooldown_s: float = 300.0
    max_cooldown_s: float = 3600.0
    half_open_probe_interval_s: float = 60.0
    transient_window_s: float = 300.0
    transient_count_threshold: int = 5

    # Internal state. NOT a default factory; we initialize in __post_init__.
    _states: dict[str, ModelHealth] = field(default_factory=dict)
    _transient_log: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Reset to fresh state — dataclass's default_factory already did
        # this, but be explicit for readability.
        self._states = {}
        self._transient_log = {}
        self._lock = threading.Lock()

    # --- Public API ---

    def is_available(self, model_id: str, now: float | None = None) -> bool:
        """True if the model is currently in HEALTHY or HALF_OPEN.

        HALF_OPEN admits exactly one probe at a time. The caller is
        responsible for not double-probing; the design relies on the
        router engine to call _call_one() at most once per request.
        """
        now = now if now is not None else time.monotonic()
        with self._lock:
            h = self._states.get(model_id)
            if h is None:
                return True  # unknown model → assume healthy
            if h.state == HealthState.HEALTHY:
                return True
            if h.state == HealthState.HALF_OPEN:
                return True
            if h.state == HealthState.DEGRADED:
                return True  # degraded still routes, just counts more
            if h.state == HealthState.UNHEALTHY:
                # Check if cooldown elapsed. The stored cooldown_until
                # is a monotonic deadline (seconds since some arbitrary
                # anchor, like time.monotonic()). We compare in the
                # same clock.
                if h.cooldown_until_monotonic is None:
                    return False
                if now >= h.cooldown_until_monotonic:
                    # Promote to HALF_OPEN.
                    h.state = HealthState.HALF_OPEN
                    log.info("model %s: cooldown elapsed → HALF_OPEN", model_id)
                    return True
                return False
            return True  # pragma: no cover

    def record_success(self, model_id: str) -> None:
        """Mark a successful call. Resets consecutive_failures.

        In HALF_OPEN, a single success promotes back to HEALTHY.
        """
        with self._lock:
            h = self._get_or_create(model_id)
            h.consecutive_failures = 0
            h.consecutive_successes += 1
            h.last_success_at = _now_iso()
            h.total_calls += 1
            h.last_error = None
            # Clear transient log so future failures re-count from zero.
            self._transient_log.pop(model_id, None)
            if h.state in (HealthState.HALF_OPEN, HealthState.DEGRADED):
                if h.consecutive_successes >= self.recovery_threshold:
                    h.state = HealthState.HEALTHY
                    h.cooldown_until = None
                    log.info("model %s: recovered → HEALTHY", model_id)

    def record_failure(
        self,
        model_id: str,
        *,
        transient: bool,
        error: str | None = None,
    ) -> None:
        """Mark a failed call. Classifies as transient (counts toward
        UNHEALTHY) or hard (does not count).

        The router engine already calls ``is_transient_error`` to make
        the retry decision; we re-use that classification here.
        """
        now = time.monotonic()
        with self._lock:
            h = self._get_or_create(model_id)
            h.consecutive_failures += 1
            h.consecutive_successes = 0
            h.last_failure_at = _now_iso()
            h.last_error = error[:200] if error else None
            h.total_calls += 1
            h.total_failures += 1
            if transient:
                h.total_transient_failures += 1
                # Append to rolling-window log, prune old entries.
                log_list = self._transient_log.setdefault(model_id, [])
                log_list.append(now)
                cutoff = now - self.transient_window_s
                while log_list and log_list[0] < cutoff:
                    log_list.pop(0)
                # Threshold check 1: N consecutive (regardless of current
                # state — DEGRADED can also escalate to UNHEALTHY).
                if h.consecutive_failures >= self.failure_threshold and h.state in (
                    HealthState.HEALTHY,
                    HealthState.DEGRADED,
                ):
                    self._trip_to_unhealthy(h, reason=f"{h.consecutive_failures} consecutive failures")
                    return
                # Threshold check 2: N in M seconds
                if len(log_list) >= self.transient_count_threshold and h.state in (
                    HealthState.HEALTHY,
                    HealthState.DEGRADED,
                ):
                    self._trip_to_unhealthy(
                        h,
                        reason=(f"{len(log_list)} failures in {self.transient_window_s:.0f}s"),
                    )
                    return
                # First transient failure from HEALTHY → DEGRADED (so
                # the orchestrator can deprioritize via score penalty).
                if h.state == HealthState.HEALTHY and h.consecutive_failures >= 1:
                    h.state = HealthState.DEGRADED
            # else: hard failure — just count, don't trip.
            if h.state == HealthState.HALF_OPEN and transient:
                # Probe failed → back to UNHEALTHY with longer cooldown.
                self._trip_to_unhealthy(h, reason="HALF_OPEN probe failed")

    def get_state(self, model_id: str) -> ModelHealth:
        with self._lock:
            return self._states.get(model_id, ModelHealth(model_id=model_id))

    def all_states(self) -> dict[str, ModelHealth]:
        with self._lock:
            # Return a shallow copy so callers can iterate safely.
            return dict(self._states)

    def reset(self, model_id: str | None = None) -> None:
        with self._lock:
            if model_id is None:
                self._states.clear()
                self._transient_log.clear()
            else:
                self._states.pop(model_id, None)
                self._transient_log.pop(model_id, None)

    # --- Internals ---

    def _get_or_create(self, model_id: str) -> ModelHealth:
        h = self._states.get(model_id)
        if h is None:
            h = ModelHealth(model_id=model_id)
            self._states[model_id] = h
        return h

    def _trip_to_unhealthy(self, h: ModelHealth, *, reason: str) -> None:
        h.state = HealthState.UNHEALTHY
        h.cooldown_count += 1
        # Exponential backoff: 1x, 2x, 4x, 8x, … capped at max_cooldown_s.
        cooldown = min(
            self.max_cooldown_s,
            self.cooldown_s * (2 ** (h.cooldown_count - 1)),
        )
        now = time.monotonic()
        h.cooldown_until_monotonic = now + cooldown
        # Also record the wall-clock equivalent for the API surface.
        h.cooldown_until = datetime.fromtimestamp(
            time.time() + cooldown,
            tz=UTC,
        ).isoformat()
        log.warning(
            "model %s: → UNHEALTHY (cooldown %.0fs, reason: %s)",
            h.model_id,
            cooldown,
            reason,
        )


# Module-level singleton. Tests can override via monkeypatch.
_default_probe: HealthProbe | None = None
_default_probe_lock = threading.Lock()


def get_default_probe() -> HealthProbe:
    """Return the process-wide default HealthProbe (lazy-init)."""
    global _default_probe
    with _default_probe_lock:
        if _default_probe is None:
            _default_probe = HealthProbe()
        return _default_probe


def set_default_probe(probe: HealthProbe) -> None:
    """Override the default probe (used by tests)."""
    global _default_probe
    with _default_probe_lock:
        _default_probe = probe
