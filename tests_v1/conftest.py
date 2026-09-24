from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
import pytest

from free_router.quota import QuotaStore


@pytest.fixture
def quota() -> QuotaStore:
    return QuotaStore(fakeredis.FakeRedis(decode_responses=True), require_durable=False)


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    now = datetime.now(UTC).isoformat()
    providers = ["gemini", "groq", "cloudflare", "siliconflow", "openrouter"]
    rows = []
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "test-account")
    for provider in providers:
        monkeypatch.setenv(
            "CLOUDFLARE_API_TOKEN" if provider == "cloudflare" else f"{provider.upper()}_API_KEY",
            "test-key",
        )
        host = {
            "gemini": "ai.google.dev",
            "groq": "console.groq.com",
            "cloudflare": "developers.cloudflare.com",
            "siliconflow": "siliconflow.cn",
            "openrouter": "openrouter.ai",
        }[provider]
        model = (
            "@cf/zai-org/glm-4.7-flash"
            if provider == "cloudflare"
            else "test/model:free"
            if provider == "openrouter"
            else f"test-{provider}"
        )
        model_quota = {"rpm": 100, "rpd": 100, "tpm": 100000, "tpd": 1000000}
        if provider == "cloudflare":
            model_quota.update(
                daily_neurons=10000,
                neurons_per_million_input=1000,
                neurons_per_million_output=1000,
            )
        rows.append(
            {
                "provider": provider,
                "model": model,
                "workloads": ["journal", "simon-news", "cactus-brief"],
                "stages": ["author", "reviewer"],
                "context_tokens": 8192,
                "quota": model_quota,
                "evidence_url": f"https://{host}/free",
                "evidence_checked_at": now,
                "account_checked_at": now,
                "no_billing": True,
                "zero_price": True,
                "smoke_passed": True,
                "quality_passed": True,
                "standby": provider in {"siliconflow", "openrouter"},
            }
        )
    path = tmp_path / "verified.json"
    path.write_text(json.dumps({"models": rows}), encoding="utf-8")
    return path
