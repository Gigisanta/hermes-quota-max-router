#!/usr/bin/env python3
"""Offline editorial author comparison. Sends only pinned, fictional Journal evidence.

The output is metadata only. This script cannot publish or admit a router model.
The 60 cases are derived one-to-one from newsblog's frozen synthetic reviewer set.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from bin.newsblog.article import (  # noqa: E402
    parse_json_document,
    render_document,
    validate_document,
)
from bin.newsblog.gate import gate_report  # noqa: E402

from scripts.evaluate_free_reviewer import (  # noqa: E402
    FROZEN_JOURNAL_SUITE_SHA256,
    VERIFIED_EVAL_TARGETS,
    FreeQuotaUnavailable,
    _check_simplellm_quota,
    _provider_eval_lock,
    _write_report,
)

CASES = Path(__file__).resolve().parents[3] / "bin/newsblog/frozen_cases.json"
URL = "https://example.org/editorial-evaluation/fictional-evidence"
AUTHOR_EVALUATOR_VERSION = "journal-author-gates-v2"
USER_TEMPLATE = (
    "Tema: {topic}\nTítulo canónico: {topic}\n\n"
    "FUENTES:\n1. Informe ficticio | prueba sintética | {url}\n{evidence}\nFIN_FUENTES\n\n"
    "Escribí un artículo original en español sustentado sólo por la fuente. "
    "La fuente es inventada y sólo se usa para esta evaluación. "
    "Si la evidencia es escasa, escribí sólo lo sustentado sin rellenar. "
    'Respondé exclusivamente un objeto JSON con "schema":"newsblog.article.v2" y '
    "schema, kind=brief, title, standfirst, sections (2 a 6 objetos con heading, "
    "text y source_ids numéricos) y tags. Usá source_ids válidos de FUENTES. "
    "No uses el esquema title/paragraphs ni URLs en text."
)


def _journal_system() -> str:
    """Read the real author prompt without importing live Newsblog dependencies."""
    source = CASES.with_name("draft.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SYSTEM" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("journal_author_prompt_not_found")


JOURNAL_SYSTEM = _journal_system()

JOURNAL_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "newsblog_article",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "schema": {"type": "string", "enum": ["newsblog.article.v2"]},
                "kind": {"type": "string", "enum": ["brief"]},
                "title": {"type": "string"},
                "standfirst": {"type": "string"},
                "sections": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "text": {"type": "string"},
                            "source_ids": {
                                "type": "array",
                                "items": {"type": "integer", "enum": [1]},
                            },
                        },
                        "required": ["heading", "text", "source_ids"],
                        "additionalProperties": False,
                    },
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["schema", "kind", "title", "standfirst", "sections", "tags"],
            "additionalProperties": False,
        },
    },
}


def _numbers(text: str) -> set[str]:
    return {re.sub(r"\D", "", n) for n in re.findall(r"\d+(?:[.,]\d+)?\s*%?", text)}


def _check_document(content: str, evidence: str) -> dict[str, object]:
    raw = parse_json_document(content)
    document = validate_document(raw, [URL], allow_legacy=False)
    sections = document["sections"]
    body = " ".join(str(section["text"]) for section in sections)
    evidence_numbers = _numbers(evidence)
    invented_numbers = _numbers(body + " " + str(document["standfirst"])) - evidence_numbers
    if invented_numbers:
        raise ValueError("unsupported_number")
    if any("http" in str(section["text"]).lower() for section in sections):
        raise ValueError("url_in_article_text")
    product_checks = gate_report(
        render_document(document, [URL]),
        source_urls=[URL],
        evidence_text=evidence,
        source_blobs=[evidence],
        check_entities=True,
    )
    return {
        "sections": len(sections),
        "invented_numbers": 0,
        "valid_citations": True,
        "product_gate_ok": bool(product_checks["ok"]),
        "product_gate_failures": [
            name for name, result in product_checks["gates"].items() if not result["passed"]
        ],
    }


def _quality_result(checks: dict[str, object]) -> dict[str, bool]:
    """Keep format success separate from the product's editorial decision."""
    return {
        "structural_passed": True,
        "passed": checks.get("product_gate_ok") is True,
    }


def _response_content(response: httpx.Response, model: str) -> tuple[str, int | None, int | None]:
    if response.status_code != 200:
        raise ValueError(f"http_{response.status_code}")
    data = response.json()
    if data.get("model") != model:
        raise ValueError("model_mismatch")
    choice = data["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("incomplete_response")
    content = choice["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("missing_content")
    usage = data.get("usage") or {}
    return content, usage.get("prompt_tokens"), usage.get("completion_tokens")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=VERIFIED_EVAL_TARGETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=1400)
    parser.add_argument("--structured", action="store_true", help="test Google JSON schema mode")
    args = parser.parse_args()
    if not 1 <= args.limit <= 60 or not 512 <= args.max_tokens <= 2048:
        parser.error("invalid case or output limit")
    if args.output.is_symlink():
        parser.error("output must not be a symlink")
    target = VERIFIED_EVAL_TARGETS[args.provider]
    key = os.getenv(str(target["env"]), "")
    if not key:
        parser.error(f"{target['env']} is missing")
    raw = CASES.read_bytes()
    suite_hash = hashlib.sha256(raw).hexdigest()
    if suite_hash != FROZEN_JOURNAL_SUITE_SHA256:
        parser.error("frozen synthetic suite changed")
    cases = json.loads(raw)["cases"][: args.limit]
    if len({case["id"] for case in cases}) != len(cases):
        parser.error("duplicate case IDs")
    prompt_hash = hashlib.sha256(
        (
            JOURNAL_SYSTEM
            + USER_TEMPLATE
            + (json.dumps(JOURNAL_RESPONSE_FORMAT, sort_keys=True) if args.structured else "")
        ).encode()
    ).hexdigest()
    report = {
        "workload": "journal",
        "stage": "author",
        "provider": args.provider,
        "model": target["model"],
        "suite_sha256": suite_hash,
        "prompt_sha256": prompt_hash,
        "case_count": args.limit,
        "max_output_tokens": args.max_tokens,
        "structured_output": args.structured,
        "started_at": datetime.now(UTC).isoformat(),
        "evaluation_only": True,
        "production_admission": False,
        "fixture_type": "fictional",
        "evaluator_version": AUTHOR_EVALUATOR_VERSION,
        "results": [],
    }
    if args.output.exists():
        if not args.resume:
            parser.error("output exists; use --resume")
        prior_report = json.loads(args.output.read_text(encoding="utf-8"))
        for field in (
            "workload",
            "stage",
            "provider",
            "model",
            "suite_sha256",
            "prompt_sha256",
            "case_count",
            "max_output_tokens",
            "structured_output",
            "evaluator_version",
        ):
            if prior_report.get(field) != report[field]:
                parser.error("resume metadata mismatch")
        report = prior_report
    results = report.get("results")
    if not isinstance(results, list) or len({row.get("id") for row in results}) != len(results):
        parser.error("invalid existing results")
    indexed = {row["id"]: i for i, row in enumerate(results)}
    if set(indexed) - {case["id"] for case in cases}:
        parser.error("unexpected existing case ID")
    with (
        _provider_eval_lock(str(target.get("lock_provider", args.provider))),
        httpx.Client(timeout=httpx.Timeout(90, connect=10), trust_env=False) as client,
    ):
        for case in cases:
            if case["id"] in indexed and results[indexed[case["id"]]].get("error") not in (
                "http_429",
                "http_503",
                "ReadTimeout",
                "ConnectTimeout",
            ):
                continue
            started = time.monotonic()
            item: dict[str, object] = {"id": case["id"]}
            prompt = USER_TEMPLATE.format(topic=case["topic"], url=URL, evidence=case["evidence"])
            try:
                if args.provider == "simplellm":
                    _check_simplellm_quota(
                        client,
                        key,
                        input_bytes=len((JOURNAL_SYSTEM + prompt).encode()) + 128,
                        max_tokens=args.max_tokens,
                    )
                payload = {
                    "model": target["model"],
                    "messages": [
                        {"role": "system", "content": JOURNAL_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": args.max_tokens,
                    "temperature": 0,
                    "stream": False,
                }
                if args.structured:
                    if args.provider not in ("gemini", "gemini-3.1"):
                        parser.error("structured mode is only verified for Gemini")
                    payload["response_format"] = JOURNAL_RESPONSE_FORMAT
                response = client.post(
                    str(target["url"]),
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
                content, input_tokens, output_tokens = _response_content(
                    response, str(target["model"])
                )
                item.update(prompt_tokens=input_tokens, completion_tokens=output_tokens)
                try:
                    item.update(_check_document(content, case["evidence"]))
                except ValueError:
                    try:
                        shape = parse_json_document(content)
                        if isinstance(shape, dict):
                            item["response_keys"] = sorted(str(key) for key in shape)
                            item["response_schema"] = str(shape.get("schema", ""))[:80]
                    except (ValueError, TypeError):
                        item["response_keys"] = []
                    raise
                item.update(_quality_result(item))
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                FreeQuotaUnavailable,
            ) as exc:
                item.update(
                    passed=False,
                    error=(
                        str(exc)
                        if isinstance(exc, (ValueError, FreeQuotaUnavailable))
                        else type(exc).__name__
                    ),
                )
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if case["id"] in indexed:
                previous = results[indexed[case["id"]]]
                item["attempts"] = previous.get(
                    "attempts",
                    [
                        {
                            "at": report.get("started_at"),
                            "error": previous.get("error"),
                            "elapsed_seconds": previous.get("elapsed_seconds"),
                        }
                    ],
                ) + [
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "error": item.get("error"),
                        "elapsed_seconds": item["elapsed_seconds"],
                    }
                ]
                results[indexed[case["id"]]] = item
            else:
                item["attempts"] = [
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "error": item.get("error"),
                        "elapsed_seconds": item["elapsed_seconds"],
                    }
                ]
                indexed[case["id"]] = len(results)
                results.append(item)
            report["metrics"] = {
                "attempted": len(results),
                "passed": sum(row.get("passed") is True for row in results),
                "failed": sum(row.get("passed") is False for row in results),
                "structural_passed": sum(row.get("structural_passed") is True for row in results),
                "attempts": sum(len(row.get("attempts", [])) for row in results),
                "api_failures": sum(
                    attempt.get("error")
                    in ("http_429", "http_503", "ReadTimeout", "ConnectTimeout")
                    for row in results
                    for attempt in row.get("attempts", [])
                ),
            }
            _write_report(args.output, report)
            print(
                f"{args.provider} author {len(results)}/{len(cases)} passed={report['metrics']['passed']}",
                flush=True,
            )
            if item.get("error") in ("http_429", "http_503", "ReadTimeout", "ConnectTimeout"):
                break
            remaining = float(target["min_interval_seconds"]) - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    return 0 if len(results) == len(cases) and report["metrics"]["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
