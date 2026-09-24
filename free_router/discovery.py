"""Daily official catalog audit; discovery never grants routing access by itself."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from free_router.config import ModelSpec
from free_router.provider import ProviderClient, ProviderFailure
from free_router.quota import QuotaStore

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
FREE_LLM_DIRECTORY = "https://raw.githubusercontent.com/nejib1/Free-LLM/main/README.md"
ACCESS_AUDIT_PROMPT = (
    "Dato público de prueba: La biblioteca municipal abre el martes a las 10. "
    "Redactá una sola oración informativa, fiel al dato y sin agregar información."
)
ACCESS_AUDIT_OUTPUT_TOKENS = 48
ACCESS_AUDIT_COOLDOWN_SECONDS = 24 * 60 * 60


def _zero(value: object) -> bool:
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, TypeError):
        return False


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _editorial_smoke_passed(content: str) -> bool:
    normalized = " ".join(content.split()).casefold()
    return (
        re.fullmatch(
            r"(?:la )?biblioteca municipal (?:abre|abrirá) el martes a las "
            r"(?:10(?::00)?|diez)\.?",
            normalized,
        )
        is not None
    )


def _provider_limits(models: list[ModelSpec], provider: str) -> tuple[int, int, int, int]:
    quotas = [model.quota for model in models if model.provider == provider]
    return tuple(
        min(getattr(quota, key) for quota in quotas) for key in ("rpm", "rpd", "tpm", "tpd")
    )


async def audit_catalog(
    models: list[ModelSpec],
    quota: QuotaStore,
    *,
    client: httpx.AsyncClient | None = None,
    output: Path = Path("var/discovery.json"),
) -> dict:
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    result: dict = {
        "checked_at": datetime.now(UTC).isoformat(),
        "sources": {},
        "candidates": [],
        "demoted": [],
    }
    try:
        try:
            response = await client.get(OPENROUTER_MODELS)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise ValueError("invalid_model_catalog")
            free_models = {}
            for row in data:
                mid = row.get("id", "")
                pricing = row.get("pricing") or {}
                if (
                    mid.endswith(":free")
                    and _zero(pricing.get("prompt"))
                    and _zero(pricing.get("completion"))
                    and _zero(pricing.get("request", "0"))
                ):
                    free_models[mid] = row
            result["sources"]["openrouter"] = {"status": "ok", "free_models": len(free_models)}
            known = {m.model for m in models if m.provider == "openrouter"}
            result["candidates"] = [
                {
                    "provider": "openrouter",
                    "model": mid,
                    "context_tokens": row.get("context_length"),
                }
                for mid, row in free_models.items()
                if mid not in known
            ]
            for model in models:
                if model.provider == "openrouter" and model.model not in free_models:
                    quota.cool_down(model, 7 * 86400, permanent=True)
                    result["demoted"].append(model.id)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            result["sources"]["openrouter"] = {
                "status": "unavailable",
                "reason": type(exc).__name__,
            }
        try:
            response = await client.get(FREE_LLM_DIRECTORY)
            result["sources"]["free_llm_directory"] = {
                "status": "ok" if response.status_code == 200 else "unavailable",
                "reference_only": True,
            }
        except httpx.HTTPError:
            result["sources"]["free_llm_directory"] = {
                "status": "unavailable",
                "reference_only": True,
            }
    finally:
        if own_client:
            await client.aclose()
    _atomic_json(output, result)
    return result


async def audit_access(
    models: list[ModelSpec],
    quota: QuotaStore,
    provider: ProviderClient,
    *,
    blocked: dict[str, str] | None = None,
    output: Path = Path("var/access-audit.json"),
) -> dict:
    """Probe verified models with one reserved, public editorial smoke request each.

    The report contains model IDs and outcomes only. It never stores prompts, model
    responses, credentials, or changes the price/quota attestations in the model file.
    """
    checked_at = datetime.now(UTC).isoformat()
    report: dict = {
        "checked_at": checked_at,
        "probe": "public_editorial_minimal_v1",
        "models": [],
    }
    blocked = blocked or {}
    messages = [{"role": "user", "content": ACCESS_AUDIT_PROMPT}]
    input_tokens = max(1, len(ACCESS_AUDIT_PROMPT.encode("utf-8")) + 64)

    for spec in models:
        entry: dict[str, Any] = {
            "id": spec.id,
            "provider": spec.provider,
            "checked_at": checked_at,
        }
        if spec.id in blocked:
            entry.update(status="skipped", reason=blocked[spec.id])
            report["models"].append(entry)
            continue

        try:
            cooldown = quota.cooldown_remaining(spec)
        except RuntimeError:
            entry.update(status="blocked", reason="quota_store_unavailable")
            report["models"].append(entry)
            continue
        if cooldown > 0:
            entry.update(status="skipped", reason="cooldown_active", retry_after=cooldown)
            report["models"].append(entry)
            continue

        try:
            reservation = quota.reserve(
                spec,
                "audit",
                input_tokens,
                ACCESS_AUDIT_OUTPUT_TOKENS,
                provider_limits=_provider_limits(models, spec.provider),
            )
        except RuntimeError:
            entry.update(status="blocked", reason="quota_store_unavailable")
            report["models"].append(entry)
            continue
        if not reservation.keys:
            retry_after = max(1, reservation.retry_after)
            try:
                quota.cool_down(spec, retry_after)
                reason = "audit_quota_reservation_unavailable"
            except RuntimeError:
                reason = "quota_store_unavailable"
            entry.update(status="blocked", reason=reason, retry_after=retry_after)
            report["models"].append(entry)
            continue

        try:
            result = await provider.complete(spec, messages, ACCESS_AUDIT_OUTPUT_TOKENS, 0.0)
        except ProviderFailure as exc:
            try:
                quota.cool_down(
                    spec,
                    exc.retry_after,
                    permanent=exc.permanent,
                    account_wide=exc.reason == "upstream_rate_limited",
                )
                reason = exc.reason
            except RuntimeError:
                reason = "quota_store_unavailable"
            entry.update(
                status="failed",
                reason=reason,
                retry_after=exc.retry_after,
            )
            report["models"].append(entry)
            continue
        except Exception as exc:
            try:
                quota.cool_down(spec, ACCESS_AUDIT_COOLDOWN_SECONDS)
                reason = "audit_request_failed"
            except RuntimeError:
                reason = "quota_store_unavailable"
            entry.update(status="failed", reason=reason, error_type=type(exc).__name__)
            report["models"].append(entry)
            continue

        prompt_usage = result.get("prompt_tokens")
        completion_usage = result.get("completion_tokens")
        if isinstance(prompt_usage, int) and isinstance(completion_usage, int):
            quota.reconcile(
                reservation,
                input_tokens,
                ACCESS_AUDIT_OUTPUT_TOKENS,
                prompt_usage,
                completion_usage,
            )

        actual_model = result.get("actual_model")
        permitted_models = {spec.model}
        if spec.provider == "openrouter":
            permitted_models.add(spec.model.removesuffix(":free"))
        smoke_passed = (
            actual_model in permitted_models
            and isinstance(result.get("content"), str)
            and _editorial_smoke_passed(result["content"])
        )
        if not smoke_passed:
            try:
                quota.cool_down(spec, ACCESS_AUDIT_COOLDOWN_SECONDS)
                reason = "editorial_smoke_failed"
            except RuntimeError:
                reason = "quota_store_unavailable"
            entry.update(status="failed", reason=reason)
        else:
            entry.update(status="passed", access_passed=True, editorial_smoke_passed=True)
        report["models"].append(entry)

    try:
        _atomic_json(output, report)
    except OSError:
        # Without a durable audit record, temporarily stop every model whose probe
        # otherwise passed. Redis failure itself also makes router readiness fail closed.
        for spec, entry in zip(models, report["models"], strict=True):
            if entry.get("status") == "passed":
                try:
                    quota.cool_down(spec, ACCESS_AUDIT_COOLDOWN_SECONDS)
                except RuntimeError:
                    pass
        report["audit_persisted"] = False
        return report
    report["audit_persisted"] = True
    return report


async def audit_all(
    models: list[ModelSpec],
    quota: QuotaStore,
    provider: ProviderClient,
    *,
    catalog_output: Path = Path("var/discovery.json"),
    access_output: Path = Path("var/access-audit.json"),
) -> dict:
    """Run the catalog audit and the real-access audit without rewriting attestations."""
    try:
        catalog = await audit_catalog(models, quota, client=provider.client, output=catalog_output)
    except Exception:
        # An unavailable or unpersistable live catalog is not price evidence.
        catalog = {
            "checked_at": datetime.now(UTC).isoformat(),
            "sources": {"openrouter": {"status": "unavailable", "reason": "audit_failed"}},
            "candidates": [],
            "demoted": [],
        }

    openrouter_source = catalog.get("sources", {}).get("openrouter", {})
    demoted = set(catalog.get("demoted", []))
    blocked: dict[str, str] = {}
    for spec in models:
        if spec.provider != "openrouter":
            continue
        if openrouter_source.get("status") != "ok":
            blocked[spec.id] = "openrouter_free_catalog_unavailable"
            try:
                quota.cool_down(spec, ACCESS_AUDIT_COOLDOWN_SECONDS)
            except RuntimeError:
                pass
        elif spec.id in demoted:
            blocked[spec.id] = "openrouter_model_not_in_free_catalog"

    access = await audit_access(models, quota, provider, blocked=blocked, output=access_output)
    return {"catalog": catalog, "access": access}
