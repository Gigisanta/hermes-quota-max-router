"""End-to-end smoke: Registry + QuotaManager integration.

Verifies that the QuotaManager syncs from the ModelRegistry, consumes
correctly, blocks when exhausted, and reports a usable snapshot.

Run: `python scripts/demo_quota.py` (no Redis required)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager


def main() -> int:
    reg = ModelRegistry()
    qm = QuotaManager()
    synced = qm.sync_from_registry(reg)
    print(f"\n[smoke] synced {synced} models into quota store\n")

    print("=== Initial state ===")
    for s in qm.all_snapshots():
        if s.total and s.total > 0:
            print(f"  {s.model_id:<60} {s.remaining:>12,}/{s.total:<12,} ({s.pct_remaining:5.1%})")
        else:
            print(f"  {s.model_id:<60} {'unlimited (paid)':>26}")

    target = "deepseek/deepseek-r1-0528"
    print(f"\n=== Consume 2M tokens on {target} ===")
    ok = qm.consume(target, 2_000_000)
    snap = qm.snapshot(target)
    print(f"  consume ok={ok}  remaining={snap.remaining:,}  pct={snap.pct_remaining:.2%}")

    print("\n=== Should block a 99M-token request? ===")
    print(f"  {qm.should_block(target, 99_000_000)}")

    print("\n=== Should block a 1k-token request? ===")
    print(f"  {qm.should_block(target, 1_000)}")

    print("\n=== Reset all quotas ===")
    n = qm.reset_all()
    print(f"  reset {n} models; deepseek remaining now = {qm.remaining(target):,}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
