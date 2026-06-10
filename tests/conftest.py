"""
Test configuration for hermes-quota-max-router.

PURPOSE: hermes/quota-router's tests fail when run as a full suite because the
test host's environment contains:
  - ROUTER_MASTER_KEY     (set by Hermes' .env)
  - *_API_KEY             (set by Hermes' .env for Gemini, DeepSeek, OpenRouter, etc.)
  - REDIS_URL             (set by Hermes; absent locally → quota manager falls back
                           to fakeredis which IS shared per-process in some configs)
  - ROUTER_LIVE / QUOTAMAX_*  (set ad-hoc by dev sessions)

When `build_app()` reads `os.environ.get("ROUTER_MASTER_KEY")` at construction
time, the test fixture that expects "auth disabled" picks up the dev's key, and
the test fails.

The `QUOTA_DB_PATH` env var below lets us point the SQLite registries at a
tmp_path unique to this test session so the discovered.json + curated.json we
ship in `registry/` aren't mutated during the test run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# Vars that must be unset for tests that expect a "clean" environment.
# Tests that NEED a master key set one explicitly via monkeypatch.setenv.
_ENV_VARS_TO_CLEAR = (
    "ROUTER_MASTER_KEY",
    "ROUTER_LIVE",
    "ROUTER_HTTP_PORT",
    "ROUTER_PORT",
    "REDIS_URL",
    "QUOTAMAX_BASE_URL",
    "QUOTAMAX_API_KEY",
    "HERMES_SESSION_KEY",  # don't accidentally pick up a real session
)


# Provider keys are filtered by `core.orchestrator.has_key_for_model` to
# prevent the router from picking models whose key is missing. The tests
# below assume the seeded registry is fully "available" — so we set
# dummy keys for all known providers. These keys are non-functional;
# tests that actually call LiteLLM use their own fixtures.
_PROVIDER_KEYS_TO_FAKE_SET = (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "HUGGINGFACE_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "Z_AI_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_router_env(monkeypatch, tmp_path):
    """Strip router-related env vars before every test and isolate SQLite dbs.

    `monkeypatch.setenv`/`delenv` is automatically reverted at test teardown.
    """
    # 1. Remove any vars that could influence build_app() or the quota manager.
    for var in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)

    # 2. Set dummy provider keys so the registry is fully "available" for
    #    routing tests. These are not used to make real calls.
    for k in _PROVIDER_KEYS_TO_FAKE_SET:
        monkeypatch.setenv(k, "test-fixture-key-not-functional")

    # 3. Point registries at the session-scoped tmp_path so tests don't
    #    mutate the shipped `registry/data/*.sqlite` files.
    monkeypatch.setenv("QUOTA_DB_DIR", str(tmp_path / "router_dbs"))

    yield


@pytest.fixture
def router_env_clean(monkeypatch):
    """Clean env (no master key, no live mode) — the default for auth tests."""
    for var in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def router_env_with_master_key(monkeypatch):
    """Env with a known master key for auth-positive tests."""
    monkeypatch.delenv("ROUTER_MASTER_KEY", raising=False)
    monkeypatch.setenv("ROUTER_MASTER_KEY", "test-master-key-fixture")
    for var in _ENV_VARS_TO_CLEAR:
        if var != "ROUTER_MASTER_KEY":
            monkeypatch.delenv(var, raising=False)
    return monkeypatch
