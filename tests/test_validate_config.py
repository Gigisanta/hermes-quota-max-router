"""Tests for config validation (Phase 10)."""

import json
from pathlib import Path

import pytest

from scripts.validate_config import (
    validate_all,
    validate_config_yaml,
    validate_models_json,
    validate_redis,
)

# --- validate_config_yaml ---


def test_valid_config_passes(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("model_list:\n  - model_name: foo\n    litellm_params:\n      model: fake/m\n")
    report = validate_config_yaml(p)
    assert report.ok
    assert any("1 model_list entries" in i for i in report.info)


def test_missing_config_returns_error(tmp_path: Path) -> None:
    p = tmp_path / "nope.yaml"
    report = validate_config_yaml(p)
    assert not report.ok
    assert any("not found" in e for e in report.errors)


def test_invalid_yaml_returns_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("model_list: [unclosed")
    report = validate_config_yaml(p)
    assert not report.ok
    assert any("parse error" in e for e in report.errors)


def test_config_without_model_list_errors(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("litellm_settings: {}\n")
    report = validate_config_yaml(p)
    assert not report.ok


def test_config_entry_missing_model_name_errors(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("model_list:\n  - litellm_params: {}\n")
    report = validate_config_yaml(p)
    assert not report.ok
    assert any("model_name" in e for e in report.errors)


def test_config_entry_missing_litellm_params_errors(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("model_list:\n  - model_name: foo\n")
    report = validate_config_yaml(p)
    assert not report.ok
    assert any("litellm_params" in e for e in report.errors)


# --- validate_models_json ---


def test_valid_seed_passes(tmp_path: Path) -> None:
    p = tmp_path / "models.json"
    p.write_text(
        json.dumps(
            {
                "version": "2026-06-10",
                "models": [
                    {
                        "model_id": "x/y",
                        "provider": "x",
                        "display_name": "Y",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 1,
                        "strength_tags": [],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 50.0,
                    }
                ],
            }
        )
    )
    report = validate_models_json(p)
    assert report.ok
    assert report.models_loaded == 1


def test_missing_seed_errors(tmp_path: Path) -> None:
    p = tmp_path / "nope.json"
    report = validate_models_json(p)
    assert not report.ok


def test_invalid_json_errors(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid")
    report = validate_models_json(p)
    assert not report.ok
    assert any("parse error" in e for e in report.errors)


def test_missing_models_list_errors(tmp_path: Path) -> None:
    p = tmp_path / "no_models.json"
    p.write_text(json.dumps({"version": "x"}))
    report = validate_models_json(p)
    assert not report.ok


def test_duplicate_model_id_errors(tmp_path: Path) -> None:
    p = tmp_path / "dup.json"
    p.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": "x/y",
                        "provider": "x",
                        "display_name": "A",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 1,
                        "strength_tags": [],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 50.0,
                    },
                    {
                        "model_id": "x/y",
                        "provider": "x",
                        "display_name": "B",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": True,
                        "tier_rank": 1,
                        "strength_tags": [],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 50.0,
                    },
                ]
            }
        )
    )
    report = validate_models_json(p)
    assert not report.ok
    assert any("duplicate" in e for e in report.errors)


def test_missing_required_field_errors(tmp_path: Path) -> None:
    p = tmp_path / "missing.json"
    p.write_text(
        json.dumps(
            {
                "models": [
                    {"model_id": "x/y"},  # missing everything else
                ]
            }
        )
    )
    report = validate_models_json(p)
    assert not report.ok
    assert any("missing fields" in e for e in report.errors)


def test_is_free_not_bool_errors(tmp_path: Path) -> None:
    p = tmp_path / "bad_is_free.json"
    p.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": "x/y",
                        "provider": "x",
                        "display_name": "Y",
                        "context_window": 1000,
                        "input_price": 0.0,
                        "output_price": 0.0,
                        "is_free": "yes",
                        "tier_rank": 1,
                        "strength_tags": [],
                        "weakness_tags": [],
                        "best_for": [],
                        "performance_score": 50.0,
                    },
                ]
            }
        )
    )
    report = validate_models_json(p)
    assert not report.ok
    assert any("is_free" in e for e in report.errors)


# --- validate_redis ---


def test_redis_unreachable_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """If Redis is down (default in this env), the report gets a warning."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")  # no server
    report = validate_redis()
    assert any("unavailable" in w for w in report.warnings)
    assert report.ok  # warning, not error


# --- validate_all ---


def test_validate_all_against_real_repo() -> None:
    """Smoke: validate_all() against the actual repo files doesn't crash.

    Note: model count varies because previous Auto-Updater tests
    mutated registry/models.json. The 2026-06-10 curated seed has 4 models
    (after live Gemini key verification added gemini-2.5-flash-lite and
    pruned moonshot/paid entries). We assert >= 4.
    """
    report = validate_all()
    assert report.models_loaded >= 4
    assert report.ok
