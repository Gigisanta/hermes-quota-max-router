"""Smoke test for the Gradio dashboard helpers (no UI launched)."""
import pytest

from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager
from dashboard.app import build_state, format_decision, format_quota_table, run_chat, run_updater


@pytest.fixture
def state(tmp_path):
    reg = ModelRegistry(db_path=tmp_path / "r.sqlite", seed_path=tmp_path / "s.json")
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    return {
        "registry": reg,
        "quota": qm,
        "analyzer": __import__("core.task_analyzer", fromlist=["HeuristicTaskAnalyzer"]).HeuristicTaskAnalyzer(),
        "orchestrator": __import__("core.orchestrator", fromlist=["RuleBasedOrchestrator"]).RuleBasedOrchestrator(),
    }


def test_build_state_loads_real_seed(tmp_path) -> None:
    # Use the real seed for a meaningful state
    s = build_state()
    assert s["registry"].count() >= 1
    assert s["quota"].all_snapshots()


def test_format_decision_renders_markdown() -> None:
    from core.orchestrator import RuleBasedOrchestrator
    from core.schemas import TaskAnalysis
    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    orch = RuleBasedOrchestrator()
    analysis = TaskAnalysis(required_tags=["coding_sota"], task_type="code")
    decision = orch.route(analysis, reg, qm)
    md = format_decision(decision)
    assert "**Strategy:**" in md
    assert "**Primary:**" in md
    assert decision.primary_model in md


def test_format_quota_table_includes_all_models() -> None:
    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    md = format_quota_table(qm)
    assert "| Model |" in md
    # At least one free model should appear with a percentage.
    # The 2026-06-10 curated seed is free-only by design; the dashboard
    # shows "paid" only when a paid model exists in the registry.
    assert "%" in md
    # Every model in the registry should appear in the table.
    for m in reg.all():
        if m.is_free and m.daily_quota_tokens:
            assert m.model_id in md or m.model_id.replace("/", "/") in md


def test_run_chat_with_empty_message(state) -> None:
    decision, response, metrics, status = run_chat(state, "")
    assert "Type a request" in decision
    assert response == ""
    assert metrics == ""
    assert status == ""


def test_run_chat_with_real_message(state) -> None:
    """Headless run (no router reachable) should still produce a decision
    and a clear error string for the user."""
    decision, response, metrics, status = run_chat(
        state, "Refactor this Python function and add pytest coverage",
    )
    assert "Strategy" in decision
    # Either a real response (if router is up) or an "unreachable" string.
    assert response  # non-empty
    assert status.startswith("Last call") or "Failed" in status or "unreachable" in response.lower()


def test_run_updater_with_valid_feed(state, tmp_path) -> None:
    feed = tmp_path / "feed.json"
    feed.write_text('{"models": [{"model_id": "new/dash", "provider": "n", '
                    '"display_name": "N", "context_window": 1000, '
                    '"input_price": 0.0, "output_price": 0.0, "is_free": true, '
                    '"tier_rank": 10, "strength_tags": [], "weakness_tags": [], '
                    '"best_for": [], "performance_score": 50.0}]}')
    # Override the seed path the updater writes to (use a temp one)
    from core.auto_updater import RegistryUpdater
    tmp_seed = tmp_path / "models.json"
    tmp_seed.write_text('{"version": "2026-06-09", "models": []}')
    # Run manually so we control the seed path
    updater = RegistryUpdater(state["registry"], tmp_seed)
    feed_models = __import__("core.auto_updater", fromlist=["LocalFeedProvider"]).LocalFeedProvider(feed).fetch()
    result = updater.apply_feed(feed_models)
    assert "new/dash" in result.added
    assert state["registry"].get("new/dash") is not None


def test_run_updater_with_missing_feed(state, tmp_path) -> None:
    md = run_updater(str(tmp_path / "nonexistent.json"))
    assert "not found" in md.lower()
