"""Tests for QuotaManager (Phase 2). Uses fakeredis explicitly for isolation."""
import pytest
import fakeredis

from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager, QuotaSnapshot


@pytest.fixture
def fake_store() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def qm(fake_store: fakeredis.FakeRedis) -> QuotaManager:
    return QuotaManager(store=fake_store)


@pytest.fixture
def seeded_qm(qm: QuotaManager, tmp_registry) -> QuotaManager:
    """tmp_registry is from conftest? We just create a tiny one inline."""
    return qm


def test_sync_from_registry_loads_seeded_models(qm: QuotaManager, tmp_path) -> None:
    import json
    from core.model_registry import ModelRegistry, Model
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({
        "models": [{
            "model_id": "x/free", "provider": "x", "display_name": "X Free",
            "context_window": 1000, "input_price": 0.0, "output_price": 0.0,
            "is_free": True, "tier_rank": 1,
            "strength_tags": ["deep_reasoning"], "weakness_tags": [],
            "best_for": ["coding"], "performance_score": 90.0,
            "daily_quota_tokens": 1000, "current_remaining_tokens": 1000,
        }]
    }))
    reg = ModelRegistry(db_path=tmp_path / "r.sqlite", seed_path=seed)
    n = qm.sync_from_registry(reg)
    assert n == 1
    assert qm.remaining("x/free") == 1000


def test_consume_decrements(qm: QuotaManager) -> None:
    # Seed manually
    qm._write_full("m/a", total=1000, last_reset=None, reset_schedule="daily_at_midnight")
    assert qm.remaining("m/a") == 1000
    assert qm.consume("m/a", 300) is True
    assert qm.remaining("m/a") == 700


def test_consume_blocks_when_insufficient(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=100, last_reset=None, reset_schedule="daily_at_midnight")
    assert qm.consume("m/a", 150) is False
    assert qm.remaining("m/a") == 100  # unchanged


def test_consume_unknown_model_blocks(qm: QuotaManager) -> None:
    assert qm.consume("ghost/model", 10) is False


def test_consume_zero_or_negative_is_noop(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=100, last_reset=None, reset_schedule="")
    assert qm.consume("m/a", 0) is True
    assert qm.consume("m/a", -5) is True
    assert qm.remaining("m/a") == 100


def test_paid_model_unlimited(qm: QuotaManager) -> None:
    qm._write_full("paid/gpt-5.5", total=0, last_reset=None, reset_schedule="monthly_subscription")
    # total=0 → unlimited path
    assert qm.consume("paid/gpt-5.5", 1_000_000) is True
    assert qm.should_block("paid/gpt-5.5", 999_999_999) is False


def test_should_block_logic(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=500, last_reset=None, reset_schedule="")
    assert qm.should_block("m/a", 100) is False
    assert qm.should_block("m/a", 500) is False
    assert qm.should_block("m/a", 501) is True


def test_snapshot_pct(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=1000, last_reset=None, reset_schedule="")
    qm.consume("m/a", 250)
    s = qm.snapshot("m/a")
    assert isinstance(s, QuotaSnapshot)
    assert s.remaining == 750
    assert s.pct_remaining == 0.75


def test_reset_restores_full(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=1000, last_reset=None, reset_schedule="")
    qm.consume("m/a", 800)
    assert qm.remaining("m/a") == 200
    qm.reset("m/a")
    assert qm.remaining("m/a") == 1000


def test_reset_all(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=100, last_reset=None, reset_schedule="")
    qm._write_full("m/b", total=200, last_reset=None, reset_schedule="")
    qm.consume("m/a", 50)
    qm.consume("m/b", 100)
    n = qm.reset_all()
    assert n == 2
    assert qm.remaining("m/a") == 100
    assert qm.remaining("m/b") == 200


def test_all_snapshots_lists_every_model(qm: QuotaManager) -> None:
    qm._write_full("m/a", total=100, last_reset=None, reset_schedule="")
    qm._write_full("m/b", total=200, last_reset=None, reset_schedule="")
    snaps = qm.all_snapshots()
    ids = sorted(s.model_id for s in snaps)
    assert ids == ["m/a", "m/b"]
