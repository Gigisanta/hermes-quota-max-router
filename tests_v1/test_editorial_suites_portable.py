"""Standalone CI checks for editorial intake and pilot metadata."""

import json
import sqlite3

from scripts import prepare_editorial_suites as suites
from scripts.record_editorial_pilot_day import _sample


def test_cactus_inventory_requires_complete_stories_and_distinct_iso_weeks(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "audit"
    snapshot = {
        "dollar": {"Oficial": {"venta": 1000}},
        "arg_stocks": {"Merval": {"price": 100}},
        "global_indices": {"S&P 500": {"price": 100}},
        "risk_pais": {"embi": 500},
        "ar_macro": {"inflacion_mensual": 2},
        "fetch_time": "2026-09-02T12:00:00Z",
    }
    valid = {
        "narrative": "Escenario ficticio",
        "themes": [{"title": "t", "blurb": "b"}],
        "stories": [
            {
                "headline": "Titular",
                "what": "Suceso",
                "why": "Impacto",
                "source": "Agencia",
                "tier": 1,
                "category": "ARG",
                "url": "https://example.org/fiction",
                "rank": 0,
            }
        ],
        "watch_next": ["Seguir la simulación"],
        "used_llm": True,
    }
    for day in ("2026-09-02", "2026-09-06", "2026-09-13"):
        directory = root / day
        directory.mkdir(parents=True)
        (directory / "snapshot.json").write_text(json.dumps(snapshot))
        (directory / "brief.json").write_text(json.dumps(valid))
    incomplete = root / "2026-09-20"
    incomplete.mkdir()
    (incomplete / "snapshot.json").write_text(json.dumps(snapshot))
    (incomplete / "brief.json").write_text(
        json.dumps({**valid, "stories": [{"url": "https://example.org/fiction"}]})
    )
    monkeypatch.setattr(suites, "CACTUS_AUDIT", root)
    monkeypatch.setattr(suites, "CACTUS_PUBLISHED", tmp_path / "absent")
    inventory = suites.cactus_inventory()
    assert inventory["available_complete_editions"] == 3
    assert inventory["available_complete_weeks"] == 2
    assert inventory["missing_weeks"] == 8


def test_simon_topic_intake_is_50_distinct_public_dois(monkeypatch, tmp_path) -> None:
    db = tmp_path / "candidate.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("""CREATE TABLE candidate
        (key TEXT, title TEXT, url TEXT, summary TEXT, published TEXT,
         source_id TEXT, first_seen REAL)""")
    for index in range(50):
        doi = f"10.1234/topic{index:02d}"
        connection.execute(
            "INSERT INTO candidate VALUES(?,?,?,?,?,?,?)",
            (
                doi,
                f"Original topic {index}",
                f"https://doi.org/{doi}",
                "Public research abstract. " * 12,
                "2026-09-01",
                "europepmc-test",
                index,
            ),
        )
    connection.commit()
    connection.close()
    monkeypatch.setattr(suites, "SIMON_DB", db)
    intake = suites.simon_topics()
    assert intake["case_count"] == 50
    assert len({case["id"] for case in intake["cases"]}) == 50
    assert intake["evidence_complete"] is False


def test_daily_pilot_sampler_does_not_infer_zero_spend() -> None:
    sample = _sample(
        {"workloads": {"journal": {"ready": False, "providers": 0}}, "private_token": "x"},
        {"daily_requests": [], "events": [], "queue": {}},
        daily_spend_usd=None,
        gemini_used_rpd=None,
    )
    assert sample["spend_verified_zero"] is None
    assert "private_token" not in json.dumps(sample)
