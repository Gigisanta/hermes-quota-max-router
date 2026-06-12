"""Tests for the FastAPI server (Phase 7).

Uses FastAPI's TestClient (sync over httpx) to exercise endpoints.
The router is in `live=False` mode so no network is hit.
"""

from fastapi.testclient import TestClient

from server.app import build_app


def test_health_returns_ok() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.get("/v1/router/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["live_mode"] is False
    assert body["models_count"] >= 1


def test_list_models_includes_seed() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert any("deepseek" in m["id"] for m in body["data"])


def test_quota_endpoint_returns_per_model() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.get("/v1/router/quota")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) >= 1
    first = body["data"][0]
    assert "model_id" in first
    assert "remaining" in first


def test_chat_completion_routes_to_free_model() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "Refactor this Python function and add tests"},
                ],
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert "choices" in body
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"]  # non-empty
    assert body["usage"]["total_tokens"] > 0
    # Hermes extension: router_decision is included
    assert body["router_decision"] is not None
    assert body["router_decision"]["chosen_strategy"] == "direct"
    # Phase 8: security headers present
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "no-referrer"


def test_chat_completion_with_explicit_model() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai-codex/gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "openai-codex/gpt-5.5"
    assert body["router_decision"]["reasoning"] == "Caller-specified model; orchestration skipped."


def test_chat_completion_empty_messages_400() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 400


def test_metrics_endpoint_increments() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        # Make 2 calls
        for msg in ["Refactor Python", "Write a story"]:
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": msg}],
                },
            )
        r = client.get("/v1/router/metrics")
    assert r.status_code == 200
    body = r.text
    assert "router_calls_total" in body
    assert "router_tokens_total" in body
    assert "router_call_duration_seconds_avg" in body


def test_metrics_have_prometheus_format() -> None:
    app = build_app(live=False)
    with TestClient(app) as client:
        client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "test"}],
            },
        )
        r = client.get("/v1/router/metrics")
    body = r.text
    # Each line should be either a counter, gauge, or empty
    for line in body.strip().split("\n"):
        if not line:
            continue
        assert any(
            line.startswith(prefix)
            for prefix in (
                "router_calls_total",
                "router_tokens_total",
                "router_errors_total",
                "router_call_duration_seconds",
            )
        ), f"unexpected line: {line}"


def test_response_shape_matches_openai() -> None:
    """Sanity: the response has the exact OpenAI field names."""
    app = build_app(live=False)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    body = r.json()
    required = {"id", "object", "created", "model", "choices", "usage"}
    assert required <= set(body.keys())
    assert body["object"] == "chat.completion"
    assert set(body["usage"].keys()) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert set(body["choices"][0].keys()) == {"index", "message", "finish_reason"}
    assert set(body["choices"][0]["message"].keys()) == {"role", "content"}
