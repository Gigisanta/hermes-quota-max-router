"""
Tests for QuotaManager.maybe_reset_due() auto-reset logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.quota_manager import QuotaManager


class FakeStore:
    """Minimal in-memory stand-in for Redis hash operations."""

    def __init__(self):
        self._data: dict[str, dict[str, str]] = {}

    def ping(self) -> bool:
        return True

    def hset(self, name, key=None, value=None, mapping=None):
        self._data.setdefault(name, {})
        if mapping:
            self._data[name].update({k: str(v) for k, v in mapping.items()})
        elif key is not None and value is not None:
            self._data[name][key] = str(value)
        return 1

    def hgetall(self, name):
        return dict(self._data.get(name, {}))

    def expire(self, name, time):
        return True

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self._data if k.startswith(prefix)]


@pytest.fixture
def qm():
    store = FakeStore()
    q = QuotaManager(store=store)  # type: ignore[arg-type]
    # Freeze 'now' to midday UTC to avoid TZ-edge flakiness around midnight UTC,
    # where (now - 2h) would actually be yesterday's date.
    frozen_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    return q, store, frozen_now


def _seed_quota(store, model_id, total=1000, remaining=200, last_reset_iso=None, schedule="daily_at_midnight"):
    store.hset(
        f"quota:{model_id}",
        mapping={
            "total": str(total),
            "remaining": str(remaining),
            "last_reset": last_reset_iso or datetime.now(timezone.utc).isoformat(),
            "reset_schedule": schedule,
        },
    )


class TestMaybeResetDue:
    def test_daily_resets_when_last_reset_was_yesterday(self, qm):
        q, store, now = qm
        yesterday = (now - timedelta(days=1)).isoformat()
        _seed_quota(store, "model-a", remaining=10, last_reset_iso=yesterday)
        n = q.maybe_reset_due(now=now)
        assert n == 1
        snap = q.snapshot("model-a")
        assert snap.remaining == snap.total  # back to total

    def test_daily_does_not_reset_when_last_reset_was_today(self, qm):
        q, store, now = qm
        earlier_today = (now - timedelta(hours=2)).isoformat()
        _seed_quota(store, "model-a", remaining=10, last_reset_iso=earlier_today)
        n = q.maybe_reset_due(now=now)
        assert n == 0
        # remaining unchanged
        assert q.remaining("model-a") == 10

    def test_hourly_resets_after_1h(self, qm):
        q, store, now = qm
        two_hours_ago = (now - timedelta(hours=2)).isoformat()
        _seed_quota(store, "model-b", remaining=5, last_reset_iso=two_hours_ago, schedule="hourly")
        n = q.maybe_reset_due(now=now)
        assert n == 1
        snap = q.snapshot("model-b")
        assert snap.remaining == snap.total

    def test_hourly_does_not_reset_within_1h(self, qm):
        q, store, now = qm
        thirty_min_ago = (now - timedelta(minutes=30)).isoformat()
        _seed_quota(store, "model-b", remaining=5, last_reset_iso=thirty_min_ago, schedule="hourly")
        n = q.maybe_reset_due(now=now)
        assert n == 0
        assert q.remaining("model-b") == 5

    def test_empty_schedule_is_manual_only(self, qm):
        """Models with no schedule (paid) are never auto-reset."""
        q, store, now = qm
        ten_days_ago = (now - timedelta(days=10)).isoformat()
        _seed_quota(store, "model-c", remaining=1, last_reset_iso=ten_days_ago, schedule="")
        n = q.maybe_reset_due(now=now)
        assert n == 0
        assert q.remaining("model-c") == 1

    def test_unparseable_last_reset_resets_defensively(self, qm):
        q, store, now = qm
        _seed_quota(store, "model-d", remaining=1, last_reset_iso="not-a-date")
        n = q.maybe_reset_due(now=now)
        assert n == 1
        snap = q.snapshot("model-d")
        assert snap.remaining == snap.total

    def test_multiple_quotas_partial_reset(self, qm):
        """Only the due ones reset, not the rest."""
        q, store, now = qm
        yesterday = (now - timedelta(days=1)).isoformat()
        earlier_today = (now - timedelta(hours=2)).isoformat()
        _seed_quota(store, "model-due", remaining=10, last_reset_iso=yesterday)
        _seed_quota(store, "model-fresh", remaining=20, last_reset_iso=earlier_today)
        n = q.maybe_reset_due(now=now)
        assert n == 1
        assert q.remaining("model-due") == q.snapshot("model-due").total
        assert q.remaining("model-fresh") == 20

    def test_idempotent_when_nothing_due(self, qm):
        q, store, now = qm
        _seed_quota(store, "model-x", remaining=5, last_reset_iso=now.isoformat())
        assert q.maybe_reset_due(now=now) == 0
        assert q.maybe_reset_due(now=now) == 0  # calling twice doesn't change anything
