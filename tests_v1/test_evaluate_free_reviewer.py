"""Evaluator must not overstate quality or consume unknown free quota."""

import hashlib
import json
import sys

import httpx
import pytest

from scripts import evaluate_free_reviewer as evaluator
from scripts.evaluate_free_reviewer import (
    FreeQuotaUnavailable,
    _check_simplellm_quota,
    _verdict,
    _write_report,
)


def _response(verdict: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gemma-4-E4B",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}],
        },
    )


@pytest.mark.parametrize(
    "verdict",
    [
        {"approved": True, "issues": ["Cita incorrecta"]},
        {"approved": False, "issues": []},
    ],
)
def test_contradictory_verdict_is_not_counted_as_correct(verdict: dict) -> None:
    with pytest.raises(ValueError, match="inconsistent_verdict"):
        _verdict(_response(verdict), "gemma-4-E4B")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "remaining_rph": 0,
            "remaining_rpd": 500,
            "remaining_rpm": 5,
            "remaining_tph": 5000,
            "remaining_tpd": 5000,
        },
        {
            "remaining_rph": 5,
            "remaining_rpd": 500,
            "remaining_rpm": 5,
            "remaining_tph": 200,
            "remaining_tpd": 5000,
        },
        {"remaining_rph": 5, "remaining_rpd": 500, "remaining_rpm": 5, "remaining_tpd": 5000},
    ],
)
def test_simplellm_preflight_fails_closed(payload: dict) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/rate-limit"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(FreeQuotaUnavailable):
            _check_simplellm_quota(client, "test-only", input_bytes=100, max_tokens=512)


def test_report_writer_refuses_symlink(tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    link = tmp_path / "report.json"
    link.symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        _write_report(link, {"results": []})
    assert victim.read_text(encoding="utf-8") == "keep"


def test_resume_retries_error_without_duplicate(monkeypatch, tmp_path) -> None:
    suite = tmp_path / "synthetic.json"
    suite.write_text(
        json.dumps(
            {
                "description": "casos sintéticos",
                "cases": [
                    {
                        "id": "one",
                        "article": "Una cifra sin evidencia.",
                        "evidence": "La fuente no contiene cifras.",
                        "expected_approved": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        evaluator, "FROZEN_JOURNAL_SUITE_SHA256", hashlib.sha256(suite.read_bytes()).hexdigest()
    )
    output = tmp_path / "report.json"
    output.write_text(
        json.dumps(
            {
                "provider": "gemini",
                "model": "gemini-3.5-flash-lite",
                "suite_sha256": evaluator.FROZEN_JOURNAL_SUITE_SHA256,
                "review_prompt_sha256": hashlib.sha256(
                    evaluator.REVIEW_SYSTEM.encode()
                ).hexdigest(),
                "results": [{"id": "one", "error": "http_503"}],
            }
        ),
        encoding="utf-8",
    )
    real_client = httpx.Client

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gemini-3.5-flash-lite",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"approved":false,"issues":["Sin evidencia"]}'},
                    }
                ],
            },
        )

    def mock_client(**_: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(evaluator.httpx, "Client", mock_client)
    monkeypatch.setattr(evaluator.time, "sleep", lambda _: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_free_reviewer.py",
            "--provider",
            "gemini",
            "--cases",
            str(suite),
            "--output",
            str(output),
            "--resume",
        ],
    )
    assert evaluator.main() == 0
    results = json.loads(output.read_text(encoding="utf-8"))["results"]
    assert len(results) == 1
    assert results[0]["correct"] is True
    assert "error" not in results[0]
