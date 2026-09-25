"""Durable local queue for public editorial requests; pending work never expires."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

LEASE_SECONDS = 900


class JobQueue:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, workload TEXT NOT NULL, stage TEXT NOT NULL,
                request_json TEXT, result_json TEXT, status TEXT NOT NULL,
                next_attempt REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, idempotency_key TEXT NOT NULL,
                UNIQUE(workload, idempotency_key))""")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, next_attempt)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "planned_tokens" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN planned_tokens INTEGER")
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, workload TEXT NOT NULL, stage TEXT NOT NULL,
                status TEXT NOT NULL, provider TEXT, model TEXT,
                latency_ms INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL, created_at REAL NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS events_time ON events(created_at)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(
        self,
        workload: str,
        stage: str,
        request: dict,
        idempotency_key: str,
        retry_after: int,
        *,
        planned_tokens: int | None = None,
    ) -> str:
        now = time.time()
        job_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO jobs
                (id, workload, stage, request_json, status, next_attempt, created_at,
                 updated_at, idempotency_key, planned_tokens)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    workload,
                    stage,
                    json.dumps(request, ensure_ascii=False),
                    now + retry_after,
                    now,
                    now,
                    idempotency_key,
                    planned_tokens,
                ),
            )
            row = conn.execute(
                "SELECT id FROM jobs WHERE workload=? AND idempotency_key=?",
                (workload, idempotency_key),
            ).fetchone()
            conn.commit()
            return row["id"]

    def begin(
        self,
        workload: str,
        stage: str,
        request: dict,
        idempotency_key: str,
        *,
        planned_tokens: int | None = None,
    ) -> tuple[str, bool]:
        """Atomically claim a new request before any provider call."""
        now = time.time()
        job_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = (
                conn.execute(
                    """INSERT OR IGNORE INTO jobs
                (id, workload, stage, request_json, status, next_attempt, lease_until,
                 attempts, created_at, updated_at, idempotency_key, planned_tokens)
                VALUES (?, ?, ?, ?, 'running', ?, ?, 1, ?, ?, ?, ?)""",
                    (
                        job_id,
                        workload,
                        stage,
                        json.dumps(request, ensure_ascii=False),
                        now + LEASE_SECONDS,
                        now + LEASE_SECONDS,
                        now,
                        now,
                        idempotency_key,
                        planned_tokens,
                    ),
                ).rowcount
                == 1
            )
            if not inserted:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE workload=? AND idempotency_key=?",
                    (workload, idempotency_key),
                ).fetchone()
                job_id = row["id"]
            conn.commit()
        return job_id, inserted

    def get(self, job_id: str, workload: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND workload=?", (job_id, workload)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "next_attempt": row["next_attempt"],
            "attempts": row["attempts"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def completed_author_provider(self, job_id: str, workload: str) -> str | None:
        """Resolve review provenance from our own completed author job."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT stage, status, result_json FROM jobs WHERE id=? AND workload=?",
                (job_id, workload),
            ).fetchone()
        if not row or row["stage"] != "author" or row["status"] != "completed":
            return None
        try:
            result = json.loads(row["result_json"])
            if result["router"]["job_id"] != job_id:
                return None
            provider = result["router"]["provider"]
        except (TypeError, ValueError, KeyError):
            return None
        return provider if isinstance(provider, str) and provider else None

    def claim_due(self, *, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE jobs SET status='queued', lease_until=0
                            WHERE status='running' AND lease_until<?""",
                (now,),
            )
            row = conn.execute(
                """SELECT * FROM jobs WHERE status='queued' AND next_attempt<=?
                                  ORDER BY next_attempt, created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE jobs SET status='running', lease_until=?, attempts=attempts+1,
                                updated_at=? WHERE id=?""",
                    (now + LEASE_SECONDS, now, row["id"]),
                )
            conn.commit()
        if not row:
            return None
        return {
            "id": row["id"],
            "workload": row["workload"],
            "stage": row["stage"],
            "request": json.loads(row["request_json"]),
            "attempts": row["attempts"] + 1,
        }

    def complete(self, job_id: str, result: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET status='completed', result_json=?, request_json=NULL,
                            lease_until=0, updated_at=? WHERE id=?""",
                (json.dumps(result, ensure_ascii=False), time.time(), job_id),
            )

    def reschedule(self, job_id: str, retry_after: int) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET status='queued', next_attempt=?, lease_until=0,
                            updated_at=? WHERE id=?""",
                (now + max(5, retry_after), now, job_id),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def record_event(
        self,
        workload: str,
        stage: str,
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO events
                (workload, stage, status, provider, model, latency_ms,
                 prompt_tokens, completion_tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workload,
                    stage,
                    status,
                    provider,
                    model,
                    latency_ms,
                    prompt_tokens,
                    completion_tokens,
                    time.time(),
                ),
            )

    def metrics(self, *, days: int = 7) -> list[dict]:
        since = time.time() - days * 86400
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT workload, stage, status, provider, model,
                          COUNT(*) AS requests, SUM(latency_ms) AS latency_ms,
                          SUM(prompt_tokens) AS prompt_tokens,
                          SUM(completion_tokens) AS completion_tokens
                   FROM events WHERE created_at>=?
                   GROUP BY workload, stage, status, provider, model""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_request_counts(self, *, days: int = 7) -> list[dict]:
        """Count unique accepted jobs by UTC day, including work still queued."""
        if days < 1:
            raise ValueError("days_must_be_positive")
        today = datetime.now(UTC).date()
        first_day = today - timedelta(days=days - 1)
        start = datetime.combine(first_day, datetime.min.time(), tzinfo=UTC).timestamp()
        end = datetime.combine(
            today + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        ).timestamp()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT date(created_at, 'unixepoch') AS day, workload,
                          COUNT(*) AS requests,
                          COUNT(planned_tokens) AS planned_token_samples,
                          MAX(planned_tokens) AS max_planned_tokens
                   FROM jobs WHERE created_at >= ? AND created_at < ?
                   GROUP BY day, workload ORDER BY day, workload""",
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup(self, *, completed_older_than_days: int = 7) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM jobs WHERE status='completed' AND updated_at<?",
                (time.time() - completed_older_than_days * 86400,),
            )
            conn.execute("DELETE FROM events WHERE created_at<?", (time.time() - 30 * 86400,))
