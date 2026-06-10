"""Server tests for Phase 12: multi-turn session support."""
from fastapi.testclient import TestClient

from server.app import build_app


def test_chat_completion_with_session_id() -> None:
    app = build_app(live=False)
    with TestClient(app) as c:
        r1 = c.post("/v1/chat/completions", json={
            "session_id": "test-session-1",
            "messages": [{"role": "user", "content": "Refactor Python code"}],
        })
        r2 = c.post("/v1/chat/completions", json={
            "session_id": "test-session-1",
            "messages": [{"role": "user", "content": "Add tests too"}],
        })
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both should route to the same model (continuity)
    assert r1.json()["model"] == r2.json()["model"]


def test_sessions_endpoint_lists_active() -> None:
    app = build_app(live=False)
    with TestClient(app) as c:
        c.post("/v1/chat/completions", json={
            "session_id": "active-1",
            "messages": [{"role": "user", "content": "hi"}],
        })
        c.post("/v1/chat/completions", json={
            "session_id": "active-2",
            "messages": [{"role": "user", "content": "hello"}],
        })
        r = c.get("/v1/router/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {s["session_id"] for s in body["data"]}
    assert "active-1" in ids
    assert "active-2" in ids


def test_health_includes_active_sessions_count() -> None:
    app = build_app(live=False)
    with TestClient(app) as c:
        c.post("/v1/chat/completions", json={
            "session_id": "x",
            "messages": [{"role": "user", "content": "hi"}],
        })
        h = c.get("/v1/router/health")
    assert h.status_code == 200
    body = h.json()
    assert body["active_sessions"] >= 1


def test_session_summary_includes_quota() -> None:
    app = build_app(live=False)
    with TestClient(app) as c:
        c.post("/v1/chat/completions", json={
            "session_id": "quota-track",
            "messages": [{"role": "user", "content": "Refactor Python"}],
        })
        r = c.get("/v1/router/sessions")
    sessions = {s["session_id"]: s for s in r.json()["data"]}
    s = sessions.get("quota-track")
    assert s is not None
    assert "quota_consumed" in s
    assert s["turn_count"] >= 1
    # Quota must reflect a non-zero token spend
    total_spent = sum(s["quota_consumed"].values())
    assert total_spent > 0


def test_chat_without_session_id_works() -> None:
    app = build_app(live=False)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "no session here"}],
        })
    assert r.status_code == 200
