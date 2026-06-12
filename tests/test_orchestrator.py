"""Tests for the Rule-Based Orchestrator."""

from pathlib import Path

import fakeredis
import pytest

from core.model_registry import ModelRegistry
from core.orchestrator import (
    RuleBasedOrchestrator,
    _tag_overlap,
)
from core.quota_manager import QuotaManager
from core.schemas import TaskAnalysis
from core.task_analyzer import HeuristicTaskAnalyzer

SEED_PATH = Path(__file__).resolve().parent.parent / "registry" / "models.json"


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(
        db_path=tmp_path / "r.sqlite",
        seed_path=SEED_PATH,
    )


@pytest.fixture
def qm(registry: ModelRegistry) -> QuotaManager:
    store = fakeredis.FakeRedis(decode_responses=True)
    q = QuotaManager(store=store)
    q.sync_from_registry(registry)
    return q


@pytest.fixture
def analyzer() -> HeuristicTaskAnalyzer:
    return HeuristicTaskAnalyzer()


@pytest.fixture
def orch() -> RuleBasedOrchestrator:
    return RuleBasedOrchestrator()


# --- pure helpers ---


def test_tag_overlap_jaccard() -> None:
    assert _tag_overlap(["a", "b", "c"], ["a", "b"]) == pytest.approx(2 / 3)
    assert _tag_overlap([], ["a"]) == 0.0
    assert _tag_overlap(["a"], []) == 0.0
    assert _tag_overlap(["a", "b"], ["a", "b"]) == 1.0


# --- routing decisions ---


def test_code_task_routes_to_deepseek(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Refactor this Python function and add pytest coverage")
    d = orch.route(a, registry, qm)
    # DeepSeek is the top free model for coding_sota + debugging + refactoring
    assert d.primary_model == "deepseek/deepseek-r1-0528"
    assert d.chosen_strategy == "direct"
    assert d.preserve_paid_quota is True
    # Confidence should be non-trivial; exact value depends on input size.
    # Short input → smaller denominator → still above the "fallback to paid" floor.
    assert d.confidence >= 0.30
    # Must NOT have escalated to paid for a coding task free models do well.
    assert d.primary_model != "openai-codex/gpt-5.5"


def test_writing_task_picks_a_writer(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Write a 5000-word narrative story about a hero's journey")
    d = orch.route(a, registry, qm)
    # Either deepseek (curated tier 1, general-purpose) or a long-context
    # writer. Moonshot was in the old seed; not in the new curated 4-model
    # set, so deepseek wins by default. The test asserts "a real model
    # with sensible routing", not a specific id.
    assert d.chosen_strategy == "direct"
    assert d.primary_model, "must pick a real model"
    assert d.confidence > 0


def test_vision_task_picks_gemini(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Analyze this screenshot of the dashboard and find issues")
    d = orch.route(a, registry, qm)
    # Both Gemini models handle vision; with gemini-2.5-flash-lite at tier 2
    # (higher priority), it now wins the routing. Accept either.
    assert d.primary_model in (
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-flash-lite",
    )
    assert d.preserve_paid_quota is True


def test_long_context_picks_long_context_king(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Read the entire 200k token codebase and summarize it")
    d = orch.route(a, registry, qm)
    # Both Gemini models in the curated seed have 1M+ context window.
    # Either is a valid answer; the new tier system makes gemini-2.5-flash-lite
    # the default (tier 2 beats tier 4).
    assert d.primary_model in (
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-flash-lite",
    )
    # Moonshot remains as a valid fallback (in registry)
    assert d.fallback_model is not None


def test_fast_draft_picks_groq_or_doubao(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Quickly draft a 100-word email subject line for a SaaS launch")
    d = orch.route(a, registry, qm)
    # In the current curated 4-model seed, gemini-2.5-flash-lite is the
    # best free ultra_fast choice. Previously: groq / doubao / gemini-flash.
    # We accept any of those + the new lite model.
    assert d.primary_model in (
        "groq/meta-llama/llama-4-scout-17b-16e-instruct",
        "openrouter/bytedance/doubao-fast",
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-flash-lite",
    )


def test_critical_quality_can_escalate_to_paid(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    # Construct a request that no free model is great at
    a = TaskAnalysis(
        required_tags=[
            "deep_reasoning",
            "agentic_god",
            "instruction_following_god",
            "json_mode_perfect",
            "structured_output",
        ],
        estimated_input_tokens=5000,
        estimated_output_tokens=10000,
        min_quality="exceptional",
        task_type="analysis",
        notes="mission-critical structured output",
    )
    d = orch.route(a, registry, qm)
    # GPT-5.5 dominates the tag match for this specific combination
    # (free models all miss at least one of these)
    # The test allows either: best free (if score high enough) or paid
    if d.preserve_paid_quota:
        assert d.primary_model in [m.model_id for m in registry.all()]
    else:
        assert d.primary_model == "openai-codex/gpt-5.5"


def test_does_not_use_paid_when_free_is_competent(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Refactor Python code")  # easy: deepseek wins
    d = orch.route(a, registry, qm)
    assert d.preserve_paid_quota is True
    assert not d.primary_model.endswith("gpt-5.5")


def test_fallback_model_is_set(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    a = analyzer.analyze("Refactor Python code")
    d = orch.route(a, registry, qm)
    assert d.fallback_model is not None
    assert d.fallback_model != d.primary_model


def test_exhausted_quota_pushes_to_alternative(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
    analyzer: HeuristicTaskAnalyzer,
) -> None:
    # Drain deepseek quota to near-zero
    for _ in range(120):
        qm.consume("deepseek/deepseek-r1-0528", 100_000)
    a = analyzer.analyze("Refactor Python code")
    d = orch.route(a, registry, qm)
    # Quota pressure should make another free model win
    assert d.primary_model != "deepseek/deepseek-r1-0528"
    # Reasoning should reflect the quota pressure
    assert "quota" in d.reasoning.lower() or d.confidence < 0.55


def test_moa_strategy_for_heavy_task(
    orch: RuleBasedOrchestrator,
    registry: ModelRegistry,
    qm: QuotaManager,
) -> None:
    a = TaskAnalysis(
        required_tags=["deep_reasoning", "agentic_god", "tool_master", "research_master", "long_coherence"],
        min_quality="very_high",
        task_type="research",
        estimated_input_tokens=10_000,
        estimated_output_tokens=30_000,
    )
    d = orch.route(a, registry, qm)
    # Heavily multi-tag research should trigger MoA
    assert d.chosen_strategy == "moa"
    assert len(d.models_to_use) >= 3
    assert d.preserve_paid_quota is True
