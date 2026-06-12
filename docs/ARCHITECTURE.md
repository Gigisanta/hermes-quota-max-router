# Hermes QuotaMax Router — Architecture

## Overview

QuotaMax Router is an OpenAI-compatible HTTP proxy that picks the best
verified-free LLM for every request, executes the call via LiteLLM, and falls
back across free providers automatically. The system has grown over 14
iterations into four layers: a pure-Python `core/` library, a FastAPI server
exposing `/v1/chat/completions` (blocking + SSE streaming + tool-calling), a
Gradio dashboard that drives the router over HTTP, and a small `scripts/`
toolbox for live operation and a self-test cron. On top of that sits a
`model-provider` plugin and a sub-agent registration that make the router
discoverable from Hermes Agent exactly like any other LLM backend.

## Components

### `core/` — routing library (16 modules)

- **`schemas.py`** — Pydantic models `TaskAnalysis` + `RoutingDecision` (the wire format the rest of `core/` talks in).
- **`model_registry.py`** — SQLite + JSON seed CRUD over `Model` records; the source of truth for the curated tier.
- **`layered_registry.py`** — merges `registry/models.json` (curated) with `registry/discovered.json` (auto); curated wins on conflict.
- **`quota_manager.py`** — per-model daily-token budget; Redis-backed in production, `fakeredis` in dev/tests.
- **`auto_updater.py`** — `FeedProvider` protocol + `LocalFeedProvider` + `RegistryUpdater.apply_feed()`; add/update/remove with version bump and changelog.
- **`remote_feeds.py`** — `RemoteFeedProvider` that hits `core/catalogs.py` endpoints and aggregates OpenRouter + HF + curated into a flat model list.
- **`catalogs.py`** — `CATALOGS` list (OpenRouter public, HuggingFace warm-inference, curated static) with parsers that normalize each into the registry schema.
- **`task_analyzer.py`** — `HeuristicTaskAnalyzer` (deterministic keyword/regex) + `LLMTaskAnalyzer` (LiteLLM-backed); emits a `TaskAnalysis`.
- **`orchestrator.py`** — `RuleBasedOrchestrator` + `LLMOrchestrator`; scores candidates by tag match + perf + quota + provider key, returns a `RoutingDecision`.
- **`moa_engine.py`** — fan-out N free models in parallel via `litellm.acompletion`, then a free synthesizer (`gemini-2.5-flash` by default) merges the answers.
- **`router_engine.py`** — the end-to-end execution layer: analyze → route → execute (direct, fallback, or MoA), consume quota, log JSON line.
- **`cost_tracker.py`** — in-process USD accumulator; per-model totals + call count, surface in `/v1/router/cost`.
- **`budget.py`** — `BudgetMonitor` with `warn_pct` (0.80) and `block_pct` (1.00) thresholds; fires one-shot `BudgetEvent`s, demotes a model in the orchestrator.
- **`session.py`** — `SessionContext` + `SessionManager`; per-`session_id` turn history, last-model memory, quota deltas, LRU eviction at 1000 sessions.
- **`security.py`** — `require_master_key` auth, `TokenBucket` rate limiter (capacity 60, 1/s refill), `with_retry` exponential backoff, transient-error classification.
- **`__init__.py`** — package marker + `__version__`.

### `server/` — HTTP layer (1 module)

- **`server/app.py`** — FastAPI factory `build_app()`: Bearer auth + per-IP rate limit + security headers + in-process quota auto-reset loop. Endpoints: `POST /v1/chat/completions` (blocking + SSE), `GET /v1/models`, `GET /v1/router/health`, `/v1/router/quota`, `/v1/router/cost`, `/v1/router/budget`, `/v1/router/sessions`, `/v1/router/metrics` (Prometheus text).

### `dashboard/` — visual layer (1 module)

- **`dashboard/app.py`** — Gradio 3-tab UI: **Chat** (calls real `/v1/chat/completions` over HTTP, displays the `RoutingDecision` + reply + tokens + cost), **Registry** (live quota table), **Updater** (apply a local feed file via `RegistryUpdater`).

### `scripts/` — ops toolbox

- **`run_router_live.py`** — `uvicorn` launcher that sets `ROUTER_LIVE=1`, `ROUTER_PORT=8088`, and reads `GEMINI_API_KEY` from env; refuses to start without a key in live mode.
- **`e2e_hermes_provider.py`** — Hermes-side end-to-end: forces provider re-discovery, calls `get_provider_profile("quotamax-router")`, fetches the model list, runs a real chat completion; the script proves the full Hermes → router → free model chain.
- **`healthcheck.py`** — self-test: `GET /v1/router/health` + `POST /v1/chat/completions`, verify response is non-stub, append JSONL alert on failure; supports `--once` and `--daemon` (default 6h interval).
- **`install_hermes_plugin.py`** — symlink `scripts/hermes_plugin/quotamax-router` into `~/.hermes/plugins/model-providers/`, patch `~/.hermes/config.yaml` (auxiliary + delegation), back up the config, verify discovery; idempotent, supports `--uninstall`.
- **`operations.py`** — CLI: `reset-quotas`, `auto-update [feed|live|discover]`, `usage-report [LOG]`; `live`/`discover` triggers `RemoteFeedProvider` against OpenRouter + HF.
- **`validate_config.py`** — startup config validation: `config/config.yaml` parses, `registry/models.json` is well-formed + unique `model_id`s, Redis pingable (else loud warning + fakeredis fallback); emits a structured `ValidationReport`.
- **`run_live_server.py`**, **`live_e2e.py`**, **`demo_e2e.py`**, **`demo_quota.py`** — manual smoke / demo entrypoints.

## Request flow

What happens when a `POST /v1/chat/completions` lands (no `stream`, no `tools`):

1. **HTTP arrival** at `server/app.py:chat_completions`.
2. **`auth_and_rate_limit` dependency** — checks `Authorization: Bearer ***` against `ROUTER_MASTER_KEY` (skipped if env var is empty), then takes a token from the per-IP `TokenBucket` (60 burst, 1/s refill). 429 on bucket exhaustion.
3. **Security headers** added by the `add_security_headers` middleware (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`).
4. **Optional session attach** — if the request carries `session_id`, `SessionManager.get_or_create()` returns the existing `SessionContext` (or allocates a new one with LRU eviction at 1000) and replays the prior turn's user message into its history.
5. **`router_engine.completion(messages, model=None)`** is called. The engine never picks a model when the caller already named one.
6. **`analyzer.analyze(user_msg, history)`** — `HeuristicTaskAnalyzer` (or `LLMTaskAnalyzer` if `ROUTER_ORCHESTRATOR_MODE=llm`) returns a `TaskAnalysis` with `required_tags`, `estimated_*_tokens`, `needs_tools`, `needs_long_context`, `task_type`, `min_quality`, `language`.
7. **`orchestrator.route(analysis, registry, quota)`** — `RuleBasedOrchestrator` filters the merged registry for free models with a working provider key, scores each by `_match_strength` (tag coverage with a 0.4 floor for any non-zero match) + `performance_score/100` + a tag-specific bonus, vetoes any whose pre-flight `quota.should_block()` is True, and returns a `RoutingDecision(primary_model, fallback_model, chosen_strategy, confidence, reasoning, ...)`.
8. **Strategy branch** in `RouterEngine.completion`:
   - `direct` (default) → `_execute_with_fallback(decision, primary, fallback, ...)`.
   - `moa` → `_execute_moa(decision, ...)` calls `MoAEngine.run()` (parallel fan-out across `decision.models_to_use`, synthesize with the free `synth_model`).
   - `fallback` → same path as `direct`, but with a stronger preference for the fallback when the primary is blocked.
   - Empty registry / every model blocked → `RouterCallResult(content="[no_model_available]", error="no_model_available")` and the call is logged.
9. **Pre-flight quota check** in `_execute_with_fallback` — if `quota.should_block(primary, estimated_tokens)` is True and the fallback is also blocked, return `[blocked] Quota exhausted for ...` with `error="quota_exhausted"`.
10. **LiteLLM call** — `litellm.completion(model=primary, messages=..., temperature=0.2)`. If the call raises, swap to the fallback (if it's not blocked) and retry. `with_retry` adds exponential backoff on transient errors (`is_transient_error`).
11. **Quota consume** — on success, `quota.consume(model_used, usage.total_tokens)` decrements the daily budget.
12. **Cost + budget + session update** — `compute_cost_usd(registry, model_used, in, out)` → `cost_tracker.record()`; `budget_monitor.check()` fires a `warn` or `block` event at 80% / 100% consumed; `SessionContext.append("user", ...)` + `append("assistant", ..., model_used, tokens)` keeps the multi-turn state coherent.
13. **Metrics** — increment `calls_per_model`, `tokens_per_model`, `errors_per_model`, append to `latency_samples` (capped at 1000).
14. **Response** — `ChatCompletionResponse` in OpenAI shape (`id`, `choices[0].message`, `usage`, `finish_reason`) plus Hermes extensions `router_decision`, `router_error`, `fallback_used`.
15. **Log** — `_log()` appends one JSON line per call to `logs/router.jsonl` (timestamp, strategy, model, tokens, duration, fallback_used, confidence, error).

## Streaming flow

What happens when `stream=true` on `POST /v1/chat/completions`:

1. The same `auth_and_rate_limit` → `add_security_headers` chain runs.
2. `_stream_chat(req)` returns a `StreamingResponse(media_type="text/event-stream")`. A `chunk_id = f"chatcmpl-{ms}"`
   and `created_ts = int(time.time())` are minted once and reused for every chunk in this response.
3. **Routing first** — `router_engine.stream()` runs the same `analyzer.analyze(user_msg)` + orchestrator route, so `model="auto"` (or empty/None) still gets resolved to a concrete model *before* we start emitting chunks. A `model="unknown"` + `[no_model_available]` final chunk is emitted if every model is blocked, and the stream ends.
4. **Live stream** — `litellm.completion(..., stream=True)` is called with the chosen model. For each `ModelResponseStream` piece the engine pulls `piece.choices[0].delta`, copying `role` and `content` (and skipping empty deltas some providers emit) into a `{"model": chosen, "delta": {...}, "finish_reason": ...}` dict.
5. **Stub stream** — if `live=False`, the full `_stub_response` content is yielded in a single delta + `finish_reason: "stop"`, then the generator returns.
6. **SSE framing** — the server wraps each yielded dict as `ChatCompletionChunk`, serializes to JSON, and emits `data: {json}\n\n`. After the generator returns, `data: [DONE]\n\n` is appended.
7. **Error handling** — any exception inside the generator is caught at the `_gen()` boundary; an `err_chunk` with `delta.content = "\n\n[stream error: {exc}]"` and `finish_reason: "stop"` is emitted, then `data: [DONE]\n\n` is appended so clients always get a clean termination.
8. **Tool calls in streams** — `litellm` may emit a delta with `tool_calls` instead of (or alongside) `content`. Those deltas are passed through the same `ChatCompletionChunk` shape; the OpenAI client reassembles them at the end. (Final tool-call assembly is the client's responsibility, matching the OpenAI contract.)

## Tool-calling flow

When the routed model emits one or more `tool_calls` in its response:

1. The caller includes `tools: [...]` (and optionally `tool_choice: "auto" | "required" | {"type": "function", ...}`) in the request body. The server's `ChatCompletionRequest` accepts both, and `router_engine.completion()` (or `stream()`) forwards them verbatim to LiteLLM.
2. **Routing** is unchanged — the orchestrator picks a model that's strong on `tool_master` / `parallel_tool_use` tags (Qwen3-235B-Thinking is the curated king; discovered OpenRouter models can also win).
3. **Model call** — `litellm.completion(model=chosen, messages=..., tools=tools, tool_choice=tool_choice)`. The response message may have `content=None` and a populated `tool_calls` array; `_call_one()` extracts both and returns `{"content": ..., "tool_calls": ..., "usage": ...}`.
4. **Quota consume** runs on the same `usage.total_tokens` regardless of whether the response is text or tool calls.
5. **Response shaping** in `_blocking_chat`:
   - `message.content` is set to the model's text (or `None` if there's only tool calls).
   - Each raw `tool_call` dict is mapped to `ToolCall(id=..., type="function", function=ToolCallFunction(name=..., arguments=...))`. The `arguments` field is a JSON-encoded string, matching OpenAI.
   - `finish_reason` becomes `"tool_calls"` when at least one is present, else `"stop"`.
6. **Client round-trip** — the caller executes the tool(s) and posts a follow-up `chat.completions` request with the same `messages` array, appending one `ChatMessage(role="assistant", tool_calls=[...])` (the prior turn) and one `ChatMessage(role="tool", tool_call_id=..., content=...)` per executed tool. The router will see them as a regular multi-turn call and re-route (the session context, if provided, keeps the same `session_id` so the prior `last_model` is preserved as a hint, not a constraint).
7. **Streaming + tool calls** work the same way: LiteLLM yields deltas whose `tool_calls` field is a partial list (one tool per chunk, accumulating). The server passes each delta through unchanged in `ChatCompletionChunk.choices[0].delta`; the OpenAI client reassembles the final `tool_calls` array on `finish_reason="tool_calls"`.

## Free-tier pipeline

How a model ends up in the merged registry (`/v1/models` = curated + discovered):

1. **Curated seed** — `registry/models.json` ships with 4 hand-picked, manually-verified models (tier 1–4, all `is_free: true`). `ModelRegistry(db_path=..., seed_path=registry/models.json)` loads them on first init into `data/registry.sqlite`. This is the source of truth the spec ranks against.
2. **Discovered layer** — `registry/discovered.json` is regenerated by the `Auto-Updater` from remote catalogs. `RemoteFeedProvider.fetch_all()` walks `core/catalogs.CATALOGS` in order:
   - **OpenRouter public** (`https://openrouter.ai/api/v1/models`) — `_parse_openrouter` walks `data[]`, reads `pricing.prompt` and `pricing.completion` (strings), marks `is_free = prompt == 0 and completion == 0`, infers `context_length` from `context_length` / `top_provider.context_length` / `architecture.context_length`, attaches `vision_master`/`multimodal` if `input_modalities` contains `image` or `file`. All OpenRouter entries start at `tier_rank: 10`.
   - **HuggingFace warm-inference** (`https://huggingface.co/api/models?inference=warm&filter=text-generation&limit=200`) — `_parse_huggingface` caps at 200, sets `is_free` only when the model is not private, not gated, and has an `inference_provider`. Mapped HF tags become `coding_sota` / `vision_master` / `long_context_king` / `instruction_following_god` etc. All HF entries start at `tier_rank: 20`.
   - **Curated static fallback** — `_parse_static_curated` re-reads `registry/models.json` so a registry always has at least the 4 hand-picked entries even with zero network.
3. **Network degradation** — `RemoteFeedProvider` catches `httpx.HTTPError` per catalog and continues; if OpenRouter is down, you still get HF + curated; if both network sources fail, you still get curated.
4. **Merge** — `RegistryUpdater.apply_feed(feed_models)` (with `remove_missing=False` by default) diffs the feed against the current `ModelRegistry`:
   - new `model_id` → ADD, written back to the JSON seed and the SQLite db;
   - existing `model_id` → UPDATE (only fields that differ);
   - in registry but absent from the feed → kept (no accidental wipe);
   - version bumped via `_bump_version()` to a `YYYY-MM-DD` or `YYYY-MM-DD-revN` stamp.
5. **Layered merge at runtime** — `LayeredRegistry.from_defaults()` opens two `ModelRegistry` instances: `registry_curated.sqlite` (seeded from `models.json`) and `registry_discovered.sqlite` (seeded from `discovered.json`). Lookups go curated-first, then discovered fill in the gaps. The FastAPI server uses `LayeredRegistry` by default; `use_layered=False` is for tests + dev predictability.
6. **Live fetch at boot** — `server/app.py:build_app()` then calls `quota.sync_from_registry(registry)` (or iterates `LayeredRegistry.all()` to seed each model's `daily_quota_tokens` into `quota_manager`'s `quota:{model_id}` hash). For the discovered layer, `daily_quota_tokens` defaults to 10M (OpenRouter) or 1M (HF) — these are best-effort estimates, refined by future benchmark passes.
7. **Cron-driven refresh** — the upstream feeds change constantly. Run `python -m scripts.operations auto-update live` (or call `RemoteFeedProvider` directly) on whatever cadence the deployment prefers; the resulting JSON overwrites `discovered.json` and the SQLite db. The next request will see the new models.

## Hermes integration

QuotaMax Router is exposed to Hermes Agent as both a **model-provider plugin**
(discovered automatically on Hermes startup) and a **sub-agent registration**
(configured once in `~/.hermes/config.yaml`). With both in place, any
Hermes sub-agent can be told `provider="quotamax-router/auto"` (or use the
`quotamax` delegation role) and the router transparently picks a free model.
The self-test cron `quotamax_healthcheck` (registered via Hermes cron) runs
every 6 hours and asserts the router is up *and* serving non-stub replies,
appending to `logs/alerts.jsonl` on failure. See `docs/HERMES_INTEGRATION.md`
for the full step-by-step; the two discovery paths are:

```
# 1. Plugin discovery (auto on Hermes startup)
~/.hermes/plugins/model-providers/quotamax-router/   (symlink target)
        │
        │   installed by: python scripts/install_hermes_plugin.py
        ▼
scripts/hermes_plugin/quotamax-router/
   ├── plugin.yaml    (kind: model-provider, name: quotamax-router)
   └── __init__.py    (QuotaMaxRouterProfile with live fetch_models())
        │
        │   imported by: providers._discover_providers()
        ▼
hermes.providers._REGISTRY["quotamax-router"]
        │
        │   referenced by: hermes chat --provider quotamax-router
        ▼
http://127.0.0.1:8088/v1   (the running router)
```

```
# 2. Sub-agent registration (one-time config edit)
~/.hermes/config.yaml
   ├── auxiliary:
   │     quotamax_subagent:
   │       provider: quotamax-router
   │       model: auto
   │       base_url: ${QUOTAMAX_BASE_URL:-http://127.0.0.1:8088/v1}
   │       api_key: ${QUOTAMAX_API_KEY:-}
   │       api_mode: chat_completions
   │       timeout: 60
   └── delegation:
         subagent_models:
           quotamax: quotamax-router/auto
        │
        │   consumed by: delegate_task(..., provider="quotamax-router/auto")
        │                or:   hermes chat --provider quotamax-router --model auto
        ▼
http://127.0.0.1:8088/v1/chat/completions
```

## Failure modes

What the system does when an upstream is unhealthy or unreachable:

- **Primary model returns an error** (5xx, timeout, 429 from a rate-limit
  window) — `RouterEngine._execute_with_fallback` catches the exception,
  swaps to the `fallback_model` from the `RoutingDecision` (if it's not
  blocked), and retries. The final response carries `fallback_used: true`
  and `error: <primary error>` only if the fallback also fails.
- **Both primary and fallback fail** — `RouterCallResult(error=str(e))` is
  returned; the blocking endpoint returns it in `router_error`; the
  streaming endpoint emits a final chunk with the error text + `finish_reason: "stop"`
  + `data: [DONE]` so the client always sees a clean termination.
- **Quota exhausted for the chosen model** — pre-flight `quota.should_block(primary, est_tokens)` returns True; the engine either swaps to the fallback (if its quota is healthy) or returns `[blocked] Quota exhausted for ...` with `error="quota_exhausted"`. The orchestrator will demote that model on the next call because `BudgetMonitor` will have fired a `block` event at 100% consumed.
- **All models blocked** — every model's daily quota is exhausted; the orchestrator returns a `RoutingDecision(primary_model="")` and the engine returns `[no_model_available]`. The cron can call `python -m scripts.operations reset-quotas` to refill; in-process, `quota_manager.maybe_reset_due()` runs on a 1h loop and resets any model whose `reset_schedule` is due.
- **OpenRouter down, HuggingFace up** — `RemoteFeedProvider.fetch_all()` catches the per-catalog `httpx.HTTPError`, logs a warning, and continues; the discovered layer is populated with HF + curated only. The `healthcheck` cron will see fewer models but the router still serves.
- **Both OpenRouter and HuggingFace down** — `_parse_static_curated` is the last catalog entry, so the discovered layer is empty *but the curated 4 models still serve* (they live in `ModelRegistry` directly, not in the discovered layer). The `auto-update live` cron will retry on the next cycle.
- **No upstream API keys in env** — `has_key_for_model()` filters out any provider without a key in the env mapping (`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `MOONSHOT_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `HUGGINGFACE_API_KEY`, `XAI_API_KEY`, `MISTRAL_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`). The orchestrator can still pick a model whose provider has a key, but if *no* keys are set it returns `[no_model_available]`. The `healthcheck` cron catches this case (exit code 2: stub mode) and alerts.
- **Router process crash** — the self-test cron (every 6h, or on demand with `--once`) hits `GET /v1/router/health`; on non-200 it appends to `logs/alerts.jsonl`. Restart with `python scripts/run_router_live.py`. The dashboard is independent; if the router is down the Chat tab shows "Router unreachable".
- **Session store overflow (>1000 active sessions)** — `SessionManager.get_or_create` evicts the oldest session by `created_at` (LRU-ish). New `session_id`s get a fresh `SessionContext`; old ones lose their history. No data loss for the in-flight call.
- **Tier-ranked model goes paid** — the next auto-update sets `is_free=false` on the discovered entry. The orchestrator's `is_free` filter skips it for `preserve_paid_quota=True` decisions; the model stays in the registry as a paid fallback for when free quality is insufficient.
