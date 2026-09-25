"""Explicit admission: only account-verified zero-price models enter routing."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

WORKLOADS = ("journal", "simon-news", "cactus-brief")
STAGES = ("author", "reviewer")
PROVIDERS = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "openai",
        "GEMINI_API_KEY",
        "ai.google.dev",
    ),
    "groq": ("https://api.groq.com/openai/v1", "openai", "GROQ_API_KEY", "console.groq.com"),
    "novita": ("https://api.novita.ai/openai/v1", "openai", "NOVITA_API_KEY", "novita.ai"),
    "siliconflow": (
        "https://api.siliconflow.cn/v1",
        "openai",
        "SILICONFLOW_API_KEY",
        "siliconflow.cn",
    ),
    "simplellm": (
        "https://api.simplellm.eu/v1",
        "openai",
        "SIMPLELLM_API_KEY",
        "simplellm.eu",
    ),
    "vikasit": (
        "https://api.vikasit.ai/v1",
        "openai",
        "VIKASIT_API_KEY",
        "vikasit.ai",
    ),
    "cloudflare": ("", "cloudflare", "CLOUDFLARE_API_TOKEN", "developers.cloudflare.com"),
}
NON_PERMANENT_OR_PAID_MODELS = {
    ("novita", "inclusionai/ling-3.0-flash-fin"),
    ("novita", "inclusionai/ling-3.0-flash-sante"),
    ("novita", "inclusionai/ling-3.0-flash-vl"),
    # Workers AI documents these models as requiring Workers Paid or prepaid
    # AI Gateway credits. They cannot use the 10,000-Neuron Free allocation.
    ("cloudflare", "@cf/moonshotai/kimi-k2.6"),
    ("cloudflare", "@cf/moonshotai/kimi-k2.7-code"),
    ("cloudflare", "@cf/zai-org/glm-5.2"),
    ("cloudflare", "@cf/zai-org/glm-5.3"),
    ("cloudflare", "@cf/zai-org/glm-5.3-flash"),
    ("cloudflare", "@cf/deepseek-ai/deepseek-v4-flash-0731"),
    ("cloudflare", "@cf/deepseek-ai/deepseek-v4-pro-0813"),
}
MAX_EVIDENCE_AGE = timedelta(days=7)
MAX_QUALITY_AGE = timedelta(days=30)
MIN_QUALITY_CASES = {"journal": 60, "simon-news": 50, "cactus-brief": 10}
_SUITE_SHA256 = re.compile(r"[0-9a-f]{64}")


def _quality_evaluations(
    raw: object,
    workloads: tuple[str, ...],
    stages: tuple[str, ...],
    now: datetime,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, tuple[str, int]]]]:
    """Compute conservative scores from comparable, dated editorial case results."""
    if not isinstance(raw, dict) or set(raw) != set(workloads):
        raise ValueError("missing_quality_evaluations")
    scores: dict[str, dict[str, int]] = {}
    suites: dict[str, dict[str, tuple[str, int]]] = {}
    for workload in workloads:
        records = raw[workload]
        if not isinstance(records, dict) or set(records) != set(stages):
            raise ValueError("missing_quality_evaluations")
        scores[workload] = {}
        suites[workload] = {}
        for stage in stages:
            row = records[stage]
            if not isinstance(row, dict):
                raise ValueError("invalid_quality_evaluation")
            passed, total = row.get("passed"), row.get("total")
            suite = row.get("suite_sha256")
            if (
                type(passed) is not int
                or type(total) is not int
                or total < MIN_QUALITY_CASES[workload]
                or total > 10_000
                or not 0 < passed <= total
                or not isinstance(suite, str)
                or not _SUITE_SHA256.fullmatch(suite)
            ):
                raise ValueError("invalid_quality_evaluation")
            try:
                checked_at = datetime.fromisoformat(row["evaluated_at"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, ValueError) as exc:
                raise ValueError("invalid_quality_evaluation") from exc
            if checked_at.tzinfo is None or checked_at > now or now - checked_at > MAX_QUALITY_AGE:
                raise ValueError("stale_quality_evaluation")
            # Wilson 95% lower bound prevents a 1/1 smoke from outranking a
            # thorough 59/60 editorial evaluation.
            p = passed / total
            z2 = 1.96**2
            lower = (
                p + z2 / (2 * total) - 1.96 * math.sqrt(p * (1 - p) / total + z2 / (4 * total**2))
            ) / (1 + z2 / total)
            scores[workload][stage] = round(1000 * max(0.0, lower))
            suites[workload][stage] = (suite, total)
    return scores, suites


@dataclass(frozen=True)
class Quota:
    rpm: int
    rpd: int
    tpm: int
    tpd: int
    rph: int | None = None
    tph: int | None = None
    daily_neurons: int | None = None
    neurons_per_million_input: int | None = None
    neurons_per_million_output: int | None = None
    reset_tz: str = "UTC"

    @classmethod
    def parse(cls, raw: dict) -> Quota:
        if not isinstance(raw, dict):
            raise ValueError("unverified_quota")
        vals = [raw.get(k) for k in ("rpm", "rpd", "tpm", "tpd")]
        if any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in vals):
            raise ValueError("unverified_quota")
        hourly = [raw.get("rph"), raw.get("tph")]
        if any(value is not None for value in hourly) and any(
            type(value) is not int or value <= 0 for value in hourly
        ):
            raise ValueError("unverified_hourly_quota")
        return cls(**raw)


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    workloads: tuple[str, ...]
    stages: tuple[str, ...]
    context_tokens: int
    quota: Quota
    evidence_url: str
    evidence_checked_at: datetime
    account_checked_at: datetime
    no_billing: bool
    zero_price: bool
    smoke_passed: bool
    quality_passed: bool
    api_base: str
    api_style: str
    api_key_env: str
    standby: bool
    quality_scores: dict[str, dict[str, int]]
    quality_suites: dict[str, dict[str, tuple[str, int]]]

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.model}"

    @classmethod
    def parse(cls, raw: dict, *, now: datetime | None = None) -> ModelSpec:
        if not isinstance(raw, dict):
            raise ValueError("invalid_model_record")
        now = now or datetime.now(UTC)
        provider = raw.get("provider")
        if provider not in PROVIDERS:
            raise ValueError("unknown_provider")
        base, style, key_env, proof_host = PROVIDERS[provider]
        model = raw.get("model")
        if not isinstance(model, str) or not model or any(ch.isspace() for ch in model):
            raise ValueError("invalid_model")
        if (provider, model) in NON_PERMANENT_OR_PAID_MODELS:
            raise ValueError("model_not_permanently_free")
        # Vikasit sells every other inference model per token. Its published
        # zero-price allowance applies only to this exact model ID.
        if provider == "vikasit" and model != "vikasit-nova":
            raise ValueError("model_not_permanently_free")
        evidence_url = raw.get("evidence_url", "")
        parsed_evidence = urlparse(evidence_url)
        host = parsed_evidence.hostname or ""
        if parsed_evidence.scheme != "https" or (
            host != proof_host and not host.endswith("." + proof_host)
        ):
            raise ValueError("untrusted_price_evidence")
        try:
            evidence_at = datetime.fromisoformat(raw["evidence_checked_at"].replace("Z", "+00:00"))
            account_at = datetime.fromisoformat(raw["account_checked_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("missing_verification_time") from exc
        if any(
            t.tzinfo is None or t > now or now - t > MAX_EVIDENCE_AGE
            for t in (evidence_at, account_at)
        ):
            raise ValueError("stale_verification")
        if not all(
            raw.get(k) is True
            for k in ("no_billing", "zero_price", "smoke_passed", "quality_passed")
        ):
            raise ValueError("unverified_free_model")
        workloads = tuple(raw.get("workloads", ()))
        stages = tuple(raw.get("stages", ()))
        if not workloads or not set(workloads).issubset(WORKLOADS):
            raise ValueError("invalid_workloads")
        if not stages or not set(stages).issubset(STAGES):
            raise ValueError("invalid_stages")
        quality_scores, quality_suites = _quality_evaluations(
            raw.get("quality_evaluations"), workloads, stages, now
        )
        context = raw.get("context_tokens")
        if type(context) is not int or context < 1024:
            raise ValueError("unverified_context")
        quota = Quota.parse(raw.get("quota", {}))
        if provider == "vikasit" and quota.tpd > 2_000_000:
            raise ValueError("unverified_free_daily_tokens")
        if provider == "simplellm" and (quota.rph is None or quota.tph is None):
            raise ValueError("missing_simplellm_hourly_quota")
        try:
            ZoneInfo(quota.reset_tz)
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid_quota_reset_timezone") from exc
        if provider == "cloudflare":
            # Workers AI resets the account-wide free Neurons bucket at 00:00 UTC.
            # A different zone would split one account across Redis day keys.
            if quota.reset_tz != "UTC":
                raise ValueError("invalid_cloudflare_reset_timezone")
            if any(
                type(v) is not int or v <= 0
                for v in (
                    quota.daily_neurons,
                    quota.neurons_per_million_input,
                    quota.neurons_per_million_output,
                )
            ) or (quota.daily_neurons is not None and quota.daily_neurons > 10_000):
                raise ValueError("unverified_neurons")
            account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
            if not account:
                raise ValueError("missing_cloudflare_account")
            base = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1"
        if not os.getenv(key_env, ""):
            raise ValueError("missing_provider_key")
        return cls(
            provider,
            model,
            workloads,
            stages,
            context,
            quota,
            evidence_url,
            evidence_at,
            account_at,
            True,
            True,
            True,
            True,
            base,
            style,
            key_env,
            raw.get("standby") is True,
            quality_scores,
            quality_suites,
        )


def load_models(
    path: Path | None = None, *, now: datetime | None = None
) -> tuple[list[ModelSpec], dict[str, str]]:
    """Invalid entries are visible in status but cannot serve requests."""
    path = path or Path(os.getenv("ROUTER_VERIFIED_MODELS", "var/verified-models.json"))
    if not path.exists():
        return [], {"catalog": "missing_verified_catalog"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], {"catalog": type(exc).__name__}
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
        return [], {"catalog": "invalid_catalog_schema"}
    models: list[ModelSpec] = []
    rejected: dict[str, str] = {}
    for i, row in enumerate(raw.get("models", [])):
        name = (
            f"{row.get('provider', '?')}/{row.get('model', i)}"
            if isinstance(row, dict)
            else f"row_{i}"
        )
        try:
            models.append(ModelSpec.parse(row, now=now))
        except (TypeError, ValueError) as exc:
            rejected[name] = str(exc)
    zones: dict[str, set[str]] = {}
    for model in models:
        zones.setdefault(model.provider, set()).add(model.quota.reset_tz)
    inconsistent = {provider for provider, values in zones.items() if len(values) != 1}
    if inconsistent:
        for model in models:
            if model.provider in inconsistent:
                rejected[model.id] = "inconsistent_account_reset_timezone"
        models = [model for model in models if model.provider not in inconsistent]
    suites: dict[tuple[str, str], dict[str, set[tuple[str, int]]]] = {}
    for model in models:
        for workload in model.workloads:
            for stage in model.stages:
                suites.setdefault((workload, stage), {}).setdefault(model.provider, set()).add(
                    model.quality_suites[workload][stage]
                )
    canonical: dict[tuple[str, str], tuple[str, int] | None] = {}
    for pair, providers in suites.items():
        votes: dict[tuple[str, int], int] = {}
        for provider_suites in providers.values():
            if len(provider_suites) == 1:
                suite = next(iter(provider_suites))
                votes[suite] = votes.get(suite, 0) + 1
        if not votes:
            canonical[pair] = None
            continue
        top = max(votes.values())
        winners = [suite for suite, count in votes.items() if count == top]
        canonical[pair] = winners[0] if len(winners) == 1 else None
    for model in models:
        if any(
            len(suites[(workload, stage)][model.provider]) != 1
            or model.quality_suites[workload][stage] != canonical[(workload, stage)]
            for workload in model.workloads
            for stage in model.stages
        ):
            rejected[model.id] = "incomparable_quality_suite"
    models = [model for model in models if model.id not in rejected]
    return models, rejected


def workload_token(workload: str) -> str:
    return os.getenv(f"ROUTER_TOKEN_{workload.upper().replace('-', '_')}", "")
