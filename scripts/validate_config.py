"""Config validation — Phase 10.

Runs at server startup (or on-demand) to catch misconfigurations BEFORE
the first request hits the router. Three categories of checks:

  1. config/config.yaml: parses, has model_list, all entries have litellm_params
  2. registry/models.json: parses, has version, every model_id is unique,
     every model has the required fields
  3. Redis connectivity: try to ping, fall back to fakeredis with a loud warning

Returns a structured ValidationReport; never raises on a recoverable
issue (that's logged), but exits non-zero on hard failures.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


REQUIRED_MODEL_FIELDS = {
    "model_id", "provider", "display_name", "context_window",
    "input_price", "output_price", "is_free", "tier_rank",
    "strength_tags", "weakness_tags", "best_for", "performance_score",
}


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    models_loaded: int = 0

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_info(self, msg: str) -> None:
        self.info.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
            "models_loaded": self.models_loaded,
        }


def validate_config_yaml(path: Path = REPO_ROOT / "config" / "config.yaml",
                        report: ValidationReport | None = None) -> ValidationReport:
    report = report or ValidationReport()
    if not path.exists():
        report.add_error(f"config.yaml not found at {path}")
        return report
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as e:
        report.add_error(f"config.yaml parse error: {e}")
        return report

    if "model_list" not in cfg:
        report.add_error("config.yaml missing 'model_list'")
        return report

    if not isinstance(cfg["model_list"], list) or not cfg["model_list"]:
        report.add_error("config.yaml 'model_list' must be a non-empty list")
        return report

    seen: set[str] = set()
    for entry in cfg["model_list"]:
        if "model_name" not in entry:
            report.add_error(f"model_list entry missing 'model_name': {entry}")
            continue
        if "litellm_params" not in entry:
            report.add_error(f"model_list entry missing 'litellm_params': {entry['model_name']}")
            continue
        if entry["model_name"] in seen:
            report.add_warning(f"duplicate model_name: {entry['model_name']}")
        seen.add(entry["model_name"])

    report.add_info(f"config.yaml: {len(cfg['model_list'])} model_list entries")
    return report


def validate_models_json(path: Path = REPO_ROOT / "registry" / "models.json",
                         report: ValidationReport | None = None) -> ValidationReport:
    report = report or ValidationReport()
    if not path.exists():
        report.add_error(f"models.json not found at {path}")
        return report
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        report.add_error(f"models.json parse error: {e}")
        return report

    if "models" not in data or not isinstance(data["models"], list):
        report.add_error("models.json missing 'models' list")
        return report

    seen: set[str] = set()
    for m in data["models"]:
        if not isinstance(m, dict):
            report.add_error(f"model entry is not a dict: {m!r}")
            continue
        mid = m.get("model_id")
        if not mid:
            report.add_error(f"model entry missing 'model_id': {m!r}")
            continue
        if mid in seen:
            report.add_error(f"duplicate model_id: {mid}")
        seen.add(mid)
        missing = REQUIRED_MODEL_FIELDS - set(m.keys())
        if missing:
            report.add_error(f"model {mid} missing fields: {sorted(missing)}")
        if not isinstance(m.get("is_free"), bool):
            report.add_error(f"model {mid} is_free is not a bool")
        if not isinstance(m.get("tier_rank"), int):
            report.add_error(f"model {mid} tier_rank is not an int")

    report.models_loaded = len(data["models"])
    report.add_info(f"models.json: version={data.get('version', '?')}, "
                    f"models={report.models_loaded}")
    return report


def validate_redis(report: ValidationReport | None = None) -> ValidationReport:
    report = report or ValidationReport()
    try:
        import redis  # type: ignore
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
        client.ping()
        report.add_info(f"Redis OK: {url}")
    except Exception as e:  # noqa: BLE001
        report.add_warning(
            f"Redis unavailable ({type(e).__name__}: {e}). "
            f"QuotaManager will fall back to fakeredis (in-process, not durable)."
        )
    return report


def validate_all() -> ValidationReport:
    """Run all checks and return a combined report."""
    report = ValidationReport()
    validate_config_yaml(report=report)
    validate_models_json(report=report)
    validate_redis(report=report)
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    report = validate_all()
    print(f"OK: {report.ok}")
    print(f"Models loaded: {report.models_loaded}")
    for e in report.errors:
        print(f"  ERROR:   {e}")
    for w in report.warnings:
        print(f"  WARNING: {w}")
    for i in report.info:
        print(f"  INFO:    {i}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
