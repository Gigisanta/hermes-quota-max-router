"""FastAPI server — OpenAI-compatible HTTP interface to the router.

Endpoints:
  POST /v1/chat/completions   — OpenAI-compatible, routes via RouterEngine
  GET  /v1/models             — Lists all models in the registry
  GET  /v1/router/quota       — Current quota snapshot
  GET  /v1/router/health      — Liveness + version
  GET  /v1/router/metrics     — Prometheus-format metrics

Phase 8 hardening:
  - Optional Bearer-token auth via ROUTER_MASTER_KEY env var
  - Per-client token-bucket rate limit (capacity 60, refill 1/s)
  - Exponential backoff on transient LLM errors
  - Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)

Run:
  python server/app.py        # port 8080 by default
  uvicorn server.app:app --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager
from core.cost_tracker import CostTracker, compute_cost_usd
from core.budget import BudgetMonitor
from core.session import SessionManager
from core.task_analyzer import HeuristicTaskAnalyzer
from core.orchestrator import RuleBasedOrchestrator, LLMOrchestrator
from core.moa_engine import MoAEngine
from core.router_engine import RouterEngine
from core.security import (
    TokenBucket,
    get_master_key,
    require_master_key,
    with_retry,
)

# iter 15: extracted modules (god-object refactor)
from server.dependencies import make_auth_and_rate_limit
from server.lifecycle import is_quota_reset_disabled, make_quota_reset_loop
from server.middlewares import make_security_headers_middleware

log = logging.getLogger(__name__)


# --- OpenAI-compatible request/response schemas ---

class ToolCallFunction(BaseModel):
    name: str
    arguments: str  # JSON-encoded string


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


class ChatMessage(BaseModel):
    role: str
    content: str | None = ""
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    temperature: float | None = 1.0
    stream: bool = False
    # OpenAI-compatible function-calling / tool-calling
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    # Hermes extension: force routing strategy (debug only)
    force_strategy: str | None = Field(default=None, exclude=True)
    # Hermes extension: optional session id for multi-turn context
    session_id: str | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionChoiceDelta(BaseModel):
    """For streaming: each chunk carries a delta, not a full message."""
    index: int
    delta: dict
    finish_reason: str | None = None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    # Hermes extensions — non-standard but useful
    router_decision: dict | None = None
    router_error: str | None = None
    fallback_used: bool = False


class ChatCompletionChunk(BaseModel):
    """SSE chunk for streaming responses. Matches OpenAI's delta format."""
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChoiceDelta]


# --- App factory ---

def build_app(
    registry=None,
    quota=None,
    live: bool = False,
    use_layered: bool = True,
    health_probe=None,
) -> FastAPI:
    """Build the FastAPI app.

    If `use_layered=True` (production default), uses LayeredRegistry
    (curated + discovered). If False, uses the plain ModelRegistry
    (tests + dev predictability).

    `live` defaults to False (stubs) to keep tests deterministic even
    when developer shells contain real API keys. Set ROUTER_LIVE=1 or
    pass live=True to force real provider calls.

    `health_probe` defaults to the process-wide singleton from
    `core.health_probe.get_default_probe()`. Pass an explicit instance
    to inject state in tests.

    Security: the server REFUSES TO START if `ROUTER_MASTER_KEY` is unset
    and `ROUTER_ALLOW_INSECURE_NO_AUTH` is not explicitly set to `1`. This
    is the iter 15 fix for the "if master_key: silently disables auth"
    hole. To run a public dev instance, set `ROUTER_ALLOW_INSECURE_NO_AUTH=1`
    in the environment.
    """
    _master_key = os.environ.get("ROUTER_MASTER_KEY", "").strip()
    _allow_insecure = os.environ.get("ROUTER_ALLOW_INSECURE_NO_AUTH", "").strip().lower() in {
        "1", "true", "yes",
    }
    if not _master_key and not _allow_insecure:
        raise RuntimeError(
            "ROUTER_MASTER_KEY is not set. Either set it to a strong secret, "
            "or explicitly opt in to unauthenticated dev mode by setting "
            "ROUTER_ALLOW_INSECURE_NO_AUTH=1. Refusing to start the server "
            "with auth silently disabled."
        )

    env_live = os.environ.get("ROUTER_LIVE", "").strip().lower()
    if env_live in ("1", "true", "yes"):
        live = True
    elif env_live in ("0", "false", "no"):
        live = False
    if registry is None:
        if use_layered:
            from core.layered_registry import LayeredRegistry
            registry = LayeredRegistry.from_defaults()
        else:
            registry = ModelRegistry()
    if quota is None:
        quota = QuotaManager()
        # sync_from_registry works on plain ModelRegistry. For Layered,
        # iterate all and seed.
        if hasattr(registry, "curated"):  # LayeredRegistry
            for m in registry.all():
                if m.daily_quota_tokens and m.daily_quota_tokens > 0:
                    quota._write_full(
                        m.model_id,
                        total=m.daily_quota_tokens,
                        last_reset=m.last_reset,
                        reset_schedule=m.reset_schedule or "",
                    )
        else:
            quota.sync_from_registry(registry)

    # Build the orchestrator + MoA engine based on ROUTER_ORCHESTRATOR_MODE.
    # Modes:
    #   "rule"  → RuleBasedOrchestrator (default, deterministic, no network)
    #   "llm"   → LLMOrchestrator (uses ROUTER_BRAIN_MODEL, default gemini-2.5-flash)
    #   "moa"   → RuleBasedOrchestrator + MoAEngine (fan-out + synthesize)
    # Live calls only happen if the brain model key is present in env; otherwise
    # the engine still routes and falls back to direct execution.
    orchestrator_mode = os.environ.get("ROUTER_ORCHESTRATOR_MODE", "rule").strip().lower()
    brain_model = os.environ.get("ROUTER_BRAIN_MODEL", "gemini/gemini-2.5-flash").strip()
    synth_model = os.environ.get("ROUTER_SYNTH_MODEL", "gemini/gemini-2.5-flash").strip()

    if orchestrator_mode == "llm":
        active_orchestrator: RuleBasedOrchestrator | LLMOrchestrator = LLMOrchestrator(
            model=brain_model,
            live=live,
        )
        active_moa: MoAEngine | None = None
    elif orchestrator_mode == "moa":
        active_orchestrator = RuleBasedOrchestrator()
        active_moa = MoAEngine(registry, quota, synthesizer_model=synth_model)
    else:
        active_orchestrator = RuleBasedOrchestrator()
        active_moa = None

    router_engine = RouterEngine(
        registry, quota,
        HeuristicTaskAnalyzer(), active_orchestrator,
        moa_engine=active_moa,
        live=live,
        health_probe=health_probe,  # iter 15
    )

    # Phase 8: rate limiter. iter 15: configurable via env for tests +
    # ops. Defaults match the original (60 burst, 1/s refill).
    _burst = float(os.environ.get("ROUTER_RATE_LIMIT_BURST", "60") or "60")
    _refill = float(os.environ.get("ROUTER_RATE_LIMIT_REFILL", "1") or "1")
    rate_limiter = TokenBucket(capacity=_burst, refill_rate=_refill)

    # Simple in-memory metrics (Prometheus exposition format)
    # F7-fix: added router_fallback_total so silent primary→fallback swaps
    # are visible. Without this, users think they're on deepseek when they're
    # actually on gemini-flash-lite.
    metrics = {
        "calls_per_model": defaultdict(int),
        "tokens_per_model": defaultdict(int),
        "errors_per_model": defaultdict(int),
        "latency_samples": [],  # list[float]
        "rate_limited_total": 0,
        "fallback_total": 0,
    }

    # Phase 12: per-session context (multi-turn conversations)
    session_manager = SessionManager(history_max_turns=20, max_sessions=1000)

    # Phase 13: cost tracking (USD per call)
    cost_tracker = CostTracker()

    # Phase 15: budget monitor (warn/block thresholds)
    budget_monitor = BudgetMonitor(warn_pct=0.80, block_pct=1.00)

    app = FastAPI(title="Hermes QuotaMax Router", version="0.1.0")
    master_key = os.environ.get("ROUTER_MASTER_KEY", "")

    # iter 15: extracted quota-reset background loop (lifecycle.py)
    _reset_disabled = is_quota_reset_disabled()
    if not _reset_disabled:
        _start_loop = make_quota_reset_loop(quota.maybe_reset_due)
        _quota_reset_task: asyncio.Task[None] | None = None

        @app.on_event("startup")
        async def _start_quota_reset_loop() -> None:
            nonlocal _quota_reset_task
            _quota_reset_task = _start_loop()

        @app.on_event("shutdown")
        async def _stop_quota_reset_loop() -> None:
            if _quota_reset_task is not None:
                _quota_reset_task.cancel()

    # iter 15: extracted security-headers middleware (middlewares.py)
    app.add_middleware(make_security_headers_middleware)  # type: ignore[arg-type]

    # iter 15: extracted auth + rate-limit dependency (dependencies.py)
    def _bump_rate_limited() -> None:
        metrics["rate_limited_total"] += 1

    auth_and_rate_limit = make_auth_and_rate_limit(
        master_key=master_key,
        rate_limiter=rate_limiter,
        on_rate_limited=_bump_rate_limited,
    )

    # --- endpoints ---

    @app.post("/v1/chat/completions",
              dependencies=[Depends(auth_and_rate_limit)])
    def chat_completions(req: ChatCompletionRequest, request: Request):
        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        if req.stream:
            return _stream_chat(req)

        return _blocking_chat(req)

    def _blocking_chat(req: ChatCompletionRequest) -> ChatCompletionResponse:
        started = time.monotonic()
        msgs = [m.model_dump(exclude_none=True) for m in req.messages]

        # Phase 12: attach to a session if provided
        sess = None
        if req.session_id:
            sess = session_manager.get_or_create(req.session_id)
            for m in req.messages[:-1]:  # everything except the last (current) turn
                sess.append(m.role, m.content or "")

        from core.router_engine import RouterEngine, RouterCallResult
        def _do_call() -> RouterCallResult:
            # Treat "auto" (or empty/None) as "let the orchestrator decide".
            chosen_model = req.model if (req.model and req.model != "auto") else None
            return router_engine.completion(
                messages=msgs,
                model=chosen_model,
                tools=req.tools,
                tool_choice=req.tool_choice,
            )

        result = _do_call()
        duration = time.monotonic() - started

        # Phase 13: record cost
        cost_usd = compute_cost_usd(
            registry, result.model_used or "",
            result.input_tokens, result.output_tokens,
        )
        if result.model_used:
            cost_tracker.record(result.model_used, cost_usd)
            # Phase 15: check budget
            budget_monitor.check(quota, result.model_used)

        # Phase 12: record this turn in the session
        if sess is not None:
            sess.append("user", msgs[-1].get("content") or "")
            sess.append("assistant", result.content,
                        model_used=result.model_used, tokens=result.total_tokens)

        # Metrics
        metrics["calls_per_model"][result.model_used or "(none)"] += 1
        metrics["tokens_per_model"][result.model_used or "(none)"] += result.total_tokens
        if result.error:
            metrics["errors_per_model"][result.model_used or "(none)"] += 1
        # F7-fix: track silent primary→fallback swaps explicitly. Operators
        # need to see when their "deepseek-r1" budget is exhausted and
        # requests are being silently rerouted to gemini-flash-lite.
        if getattr(result, "fallback_used", False):
            metrics["fallback_total"] += 1
            _lg = logging.getLogger(__name__)
            _lg.warning(
                "router_fallback: chosen_model=%s actual_model=%s reason=%s",
                (result.decision.models_to_use[0] if getattr(result.decision, "models_to_use", None) else "(unknown)"),
                result.model_used or "(unknown)",
                result.error or "(none)",
            )
        metrics["latency_samples"].append(duration)
        if len(metrics["latency_samples"]) > 1000:
            metrics["latency_samples"] = metrics["latency_samples"][-1000:]

        # Build the response message; include tool_calls if the model returned any.
        message = ChatMessage(role="assistant", content=result.content)
        tool_calls = result.tool_calls or None
        if tool_calls:
            message.tool_calls = [
                ToolCall(
                    id=tc.get("id", f"call_{i}"),
                    type=tc.get("type", "function"),
                    function=ToolCallFunction(
                        name=tc.get("function", {}).get("name", ""),
                        arguments=tc.get("function", {}).get("arguments", ""),
                    ),
                )
                for i, tc in enumerate(tool_calls)
            ]
            if not result.content:
                message.content = None

        return ChatCompletionResponse(
            id=f"chatcmpl-{int(time.time() * 1000)}",
            created=int(time.time()),
            model=result.model_used or "unknown",
            choices=[ChatCompletionChoice(
                index=0,
                message=message,
                finish_reason="tool_calls" if tool_calls else "stop",
            )],
            usage=ChatCompletionUsage(
                prompt_tokens=result.input_tokens,
                completion_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
            ),
            router_decision=result.decision.model_dump() if result.decision else None,
            router_error=result.error,
            fallback_used=result.fallback_used,
        ).model_dump(exclude_none=True)

    def _stream_chat(req: ChatCompletionRequest):
        """SSE stream of ChatCompletionChunk deltas, OpenAI-compatible."""
        from fastapi.responses import StreamingResponse
        msgs = [m.model_dump(exclude_none=True) for m in req.messages]
        chosen_model = req.model if (req.model and req.model != "auto") else None
        chunk_id = f"chatcmpl-{int(time.time() * 1000)}"
        created_ts = int(time.time())

        def _gen():
            try:
                for piece in router_engine.stream(
                    messages=msgs,
                    model=chosen_model,
                    tools=req.tools,
                    tool_choice=req.tool_choice,
                ):
                    # piece is a dict with at least {delta, finish_reason}
                    chunk = ChatCompletionChunk(
                        id=chunk_id,
                        created=created_ts,
                        model=piece.get("model", "unknown"),
                        choices=[ChatCompletionChoiceDelta(
                            index=0,
                            delta=piece.get("delta", {}),
                            finish_reason=piece.get("finish_reason"),
                        )],
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001
                # Surface the error to the client as the final chunk.
                err_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": "unknown",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": f"\n\n[stream error: {exc}]"},
                        "finish_reason": "stop",
                    }],
                }
                yield f"data: {json.dumps(err_chunk)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": m.model_id,
                    "object": "model",
                    "owned_by": m.provider,
                    "context_window": m.context_window,
                    "is_free": m.is_free,
                    "tier_rank": m.tier_rank,
                }
                for m in registry.all()
            ],
        }

    @app.get("/v1/router/quota")
    def router_quota() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "model_id": s.model_id,
                    "total": s.total,
                    "remaining": s.remaining,
                    "pct_remaining": s.pct_remaining,
                    "last_reset": s.last_reset,
                }
                for s in quota.all_snapshots()
            ],
        }

    @app.get("/v1/router/health")
    def router_health() -> dict[str, Any]:
        # registry is either ModelRegistry or LayeredRegistry
        if hasattr(registry, "summary"):
            count = registry.count()
            layer_info = registry.summary()
        else:
            count = registry.count()
            layer_info = {"curated_count": count, "discovered_count": 0,
                          "merged_count": count, "free_count": len(registry.free_first())}
        # iter 15: include per-model health probe state so operators can
        # see which free models are currently being skipped.
        from core.health_probe import HealthState
        probe = router_engine.health_probe
        health_states = probe.all_states()
        unhealthy = [
            {"model_id": mid, "state": h.state.value,
             "consecutive_failures": h.consecutive_failures,
             "cooldown_until": h.cooldown_until}
            for mid, h in health_states.items()
            if h.state in (HealthState.UNHEALTHY, HealthState.HALF_OPEN)
        ]
        return {
            "status": "ok",
            "version": "0.1.0",
            "models_count": count,
            "live_mode": live,
            "active_sessions": session_manager.count(),
            "registry": layer_info,
            "unhealthy_models": unhealthy,
            "health_tracked_models": len(health_states),
        }

    @app.get("/v1/router/cost")
    def router_cost() -> dict[str, Any]:
        """Total USD cost accumulated since process start."""
        snap = cost_tracker.snapshot()
        return {
            "total_usd": snap.total_usd,
            "per_model": snap.per_model,
            "call_count": snap.call_count,
        }

    @app.get("/v1/router/budget")
    def router_budget() -> dict[str, Any]:
        """Per-model burn rates + recent events."""
        return {
            "burn_rates": budget_monitor.burn_rates(quota),
            "events": [
                {
                    "model_id": e.model_id,
                    "level": e.level,
                    "pct_consumed": round(e.pct_consumed, 4),
                    "timestamp": e.timestamp,
                }
                for e in budget_monitor.events[-20:]
            ],
            "thresholds": {
                "warn_pct": budget_monitor.warn_pct,
                "block_pct": budget_monitor.block_pct,
            },
        }

    @app.get("/v1/router/sessions")
    def router_sessions() -> dict[str, Any]:
        """List all active sessions (multi-turn contexts)."""
        return {
            "object": "list",
            "data": session_manager.all_summaries(),
        }

    @app.get("/v1/router/metrics", response_class=PlainTextResponse)
    def router_metrics() -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        for model_id, n in metrics["calls_per_model"].items():
            lines.append(
                f'router_calls_total{{model="{model_id}"}} {n}'
            )
        for model_id, t in metrics["tokens_per_model"].items():
            lines.append(
                f'router_tokens_total{{model="{model_id}"}} {t}'
            )
        for model_id, e in metrics["errors_per_model"].items():
            if e:
                lines.append(
                    f'router_errors_total{{model="{model_id}"}} {e}'
                )
        if metrics["latency_samples"]:
            samples = metrics["latency_samples"]
            avg = sum(samples) / len(samples)
            p50 = sorted(samples)[len(samples) // 2]
            lines.append(f"router_call_duration_seconds_avg {avg:.4f}")
            lines.append(f"router_call_duration_seconds_p50 {p50:.4f}")
        if metrics["rate_limited_total"]:
            lines.append(f"router_rate_limited_total {metrics['rate_limited_total']}")
        if metrics["fallback_total"]:
            lines.append(f"router_fallback_total {metrics['fallback_total']}")
        return "\n".join(lines) + "\n"

    return app


# iter 15: removed module-level `app = build_app(live=False)` because it
# ran at import time and the hardened `build_app()` refuses to start
# without a master key. We use the FastAPI factory pattern instead:
#
#   uvicorn server.app:build_app --factory
#
# For the CLI: `python -m server.app` still works (see `main()` below).
# For tests: callers explicitly invoke `build_app(live=False)` themselves.
# This file is therefore safe to import without side-effects.


def main() -> int:
    import uvicorn
    port = int(os.environ.get("ROUTER_HTTP_PORT", "8080"))
    # iter 15: pass the factory to uvicorn rather than the (now removed)
    # module-level `app` symbol.
    uvicorn.run(
        "server.app:build_app",
        host="127.0.0.1",
        port=port,
        factory=True,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
