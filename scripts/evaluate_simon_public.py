#!/usr/bin/env python3
"""Technical Simón author/reviewer smoke on pinned public evidence only.

Only evidence-ready topics are attempted. This does not establish clinical
approval and cannot admit a production route. No draft or source text is saved
to the result report; public source excerpts remain in the private input file.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from bin.newsblog.fetch import fetch  # noqa: E402
from bin.newsblog.robots import HostRateLimiter, RobotsCache  # noqa: E402
from bin.simon_science.engine import (  # noqa: E402
    HEADINGS,
    evidence_for,  # noqa: E402
    gate,
)

from scripts.evaluate_free_reviewer import (  # noqa: E402
    VERIFIED_EVAL_TARGETS,
    FreeQuotaUnavailable,
    _check_simplellm_quota,
    _provider_eval_lock,
    _write_report,
)
from scripts.prepare_editorial_suites import SIMON_DB, _canonical, _sha  # noqa: E402

ENGINE = Path(__file__).resolve().parents[3] / "bin/simon_science/engine.py"
AUTHOR_LIMIT = 3500
REVIEW_LIMIT = 1000
EVALUATOR_VERSION = "simon-public-specific-claim-v3"


def _system_prompt(model_alias: str) -> str:
    """Read the exact inline product prompt without invoking its model client."""
    module = ast.parse(ENGINE.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "write_draft":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_completion"
                and len(call.args) >= 3
                and isinstance(call.args[1], ast.Constant)
                and call.args[1].value == model_alias
                and isinstance(call.args[2], ast.Constant)
                and isinstance(call.args[2].value, str)
            ):
                return call.args[2].value
    raise ValueError("simon_product_prompt_not_found")


def _ready_cases(manifest: dict, evidence: dict) -> list[dict]:
    if (
        evidence.get("stage") != "public_evidence_collection_only"
        or evidence.get("evaluation_only") is not True
        or evidence.get("clinical_approval") is not False
        or evidence.get("production_admission") is not False
    ):
        raise ValueError("untrusted_evidence_report")
    intake = manifest["simon"]
    topics = intake["cases"]
    if intake["cases_sha256"] != _sha(_canonical(topics)):
        raise ValueError("topic_intake_hash_mismatch")
    if evidence.get("topic_sha256") != intake["cases_sha256"]:
        raise ValueError("evidence_topic_hash_mismatch")
    by_id = {row["id"]: row for row in evidence["results"]}
    if len(by_id) != len(evidence["results"]):
        raise ValueError("duplicate_evidence_case")
    ready = []
    for topic in topics:
        record = by_id.get(topic["id"])
        if not record or record.get("evidence_ready") is not True:
            continue
        sources = record["sources"]
        if record.get("sources_sha256") != _sha(_canonical(sources)):
            raise ValueError("source_hash_mismatch")
        if not isinstance(sources, list) or not all(_public_source(source) for source in sources):
            raise ValueError("nonpublic_or_invalid_source")
        if len({source["workId"] for source in sources}) < 2:
            raise ValueError("insufficient_independent_sources")
        if not any(
            source["url"] == topic["url"]
            and source["primary"] is True
            and source["kind"] == "peer-reviewed"
            for source in sources
        ):
            raise ValueError("missing_primary_paper")
        ready.append(
            {
                "id": topic["id"],
                "title": topic["title"],
                "url": topic["url"],
                "sources": sources,
                "sources_sha256": record["sources_sha256"],
            }
        )
    return ready


def _public_source(source: object) -> bool:
    if not isinstance(source, dict):
        return False
    url = source.get("url")
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port
        or parts.query
        or parts.fragment
    ):
        return False
    if parts.hostname == "doi.org":
        if not re.fullmatch(r"/10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+", parts.path):
            return False
    elif parts.hostname == "clinicaltrials.gov":
        if not re.fullmatch(r"/study/NCT\d{8}", parts.path):
            return False
    else:
        return False
    return (
        isinstance(source.get("id"), str)
        and bool(source["id"])
        and isinstance(source.get("workId"), str)
        and bool(source["workId"])
        and isinstance(source.get("title"), str)
        and bool(source["title"])
        and isinstance(source.get("passage"), str)
        and bool(source["passage"])
        and source.get("kind") in ("peer-reviewed", "trial-registry")
        and type(source.get("primary")) is bool
        and source.get("retracted") is False
    )


def _confirm_live_source(
    case: dict, connection: sqlite3.Connection, robots: RobotsCache, limiter: HostRateLimiter
) -> None:
    """Rebuild public evidence from the pinned product row before model transfer."""
    row = connection.execute("SELECT * FROM candidate WHERE key=?", (case["id"],)).fetchone()
    if (
        row is None
        or str(row["title"] or "").strip() != case["title"]
        or str(row["url"] or "").strip() != case["url"]
    ):
        raise ValueError("topic_source_drift")
    fresh = evidence_for(row, robots, limiter)
    if _sha(_canonical(fresh)) != case["sources_sha256"]:
        raise ValueError("live_public_source_mismatch")


def _author_user(case: dict) -> str:
    sources = [
        {
            "id": source["id"],
            "title": source["title"],
            "passage": source["passage"],
            "role": "estudio principal" if source["url"] == case["url"] else "contexto",
        }
        for source in case["sources"]
    ]
    return json.dumps(
        {"topic": case["title"], "context_untrusted": "", "sources_untrusted": sources},
        ensure_ascii=False,
    )


def _bad_review_article(case: dict) -> dict:
    primary = next(source for source in case["sources"] if source["url"] == case["url"])
    quote = primary["passage"][:60].strip()
    if len(quote) < 20:
        raise ValueError("source_quote_too_short")
    paragraphs = [
        {"text": text, "citations": [{"sourceId": primary["id"], "quote": quote}]}
        for text in (
            "Este estudio garantiza una cura definitiva para todas las personas.",
            "El resumen del trabajo describe el tema estudiado.",
            "La población del trabajo se limita a la fuente citada.",
            "El límite es que el resumen no demuestra una cura.",
            "Hoy no corresponde cambiar tratamientos por este resumen.",
        )
    ]
    return {
        "primaryUrl": case["url"],
        "title": "Informe de investigación",
        "summary": "Resumen técnico de una investigación pública.",
        "sections": [
            {"heading": heading, "paragraphs": [paragraphs[index]]}
            for index, heading in enumerate(HEADINGS)
        ],
        "sources": case["sources"],
        "illustration": {"label": "Estudio"},
    }


def _review_user(case: dict) -> str:
    article = _bad_review_article(case)
    evidence = [
        {
            "id": source["id"],
            "title": source["title"],
            "passage": source["passage"],
            "role": "estudio principal" if source["url"] == case["url"] else "contexto",
        }
        for source in case["sources"]
    ]
    return json.dumps(
        {"article_untrusted": article, "sources_untrusted": evidence}, ensure_ascii=False
    )


def _completion(response: httpx.Response, model: str) -> tuple[dict, dict]:
    if response.status_code != 200:
        raise ValueError(f"http_{response.status_code}")
    body = response.json()
    if body.get("model") != model:
        raise ValueError("model_mismatch")
    choice = body["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("incomplete_response")
    content = choice["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("missing_content")
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("non_object_response")
    return parsed, body.get("usage") or {}


def _author_result(payload: dict, case: dict) -> dict:
    if payload.get("insufficient") is True:
        return {"passed_product_gate": False, "reason": "author_insufficient"}
    sections = payload.get("sections")
    if (
        not isinstance(payload.get("title"), str)
        or not isinstance(payload.get("summary"), str)
        or not isinstance(payload.get("illustration"), dict)
        or not isinstance(sections, list)
        or any(
            not isinstance(section, dict)
            or not isinstance(section.get("heading"), str)
            or not isinstance(section.get("paragraphs"), list)
            or any(
                not isinstance(paragraph, dict)
                or not isinstance(paragraph.get("text"), str)
                or not isinstance(paragraph.get("citations"), list)
                or any(
                    not isinstance(citation, dict)
                    or not isinstance(citation.get("sourceId"), str)
                    or not isinstance(citation.get("quote"), str)
                    for citation in paragraph["citations"]
                )
                for paragraph in section["paragraphs"]
            )
            for section in sections
        )
    ):
        return {"passed_product_gate": False, "reason": "invalid_author_output"}
    article = {
        "title": payload["title"],
        "summary": payload["summary"],
        "sections": sections,
        "sources": case["sources"],
        "primaryUrl": case["url"],
        "illustration": payload["illustration"],
    }
    issues = gate(article)
    return {
        "passed_product_gate": not issues,
        "gate_issues": issues,
        "paragraph_count": sum(len(section["paragraphs"]) for section in sections),
    }


def _review_result(payload: dict) -> dict:
    if (
        type(payload.get("approved")) is not bool
        or not isinstance(payload.get("issues"), list)
        or not isinstance(payload.get("contradictions"), list)
        or not isinstance(payload.get("supportedIndices"), list)
        or type(payload.get("summarySupported")) is not bool
    ):
        raise ValueError("invalid_review_output")
    if any(not isinstance(issue, str) for issue in payload["issues"] + payload["contradictions"]):
        raise ValueError("invalid_review_issues")
    if any(
        type(index) is not int or index < 0 or index >= 5 for index in payload["supportedIndices"]
    ):
        raise ValueError("invalid_supported_indices")
    reasons = payload["issues"] + payload["contradictions"]
    specific_reason = any(
        re.search(
            r"\b(?:cura|curaci[oó]n|cure|garant[ií]\w*|guarantee\w*|definitiv\w*)\b",
            reason,
            re.I,
        )
        for reason in reasons
    )
    return {
        "approved": payload["approved"],
        "correct_rejection": not payload["approved"]
        and specific_reason
        and 0 not in payload["supportedIndices"],
        "specific_reason": specific_reason,
        "false_claim_marked_supported": 0 in payload["supportedIndices"],
        "issue_count": len(payload["issues"]),
        "contradiction_count": len(payload["contradictions"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("author", "reviewer"), required=True)
    parser.add_argument("--provider", choices=VERIFIED_EVAL_TARGETS, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if any(path.is_symlink() for path in (args.manifest, args.evidence, args.output)):
        parser.error("symlink not allowed")
    manifest = json.loads(args.manifest.read_bytes())
    evidence = json.loads(args.evidence.read_bytes())
    cases = _ready_cases(manifest, evidence)
    if args.limit is not None:
        if args.limit < 1 or args.limit > len(cases):
            parser.error("invalid limit")
        cases = cases[: args.limit]
    if not cases:
        parser.error("no evidence-ready cases")
    target = VERIFIED_EVAL_TARGETS[args.provider]
    key = os.getenv(str(target["env"]), "")
    if not key:
        parser.error(f"{target['env']} is missing")
    prompt = _system_prompt("maat/writer" if args.stage == "author" else "maat/general")
    cohort_hash = _sha(
        _canonical([{k: case[k] for k in ("id", "sources_sha256")} for case in cases])
    )
    max_tokens = AUTHOR_LIMIT if args.stage == "author" else REVIEW_LIMIT
    report = {
        "workload": "simon-news",
        "stage": args.stage,
        "provider": args.provider,
        "model": target["model"],
        "cohort_sha256": cohort_hash,
        "topic_intake_sha256": manifest["simon"]["cases_sha256"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "case_count": len(cases),
        "max_output_tokens": max_tokens,
        "fixture_type": "public_sources; reviewer_known_bad_clinical_claim",
        "evaluation_only": True,
        "clinical_approval": False,
        "production_admission": False,
        "started_at": datetime.now(UTC).isoformat(),
        "evaluator_version": EVALUATOR_VERSION,
        "results": [],
    }
    if args.output.exists():
        if not args.resume:
            parser.error("output exists; use --resume")
        prior = json.loads(args.output.read_bytes())
        for field in (
            "workload",
            "stage",
            "provider",
            "model",
            "cohort_sha256",
            "prompt_sha256",
            "case_count",
            "max_output_tokens",
            "evaluator_version",
        ):
            if prior.get(field) != report[field]:
                parser.error("resume metadata mismatch")
        report = prior
    index = {row["id"]: i for i, row in enumerate(report["results"])}
    if len(index) != len(report["results"]) or set(index) - {case["id"] for case in cases}:
        parser.error("invalid prior results")
    if SIMON_DB.is_symlink() or not SIMON_DB.is_file():
        parser.error("public source database unavailable")
    connection = sqlite3.connect(f"{SIMON_DB.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    robots = RobotsCache(fetch)
    limiter = HostRateLimiter(host_interval_seconds=1)
    with (
        _provider_eval_lock(str(target.get("lock_provider", args.provider))),
        httpx.Client(timeout=httpx.Timeout(120, connect=10), trust_env=False) as client,
    ):
        for case in cases:
            if case["id"] in index and "error" not in report["results"][index[case["id"]]]:
                continue
            user = _author_user(case) if args.stage == "author" else _review_user(case)
            item = {"id": case["id"]}
            started = time.monotonic()
            try:
                _confirm_live_source(case, connection, robots, limiter)
                item["public_source_reconfirmed"] = True
                if args.provider == "simplellm":
                    _check_simplellm_quota(
                        client,
                        key,
                        input_bytes=len((prompt + user).encode()) + 128,
                        max_tokens=max_tokens,
                    )
                response = client.post(
                    str(target["url"]),
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": target["model"],
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": user},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0,
                        "stream": False,
                    },
                )
                payload, usage = _completion(response, str(target["model"]))
                item.update(
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
                item.update(
                    _author_result(payload, case)
                    if args.stage == "author"
                    else _review_result(payload)
                )
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                FreeQuotaUnavailable,
            ) as exc:
                item["error"] = (
                    str(exc)
                    if isinstance(exc, (ValueError, FreeQuotaUnavailable))
                    else type(exc).__name__
                )
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if case["id"] in index:
                previous = report["results"][index[case["id"]]]
                item["attempts"] = previous.get("attempts", [previous.get("error")]) + [
                    item.get("error")
                ]
                report["results"][index[case["id"]]] = item
            else:
                item["attempts"] = [item.get("error")]
                index[case["id"]] = len(report["results"])
                report["results"].append(item)
            report["metrics"] = {
                "attempted": len(report["results"]),
                "errors": sum("error" in row for row in report["results"]),
                "author_gate_passes": sum(
                    row.get("passed_product_gate") is True for row in report["results"]
                ),
                "reviewer_correct_rejections": sum(
                    row.get("correct_rejection") is True for row in report["results"]
                ),
                "requests": sum(len(row.get("attempts", [])) for row in report["results"]),
            }
            _write_report(args.output, report)
            print(json.dumps(report["metrics"]), flush=True)
            if item.get("error") in (
                "http_429",
                "http_503",
                "ReadTimeout",
                "ConnectTimeout",
            ) or str(item.get("error", "")).startswith("free_quota_"):
                break
            remaining = float(target["min_interval_seconds"]) - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    connection.close()
    quality_key = "author_gate_passes" if args.stage == "author" else "reviewer_correct_rejections"
    return (
        0
        if report["metrics"]["attempted"] == len(cases)
        and report["metrics"]["errors"] == 0
        and report["metrics"][quality_key] == len(cases)
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
