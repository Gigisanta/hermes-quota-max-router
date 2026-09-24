from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from free_router.app import build_app
from free_router.config import ModelSpec, load_models
from free_router.discovery import audit_catalog
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
        worker_enabled=False,
    )


def test_real_completion_and_distinct_reviewer(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    with TestClient(_app(catalog, quota, tmp_path, fake)) as client:
        author = client.post("/v1/chat/completions", json=_body(), headers=_headers())
        assert author.status_code == 200
        data = author.json()
        assert data["choices"][0]["message"]["content"] == '{"ok":true}'
        assert data["router"]["provider"] in {"gemini", "groq", "cloudflare"}
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


def test_exhaustion_is_durable_202_never_fake_success(catalog, quota, tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    app = _app(
        catalog,
        quota,
        tmp_path,
        FakeProvider({"gemini", "groq", "cloudflare", "siliconflow", "openrouter"}),
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


def test_paid_openrouter_variant_is_rejected(catalog, monkeypatch):
    raw = json.loads(catalog.read_text())["models"][0]
    raw.update(
        provider="openrouter",
        model="anthropic/claude",
        evidence_url="https://openrouter.ai/pricing",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(ValueError, match="paid_openrouter_model"):
        ModelSpec.parse(raw)


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


@pytest.mark.asyncio
async def test_price_flip_demotes_openrouter(tmp_path, quota, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    now = datetime.now(UTC).isoformat()
    raw = {
        "provider": "openrouter",
        "model": "vendor/model:free",
        "workloads": ["journal"],
        "stages": ["author"],
        "context_tokens": 4096,
        "quota": {"rpm": 20, "rpd": 50, "tpm": 10000, "tpd": 100000},
        "evidence_url": "https://openrouter.ai/pricing",
        "evidence_checked_at": now,
        "account_checked_at": now,
        "no_billing": True,
        "zero_price": True,
        "smoke_passed": True,
        "quality_passed": True,
    }
    spec = ModelSpec.parse(raw)

    def handler(request):
        if "models" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "vendor/model:free", "pricing": {"prompt": "0.1", "completion": "0"}}
                    ]
                },
            )
        return httpx.Response(200, text="directory")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await audit_catalog([spec], quota, client=client, output=tmp_path / "audit.json")
    assert spec.id in report["demoted"]
    assert quota.cooldown_remaining(spec) > 0


def test_missing_verified_catalog_queues_without_call(tmp_path, quota, monkeypatch):
    monkeypatch.setenv("ROUTER_TOKEN_JOURNAL", "journal-token")
    fake = FakeProvider()
    with TestClient(_app(tmp_path / "absent.json", quota, tmp_path, fake)) as client:
        response = client.post("/v1/chat/completions", json=_body(), headers=_headers())
    assert response.status_code == 202
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
