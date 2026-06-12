"""Tests for the MoA Engine (Phase 5).

The async engine is tested with a fake acompletion so no network is hit.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from core.moa_engine import MoAEngine, ModelCall, _call_one
from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager
from core.schemas import TaskAnalysis


@dataclass
class _FakeResp(dict):
    """Fake LiteLLM response: dict-like AND attribute-like.

    The MoA engine does `resp["choices"]` AND `resp.get("usage", {})` in
    different places. Subclassing dict makes both work.
    """

    text: str = ""
    tokens: int = 0

    def __init__(self, text: str = "", tokens: int = 0) -> None:
        super().__init__(
            choices=[{"message": {"content": text}}],
            usage={"total_tokens": tokens},
        )
        self.text = text
        self.tokens = tokens


def _mk_resp(text: str, tokens: int = 0) -> _FakeResp:
    return _FakeResp(text=text, tokens=tokens)


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(
        db_path=tmp_path / "r.sqlite",
        seed_path=tmp_path / "unused.json",
    )


@pytest.fixture
def qm(registry: ModelRegistry) -> QuotaManager:
    store = fakeredis.FakeRedis(decode_responses=True)
    q = QuotaManager(store=store)
    q.sync_from_registry(registry)
    # Pre-seed quotas for fake models
    for mid in ["fake/a", "fake/b", "fake/c"]:
        q._write_full(mid, total=1_000_000, last_reset=None, reset_schedule="")
    return q


@pytest.fixture
def engine(qm: QuotaManager, registry: ModelRegistry) -> MoAEngine:
    # Use a fake synthesizer to avoid any real model call
    return MoAEngine(registry, qm, synthesizer_model="fake/synth", timeout_s=5.0)


def test_call_one_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(*args: Any, **kwargs: Any):
        return _mk_resp("hello from fake", tokens=42)

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    call = asyncio.run(_call_one("fake/a", [{"role": "user", "content": "hi"}], 5.0))
    assert isinstance(call, ModelCall)
    assert call.response == "hello from fake"
    assert call.error is None
    assert call.tokens == 42


def test_call_one_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_acomp(*args: Any, **kwargs: Any):
        await asyncio.sleep(10)
        return None

    monkeypatch.setattr("litellm.acompletion", slow_acomp)
    call = asyncio.run(_call_one("fake/a", [{"role": "user", "content": "hi"}], 0.1))
    assert call.response is None
    assert call.error == "timeout"


def test_call_one_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    async def bad_acomp(*args: Any, **kwargs: Any):
        raise RuntimeError("auth failed")

    monkeypatch.setattr("litellm.acompletion", bad_acomp)
    call = asyncio.run(_call_one("fake/a", [{"role": "user", "content": "hi"}], 5.0))
    assert call.response is None
    assert "auth failed" in (call.error or "")


def test_run_with_all_failures_returns_failure_marker(
    engine: MoAEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def bad_acomp(*args: Any, **kwargs: Any):
        raise RuntimeError("nope")

    monkeypatch.setattr("litellm.acompletion", bad_acomp)
    result = asyncio.run(
        engine.run(
            prompt="anything",
            models=["fake/a", "fake/b"],
            analysis=TaskAnalysis(),
        )
    )
    assert result.success_count == 0
    assert "MoA failed" in result.synthesized
    assert set(result.errors.keys()) == {"fake/a", "fake/b"}


def test_run_synthesizes_when_at_least_one_succeeds(
    engine: MoAEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_acompletion(model: str, *args: Any, **kwargs: Any):
        if model == "fake/synth":
            return _mk_resp("synthesized final answer", tokens=50)
        if "synth" in model:
            return _mk_resp("synth", tokens=10)
        if "a" in model.split("/")[-1]:
            return _mk_resp("answer from A", tokens=100)
        if "b" in model.split("/")[-1]:
            raise RuntimeError("B broken")
        raise ValueError(f"unexpected model {model}")

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    result = asyncio.run(
        engine.run(
            prompt="Q?",
            models=["fake/a", "fake/b"],
            analysis=TaskAnalysis(),
        )
    )
    assert result.success_count == 1
    assert result.per_model == {"fake/a": "answer from A"}
    assert result.synthesized == "synthesized final answer"
    assert result.errors == {"fake/b": "B broken"}
    # Quotas should be consumed for fake/a (100) and synth (50)
    assert engine.quota.remaining("fake/a") == 1_000_000 - 100


def test_run_consumes_quota_for_synthesizer(engine: MoAEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(model: str, *args: Any, **kwargs: Any):
        return _mk_resp(f"resp from {model}", tokens=200)

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    # Pre-set synth quota
    engine.quota._write_full("fake/synth", total=500_000, last_reset=None, reset_schedule="")
    asyncio.run(
        engine.run(
            prompt="Q",
            models=["fake/a"],
            analysis=TaskAnalysis(),
        )
    )
    # 200 consumed from fake/a and 200 from fake/synth
    assert engine.quota.remaining("fake/a") == 1_000_000 - 200
    assert engine.quota.remaining("fake/synth") == 500_000 - 200


def test_run_requires_at_least_one_model(engine: MoAEngine) -> None:
    with pytest.raises(ValueError):
        asyncio.run(engine.run("Q", [], TaskAnalysis()))


def test_run_records_total_duration(engine: MoAEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(*args: Any, **kwargs: Any):
        return _mk_resp("ok", tokens=10)

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    result = asyncio.run(engine.run("Q", ["fake/a"], TaskAnalysis()))
    assert result.total_duration_s >= 0.0
    assert result.total_tokens >= 20  # 10 from fake/a + 10 from synth
    assert result.synthesizer_model == "fake/synth"
