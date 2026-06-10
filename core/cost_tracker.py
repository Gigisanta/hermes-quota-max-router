"""Cost tracking — Phase 13.

Computes USD cost per call from the model's pricing + token usage.
Pricing lives in the registry (input_price, output_price per token).

`CostTracker` is stateful: it accumulates total spend per model across
all calls since process start. Useful for the dashboard and for
budget alerts. For persistence, swap the dict for Redis.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model_registry import ModelRegistry
    from .schemas import RoutingDecision

log = logging.getLogger(__name__)


def compute_cost_usd(
    registry: "ModelRegistry",
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Cost in USD for a single call. Returns 0.0 for free/unknown models."""
    m = registry.get(model_id)
    if m is None:
        return 0.0
    if m.is_free:
        return 0.0
    in_cost = input_tokens * m.input_price
    out_cost = output_tokens * m.output_price
    return round(in_cost + out_cost, 8)


@dataclass
class CostSnapshot:
    total_usd: float
    per_model: dict[str, float] = field(default_factory=dict)
    call_count: int = 0


class CostTracker:
    """In-memory cost accumulator. Thread-safe via GIL (dict ops are atomic)."""

    def __init__(self) -> None:
        self._per_model: dict[str, float] = defaultdict(float)
        self._calls_per_model: dict[str, int] = defaultdict(int)

    def record(self, model_id: str, cost_usd: float) -> None:
        self._per_model[model_id] += cost_usd
        self._calls_per_model[model_id] += 1

    def snapshot(self) -> CostSnapshot:
        return CostSnapshot(
            total_usd=round(sum(self._per_model.values()), 8),
            per_model={k: round(v, 8) for k, v in self._per_model.items()},
            call_count=sum(self._calls_per_model.values()),
        )

    def per_model(self) -> dict[str, float]:
        return {k: round(v, 8) for k, v in self._per_model.items()}

    def reset(self) -> None:
        self._per_model.clear()
        self._calls_per_model.clear()
