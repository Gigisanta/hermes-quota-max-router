"""Tests for the Model Registry (Phase 1)."""

import json
from pathlib import Path

import pytest

from core.model_registry import ModelRegistry


@pytest.fixture
def tmp_registry(tmp_path: Path) -> ModelRegistry:
    db = tmp_path / "reg.sqlite"
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": "test/free-1",
                        "provider": "test",
                        "display_name": "Test Free 1",
                        "context_window": 100000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 1,
                        "strength_tags": ["deep_reasoning"],
                        "weakness_tags": [],
                        "best_for": ["coding"],
                        "performance_score": 90.0,
                        "notes": "seed",
                    },
                    {
                        "model_id": "test/paid-1",
                        "provider": "test",
                        "display_name": "Test Paid 1",
                        "context_window": 100000,
                        "input_price": 0.0001,
                        "output_price": 0.0003,
                        "is_free": False,
                        "tier_rank": 99,
                        "strength_tags": ["agentic_god"],
                        "weakness_tags": [],
                        "best_for": ["orchestration"],
                        "performance_score": 95.0,
                        "notes": "preserve",
                    },
                ]
            }
        )
    )
    return ModelRegistry(db_path=db, seed_path=seed)


def test_seed_loaded(tmp_registry: ModelRegistry) -> None:
    assert tmp_registry.count() == 2


def test_free_first_ordering(tmp_registry: ModelRegistry) -> None:
    free = tmp_registry.free_first()
    assert len(free) == 1
    assert free[0].model_id == "test/free-1"
    assert free[0].is_free is True


def test_upsert_updates_existing(tmp_registry: ModelRegistry) -> None:
    m = tmp_registry.get("test/free-1")
    assert m is not None
    m.performance_score = 99.9
    m.notes = "updated"
    tmp_registry.upsert(m)
    again = tmp_registry.get("test/free-1")
    assert again is not None
    assert again.performance_score == 99.9
    assert again.notes == "updated"


def test_all_orders_free_before_paid(tmp_registry: ModelRegistry) -> None:
    models = tmp_registry.all()
    # First half should all be free
    free_idx = [i for i, m in enumerate(models) if m.is_free]
    paid_idx = [i for i, m in enumerate(models) if not m.is_free]
    if free_idx and paid_idx:
        assert max(free_idx) < min(paid_idx)
