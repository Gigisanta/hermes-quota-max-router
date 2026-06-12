"""Provider catalogs — Phase 16.

A curated list of "providers to watch" that we know have generous
free tiers as of mid-2026. The Auto-Updater hits each catalog endpoint,
parses the response into our Model schema, and feeds them to the
RegistryUpdater.

Three categories:
  - **Static catalogs** (in this file): we know these endpoints exist
    and have stable schemas. We hit them.
  - **Aggregators** (OpenRouter, HuggingFace): one endpoint gives us
    access to 50+ underlying models. We auto-discover from the
    aggregator instead of scraping every provider.
  - **Curated additions**: human-added entries for providers without
    a programmatic API. These are the "spec.md §2" source of truth.

Each catalog row has:
  - `name`: human label
  - `endpoint`: full URL
  - `parser`: how to turn JSON into list[dict] matching our Model schema
  - `free_filter`: optional selector for "free tier only" entries
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    name: str
    endpoint: str
    parser: Callable[[dict], list[dict]]
    free_filter: Callable[[dict], bool] | None = None
    requires_auth: bool = False
    notes: str = ""


# --- Parsers ---
# Each parser takes the raw JSON response from a catalog endpoint and
# returns a list of dicts matching registry/models.json schema. We
# normalize on the fly: convert prices, default missing fields, etc.


def _normalize(raw: dict, defaults: dict) -> dict:
    """Merge raw + defaults + computed fields into a Model dict."""
    out = {**defaults, **raw}
    # Type coercions
    for k in ("context_window", "tier_rank", "daily_quota_tokens", "current_remaining_tokens"):
        if k in out and out[k] is not None:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                out[k] = defaults.get(k)
    for k in ("input_price", "output_price", "performance_score"):
        if k in out and out[k] is not None:
            try:
                out[k] = float(out[k])
            except (TypeError, ValueError):
                out[k] = defaults.get(k, 0.0)
    for k in ("strength_tags", "weakness_tags", "best_for"):
        v = out.get(k)
        if v is None:
            out[k] = []
        elif isinstance(v, str):
            out[k] = [t.strip() for t in v.split(",") if t.strip()]
    return out


# --- OpenRouter public models endpoint ---
# https://openrouter.ai/api/v1/models — returns all models, with `pricing`
# in USD per token as strings, and `id` like "vendor/model-name".
# Many entries have `id` ending in `:free` for the no-cost tier.


def _parse_openrouter(data: dict) -> list[dict]:
    out: list[dict] = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        # Skip the "free" suffix when storing; the free status is in
        # `pricing` (both prompt and completion == "0")
        prompt = float(m.get("pricing", {}).get("prompt", "0") or "0")
        completion = float(m.get("pricing", {}).get("completion", "0") or "0")
        is_free = prompt == 0.0 and completion == 0.0
        # OpenRouter schema: context_length is at TOP LEVEL (not under
        # architecture). Fall back to top_provider.context_length, then
        # architecture.context_length, then 0.
        ctx = (
            int(m.get("context_length") or 0)
            or int((m.get("top_provider") or {}).get("context_length") or 0)
            or int((m.get("architecture") or {}).get("context_length") or 0)
        )
        # Detect modalities → tags
        arch = m.get("architecture") or {}
        in_mods = arch.get("input_modalities") or []
        strength: list[str] = ["high_volume"] if is_free else ["premium"]
        if "image" in in_mods or "file" in in_mods:
            strength.append("vision_master")
            strength.append("multimodal")
        if is_free:
            strength.append("cheap_parallel")
        out.append(
            _normalize(
                {
                    "model_id": mid,
                    "provider": mid.split("/")[0] if "/" in mid else "openrouter",
                    "display_name": m.get("name", mid),
                    "context_window": ctx,
                    "input_price": prompt,
                    "output_price": completion,
                    "is_free": is_free,
                    # OpenRouter has many models; start tier_rank at 10 to slot
                    # below our curated 1-6. Auto-Updater will re-rank on merge.
                    "tier_rank": 10,
                    "daily_quota_tokens": 10_000_000 if is_free else None,
                    "strength_tags": strength,
                    "best_for": [m.get("description", "").split(".")[0][:80] or "general"],
                    "performance_score": 80.0 if is_free else 88.0,
                    "notes": f"[auto-discovery] openrouter:{mid}",
                },
                defaults={
                    "strength_tags": [],
                    "weakness_tags": [],
                    "best_for": [],
                    "performance_score": 75.0,
                    "is_free": False,
                    "context_window": 8192,
                    "input_price": 0.0,
                    "output_price": 0.0,
                },
            )
        )
    return out


# --- HuggingFace Inference API (free community providers) ---
# https://huggingface.co/api/models?inference=warm&filter=conversational
# Returns a list of model metadata. Free if `inference_provider` is set
# AND the model is NOT marked private/gated. We DON'T mark every HF
# model as free by default — gated or paid-inference models get is_free=False.


def _parse_huggingface(data: list | dict) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("models", data) or []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for m in data[:200]:  # cap to keep the merge sane
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        # HF "free" is conditional:
        #   - private=True → NOT free
        #   - gated=True   → usually requires accepting a license, skip
        #   - inference_provider absent → no warm inference, not reliable
        is_private = bool(m.get("private"))
        is_gated = bool(m.get("gated"))
        has_inference = bool(m.get("inference_provider"))
        is_free = (not is_private) and (not is_gated) and has_inference
        # HF tags are a list of strings; intersect with our schema.
        raw_tags = m.get("tags", []) or []
        mapped: list[str] = []
        for t in raw_tags:
            tt = str(t).lower()
            if "code" in tt and "coding_sota" not in mapped:
                mapped.append("coding_sota")
            if "chat" in tt or "instruct" in tt:
                mapped.extend(["instruction_following_god"])
            if "vision" in tt or "multimodal" in tt:
                mapped.extend(["vision_master", "multimodal"])
            if "long-context" in tt or "32k" in tt or "128k" in tt:
                mapped.append("long_context_king")
        out.append(
            _normalize(
                {
                    "model_id": f"hf/{mid}",
                    "provider": "huggingface",
                    "display_name": mid,
                    "context_window": 8192,
                    "input_price": 0.0,
                    "output_price": 0.0,
                    "is_free": is_free,
                    "tier_rank": 20,  # below our 1-6 and OpenRouter's 10
                    "daily_quota_tokens": 1_000_000 if is_free else 0,
                    "strength_tags": mapped[:6] if mapped else (["high_volume"] if is_free else []),
                    "best_for": ["experimental", "research"] if is_free else [],
                    "performance_score": 70.0 if is_free else 50.0,
                    "notes": f"[auto-discovery] huggingface:{mid}{' (gated)' if is_gated else ''}",
                },
                defaults={
                    "strength_tags": [],
                    "weakness_tags": ["experimental"] if is_free else [],
                    "best_for": [],
                    "performance_score": 70.0,
                    "is_free": False,
                    "context_window": 8192,
                    "input_price": 0.0,
                    "output_price": 0.0,
                },
            )
        )
    return out


# --- Curated catalogs (in-repo, no network) ---
# These mirror the spec's "June 2026" table. The auto-updater can
# fall back to them when the network is down. The schema matches
# registry/models.json exactly.


def _parse_static_curated(data: dict) -> list[dict]:
    return data.get("models", [])


# --- Master catalog list ---

CATALOGS: list[CatalogEntry] = [
    CatalogEntry(
        name="openrouter_public",
        endpoint="https://openrouter.ai/api/v1/models",
        parser=_parse_openrouter,
        notes=(
            "OpenRouter's public models endpoint. Returns 100+ models with "
            "real pricing. The parser auto-detects free tier (price=0)."
        ),
    ),
    CatalogEntry(
        name="huggingface_warm",
        endpoint=("https://huggingface.co/api/models?inference=warm&filter=text-generation&limit=200"),
        parser=_parse_huggingface,
        notes=(
            "HuggingFace warm-inference models. Returns up to 200 entries; "
            "all are free but performance varies."
        ),
    ),
    CatalogEntry(
        name="curated_static",
        endpoint="registry/models.json",
        parser=_parse_static_curated,
        notes=(
            "The in-repo seed. Used as fallback when network is down. "
            "This is the 'source of truth' for curated models."
        ),
    ),
]
