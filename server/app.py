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

log = logging.getLogger(__name__)


# --- OpenAI-compatible request/response schemas ---

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    temperature: float | None = 1.0
    stream: bool = False
    # Hermes extension: force routing strategy (debug only)
    force_strategy: str | None = Field(default=None, exclude=True)
    # Hermes extension: optional session id for multi-turn context
    session_id: str | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


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


# --- App factory ---

def build_app(
    registry=None,
    quota=None,
    live: bool = False,
    use_layered: bool = True,
) -> FastAPI:
    """Build the FastAPI app.

    If `use_layered=True` (production default), uses LayeredRegistry
    (curated + discovered). If False, uses the plain ModelRegistry
    (tests + dev predictability).

    `live` defaults to False (stubs) to keep tests deterministic even
    when developer shells contain real API keys. Set ROUTER_LIVE=1 or
    pass live=True to force real provider calls.
    """
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
    )

    # Phase 8: rate limiter (60 burst, 1/s refill per client)
    rate_limiter = TokenBucket(capacity=60.0, refill_rate=1.0)

    # Simple in-memory metrics (Prometheus exposition format)
    metrics = {
        "calls_per_model": defaultdict(int),
        "tokens_per_model": defaultdict(int),
        "errors_per_model": defaultdict(int),
        "latency_samples": [],  # list[float]
        "rate_limited_total": 0,
    }

    # Phase 12: per-session context (multi-turn conversations)
    session_manager = SessionManager(history_max_turns=20, max_sessions=1000)

    # Phase 13: cost tracking (USD per call)
    cost_tracker = CostTracker()

    # Phase 15: budget monitor (warn/block thresholds)
    budget_monitor = BudgetMonitor(warn_pct=0.80, block_pct=1.00)

    app = FastAPI(title="Hermes QuotaMax Router", version="0.1.0")
    master_key = os.environ.get("ROUTER_MASTER_KEY", "")

    # Phase 16: in-process quota auto-reset scheduler.
    # Runs `quota.maybe_reset_due()` every ``ROUTER_QUOTA_RESET_INTERVAL_S``
    # seconds (default 1 hour). Idempotent — cheap when nothing is due.
    # Disable with ROUTER_QUOTA_RESET_DISABLED=1.
    import asyncio
    _reset_interval_s = float(os.environ.get("ROUTER_QUOTA_RESET_INTERVAL_S", "3600"))
    _reset_disabled = os.environ.get("ROUTER_QUOTA_RESET_DISABLED", "").strip().lower() in ("1", "true", "yes")

    async def _quota_reset_loop() -> None:
        if _reset_disabled or _reset_interval_s <= 0:
            return
        while True:
            try:
                n = quota.maybe_reset_due()
                if n:
                    log.info("quota_reset_loop: reset %d model(s)", n)
            except Exception as e:  # noqa: BLE001
                log.warning("quota_reset_loop: %s", e)
            await asyncio.sleep(_reset_interval_s)

    @app.on_event("startup")
    async def _start_quota_reset_loop() -> None:
        if _reset_disabled:
            return
        app.state.quota_reset_task = asyncio.create_task(_quota_reset_loop())  # type: ignore[attr-defined]

    @app.on_event("shutdown")
    async def _stop_quota_reset_loop() -> None:
        task = getattr(app.state, "quota_reset_task", None)  # type: ignore[attr-defined]
        if task is not None:
            task.cancel()

    # --- security middleware ---

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # --- dependencies ---

    def auth_and_rate_limit(request: Request) -> str:
        """Combined: auth + rate limit. Returns the client key for logging."""
        # 1. Auth (captured at app build time, not read dynamically per request)
        if master_key:
            auth = request.headers.get("authorization")
            if not auth or not auth.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
            provided = auth.removeprefix("Bearer ").strip()
            if provided != master_key:
                raise HTTPException(status_code=401, detail="Invalid API key")
        # 2. Rate limit (per IP, falls back to "unknown")
        client_ip = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client_ip, cost=1.0):
            metrics["rate_limited_total"] += 1
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return client_ip

    # --- endpoints ---

    @app.post("/v1/chat/completions", response_model=ChatCompletionResponse,
              dependencies=[Depends(auth_and_rate_limit)])
    def chat_completions(req: ChatCompletionRequest) -> ChatCompletionResponse:
        if req.stream:
            raise HTTPException(status_code=400, detail="Streaming not yet supported")
        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")

        started = time.monotonic()
        msgs = [m.model_dump() for m in req.messages]

        # Phase 12: attach to a session if provided
        sess = None
        if req.session_id:
            sess = session_manager.get_or_create(req.session_id)
            for m in req.messages[:-1]:  # everything except the last (current) turn
                sess.append(m.role, m.content)

        def _do_call() -> RouterEngine:  # type: ignore[type-arg]
            # Treat "auto" (or empty/None) as "let the orchestrator decide".
            # The router_engine treats model=None as orchestrator-driven;
            # any other string is passed through to LiteLLM as a specific model.
            chosen_model = req.model if (req.model and req.model != "auto") else None
            return router_engine.completion(messages=msgs, model=chosen_model)

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
            sess.append("user", msgs[-1]["content"])
            sess.append("assistant", result.content,
                        model_used=result.model_used, tokens=result.total_tokens)

        # Metrics
        metrics["calls_per_model"][result.model_used or "(none)"] += 1
        metrics["tokens_per_model"][result.model_used or "(none)"] += result.total_tokens
        if result.error:
            metrics["errors_per_model"][result.model_used or "(none)"] += 1
        metrics["latency_samples"].append(duration)
        if len(metrics["latency_samples"]) > 1000:
            metrics["latency_samples"] = metrics["latency_samples"][-1000:]

        return ChatCompletionResponse(
            id=f"chatcmpl-{int(time.time() * 1000)}",
            created=int(time.time()),
            model=result.model_used or "unknown",
            choices=[ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=result.content),
            )],
            usage=ChatCompletionUsage(
                prompt_tokens=result.input_tokens,
                completion_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
            ),
            router_decision=result.decision.model_dump() if result.decision else None,
            router_error=result.error,
            fallback_used=result.fallback_used,
        )

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
        return {
            "status": "ok",
            "version": "0.1.0",
            "models_count": count,
            "live_mode": live,
            "active_sessions": session_manager.count(),
            "registry": layer_info,
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
        return "\n".join(lines) + "\n"

    return app


# Entry point for `uvicorn server.app:app`
app = build_app(live=False)


def main() -> int:
    import uvicorn
    port = int(os.environ.get("ROUTER_HTTP_PORT", "8080"))
    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
