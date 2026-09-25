from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.measure_peak_baseline import (
    EvidenceError,
    _open_read_only,
    build_report,
    main,
    write_daily_peak_if_valid,
)

START = datetime(2026, 9, 14, tzinfo=UTC)
END = START + timedelta(days=7)
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def _epoch(day_offset: int, hour: int = 12) -> int:
    return int((START + timedelta(days=day_offset, hours=hour)).timestamp())


def _make_journal(
    path: Path, *, gap_day: int | None = None, unknown_token_day: int | None = None
) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE model_runs (
            run_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            created_ts REAL NOT NULL,
            total_tokens INTEGER,
            prompt TEXT
        )"""
    )
    for offset in range(7):
        if offset == gap_day:
            continue
        for role, tokens in (("author", 120 + offset), ("reviewer", 240 + offset)):
            value = None if offset == unknown_token_day and role == "reviewer" else tokens
            connection.execute(
                "INSERT INTO model_runs VALUES (?, ?, ?, ?, ?)",
                (f"run-{offset}-{role}", role, _epoch(offset), value, "fixture-only private text"),
            )
    connection.commit()
    connection.close()
    return path


def _make_simon(path: Path, *, gap_day: int | None = None) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE candidate (
            key TEXT PRIMARY KEY,
            attempted_at REAL,
            title TEXT,
            summary TEXT
        )"""
    )
    for offset in range(7):
        if offset == gap_day:
            continue
        for index in range(2):
            connection.execute(
                "INSERT INTO candidate VALUES (?, ?, ?, ?)",
                (f"key-{offset}-{index}", _epoch(offset, 13), "fixture title", "fixture content"),
            )
    connection.commit()
    connection.close()
    return path


def _report(journal: Path, simon: Path) -> dict:
    return build_report(
        journal,
        simon,
        START.date().isoformat(),
        END.date().isoformat(),
        now=NOW,
    )


def _nested_keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_keys(child)


def test_journal_counts_author_reviewer_runs_utc_and_reports_partial_token_usage(
    tmp_path: Path,
):
    journal = _make_journal(tmp_path / "journal.sqlite3", unknown_token_day=3)
    simon = _make_simon(tmp_path / "simon.sqlite3")
    before = journal.read_bytes()

    report = _report(journal, simon)
    journal_report = report["workloads"]["journal"]

    assert journal_report["observed_event_days"] == 7
    assert journal_report["max_observed_daily_requests_lower_bound"] == 2
    assert journal_report["daily"]["2026-09-14"]["requests"] == 2
    assert journal_report["status"] == "insufficient_evidence"
    assert journal_report["peak_requests"] is None
    assert journal_report["peak_comparable"] is False
    assert journal_report["capture_completeness"] == "not_established_best_effort"
    assert (
        "model_runs_capture_is_best_effort_not_a_complete_attempt_log"
        in journal_report["reason_codes"]
    )
    assert journal_report["total_tokens_telemetry"] == {
        "status": "partial",
        "requests_in_scope": 14,
        "requests_with_total_tokens": 13,
        "coverage_ratio": 0.9286,
        "max_observed_total_tokens_per_request": 246,
    }
    assert journal.read_bytes() == before
    assert "tokens_per_request" not in set(_nested_keys(report))


def test_seven_visible_days_still_fail_closed_for_simon_attempt_semantics_and_cactus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    journal = _make_journal(tmp_path / "journal.sqlite3")
    simon = _make_simon(tmp_path / "simon.sqlite3")
    monkeypatch.chdir(tmp_path)

    report = _report(journal, simon)

    assert report["status"] == "insufficient_evidence"
    assert report["workloads"]["simon-news"]["observed_event_days"] == 7
    assert (
        "candidate_attempt_timestamp_is_written_after_generation"
        in report["workloads"]["simon-news"]["reason_codes"]
    )
    assert report["workloads"]["cactus-brief"]["cadence"] == "weekly"
    assert report["workloads"]["cactus-brief"]["weekly_audits"]["converted_to_daily"] is False
    assert report["daily_peak_file"] == {
        "written": False,
        "reason": "insufficient_evidence",
    }
    assert not (tmp_path / "var/daily-peak.json").exists()


def test_a_missing_utc_day_is_reported_and_has_no_peak(tmp_path: Path):
    journal = _make_journal(tmp_path / "journal.sqlite3", gap_day=4)
    simon = _make_simon(tmp_path / "simon.sqlite3")

    report = _report(journal, simon)
    journal_report = report["workloads"]["journal"]

    assert report["status"] == "insufficient_evidence"
    assert journal_report["observed_event_days"] == 6
    assert journal_report["missing_dates"] == ["2026-09-18"]
    assert journal_report["max_observed_daily_requests_lower_bound"] == 2
    assert journal_report["peak_requests"] is None


def test_ro_connection_refuses_write_and_nonexistent_database_is_not_created(tmp_path: Path):
    path = _make_journal(tmp_path / "journal.sqlite3")
    before = path.read_bytes()
    with _open_read_only(path) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM model_runs")
    assert path.read_bytes() == before

    missing = tmp_path / "must-not-be-created.sqlite3"
    with pytest.raises(EvidenceError, match="database_unavailable_or_not_readable"):
        with _open_read_only(missing):
            pass
    assert not missing.exists()


def test_wrong_window_and_incomplete_current_day_are_rejected(tmp_path: Path):
    journal = _make_journal(tmp_path / "journal.sqlite3")
    simon = _make_simon(tmp_path / "simon.sqlite3")
    with pytest.raises(EvidenceError, match="window_must_be_exactly_seven_utc_days"):
        build_report(journal, simon, "2026-09-14", "2026-09-20", now=NOW)
    with pytest.raises(EvidenceError, match="window_contains_incomplete_or_future_utc_day"):
        build_report(journal, simon, "2026-09-18", "2026-09-25", now=NOW)

    # The exclusive boundary may be today's UTC date: all seven included
    # dates then end yesterday and are complete.
    completed_window = build_report(journal, simon, "2026-09-17", "2026-09-24", now=NOW)
    assert completed_window["window_utc"]["calendar_days_complete"] is True


def test_peak_file_writer_rejects_incomplete_report_without_creating_parent(tmp_path: Path):
    destination = tmp_path / "var" / "daily-peak.json"
    assert not write_daily_peak_if_valid({"status": "insufficient_evidence"}, destination)
    assert not destination.parent.exists()


def test_cli_reports_lower_bound_and_does_not_write_daily_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    journal = _make_journal(tmp_path / "journal.sqlite3")
    simon = _make_simon(tmp_path / "simon.sqlite3")
    destination = tmp_path / "var" / "daily-peak.json"
    monkeypatch.chdir(tmp_path)

    result = main(
        [
            "--journal-db",
            str(journal),
            "--simon-db",
            str(simon),
            "--start-date",
            START.date().isoformat(),
            "--end-date",
            END.date().isoformat(),
            "--write-daily-peak",
            "--output",
            str(destination),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    journal_report = report["workloads"]["journal"]
    assert result == 2
    assert journal_report["max_observed_daily_requests_lower_bound"] == 2
    assert journal_report["peak_requests"] is None
    assert journal_report["peak_comparable"] is False
    assert report["daily_peak_file"] == {
        "written": False,
        "reason": "insufficient_evidence",
    }
    assert not destination.exists()
    assert not destination.parent.exists()
    assert "fixture-only private text" not in captured.out
