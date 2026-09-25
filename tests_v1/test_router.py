from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from free_router.app import build_app
from free_router.config import ModelSpec, load_models
from free_router.provider import ProviderClient, ProviderFailure
from free_router.queue import JobQueue
from free_router.quota import QuotaStore
from free_router.router import EditorialRouter


class FakeProvider:
    def __init__(self, failures: set[str] | None = None):
        self.calls: list[str] = []
        self.failures = failures or set()

    async def complete(self, spec, messages, max_tokens, temperature):
        self.calls.append(spec.provider)
        if spec.provider in self.failures:
            raise ProviderFailure("upstream_rate_limited", 45)
        return {
            "content": '{"ok":true}',
            "finish_reason": "stop",
            "actual_model": spec.model,
            "prompt_tokens": 20,
            "completion_tokens": 5,
        }

    async def close(self):
        pass


def _headers(stage="author", author=None, author_job=None):
    headers = {
        "Authorization": "Bearer journal-token",
        "X-Maat-Workload": "journal",
        "X-Maat-Stage": stage,
        "X-Maat-Data-Class": "public_editorial",
    }
    if author:
        headers["X-Maat-Author-Provider"] = author
    if author_job:
        headers["X-Maat-Author-Job"] = author_job
    return headers


def _body(content="Resumí la evidencia pública"):
    return {"model": "auto", "messages": [{"role": "user", "content": content}], "max_tokens": 100}


def _app(catalog: Path, quota: QuotaStore, tmp_path: Path, provider=None):
    return build_app(
        quota=quota,
        provider=provider or FakeProvider(),
        queue=JobQueue(tmp_path / "jobs.sqlite3"),
        catalog_path=catalog,
        daily_peak={"journal": 10, "simon-news": 10, "cactus-brief": 10},
        production_workloads=("journal", "simon-news", "cactus-brief"),
        worker_enabled=False,
    )


def test_redis_without_durable_quota_ledger_fails_closed(catalog, quota, monkeypatch):
    quota.require_durable = True
    settings = {
        "appendonly": "yes",
        "appendfsync": "always",
        "maxmemory-policy": "noeviction",
    }
    monkeypatch.setattr(quota.client, "config_get", lambda key: {key: settings[key]})
    monkeypatch.setattr(
        quota.client,
        "info",
        lambda section: {"aof_enabled": 1, "aof_last_write_status": "ok"},
    )
    spec = load_models(catalog)[0][0]
    assert quota.healthy()
    assert quota.reserve(spec, "journal", 100, 100).keys

    settings["appendonly"] = "no"
    assert not quota.healthy()
    with pytest.raises(RuntimeError, match="quota_store_unavailable"):
        quota.reserve(spec, "journal", 100, 100)

    settings["appendonly"] = "yes"
    original_eval = quota.client.eval

    def loses_durability(*args):
        result = original_eval(*args)
        settings["appendonly"] = "no"
        return result

    monkeypatch.setattr(quota.client, "eval", loses_durability)
    with pytest.raises(RuntimeError, match="quota_store_unavailable"):
        quota.reserve(spec, "journal", 100, 100)


def test_real_completion_and_distinct_reviewer(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    with TestClient(_app(catalog, quota, tmp_path, fake)) as client:
        author = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert author.status_code == 200
        data = author.json()
        assert data["choices"][0]["message"]["content"] == '{"ok":true}'
        assert data["router"]["provider"] in {"gemini", "groq", "cloudflare"}
        daily = client.get("/v1/router/metrics").json()["daily_requests"]
        journal = next(row for row in daily if row["workload"] == "journal")
        assert journal["planned_token_samples"] == journal["requests"] == 1
        assert journal["max_planned_tokens"] >= _body()["max_tokens"]
        review = client.post(
            "/v1/chat/completions",
            json=_body("Revisá el artículo"),
            headers=_headers("reviewer", data["router"]["provider"], data["router"]["job_id"]),
        )
        assert review.status_code == 200
        assert review.json()["router"]["provider"] != data["router"]["provider"]
        assert client.get("/v1/router/status").json()["workloads"]["journal"]["ready"]
        metrics = client.get("/v1/router/metrics").json()
        assert sum(row["requests"] for row in metrics["events"]) == 2
        assert all("content" not in row for row in metrics["events"])
        assert sum(row["requests"] for row in metrics["daily_requests"]) == 2
        assert all("content" not in row for row in metrics["daily_requests"])


def test_reviewer_rejects_unverified_author_provider_header(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    app = _app(catalog, quota, tmp_path, fake)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_body("Revisá el artículo"),
            headers=_headers("reviewer", "typo"),
        )
    assert response.status_code == 400
    assert fake.calls == []
    assert app.state.queue.counts() == {}


def test_reviewer_rejects_wrong_valid_author_provider(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    app = _app(catalog, quota, tmp_path, fake)
    with TestClient(app) as client:
        author = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert author.status_code == 200
        route = author.json()["router"]
        wrong = next(name for name in ("gemini", "groq", "cloudflare") if name != route["provider"])
        review = client.post(
            "/v1/chat/completions",
            json=_body("Revisá el artículo"),
            headers=_headers("reviewer", wrong, route["job_id"]),
        )
        assert review.status_code == 400
        assert review.json()["detail"] == "invalid_author_provider"
    assert len(fake.calls) == 1
    assert app.state.queue.counts() == {"completed": 1}


def test_repeated_completion_returns_durable_result_without_new_provider_call(
    catalog, quota, tmp_path, monkeypatch
):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    with TestClient(_app(catalog, quota, tmp_path, fake)) as client:
        first = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        second = client.post("/v1/chat/completions", json=_body(), headers=_headers())
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(fake.calls) == 1


def test_daily_requests_count_queued_jobs_once_and_separate_utc_days(tmp_path):
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    job_id, inserted = queue.begin(
        "journal", "author", {"body": "Dato público"}, "same", planned_tokens=1200
    )
    assert inserted
    repeated_id, inserted = queue.begin(
        "journal", "author", {"body": "Dato público"}, "same", planned_tokens=9999
    )
    assert not inserted and repeated_id == job_id
    queue.reschedule(job_id, 5)
    today_id = queue.enqueue("simon-news", "author", {"body": "Otro dato público"}, "other", 5)
    future_id = queue.enqueue("cactus-brief", "author", {"body": "Dato futuro"}, "future", 5)
    today = datetime.now(UTC).date()
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    with queue._connect() as conn:
        conn.execute(
            "UPDATE jobs SET created_at=? WHERE id=?",
            ((midnight - timedelta(days=1)).timestamp(), job_id),
        )
        conn.execute(
            "UPDATE jobs SET created_at=? WHERE id=?",
            (midnight.timestamp(), today_id),
        )
        conn.execute(
            "UPDATE jobs SET created_at=? WHERE id=?",
            ((midnight + timedelta(days=8)).timestamp(), future_id),
        )
    rows = queue.daily_request_counts(days=7)
    assert sum(row["requests"] for row in rows) == 2
    assert {row["workload"] for row in rows} == {"journal", "simon-news"}
    assert len({row["day"] for row in rows}) == 2
    journal = next(row for row in rows if row["workload"] == "journal")
    assert journal["planned_token_samples"] == 1
    assert journal["max_planned_tokens"] == 1200


def test_exhaustion_is_durable_202_never_fake_success(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    app = _app(
        catalog,
        quota,
        tmp_path,
        FakeProvider({"gemini", "groq", "cloudflare", "siliconflow"}),
    )
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        job_id = response.json()["id"]
        assert client.get(f"/v1/jobs/{job_id}", headers=_headers()).json()["status"] == "queued"
    reopened = JobQueue(tmp_path / "jobs.sqlite3")
    assert reopened.get(job_id, "journal")["status"] == "queued"


def test_failover_and_private_data_guard(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    with TestClient(_app(catalog, quota, tmp_path, FakeProvider({"gemini"}))) as client:
        response = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert response.status_code == 200
        assert response.json()["router"]["provider"] != "gemini"
        assert (
            client.post(
                "/v1/chat/completions", json=_body("ana@example.org"), headers=_headers()
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/chat/completions",
                json=_body(),
                headers={**_headers(), "X-Maat-Data-Class": "private"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/chat/completions", json=_body(), headers=_headers("reviewer")
            ).status_code
            == 400
        )


def test_personal_data_and_oversized_request_never_enter_queue(
    catalog, quota, tmp_path, monkeypatch
):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    app = _app(catalog, quota, tmp_path)
    with TestClient(app) as client:
        original = app.state.queue.counts()
        personal = client.post(
            "/v1/chat/completions",
            json=_body(
                "Cliente Juan Pérez, DNI 12345678, teléfono +54 9 11 5555-0101; tenencias: bonos"
            ),
            headers=_headers(),
        )
        colon_client = client.post(
            "/v1/chat/completions",
            json=_body("Cliente: Juan Pérez tiene 25.000 nominales del bono AL30."),
            headers=_headers(),
        )
        unlabelled_client = client.post(
            "/v1/chat/completions",
            json=_body("Mi cliente Juan Pérez venderá sus acciones antes del balance."),
            headers=_headers(),
        )
        oversized = client.post(
            "/v1/chat/completions",
            json=_body("x" * 1_000_000),
            headers=_headers(),
        )
        assert personal.status_code == 403
        assert colon_client.status_code == 403
        assert unlabelled_client.status_code == 403
        assert oversized.status_code == 422
        assert app.state.queue.counts() == original


def test_quota_reservations_are_atomic(catalog, quota):
    spec = load_models(catalog)[0][0]
    with ThreadPoolExecutor(max_workers=16) as pool:
        reservations = list(
            pool.map(
                lambda _: quota.reserve(
                    spec, "cactus-brief", 10, 10, provider_limits=(10, 10, 10000, 10000)
                ),
                range(30),
            )
        )
    assert sum(bool(r.keys) for r in reservations) == 4
    assert all(r.retry_after > 0 for r in reservations if not r.keys)


def test_deadline_workload_cannot_consume_other_projects_reserved_capacity(catalog, quota):
    spec = load_models(catalog)[0][0]
    limits = (10, 10, 10000, 10000)
    cactus = [
        quota.reserve(spec, "cactus-brief", 10, 10, provider_limits=limits) for _ in range(10)
    ]
    journal = [quota.reserve(spec, "journal", 10, 10, provider_limits=limits) for _ in range(10)]
    assert sum(bool(row.keys) for row in cactus) == 4
    assert sum(bool(row.keys) for row in journal) == 2


def test_queue_recovers_stale_lease(tmp_path):
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    job_id = queue.enqueue("journal", "author", {"body": _body()}, "same", 0)
    first = queue.claim_due()
    assert first["id"] == job_id
    assert queue.claim_due(now=first["attempts"] - 1) is None
    second = queue.claim_due(now=10**10)
    assert second["id"] == job_id
    queue.complete(job_id, {"choices": ["real"]})
    assert queue.get(job_id, "journal")["result"] == {"choices": ["real"]}


def test_idempotent_begin_is_atomic(tmp_path):
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda _: queue.begin("journal", "author", {"body": _body()}, "same"),
                range(24),
            )
        )
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(inserted for _, inserted in results) == 1


def test_openrouter_is_not_an_admitted_provider(catalog):
    raw = json.loads(catalog.read_text())["models"][0]
    raw.update(
        provider="openrouter",
        model="vendor/model:free",
        evidence_url="https://openrouter.ai/docs/guides/routing/model-variants/free",
    )
    with pytest.raises(ValueError, match="unknown_provider"):
        ModelSpec.parse(raw)


@pytest.mark.asyncio
async def test_novita_transport_uses_documented_rest_path_without_admitting_temporary_model(
    catalog, monkeypatch
):
    raw = json.loads(catalog.read_text())["models"][0]
    spec = replace(
        ModelSpec.parse(raw),
        provider="novita",
        model="inclusionai/ling-3.0-flash-fin",
        api_base="https://api.novita.ai/openai/v1",
        api_key_env="NOVITA_API_KEY",
    )
    monkeypatch.setenv("NOVITA_API_KEY", "test-key")
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "model": spec.model,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ProviderClient(client).complete(spec, [{"role": "user", "content": "dato"}], 32, 0)
    assert seen == ["https://api.novita.ai/openai/v1/chat/completions"]


@pytest.mark.asyncio
async def test_siliconflow_uses_free_model_compatible_chat_endpoint(catalog, monkeypatch):
    spec = next(model for model in load_models(catalog)[0] if model.provider == "siliconflow")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "model": spec.model,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProviderClient(client).complete(
            spec, [{"role": "user", "content": "Texto público"}], 123, 0.3
        )
    assert result["content"] == "ok"
    assert seen[0]["max_tokens"] == 123
    assert "max_completion_tokens" not in seen[0]


@pytest.mark.asyncio
async def test_unverified_effective_model_or_missing_finish_reason_never_succeeds(
    catalog, monkeypatch
):
    spec = next(model for model in load_models(catalog)[0] if model.provider == "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    responses = [
        {
            "model": "other-model",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        },
        {"model": spec.model, "choices": [{"message": {"content": "ok"}}]},
    ]

    def handler(request):
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        upstream = ProviderClient(client)
        for _ in range(2):
            with pytest.raises(ProviderFailure, match="malformed_upstream_response"):
                await upstream.complete(spec, [{"role": "user", "content": "Texto público"}], 80, 0)


@pytest.mark.asyncio
async def test_truncated_or_filtered_upstream_response_never_counts_as_success(catalog):
    spec = next(model for model in load_models(catalog)[0] if model.provider == "groq")
    reasons = iter(("length", "content_filter"))

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": spec.model,
                "choices": [
                    {
                        "message": {"content": '{"approved": true}'},
                        "finish_reason": next(reasons),
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        upstream = ProviderClient(client)
        for _ in range(2):
            with pytest.raises(ProviderFailure, match="malformed_upstream_response"):
                await upstream.complete(spec, [{"role": "user", "content": "Texto público"}], 80, 0)


def test_official_evidence_must_use_https(catalog):
    raw = json.loads(catalog.read_text())["models"][0]
    raw["evidence_url"] = "http://ai.google.dev/pricing"
    with pytest.raises(ValueError, match="untrusted_price_evidence"):
        ModelSpec.parse(raw)


@pytest.mark.parametrize("revoked", ["zero_price", "no_billing"])
def test_price_or_billing_change_removes_provider_from_reserve(catalog, quota, revoked):
    raw = json.loads(catalog.read_text())
    standby = next(row for row in raw["models"] if row["provider"] == "siliconflow")
    standby[revoked] = False
    catalog.write_text(json.dumps(raw), encoding="utf-8")

    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
    )
    assert router.readiness("journal")["providers"] == 3
    assert not router.readiness("journal")["ready"]
    assert router.rejected["siliconflow/test-siliconflow"] == "unverified_free_model"


def test_missing_verified_catalog_queues_without_call(tmp_path, quota, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    with TestClient(_app(tmp_path / "absent.json", quota, tmp_path, fake)) as client:
        response = client.post("/v1/chat/completions", json=_body(), headers=_headers())
    assert response.status_code == 202
    assert fake.calls == []


def test_production_requires_explicit_adoption(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    app = build_app(
        quota=quota,
        provider=fake,
        queue=JobQueue(tmp_path / "jobs.sqlite3"),
        catalog_path=catalog,
        daily_peak={"journal": 10, "simon-news": 10, "cactus-brief": 10},
        production_workloads=(),
        worker_enabled=False,
    )
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert response.status_code == 202
        assert response.json()["reason"] == "production_not_adopted"
        assert not client.get("/v1/router/status").json()["production"]["journal"]["activated"]
    assert fake.calls == []


def test_activated_workload_uses_remaining_free_providers_during_reserve_deficit(
    catalog, quota, tmp_path, monkeypatch
):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    app = _app(catalog, quota, tmp_path, fake)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert first.status_code == 200
        assert client.get("/v1/router/status").json()["production"]["journal"]["activated"]
        day = datetime.now(UTC).date().isoformat()
        quota.client.set(f"fr:v1:siliconflow:rpd:{day}", 90)
        reserve = client.get("/v1/router/status").json()["workloads"]["journal"]
        assert reserve["providers"] == 3
        assert not reserve["ready"]
        second = client.post(
            "/v1/chat/completions",
            json=_body("Otra fuente pública"),
            headers=_headers(),
        )
        assert second.status_code == 200
        assert second.json()["router"]["provider"] != "siliconflow"
    restarted = _app(catalog, quota, tmp_path, fake)
    with TestClient(restarted) as client:
        third = client.post(
            "/v1/chat/completions",
            json=_body("Tercera fuente pública"),
            headers=_headers(),
        )
        assert third.status_code == 200
        assert third.json()["router"]["provider"] != "siliconflow"


def test_queued_pilot_loses_bypass_when_pilot_mode_is_disabled(
    catalog, quota, tmp_path, monkeypatch
):
    monkeypatch.setenv("ROUTER_PILOT_MODE", "0")
    quota.activate_production("journal")

    async def skip_daily_audit(*args, **kwargs):
        return {}

    monkeypatch.setattr("free_router.app.audit_all", skip_daily_audit)
    fake = FakeProvider()
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    job_id = queue.enqueue(
        "journal",
        "author",
        {
            "body": {
                "messages": [{"role": "user", "content": "Dato público"}],
                "max_tokens": 100,
                "temperature": 0,
            },
            "pilot": True,
        },
        "queued-pilot",
        0,
    )
    app = build_app(
        quota=quota,
        provider=fake,
        queue=queue,
        catalog_path=catalog,
        daily_peak={"journal": 10, "simon-news": 10, "cactus-brief": 10},
        production_workloads=("journal",),
    )
    with TestClient(app):
        for _ in range(100):
            if queue.get(job_id, "journal")["attempts"] > 0:
                break
            time.sleep(0.01)
    assert queue.get(job_id, "journal")["status"] == "queued"
    assert fake.calls == []


def test_shared_daily_capacity_blocks_all_workloads(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 80, "simon-news": 80, "cactus-brief": 80},
    )
    status = router.readiness("journal")
    assert "insufficient_shared_daily_headroom" in status["reasons"]
    assert not status["ready"]


def test_measured_peaks_allocate_project_quota_and_preserve_deadline_reserve(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 80, "simon-news": 10, "cactus-brief": 10},
    )
    assert router._workload_share("cactus-brief") == 0.25
    assert router._workload_share("journal") > router._workload_share("simon-news")
    assert sum(
        router._workload_share(w) for w in ("journal", "simon-news", "cactus-brief")
    ) == pytest.approx(1)


def test_measured_request_envelope_prevents_full_context_capacity_understatement(catalog, quota):
    raw = json.loads(catalog.read_text(encoding="utf-8"))
    for row in raw["models"]:
        row["quota"]["tpd"] = 20_000
    catalog.write_text(json.dumps(raw), encoding="utf-8")
    legacy = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 2, "simon-news": 2, "cactus-brief": 2},
    )
    assert legacy.readiness("journal")["verified_daily_requests"] == 0

    measured = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {
            name: {"requests": 2, "tokens_per_request": 1000}
            for name in ("journal", "simon-news", "cactus-brief")
        },
    )
    status = measured.readiness("journal")
    assert status["ready"]
    assert status["planned_tokens_per_request"] == 1000
    assert status["verified_daily_requests"] >= 4

    spec = next(model for model in load_models(catalog)[0] if model.provider == "groq")
    # Forecasting with a measured envelope never relaxes the actual Redis
    # reservation for a request larger than its project's token share.
    oversized = quota.reserve(
        spec,
        "journal",
        5000,
        1000,
        provider_limits=(100, 100, 100000, 20_000),
        workload_share=0.25,
    )
    assert oversized.keys == ()


def test_boolean_peaks_never_count_as_measured_volume(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": True, "simon-news": True, "cactus-brief": True},
    )
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["measured_peak_requests"] is None
    assert "missing_measured_daily_peak" in status["reasons"]


def test_exhausted_account_is_removed_from_current_reserve(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
        production_workloads=("journal",),
    )
    day = datetime.now(UTC).date().isoformat()
    quota.client.set(f"fr:v1:siliconflow:rpd:{day}", 90)
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["providers"] == 3
    assert "less_than_four_independent_providers" in status["reasons"]


@pytest.mark.asyncio
async def test_reserve_deficit_retries_at_next_daily_quota_window(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
        production_workloads=("journal",),
    )
    spec = next(model for model in load_models(catalog)[0] if model.provider == "siliconflow")
    day = datetime.now(UTC).date().isoformat()
    quota.client.set(f"fr:v1:siliconflow:rpd:{day}", 90)
    result = await router.attempt(
        {
            "messages": [{"role": "user", "content": "Fuente pública"}],
            "max_tokens": 100,
            "temperature": 0,
        },
        workload="journal",
        stage="author",
    )
    assert result.response is None
    assert result.reason == "reserve_not_ready"
    assert abs(result.retry_after - quota.seconds_until_daily_reset(spec)) <= 1


def test_exhausted_model_is_removed_from_current_reserve(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
    )
    spec = next(model for model in load_models(catalog)[0] if model.provider == "siliconflow")
    day = datetime.now(UTC).date().isoformat()
    quota.client.set(f"fr:v1:{spec.provider}:{spec.model}:rpd:{day}", 90)
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["providers"] == 3


def test_exhausted_token_budget_removes_provider(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
    )
    day = datetime.now(UTC).date().isoformat()
    quota.client.set(f"fr:v1:groq:tpd:{day}", 900000)
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["providers"] == 3


def test_exhausted_cloudflare_free_neurons_remove_provider(catalog, quota):
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
    )
    day = datetime.now(UTC).date().isoformat()
    quota.client.set(f"fr:v1:cloudflare:neurons:{day}", 9000)
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["providers"] == 3


def test_cloudflare_quota_cannot_exceed_free_daily_neurons(catalog):
    raw = json.loads(catalog.read_text())
    next(row for row in raw["models"] if row["provider"] == "cloudflare")["quota"][
        "daily_neurons"
    ] = 20_000
    catalog.write_text(json.dumps(raw), encoding="utf-8")
    models, rejected = load_models(catalog)
    assert all(model.provider != "cloudflare" for model in models)
    assert rejected["cloudflare/@cf/zai-org/glm-4.7-flash"] == "unverified_neurons"


@pytest.mark.parametrize(
    "model",
    [
        "inclusionai/ling-3.0-flash-fin",
        "inclusionai/ling-3.0-flash-sante",
        "inclusionai/ling-3.0-flash-vl",
    ],
)
def test_novita_temporary_or_paid_models_cannot_join_permanent_reserve(catalog, model):
    row = json.loads(catalog.read_text())["models"][0]
    row["provider"] = "novita"
    row["model"] = model
    with pytest.raises(ValueError, match="model_not_permanently_free"):
        ModelSpec.parse(row)


def test_cloudflare_neurons_must_share_utc_account_window(catalog):
    raw = json.loads(catalog.read_text())["models"]
    cloudflare = next(row for row in raw if row["provider"] == "cloudflare")
    cloudflare["quota"]["reset_tz"] = "Pacific/Honolulu"
    with pytest.raises(ValueError, match="invalid_cloudflare_reset_timezone"):
        ModelSpec.parse(cloudflare)


def test_provider_models_with_conflicting_reset_windows_are_removed(catalog):
    raw = json.loads(catalog.read_text())
    sibling = json.loads(
        json.dumps(next(row for row in raw["models"] if row["provider"] == "groq"))
    )
    sibling["model"] = "second-groq"
    sibling["quota"]["reset_tz"] = "Pacific/Honolulu"
    raw["models"].append(sibling)
    catalog.write_text(json.dumps(raw), encoding="utf-8")

    models, rejected = load_models(catalog)
    assert all(model.provider != "groq" for model in models)
    assert rejected["groq/test-groq"] == "inconsistent_account_reset_timezone"
    assert rejected["groq/second-groq"] == "inconsistent_account_reset_timezone"


def test_provider_rate_limit_cools_all_models_on_same_account(catalog, quota):
    spec = next(model for model in load_models(catalog)[0] if model.provider == "gemini")
    sibling = replace(spec, model="second-free-gemini-model")
    quota.cool_down(spec, 45, account_wide=True)
    assert quota.cooldown_remaining(sibling) > 0


def test_standby_without_reviewer_does_not_satisfy_reserve(catalog, quota):
    raw = json.loads(catalog.read_text())
    next(row for row in raw["models"] if row["provider"] == "siliconflow")["stages"] = ["author"]
    catalog.write_text(json.dumps(raw), encoding="utf-8")
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
    )
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["providers"] == 3
    assert "less_than_four_independent_providers" in status["reasons"]


def test_aggregator_does_not_satisfy_independent_reserve(catalog, quota):
    raw = json.loads(catalog.read_text())
    raw["models"] = [row for row in raw["models"] if row["provider"] != "siliconflow"]
    catalog.write_text(json.dumps(raw), encoding="utf-8")
    router = EditorialRouter(
        quota,
        FakeProvider(),
        catalog,
        {"journal": 10, "simon-news": 10, "cactus-brief": 10},
    )
    status = router.readiness("journal")
    assert not status["ready"]
    assert status["providers"] == 3
    assert "less_than_four_independent_providers" in status["reasons"]


def test_invalid_catalog_schema_fails_closed(tmp_path):
    path = tmp_path / "models.json"
    path.write_text('{"models":[null,42]}', encoding="utf-8")
    models, rejected = load_models(path)
    assert models == []
    assert rejected == {"row_0": "invalid_model_record", "row_1": "invalid_model_record"}
