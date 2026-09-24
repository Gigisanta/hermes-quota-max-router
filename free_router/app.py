"""Loopback-only OpenAI-compatible editorial API with durable queue semantics."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from free_router import __version__
from free_router.config import STAGES, WORKLOADS, workload_token
from free_router.discovery import audit_all
from free_router.provider import ProviderClient
from free_router.queue import JobQueue
from free_router.quota import QuotaStore
from free_router.router import EditorialRouter

_LOG = logging.getLogger(__name__)

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PRIVATE_KEYS = re.compile(
    r'"(?:patient_id|client_id|account_number|client_holdings|medical_record)"\s*:', re.I
)
_PRIVATE_MARKERS = re.compile(
    r"\b(?:DNI|CUIT|CUIL|CBU|CVU|tel[eé]fono|celular|paciente|"
    r"historia\s+cl[ií]nica|n[uú]mero\s+de\s+cuenta|whatsapp|clientes?)\b"
    r"|\+54[\s-]*(?:9[\s-]*)?\d[\d\s-]{7,}"
    r"|\b(?:tenencias?|cartera\s+personal)\s*[:=]",
    re.I,
)
_NAMED_CLIENT = re.compile(r"\bCliente\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\b")


class ChatMessage(BaseModel):
    role: str
    content: str = Field(max_length=65536)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "auto"
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    temperature: float = Field(default=0.3, ge=0, le=1)
    stream: bool = False


def _authorize(workload: str | None, authorization: str | None) -> str:
    if workload not in WORKLOADS:
        raise HTTPException(400, "unknown_editorial_workload")
    expected = workload_token(workload)
    if not expected:
        raise HTTPException(503, "workload_token_not_configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid_workload_token")
    return workload


def _peak_config() -> dict[str, int]:
    path = Path(os.getenv("ROUTER_DAILY_PEAK_FILE", "var/daily-peak.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if k in WORKLOADS and type(v) is int and v > 0}
    except (OSError, ValueError, TypeError):
        return {}


def build_app(
    *,
    quota: QuotaStore | None = None,
    provider: ProviderClient | None = None,
    queue: JobQueue | None = None,
    catalog_path: Path | None = None,
    daily_peak: dict[str, int] | None = None,
    production_workloads: tuple[str, ...] | None = None,
    worker_enabled: bool = True,
) -> FastAPI:
    if quota is None:
        # Construction does not need Redis to be online; every route fails closed if it is down.
        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
            socket_timeout=2,
        )
        quota = QuotaStore(client)
    provider = provider or ProviderClient()
    queue = queue or JobQueue(Path(os.getenv("ROUTER_QUEUE_DB", "var/queue.sqlite3")))
    router = EditorialRouter(
        quota,
        provider,
        catalog_path or Path(os.getenv("ROUTER_VERIFIED_MODELS", "var/verified-models.json")),
        daily_peak or _peak_config(),
        production_workloads=production_workloads,
    )

    def complete_job(job_id: str, result: dict) -> None:
        """Bind the public result to its durable job before returning it."""
        result["router"]["job_id"] = job_id
        queue.complete(job_id, result)

    def record_result(
        workload: str, stage: str, status: str, result: dict | None, elapsed: float
    ) -> None:
        usage = (result or {}).get("usage") or {}
        route = (result or {}).get("router") or {}
        try:
            queue.record_event(
                workload,
                stage,
                status,
                provider=route.get("provider"),
                model=route.get("model"),
                latency_ms=int(elapsed * 1000),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )
        except (OSError, sqlite3.Error):
            pass  # Telemetry must not turn a completed editorial job into a retry.

    async def drain_queue() -> None:
        while True:
            try:
                job = queue.claim_due()
                if job:
                    payload = job["request"]
                    if payload.get("pilot", False) and os.getenv("ROUTER_PILOT_MODE") != "1":
                        # A queued calibration job must never become production work.
                        queue.reschedule(job["id"], 300)
                        continue
                    started = time.monotonic()
                    result = await router.attempt(
                        payload["body"],
                        workload=job["workload"],
                        stage=job["stage"],
                        author_provider=payload.get("author_provider"),
                        pilot=payload.get("pilot", False),
                    )
                    if result.response is None:
                        queue.reschedule(job["id"], result.retry_after)
                    else:
                        complete_job(job["id"], result.response)
                        record_result(
                            job["workload"],
                            job["stage"],
                            "resumed",
                            result.response,
                            time.monotonic() - started,
                        )
                else:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # No payload or secret in operational logs. Keep the worker alive.
                await asyncio.sleep(30)

    async def audit_loop() -> None:
        while True:
            try:
                router.reload()
                await audit_all(router.models, quota, router.provider)
                for workload, reserve in router.status()["workloads"].items():
                    if not reserve["ready"]:
                        _LOG.warning(
                            "free_router_reserve_deficit workload=%s providers=%s reasons=%s",
                            workload,
                            reserve["providers"],
                            ",".join(reserve["reasons"]),
                        )
                queue.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Status keeps the last successful audit and expired evidence closes routes.
            await asyncio.sleep(24 * 3600)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(drain_queue()) if worker_enabled else None
        audit_task = asyncio.create_task(audit_loop()) if worker_enabled else None
        try:
            yield
        finally:
            for background in (task, audit_task):
                if background:
                    background.cancel()
                    try:
                        await background
                    except asyncio.CancelledError:
                        pass
            await provider.close()

    app = FastAPI(title="MaatWork Free Editorial Router", version=__version__, lifespan=lifespan)
    app.state.router = router
    app.state.queue = queue

    @app.get("/health")
    def health() -> dict:
        redis_ok = quota.healthy()
        return {
            "status": "ok" if redis_ok else "degraded",
            "version": __version__,
            "redis": redis_ok,
            "queue": queue.counts(),
        }

    @app.get("/v1/router/status")
    def status() -> dict:
        return router.status()

    @app.get("/v1/router/metrics")
    def metrics() -> dict:
        return {"window_days": 7, "events": queue.metrics(days=7), "queue": queue.counts()}

    @app.get("/v1/models")
    def models() -> dict:
        router.reload()
        return {
            "object": "list",
            "data": [
                {"id": m.id, "object": "model", "owned_by": m.provider} for m in router.models
            ],
        }

    @app.post("/v1/chat/completions")
    async def completion(
        body: ChatRequest,
        x_maat_workload: str | None = Header(default=None),
        x_maat_stage: str | None = Header(default=None),
        x_maat_data_class: str | None = Header(default=None),
        x_maat_author_provider: str | None = Header(default=None),
        x_maat_author_job: str | None = Header(default=None),
        x_maat_pilot: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> Any:
        workload = _authorize(x_maat_workload, authorization)
        if x_maat_data_class != "public_editorial":
            raise HTTPException(403, "only_public_editorial_content")
        if x_maat_stage not in STAGES:
            raise HTTPException(400, "invalid_editorial_stage")
        if x_maat_stage == "reviewer" and not x_maat_author_provider:
            raise HTTPException(400, "author_provider_required_for_review")
        if x_maat_stage == "reviewer":
            if not x_maat_author_job or not re.fullmatch(r"[0-9a-f]{32}", x_maat_author_job):
                raise HTTPException(400, "author_job_required_for_review")
            recorded_provider = queue.completed_author_provider(x_maat_author_job, workload)
            if recorded_provider is None or recorded_provider != x_maat_author_provider:
                raise HTTPException(400, "invalid_author_provider")
        if body.model != "auto" or body.stream:
            raise HTTPException(422, "only_nonstreaming_auto_is_supported")
        if any(
            m.role not in ("system", "user", "assistant") or not m.content.strip()
            for m in body.messages
        ):
            raise HTTPException(422, "invalid_messages")
        content = "\n".join(m.content for m in body.messages)
        if len(content.encode("utf-8")) > 131072:
            raise HTTPException(413, "editorial_request_too_large")
        if any(
            guard.search(content)
            for guard in (_EMAIL, _PRIVATE_KEYS, _PRIVATE_MARKERS, _NAMED_CLIENT)
        ):
            raise HTTPException(403, "possible_private_data")
        pilot = x_maat_pilot == "true" and os.getenv("ROUTER_PILOT_MODE") == "1"
        request_data = {
            "messages": [m.model_dump() for m in body.messages],
            "max_tokens": body.max_tokens,
            "temperature": body.temperature,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "idempotency_key": idempotency_key,
                    "body": request_data,
                    "stage": x_maat_stage,
                    "author_provider": x_maat_author_provider,
                    "author_job": x_maat_author_job,
                    "pilot": pilot,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        job_id, inserted = queue.begin(
            workload,
            x_maat_stage,
            {
                "body": request_data,
                "author_provider": x_maat_author_provider,
                "author_job": x_maat_author_job,
                "pilot": pilot,
            },
            fingerprint,
        )
        if not inserted:
            previous = queue.get(job_id, workload)
            assert previous is not None
            if previous["status"] == "completed" and previous["result"]:
                return previous["result"]
            wait = max(5, int(previous["next_attempt"] - time.time()))
            return JSONResponse(
                status_code=202,
                headers={"Retry-After": str(wait), "Location": f"/v1/jobs/{job_id}"},
                content={
                    "id": job_id,
                    "status": previous["status"],
                    "reason": "existing_queued_job",
                    "retry_after_seconds": wait,
                },
            )
        started = time.monotonic()
        try:
            outcome = await router.attempt(
                request_data,
                workload=workload,
                stage=x_maat_stage,
                author_provider=x_maat_author_provider,
                pilot=pilot,
            )
        except Exception as exc:
            _LOG.error("free_router_attempt_error type=%s", type(exc).__name__)
            queue.reschedule(job_id, 60)
            return JSONResponse(
                status_code=202,
                headers={"Retry-After": "60", "Location": f"/v1/jobs/{job_id}"},
                content={
                    "id": job_id,
                    "status": "queued",
                    "reason": "router_error",
                    "retry_after_seconds": 60,
                },
            )
        if outcome.response:
            complete_job(job_id, outcome.response)
            record_result(
                workload, x_maat_stage, "completed", outcome.response, time.monotonic() - started
            )
            return outcome.response
        queue.reschedule(job_id, outcome.retry_after)
        record_result(workload, x_maat_stage, "queued", None, time.monotonic() - started)
        return JSONResponse(
            status_code=202,
            headers={"Retry-After": str(outcome.retry_after), "Location": f"/v1/jobs/{job_id}"},
            content={
                "id": job_id,
                "status": "queued",
                "reason": outcome.reason,
                "retry_after_seconds": outcome.retry_after,
            },
        )

    @app.get("/v1/jobs/{job_id}")
    def get_job(
        job_id: str,
        x_maat_workload: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        workload = _authorize(x_maat_workload, authorization)
        job = queue.get(job_id, workload)
        if job is None:
            raise HTTPException(404, "job_not_found")
        return job

    return app


app = build_app()
