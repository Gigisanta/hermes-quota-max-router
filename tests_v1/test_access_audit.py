from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from free_router.config import load_models
from free_router.discovery import (
    ACCESS_AUDIT_OUTPUT_TOKENS,
    ACCESS_AUDIT_PROMPT,
    OPENROUTER_MODELS,
    audit_access,
    audit_all,
)
from free_router.provider import ProviderClient
from free_router.quota import QuotaStore, Reservation


@pytest.mark.asyncio
async def test_audit_all_reserves_one_tiny_public_probe_per_model_without_rewriting_attestations(
    catalog: Path, quota: QuotaStore, tmp_path: Path
):
    models, rejected = load_models(catalog)
    assert not rejected
    original_catalog = catalog.read_bytes()
    remaining_before = {spec.id: quota.remaining_daily_requests(spec) for spec in models}
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == OPENROUTER_MODELS:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "test/model:free",
                            "context_length": 8192,
                            "pricing": {"prompt": "0", "completion": "0", "request": "0"},
                        }
                    ]
                },
            )
        if request.method == "GET":
            return httpx.Response(200, text="reference directory")

        body = json.loads(request.content)
        calls.append(body)
        assert body["messages"] == [{"role": "user", "content": ACCESS_AUDIT_PROMPT}]
        assert len(ACCESS_AUDIT_PROMPT.encode("utf-8")) < 180
        limit_key = "max_completion_tokens" if body["model"] == "test-cerebras" else "max_tokens"
        assert body[limit_key] == ACCESS_AUDIT_OUTPUT_TOKENS
        assert body["temperature"] == 0.0
        assert body["stream"] is False
        spec_model = body["model"]
        return httpx.Response(
            200,
            json={
                "model": spec_model,
                "choices": [
                    {
                        "message": {"content": "La biblioteca municipal abre el martes a las 10."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 24, "completion_tokens": 11},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ProviderClient(client)
    catalog_output = tmp_path / "discovery.json"
    access_output = tmp_path / "access-audit.json"
    try:
        report = await audit_all(
            models,
            quota,
            provider,
            catalog_output=catalog_output,
            access_output=access_output,
        )
    finally:
        await provider.close()

    assert len(calls) == len(models)
    assert all(row["status"] == "passed" for row in report["access"]["models"])
    assert report["access"]["audit_persisted"] is True
    assert {row["model"] for row in calls} == {spec.model for spec in models}
    assert catalog.read_bytes() == original_catalog
    for spec in models:
        assert quota.remaining_daily_requests(spec) == remaining_before[spec.id] - 1
    persisted = access_output.read_text(encoding="utf-8")
    assert ACCESS_AUDIT_PROMPT not in persisted
    assert "La biblioteca municipal abre" not in persisted
    assert "test-key" not in persisted
    assert "content" not in persisted
    assert catalog_output.exists()


@pytest.mark.asyncio
async def test_provider_access_failure_cools_down_without_storing_response_or_key(
    catalog: Path, quota: QuotaStore, tmp_path: Path
):
    models, _ = load_models(catalog)
    spec = next(model for model in models if model.provider == "gemini")
    leaked_text = "private upstream body and test-key"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=leaked_text)

    provider = ProviderClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    output = tmp_path / "access-audit.json"
    try:
        report = await audit_access([spec], quota, provider, output=output)
    finally:
        await provider.close()

    row = report["models"][0]
    assert row["status"] == "failed"
    assert row["reason"] == "upstream_access_revoked"
    assert row["retry_after"] == 60
    assert quota.cooldown_remaining(spec) > 0
    persisted = output.read_text(encoding="utf-8")
    assert leaked_text not in persisted
    assert "test-key" not in persisted
    assert "content" not in persisted


@pytest.mark.asyncio
async def test_editorial_smoke_failure_cools_down_and_discards_model_output(
    catalog: Path, quota: QuotaStore, tmp_path: Path
):
    models, _ = load_models(catalog)
    spec = next(model for model in models if model.provider == "groq")
    bad_output = "La biblioteca municipal abre el jueves a las 11."

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": bad_output}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 25, "completion_tokens": 12},
            },
        )

    provider = ProviderClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    output = tmp_path / "access-audit.json"
    try:
        report = await audit_access([spec], quota, provider, output=output)
    finally:
        await provider.close()

    assert report["models"][0]["reason"] == "editorial_smoke_failed"
    assert quota.cooldown_remaining(spec) > 0
    assert bad_output not in output.read_text(encoding="utf-8")


def test_editorial_smoke_rejects_extra_facts():
    from free_router.discovery import _editorial_smoke_passed

    assert _editorial_smoke_passed("La biblioteca municipal abre el martes a las 10.")
    assert not _editorial_smoke_passed(
        "La biblioteca municipal abre el martes a las 10 y cierra a las 16."
    )


@pytest.mark.asyncio
async def test_openrouter_is_not_probed_when_live_free_price_catalog_is_unavailable(
    catalog: Path, quota: QuotaStore, tmp_path: Path
):
    models, _ = load_models(catalog)
    spec = next(model for model in models if model.provider == "openrouter")
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == OPENROUTER_MODELS:
            return httpx.Response(503)
        if request.method == "GET":
            return httpx.Response(200, text="reference directory")
        posted.append(request)
        return httpx.Response(200, json={})

    provider = ProviderClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        report = await audit_all(
            [spec],
            quota,
            provider,
            catalog_output=tmp_path / "discovery.json",
            access_output=tmp_path / "access-audit.json",
        )
    finally:
        await provider.close()

    assert posted == []
    assert report["access"]["models"][0]["status"] == "skipped"
    assert report["access"]["models"][0]["reason"] == "openrouter_free_catalog_unavailable"
    assert quota.cooldown_remaining(spec) > 0


@pytest.mark.asyncio
async def test_missing_reserved_quota_never_sends_probe(tmp_path: Path):
    class ExhaustedQuota:
        def __init__(self):
            self.cooldowns: list[tuple[str, int]] = []

        def cooldown_remaining(self, _spec):
            return 0

        def reserve(self, *_args, **_kwargs):
            return Reservation((), (), 37)

        def cool_down(self, spec, seconds, **_kwargs):
            self.cooldowns.append((spec.id, seconds))

    spec = type(
        "Spec",
        (),
        {
            "id": "gemini/public",
            "provider": "gemini",
            "model": "public",
            "quota": type("Q", (), {"rpm": 10, "rpd": 10, "tpm": 1000, "tpd": 10000})(),
        },
    )()
    quota = ExhaustedQuota()
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={})

    provider = ProviderClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        report = await audit_access([spec], quota, provider, output=tmp_path / "audit.json")
    finally:
        await provider.close()

    assert posted == []
    assert report["models"][0]["reason"] == "audit_quota_reservation_unavailable"
    assert quota.cooldowns == [(spec.id, 37)]


def test_audit_cli_runs_catalog_and_access_audits_without_printing_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from free_router import cli

    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    class RedisFactory:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            return fake_redis

    class FakeProvider:
        closed = False

        async def close(self):
            self.closed = True

    provider = FakeProvider()
    seen: dict = {}

    async def fake_audit_all(models, quota, audit_provider):
        seen.update(models=models, quota=quota, provider=audit_provider)
        return {"catalog": {"sources": {}}, "access": {"models": []}}

    monkeypatch.setattr(cli.redis, "Redis", RedisFactory)
    monkeypatch.setattr(cli, "load_models", lambda: ([], {}))
    monkeypatch.setattr(cli, "ProviderClient", lambda: provider)
    monkeypatch.setattr(cli, "audit_all", fake_audit_all)
    monkeypatch.setattr(sys, "argv", ["quotamax", "audit"])

    cli.main()

    output = capsys.readouterr().out
    assert '"catalog"' in output and '"access"' in output
    assert "test-key" not in output
    assert seen["models"] == []
    assert seen["provider"] is provider
    assert provider.closed


def test_application_daily_audit_loop_runs_access_audit(
    catalog: Path, quota: QuotaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import free_router.app as app_module

    called = threading.Event()

    async def fake_audit_all(models, actual_quota, provider):
        assert models
        assert actual_quota is quota
        called.set()
        return {"catalog": {}, "access": {"models": []}}

    class FakeProvider:
        async def close(self):
            pass

        async def complete(self, *_args, **_kwargs):
            raise AssertionError("the patched audit must not make provider calls")

    monkeypatch.setattr(app_module, "audit_all", fake_audit_all)
    app = app_module.build_app(
        quota=quota,
        provider=FakeProvider(),
        queue=app_module.JobQueue(tmp_path / "jobs.sqlite3"),
        catalog_path=catalog,
        daily_peak={"journal": 10, "simon-news": 10, "cactus-brief": 10},
        worker_enabled=True,
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert called.wait(2)
