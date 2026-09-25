#!/usr/bin/env python3
"""Rerun Cactus' own verifier on complete archived briefs, without publication.

Run with the Cactus project Python environment. This is a historical baseline,
not a Gemini author/reviewer test or proof that source news is verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

CACTUS_ROOT = Path("/Users/gigi/HerMaatOS/work/maatwork-brand-repos/cactuswealth-market-brief")
PUBLISHED_ROOT = Path("/Users/gigi/HerMaatOS/work/maatwork-brand-repos/cactus-landing/brief")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(CACTUS_ROOT))
from src.cw.agent.verify import verify  # noqa: E402
from src.cw.model.types import Brief, MarketSnapshot, Story, Theme  # noqa: E402

from scripts.prepare_editorial_suites import _complete_cactus_pair  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-root", type=Path, default=CACTUS_ROOT / "output/.audit")
    parser.add_argument("--published-root", type=Path, default=PUBLISHED_ROOT)
    args = parser.parse_args()
    if (
        args.output.exists()
        or args.output.is_symlink()
        or args.audit_root.is_symlink()
        or args.published_root.is_symlink()
    ):
        parser.error("output exists or symlink is not allowed")
    entries = []
    seen_dates = set()
    for directory in [*sorted(args.audit_root.iterdir()), *sorted(args.published_root.iterdir())]:
        if not directory.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", directory.name):
            continue
        try:
            date.fromisoformat(directory.name)
        except ValueError:
            continue
        if directory.name in seen_dates:
            continue
        source = directory / "snapshot.json"
        article = directory / "brief.json"
        if (
            not source.is_file()
            or not article.is_file()
            or source.is_symlink()
            or article.is_symlink()
        ):
            continue
        snapshot_raw = source.read_bytes()
        brief_raw = article.read_bytes()
        try:
            snapshot_data = json.loads(snapshot_raw)
            data = json.loads(brief_raw)
        except ValueError:
            continue
        if not _complete_cactus_pair(snapshot_data, data):
            continue
        seen_dates.add(directory.name)
        snapshot = MarketSnapshot(**snapshot_data)
        brief = Brief(
            narrative=data["narrative"],
            themes=tuple(Theme(**theme) for theme in data["themes"]),
            stories=tuple(Story(**story) for story in data["stories"]),
            watch_next=tuple(data["watch_next"]),
            snapshot=snapshot,
            used_llm=bool(data["used_llm"]),
        )
        cleaned, warnings, ok = verify(brief)
        entries.append(
            {
                "date": directory.name,
                "snapshot_sha256": hashlib.sha256(snapshot_raw).hexdigest(),
                "brief_sha256": hashlib.sha256(brief_raw).hexdigest(),
                "product_verifier_ok": ok,
                "stories_before": len(brief.stories),
                "stories_after": len(cleaned.stories),
                "warning_count": len(warnings),
                "critical_warning_count": sum(item.startswith("CRITICAL") for item in warnings),
            }
        )
    weeks = {date.fromisoformat(entry["date"]).isocalendar()[:2] for entry in entries}
    report = {
        "workload": "cactus-brief",
        "stage": "historical_product_verifier_baseline",
        "created_at": datetime.now(UTC).isoformat(),
        "evaluation_only": True,
        "production_admission": False,
        "source_news_verified": False,
        "required_complete_weeks": 10,
        "available_complete_editions": len(entries),
        "available_complete_weeks": len(weeks),
        "entries": entries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{args.output.name}.", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(
        json.dumps(
            {
                "available_complete_weeks": len(weeks),
                "available_complete_editions": len(entries),
                "product_verifier_ok": sum(item["product_verifier_ok"] for item in entries),
                "stories_dropped": sum(
                    item["stories_before"] - item["stories_after"] for item in entries
                ),
            }
        )
    )
    return 0 if len(weeks) >= 10 and all(item["product_verifier_ok"] for item in entries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
