# Contributing to Hermes QuotaMax Router

Thanks for your interest in contributing! The router is a community project
maintained by Giolivo Santarelli ([@Gigisanta](https://github.com/Gigisanta))
with the help of [Hermes Agent](https://hermes-agent.nousresearch.com/docs).

The goal of this guide is to make contributing fast, fair, and fun. Please
read it before opening an issue or PR — it will save you and the maintainers
time.

---

## Ground rules

- **Be respectful.** Follow our [Code of Conduct](./CODE_OF_CONDUCT.md).
- **One thing per PR.** Small, focused PRs are easier to review and merge.
- **Reproduce first.** A bug report without steps and environment is
  un-actionable. A fix without a regression test is un-verifiable.
- **No surprise dependencies.** New runtime deps must be justified in the PR
  description (size, license, maintenance status, why it's the only option).
- **Backwards compatibility.** The router is consumed by `pip install` and the
  Hermes plugin. Breaking changes need a `CHANGELOG.md` entry under
  `[Unreleased] → Changed → BREAKING` and a migration note.

---

## Development setup

You need **Python 3.11+** (3.12 recommended) and **git**.

```bash
git clone https://github.com/Gigisanta/hermes-quota-max-router.git
cd hermes-quota-max-router
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install                # optional but recommended
# Validate that everything works:
pytest tests/ -q                  # or: make test
```

The first test run takes ~30s and exercises the full suite (currently 240
tests). The default `validate_config` script also runs without any external
services (it falls back to `fakeredis`).

---

## Running the router locally

```bash
cp .env.example .env             # edit if you have real API keys
python -m scripts.validate_config  # quick smoke test
python -m server.app               # starts on 127.0.0.1:8080
# In another shell:
curl http://127.0.0.1:8080/v1/router/health
```

The router is **stateless** except for the quota counter in Redis (or
`fakeredis` fallback). To wipe state, delete `.fakesqlite` and restart.

---

## Tests

We test with **pytest** (unit + integration, 240 cases, 90% coverage).

```bash
# Full suite with coverage
pytest tests/ --cov=core --cov=server --cov=dashboard --cov-report=term-missing

# One file
pytest tests/test_orchestrator.py -v

# Stop on first failure
pytest tests/ -x -v

# Run a specific test
pytest tests/test_router_engine.py::test_stream -v
```

### When you add a new feature, add a test

| Area | Test file pattern |
|---|---|
| `core/orchestrator.py` | `tests/test_orchestrator.py` |
| `core/router_engine.py` | `tests/test_router_engine.py` |
| `core/quota_manager.py` | `tests/test_quota_manager.py` |
| `core/model_registry.py` | `tests/test_model_registry.py` |
| `server/app.py` (endpoints) | `tests/test_server*.py` |
| Dashboard helpers | `tests/test_dashboard.py` |

If your change is hard to test (rare — most things are easy), explain in the
PR why.

---

## Code style

We use **ruff** (lint + format) and **mypy** (types, non-strict for now).

```bash
# Lint
ruff check core/ server/ scripts/ dashboard/ tests/
# Format
ruff format core/ server/ scripts/ dashboard/ tests/
# Types (best effort — no_strict_optional = true)
mypy --ignore-missing-imports core/ server/
```

Style rules:
- **Imports**: absolute, sorted by `ruff format` (isort-style).
- **Type hints**: required on all new public functions. `Optional[X]` is OK
  but `X | None` is preferred (PEP 604).
- **Line length**: 110.
- **Strings**: prefer f-strings. No `%` formatting.
- **No `print()` in production code.** Use `logging.getLogger(__name__)`.
- **No `except Exception:` without `# noqa: BLE001` and a justification.**
  Catch specific exceptions: `litellm.exceptions.APIError`,
  `httpx.HTTPError`, `redis.RedisError`, `sqlite3.Error`, `asyncio.TimeoutError`.
- **No `sys.path` hacks.** Use `pyproject.toml` `pythonpath` or relative
  imports.
- **No `type: ignore` without a comment** explaining why it's safe.

---

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/). The
hermes autopilot (and humans) follow this:

```
<type>(<scope>): <subject>

<body wrapped at 72 cols>

<footer with "Closes #NNN" or "BREAKING: ...">
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`.

Examples (real commits from this repo):

```
feat(router): circuit breaker for unhealthy free models
fix(security): require ROUTER_MASTER_KEY in production unless explicit opt-in
docs: add one-command install snippet to README
refactor(server): split build_app() into routers/ + middlewares/ + lifecycle/
test: add 8 regression tests for iter 15 hardening
```

---

## Pull request process

1. **Open a feature branch** off `main`:
   `git checkout -b feat/your-thing`
2. **Make focused commits** with conventional messages.
3. **Run the full test suite** locally and paste the summary in the PR
   description.
4. **Fill the PR template** (`.github/PULL_REQUEST_TEMPLATE.md`).
5. **Self-review your diff** before requesting review.
6. **Wait for CI green.** Lint, types, and tests all run automatically.
7. **Address review feedback** in new commits (don't force-push mid-review
   unless asked — it makes review painful).

A maintainer will review within 5 business days. PRs without tests or with
unrelated formatting changes may be sent back for cleanup.

---

## Adding a new free model provider

The most common kind of contribution. Three paths, in order of preference:

### 1. Auto-discovered (easiest, default)
If the model appears in **OpenRouter** or **Hugging Face** with a free
tier, the auto-updater will pick it up within 48-72h. No code change
needed. Verify in `registry/merged.json`.

### 2. Add to curated seed (recommended for production-critical models)
Edit `config/config.yaml` and add a `model_list` entry. The router will
prefer the curated model when its tier_rank wins. Document the rationale
in `docs/PROVIDERS.md`.

### 3. Add a new provider plugin
If the model is on a provider **not** yet supported (e.g. a new
inference service), add a new fetcher in `core/remote_feeds.py` and
register it in `core/catalogs.py`. See `docs/PROVIDERS.md` for the
provider contract.

---

## Project layout

```
core/         # Pure logic (orchestrator, router, registry, quota, security)
server/       # FastAPI app (entry point: python -m server.app)
dashboard/    # Gradio UI (entry point: python -m dashboard.app)
scripts/      # CLIs (validate_config, healthcheck, install_hermes_plugin, …)
registry/     # Generated + curated model registries (JSON)
prompts/      # LLM-driven routing prompts (Markdown)
config/       # LiteLLM config (YAML)
docs/         # Architecture, providers, runbook, Hermes integration
tests/        # 240 pytest cases, 90% coverage
```

The `core/` package is pure (no I/O at import time). `server/` and
`scripts/` can do I/O. This split is enforced informally — please keep it
when adding new modules.

---

## Communication

- **GitHub Issues** — bugs, feature requests, questions.
- **GitHub Discussions** — design proposals, "how do I…", show-and-tell.
- **PRs** — code contributions.

For security issues, see [SECURITY.md](./SECURITY.md). **Do not file
public issues for security problems.**

---

## License

By contributing, you agree that your contributions will be licensed under
the [MIT License](./LICENSE) that covers this project.
