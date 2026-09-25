#!/usr/bin/env python3
"""Collect public Simón evidence for a pinned topic intake, without model calls.

Uses the product's own robots/rate-limit and evidence selection logic. Output
contains public article excerpts and must remain in ignored private var/evals.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from bin.newsblog.fetch import fetch  # noqa: E402
from bin.newsblog.robots import HostRateLimiter, RobotsCache  # noqa: E402
from bin.simon_science.engine import evidence_for  # noqa: E402

from scripts.evaluate_free_reviewer import _write_report  # noqa: E402
from scripts.prepare_editorial_suites import SIMON_DB, _canonical, _sha  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if args.output.is_symlink() or args.manifest.is_symlink():
        parser.error("symlink not allowed")
    if not 1 <= args.limit <= 50:
        parser.error("invalid limit")
    manifest = json.loads(args.manifest.read_bytes())
    intake = manifest["simon"]
    cases = intake["cases"]
    if intake.get("case_count") != 50 or len(cases) != 50:
        parser.error("invalid topic intake")
    if _sha(_canonical(cases)) != intake.get("cases_sha256"):
        parser.error("topic intake hash mismatch")
    report = {
        "workload": "simon-news",
        "stage": "public_evidence_collection_only",
        "topic_sha256": intake["cases_sha256"],
        "started_at": datetime.now(UTC).isoformat(),
        "evaluation_only": True,
        "clinical_approval": False,
        "production_admission": False,
        "results": [],
    }
    if args.output.exists():
        if not args.resume:
            parser.error("output exists; use --resume")
        prior = json.loads(args.output.read_bytes())
        if (
            prior.get("topic_sha256") != intake["cases_sha256"]
            or prior.get("stage") != report["stage"]
        ):
            parser.error("resume metadata mismatch")
        report = prior
    prior = {row["id"]: index for index, row in enumerate(report["results"])}
    if len(prior) != len(report["results"]) or set(prior) - {row["id"] for row in cases}:
        parser.error("invalid prior results")
    connection = sqlite3.connect(f"{SIMON_DB.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    robots = RobotsCache(fetch)
    limiter = HostRateLimiter(host_interval_seconds=1)
    try:
        for case in cases[: args.limit]:
            if case["id"] in prior and "error" not in report["results"][prior[case["id"]]]:
                continue
            row = connection.execute(
                "SELECT * FROM candidate WHERE key=?", (case["id"],)
            ).fetchone()
            if (
                row is None
                or str(row["title"] or "").strip() != case["title"]
                or str(row["summary"] or "").strip() != case["public_abstract_excerpt"]
            ):
                parser.error("topic source drift; do not evaluate this manifest")
            started = time.monotonic()
            item = {"id": case["id"], "collected_at": datetime.now(UTC).isoformat()}
            try:
                sources = evidence_for(row, robots, limiter)
                work_ids = {source["workId"] for source in sources}
                ready = len(work_ids) >= 2 and any(
                    source["primary"] and source["kind"] == "peer-reviewed" for source in sources
                )
                item.update(
                    source_count=len(sources),
                    independent_source_count=len(work_ids),
                    evidence_ready=ready,
                    sources=sources,
                    sources_sha256=_sha(_canonical(sources)),
                )
            except (OSError, ValueError, TimeoutError) as exc:
                item["error"] = type(exc).__name__
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if case["id"] in prior:
                previous = report["results"][prior[case["id"]]]
                item["attempts"] = previous.get("attempts", [previous.get("error")]) + [
                    item.get("error")
                ]
                report["results"][prior[case["id"]]] = item
            else:
                item["attempts"] = [item.get("error")]
                prior[case["id"]] = len(report["results"])
                report["results"].append(item)
            report["metrics"] = {
                "examined": len(report["results"]),
                "evidence_ready": sum(r.get("evidence_ready") is True for r in report["results"]),
                "insufficient": sum(r.get("evidence_ready") is False for r in report["results"]),
                "errors": sum("error" in r for r in report["results"]),
            }
            _write_report(args.output, report)
            print(json.dumps(report["metrics"]), flush=True)
            if "error" in item:
                break
    finally:
        connection.close()
    return (
        0
        if report["metrics"]["examined"] == 50 and report["metrics"]["evidence_ready"] == 50
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
