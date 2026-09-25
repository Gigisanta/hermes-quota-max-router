#!/usr/bin/env python3
"""Capture one read-only daily router sample for the seven-day editorial pilot."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_free_reviewer import _write_report  # noqa: E402

BASE = "http://127.0.0.1:8123/v1/router"
WORKLOADS = ("journal", "simon-news", "cactus-brief")


def _read(name: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/{name}", timeout=5) as response:  # noqa: S310
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError(f"invalid_{name}")
    return data


def _sample(
    status: dict, metrics: dict, *, daily_spend_usd: float | None, gemini_used_rpd: int | None
) -> dict:
    return {
        "sampled_at": datetime.now(UTC).isoformat(),
        "pilot_only": True,
        "workloads": {
            name: {
                "ready": status.get("workloads", {}).get(name, {}).get("ready"),
                "providers": status.get("workloads", {}).get(name, {}).get("providers"),
                "measured_peak_requests": status.get("workloads", {})
                .get(name, {})
                .get("measured_peak_requests"),
                "planned_tokens_per_request": status.get("workloads", {})
                .get(name, {})
                .get("planned_tokens_per_request"),
            }
            for name in WORKLOADS
        },
        "router_daily_requests": metrics.get("daily_requests"),
        "router_events_count": len(metrics.get("events", [])),
        "router_queue": metrics.get("queue"),
        "gemini_used_rpd_observed": gemini_used_rpd,
        "daily_spend_usd_observed": daily_spend_usd,
        "spend_verified_zero": daily_spend_usd == 0 if daily_spend_usd is not None else None,
        "source_note": "AI Studio quota/Spend values require manual observed input; null is unknown",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--daily-spend-usd", type=float)
    parser.add_argument("--gemini-used-rpd", type=int)
    args = parser.parse_args()
    if args.daily_spend_usd is not None and args.daily_spend_usd < 0:
        parser.error("negative spend")
    if args.gemini_used_rpd is not None and args.gemini_used_rpd < 0:
        parser.error("negative RPD")
    day = datetime.now(UTC).date().isoformat()
    output = args.output or Path(f"var/evals/editorial-pilot-{day}.json")
    if output.exists() or output.is_symlink():
        parser.error("output already exists; preserve the original daily observation")
    try:
        status, metrics = _read("status"), _read("metrics")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"router sample unavailable: {type(exc).__name__}")
    report = _sample(
        status,
        metrics,
        daily_spend_usd=args.daily_spend_usd,
        gemini_used_rpd=args.gemini_used_rpd,
    )
    _write_report(output, report)
    print(
        json.dumps(
            {"date": day, "path": str(output), "spend_verified_zero": report["spend_verified_zero"]}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
