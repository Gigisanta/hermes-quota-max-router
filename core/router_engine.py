"""Router Engine — Phase 6.

The execution layer. Wraps `litellm.completion` with:
  - Pre-call routing decision (analyzer + orchestrator)
  - Optional override of the `model` arg with the orchestrator's pick
  - Post-call quota consumption based on `usage.total_tokens`
  - Fallback to the orchestrator's `fallback_model` on failure
  - Structured logging per call (JSON line in logs/router.jsonl)

This is what the FastAPI server (Phase 6, next) calls. It can also be
imported directly by any agent that wants zero-friction routing:

    from core.router_engine import RouterEngine
    engine = RouterEngine(registry, quota_manager, analyzer, orchestrator)
    response = engine.completion(messages=[{"role": "user", "content": "..."}])

If you pass `model=...` explicitly, the router will use THAT model and
skip orchestration. If you pass `model=None` (the default), the router
decides for you.

If `live=True`, the engine calls real LiteLLM. If `live=False`, it
returns a deterministic stub response — useful for tests and demos.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_registry import ModelRegistry
from .moa_engine import MoAEngine, run_sync
from .orchestrator import RuleBasedOrchestrator
from .quota_manager import QuotaManager
from .schemas import RoutingDecision, TaskAnalysis
from .task_analyzer import HeuristicTaskAnalyzer

log = logging.getLogger(__name__)

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "router.jsonl"


@dataclass
class RouterCallResult:
    """The full record of one router-engine call."""
    decision: RoutingDecision
    model_used: str
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration_s: float = 0.0
    fallback_used: bool = False
    error: str | None = None
    analysis: TaskAnalysis | None = None

    def to_dict(self) -> dict:
        d = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_strategy": self.decision.chosen_strategy,
            "model_used": self.model_used,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "duration_s": round(self.duration_s, 4),
            "fallback_used": self.fallback_used,
            "preserve_paid_quota": self.decision.preserve_paid_quota,
            "confidence": self.decision.confidence,
            "error": self.error,
        }
        if self.analysis:
            d["task_type"] = self.analysis.task_type
            d["tags"] = self.analysis.required_tags
        return d


def _stub_response(model: str, messages: list[dict]) -> dict:
    """Deterministic stub for `live=False` or no-API-key mode.

    Returns a dict shaped like `litellm.completion()` returns.
    """
    user_msg = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": f"[stub:{model}] You said: {user_msg[:200]}",
            }
        }],
        "usage": {
            "prompt_tokens": max(1, len(user_msg) // 4),
            "completion_tokens": 20,
            "total_tokens": max(1, len(user_msg) // 4) + 20,
        },
    }


class RouterEngine:
    def __init__(
        self,
        registry: ModelRegistry,
        quota_manager: QuotaManager,
        analyzer: HeuristicTaskAnalyzer | None = None,
        orchestrator: RuleBasedOrchestrator | None = None,
        moa_engine: MoAEngine | None = None,
        live: bool = False,
        log_path: Path | None = None,
    ) -> None:
        self.registry = registry
        self.quota = quota_manager
        self.analyzer = analyzer or HeuristicTaskAnalyzer()
        self.orchestrator = orchestrator or RuleBasedOrchestrator()
        self.moa_engine = moa_engine
        self.live = live
        self.log_path = log_path or LOG_PATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # --- main entrypoint ---

    def completion(
        self,
        messages: list[dict],
        model: str | None = None,
        history: list[dict] | None = None,
    ) -> RouterCallResult:
        """Route + execute a chat completion.

        If `model` is None, the orchestrator decides.
        If `model` is provided, that model is used directly (no override).
        """
        started = time.monotonic()
        user_msg = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        analysis = self.analyzer.analyze(user_msg, history)

        if model is None:
            decision = self.orchestrator.route(analysis, self.registry, self.quota)
            chosen = decision.primary_model
            fallback = decision.fallback_model
            strategy = decision.chosen_strategy
        else:
            decision = RoutingDecision(
                chosen_strategy="direct",
                primary_model=model,
                fallback_model=None,
                models_to_use=[model],
                reasoning="Caller-specified model; orchestration skipped.",
                estimated_tokens=analysis.estimated_input_tokens + analysis.estimated_output_tokens,
                quality_expectation=analysis.min_quality,
                preserve_paid_quota=True,
                tags_matched=[],
                confidence=1.0,
            )
            chosen = model
            fallback = None
            strategy = "direct"

        # --- execute ---
        if not chosen:
            rc = RouterCallResult(
                decision=decision,
                model_used="",
                content="[no_model] Orchestrator could not pick a model (all quota exhausted or empty registry).",
                duration_s=time.monotonic() - started,
                error="no_model_available",
                analysis=analysis,
            )
            self._log(rc)
            return rc
        if strategy == "moa" and self.moa_engine is not None:
            return self._execute_moa(decision, analysis, user_msg, started)

        return self._execute_with_fallback(
            decision, analysis, chosen, fallback, messages, started,
        )

    # --- execution paths ---

    def _execute_moa(
        self,
        decision: RoutingDecision,
        analysis: TaskAnalysis,
        user_msg: str,
        started: float,
    ) -> RouterCallResult:
        assert self.moa_engine is not None  # for type checkers
        try:
            moa = run_sync(self.moa_engine, user_msg, decision.models_to_use, analysis)
            rc = RouterCallResult(
                decision=decision,
                model_used="+".join(decision.models_to_use),
                content=moa.synthesized,
                total_tokens=moa.total_tokens,
                duration_s=time.monotonic() - started,
                analysis=analysis,
            )
        except Exception as e:  # noqa: BLE001
            rc = RouterCallResult(
                decision=decision,
                model_used=",".join(decision.models_to_use),
                content="[MoA execution failed: " + str(e)[:200] + "]",
                duration_s=time.monotonic() - started,
                error=str(e),
                analysis=analysis,
            )
        self._log(rc)
        return rc

    def _execute_with_fallback(
        self,
        decision: RoutingDecision,
        analysis: TaskAnalysis,
        primary: str,
        fallback: str | None,
        messages: list[dict],
        started: float,
    ) -> RouterCallResult:
        # Pre-flight quota check
        if self.quota.should_block(primary, decision.estimated_tokens):
            if fallback and not self.quota.should_block(fallback, decision.estimated_tokens):
                primary, fallback, fallback_used = fallback, primary, True
            else:
                rc = RouterCallResult(
                    decision=decision,
                    model_used=primary,
                    content=f"[blocked] Quota exhausted for {primary} and no fallback.",
                    duration_s=time.monotonic() - started,
                    error="quota_exhausted",
                    analysis=analysis,
                )
                self._log(rc)
                return rc
        else:
            fallback_used = False

        # Try primary
        result = self._call_one(primary, messages)
        if result["error"] and fallback and not self.quota.should_block(
            fallback, decision.estimated_tokens,
        ):
            log.warning("Primary %s failed (%s); using fallback %s", primary, result["error"], fallback)
            result = self._call_one(fallback, messages)
            fallback_used = True
            model_used = fallback
        else:
            model_used = primary

        # Consume quota on success
        usage = result["usage"]
        if not result["error"] and usage.get("total_tokens"):
            self.quota.consume(model_used, usage["total_tokens"])

        rc = RouterCallResult(
            decision=decision,
            model_used=model_used,
            content=result["content"],
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            duration_s=time.monotonic() - started,
            fallback_used=fallback_used,
            error=result["error"],
            analysis=analysis,
        )
        self._log(rc)
        return rc

    # --- internals ---

    def _call_one(self, model: str, messages: list[dict]) -> dict:
        """Call LiteLLM (or stub). Returns {content, error, usage}."""
        if not self.live:
            data = _stub_response(model, messages)
            return {
                "content": data["choices"][0]["message"]["content"],
                "error": None,
                "usage": data["usage"],
            }
        try:
            from litellm import completion
            resp = completion(model=model, messages=messages, temperature=0.2)
            return {
                "content": resp["choices"][0]["message"]["content"] or "",
                "error": None,
                "usage": dict(resp.get("usage") or {}),
            }
        except Exception as e:  # noqa: BLE001
            return {"content": "", "error": str(e)[:200], "usage": {}}

    def _log(self, rc: RouterCallResult) -> None:
        line = json.dumps(rc.to_dict(), ensure_ascii=False)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    from core.model_registry import ModelRegistry
    from core.quota_manager import QuotaManager
    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    engine = RouterEngine(reg, qm, live=False)
    r = engine.completion(messages=[{"role": "user", "content": "Refactor this Python function"}])
    print(f"Model:    {r.model_used}")
    print(f"Strategy: {r.decision.chosen_strategy}")
    print(f"Tokens:   {r.total_tokens} (consumed from quota)")
    print(f"Duration: {r.duration_s:.3f}s")
    print(f"Content:  {r.content[:200]}")
