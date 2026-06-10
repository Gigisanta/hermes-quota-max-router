"""Cron-friendly scripts — Phase 9.

Three operational scripts, all idempotent and safe to re-run:

  scripts/reset_quotas.py
    Resets all `daily_quota_tokens` entries to their `total` value.
    Intended to run at midnight (or per-provider reset times).

  scripts/auto_update.py
    Loads a feed file and applies it via RegistryUpdater. Intended
    to run every 48-72h (the spec's "Auto-Updater Agent" cadence).

  scripts/usage_report.py
    Reads `logs/router.jsonl` and prints a per-model usage summary.
    Intended to run daily/hourly for cost visibility.

All scripts use the same Redis-or-fakeredis fallback as the rest of
the system, so they work in dev with zero infrastructure.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def cmd_reset_quotas() -> int:
    """Reset all quotas to their totals."""
    from core.model_registry import ModelRegistry
    from core.quota_manager import QuotaManager

    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    n = qm.reset_all()
    print(f"[reset_quotas] reset {n} models at {datetime.now(timezone.utc).isoformat()}")
    return 0


def cmd_auto_update(feed_path: str) -> int:
    """Apply a feed to the registry.

    `feed_path` is interpreted as:
      - A local JSON file if it exists on disk
      - The string "live" / "discover" triggers RemoteFeedProvider
      - Otherwise, defaults to registry/feed_sample.json

    The merged registry is written to BOTH:
      - registry/discovered.json (newly discovered models only)
      - registry/models.json (curated + discovered merged)
    The curated seed itself is never mutated.
    """
    from core.model_registry import ModelRegistry
    from core.auto_updater import LocalFeedProvider, RegistryUpdater
    from core.layered_registry import DISCOVERED_PATH

    path = Path(feed_path)
    feed_models: list[dict]

    if feed_path in ("live", "discover", ":live", ":discover"):
        from core.remote_feeds import RemoteFeedProvider
        provider = RemoteFeedProvider(timeout_s=20.0)
        feed_models = provider.fetch_all()
        print(f"[auto_update] live discovery: {len(feed_models)} models from remote catalogs")
    elif path.exists() and path.is_file():
        feed_models = LocalFeedProvider(path).fetch()
    else:
        print(f"[auto_update] ❌ feed not found and not 'live': {path}", file=sys.stderr)
        return 1

    # Write discovered models to discovered.json (not the curated seed)
    discovered_registry = ModelRegistry(
        db_path=DISCOVERED_PATH.parent / "data" / "registry_discovered.sqlite",
        seed_path=None,  # start empty
    )
    updater = RegistryUpdater(discovered_registry, DISCOVERED_PATH)
    result = updater.apply_feed(feed_models)
    print(f"[auto_update] version: {result.old_version} → {result.new_version}")
    print(f"[auto_update] discovered.json: {len(result.added)} added, "
          f"{len(result.updated)} updated, {len(result.unchanged)} unchanged")
    if result.errors:
        print(f"[auto_update] errors: {result.errors}", file=sys.stderr)
        return 2

    # Also dump a snapshot of the merged registry (curated + discovered)
    # to a separate file for visibility.
    from core.layered_registry import LayeredRegistry, CURATED_PATH
    layered = LayeredRegistry.from_defaults(CURATED_PATH, DISCOVERED_PATH)
    merged_path = REPO_ROOT / "registry" / "merged.json"
    with open(merged_path, "w") as f:
        json.dump(
            {
                "version": result.new_version,
                "curated_count": layered.curated.count(),
                "discovered_count": layered.discovered.count(),
                "merged_count": layered.count(),
                "models": [m.to_dict() for m in layered.all()],
            },
            f, indent=2,
        )
    print(f"[auto_update] merged snapshot: {merged_path} ({layered.count()} models)")
    return 0


def cmd_usage_report(log_path: str = "logs/router.jsonl") -> int:
    """Per-model usage summary from the JSONL log.

    Relative paths are resolved against the current working directory
    (so cron jobs pointing at the project dir work as expected).
    """
    p = Path(log_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        print(f"[usage_report] no log file at {p}")
        return 0

    calls: dict[str, int] = defaultdict(int)
    tokens: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)
    fallbacks: int = 0
    paid_calls: int = 0
    total_duration: float = 0.0
    n = 0

    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            m = rec.get("model_used") or "(none)"
            calls[m] += 1
            tokens[m] += int(rec.get("total_tokens", 0))
            if rec.get("error"):
                errors[m] += 1
            if rec.get("fallback_used"):
                fallbacks += 1
            if not rec.get("preserve_paid_quota", True):
                paid_calls += 1
            total_duration += float(rec.get("duration_s", 0.0))

    print(f"=== Usage report ({n} calls) ===")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print(f"Fallbacks used: {fallbacks}")
    print(f"Paid-quota calls (preserve_paid_quota=false): {paid_calls}")
    if n:
        print(f"Avg latency: {total_duration / n * 1000:.1f} ms")
    print()
    print(f"{'Model':<55} {'Calls':>7} {'Tokens':>12} {'Errors':>7}")
    print("-" * 85)
    for m in sorted(calls.keys(), key=lambda k: -calls[k]):
        print(f"{m[:55]:<55} {calls[m]:>7} {tokens[m]:>12,} {errors[m]:>7}")
    return 0


# --- CLI ---

USAGE = """\
Usage:
  python -m scripts.operations reset-quotas
  python -m scripts.operations auto-update [FEED_PATH|live]
  python -m scripts.operations usage-report [LOG_PATH]

  'live' or 'discover' as FEED_PATH triggers RemoteFeedProvider:
  hits OpenRouter + HuggingFace + curated catalog.
"""


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    if cmd == "reset-quotas":
        return cmd_reset_quotas()
    if cmd == "auto-update":
        feed = argv[1] if len(argv) > 1 else str(REPO_ROOT / "registry" / "feed_sample.json")
        return cmd_auto_update(feed)
    if cmd == "usage-report":
        log = argv[1] if len(argv) > 1 else "logs/router.jsonl"
        return cmd_usage_report(log)
    print(f"unknown command: {cmd}\n{USAGE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
