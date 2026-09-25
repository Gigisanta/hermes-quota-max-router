#!/usr/bin/env python3
"""Freeze public editorial inputs without sending them to a model.

Simón's topic intake is not an evidence-complete author/reviewer suite: each
topic still needs independently checked sources and human clinical review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_free_reviewer import _write_report  # noqa: E402

ROOT = Path("/Users/gigi/HerMaatOS")
SIMON_DB = ROOT / "var/simon-science/engine.sqlite3"
CACTUS_AUDIT = ROOT / "work/maatwork-brand-repos/cactuswealth-market-brief/output/.audit"
CACTUS_PUBLISHED = ROOT / "work/maatwork-brand-repos/cactus-landing/brief"
SECONDARY = re.compile(
    r"\b(?:review|meta[- ]analysis|protocol|guideline|consensus|perspective|commentary)\b"
    r"|\bcase[- ](?:report|study)\b",
    re.I,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def simon_topics() -> dict[str, object]:
    if SIMON_DB.is_symlink() or not SIMON_DB.is_file():
        raise ValueError("simon_db_unavailable")
    connection = sqlite3.connect(f"{SIMON_DB.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT key, title, url, summary, published, source_id
               FROM candidate WHERE source_id LIKE 'europepmc-%'
               ORDER BY first_seen ASC, key ASC"""
        ).fetchall()
    finally:
        connection.close()
    eligible = []
    for row in rows:
        doi = str(row["key"] or "").lower().strip()
        title = str(row["title"] or "").strip()
        summary = str(row["summary"] or "").strip()
        url = str(row["url"] or "")
        if (
            not re.fullmatch(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", doi)
            or SECONDARY.search(title)
            or len(summary) < 180
            or url.lower() != "https://doi.org/" + doi
            or not str(row["source_id"]).startswith("europepmc-")
        ):
            continue
        eligible.append(
            {
                "id": doi,
                "title": title,
                "url": url,
                "public_abstract_excerpt": summary,
                "published": row["published"],
            }
        )
    if len(eligible) < 50:
        raise ValueError("fewer_than_50_original_public_doi_topics")
    cases = eligible[:50]
    if len({case["id"] for case in cases}) != 50:
        raise ValueError("duplicate_doi")
    raw = _canonical(cases)
    return {
        "workload": "simon-news",
        "stage": "topic_intake_only",
        "fixture_type": "public_europe_pmc_queue_excerpt",
        "selection": "first_seen_ascending_then_doi; original DOI papers; no favorable selection",
        "case_count": 50,
        "cases_sha256": _sha(raw),
        "cases": cases,
        "evidence_complete": False,
        "clinical_approval": False,
        "production_admission": False,
    }


def cactus_inventory() -> dict[str, object]:
    by_date = {}
    for root in (CACTUS_AUDIT, CACTUS_PUBLISHED):
        if not root.is_dir() or root.is_symlink():
            continue
        for directory in sorted(root.iterdir()):
            if (
                not directory.is_dir()
                or directory.name in by_date
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", directory.name)
            ):
                continue
            try:
                date.fromisoformat(directory.name)
            except ValueError:
                continue
            pair = [directory / "snapshot.json", directory / "brief.json"]
            if not all(path.is_file() and not path.is_symlink() for path in pair):
                continue
            try:
                snapshot, brief = (json.loads(path.read_bytes()) for path in pair)
            except (ValueError, OSError):
                continue
            if not _complete_cactus_pair(snapshot, brief):
                continue
            by_date[directory.name] = {
                "date": directory.name,
                "archive_origin": "audit" if root == CACTUS_AUDIT else "published_bundle",
                "snapshot_sha256": _sha(pair[0].read_bytes()),
                "brief_sha256": _sha(pair[1].read_bytes()),
            }
    entries = [by_date[date] for date in sorted(by_date)]
    weeks = {date.fromisoformat(entry["date"]).isocalendar()[:2] for entry in entries}
    raw = _canonical(entries)
    return {
        "workload": "cactus-brief",
        "stage": "historical_inventory_only",
        "available_complete_editions": len(entries),
        "available_complete_weeks": len(weeks),
        "required_complete_weeks": 10,
        "missing_weeks": max(0, 10 - len(weeks)),
        "entries_sha256": _sha(raw),
        "entries": entries,
        "source_news_verified": False,
        "evaluation_ready": False,
        "production_admission": False,
    }


def _complete_cactus_pair(snapshot: object, brief: object) -> bool:
    if not isinstance(snapshot, dict) or not isinstance(brief, dict):
        return False
    if (
        not all(
            isinstance(snapshot.get(key), dict) and snapshot[key]
            for key in ("dollar", "arg_stocks", "global_indices", "risk_pais", "ar_macro")
        )
        or not isinstance(snapshot.get("fetch_time"), str)
        or not snapshot["fetch_time"]
    ):
        return False
    if not isinstance(brief.get("narrative"), str) or not brief["narrative"].strip():
        return False
    if (
        not all(
            isinstance(brief.get(key), list) and brief[key]
            for key in ("themes", "stories", "watch_next")
        )
        or type(brief.get("used_llm")) is not bool
    ):
        return False
    return (
        all(
            isinstance(item, dict)
            and isinstance(item.get("title"), str)
            and isinstance(item.get("blurb"), str)
            for item in brief["themes"]
        )
        and all(
            isinstance(item, dict)
            and set(item)
            == {"headline", "what", "why", "source", "tier", "category", "url", "rank"}
            and all(
                isinstance(item[key], str) and item[key].strip()
                for key in ("headline", "what", "why", "source", "category", "url")
            )
            and type(item["tier"]) is int
            and type(item["rank"]) is int
            and item["url"].startswith("https://")
            for item in brief["stories"]
        )
        and all(isinstance(item, str) and item.strip() for item in brief["watch_next"])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists")
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "evaluation_only": True,
        "simon": simon_topics(),
        "cactus": cactus_inventory(),
    }
    _write_report(args.output, report)
    print(
        json.dumps(
            {
                "simon_topics": report["simon"]["case_count"],
                "simon_hash": report["simon"]["cases_sha256"],
                "cactus_weeks": report["cactus"]["available_complete_weeks"],
                "cactus_hash": report["cactus"]["entries_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
