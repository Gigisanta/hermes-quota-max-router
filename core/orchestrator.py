"""Orchestrator — the brain of the router.

Two backends behind the same interface:

  - RuleBasedOrchestrator: deterministic scoring over the Model Registry.
    No external calls. The MVP brain and the cold-start fallback.
  - LLMOrchestrator: uses LiteLLM with prompts/orchestrator_system.md.
    The "real" brain — encodes the spec's hard rules in the system prompt
    and lets the LLM do nuanced reasoning.

Both return a RoutingDecision that downstream Router Engine executes.

Usage:
  orch = RuleBasedOrchestrator()
  decision = orch.route(analysis, registry, quota_manager)
  # -> RoutingDecision(chosen_strategy="direct", primary_model="deepseek/...",
  #                    confidence=0.82, reasoning="...", ...)
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Protocol

from .model_registry import Model, ModelRegistry
from .quota_manager import QuotaManager
from .schemas import RoutingDecision, TaskAnalysis

log = logging.getLogger(__name__)


# Strategy-specific confidence floors. Below these, the orchestrator either
# degrades to fallback (with preserve_paid_quota=True) or escalates to MoA.
CONFIDENCE_DIRECT = 0.55
CONFIDENCE_MOA = 0.50
CONFIDENCE_FALLBACK = 0.30

# How many top free models to consider for MoA fan-out
MOA_FANOUT = 3


# Map provider name (as stored in `Model.provider`) to the env var
# that holds a working API key for that provider. If the env var is
# unset (or empty), we skip that provider's models entirely. This
# prevents the router from picking a model that would 401 in production.
_PROVIDER_KEY_ENV: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY",),
    "google": ("GEMINI_API_KEY",),  # OpenRouter routes google/* through same key
    "openrouter": ("OPENROUTER_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "moonshotai": ("MOONSHOT_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "huggingface": ("HUGGINGFACE_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    # iter 15: added providers from the catalog gap fill.
    "cerebras": ("CEREBRAS_API_KEY",),
    "sambanova": ("SAMBANOVA_API_KEY",),
    "minimax": ("LOCAL_OPENAI_API_KEY",),  # self-hosted M3 weights via vLLM/Ollama
}


def has_key_for_model(model: "Model") -> bool:
    """Return True if the provider backing this model has a key in env.

    Curated OpenRouter entries (e.g. ``openrouter/qwen/...``) require
    ``OPENROUTER_API_KEY``. Native entries (``gemini/...``, ``deepseek/...``)
    require the provider's own key. The mapping is best-effort: a model
    with no entry in ``_PROVIDER_KEY_ENV`` is treated as requiring no
    auth (e.g. self-hosted, public).
    """
    import os as _os
    providers = _PROVIDER_KEY_ENV.get(model.provider, ())
    if not providers:
        return True  # unknown provider → assume no key required
    return any(_os.environ.get(name, "").strip() for name in providers)


class Orchestrator(Protocol):
    def route(
        self,
        analysis: TaskAnalysis,
        registry: ModelRegistry,
        quota_manager: QuotaManager,
    ) -> RoutingDecision: ...


def _tag_overlap(required: list[str], candidate: list[str]) -> float:
    """Jaccard-like overlap: |A∩B| / |A|. Returns 0 if required is empty."""
    if not required:
        return 0.0
    s = set(required)
    return len(s & set(candidate)) / len(s)


def _match_strength(required: list[str], candidate: list[str]) -> float:
    """How strongly a model matches a task, in [0, 1].

    - If all required tags are covered by strengths: 1.0
    - If none covered: 0.0
    - Linear in between, BUT if at least 1 top-priority tag matches, we
      give a strong floor (0.4) so a partial match is still a real candidate.

    This is intentionally more forgiving than Jaccard: routing decisions
    care about the BEST tag match, not the average.
    """
    if not required:
        return 0.0
    s_req, s_cand = set(required), set(candidate)
    if not (s_req & s_cand):
        return 0.0
    coverage = len(s_req & s_cand) / len(s_req)
    # Floor of 0.4 for any non-zero match — avoids killing good partial fits
    return 0.4 + 0.6 * coverage


def _score_candidate(
    model: Model,
    analysis: TaskAnalysis,
    quota: QuotaManager,
) -> tuple[float, bool]:
    """Composite score in [0, 1]. Higher is better. Returns (score, is_blocked).

    is_blocked is True when the model's quota is too low for the request —
    caller should veto it from consideration.
    """
    match = _match_strength(analysis.required_tags, model.strength_tags)
    perf = model.performance_score / 100.0  # 0..1

    # Quota factor + block check
    snap = quota.snapshot(model.model_id)
    needed = analysis.estimated_input_tokens + analysis.estimated_output_tokens
    if not snap.has_quota():
        quota_factor = 1.0
        blocked = False
    elif snap.remaining is None or snap.total is None or snap.total == 0:
        quota_factor = 0.5
        blocked = False
    else:
        ratio = max(0.0, min(1.0, snap.remaining / snap.total))
        quota_factor = 0.05 + 0.95 * ratio
        blocked = snap.remaining < needed

    # Quality floor
    quality_weight = 0.0
    if analysis.min_quality == "very_high":
        quality_weight = 0.10 * perf
    elif analysis.min_quality == "exceptional":
        quality_weight = 0.25 * perf

    # Task-specific boosts
    boost = 0.0
    if analysis.needs_long_context and model.context_window >= 200_000:
        # Prefer the LARGEST context window. Gemini's 1M crushes Moonshot's 200k
        # when both qualify, because the spec says "long_context_king" is Gemini.
        if model.context_window >= 1_000_000:
            boost += 0.30  # strong winner-tier boost for 1M-class windows
        else:
            boost += 0.10
    if analysis.needs_multimodal and "vision_master" in model.strength_tags:
        boost += 0.20
    if analysis.needs_tools and "tool_master" in model.strength_tags:
        boost += 0.15
    # Speed / draft preference (no explicit "needs_fast" flag — infer from low
    # quality bar and short task)
    if "ultra_fast" in model.strength_tags and analysis.min_quality == "high":
        # Tasks with small estimates are "drafts" → favor ultra_fast + high_volume
        if analysis.estimated_output_tokens < 1000:
            boost += 0.15

    # Hard veto: if model has none of the required tags, score is 0
    if match == 0.0 and not analysis.required_tags:
        base = 0.0
    elif match == 0.0:
        base = 0.0
    else:
        base = match * perf * 0.6 + quota_factor * 0.15 + quality_weight + boost

    return min(1.0, base), blocked


class RuleBasedOrchestrator:
    """Deterministic scoring orchestrator. No LLM calls."""

    def route(
        self,
        analysis: TaskAnalysis,
        registry: ModelRegistry,
        quota_manager: QuotaManager,
    ) -> RoutingDecision:
        all_models = registry.all()
        # Hard filter: only consider models whose provider key is in env.
        # Avoids 401s on every call when a key is missing.
        # iter 15: removed the `free_all`/`paid_all` aliases (dead) and
        # collapsed to a single pass.
        free = [m for m in all_models if m.is_free and has_key_for_model(m)]
        paid = [m for m in all_models if (not m.is_free) and has_key_for_model(m)]

        # Score every free model, hard-veto blocked ones
        scored_free: list[tuple[float, Model]] = []
        blocked_free: list[Model] = []
        for m in free:
            scored = _score_candidate(m, analysis, quota_manager)
            score: float = scored[0]
            is_blocked: bool = scored[1]
            if is_blocked:
                blocked_free.append(m)
            else:
                scored_free.append((score, m))
        scored_free.sort(key=lambda t: t[0], reverse=True)

        scored_paid: list[tuple[float, Model]] = sorted(
            ((_score_candidate(m, analysis, quota_manager)[0], m) for m in paid),
            key=lambda t: t[0], reverse=True,
        )

        # --- Strategy selection ---
        top_score, top_model = (scored_free[0] if scored_free else (0.0, None))
        second_score, second_model = (scored_free[1] if len(scored_free) > 1 else (0.0, None))

        # MoA: if task is demanding (≥3 tags, very_high/exceptional) AND ≥3 free
        # models have non-zero match, fan out for synthesis.
        moa_candidates = [m for s, m in scored_free[:MOA_FANOUT] if s > 0.0]
        if (
            analysis.min_quality in ("very_high", "exceptional")
            and len(analysis.required_tags) >= 3
            and len(moa_candidates) >= 3
        ):
            models = [m.model_id for m in moa_candidates]
            return RoutingDecision(
                chosen_strategy="moa",
                primary_model=models[0],
                fallback_model=models[1] if len(models) > 1 else None,
                models_to_use=models,
                reasoning=(
                    f"MoA fan-out across {len(models)} free models covering "
                    f"{', '.join(analysis.required_tags[:4])}. Top scorer: "
                    f"{models[0]} (perf={moa_candidates[0].performance_score:.0f}/100, "
                    f"score={top_score:.2f})."
                ),
                estimated_tokens=analysis.estimated_input_tokens + analysis.estimated_output_tokens,
                quality_expectation=analysis.min_quality,  # type: ignore[arg-type]
                preserve_paid_quota=True,
                tags_matched=analysis.required_tags,
                confidence=round(min(0.95, top_score + 0.15), 2),
            )

        # Default: direct on the best free model
        if top_model is not None and top_score >= CONFIDENCE_DIRECT:
            return RoutingDecision(
                chosen_strategy="direct",
                primary_model=top_model.model_id,
                fallback_model=second_model.model_id if second_model else None,
                models_to_use=[top_model.model_id],
                reasoning=(
                    f"Direct call to {top_model.model_id} — best free-tier match "
                    f"for tags {analysis.required_tags}. Score={top_score:.2f} "
                    f"(perf={top_model.performance_score:.0f}, match="
                    f"{_match_strength(analysis.required_tags, top_model.strength_tags):.2f})."
                ),
                estimated_tokens=analysis.estimated_input_tokens + analysis.estimated_output_tokens,
                quality_expectation=analysis.min_quality,  # type: ignore[arg-type]
                preserve_paid_quota=True,
                tags_matched=[
                    t for t in analysis.required_tags if t in top_model.strength_tags
                ],
                confidence=round(min(0.95, top_score), 2),
            )

        # Paid escalation: if free is weak (score below CONFIDENCE_DIRECT) and
        # the task demands very_high or exceptional quality
        if (
            scored_paid
            and top_score < CONFIDENCE_DIRECT
            and analysis.min_quality in ("very_high", "exceptional")
        ):
            paid_top_score, paid_top = scored_paid[0]
            # Fallback to the best available FREE (not blocked) if any
            free_fallback = top_model.model_id if top_model else None
            return RoutingDecision(
                chosen_strategy="fallback",
                primary_model=paid_top.model_id,
                fallback_model=free_fallback,
                models_to_use=[paid_top.model_id],
                reasoning=(
                    f"Free-tier best ({top_model.model_id if top_model else 'none'}) "
                    f"scored {top_score:.2f} < threshold {CONFIDENCE_DIRECT:.2f} for "
                    f"min_quality={analysis.min_quality}. Escalating to paid "
                    f"{paid_top.model_id} (score={paid_top_score:.2f})."
                ),
                estimated_tokens=analysis.estimated_input_tokens + analysis.estimated_output_tokens,
                quality_expectation=analysis.min_quality,  # type: ignore[arg-type]
                preserve_paid_quota=False,
                tags_matched=analysis.required_tags,
                confidence=round(paid_top_score, 2),
            )

        # Last resort: weak direct on best free, with any non-blocked 2nd as fallback
        if top_model is not None:
            return RoutingDecision(
                chosen_strategy="direct",
                primary_model=top_model.model_id,
                fallback_model=second_model.model_id if second_model else None,
                models_to_use=[top_model.model_id],
                reasoning=(
                    f"Weak confidence in free-tier match ({top_score:.2f}). "
                    f"Using best available: {top_model.model_id}."
                ) + (
                    f" Quota vetoed: {', '.join(m.model_id for m in blocked_free[:3])}."
                    if blocked_free else ""
                ),
                estimated_tokens=analysis.estimated_input_tokens + analysis.estimated_output_tokens,
                quality_expectation="high",
                preserve_paid_quota=True,
                tags_matched=[
                    t for t in analysis.required_tags if t in top_model.strength_tags
                ],
                confidence=round(max(CONFIDENCE_FALLBACK, top_score), 2),
            )

        # No models at all — should be impossible
        return RoutingDecision(
            chosen_strategy="fallback",
            primary_model="",
            reasoning="No models available in registry.",
            confidence=0.0,
        )


class LLMOrchestrator:
    """LiteLLM-backed orchestrator using prompts/orchestrator_system.md."""

    DEFAULT_MODEL = "openai-codex/gpt-5.5"  # paid, but used only as the brain
    PROMPT_PATH = "prompts/orchestrator_system.md"

    def __init__(self, model: str | None = None, prompt_path: str | None = None, live: bool = True) -> None:
        self.model = model or self.DEFAULT_MODEL
        self.prompt_path = prompt_path or self.PROMPT_PATH
        # When `live=False`, the orchestrator returns a stub RoutingDecision
        # so test/dev runs without keys don't 401. Live routing is the default
        # (matches what callers expect from a "real" orchestrator).
        self.live = live

    def route(
        self,
        analysis: TaskAnalysis,
        registry: ModelRegistry,
        quota_manager: QuotaManager,
    ) -> RoutingDecision:
        if not self.live:
            # Deterministic stub: pick the top free model (or first available)
            # without calling any LLM. Used in tests and when the brain
            # model key is missing.
            free = [m for m in registry.all() if m.is_free]
            top = sorted(free, key=lambda m: m.tier_rank)[0] if free else (
                registry.all()[0] if registry.all() else None
            )
            if top is None:
                return RoutingDecision(
                    chosen_strategy="fallback",
                    primary_model="",
                    reasoning="No models available.",
                    confidence=0.0,
                )
            return RoutingDecision(
                chosen_strategy="direct",
                primary_model=top.model_id,
                fallback_model=None,
                models_to_use=[top.model_id],
                reasoning=f"[LLMOrchestrator stub] picked top free model {top.model_id} "
                          f"(brain model {self.model} not live).",
                estimated_tokens=analysis.estimated_input_tokens + analysis.estimated_output_tokens,
                quality_expectation=analysis.min_quality,
                preserve_paid_quota=True,
                tags_matched=[],
                confidence=0.5,
            )

        from litellm import completion

        system_prompt = Path(self.prompt_path).read_text()
        context = self._build_context(analysis, registry, quota_manager)

        resp = completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        text = resp["choices"][0]["message"]["content"]
        data = json.loads(text)
        return RoutingDecision(**data)

    @staticmethod
    def _build_context(
        analysis: TaskAnalysis,
        registry: ModelRegistry,
        quota_manager: QuotaManager,
    ) -> str:
        models = registry.all()
        rows = []
        for m in models:
            snap = quota_manager.snapshot(m.model_id)
            quota_str = (
                f"{snap.remaining:,}/{snap.total:,} ({snap.pct_remaining:.0%})"
                if snap.has_quota() else "unlimited (paid)"
            )
            rows.append({
                "model_id": m.model_id,
                "free": m.is_free,
                "tier_rank": m.tier_rank,
                "performance": m.performance_score,
                "quota": quota_str,
                "strengths": m.strength_tags,
            })
        return json.dumps({
            "task_analysis": analysis.model_dump(),
            "available_models": rows,
        }, indent=2)


if __name__ == "__main__":
    from core.model_registry import ModelRegistry
    from core.quota_manager import QuotaManager
    from core.task_analyzer import HeuristicTaskAnalyzer

    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    analyzer = HeuristicTaskAnalyzer()
    orch = RuleBasedOrchestrator()

    for msg in [
        "Refactor this Python function and add pytest coverage",
        "Write a 5000-word essay on the history of Rome",
        "Prove the Riemann hypothesis using chain of thought",
        "Quick draft: summarize this 200k token codebase",
    ]:
        a = analyzer.analyze(msg)
        d = orch.route(a, reg, qm)
        print(f"\n>>> {msg[:60]}")
        print(f"    strategy={d.chosen_strategy} primary={d.primary_model}")
        print(f"    confidence={d.confidence} preserve_paid={d.preserve_paid_quota}")
        print(f"    reasoning: {d.reasoning[:120]}")
