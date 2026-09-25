"""Daily reference discovery and real-access audit; neither grants admission."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from free_router.config import ModelSpec
from free_router.provider import ProviderClient, ProviderFailure
from free_router.quota import QuotaStore

FREE_LLM_DIRECTORY = "https://raw.githubusercontent.com/nejib1/Free-LLM/main/README.md"
QUICKREF_START = "<!--TABLE:QUICKREF:START-->"
QUICKREF_END = "<!--TABLE:QUICKREF:END-->"
ACCESS_AUDIT_PROMPT = (
    "Dato público de prueba: La biblioteca municipal abre el martes a las 10. "
    "Redactá una sola oración informativa, fiel al dato y sin agregar información."
)
ACCESS_AUDIT_OUTPUT_TOKENS = 48
# Gemma 4 E4B can spend a short budget on reasoning before emitting the
# visible sentence. A live 48-token probe returned HTTP 200 with empty content;
# the same prompt completed with a 512-token cap on the verified free model.
SIMPLELLM_ACCESS_AUDIT_OUTPUT_TOKENS = 512
ACCESS_AUDIT_COOLDOWN_SECONDS = 24 * 60 * 60


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


def _provider_limits(
    models: list[ModelSpec], provider: str
) -> tuple[int, int, int, int, int | None, int | None]:
    quotas = [model.quota for model in models if model.provider == provider]
    base = tuple(
        min(getattr(quota, key) for quota in quotas) for key in ("rpm", "rpd", "tpm", "tpd")
    )
    hourly = tuple(
        min(values)
        if (values := [getattr(quota, key) for quota in quotas if getattr(quota, key, None)])
        else None
        for key in ("rph", "tph")
    )
    return (*base, *hourly)


def _directory_entries(readme: str) -> list[dict[str, str]]:
    """Extract names from the directory, never instructions or admission evidence."""
    start = readme.find(QUICKREF_START)
    end = readme.find(QUICKREF_END, start + len(QUICKREF_START))
    if start < 0 or end < 0:
        return []
    table = readme[start + len(QUICKREF_START) : end]
    names: set[str] = set()
    for line in table.splitlines()[:120]:
        match = re.match(
            r"^\|\s*\[([A-Za-z0-9][A-Za-z0-9 ._()+-]{0,79})\]"
            r"\(https://[^)\s]+\)\s*\|",
            line,
        )
        if match:
            names.add(match.group(1).strip())
    return [
        {"name": name, "status": "unvetted_directory_entry"}
        for name in sorted(names, key=str.casefold)
    ]


async def audit_catalog(
    models: list[ModelSpec],
    quota: QuotaStore,
    *,
    client: httpx.AsyncClient | None = None,
    output: Path = Path("var/discovery.json"),
) -> dict:
    # Keep the historical arguments for callers; the directory is reference-only.
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    result: dict = {
        "checked_at": datetime.now(UTC).isoformat(),
        "sources": {},
        "directory_entries": [],
        "candidates": [],
        "demoted": [],
    }
    try:
        try:
            response = await client.get(FREE_LLM_DIRECTORY)
            if response.status_code == 200:
                result["directory_entries"] = _directory_entries(response.text[:1_000_000])
            directory_ok = bool(result["directory_entries"])
            result["sources"]["free_llm_directory"] = {
                "status": (
                    "ok"
                    if directory_ok
                    else "malformed"
                    if response.status_code == 200
                    else "unavailable"
                ),
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
        output_tokens = (
            SIMPLELLM_ACCESS_AUDIT_OUTPUT_TOKENS
            if spec.provider == "simplellm"
            else ACCESS_AUDIT_OUTPUT_TOKENS
        )
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
                output_tokens,
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
            result = await provider.complete(spec, messages, output_tokens, 0.0)
        except ProviderFailure as exc:
            if exc.reason == "provider_concurrency_busy":
                try:
                    quota.refund_unsent(reservation)
                    entry.update(status="skipped", reason="provider_concurrency_busy")
                except RuntimeError:
                    entry.update(status="blocked", reason="quota_store_unavailable")
                report["models"].append(entry)
                continue
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
                output_tokens,
                prompt_usage,
                completion_usage,
            )

        actual_model = result.get("actual_model")
        smoke_passed = (
            actual_model == spec.model
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
        # A failed reference lookup does not affect the independently verified models.
        catalog = {
            "checked_at": datetime.now(UTC).isoformat(),
            "sources": {"free_llm_directory": {"status": "unavailable", "reason": "audit_failed"}},
            "directory_entries": [],
            "candidates": [],
            "demoted": [],
        }

    access = await audit_access(models, quota, provider, output=access_output)
    return {"catalog": catalog, "access": access}
