"""Tests for the Router Engine (Phase 6).

The engine is tested in `live=False` mode so no network is hit. The
only `litellm` interaction is verified by patching it and asserting
that the right model is called.
"""

import json
from pathlib import Path

import fakeredis
import pytest

from core.moa_engine import MoAEngine
from core.model_registry import ModelRegistry
from core.orchestrator import RuleBasedOrchestrator
from core.quota_manager import QuotaManager
from core.router_engine import RouterCallResult, RouterEngine, _stub_response
from core.task_analyzer import HeuristicTaskAnalyzer


@pytest.fixture
def state(tmp_path: Path) -> dict:
    """Build a state with a single tiny model that has a real quota.

    Using a hand-crafted 1-model registry (not the real 7-model seed)
    makes the test deterministic: we know exactly what quota is available
    and can drain it predictably.
    """
    import json

    seed = tmp_path / "s.json"
    seed.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": "fake/deepseek",
                        "provider": "fake",
                        "display_name": "Fake DS",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 1,
                        "strength_tags": ["coding_sota", "deep_reasoning"],
                        "weakness_tags": [],
                        "best_for": ["coding"],
                        "performance_score": 90.0,
                        "daily_quota_tokens": 100_000,
                        "current_remaining_tokens": 100_000,
                    }
                ],
            }
        )
    )
    reg = ModelRegistry(db_path=tmp_path / "r.sqlite", seed_path=seed)
    qm = QuotaManager(store=fakeredis.FakeRedis(decode_responses=True))
    qm.sync_from_registry(reg)
    return {
        "registry": reg,
        "quota": qm,
        "analyzer": HeuristicTaskAnalyzer(),
        "orchestrator": RuleBasedOrchestrator(),
    }


@pytest.fixture
def engine(state: dict, tmp_path: Path) -> RouterEngine:
    return RouterEngine(
        state["registry"],
        state["quota"],
        state["analyzer"],
        state["orchestrator"],
        live=False,
        log_path=tmp_path / "router.jsonl",
    )


# --- _stub_response ---


def test_stub_response_shape() -> None:
    r = _stub_response("fake/m", [{"role": "user", "content": "hi there"}])
    assert "choices" in r
    assert r["choices"][0]["message"]["content"].startswith("[stub:")
    assert r["usage"]["total_tokens"] > 0


def test_stub_response_empty_user_message() -> None:
    r = _stub_response("fake/m", [])
    assert r["usage"]["total_tokens"] >= 21  # 1 + 20 floor


# --- explicit model path ---


def test_explicit_model_skips_orchestration(engine: RouterEngine) -> None:
    r = engine.completion(
        messages=[{"role": "user", "content": "hello"}],
        model="openai-codex/gpt-5.5",  # forced
    )
    assert r.model_used == "openai-codex/gpt-5.5"
    assert r.decision.chosen_strategy == "direct"
    assert "Caller-specified" in r.decision.reasoning


# --- orchestrated path ---


def test_orchestrated_path_picks_free_model(engine: RouterEngine, state: dict) -> None:
    r = engine.completion(
        messages=[{"role": "user", "content": "Refactor this Python function and add tests"}],
    )
    assert r.decision.chosen_strategy == "direct"
    # The only free model in this fixture is fake/deepseek
    assert r.model_used == "fake/deepseek"
    assert r.total_tokens > 0
    assert r.error is None


def test_quota_is_consumed_on_success(engine: RouterEngine, state: dict) -> None:
    before = state["quota"].remaining("fake/deepseek")
    assert before is not None
    engine.completion(
        messages=[{"role": "user", "content": "Refactor Python code"}],
    )
    after = state["quota"].remaining("fake/deepseek")
    assert after is not None
    assert after < before


def test_quota_not_consumed_on_quota_block(engine: RouterEngine, state: dict) -> None:
    # Drain the quota entirely
    state["quota"].consume("fake/deepseek", 100_000)
    snap_before = state["quota"].snapshot("fake/deepseek")
    assert snap_before.remaining == 0
    r = engine.completion(
        messages=[{"role": "user", "content": "Refactor Python code"}],
    )
    # Quota is exhausted on the only free model → orchestrator returns
    # empty primary → router returns no_model_available.
    assert r.error == "no_model_available"
    snap_after = state["quota"].snapshot("fake/deepseek")
    assert snap_after.remaining == 0  # not negative


def test_logging_writes_jsonl(engine: RouterEngine, tmp_path: Path) -> None:
    engine.completion(messages=[{"role": "user", "content": "hello"}])
    log_file = tmp_path / "router.jsonl"
    assert log_file.exists()
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["model_used"]
    assert "timestamp" in record
    assert "total_tokens" in record


# --- MoA path ---


def test_moa_path_uses_engine_when_decision_is_moa(
    state: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the orchestrator to choose moa, then verify MoAEngine is invoked."""
    from core.schemas import RoutingDecision

    # Bypass orchestrator: build a router that always returns moa
    class _MoAOrchestrator:
        def route(self, analysis, registry, quota):
            return RoutingDecision(
                chosen_strategy="moa",
                primary_model="deepseek/deepseek-r1-0528",
                fallback_model="openrouter/qwen/qwen3-235b-a22b-thinking-2507",
                models_to_use=["deepseek/deepseek-r1-0528", "openrouter/qwen/qwen3-235b-a22b-thinking-2507"],
                reasoning="forced moa",
                estimated_tokens=1000,
                quality_expectation="very_high",
                preserve_paid_quota=True,
                tags_matched=analysis.required_tags,
                confidence=0.9,
            )

    # Fake acompletion for MoA
    async def fake_acomp(model: str, *args, **kwargs):
        class _R(dict):
            pass

        r = _R(choices=[{"message": {"content": f"resp from {model}"}}], usage={"total_tokens": 50})
        return r

    monkeypatch.setattr("litellm.acompletion", fake_acomp)

    # Pre-seed synth quota
    state["quota"]._write_full("gemini/gemini-2.5-flash", total=500_000, last_reset=None, reset_schedule="")

    moa = MoAEngine(
        state["registry"], state["quota"], synthesizer_model="gemini/gemini-2.5-flash", timeout_s=2.0
    )
    engine = RouterEngine(
        state["registry"],
        state["quota"],
        state["analyzer"],
        _MoAOrchestrator(),
        moa_engine=moa,
        live=False,
        log_path=tmp_path / "router.jsonl",
    )
    r = engine.completion(messages=[{"role": "user", "content": "research this deeply"}])
    assert r.decision.chosen_strategy == "moa"
    assert "deepseek" in r.model_used
    assert "resp from" in r.content or "synthesized" in r.content.lower() or "synth" in r.content
    assert r.total_tokens > 0


def test_router_call_result_to_dict_has_required_fields() -> None:
    from core.schemas import RoutingDecision

    rc = RouterCallResult(
        decision=RoutingDecision(chosen_strategy="direct", primary_model="x/y", reasoning="r"),
        model_used="x/y",
        content="hi",
        total_tokens=42,
        duration_s=0.1,
    )
    d = rc.to_dict()
    assert d["model_used"] == "x/y"
    assert d["total_tokens"] == 42
    assert d["decision_strategy"] == "direct"
    assert d["preserve_paid_quota"] is True
    assert "timestamp" in d


def test_log_survives_non_json_native_tool_calls(tmp_path: Path) -> None:
    """Regression: in live mode litellm returns ChatCompletionMessageToolCall
    objects (not dicts) inside tool_calls. json.dumps without default= raised
    TypeError inside _log and the whole request 500'd."""
    from core.model_registry import ModelRegistry
    from core.quota_manager import QuotaManager
    from core.schemas import RoutingDecision

    class FakeToolCall:  # mimics a pydantic object, not JSON-native
        def model_dump(self) -> dict:
            return {"id": "call_1", "type": "function"}

    reg = ModelRegistry(db_path=tmp_path / "reg.sqlite")
    engine = RouterEngine(reg, QuotaManager(), log_path=tmp_path / "calls.jsonl")
    rc = RouterCallResult(
        decision=RoutingDecision(chosen_strategy="direct", primary_model="x/y", reasoning="r"),
        model_used="x/y",
        content="",
        tool_calls=[FakeToolCall()],  # type: ignore[list-item]
    )
    engine._log(rc)  # must not raise
    logged = (tmp_path / "calls.jsonl").read_text()
    assert "tool_calls" in logged
