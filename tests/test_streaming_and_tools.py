"""Iter 13 — Streaming and tool-calling in /v1/chat/completions."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.app import build_app


@pytest.fixture
def client() -> TestClient:
    app = build_app(live=False, use_layered=False)
    return TestClient(app)


def test_blocking_response_includes_tools_field_in_schema(client: TestClient) -> None:
    """The request schema accepts a `tools` field and the response still
    works when no tools are actually invoked (i.e. plain text reply)."""
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "ping"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "choices" in body
    # In stub mode, the model doesn't actually call tools, so message
    # should just contain a normal text reply.
    msg = body["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert "ping" in msg["content"].lower() or "stub" in msg["content"].lower()
    # The schema only adds tool_calls when the model actually emitted one.
    assert "tool_calls" not in msg or msg.get("tool_calls") is None


def test_streaming_returns_sse_chunks(client: TestClient) -> None:
    """stream=true must return Server-Sent Events with at least one
    delta chunk and a final [DONE] marker."""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        chunks: list[dict] = []
        done = False
        for line in r.iter_lines():
            if not line:
                continue
            if line == "data: [DONE]":
                done = True
                break
            if line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
                chunks.append(payload)
    assert done, "stream did not emit [DONE]"
    assert len(chunks) >= 1, "no chunks received"
    first = chunks[0]
    assert first["object"] == "chat.completion.chunk"
    assert "choices" in first
    delta = first["choices"][0]["delta"]
    # The stub yields the whole reply in a single delta; the role
    # should be "assistant" and the content should mention the prompt.
    assert delta.get("role") == "assistant"
    assert delta.get("content")


def test_streaming_with_tools_in_request(client: TestClient) -> None:
    """stream=true + tools= must be accepted (not a 400)."""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "What's the weather in Buenos Aires?"}],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        },
    ) as r:
        assert r.status_code == 200
        # Drain the stream so the connection closes.
        for _ in r.iter_lines():
            pass


def test_chat_completion_request_accepts_tool_choice(client: TestClient) -> None:
    """`tool_choice="auto"` is valid OpenAI syntax; the router must not 400."""
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "ping"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
        },
    )
    assert r.status_code == 200, r.text
