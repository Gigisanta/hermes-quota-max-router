#!/usr/bin/env python3
"""Read-only seven-day UTC evidence report for editorial workload peaks."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

WINDOW_DAYS = 7
JOURNAL_ROLES = ("author", "reviewer")
SQLITE_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", -1),
}
READABLE_TABLES = {"model_runs", "candidate", "model_attempt"}


class EvidenceError(Exception):
    """A sanitized reason why a source cannot support a measurement."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _authorize_read_only(
    action: int, arg1: str | None, arg2: str | None, _database: str | None, _trigger: str | None
) -> int:
    if action in SQLITE_READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "table_info" and arg2 in READABLE_TABLES:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@contextmanager
def _open_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise EvidenceError("database_not_a_file")
        uri = f"{resolved.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(_authorize_read_only)
    except EvidenceError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise EvidenceError("database_unavailable_or_not_readable") from exc
    try:
        yield connection
    finally:
        connection.close()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        raise EvidenceError("required_table_missing")
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _parse_day(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError(f"invalid_{field}_utc_date") from exc
    if parsed.isoformat() != value:
        raise EvidenceError(f"invalid_{field}_utc_date")
    return parsed


def _utc_window(start_text: str, end_text: str, now: datetime) -> tuple[date, date, int, int]:
    start = _parse_day(start_text, "start")
    end = _parse_day(end_text, "end_exclusive")
    if (end - start).days != WINDOW_DAYS:
        raise EvidenceError("window_must_be_exactly_seven_utc_days")
    now_utc = now.astimezone(UTC)
    if end > now_utc.date():
        raise EvidenceError("window_contains_incomplete_or_future_utc_day")
    start_epoch = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())
    end_epoch = int(datetime.combine(end, time.min, tzinfo=UTC).timestamp())
    return start, end, start_epoch, end_epoch


def _blank_daily_rows(start: date) -> dict[str, dict[str, int | None]]:
    return {
        (start + timedelta(days=offset)).isoformat(): {
            "requests": 0,
            "total_tokens_recorded_requests": 0,
            "max_observed_total_tokens": None,
        }
        for offset in range(WINDOW_DAYS)
    }


def _daily_summary(rows: dict[str, dict[str, int | None]]) -> dict[str, object]:
    missing_days = [day for day, values in rows.items() if int(values["requests"] or 0) == 0]
    counts = [int(values["requests"] or 0) for values in rows.values()]
    return {
        "expected_days": WINDOW_DAYS,
        "observed_event_days": WINDOW_DAYS - len(missing_days),
        "covered_dates": [day for day in rows if day not in missing_days],
        "missing_dates": missing_days,
        "daily": rows,
        "max_observed_daily_requests_lower_bound": max(counts, default=0) or None,
        "peak_requests": None,
        "seven_day_event_coverage": not missing_days,
    }


def _token_coverage(rows: dict[str, dict[str, int | None]]) -> dict[str, object]:
    total_requests = sum(int(values["requests"] or 0) for values in rows.values())
    recorded = sum(int(values["total_tokens_recorded_requests"] or 0) for values in rows.values())
    maximums = [
        int(values["max_observed_total_tokens"])
        for values in rows.values()
        if values["max_observed_total_tokens"] is not None
    ]
    if total_requests == 0:
        status = "no_observed_requests"
        ratio: float | None = None
    elif recorded == 0:
        status = "not_recorded"
        ratio = 0.0
    elif recorded < total_requests:
        status = "partial"
        ratio = round(recorded / total_requests, 4)
    else:
        status = "complete_for_observed_rows"
        ratio = 1.0
    return {
        "status": status,
        "requests_in_scope": total_requests,
        "requests_with_total_tokens": recorded,
        "coverage_ratio": ratio,
        "max_observed_total_tokens_per_request": max(maximums, default=None),
    }


def measure_journal(path: Path, start: date, start_epoch: int, end_epoch: int) -> dict[str, object]:
    daily = _blank_daily_rows(start)
    try:
        with _open_read_only(path) as connection:
            columns = _table_columns(connection, "model_runs")
            required = {"run_id", "role", "created_ts", "total_tokens"}
            if not required.issubset(columns):
                raise EvidenceError("model_runs_schema_not_comparable")

            bad_timestamps = int(
                connection.execute(
                    """SELECT COUNT(*) FROM model_runs
                   WHERE role IN ('author', 'reviewer')
                     AND typeof(created_ts) NOT IN ('integer', 'real')"""
                ).fetchone()[0]
            )
            results = connection.execute(
                """SELECT strftime('%Y-%m-%d', created_ts, 'unixepoch') AS utc_day,
                          COUNT(*) AS request_count,
                          SUM(CASE WHEN typeof(total_tokens)='integer' AND total_tokens>=0
                                   THEN 1 ELSE 0 END) AS known_token_count,
                          MAX(CASE WHEN typeof(total_tokens)='integer' AND total_tokens>=0
                                   THEN total_tokens END) AS max_total_tokens
                   FROM model_runs
                   WHERE role IN ('author', 'reviewer')
                     AND typeof(created_ts) IN ('integer', 'real')
                     AND created_ts >= ? AND created_ts < ?
                   GROUP BY utc_day""",
                (start_epoch, end_epoch),
            ).fetchall()
            for row in results:
                day = row["utc_day"]
                if day not in daily:
                    continue
                daily[day] = {
                    "requests": int(row["request_count"]),
                    "total_tokens_recorded_requests": int(row["known_token_count"] or 0),
                    "max_observed_total_tokens": (
                        int(row["max_total_tokens"])
                        if row["max_total_tokens"] is not None
                        else None
                    ),
                }
    except EvidenceError as exc:
        summary = _daily_summary(daily)
        return {
            "status": "insufficient_evidence",
            "reason_codes": [exc.reason],
            "counter_definition": (
                "observed model_runs rows with role author or reviewer; "
                "best-effort capture means observed counts are lower bounds"
            ),
            "capture_completeness": "not_established_best_effort",
            **summary,
            "total_tokens_telemetry": _token_coverage(daily),
            "invalid_timestamp_rows": None,
            "peak_comparable": False,
        }

    summary = _daily_summary(daily)
    token_coverage = _token_coverage(daily)
    reasons: list[str] = []
    if not summary["seven_day_event_coverage"]:
        reasons.append("one_or_more_utc_days_have_no_observed_model_run")
    if bad_timestamps:
        reasons.append("model_runs_contains_unbucketable_timestamps")
    reasons.append("model_runs_capture_is_best_effort_not_a_complete_attempt_log")
    return {
        "status": "insufficient_evidence",
        "reason_codes": reasons,
        "counter_definition": (
            "observed model_runs rows with role author or reviewer; "
            "best-effort capture means observed counts are lower bounds"
        ),
        "capture_completeness": "not_established_best_effort",
        **summary,
        "total_tokens_telemetry": token_coverage,
        "invalid_timestamp_rows": bad_timestamps,
        "peak_comparable": False,
    }


def _measure_simon_legacy(
    path: Path, start: date, start_epoch: int, end_epoch: int
) -> dict[str, object]:
    daily_counts = {
        (start + timedelta(days=offset)).isoformat(): 0 for offset in range(WINDOW_DAYS)
    }
    try:
        with _open_read_only(path) as connection:
            columns = _table_columns(connection, "candidate")
            if not {"key", "attempted_at"}.issubset(columns):
                raise EvidenceError("candidate_schema_not_comparable")
            bad_timestamps = int(
                connection.execute(
                    """SELECT COUNT(*) FROM candidate
                   WHERE attempted_at IS NOT NULL
                     AND typeof(attempted_at) NOT IN ('integer', 'real')"""
                ).fetchone()[0]
            )
            results = connection.execute(
                """SELECT strftime('%Y-%m-%d', attempted_at, 'unixepoch') AS utc_day,
                          COUNT(*) AS candidate_count
                   FROM candidate
                   WHERE typeof(attempted_at) IN ('integer', 'real')
                     AND attempted_at >= ? AND attempted_at < ?
                   GROUP BY utc_day""",
                (start_epoch, end_epoch),
            ).fetchall()
            for row in results:
                day = row["utc_day"]
                if day in daily_counts:
                    daily_counts[day] = int(row["candidate_count"])
    except EvidenceError as exc:
        daily_rows = {day: {"observed_candidates": count} for day, count in daily_counts.items()}
        return {
            "status": "insufficient_evidence",
            "reason_codes": [exc.reason],
            "counter_definition": "candidate rows with attempted_at in the UTC day",
            "expected_days": WINDOW_DAYS,
            "observed_event_days": sum(value > 0 for value in daily_counts.values()),
            "covered_dates": [day for day, value in daily_counts.items() if value > 0],
            "missing_dates": [day for day, value in daily_counts.items() if value == 0],
            "daily": daily_rows,
            "max_observed_daily_candidates": max(daily_counts.values(), default=0) or None,
            "peak_requests": None,
            "seven_day_event_coverage": False,
            "peak_comparable": False,
            "attempt_semantics": "unverified",
            "total_tokens_telemetry": {
                "status": "not_recorded",
                "requests_in_scope": None,
                "requests_with_total_tokens": 0,
                "coverage_ratio": None,
                "max_observed_total_tokens_per_request": None,
            },
            "reservation_budget_evidence": "not_recorded",
        }

    missing_days = [day for day, count in daily_counts.items() if count == 0]
    reasons = []
    if missing_days:
        reasons.append("one_or_more_utc_days_have_no_observed_candidate_attempt")
    if bad_timestamps:
        reasons.append("candidate_contains_unbucketable_attempt_timestamps")
    # The writer persists attempted_at after write_draft returns. A provider or
    # runtime exception before that update is therefore absent from this count.
    reasons.append("candidate_attempt_timestamp_is_written_after_generation")
    daily_rows = {day: {"observed_candidates": count} for day, count in daily_counts.items()}
    return {
        "status": "insufficient_evidence",
        "reason_codes": reasons,
        "counter_definition": "candidate rows with attempted_at in the UTC day",
        "expected_days": WINDOW_DAYS,
        "observed_event_days": WINDOW_DAYS - len(missing_days),
        "covered_dates": [day for day in daily_counts if day not in missing_days],
        "missing_dates": missing_days,
        "daily": daily_rows,
        "max_observed_daily_candidates": max(daily_counts.values(), default=0) or None,
        "peak_requests": None,
        "seven_day_event_coverage": not missing_days,
        "peak_comparable": False,
        "attempt_semantics": "best_effort_completion_marker_not_all_attempts",
        "invalid_timestamp_rows": bad_timestamps,
        "total_tokens_telemetry": {
            "status": "not_recorded",
            "requests_in_scope": None,
            "requests_with_total_tokens": 0,
            "coverage_ratio": None,
            "max_observed_total_tokens_per_request": None,
        },
        "reservation_budget_evidence": "not_recorded",
    }


def measure_simon(path: Path, start: date, start_epoch: int, end_epoch: int) -> dict[str, object]:
    """Count the durable start ledger, retaining legacy markers as context only."""
    daily = {
        (start + timedelta(days=offset)).isoformat(): {
            "attempts_started": 0,
            "planned_model_calls": 0,
            "planned_reviewer_calls": 0,
            "invalid_output_ceilings": 0,
        }
        for offset in range(WINDOW_DAYS)
    }
    try:
        with _open_read_only(path) as connection:
            try:
                columns = _table_columns(connection, "model_attempt")
            except EvidenceError as exc:
                if exc.reason == "required_table_missing":
                    return _measure_simon_legacy(path, start, start_epoch, end_epoch)
                raise
            required = {"stage", "started_at", "status", "max_output_tokens_requested"}
            if not required.issubset(columns):
                raise EvidenceError("model_attempt_schema_not_comparable")
            bad_timestamps = int(
                connection.execute(
                    """SELECT COUNT(*) FROM model_attempt
                   WHERE typeof(started_at) NOT IN ('integer', 'real')"""
                ).fetchone()[0]
            )
            rows = connection.execute(
                """SELECT strftime('%Y-%m-%d', started_at, 'unixepoch') AS utc_day,
                          COUNT(*) AS attempts_started,
                          SUM(CASE WHEN stage='classify' AND max_output_tokens_requested=200
                                   THEN 1 WHEN stage='author'
                                   AND max_output_tokens_requested IN (3500,4500)
                                   THEN 1 ELSE 0 END)
                          + SUM(CASE WHEN stage='author' AND max_output_tokens_requested=4500
                                     THEN 1 ELSE 0 END) AS planned_calls,
                          SUM(CASE WHEN stage='author' AND max_output_tokens_requested=4500
                                   THEN 1 ELSE 0 END) AS planned_reviewer_calls,
                          SUM(CASE WHEN (stage='classify' AND max_output_tokens_requested!=200)
                                     OR (stage='author' AND max_output_tokens_requested
                                         NOT IN (0,3500,4500))
                                     OR stage NOT IN ('classify','author')
                                   THEN 1 ELSE 0 END) AS invalid_ceilings
                   FROM model_attempt
                   WHERE typeof(started_at) IN ('integer', 'real')
                     AND started_at >= ? AND started_at < ?
                   GROUP BY utc_day""",
                (start_epoch, end_epoch),
            ).fetchall()
            for row in rows:
                day = row["utc_day"]
                if day in daily:
                    daily[day] = {
                        "attempts_started": int(row["attempts_started"]),
                        "planned_model_calls": int(row["planned_calls"] or 0),
                        "planned_reviewer_calls": int(row["planned_reviewer_calls"] or 0),
                        "invalid_output_ceilings": int(row["invalid_ceilings"] or 0),
                    }
            legacy_markers = int(
                connection.execute(
                    """SELECT COUNT(*) FROM candidate
                   WHERE typeof(attempted_at) IN ('integer', 'real')
                     AND attempted_at >= ? AND attempted_at < ?""",
                    (start_epoch, end_epoch),
                ).fetchone()[0]
            )
    except EvidenceError as exc:
        return {
            "status": "insufficient_evidence",
            "reason_codes": [exc.reason],
            "peak_requests": None,
            "peak_comparable": False,
            "daily": daily,
        }
    except sqlite3.Error:
        return {
            "status": "insufficient_evidence",
            "reason_codes": ["model_attempt_or_candidate_query_failed"],
            "peak_requests": None,
            "peak_comparable": False,
            "daily": daily,
        }

    missing_days = [day for day, row in daily.items() if row["attempts_started"] == 0]
    reasons = ["model_attempt_does_not_prove_scheduler_run_coverage"]
    if missing_days:
        reasons.append("one_or_more_utc_days_have_no_started_model_attempt")
    if bad_timestamps:
        reasons.append("model_attempt_contains_unbucketable_timestamps")
    if any(row["invalid_output_ceilings"] for row in daily.values()):
        reasons.append("model_attempt_contains_unknown_output_ceiling")
    return {
        "status": "insufficient_evidence",
        "reason_codes": reasons,
        "counter_definition": (
            "model_attempt starts: classify plans one call at 200 output tokens; "
            "author plans one call at 3500, plus reviewer at cumulative 4500; "
            "author with zero ceiling reached no model call. Ceilings are "
            "recorded before inference and do not prove actual invocation"
        ),
        "attempt_semantics": "persisted_before_inference_including_failed_and_interrupted_attempts",
        "expected_days": WINDOW_DAYS,
        "observed_event_days": WINDOW_DAYS - len(missing_days),
        "covered_dates": [day for day in daily if day not in missing_days],
        "missing_dates": missing_days,
        "daily": daily,
        "max_daily_planned_model_calls": max(
            (row["planned_model_calls"] for row in daily.values()), default=0
        )
        or None,
        "legacy_candidate_completion_markers": legacy_markers,
        "peak_requests": None,
        "seven_day_event_coverage": not missing_days,
        "peak_comparable": False,
        "invalid_timestamp_rows": bad_timestamps,
        "total_tokens_telemetry": {
            "status": "output_ceiling_only_no_actual_tokens",
            "requests_in_scope": sum(row["planned_model_calls"] for row in daily.values()),
            "requests_with_total_tokens": 0,
            "coverage_ratio": 0.0,
            "max_observed_total_tokens_per_request": None,
        },
        "reservation_budget_evidence": "output_ceiling_only_input_message_bytes_missing",
    }


def _cactus_weekly_only() -> dict[str, object]:
    return {
        "status": "insufficient_evidence",
        "cadence": "weekly",
        "reason_codes": ["weekly_audit_is_not_daily_sqlite_request_telemetry"],
        "daily_peak_requests": None,
        "daily_coverage": {
            "expected_days": WINDOW_DAYS,
            "observed_event_days": 0,
            "covered_dates": [],
            "missing_dates": None,
        },
        "weekly_audits": {
            "source_connected": False,
            "weekly_rows_read": 0,
            "converted_to_daily": False,
        },
        "total_tokens_telemetry": {
            "status": "not_available_from_sqlite_source",
            "requests_in_scope": None,
            "requests_with_total_tokens": 0,
            "coverage_ratio": None,
            "max_observed_total_tokens_per_request": None,
        },
        "reservation_budget_evidence": "not_recorded",
    }


def build_report(
    journal_db: Path, simon_db: Path, start_text: str, end_text: str, *, now: datetime | None = None
) -> dict[str, object]:
    now = now or datetime.now(UTC)
    start, end, start_epoch, end_epoch = _utc_window(start_text, end_text, now)
    journal = measure_journal(journal_db, start, start_epoch, end_epoch)
    simon = measure_simon(simon_db, start, start_epoch, end_epoch)
    cactus = _cactus_weekly_only()
    sources = {"journal": journal, "simon-news": simon, "cactus-brief": cactus}
    peak_candidates = {workload: row.get("peak_requests") for workload, row in sources.items()}
    all_peaks_valid = all(
        type(peak_candidates[key]) is int
        and peak_candidates[key] > 0
        and bool(sources[key].get("peak_comparable"))
        for key in ("journal", "simon-news", "cactus-brief")
    )
    reasons = sorted(
        {str(reason) for source in sources.values() for reason in source.get("reason_codes", [])}
    )
    if (
        not all_peaks_valid
        and "one_or_more_workloads_lacks_comparable_seven_day_evidence" not in reasons
    ):
        reasons.append("one_or_more_workloads_lacks_comparable_seven_day_evidence")
    return {
        "schema": "peak-baseline-report.v1",
        "status": "valid" if all_peaks_valid else "insufficient_evidence",
        "window_utc": {
            "start_inclusive": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "days": WINDOW_DAYS,
            "calendar_days_complete": True,
        },
        "reason_codes": reasons,
        "workloads": sources,
        "daily_peak_candidates": peak_candidates,
        "daily_peak_file": {
            "written": False,
            "reason": "not_requested_by_report_only_mode"
            if all_peaks_valid
            else "insufficient_evidence",
        },
        "reservation_budget_evidence": {
            "status": "not_recorded",
            "tokens_per_request_emitted": False,
            "missing_measurements": [
                "UTF-8 byte count for each request message",
                "64-byte allowance per message",
                "max_tokens requested for each request",
            ],
        },
    }


def write_daily_peak_if_valid(report: dict[str, object], destination: Path) -> bool:
    """Atomically emit the legacy integer config only after all three gates pass."""
    if report.get("status") != "valid":
        return False
    workloads = report.get("workloads")
    if not isinstance(workloads, dict):
        return False
    keys = ("journal", "simon-news", "cactus-brief")
    peaks: dict[str, int] = {}
    for key in keys:
        row = workloads.get(key)
        peak = row.get("peak_requests") if isinstance(row, dict) else None
        if type(peak) is not int or peak <= 0 or not row.get("peak_comparable"):
            return False
        peaks[key] = peak

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(peaks, temp_file, sort_keys=True, separators=(",", ":"))
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, destination)
        return True
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-db", required=True, type=Path)
    parser.add_argument("--simon-db", required=True, type=Path)
    parser.add_argument(
        "--start-date", required=True, help="Primer día UTC, inclusive (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--end-date", required=True, help="Límite UTC, exclusivo; ventana de 7 días."
    )
    parser.add_argument(
        "--write-daily-peak",
        action="store_true",
        help="Escribe var/daily-peak.json sólo cuando las tres cargas tienen evidencia válida.",
    )
    parser.add_argument("--output", type=Path, default=Path("var/daily-peak.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(args.journal_db, args.simon_db, args.start_date, args.end_date)
    except EvidenceError as exc:
        report = {
            "schema": "peak-baseline-report.v1",
            "status": "insufficient_evidence",
            "reason_codes": [exc.reason],
            "daily_peak_file": {"written": False, "reason": "insufficient_evidence"},
        }
    if args.write_daily_peak and report.get("status") == "valid":
        written = write_daily_peak_if_valid(report, args.output)
        report["daily_peak_file"] = {
            "written": written,
            "reason": "written_after_all_gates" if written else "peak_gate_rejected",
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
