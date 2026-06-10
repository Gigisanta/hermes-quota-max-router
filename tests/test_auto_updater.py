"""Tests for the Auto-Updater (Phase 4)."""
import json
from pathlib import Path

import pytest

from core.auto_updater import (
    LocalFeedProvider,
    RegistryUpdater,
    StaticFeedProvider,
    UpdateResult,
    _bump_version,
    _models_differ,
)
from core.model_registry import Model, ModelRegistry


@pytest.fixture
def seed_path(tmp_path: Path) -> Path:
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"version": "2026-06-09", "models": []}))
    return p


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(
        db_path=tmp_path / "r.sqlite",
        seed_path=tmp_path / "seed.json",  # won't be loaded
    )


# --- pure helpers ---

def test_bump_version_empty_returns_today() -> None:
    v = _bump_version("")
    assert v  # non-empty
    assert len(v.split("-")) == 3  # YYYY-MM-DD


def test_bump_version_same_day_appends_rev() -> None:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    v1 = _bump_version(today)
    assert v1 == f"{today}-rev1"
    v2 = _bump_version(v1)
    assert v2 == f"{today}-rev2"


def test_models_differ_detects_change() -> None:
    a = Model(
        model_id="x/y", provider="x", display_name="A", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=["a"], weakness_tags=[], best_for=[], performance_score=80.0,
    )
    b = Model(
        model_id="x/y", provider="x", display_name="A", context_window=2000,  # changed
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=["a"], weakness_tags=[], best_for=[], performance_score=80.0,
    )
    assert _models_differ(a, b) is True


def test_models_differ_ignores_quota_counter() -> None:
    a = Model(
        model_id="x/y", provider="x", display_name="A", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=["a"], weakness_tags=[], best_for=[], performance_score=80.0,
        current_remaining_tokens=500,
    )
    b = Model(
        model_id="x/y", provider="x", display_name="A", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=["a"], weakness_tags=[], best_for=[], performance_score=80.0,
        current_remaining_tokens=999,
    )
    assert _models_differ(a, b) is False


# --- LocalFeedProvider ---

def test_local_feed_provider_reads_file(tmp_path: Path) -> None:
    feed_file = tmp_path / "feed.json"
    feed_file.write_text(json.dumps({"models": [{"model_id": "a/b", "provider": "a", "display_name": "A", "context_window": 100, "input_price": 0.0, "output_price": 0.0, "is_free": True, "tier_rank": 1, "strength_tags": [], "weakness_tags": [], "best_for": [], "performance_score": 50.0}]}))
    provider = LocalFeedProvider(feed_file)
    models = provider.fetch()
    assert len(models) == 1
    assert models[0]["model_id"] == "a/b"


def test_local_feed_provider_rejects_malformed(tmp_path: Path) -> None:
    feed_file = tmp_path / "feed.json"
    feed_file.write_text(json.dumps({"not_models": []}))
    provider = LocalFeedProvider(feed_file)
    with pytest.raises(ValueError):
        provider.fetch()


# --- RegistryUpdater ---

def test_apply_feed_adds_new_model(
    registry: ModelRegistry, seed_path: Path
) -> None:
    feed = StaticFeedProvider([{
        "model_id": "new/model", "provider": "new", "display_name": "New Model",
        "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
        "is_free": True, "tier_rank": 10,
        "strength_tags": ["test"], "weakness_tags": [],
        "best_for": ["testing"], "performance_score": 50.0,
    }]).fetch()
    updater = RegistryUpdater(registry, seed_path)
    result = updater.apply_feed(feed)
    assert "new/model" in result.added
    assert result.total == 1
    assert registry.get("new/model") is not None


def test_apply_feed_updates_changed_model(
    registry: ModelRegistry, seed_path: Path
) -> None:
    # Seed registry with one model
    registry.upsert(Model(
        model_id="x/y", provider="x", display_name="Old", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=["old"], weakness_tags=[], best_for=[], performance_score=50.0,
    ))
    feed = StaticFeedProvider([{
        "model_id": "x/y", "provider": "x", "display_name": "New Name",
        "context_window": 2000, "input_price": 0.0, "output_price": 0.0,
        "is_free": True, "tier_rank": 1,
        "strength_tags": ["new"], "weakness_tags": [],
        "best_for": [], "performance_score": 75.0,
    }]).fetch()
    result = RegistryUpdater(registry, seed_path).apply_feed(feed)
    assert "x/y" in result.updated
    m = registry.get("x/y")
    assert m is not None
    assert m.display_name == "New Name"
    assert m.context_window == 2000
    assert m.performance_score == 75.0


def test_apply_feed_leaves_unchanged_untouched(
    registry: ModelRegistry, seed_path: Path
) -> None:
    fields = dict(
        model_id="x/y", provider="x", display_name="Same", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=["a"], weakness_tags=[], best_for=[], performance_score=80.0,
    )
    registry.upsert(Model(**fields))
    feed = StaticFeedProvider([fields]).fetch()
    result = RegistryUpdater(registry, seed_path).apply_feed(feed)
    assert "x/y" in result.unchanged
    assert result.added == []
    assert result.updated == []


def test_apply_feed_does_not_remove_missing_by_default(
    registry: ModelRegistry, seed_path: Path
) -> None:
    registry.upsert(Model(
        model_id="kept/old", provider="kept", display_name="Kept", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=[], weakness_tags=[], best_for=[], performance_score=50.0,
    ))
    feed = StaticFeedProvider([{
        "model_id": "new/added", "provider": "new", "display_name": "New",
        "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
        "is_free": True, "tier_rank": 10,
        "strength_tags": [], "weakness_tags": [],
        "best_for": [], "performance_score": 50.0,
    }]).fetch()
    result = RegistryUpdater(registry, seed_path).apply_feed(feed)
    assert result.removed == []
    assert registry.get("kept/old") is not None  # not removed


def test_apply_feed_removes_missing_when_explicit(
    registry: ModelRegistry, seed_path: Path
) -> None:
    registry.upsert(Model(
        model_id="dead/old", provider="dead", display_name="Dead", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=[], weakness_tags=[], best_for=[], performance_score=50.0,
    ))
    feed = StaticFeedProvider([{
        "model_id": "alive/new", "provider": "alive", "display_name": "Alive",
        "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
        "is_free": True, "tier_rank": 1,
        "strength_tags": [], "weakness_tags": [],
        "best_for": [], "performance_score": 50.0,
    }]).fetch()
    result = RegistryUpdater(registry, seed_path, remove_missing=True).apply_feed(feed)
    assert "dead/old" in result.removed
    assert registry.get("dead/old") is None
    assert registry.get("alive/new") is not None


def test_apply_feed_rewrites_seed_with_new_version(
    registry: ModelRegistry, seed_path: Path
) -> None:
    feed = StaticFeedProvider([{
        "model_id": "x/y", "provider": "x", "display_name": "X",
        "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
        "is_free": True, "tier_rank": 1,
        "strength_tags": [], "weakness_tags": [],
        "best_for": [], "performance_score": 50.0,
    }]).fetch()
    seed_path.write_text(json.dumps({"version": "2026-06-09", "models": []}))
    result = RegistryUpdater(registry, seed_path).apply_feed(feed)
    assert result.old_version == "2026-06-09"
    assert result.new_version != "2026-06-09"
    new_data = json.loads(seed_path.read_text())
    assert new_data["version"] == result.new_version
    assert new_data["source"] == "auto-updater"
    assert len(new_data["models"]) == 1


def test_apply_feed_continues_on_bad_entry(
    registry: ModelRegistry, seed_path: Path
) -> None:
    feed = [
        {"model_id": "good/one", "provider": "g", "display_name": "G",
         "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
         "is_free": True, "tier_rank": 1, "strength_tags": [],
         "weakness_tags": [], "best_for": [], "performance_score": 50.0},
        {"model_id": "bad/one"},  # missing fields
        {"model_id": "good/two", "provider": "g", "display_name": "G2",
         "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
         "is_free": True, "tier_rank": 1, "strength_tags": [],
         "weakness_tags": [], "best_for": [], "performance_score": 50.0},
    ]
    result = RegistryUpdater(registry, seed_path).apply_feed(feed)
    assert "good/one" in result.added
    assert "good/two" in result.added
    assert any("bad/one" in e for e in result.errors)
    assert registry.get("good/one") is not None
    assert registry.get("good/two") is not None


def test_apply_feed_changelog_field(
    registry: ModelRegistry, seed_path: Path
) -> None:
    registry.upsert(Model(
        model_id="x/y", provider="x", display_name="X", context_window=1000,
        input_price=0.0, output_price=0.0, is_free=True, tier_rank=1,
        strength_tags=[], weakness_tags=[], best_for=[], performance_score=50.0,
    ))
    feed = [
        {"model_id": "x/y", "provider": "x", "display_name": "X Updated",
         "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
         "is_free": True, "tier_rank": 1, "strength_tags": [],
         "weakness_tags": [], "best_for": [], "performance_score": 90.0},
        {"model_id": "brand/new", "provider": "n", "display_name": "N",
         "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
         "is_free": True, "tier_rank": 1, "strength_tags": [],
         "weakness_tags": [], "best_for": [], "performance_score": 60.0},
    ]
    result = RegistryUpdater(registry, seed_path).apply_feed(feed)
    changes = result.changes
    assert any("updated: x/y" in c for c in changes)
    assert any("added: brand/new" in c for c in changes)


def test_apply_feed_with_real_seed_file(tmp_path: Path) -> None:
    """End-to-end: clone the real seed, mutate it, apply, verify.

    CRITICAL: this test must NEVER mutate registry/models.json in the
    project root. The test applies changes to a copy in tmp_path.
    """
    real_seed = Path(__file__).resolve().parent.parent / "registry" / "models.json"
    test_seed = tmp_path / "models.json"
    test_seed.write_text(real_seed.read_text())

    db = tmp_path / "r.sqlite"
    reg = ModelRegistry(db_path=db, seed_path=tmp_path / "unused.json")
    # Load via separate registry
    reg.upsert_many([Model.from_json(m) for m in json.loads(test_seed.read_text())["models"]])

    # Feed: bump deepseek score, add a new model
    original = json.loads(test_seed.read_text())
    for m in original["models"]:
        if m["model_id"] == "deepseek/deepseek-r1-0528":
            m["performance_score"] = 99.9
    original["models"].append({
        "model_id": "test/newcomer", "provider": "test", "display_name": "Newcomer",
        "context_window": 16000, "input_price": 0.0, "output_price": 0.0,
        "is_free": True, "tier_rank": 7,
        "strength_tags": ["test"], "weakness_tags": [],
        "best_for": ["testing"], "performance_score": 70.0,
        "notes": "[VERIFY]"
    })
    feed = StaticFeedProvider(original["models"]).fetch()

    result = RegistryUpdater(reg, test_seed).apply_feed(feed)
    assert "test/newcomer" in result.added
    assert "deepseek/deepseek-r1-0528" in result.updated
    m = reg.get("deepseek/deepseek-r1-0528")
    assert m is not None and m.performance_score == 99.9
