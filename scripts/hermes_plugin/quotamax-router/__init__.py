"""
QuotaMax Router — Hermes model-provider plugin.

Routes requests through a local Hermes QuotaMax Router (an OpenAI-compatible
proxy) that auto-discovers and prefers 100%-free LLM models (Gemini Flash
Lite, Groq, DeepSeek, OpenRouter :free, etc.). When the user asks for
``quotamax-router/<model>`` or ``quotamax-router/auto``, Hermes hits this
profile, which points at the local router.

Configuration (env vars):
  QUOTAMAX_BASE_URL   — router endpoint (default: http://127.0.0.1:8088/v1)
  QUOTAMAX_API_KEY    — Bearer token (matches ROUTER_MASTER_KEY on the router)

The plugin lives at ``~/.hermes/plugins/model-providers/quotamax-router/`` and
is auto-discovered by ``providers._discover_providers()`` on first import.

After install:
    $ hermes providers list   # should show "quotamax-router"
    $ hermes models           # should list quotamax-router/<model> entries
"""

from __future__ import annotations

import logging
import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


# Static fallback list used when the local router is unreachable.
# Curated on 2026-06-10 from live API verification.
_FALLBACK_FREE_MODELS: list[str] = [
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-flash",
    "openrouter/qwen/qwen3-235b-a22b-thinking-2507",
    "deepseek/deepseek-r1-0528",
]


class QuotaMaxRouterProfile(ProviderProfile):
    """Routes only to verified 100% free LLM models.

    The Hermes transport calls ``fetch_models()`` once to enumerate what the
    router currently advertises. If the router is down, the static fallback
    list keeps the picker usable (the model calls themselves will fail
    gracefully with a clear error).
    """

    def __init__(self) -> None:
        base_url = os.environ.get("QUOTAMAX_BASE_URL", "http://127.0.0.1:8088/v1").strip()
        super().__init__(
            name="quotamax-router",
            api_mode="chat_completions",
            aliases=("quotamax", "qmr", "free-tier", "free_models"),
            display_name="QuotaMax Router (Free-Tier Aggregator)",
            description=(
                "Routes only to verified 100% free LLM models. "
                "Self-discovering, falls back across free providers automatically."
            ),
            signup_url="https://github.com/hermaat/hermes-quota-max-router",
            env_vars=("QUOTAMAX_API_KEY", "QUOTAMAX_BASE_URL"),
            base_url=base_url,
            models_url=f"{base_url.rstrip('/')}/models",
            auth_type="api_key",
            default_aux_model="auto",  # the router picks the best free model
            supports_health_check=True,
            supports_vision=True,  # gemini-flash-lite handles vision
        )
        # Internal cache so we only hit /models once per process.
        self._cached_models: list[str] | None = None

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 5.0,
    ) -> list[str] | None:
        """Fetch live model list from the local QuotaMax Router.

        Calls ``GET {base_url}/models`` (OpenAI-compatible). Returns the
        list of model id strings, or None on failure. Caller is expected
        to fall back to the static catalog.
        """
        if self._cached_models is not None:
            return self._cached_models
        url = self.models_url or (self.base_url.rstrip("/") + "/models")
        try:
            import json
            import urllib.request

            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "hermes-cli/quotamax-router-plugin")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            ids = [m["id"] for m in items if isinstance(m, dict) and "id" in m]
            if ids:
                self._cached_models = ids
                logger.info("quotamax-router: discovered %d models from %s", len(ids), url)
                return ids
        except Exception as exc:
            logger.debug("quotamax-router: live fetch failed (%s); using static fallback", exc)
        # Static fallback so the picker has *something* to show.
        return list(_FALLBACK_FREE_MODELS)

    def build_extra_body(
        self,
        *,
        session_id: str | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        """Pass session_id through so the router can group multi-turn."""
        body: dict[str, Any] = {}
        if session_id:
            body["session_id"] = session_id
        return body


# Register on import (idempotent — last-writer-wins).
register_provider(QuotaMaxRouterProfile())
