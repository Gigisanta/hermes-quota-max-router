"""Budget alerts — Phase 14.

A simple thresholding layer on top of the QuotaManager. For each
tracked model, two thresholds:
  - WARN at >= warn_pct of the daily quota consumed
  - BLOCK at >= block_pct of the daily quota consumed (orchestrator
    should demote the model)

The orchestrator is not hard-wired here — this is a passive monitor
that exposes `should_warn` / `should_block` and an event log. The
dashboard reads it for the burn-rate widget.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class BudgetEvent:
    model_id: str
    level: str  # "warn" | "block"
    pct_consumed: float
    timestamp: float = field(default_factory=time.time)


class BudgetMonitor:
    def __init__(
        self,
        warn_pct: float = 0.80,
        block_pct: float = 1.00,
    ) -> None:
        if not 0 < warn_pct < 1:
            raise ValueError(f"warn_pct must be in (0,1), got {warn_pct}")
        if not warn_pct < block_pct:
            raise ValueError("block_pct must be > warn_pct")
        self.warn_pct = warn_pct
        self.block_pct = block_pct
        # Track the last event we fired per model per level to avoid spam
        self._fired_warn: set[str] = set()
        self._fired_block: set[str] = set()
        self.events: list[BudgetEvent] = []

    def check(self, quota_manager, model_id: str) -> list[BudgetEvent]:
        """Check the current state of a model. Returns any new events fired."""
        snap = quota_manager.snapshot(model_id)
        if not snap.has_quota() or snap.pct_remaining is None:
            return []
        pct_consumed = 1.0 - snap.pct_remaining
        fired: list[BudgetEvent] = []
        if pct_consumed >= self.block_pct and model_id not in self._fired_block:
            ev = BudgetEvent(model_id, "block", pct_consumed)
            self._fired_block.add(model_id)
            self.events.append(ev)
            fired.append(ev)
        elif pct_consumed >= self.warn_pct and model_id not in self._fired_warn:
            ev = BudgetEvent(model_id, "warn", pct_consumed)
            self._fired_warn.add(model_id)
            self.events.append(ev)
            fired.append(ev)
        return fired

    def should_warn(self, quota_manager, model_id: str) -> bool:
        snap = quota_manager.snapshot(model_id)
        if not snap.has_quota() or snap.pct_remaining is None:
            return False
        return (1.0 - snap.pct_remaining) >= self.warn_pct

    def should_block(self, quota_manager, model_id: str) -> bool:
        snap = quota_manager.snapshot(model_id)
        if not snap.has_quota() or snap.pct_remaining is None:
            return False
        return (1.0 - snap.pct_remaining) >= self.block_pct

    def burn_rates(self, quota_manager) -> dict[str, dict]:
        """Per-model current consumption snapshot, suitable for the dashboard."""
        out: dict[str, dict] = {}
        for s in quota_manager.all_snapshots():
            if s.has_quota() and s.pct_remaining is not None:
                pct_consumed = 1.0 - s.pct_remaining
                out[s.model_id] = {
                    "pct_consumed": round(pct_consumed, 4),
                    "status": (
                        "block"
                        if pct_consumed >= self.block_pct
                        else "warn"
                        if pct_consumed >= self.warn_pct
                        else "ok"
                    ),
                    "remaining": s.remaining,
                    "total": s.total,
                }
        return out

    def reset_alerts(self, model_id: str | None = None) -> None:
        """Re-arm alerts (e.g. after quota reset)."""
        if model_id is None:
            self._fired_warn.clear()
            self._fired_block.clear()
        else:
            self._fired_warn.discard(model_id)
            self._fired_block.discard(model_id)
