"""Mixture-of-Agents (MoA) Engine — Phase 5.

Runs N free models in parallel against the same prompt, then synthesizes
their answers with a separate free "synthesizer" model using
`prompts/moa_synthesizer.md`. The orchestrator decides when MoA is
appropriate (see orchestrator.py); the engine just executes.

Design:
  - Uses LiteLLM's async `acompletion` for true parallel fan-out.
  - All calls are wrapped in timeouts; if a model stalls, the engine
    proceeds with whatever it has.
  - The synthesizer is always a free model (gemini-flash by default)
    to keep MoA zero-cost.
  - `consume()` is called on the QuotaManager after successful calls
    so budgets stay accurate.

Usage:
  engine = MoAEngine(registry, quota_manager, synthesizer="gemini/gemini-2.5-flash")
  result = await engine.run(
      prompt="Explain X",
      models=["deepseek/deepseek-r1-0528", "openrouter/qwen/...", "groq/..."],
      analysis=task_analysis,
  )
  # -> MoAResult(synthesized="...", per_model={"deepseek/...": "...", ...})
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model_registry import ModelRegistry
from .quota_manager import QuotaManager
from .schemas import TaskAnalysis

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_SYNTHESIZER = "gemini/gemini-2.5-flash"
SYNTH_PROMPT_PATH = "prompts/moa_synthesizer.md"


@dataclass
class ModelCall:
    model_id: str
    response: str | None = None
    error: str | None = None
    duration_s: float = 0.0
    tokens: int = 0


@dataclass
class MoAResult:
    synthesized: str
    per_model: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    total_duration_s: float = 0.0
    total_tokens: int = 0
    synthesizer_model: str = ""

    @property
    def success_count(self) -> int:
        return sum(1 for v in self.per_model.values() if v)


async def _call_one(
    model_id: str,
    messages: list[dict],
    timeout_s: float,
) -> ModelCall:
    """Run a single model call with timeout + error capture."""
    from litellm import acompletion  # type: ignore
    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            acompletion(model=model_id, messages=messages, temperature=0.2),
            timeout=timeout_s,
        )
        text = resp["choices"][0]["message"]["content"] or ""
        usage = resp.get("usage", {}) or {}
        tokens = int(usage.get("total_tokens", 0))
        return ModelCall(
            model_id=model_id,
            response=text,
            duration_s=time.monotonic() - started,
            tokens=tokens,
        )
    except asyncio.TimeoutError:
        return ModelCall(model_id=model_id, error="timeout", duration_s=time.monotonic() - started)
    except Exception as e:  # noqa: BLE001
        return ModelCall(model_id=model_id, error=str(e)[:200], duration_s=time.monotonic() - started)


async def _synthesize(
    synthesizer_model: str,
    original_prompt: str,
    responses: dict[str, str],
    timeout_s: float,
) -> tuple[str, int]:
    from litellm import acompletion  # type: ignore

    sys_prompt = Path(SYNTH_PROMPT_PATH).read_text()
    user_payload = {
        "question": original_prompt,
        "responses": [{"model": mid, "answer": ans} for mid, ans in responses.items()],
    }
    resp = await asyncio.wait_for(
        acompletion(
            model=synthesizer_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": str(user_payload)},
            ],
            temperature=0.1,
        ),
        timeout=timeout_s,
    )
    text = resp["choices"][0]["message"]["content"] or ""
    usage = resp.get("usage", {}) or {}
    return text, int(usage.get("total_tokens", 0))


class MoAEngine:
    def __init__(
        self,
        registry: ModelRegistry,
        quota_manager: QuotaManager,
        synthesizer_model: str = DEFAULT_SYNTHESIZER,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.registry = registry
        self.quota = quota_manager
        self.synthesizer_model = synthesizer_model
        self.timeout_s = timeout_s

    async def run(
        self,
        prompt: str,
        models: list[str],
        analysis: TaskAnalysis,
    ) -> MoAResult:
        """Fan out to N models in parallel, then synthesize."""
        if not models:
            raise ValueError("MoAEngine.run requires at least one model")

        started = time.monotonic()
        messages = [{"role": "user", "content": prompt}]

        calls = await asyncio.gather(
            *(_call_one(m, messages, self.timeout_s) for m in models),
            return_exceptions=False,  # we capture errors inside _call_one
        )

        per_model: dict[str, str] = {}
        errors: dict[str, str] = {}
        total_tokens = 0
        for c in calls:
            total_tokens += c.tokens
            if c.response is not None:
                per_model[c.model_id] = c.response
                # iter 15: best-effort quota consumption. If the model
                # is unknown to the quota store, consume() returns False
                # and we just skip. Run the sync Redis call in a thread
                # so the event loop stays free.
                try:
                    await asyncio.to_thread(self.quota.consume, c.model_id, c.tokens)
                except Exception as e:  # noqa: BLE001
                    log.warning("moa quota.consume failed for %s: %s", c.model_id, e)
            elif c.error:
                errors[c.model_id] = c.error

        if not per_model:
            return MoAResult(
                synthesized="[MoA failed: no model returned a response]",
                errors=errors,
                total_duration_s=time.monotonic() - started,
                total_tokens=total_tokens,
                synthesizer_model=self.synthesizer_model,
            )

        # Synthesize
        try:
            synth, synth_tokens = await _synthesize(
                self.synthesizer_model, prompt, per_model, self.timeout_s,
            )
            total_tokens += synth_tokens
            # iter 15: same as above — non-blocking quota update.
            try:
                await asyncio.to_thread(self.quota.consume, self.synthesizer_model, synth_tokens)
            except (OSError, RuntimeError) as e:
                # iter 15: narrowed from `except Exception`. Quota store
                # failure modes are OS-level (Redis down) or runtime
                # (state corruption). Other exceptions (e.g. TypeError
                # from a bug) bubble up.
                log.warning("moa quota.consume failed for synthesizer %s: %s", self.synthesizer_model, e)
        except (RuntimeError, ValueError, TypeError, KeyError) as e:
            # iter 15: narrowed. Synthesis failure is recoverable by
            # falling through to "best individual response" below.
            synth = (
                "[synthesis failed: " + str(e)[:200] + "]\n\n"
                "Best individual response:\n\n" + next(iter(per_model.values()))
            )

        return MoAResult(
            synthesized=synth,
            per_model=per_model,
            errors=errors,
            total_duration_s=time.monotonic() - started,
            total_tokens=total_tokens,
            synthesizer_model=self.synthesizer_model,
        )


# --- Sync wrapper for environments without an event loop (e.g. dashboard) ---

def run_sync(engine: "MoAEngine", prompt: str, models: list[str], analysis: TaskAnalysis) -> MoAResult:
    """Convenience: run MoA from synchronous code."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an existing loop (e.g. Jupyter). Use a thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(asyncio.run, engine.run(prompt, models, analysis)).result()
        return asyncio.run(engine.run(prompt, models, analysis))
    except RuntimeError:
        return asyncio.run(engine.run(prompt, models, analysis))


if __name__ == "__main__":
    # Smoke: dry-run the engine without hitting the network. Just verifies
    # the orchestration logic doesn't crash on edge cases.
    from .quota_manager import QuotaManager
    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    engine = MoAEngine(reg, qm)

    async def _dry() -> MoAResult:
        return await engine.run(
            prompt="test",
            models=["nonexistent/model-1", "nonexistent/model-2"],
            analysis=TaskAnalysis(),
        )

    r = asyncio.run(_dry())
    print(f"success_count={r.success_count} errors={list(r.errors.keys())}")
    print(f"synthesized[:200]={r.synthesized[:200]}")
