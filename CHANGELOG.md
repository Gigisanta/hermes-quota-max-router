# Changelog

All notable changes to Hermes QuotaMax Router. The format is roughly
[Keep a Changelog](https://keepachangelog.com/), grouped by iteration
(iter 0 = initial scaffold, iter 14 = documentation pass).

The router is an OpenAI-compatible proxy that **only routes to
verified 100%-free LLM models** and ships as a model-provider plugin
for Hermes Agent. See `docs/ARCHITECTURE.md` for the system design
and `docs/PROVIDERS.md` for the live registry snapshot.

> **Current state** (post-iter 15):
> - **Tests**: 232 passing
> - **Live registry**: 546 models total, **234 free**, 4 curated + 545 auto-discovered (3 overlap on the merge)
> - **Coverage**: 90%
> - **Hermes plugin**: `scripts/hermes_plugin/quotamax-router/` symlinked into `~/.hermes/plugins/model-providers/`
> - **Sub-agent**: `auxiliary.quotamax_subagent` + `delegation.subagent_models.quotamax` registered in `~/.hermes/config.yaml`
> - **Healthcheck cron**: `quotamax_healthcheck` registered with Hermes cron (every 360m / 6h, last run `ok`)
> - **License**: MIT (added iter 15)
> - **CI**: GitHub Actions on push/PR (added iter 15)
> - **PyPI**: planned v1.2

---

## [0.2.0] — 2026-06-12 — OSS readiness

### Added
- `Dockerfile` (non-root, healthcheck, JSON logs) + `router` service in `docker-compose.yml`.
- `examples/` — 5 runnable examples (curl, httpx, SSE streaming, openai SDK, quota status),
  all verified against a live stub server.
- `Makefile` — `install / test / test-cov / lint / format / type-check / serve` targets.
- `.pre-commit-config.yaml` — ruff + ruff-format + hygiene hooks, pytest smoke on push.
- `requirements-dev.txt` — test/lint deps split out of runtime `requirements.txt`.
- `.github/dependabot.yml` — weekly grouped pip + github-actions updates.
- `CITATION.cff`, `.editorconfig`, `.dockerignore`, README badges.
- `ROUTER_HTTP_HOST` env var (loopback default; `0.0.0.0` inside Docker).

### Changed
- Codebase formatted with `ruff format`; all `ruff check` violations fixed
  (real bugs included: `HealthProbe` and `io` were referenced without imports).
- `HealthState` now derives from `enum.StrEnum`.
- Version is single-sourced from `core.__version__` (health endpoint + FastAPI metadata).
- Tests: 272 passing.

---

## [Unreleased] — iter 15 OSS prep

### Added
- `LICENSE` (MIT) — blocks-for-OSS blocker removed.
- `pyproject.toml` (PEP 621) — enables `pip install -e .` + `quotamax` console script.
- `.github/workflows/tests.yml` — Python 3.11 + 3.12 matrix, pytest with coverage, ruff, mypy.
- `.github/workflows/lint.yml` — ruff (strict) + gitleaks secret scan.
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request,question}.yml` — opinionated templates.
- `.github/PULL_REQUEST_TEMPLATE.md` — checklist.
- `pythonpath = .` in `pytest.ini` — CI can run `pytest tests/` without `PYTHONPATH` env.
- `.gitignore` — full coverage of venv, pycache, .env, build, logs, OMH state.

### Changed
- **CRITICAL security fix** (server/app.py:280): auth gate `if master_key:` no longer silently
  allows unauthenticated access when `ROUTER_MASTER_KEY` is unset. The server now exits at
  startup if no master key is configured and `ROUTER_ALLOW_INSECURE_NO_AUTH` is not explicitly
  set to `1`. Existing deployments are unaffected (they set the env var).
- **Security headers** (server/app.py:270-274): added `Strict-Transport-Security`,
  `Content-Security-Policy`, and `Permissions-Policy`. Existing headers preserved.
- **RouterCallResult.to_dict()** (core/router_engine.py:65-82): now preserves `tool_calls`
  instead of silently dropping them from the `logs/router.jsonl` audit trail.

### Fixed
- **CRITICAL** — `tools`/`tool_choice` no longer lost when calls fail in fallback path
  (root cause: `_call_one` constructed a fresh `completion()` call without re-forwarding them).
- `with_retry` (core/security.py:120) now actually wired into the hot path with exponential
  backoff + jitter on transient LLM errors. Previously it was imported but never called.
- `is_transient_error` (core/security.py:136) no longer matches on string substring; now
  classifies via `litellm.exceptions` + HTTP status code mapping.

### Refactored
- `server/app.py:135-558` `build_app()` god-object split into:
  - `server/app.py` (orchestrator, ~150 LOC)
  - `server/routers/chat.py` (`/v1/chat/completions` + helpers)
  - `server/routers/management.py` (`/v1/models`, `/v1/router/*`)
  - `server/middlewares.py` (security headers + auth/rate-limit dependency)
  - `server/lifecycle.py` (startup/shutdown + quota reset loop)
  - `server/dependencies.py` (BudgetMonitor / CostTracker / SessionManager wiring)

### Tests
- 232 → 240 tests (+8): rate-limit-429, request-timeout, malformed-JSON, very-long-prompt,
  concurrent-requests, MoA-orchestrator-mode-switch, ROUTER_ALLOW_INSECURE_NO_AUTH guard,
  CircuitBreaker open/half-open/close cycle.

---

## Iter 14 (2026-06-11) — Documentation pass
> **Commit:** `4c6eb64 docs(iter14): full rewrite of README, ARCHITECTURE, PROVIDERS, RUNBOOK + CHANGELOG`
> **Tests after iter:** 232 cumulative (no new tests, doc-only)

### Added
- `CHANGELOG.md` (this file).
- `docs/HERMES_INTEGRATION.md`, `docs/ARCHITECTURE.md`, `docs/PROVIDERS.md`,
  `docs/RUNBOOK.md`, `README.md` — all rewritten to match the 14-iter system.

## Iter 13 (2026-06-11) — Streaming + tool-calling in /v1/chat/completions
> **Commit:** `cfbcd65 feat(iter13): streaming + tool-calling in /v1/chat/completions`
> **Tests after iter:** 232 cumulative (+4 in `test_streaming_and_tools`)

### Added
- `RouterEngine.stream()` — OpenAI-style SSE chunks with role/content/tool_calls deltas.
- `_stream_chat()` in `server/app.py` — `StreamingResponse(media_type="text/event-stream")`,
  always emits a final `data: [DONE]\n\n`.
- `ChatCompletionChunk` and `ChatCompletionChoiceDelta` Pydantic schemas.

## Iter 12 (2026-06-11) — Self-test healthcheck + Hermes cron
> **Commit:** `b376c41 feat(iter12): self-test healthcheck script + hermes cron registration`

### Added
- `scripts/healthcheck.py` — manual / one-shot / daemon (default 6h, `ROUTER_HEALTHCHECK_INTERVAL_S`).
- Hermes cron registration: `quotamax_healthcheck` every 360m.

## Iter 11 (2026-06-11) — Functional dashboard chat
> **Commit:** `ea43aff feat(iter11): functional dashboard chat tab with real /v1/chat/completions`

### Changed
- `dashboard/app.py:76-97` — chat tab was hardcoded stub, now hits the real `/v1/chat/completions`.
- Gradio dashboard live-models dropdown wired to `/v1/models`.

## Iter 10 (2026-06-11) — Hermes sub-agent registration
> **Commit:** `5ecd754 feat(iter10): sub-agent registration in Hermes config + install script`

### Added
- `scripts/install_hermes_plugin.py` — idempotent plugin install.
- Registers `auxiliary.quotamax_subagent` and `delegation.subagent_models.quotamax`.

## Iter 9 (2026-06-11) — Real Hermes provider → router → free model E2E
> **Commit:** `40c1322 feat(iter9): real Hermes provider profile -> router -> free model E2E`

### Added
- `scripts/e2e_hermes_provider.py` — verifies a real call path through the Hermes
  `quotamax-router/auto` provider.

## Iter 8 (2026-06-10) — Phase 8 hardening (auth, rate limit, retries, headers)
> **Commit:** phase 8 (pre-history)

### Added
- Optional Bearer-token auth via `ROUTER_MASTER_KEY`.
- Per-client token-bucket rate limit (capacity 60, refill 1/s).
- Exponential backoff on transient LLM errors (now actually wired in iter 15).
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
  (iter 15 added HSTS + CSP + Permissions-Policy).

## Iter 7 (2026-06-10) — FastAPI OpenAI-compatible server
> **Commit:** phase 7 (pre-history)

### Added
- `server/app.py` — 8 endpoints: `/v1/chat/completions`, `/v1/models`,
  `/v1/router/{quota,health,cost,budget,sessions,metrics}`.

## Iter 6 (2026-06-10) — Router engine end-to-end
> **Commit:** phase 6 (pre-history)

### Added
- `core/router_engine.py` — end-to-end routing + JSONL log.

## Iter 5 (2026-06-10) — MoA engine + Gradio dashboard
> **Commit:** phase 5 (pre-history)

### Added
- `core/moa_engine.py` — parallel fan-out + synthesizer.
- `dashboard/app.py` — Gradio UI on port 7860.

## Iter 4 (2026-06-10) — Auto-updater + remote feeds
> **Commit:** phase 4 (pre-history)

### Added
- `core/auto_updater.py` — periodic feed → registry merge.
- `core/remote_feeds.py` — 3 catalogs (OpenRouter, HF-warm, curated-static).

## Iter 3 (2026-06-10) — Task analyzer + orchestrator
> **Commit:** phase 3 (pre-history)

### Added
- `core/task_analyzer.py` (heuristic + LLM).
- `core/orchestrator.py` (`RuleBasedOrchestrator` + `LLMOrchestrator`).

## Iter 2 (2026-06-10) — Quota manager (Redis + fakeredis fallback)
> **Commit:** phase 2 (pre-history)

### Added
- `core/quota_manager.py` — Redis-backed budget tracking with fakeredis in-process fallback.

## Iter 1 (2026-06-10) — Model registry (SQLite + JSON seed)
> **Commit:** phase 1 (pre-history)

### Added
- `core/model_registry.py` — `ModelRegistry`, `RegistryUpdater`, `LayeredRegistry`.

## Iter 0 (2026-06-10) — Initial scaffold (Phase 0-3)
> **Commit:** `7d00a51 init: hermes-quota-max-router scaffold (Phase 0-3)`

### Added
- `config/config.yaml` — LiteLLM config (7 free + 1 paid).
- `core/`, `server/`, `dashboard/`, `scripts/`, `prompts/`, `registry/`, `tests/`, `docs/`.
- 14-iteration scope (LiteLLM proxy → registry → orchestrator → MoA → auto-update →
  dashboard → FastAPI → hardening → Hermes E2E → sub-agent → healthcheck → streaming →
  docs).
