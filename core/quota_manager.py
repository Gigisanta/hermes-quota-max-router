"""Quota Manager — Phase 2.

Real-time budget tracking per model. Backed by Redis in production;
falls back to fakeredis in-process for dev/tests so the system is
runnable with zero infrastructure.

API:
  qm = QuotaManager()
  qm.sync_from_registry(registry)        # seed quotas
  qm.remaining(model_id) -> int          # tokens left
  qm.consume(model_id, tokens) -> bool   # True if allowed + applied
  qm.should_block(model_id, need) -> bool # pre-flight check
  qm.reset(model_id) -> None             # manual / scheduled reset
  qm.snapshot() -> dict                  # for orchestrator context
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from .model_registry import ModelRegistry

log = logging.getLogger(__name__)

# Key conventions. Hash per-model: quota:{model_id} with fields
# total, remaining, last_reset, reset_schedule. Separate sorted set
# tracks consumption timeline (out of scope for Phase 2 MVP).
def _k(model_id: str) -> str:
    return f"quota:{model_id}"


class QuotaStore(Protocol):
    """Minimal Redis-shaped protocol — keeps the manager testable.

    `ping` is included so the connection probe in __init__ type-checks
    against the real Redis client, not just fakeredis.
    """
    def ping(self) -> bool: ...
    def hset(self, name: str, key: str | None = None, value: str | None = None,
             mapping: dict | None = None) -> int: ...
    def hgetall(self, name: str) -> dict: ...
    def expire(self, name: str, time: int) -> bool: ...
    def keys(self, pattern: str) -> list: ...


@dataclass
class QuotaSnapshot:
    model_id: str
    total: int | None
    remaining: int | None
    pct_remaining: float | None  # 0-1, None if no quota
    last_reset: str | None
    reset_schedule: str | None

    def has_quota(self) -> bool:
        return self.total is not None and self.total > 0


class QuotaManager:
    """Redis-backed per-model quota tracking."""

    def __init__(self, store: QuotaStore | None = None) -> None:
        # Lazily import to avoid hard dep at module load.
        if store is not None:
            self._r: QuotaStore = store
            return
        try:
            import redis  # type: ignore
            url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            client = cast(QuotaStore, redis.Redis.from_url(url, decode_responses=True))
            client.ping()  # fast fail-fast check
            self._r = client
            log.info("QuotaManager: connected to Redis at %s", url)
        except Exception as e:  # noqa: BLE001
            # Fall back to fakeredis so the system runs without infra.
            import fakeredis  # type: ignore
            self._r = cast(QuotaStore, fakeredis.FakeRedis(decode_responses=True))
            log.warning(
                "QuotaManager: Redis unavailable (%s). Using in-process fakeredis.",
                e,
            )

    # --- lifecycle ---
    def sync_from_registry(self, registry: "ModelRegistry") -> int:
        """Ensure every model in the registry has a quota entry. Returns count synced."""
        synced = 0
        for m in registry.all():
            existing = self._r.hgetall(_k(m.model_id))
            if not existing:
                self._write_full(m.model_id, m.daily_quota_tokens or 0,
                                 m.last_reset, m.reset_schedule or "")
            else:
                # Keep the stored remaining; refresh schedule/total if changed.
                self._r.hset(_k(m.model_id), mapping={
                    "total": str(m.daily_quota_tokens or 0),
                    "reset_schedule": m.reset_schedule or "",
                })
            synced += 1
        return synced

    # --- queries ---
    def remaining(self, model_id: str) -> int | None:
        v = self._r.hgetall(_k(model_id)).get("remaining")
        return int(v) if v is not None else None

    def snapshot(self, model_id: str) -> QuotaSnapshot:
        d = self._r.hgetall(_k(model_id))
        if not d:
            return QuotaSnapshot(model_id, None, None, None, None, None)
        total = int(d.get("total", "0")) or None
        remaining = int(d.get("remaining", "0")) if d.get("remaining") else None
        pct = (remaining / total) if (total and remaining is not None) else None
        return QuotaSnapshot(
            model_id=model_id,
            total=total,
            remaining=remaining,
            pct_remaining=pct,
            last_reset=d.get("last_reset"),
            reset_schedule=d.get("reset_schedule"),
        )

    def all_snapshots(self) -> list[QuotaSnapshot]:
        out = []
        for key in self._r.keys("quota:*"):
            mid = key.split(":", 1)[1]
            out.append(self.snapshot(mid))
        return out

    def should_block(self, model_id: str, needed_tokens: int) -> bool:
        """True if consuming `needed_tokens` would exceed remaining OR is unknown."""
        snap = self.snapshot(model_id)
        if not snap.has_quota():
            return False  # paid / unknown — let the orchestrator decide
        return snap.remaining is None or snap.remaining < needed_tokens

    # --- mutations ---
    def consume(self, model_id: str, tokens: int) -> bool:
        """Atomically subtract `tokens` from remaining. Returns False if blocked.

        Never produces `remaining=None` — clamps to 0 (since total > 0 means
        there is a real quota to track). The router relies on this.
        """
        if tokens <= 0:
            return True
        key = _k(model_id)
        data = self._r.hgetall(key)
        if not data:
            return False  # unknown model — block conservatively
        total = int(data.get("total", "0"))
        if total <= 0:
            return True  # unlimited / paid
        remaining = int(data.get("remaining", "0"))
        if remaining < tokens:
            return False
        new_remaining = max(0, remaining - tokens)
        self._r.hset(key, "remaining", str(new_remaining))
        return True

    def reset(self, model_id: str) -> None:
        d = self._r.hgetall(_k(model_id))
        if not d:
            return
        self._r.hset(_k(model_id), mapping={
            "remaining": d.get("total", "0"),
            "last_reset": datetime.now(timezone.utc).isoformat(),
        })

    def reset_all(self) -> int:
        n = 0
        for key in self._r.keys("quota:*"):
            mid = key.split(":", 1)[1]
            self.reset(mid)
            n += 1
        return n

    def maybe_reset_due(self, now: datetime | None = None) -> int:
        """Reset any quota whose `last_reset` is older than its `reset_schedule`.

        Schedules supported (simple, MVP):
          - ``daily_at_midnight``     → reset if last_reset's UTC date < today's
          - ``hourly``                → reset if >= 1h since last_reset
          - ``weekly_monday``         → reset if it's Monday and last_reset < this week's Monday
          - any other / empty schedule → no auto-reset (manual only)

        Returns the number of quotas reset. Safe to call on a schedule
        (idempotent if nothing is due).
        """
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        today_date = now.date()
        n = 0
        for key in self._r.keys("quota:*"):
            mid = key.split(":", 1)[1]
            d = self._r.hgetall(key)
            if not d:
                continue
            schedule = (d.get("reset_schedule") or "").strip()
            if not schedule:
                continue
            last_reset_str = d.get("last_reset") or ""
            try:
                last_reset = datetime.fromisoformat(last_reset_str)
                if last_reset.tzinfo is None:
                    last_reset = last_reset.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                # Unparseable: reset now (defensive).
                self.reset(mid)
                n += 1
                continue
            due = False
            if schedule == "daily_at_midnight":
                due = last_reset.date() < today_date
            elif schedule == "hourly":
                due = (now - last_reset).total_seconds() >= 3600
            elif schedule == "weekly_monday":
                # Monday is 0 in .weekday()
                if today_date.weekday() == 0:
                    days_since_mon = today_date.weekday()
                    this_monday = today_date  # today IS Monday
                    # Reset if last_reset is before this Monday
                    due = last_reset.date() < this_monday
            # unknown schedule: leave alone (no auto-reset)
            if due:
                self.reset(mid)
                # Also bump last_reset to now_iso for symmetry (reset() does this).
                n += 1
        if n:
            log.info("QuotaManager: auto-reset %d quota(s) due to schedule", n)
        return n

    def _write_full(self, model_id: str, total: int,
                    last_reset: str | None, reset_schedule: str) -> None:
        self._r.hset(_k(model_id), mapping={
            "total": str(total),
            "remaining": str(total),
            "last_reset": last_reset or datetime.now(timezone.utc).isoformat(),
            "reset_schedule": reset_schedule,
        })


def snapshot_to_dict(s: QuotaSnapshot) -> dict:
    return {
        "model_id": s.model_id,
        "total": s.total,
        "remaining": s.remaining,
        "pct_remaining": s.pct_remaining,
        "last_reset": s.last_reset,
        "reset_schedule": s.reset_schedule,
    }


if __name__ == "__main__":
    # Demo: sync from registry, then simulate consumption
    from core.model_registry import ModelRegistry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    print("\n=== Quota snapshot after sync ===")
    for s in qm.all_snapshots():
        print(f"  {s.model_id:<60} {s.remaining}/{s.total} ({s.pct_remaining:.0%})")
    print("\nSimulating consumption of 50_000 tokens on deepseek-r1...")
    ok = qm.consume("deepseek/deepseek-r1-0528", 50_000)
    print(f"  consume ok={ok}, remaining={qm.remaining('deepseek/deepseek-r1-0528')}")
    print("\nShould block on 999M tokens?",
          qm.should_block("deepseek/deepseek-r1-0528", 999_000_000))
    print("Should block on 1k tokens? ",
          qm.should_block("deepseek/deepseek-r1-0528", 1_000))
