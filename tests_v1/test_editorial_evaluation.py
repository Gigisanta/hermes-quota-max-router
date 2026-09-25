"""Evaluator gates must reject invalid author output before it becomes quality evidence."""

import json
import sqlite3
from dataclasses import asdict

import pytest

from scripts import prepare_editorial_suites as suites
from scripts.evaluate_cactus_synthetic import (
    SCENARIOS,
)
from scripts.evaluate_cactus_synthetic import (
    _author_check as cactus_author_check,
)
from scripts.evaluate_cactus_synthetic import (
    _case as cactus_case,
)
from scripts.evaluate_cactus_synthetic import (
    _review_check as cactus_review_check,
)
from scripts.evaluate_cactus_synthetic import (
    _review_payload as cactus_review_payload,
)
from scripts.evaluate_editorial_author import (
    JOURNAL_RESPONSE_FORMAT,
    _check_document,
    _quality_result,
)
from scripts.evaluate_simon_public import (
    _confirm_live_source,
    _public_source,
    _ready_cases,
    _review_result,
)
from scripts.record_editorial_pilot_day import _sample


def _article(text: str = "La fuente menciona 42 casos.") -> str:
    return json.dumps(
        {
            "schema": "newsblog.article.v2",
            "kind": "brief",
            "title": "Prueba",
            "standfirst": "Datos de la prueba",
            "tags": ["prueba"],
            "sections": [
                {"heading": "Qué pasó", "text": text, "source_ids": [1]},
                {
                    "heading": "Por qué importa",
                    "text": "El informe describe el resultado.",
                    "source_ids": [1],
                },
            ],
        }
    )


def test_author_accepts_only_valid_schema_and_grounded_number() -> None:
    result = _check_document(_article(), "El informe describe 42 casos.")
    assert result["valid_citations"] is True
    assert _quality_result({"product_gate_ok": False}) == {
        "structural_passed": True,
        "passed": False,
    }
    with pytest.raises(ValueError, match="unsupported_number"):
        _check_document(_article("La fuente menciona 43 casos."), "El informe describe 42 casos.")


def test_author_rejects_missing_schema_and_url_in_text() -> None:
    document = json.loads(_article())
    del document["schema"]
    with pytest.raises(ValueError, match="invalid_article_schema"):
        _check_document(json.dumps(document), "El informe describe 42 casos.")
    with pytest.raises(ValueError, match="url_in_article_text"):
        _check_document(
            _article("Véase https://example.org y 42 casos."), "El informe describe 42 casos."
        )


def test_structured_output_requires_article_schema_field() -> None:
    schema = JOURNAL_RESPONSE_FORMAT["json_schema"]["schema"]
    assert "schema" in schema["required"]
    assert schema["properties"]["schema"]["enum"] == ["newsblog.article.v2"]


def test_suite_inventory_keeps_topic_intake_separate_from_admission(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "simon.sqlite3"
    connection = sqlite3.connect(db_path)
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
    audit = tmp_path / "audit"
    week = audit / "2026-09-01"
    week.mkdir(parents=True)
    (week / "snapshot.json").write_text("{}")
    (week / "brief.json").write_text("{}")
    monkeypatch.setattr(suites, "SIMON_DB", db_path)
    monkeypatch.setattr(suites, "CACTUS_AUDIT", audit)
    monkeypatch.setattr(suites, "CACTUS_PUBLISHED", tmp_path / "no-published-bundles")
    simon = suites.simon_topics()
    cactus = suites.cactus_inventory()
    assert simon["case_count"] == 50
    assert len({case["id"] for case in simon["cases"]}) == 50
    assert simon["evidence_complete"] is False
    assert simon["clinical_approval"] is False
    assert cactus["available_complete_weeks"] == 0
    assert cactus["available_complete_editions"] == 0
    assert cactus["evaluation_ready"] is False


def test_simon_evidence_hash_and_false_accept_gate() -> None:
    topics = [
        {"id": "10.1234/example", "title": "Example", "url": "https://doi.org/10.1234/example"}
    ]
    digest = suites._sha(suites._canonical(topics))
    manifest = {"simon": {"cases": topics, "cases_sha256": digest}}
    evidence = {
        "stage": "public_evidence_collection_only",
        "evaluation_only": True,
        "clinical_approval": False,
        "production_admission": False,
        "topic_sha256": digest,
        "results": [{"id": topics[0]["id"], "evidence_ready": False}],
    }
    assert _ready_cases(manifest, evidence) == []
    evidence["topic_sha256"] = "bad"
    with pytest.raises(ValueError, match="evidence_topic_hash_mismatch"):
        _ready_cases(manifest, evidence)
    verdict = _review_result(
        {
            "approved": True,
            "issues": [],
            "contradictions": [],
            "supportedIndices": [],
            "summarySupported": True,
        }
    )
    assert verdict["correct_rejection"] is False
    generic = _review_result(
        {
            "approved": False,
            "issues": [],
            "contradictions": [],
            "supportedIndices": [],
            "summarySupported": False,
        }
    )
    assert generic["correct_rejection"] is False
    specific = _review_result(
        {
            "approved": False,
            "issues": ["La cura definitiva no está sustentada"],
            "contradictions": [],
            "supportedIndices": [],
            "summarySupported": True,
        }
    )
    assert specific["correct_rejection"] is True
    irrelevant = _review_result(
        {
            "approved": False,
            "issues": ["La evidencia es absoluta"],
            "contradictions": [],
            "supportedIndices": [],
            "summarySupported": False,
        }
    )
    assert irrelevant["correct_rejection"] is False


def test_simon_refuses_forged_private_evidence(monkeypatch, tmp_path) -> None:
    source = {
        "id": "paper-1",
        "workId": "10.1234/example",
        "title": "Public paper",
        "url": "https://doi.org/10.1234/example",
        "kind": "peer-reviewed",
        "primary": True,
        "retracted": False,
        "passage": "Public abstract",
    }
    assert _public_source(source) is True
    assert _public_source({**source, "url": "https://private.example/secret"}) is False
    assert _public_source({**source, "url": "https://doi.org/10.1234/example?leak=1"}) is False
    db = sqlite3.connect(tmp_path / "source.db")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE candidate (key TEXT, title TEXT, url TEXT)")
    db.execute(
        "INSERT INTO candidate VALUES (?, ?, ?)",
        ("10.1234/example", "Public paper", "https://doi.org/10.1234/example"),
    )
    monkeypatch.setattr("scripts.evaluate_simon_public.evidence_for", lambda *_: [source])
    case = {
        "id": "10.1234/example",
        "title": "Public paper",
        "url": "https://doi.org/10.1234/example",
        "sources_sha256": suites._sha(suites._canonical([{**source, "passage": "Private text"}])),
    }
    with pytest.raises(ValueError, match="live_public_source_mismatch"):
        _confirm_live_source(case, db, None, None)
    db.close()


def test_cactus_inventory_counts_iso_weeks_and_validates_bundle(monkeypatch, tmp_path) -> None:
    audit = tmp_path / "audit"
    snapshot, _ = cactus_case(0)
    valid_brief = {
        "narrative": "Escenario ficticio completo",
        "themes": [{"title": "t", "blurb": "b"}],
        "stories": [
            {
                "headline": "Titular ficticio",
                "what": "Suceso ficticio",
                "why": "Análisis ficticio",
                "source": "Agencia ficticia",
                "tier": 1,
                "category": "ARG",
                "url": "https://example.org/fiction/one",
                "rank": 0,
            }
        ],
        "watch_next": ["Seguir la simulación"],
        "used_llm": True,
    }
    for day in ("2026-09-02", "2026-09-06", "2026-09-13"):
        directory = audit / day
        directory.mkdir(parents=True)
        (directory / "snapshot.json").write_text(json.dumps(asdict(snapshot)))
        (directory / "brief.json").write_text(json.dumps(valid_brief))
    bad = audit / "2026-09-20"
    bad.mkdir()
    (bad / "snapshot.json").write_text("{}")
    (bad / "brief.json").write_text("{}")
    incomplete = audit / "2026-09-27"
    incomplete.mkdir()
    (incomplete / "snapshot.json").write_text(json.dumps(asdict(snapshot)))
    (incomplete / "brief.json").write_text(
        json.dumps({**valid_brief, "stories": [{"url": "https://example.org/fiction/incomplete"}]})
    )
    monkeypatch.setattr(suites, "CACTUS_AUDIT", audit)
    monkeypatch.setattr(suites, "CACTUS_PUBLISHED", tmp_path / "missing")
    inventory = suites.cactus_inventory()
    assert inventory["available_complete_editions"] == 3
    assert inventory["available_complete_weeks"] == 2


def test_cactus_fictional_cases_are_distinct_and_reject_bad_source_index() -> None:
    urls = set()
    for index in range(len(SCENARIOS)):
        snapshot, candidates = cactus_case(index)
        assert len(candidates) == 12
        assert all("fiction" in candidate.url for candidate in candidates)
        assert not urls.intersection(candidate.url for candidate in candidates)
        urls.update(candidate.url for candidate in candidates)
        with pytest.raises(ValueError, match="invalid_story_source_index"):
            cactus_author_check({"stories": [{"i": 12}]}, snapshot, candidates)
    original = cactus_review_payload(0)
    modified = json.loads(json.dumps(original))
    modified["stories"][0]["what"] = "La cifra cambió a 999."
    assert cactus_review_check(modified, original)["review_invariants_ok"] is False


def test_daily_sampler_keeps_unknown_spend_unknown_and_whitelists_status() -> None:
    sample = _sample(
        {"workloads": {"journal": {"ready": False, "providers": 0}}, "private_token": "x"},
        {"daily_requests": [], "events": [], "queue": {}},
        daily_spend_usd=None,
        gemini_used_rpd=None,
    )
    assert sample["spend_verified_zero"] is None
    assert "private_token" not in json.dumps(sample)
    assert sample["workloads"]["journal"]["ready"] is False
