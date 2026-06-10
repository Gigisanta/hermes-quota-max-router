"""Tests for SessionContext and SessionManager (Phase 12)."""
import pytest

from core.session import SessionContext, SessionManager


# --- SessionContext ---

def test_session_appends_user_and_assistant() -> None:
    s = SessionContext("s1")
    s.append("user", "hi")
    s.append("assistant", "hello!", model_used="deepseek/x", tokens=10)
    assert s.turn_count == 1
    assert s.last_model == "deepseek/x"
    assert s.quota_consumed == {"deepseek/x": 10}
    assert len(s.history) == 2


def test_session_quota_aggregates_per_model() -> None:
    s = SessionContext("s1")
    s.append("assistant", "a", model_used="m/a", tokens=5)
    s.append("assistant", "b", model_used="m/a", tokens=15)
    s.append("assistant", "c", model_used="m/b", tokens=7)
    assert s.quota_consumed == {"m/a": 20, "m/b": 7}


def test_session_history_caps_at_maxlen() -> None:
    s = SessionContext("s1", history_max_turns=2)
    for i in range(10):
        s.append("user", f"msg {i}")
    assert len(s.history) == 4  # 2 turns * 2 messages (user+assistant slots)

    # assistant records with no model_used don't count toward quota
    s2 = SessionContext("s2", history_max_turns=5)
    for i in range(20):
        s2.append("assistant", f"resp {i}")
    assert s2.quota_consumed == {}


def test_history_for_prompt_respects_max_chars() -> None:
    s = SessionContext("s1")
    s.append("user", "x" * 1000)
    s.append("assistant", "y" * 1000)
    s.append("user", "z" * 1000)
    out = s.history_for_prompt(max_chars=1500)
    total = sum(len(m["content"]) for m in out)
    assert total <= 1500 + 10  # allow for the ellipsis


def test_history_for_prompt_empty_session() -> None:
    s = SessionContext("s1")
    assert s.history_for_prompt() == []


def test_session_summary() -> None:
    s = SessionContext("s1")
    s.append("user", "hi")
    s.append("assistant", "hello", model_used="m/a", tokens=5)
    summary = s.summary()
    assert summary["session_id"] == "s1"
    assert summary["turn_count"] == 1
    assert summary["last_model"] == "m/a"
    assert summary["quota_consumed"] == {"m/a": 5}
    assert summary["age_s"] >= 0


# --- SessionManager ---

def test_get_or_create_returns_same_session() -> None:
    m = SessionManager()
    a = m.get_or_create("s1")
    b = m.get_or_create("s1")
    assert a is b


def test_get_or_create_creates_new() -> None:
    m = SessionManager()
    a = m.get_or_create("s1")
    b = m.get_or_create("s2")
    assert a is not b
    assert m.count() == 2


def test_manager_evicts_oldest_when_full() -> None:
    m = SessionManager(max_sessions=2)
    s1 = m.get_or_create("s1")
    # Force s1 to be the oldest
    s1.created_at -= 100
    m.get_or_create("s2")
    s3 = m.get_or_create("s3")
    assert m.count() == 2
    assert m.get("s1") is None  # evicted
    assert m.get("s2") is not None
    assert m.get("s3") is s3


def test_drop_removes_session() -> None:
    m = SessionManager()
    m.get_or_create("s1")
    assert m.drop("s1") is True
    assert m.get("s1") is None


def test_drop_returns_false_for_unknown() -> None:
    m = SessionManager()
    assert m.drop("nope") is False


def test_all_summaries() -> None:
    m = SessionManager()
    m.get_or_create("a")
    m.get_or_create("b")
    summaries = m.all_summaries()
    assert len(summaries) == 2
    assert {s["session_id"] for s in summaries} == {"a", "b"}


def test_thread_safe_concurrent_get_or_create() -> None:
    """Multiple threads racing on the same session_id should all get the same."""
    import threading
    m = SessionManager()
    results: list[SessionContext] = []
    lock = threading.Lock()

    def worker() -> None:
        s = m.get_or_create("race")
        with lock:
            results.append(s)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(s is results[0] for s in results)
