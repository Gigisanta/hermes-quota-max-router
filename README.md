# Hermes QuotaMax Router

OpenAI-compatible HTTP proxy that routes every request to a verified free-tier
LLM, falls back across free providers automatically, and only touches paid
quotas when the free pool can't satisfy the task. 546 models in the registry
(234 confirmed free), one FastAPI server, one Gradio dashboard, one
model-provider plugin for Hermes Agent.

## 5-minute quickstart

```bash
git clone <repo-url> hermes-quota-max-router
cd hermes-quota-max-router

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — at minimum set GEMINI_API_KEY=...

python scripts/run_router_live.py
# → http://127.0.0.1:8088  (router)  +  http://127.0.0.1:7860  (dashboard)
```

Sanity check, in another terminal:

```bash
curl -s http://127.0.0.1:8088/v1/router/health
# {"status":"ok","version":"0.1.0","live_mode":true,"models_count":546,...}
```

First chat completion:

```bash
curl -s -X POST http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Reply with just the word OK"}],
    "max_tokens": 5
  }'
```

```json
{
  "id": "chatcmpl-1781235135764",
  "object": "chat.completion",
  "model": "gemini/gemini-2.5-flash",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
               "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 7, "completion_tokens": 23, "total_tokens": 30},
  "router_decision": {"chosen_strategy": "direct", "primary_model": "gemini/gemini-2.5-flash", ...}
}
```

## Live mode

`scripts/run_router_live.py` sets `ROUTER_LIVE=1` and reads `GEMINI_API_KEY`
from the environment (loaded from `.env` by `python-dotenv` at server startup).
Without it the router still answers — every chat completion comes back with a
`[stub: ...]` placeholder so you can smoke-test routing logic without burning
real tokens.

| Env var               | Default                      | Effect                                                     |
|-----------------------|------------------------------|------------------------------------------------------------|
| `ROUTER_LIVE`         | `0`                          | `1` enables real LiteLLM calls; `0` returns stubs          |
| `ROUTER_PORT`         | `8088`                       | HTTP port (matches the Hermes plugin default)              |
| `ROUTER_MASTER_KEY`   | unset                        | If set, requires `Authorization: Bearer <key>` on `/v1/*`  |
| `GEMINI_API_KEY`      | unset                        | Required in live mode for the Gemini model family          |
| `DEEPSEEK_API_KEY`    | unset                        | Optional, enables DeepSeek fallback                        |
| `OPENROUTER_API_KEY`  | unset                        | Optional, broadens the fallback pool                       |
| `GROQ_API_KEY`        | unset                        | Optional, fast Llama inference                             |
| `TOGETHER_API_KEY`    | unset                        | Optional                                                    |
| `FIREWORKS_API_KEY`   | unset                        | Optional                                                    |
| `SILICONFLOW_API_KEY` | unset                        | Optional, Chinese / Qwen / DeepSeek mirrors                |
| `OPENAI_API_KEY`      | unset                        | Paid — touched only when no free model fits                |
| `ANTHROPIC_API_KEY`   | unset                        | Paid                                                        |
| `REDIS_URL`           | `redis://localhost:6379/0`   | Quota state; `fakeredis` is used automatically in tests    |

Launcher exit semantics: with `ROUTER_LIVE=1` and no `GEMINI_API_KEY` the
server still starts, but `/v1/chat/completions` will degrade to stubs once
the orchestrator has no live upstream to call.

## Demo

The running server is real. Hit it with a routing decision you didn't make
yourself — pass `model: "auto"` and watch the response header include the
chosen model plus a `router_decision` block:

```bash
curl -s -X POST http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "You are a terse assistant."},
      {"role": "user",   "content": "What is 2+2?"}
    ]
  }' | python3 -m json.tool
```

## Streaming + tool-calling

The endpoint is OpenAI-compatible — drop in any OpenAI client pointed at
`http://127.0.0.1:8088/v1`.

Streaming (Server-Sent Events):

```bash
curl -N -s -X POST http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "stream": true,
    "messages": [{"role": "user", "content": "List three colors, one per line."}]
  }'
# data: {"id":"chatcmpl-...","object":"chat.completion.chunk", ...}
# data: [DONE]
```

Tool-calling (OpenAI function-calling schema):

```bash
curl -s -X POST http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "What is the weather in Madrid?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto"
  }'
```

When the model decides to call a tool, the response carries
`choices[0].message.tool_calls` in standard OpenAI shape. When a tool result
comes back, append it as a `role: "tool"` message and re-call.

## Endpoints

| Method | Path                       | Purpose                                          |
|--------|----------------------------|--------------------------------------------------|
| GET    | `/v1/models`               | All models in the registry (`is_free` per model) |
| POST   | `/v1/chat/completions`     | OpenAI-compatible chat (supports `stream`, `tools`, `tool_choice`, `session_id`) |
| GET    | `/v1/router/health`        | Liveness, version, live/stub mode, registry stats |
| GET    | `/v1/router/quota`         | Per-model quota snapshots (total, remaining, %)  |
| GET    | `/v1/router/cost`          | Cost tracker summary                             |
| GET    | `/v1/router/budget`        | Budget monitor                                   |
| GET    | `/v1/router/sessions`      | Active multi-turn sessions                       |
| GET    | `/v1/router/metrics`       | Prometheus exposition                            |

Auth is optional: set `ROUTER_MASTER_KEY` and pass
`Authorization: Bearer <key>`. Rate limit is 60 requests / IP, refilling
at 1 req/s.

## Hermes integration

Three steps. Idempotent — safe to re-run.

```bash
# 1. Install the model-provider plugin
python scripts/install_hermes_plugin.py
# symlinks scripts/hermes_plugin/quotamax-router/  →  ~/.hermes/plugins/model-providers/quotamax-router/
# patches ~/.hermes/config.yaml to add auxiliary.quotamax_subagent + delegation.subagent_models.quotamax
# verifies the plugin is discovered and fetch_models() works
```

Confirm the plugin is live:

```bash
hermes plugins list --plain | grep quotamax
# → enabled   user   0.1.0   quotamax-router
```

Confirm the sub-agent is registered:

```bash
hermes config show | grep -A 8 quotamax_subagent
```

The two config keys it adds (also documented in `docs/HERMES_INTEGRATION.md`):

```yaml
auxiliary:
  quotamax_subagent:
    provider: quotamax-router
    model: auto
    base_url: ${QUOTAMAX_BASE_URL:-http://127.0.0.1:8088/v1}
    api_key: ${QUOTAMAX_API_KEY:-}
    api_mode: chat_completions
    timeout: 60
    extra_body: {}

delegation:
  subagent_models:
    quotamax: quotamax-router/auto
```

A 6-hour self-test cron is recommended (the project ships
`scripts/healthcheck.py`):

```bash
# one-shot
python scripts/healthcheck.py --once

# daemon (logs/alerts.jsonl on failure)
python scripts/healthcheck.py --daemon
```

Uninstall reverses everything:

```bash
python scripts/install_hermes_plugin.py --uninstall
```

## Architecture

One process, four layers. HTTP in, LiteLLM out, quota ledger in Redis (or
fakeredis in tests), and a JSONL append-only call log on disk.

```
   ┌───────────────────────────────────────────────────────────────┐
   │  OpenAI-compatible client  (curl, openai-python, Hermes)      │
   └───────────────────────────────┬───────────────────────────────┘
                                   │ POST /v1/chat/completions
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  server/app.py         FastAPI: auth, rate-limit, SSE, schemas│
   └───────────────────────────────┬───────────────────────────────┘
                                   │
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  core/router_engine.py RouterEngine                           │
   │    analyze → task_analyzer (heuristic)                        │
   │    route   → orchestrator (rule or LLM)                       │
   │    execute → litellm.completion | moa_engine (parallel fanout)│
   │    consume→ quota_manager   log → logs/router.jsonl           │
   └────┬──────────────┬──────────────┬────────────────┬──────────┘
        │              │              │                │
        ▼              ▼              ▼                ▼
   model_registry  quota_manager   moa_engine     auto_updater
   (SQLite+JSON)   (Redis)         (asyncio)      (periodic feed merge)
        │
        ▼
   registry/models.json  (546 models, 234 free)
```

## Testing

```bash
python -m pytest tests/ -q
# 232 passed in ~27s
```

Coverage spans unit tests for the registry, orchestrator, quota manager,
MoA engine, and security layer, plus end-to-end tests that hit the FastAPI
server in stub mode.

## Where to go next

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full layer diagram and module map
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — operator tasks (rotate keys, reset quota, force update)
- [`docs/PROVIDERS.md`](docs/PROVIDERS.md) — what each provider gives you, free vs. paid
- [`docs/HERMES_INTEGRATION.md`](docs/HERMES_INTEGRATION.md) — deeper plugin + sub-agent reference
- [`CHANGELOG.md`](CHANGELOG.md) — release notes
