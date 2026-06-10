# Hermes QuotaMax Router — Architecture

## Layer Diagram

```
                       ┌─────────────────────────────────────┐
                       │  OpenAI-compatible HTTP client     │
                       │  (curl, openai-python, hermes, ...) │
                       └────────────────┬────────────────────┘
                                        │ POST /v1/chat/completions
                                        ▼
┌──────────────────────────────────────────────────────────────┐
│ server/app.py  (FastAPI)                                      │
│   ├─ auth (Bearer master key)                                 │
│   ├─ rate limit (token bucket per IP)                         │
│   ├─ security headers middleware                             │
│   └─ Prometheus /v1/router/metrics                            │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ core/router_engine.py  (RouterEngine)                        │
│   ├─ analyze(user_msg)            → TaskAnalysis             │
│   ├─ orchestrator.route(analysis) → RoutingDecision          │
│   ├─ execute(decision):                                        │
│   │    ├─ MoA path    → MoAEngine.run()  (parallel)          │
│   │    ├─ Direct path → quota check → litellm.completion     │
│   │    └─ No model    → [no_model_available]                 │
│   └─ log to logs/router.jsonl                                  │
└────┬──────────────┬──────────────┬─────────────────┬─────────┘
     │              │              │                 │
     ▼              ▼              ▼                 ▼
┌────────┐   ┌────────────┐  ┌──────────┐   ┌──────────────┐
│ task_  │   │orchestrator│  │ moa_     │   │ quota_       │
│analyzer│   │ (rule or   │  │ engine   │   │ manager      │
│(heur/  │   │  LLM)      │  │(asyncio) │   │ (Redis or    │
│ LLM)   │   │            │  │          │   │  fakeredis)  │
└────────┘   └─────┬──────┘  └────┬─────┘   └──────┬───────┘
                    │              │                │
                    ▼              ▼                ▼
              ┌─────────────────────────┐   ┌──────────┐
              │  model_registry         │   │  Redis   │
              │  (SQLite + JSON seed)   │   │  or fk   │
              └────────────┬────────────┘   └──────────┘
                             │
                             ▼
              ┌──────────────────────────┐
              │  registry/models.json    │
              │  (rewritten by updater)  │
              └──────────────────────────┘
                             ▲
                             │ feeds
              ┌──────────────┴───────────────┐
              │  core/auto_updater.py        │
              │  (every 48-72h via cron)     │
              └──────────────────────────────┘
```

## Module Responsibilities

| Module | Purpose | Phase |
|---|---|---|
| `core/schemas.py` | Pydantic models for TaskAnalysis, RoutingDecision | 3 |
| `core/model_registry.py` | SQLite + JSON seed, CRUD on models | 1 |
| `core/quota_manager.py` | Redis (or fakeredis) budget tracking | 2 |
| `core/task_analyzer.py` | Extract semantic requirements from user message | 3 |
| `core/orchestrator.py` | Decide which model to use (rule or LLM) | 3 |
| `core/moa_engine.py` | Fan-out + synthesize across N free models | 5 |
| `core/router_engine.py` | End-to-end routing + execution + logging | 6 |
| `core/auto_updater.py` | Periodic registry refresh from feeds | 4 |
| `core/security.py` | Auth, rate limiting, retry, error classification | 8 |
| `server/app.py` | FastAPI HTTP server (OpenAI-compatible) | 7 |
| `dashboard/app.py` | Gradio UI (chat, registry, updater) | 5 |
| `scripts/operations.py` | CLI: reset, auto-update, usage report | 9 |
| `scripts/validate_config.py` | Startup config validation | 10 |

## Data Flow (one chat completion)

1. **HTTP arrives** at `/v1/chat/completions`.
2. **Auth + rate limit** (`auth_and_rate_limit` dependency).
3. **Security headers** added by middleware.
4. **`RouterEngine.completion(messages, model?)`** is called.
5. **Analyze**: `HeuristicTaskAnalyzer.analyze(user_msg)` → `TaskAnalysis`
   (required_tags, estimated_tokens, needs_tools, needs_multimodal, etc.)
6. **Route**: `RuleBasedOrchestrator.route(analysis)` → `RoutingDecision`
   (chosen_strategy, primary_model, fallback_model, reasoning, confidence)
7. **Execute**:
   - `moa` strategy → `MoAEngine.run()` (parallel fan-out + synthesize)
   - `direct` strategy → pre-flight quota check → `litellm.completion(primary, fallback)`
   - `no_model` → return `[no_model_available]` immediately
8. **Consume** quota on success.
9. **Log** the full call to `logs/router.jsonl` (one line per call).
10. **Response** in OpenAI shape, plus `router_decision` extension.

## Design Principles (recap from spec §3)

1. **Free-First Hierarchy**: free → generous → paid (only when strictly necessary)
2. **Specialization Over Cost**: specialized free beats generic paid
3. **Real-time Budget Awareness**: every call checks `quota_manager.snapshot`
4. **Predictive Burn Control**: pre-flight `should_block()` saves expensive calls
5. **Self-Updating Registry**: cron-driven Auto-Updater Agent
6. **Mixture-of-Agents Native**: parallel fan-out + free synthesizer
7. **Observability & Traceability**: every decision has `reasoning` and `confidence`
8. **Hermes-Native**: OpenAI-compatible HTTP, drop-in replacement
