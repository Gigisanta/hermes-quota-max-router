#!/usr/bin/env python3
"""Cactus author/proofreader calibration with six fictional scenarios.

These are additional synthetic cases, not missing historical editions. The
report holds only hashes and gate metadata; no output can reach publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
CACTUS_ROOT = Path("/Users/gigi/HerMaatOS/work/maatwork-brand-repos/cactuswealth-market-brief")
sys.path.insert(0, str(CACTUS_ROOT))
from src.cw.agent import prompts, proofread  # noqa: E402
from src.cw.agent.llm_client import (  # noqa: E402
    _ROUTER_NUMERIC_REVIEW_INSTRUCTION,
    _complete_editorial_review,
)
from src.cw.agent.reason import _brief_from_llm, _market_digest, _news_payload  # noqa: E402
from src.cw.agent.verify import verify  # noqa: E402
from src.cw.model.types import MarketSnapshot, NewsCandidate  # noqa: E402

from scripts.evaluate_free_reviewer import (  # noqa: E402
    VERIFIED_EVAL_TARGETS,
    FreeQuotaUnavailable,
    _check_simplellm_quota,
    _provider_eval_lock,
    _write_report,
)
from scripts.prepare_editorial_suites import _canonical, _sha  # noqa: E402

SCENARIOS = (
    ("tasas", "La tasa global ficticia sube y exige revisar la duración de bonos."),
    ("dolar", "El dólar ficticio baja y cambia el rendimiento del carry en pesos."),
    ("reservas", "Las reservas ficticias aumentan y mejora la liquidez externa."),
    ("acciones", "Las acciones ficticias caen y pesan sobre los CEDEARs."),
    ("inflacion", "La inflación ficticia cede y mueve la renta fija en pesos."),
    ("petroleo", "El petróleo ficticio sube y cambia las expectativas sobre energía."),
)
CATEGORIES = ("ARG", "GLOBAL", "TECH")


def _case(index: int) -> tuple[MarketSnapshot, list[NewsCandidate]]:
    slug, premise = SCENARIOS[index]
    snapshot = MarketSnapshot(
        dollar={
            name: {"venta": value + index * 10}
            for name, value in (("Oficial", 1000), ("Blue", 1080), ("MEP", 1060), ("CCL", 1070))
        },
        arg_stocks={"Merval": {"price": 2000000 + index * 10000, "change_1d": 0.5}},
        global_indices={
            name: {"price": value + index * 10, "change_1d": -0.5}
            for name, value in (
                ("S&P 500", 5500),
                ("Nasdaq", 18000),
                ("Dollar Index", 101),
                ("Oro", 2500),
            )
        },
        crypto={},
        risk_pais={"embi": 600 + index * 10},
        ar_macro={"inflacion_mensual": 2.0 + index * 0.1},
        fetch_time="2026-01-15T12:00:00Z",
        bonds={name: {"price": 70 + index, "change_1d": -0.2} for name in ("AL30", "GD30", "GD41")},
        reserves={"usd_millones": 40000 + index * 500},
        us_rates={
            name: {"price": value + index * 0.1}
            for name, value in (("3M", 4.0), ("5 años", 4.1), ("10 años", 4.2), ("30 años", 4.3))
        },
        cedear_volumen={"SPY": 100000000, "AAPL": 80000000, "NVDA": 70000000},
    )
    candidates = [
        NewsCandidate(
            title=f"Escenario ficticio {slug} {category} {part}: {premise}",
            source="Agencia de prueba ficticia",
            tier=1,
            url=f"https://example.org/cactus-fiction/{slug}/{category.lower()}/{part}",
            published_iso="2026-01-15",
            age_days=0.5,
            summary=(
                f"Simulación editorial; no es un hecho real. {premise} "
                f"El informe inventado de {category} número {part} describe un efecto "
                "sobre bonos, CEDEARs o liquidez sin cifras adicionales."
            ),
            category=category,
        )
        for category in CATEGORIES
        for part in range(1, 5)
    ]
    return snapshot, candidates


def _review_payload(index: int) -> dict:
    slug, premise = SCENARIOS[index]
    return {
        "narrative": f"La inflacion ficticia se analiza en {slug}. {premise}\n\n"
        "El escenario de EE.UU. cambia el costo del dinero.\n\n"
        "La tasa del Tesoro afecta a los bonos AL30 y conviene mirar duración.",
        "themes": [{"title": f"Escenario {slug}", "blurb": premise[:80]} for _ in range(3)],
        "stories": [
            {
                "headline": f"Escenario {slug} {i}",
                "what": premise,
                "why": "Los bonos AL30 pueden moverse por el costo del dinero; conviene revisar duración.",
            }
            for i in range(10)
        ],
        "watch_next": ["Seguir la tasa del Tesoro para evaluar bonos AL30."] * 3,
    }


def _parse(response: httpx.Response, model: str) -> tuple[dict, dict]:
    if response.status_code != 200:
        raise ValueError(f"http_{response.status_code}")
    raw = response.json()
    if raw.get("model") != model or raw["choices"][0].get("finish_reason") != "stop":
        raise ValueError("model_mismatch_or_incomplete")
    content = raw["choices"][0]["message"]["content"].strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("non_object_response")
    return payload, raw.get("usage") or {}


def _author_check(payload: dict, snapshot: MarketSnapshot, candidates: list[NewsCandidate]) -> dict:
    raw_stories = payload.get("stories")
    if not isinstance(raw_stories, list) or any(
        not isinstance(story, dict)
        or type(story.get("i")) is not int
        or not 0 <= story["i"] < len(candidates)
        for story in raw_stories
    ):
        raise ValueError("invalid_story_source_index")
    brief = _brief_from_llm(payload, snapshot, candidates)
    if brief is None:
        return {"product_gate_ok": False, "reason": "brief_parse_failed"}
    cleaned, warnings, ok = verify(brief)
    return {
        "product_gate_ok": ok,
        "stories_before": len(brief.stories),
        "stories_after": len(cleaned.stories),
        "warning_count": len(warnings),
        "critical_warning_count": sum(w.startswith("CRITICAL") for w in warnings),
        "source_urls_valid": all(
            story.url in {c.url for c in candidates} for story in brief.stories
        ),
    }


def _review_check(payload: dict, original: dict) -> dict:
    invariants_ok = _complete_editorial_review(payload, original)
    return {
        "review_invariants_ok": invariants_ok,
        "narrative_typo_corrected": "inflacion" not in str(payload.get("narrative", "")).lower(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=VERIFIED_EVAL_TARGETS, required=True)
    parser.add_argument("--stage", choices=("author", "reviewer"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    target = VERIFIED_EVAL_TARGETS[args.provider]
    key = os.getenv(str(target["env"]), "")
    if not key:
        parser.error(f"{target['env']} missing")
    if args.output.is_symlink():
        parser.error("symlink output")
    suite = [
        {"snapshot": asdict(_case(index)[0]), "candidates": [asdict(c) for c in _case(index)[1]]}
        for index in range(len(SCENARIOS))
    ]
    suite_hash = _sha(_canonical(suite))
    system = (
        prompts.SYSTEM
        if args.stage == "author"
        else f"{proofread._SYSTEM.rstrip()}\n\n{_ROUTER_NUMERIC_REVIEW_INSTRUCTION}"
    )
    prompt_hash = hashlib.sha256(system.encode()).hexdigest()
    report = {
        "workload": "cactus-brief",
        "stage": args.stage,
        "provider": args.provider,
        "model": target["model"],
        "suite_sha256": suite_hash,
        "prompt_sha256": prompt_hash,
        "case_count": len(SCENARIOS),
        "fixture_type": "fictional_calibration_not_historical",
        "evaluation_only": True,
        "production_admission": False,
        "started_at": datetime.now(UTC).isoformat(),
        "results": [],
    }
    if args.output.exists():
        if not args.resume:
            parser.error("output exists; use --resume")
        old = json.loads(args.output.read_bytes())
        if any(
            old.get(field) != report[field]
            for field in (
                "workload",
                "stage",
                "provider",
                "model",
                "suite_sha256",
                "prompt_sha256",
                "case_count",
            )
        ):
            parser.error("resume metadata mismatch")
        report = old
    by_id = {row["id"]: i for i, row in enumerate(report["results"])}
    if len(by_id) != len(report["results"]) or set(by_id) - set(range(len(SCENARIOS))):
        parser.error("invalid prior results")
    with (
        _provider_eval_lock(str(target.get("lock_provider", args.provider))),
        httpx.Client(timeout=httpx.Timeout(120, connect=10), trust_env=False) as client,
    ):
        for index in range(len(SCENARIOS)):
            if index in by_id and "error" not in report["results"][by_id[index]]:
                continue
            snapshot, candidates = _case(index)
            original = _review_payload(index)
            if args.stage == "author":
                user = (
                    prompts.INSTRUCTION.format(
                        market_digest=_market_digest(snapshot),
                        news_payload=_news_payload(candidates, snapshot=snapshot),
                    )
                    + "\n\nDATOS FICTICIOS DE CALIBRACIÓN: no son hechos ni cotizaciones reales."
                )
                max_tokens = 4500
            else:
                user = proofread._INSTRUCTION.format(
                    payload=json.dumps(original, ensure_ascii=False)
                )
                max_tokens = 2200
            item = {"id": index}
            started = time.monotonic()
            try:
                if args.provider == "simplellm":
                    _check_simplellm_quota(
                        client,
                        key,
                        input_bytes=len((system + user).encode()) + 128,
                        max_tokens=max_tokens,
                    )
                response = client.post(
                    str(target["url"]),
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": target["model"],
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0,
                        "stream": False,
                    },
                )
                payload, usage = _parse(response, str(target["model"]))
                item.update(
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
                item.update(
                    _author_check(payload, snapshot, candidates)
                    if args.stage == "author"
                    else _review_check(payload, original)
                )
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                FreeQuotaUnavailable,
            ) as exc:
                item["error"] = (
                    str(exc)
                    if isinstance(exc, (ValueError, FreeQuotaUnavailable))
                    else type(exc).__name__
                )
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if index in by_id:
                previous = report["results"][by_id[index]]
                item["attempts"] = previous.get("attempts", [previous.get("error")]) + [
                    item.get("error")
                ]
                report["results"][by_id[index]] = item
            else:
                item["attempts"] = [item.get("error")]
                by_id[index] = len(report["results"])
                report["results"].append(item)
            report["metrics"] = {
                "attempted": len(report["results"]),
                "errors": sum("error" in row for row in report["results"]),
                "passes": sum(
                    (
                        row.get("product_gate_ok") is True
                        if args.stage == "author"
                        else row.get("review_invariants_ok") is True
                    )
                    for row in report["results"]
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
    return (
        0
        if report["metrics"]["attempted"] == len(SCENARIOS)
        and not report["metrics"]["errors"]
        and report["metrics"]["passes"] == len(SCENARIOS)
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
