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
from typing import Any, Iterator

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
    # When the model decides to call a tool, OpenAI-style tool_calls appear
    # here. Each entry is a dict like:
    #   {"id": "call_xxx", "type": "function",
    #    "function": {"name": "...", "arguments": "{...json...}"}}
    tool_calls: list[dict] | None = None

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
        # iter 15 fix: include `tool_calls` (was silently dropped, see
        # the iter 15 changelog). JSON-safe serialization: the tool_calls
        # payload comes from litellm and may contain non-JSON-native types;
        # the default str() in json.dumps is OK for the audit trail.
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
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
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> RouterCallResult:
        """Route + execute a chat completion.

        If `model` is None, the orchestrator decides.
        If `model` is provided, that model is used directly (no override).

        `tools` and `tool_choice` are passed straight through to LiteLLM
        (OpenAI-compatible function-calling).
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
            tools=tools, tool_choice=tool_choice,
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
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
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
        result = self._call_one(primary, messages, tools=tools, tool_choice=tool_choice)
        if result["error"] and fallback and not self.quota.should_block(
            fallback, decision.estimated_tokens,
        ):
            log.warning("Primary %s failed (%s); using fallback %s", primary, result["error"], fallback)
            result = self._call_one(fallback, messages, tools=tools, tool_choice=tool_choice)
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
            tool_calls=result.get("tool_calls"),
        )
        self._log(rc)
        return rc

    # --- internals ---

    def _call_one(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict:
        """Call LiteLLM (or stub). Returns {content, error, usage, tool_calls}."""
        if not self.live:
            data = _stub_response(model, messages)
            return {
                "content": data["choices"][0]["message"]["content"],
                "error": None,
                "usage": data["usage"],
                "tool_calls": None,
            }
        try:
            from litellm import completion
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": 0.2,
            }
            if tools:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            # iter 15: actually wire `with_retry` into the hot path.
            # Previously the function existed in core.security but was
            # imported (server/app.py:51) yet never called; transient
            # errors cascaded straight to fallback. Now we attempt up to
            # 3 calls with exponential backoff before giving up.
            from .security import with_retry as _with_retry

            def _do() -> dict[str, Any]:
                resp = completion(**kwargs)
                message = resp["choices"][0]["message"] or {}
                tool_calls = message.get("tool_calls") or None
                return {
                    "content": (message.get("content") or ""),
                    "error": None,
                    "usage": dict(resp.get("usage") or {}),
                    "tool_calls": tool_calls,
                }

            try:
                return _with_retry(_do, max_attempts=3, base_delay_s=0.3, max_delay_s=4.0)
            except Exception as e:  # noqa: BLE001
                return {
                    "content": "",
                    "error": str(e)[:200],
                    "usage": {},
                    "tool_calls": None,
                }
        except ImportError:
            # litellm not installed (e.g. in minimal CI); surface as
            # transient so the fallback path takes over.
            return {
                "content": "",
                "error": "litellm not installed",
                "usage": {},
                "tool_calls": None,
            }

    # --- streaming ---

    def stream(
        self,
        messages: list[dict],
        model: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> Iterator[dict]:
        """Yield OpenAI-style SSE delta chunks.

        Each yielded dict is shaped like:
          {"model": "...",
           "delta": {"role": "assistant", "content": "..."},
           "finish_reason": "stop" | None}

        Routes the same way `completion()` does (orchestrator picks the
        model when `model` is None or "auto") so the streaming path
        doesn't bypass routing decisions. In stub mode, yields the full
        content as a single delta. In live mode, wraps
        `litellm.completion(stream=True)`.
        """
        # Route first so "auto" gets resolved to a concrete model.
        started = time.monotonic()
        user_msg = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        analysis = self.analyzer.analyze(user_msg)
        if model and model != "auto":
            chosen = model
        else:
            decision = self.orchestrator.route(analysis, self.registry, self.quota)
            chosen = decision.primary_model
            if not chosen:
                yield {
                    "model": "unknown",
                    "delta": {"role": "assistant", "content": "[no_model_available]"},
                    "finish_reason": "stop",
                }
                return

        if not self.live:
            data = _stub_response(chosen, messages)
            content = data["choices"][0]["message"]["content"]
            yield {
                "model": chosen,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
            return

        try:
            from litellm import completion
            kwargs: dict[str, Any] = {
                "model": chosen,
                "messages": messages,
                "temperature": 0.2,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            for piece in completion(**kwargs):
                # piece is a ModelResponseStream; .choices[0].delta has the chunk
                try:
                    delta = piece.choices[0].delta
                    delta_dict: dict[str, Any] = {}
                    if getattr(delta, "role", None):
                        delta_dict["role"] = delta.role
                    content_piece = getattr(delta, "content", None)
                    if content_piece:
                        delta_dict["content"] = content_piece
                    if not delta_dict and not getattr(delta, "finish_reason", None):
                        # Skip empty deltas (some providers emit them)
                        continue
                    yield {
                        "model": chosen,
                        "delta": delta_dict,
                        "finish_reason": getattr(delta, "finish_reason", None),
                    }
                except (AttributeError, IndexError):
                    # Unexpected shape; skip the piece.
                    continue
        except Exception as exc:  # noqa: BLE001
            yield {
                "model": chosen,
                "delta": {"role": "assistant", "content": f"[stream error: {exc}]"},
                "finish_reason": "stop",
            }

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
