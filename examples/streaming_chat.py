"""Streaming chat completion — prints deltas as they arrive (SSE).

Usage:
    python examples/streaming_chat.py "Write a haiku about routers."
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE_URL = os.environ.get("ROUTER_BASE_URL", "http://127.0.0.1:8080/v1")
KEY = os.environ.get("ROUTER_MASTER_KEY", "sk-router-dev-change-me")


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Write a haiku about routers."
    with httpx.stream(
        "POST",
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json={
            "model": "auto",
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line.removeprefix("data: ").strip()
            if payload == "[DONE]":
                break
            delta = json.loads(payload)["choices"][0]["delta"]
            print(delta.get("content", ""), end="", flush=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
