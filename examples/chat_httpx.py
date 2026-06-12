"""Minimal chat completion using httpx (already a project dependency).

Usage:
    python examples/chat_httpx.py "What is a token bucket?"
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("ROUTER_BASE_URL", "http://127.0.0.1:8080/v1")
KEY = os.environ.get("ROUTER_MASTER_KEY", "sk-router-dev-change-me")


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hello in five words."
    resp = httpx.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json={
            # "auto" lets the orchestrator pick the best free model for the task.
            "model": "auto",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    print(f"model used: {body['model']}")
    print(body["choices"][0]["message"]["content"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
