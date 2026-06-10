"""Tests for cost tracking (Phase 13)."""
import pytest

from core.cost_tracker import CostTracker, compute_cost_usd
from core.model_registry import ModelRegistry
from core.schemas import RoutingDecision


@pytest.fixture
def registry_with_prices(tmp_path) -> ModelRegistry:
    import json
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({
        "models": [
            {"model_id": "free/a", "provider": "f", "display_name": "FA",
             "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
             "is_free": True, "tier_rank": 1, "strength_tags": [],
             "weakness_tags": [], "best_for": [], "performance_score": 50.0},
            {"model_id": "paid/b", "provider": "p", "display_name": "PB",
             "context_window": 1000, "input_price": 0.00001, "output_price": 0.00003,
             "is_free": False, "tier_rank": 99, "strength_tags": [],
             "weakness_tags": [], "best_for": [], "performance_score": 90.0},
            {"model_id": "paid/c", "provider": "p", "display_name": "PC",
             "context_window": 1000, "input_price": 0.000005, "output_price": 0.000015,
             "is_free": False, "tier_rank": 99, "strength_tags": [],
             "weakness_tags": [], "best_for": [], "performance_score": 95.0},
        ]
    }))
    return ModelRegistry(db_path=tmp_path / "r.sqlite", seed_path=seed)


# --- compute_cost_usd ---

def test_compute_cost_free_is_zero(registry_with_prices: ModelRegistry) -> None:
    cost = compute_cost_usd(registry_with_prices, "free/a", 1_000_000, 1_000_000)
    assert cost == 0.0


def test_compute_cost_paid_simple(registry_with_prices: ModelRegistry) -> None:
    # paid/b: input 0.00001/tok, output 0.00003/tok
    # 1000 input + 500 output = 0.01 + 0.015 = 0.025
    cost = compute_cost_usd(registry_with_prices, "paid/b", 1000, 500)
    assert cost == pytest.approx(0.025, abs=1e-9)


def test_compute_cost_different_pricing(registry_with_prices: ModelRegistry) -> None:
    # paid/c: input 0.000005, output 0.000015
    # 2000 input + 1000 output = 0.01 + 0.015 = 0.025
    cost = compute_cost_usd(registry_with_prices, "paid/c", 2000, 1000)
    assert cost == pytest.approx(0.025, abs=1e-9)


def test_compute_cost_unknown_model_is_zero(registry_with_prices: ModelRegistry) -> None:
    cost = compute_cost_usd(registry_with_prices, "nonexistent/x", 1000, 1000)
    assert cost == 0.0


def test_compute_cost_zero_tokens(registry_with_prices: ModelRegistry) -> None:
    assert compute_cost_usd(registry_with_prices, "paid/b", 0, 0) == 0.0


# --- CostTracker ---

def test_tracker_starts_empty() -> None:
    t = CostTracker()
    snap = t.snapshot()
    assert snap.total_usd == 0.0
    assert snap.per_model == {}
    assert snap.call_count == 0


def test_tracker_records_single_call() -> None:
    t = CostTracker()
    t.record("paid/b", 0.025)
    snap = t.snapshot()
    assert snap.total_usd == pytest.approx(0.025, abs=1e-9)
    assert snap.per_model == {"paid/b": pytest.approx(0.025, abs=1e-9)}
    assert snap.call_count == 1


def test_tracker_aggregates_across_models() -> None:
    t = CostTracker()
    t.record("paid/b", 0.010)
    t.record("paid/c", 0.020)
    t.record("paid/b", 0.005)
    snap = t.snapshot()
    assert snap.total_usd == pytest.approx(0.035, abs=1e-9)
    assert snap.per_model["paid/b"] == pytest.approx(0.015, abs=1e-9)
    assert snap.per_model["paid/c"] == pytest.approx(0.020, abs=1e-9)
    assert snap.call_count == 3


def test_tracker_zero_cost_call() -> None:
    t = CostTracker()
    t.record("free/a", 0.0)
    snap = t.snapshot()
    assert snap.total_usd == 0.0
    assert snap.call_count == 1  # calls counted even if cost is zero


def test_tracker_reset() -> None:
    t = CostTracker()
    t.record("paid/b", 0.1)
    t.reset()
    snap = t.snapshot()
    assert snap.total_usd == 0.0
    assert snap.call_count == 0
