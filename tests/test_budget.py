"""Tests for BudgetMonitor (Phase 14)."""

import fakeredis
import pytest

from core.budget import BudgetMonitor
from core.quota_manager import QuotaManager


@pytest.fixture
def qm() -> QuotaManager:
    store = fakeredis.FakeRedis(decode_responses=True)
    q = QuotaManager(store=store)
    q._write_full("m/full", total=1000, last_reset=None, reset_schedule="")
    q._write_full("m/half", total=1000, last_reset=None, reset_schedule="")
    q._write_full("m/warn", total=1000, last_reset=None, reset_schedule="")
    q._write_full("m/ok", total=1000, last_reset=None, reset_schedule="")
    q._write_full("m/unlimited", total=0, last_reset=None, reset_schedule="monthly")
    return q


@pytest.fixture
def monitor() -> BudgetMonitor:
    return BudgetMonitor(warn_pct=0.80, block_pct=1.00)


# --- construction ---


def test_invalid_warn_pct_raises() -> None:
    with pytest.raises(ValueError):
        BudgetMonitor(warn_pct=0.0)
    with pytest.raises(ValueError):
        BudgetMonitor(warn_pct=1.5)


def test_invalid_block_pct_raises() -> None:
    with pytest.raises(ValueError):
        BudgetMonitor(warn_pct=0.5, block_pct=0.5)
    with pytest.raises(ValueError):
        BudgetMonitor(warn_pct=0.9, block_pct=0.8)


# --- should_warn / should_block ---


def test_should_warn_at_threshold(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/warn", 200)  # 20% consumed
    assert not monitor.should_warn(qm, "m/warn")
    qm.consume("m/warn", 600)  # 80% consumed
    assert monitor.should_warn(qm, "m/warn")
    assert not monitor.should_block(qm, "m/warn")


def test_should_block_at_full(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/full", 1000)  # 100% consumed
    assert monitor.should_block(qm, "m/full")
    assert monitor.should_warn(qm, "m/full")  # block also implies warn


def test_unlimited_model_never_warns(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    assert not monitor.should_warn(qm, "m/unlimited")
    assert not monitor.should_block(qm, "m/unlimited")


def test_unknown_model_safe(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    assert not monitor.should_warn(qm, "ghost/x")
    assert not monitor.should_block(qm, "ghost/x")


# --- check (event firing) ---


def test_check_fires_warn_once(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/warn", 800)  # 80% consumed
    fired1 = monitor.check(qm, "m/warn")
    assert len(fired1) == 1
    assert fired1[0].level == "warn"
    # Second check does NOT re-fire
    fired2 = monitor.check(qm, "m/warn")
    assert fired2 == []


def test_check_fires_block(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/full", 1000)
    fired = monitor.check(qm, "m/full")
    assert len(fired) == 1
    assert fired[0].level == "block"


def test_check_no_fire_below_threshold(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/ok", 100)  # 10% consumed
    fired = monitor.check(qm, "m/ok")
    assert fired == []


# --- reset_alerts ---


def test_reset_alerts_re_arms(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/warn", 800)
    monitor.check(qm, "m/warn")
    assert any(e.level == "warn" for e in monitor.events)
    # Reset alerts and re-check — should fire again
    monitor.reset_alerts("m/warn")
    qm.consume("m/warn", 100)  # now 90%
    fired = monitor.check(qm, "m/warn")
    assert len(fired) == 1  # re-fired


def test_reset_all_alerts(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/warn", 800)
    qm.consume("m/full", 1000)
    monitor.check(qm, "m/warn")
    monitor.check(qm, "m/full")
    monitor.reset_alerts()
    assert "m/warn" not in monitor._fired_warn
    assert "m/full" not in monitor._fired_block


# --- burn_rates ---


def test_burn_rates_returns_all_tracked(monitor: BudgetMonitor, qm: QuotaManager) -> None:
    qm.consume("m/ok", 100)
    qm.consume("m/warn", 800)
    qm.consume("m/full", 1000)
    rates = monitor.burn_rates(qm)
    assert rates["m/ok"]["status"] == "ok"
    assert rates["m/warn"]["status"] == "warn"
    assert rates["m/full"]["status"] == "block"
    # Unlimited models excluded
    assert "m/unlimited" not in rates
