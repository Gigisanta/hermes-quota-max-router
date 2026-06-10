# Hermes QuotaMax Router

OpenAI-compatible intelligent proxy that **maximizes use of free and generous LLM tiers** while aggressively preserving paid plan quotas.

Built on LiteLLM v2, with a Model Registry, Quota Manager, Orchestrator, Auto-Updater, and a Gradio dashboard.

## Quickstart (3 minutes)

```bash
# 1. Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# (add your API keys to .env)

# 2. Validate
python -m scripts.validate_config

# 3. Start the HTTP server
python -m server.app
# → http://127.0.0.1:8080

# 4. Try it
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Refactor Python and add tests"}]}'
```

## Or: launch the dashboard

```bash
python -m dashboard.app
# → http://127.0.0.1:7860
```

## What It Does

A request enters at `/v1/chat/completions` (OpenAI-compatible). The
RouterEngine:

1. **Analyzes** the user message → tags (coding, vision, long-context, etc.)
2. **Routes** via the Orchestrator → free model with the right specialty
3. **Executes** via LiteLLM → real call to the chosen provider
4. **Consumes** quota on success → QuotaManager in Redis
5. **Logs** the full call to `logs/router.jsonl` (JSONL)

The system is **deterministic** by default (rule-based orchestrator) but
pluggable for LLM-driven routing via the `LLMOrchestrator` class.

See `docs/ARCHITECTURE.md` for the full diagram and module map.
See `docs/RUNBOOK.md` for operator tasks.
See `docs/PROVIDERS.md` for what each provider gives you.

## Components (built across 10 iterations, 140 tests)

| Phase | Module | Purpose |
|---|---|---|
| 0 | `config/config.yaml` | LiteLLM config (7 free + 1 paid) |
| 1 | `core/model_registry.py` | SQLite + JSON seed |
| 2 | `core/quota_manager.py` | Redis budget tracking |
| 3 | `core/task_analyzer.py` + `core/orchestrator.py` | Heuristic + LLM, rule + LLM |
| 4 | `core/auto_updater.py` | Periodic feed → registry merge |
| 5 | `core/moa_engine.py` + `dashboard/app.py` | Parallel fan-out + Gradio UI |
| 6 | `core/router_engine.py` | End-to-end routing + JSONL log |
| 7 | `server/app.py` | FastAPI OpenAI-compat |
| 8 | `core/security.py` | Auth, rate limit, retry, headers |
| 9 | `scripts/operations.py` | Cron-friendly CLI (reset, update, report) |
| 10 | `scripts/validate_config.py` + E2E tests | Startup validation + integration tests |

## Tests

```bash
python -m pytest tests/        # 140 tests, ~5s
python -m pytest tests/ -v     # verbose
```

## Key files

- `spec.md` — single source of truth
- `logs/iteration-log.md` — every iteration's scope, decisions, self-critique
- `prompts/*.md` — all prompts (orchestrator, analyzer, MoA, critic, updater)
- `registry/models.json` — the seed (7 free + 1 paid models, June 2026)
- `config/config.yaml` — LiteLLM proxy config
- `logs/router.jsonl` — append-only call log (one line per request)

## Why?

The user (Gio) runs 5+ agents in parallel on daily basis. Each agent
calls LLMs thousands of times. The paid plans (ChatGPT Pro, etc.) hit
their caps within hours. The free tiers from Chinese providers (DeepSeek,
Qwen, Moonshot, Doubao) are mostly untapped. This router:

- Burns free tier first, always
- Routes to the right specialist (DeepSeek for code, Gemini for vision, etc.)
- Runs MoA fan-out when quality demands it (synthesizer is also free)
- Falls back to paid ONLY when free cannot deliver
- Self-updates the registry every 48h

**Goal**: 5-10x more agent volume at near-zero monthly cost.
