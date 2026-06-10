"""Session context — Phase 12.

Tracks per-session state across multiple turns:
  - Conversation history (last N messages, capped)
  - Quota deltas consumed during this session
  - The model that "won" the previous turn (for routing continuity)

Sessions are identified by a caller-supplied session_id. State is kept
in-memory keyed by session_id. For persistence, swap the dict for
Redis or a SQLite table (not in scope for Phase 12).

The session is also where the router engine notes the routing decision
of the previous turn. If the user is mid-conversation with deepseek,
the next turn doesn't need to re-orchestrate from scratch — a cheap
"continue with same model" path keeps the conversation coherent.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnRecord:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    model_used: str | None = None
    tokens: int = 0


class SessionContext:
    """A single conversation session.

    Thread-safe (sessions can be shared by async workers).
    """

    def __init__(
        self,
        session_id: str,
        history_max_turns: int = 20,
    ) -> None:
        self.session_id = session_id
        self.history: deque[TurnRecord] = deque(maxlen=history_max_turns * 2)
        self.quota_consumed: dict[str, int] = {}
        self.last_model: str | None = None
        self.turn_count: int = 0
        self.created_at: float = time.time()
        self._lock = threading.Lock()

    def append(self, role: str, content: str, model_used: str | None = None,
               tokens: int = 0) -> None:
        with self._lock:
            self.history.append(TurnRecord(
                role=role, content=content,
                model_used=model_used, tokens=tokens,
            ))
            if role == "assistant" and model_used:
                self.last_model = model_used
                self.quota_consumed[model_used] = (
                    self.quota_consumed.get(model_used, 0) + tokens
                )
            if role == "user":
                self.turn_count += 1

    def history_for_prompt(self, max_chars: int = 4000) -> list[dict]:
        """Return the recent history as OpenAI-style messages, truncated."""
        with self._lock:
            out: list[dict] = []
            total = 0
            for rec in reversed(list(self.history)):
                content = rec.content
                if total + len(content) > max_chars:
                    content = content[: max(0, max_chars - total)] + "…"
                out.insert(0, {"role": rec.role, "content": content})
                total += len(content)
                if total >= max_chars:
                    break
            return out

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "turn_count": self.turn_count,
                "last_model": self.last_model,
                "history_len": len(self.history),
                "quota_consumed": dict(self.quota_consumed),
                "age_s": time.time() - self.created_at,
            }


class SessionManager:
    """In-memory registry of active sessions. Thread-safe."""

    def __init__(self, history_max_turns: int = 20, max_sessions: int = 1000) -> None:
        self._sessions: dict[str, SessionContext] = {}
        self._max_sessions = max_sessions
        self._history_max = history_max_turns
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> SessionContext:
        with self._lock:
            if session_id in self._sessions:
                return self._sessions[session_id]
            if len(self._sessions) >= self._max_sessions:
                # Evict the oldest session (LRU-ish: by created_at)
                oldest_id = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].created_at,
                )
                del self._sessions[oldest_id]
            sess = SessionContext(session_id, self._history_max)
            self._sessions[session_id] = sess
            return sess

    def get(self, session_id: str) -> SessionContext | None:
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def all_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.summary() for s in self._sessions.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
