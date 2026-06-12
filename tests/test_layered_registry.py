"""Tests for the layered registry (Phase 17)."""

import json
from pathlib import Path

import pytest

from core.layered_registry import LayeredRegistry


@pytest.fixture
def curated_path(tmp_path: Path) -> Path:
    p = tmp_path / "curated.json"
    p.write_text(
        json.dumps(
            {
                "version": "2026-06-10",
                "models": [
                    {
                        "model_id": "c/a",
                        "provider": "c",
                        "display_name": "Curated A",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 1,
                        "strength_tags": ["deep_reasoning"],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 95.0,
                    },
                ],
            }
        )
    )
    return p


@pytest.fixture
def discovered_path(tmp_path: Path) -> Path:
    p = tmp_path / "discovered.json"
    p.write_text(
        json.dumps(
            {
                "version": "2026-06-10",
                "models": [
                    {
                        "model_id": "d/x",
                        "provider": "d",
                        "display_name": "Disc X",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 20,
                        "strength_tags": ["coding_sota"],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 80.0,
                    },
                    {
                        "model_id": "d/y",
                        "provider": "d",
                        "display_name": "Disc Y",
                        "context_window": 2000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 30,
                        "strength_tags": ["vision_master"],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 85.0,
                    },
                ],
            }
        )
    )
    return p


def test_layered_merges_both_sources(curated_path, discovered_path) -> None:
    reg = LayeredRegistry.from_defaults(curated_path, discovered_path)
    assert reg.count() == 3
    ids = {m.model_id for m in reg.all()}
    assert ids == {"c/a", "d/x", "d/y"}


def test_layered_curated_wins_on_conflict(curated_path, tmp_path) -> None:
    # Both registries have the same model_id — curated wins
    dup = tmp_path / "discovered.json"
    dup.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": "c/a",
                        "provider": "d",
                        "display_name": "DUPLICATE",
                        "context_window": 999,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 99,
                        "strength_tags": ["junk"],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 50.0,
                    },
                ],
            }
        )
    )
    reg = LayeredRegistry.from_defaults(curated_path, dup)
    m = reg.get("c/a")
    assert m.display_name == "Curated A"  # curated version, not the dup
    assert m.performance_score == 95.0


def test_layered_free_first(curated_path, discovered_path) -> None:
    reg = LayeredRegistry.from_defaults(curated_path, discovered_path)
    free = reg.free_first()
    # All 3 are free in this fixture
    assert len(free) == 3
    # Curated should appear first (tier 1 < tier 20)
    assert free[0].model_id == "c/a"


def test_layered_missing_discovered_file(curated_path, tmp_path) -> None:
    """If discovered.json doesn't exist, layer just returns curated."""
    no_disc = tmp_path / "no_such_file.json"
    reg = LayeredRegistry.from_defaults(curated_path, no_disc)
    assert reg.count() == 1
    assert reg.all()[0].model_id == "c/a"


def test_layered_summary(curated_path, discovered_path) -> None:
    reg = LayeredRegistry.from_defaults(curated_path, discovered_path)
    s = reg.summary()
    assert s["curated_count"] == 1
    assert s["discovered_count"] == 2
    assert s["merged_count"] == 3
    assert s["free_count"] == 3


def test_layered_by_tier(curated_path, discovered_path) -> None:
    reg = LayeredRegistry.from_defaults(curated_path, discovered_path)
    by_tier = reg.by_tier(free_only=True)
    ranks = [m.tier_rank for m in by_tier]
    assert ranks == sorted(ranks)
