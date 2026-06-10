"""End-to-end demo: wire analyzer + orchestrator + quota + MoA in one shot.

Run: `python scripts/demo_e2e.py`

Behavior:
  1. Bootstraps registry + quota
  2. Runs a few real-ish requests through the orchestrator
  3. Triggers a MoA run with the top 3 free models and fake responses
     (the test version of acompletion is NOT installed here, so MoA
     attempts the real network — if keys are missing it just records
     the failure gracefully)
  4. Shows the updated quota snapshot
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager
from core.task_analyzer import HeuristicTaskAnalyzer
from core.orchestrator import RuleBasedOrchestrator
from core.moa_engine import MoAEngine, run_sync
from core.schemas import TaskAnalysis


def main() -> int:
    print("=" * 70)
    print("HERMES QUOTAMAX ROUTER — End-to-end demo")
    print("=" * 70)

    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    an = HeuristicTaskAnalyzer()
    orch = RuleBasedOrchestrator()

    requests = [
        "Refactor this Python function and add pytest coverage",
        "Write a 5000-word essay on the history of Rome",
        "Analyze this screenshot of the dashboard",
        "Read the entire 200k token codebase and summarize it",
        "Quick draft: extract all emails from this list",
        "Prove the Riemann hypothesis step by step",
    ]

    print("\n── Routing decisions ──\n")
    for msg in requests:
        a = an.analyze(msg)
        d = orch.route(a, reg, qm)
        print(f"  [{d.chosen_strategy:8s}] {d.primary_model[:55]:55s} "
              f"conf={d.confidence:.2f}  paid!={not d.preserve_paid_quota}")
        print(f"    ↳ {d.reasoning[:130]}")

    # MoA dry-run: top 3 free models, expecting failures (no real keys)
    print("\n── MoA engine (dry-run, expect graceful failure) ──\n")
    engine = MoAEngine(reg, qm, synthesizer_model="gemini/gemini-2.5-flash", timeout_s=2.0)
    top3 = [m.model_id for m in reg.free_first()[:3]]
    result = run_sync(
        engine,
        "What is the meaning of life?",
        top3,
        TaskAnalysis(task_type="chat", min_quality="very_high"),
    )
    print(f"  Successes: {result.success_count}/{len(top3)}")
    print(f"  Errors:    {list(result.errors.keys())}")
    print(f"  Synthesized[:200]: {result.synthesized[:200]}")

    print("\n── Quota snapshot after run ──\n")
    for s in qm.all_snapshots():
        if s.has_quota() and s.total:
            print(f"  {s.model_id[:55]:55s} {s.remaining:>14,}/{s.total:<14,}  "
                  f"({s.pct_remaining:5.1%})")
        else:
            print(f"  {s.model_id[:55]:55s} {'paid/unlimited':>30}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
