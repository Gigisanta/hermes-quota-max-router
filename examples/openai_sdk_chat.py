"""The router is OpenAI-compatible: the official SDK works unchanged.

Requires:  pip install openai

Usage:
    python examples/openai_sdk_chat.py "Explain free-tier routing in one line."
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("ROUTER_BASE_URL", "http://127.0.0.1:8080/v1"),
    api_key=os.environ.get("ROUTER_MASTER_KEY", "sk-router-dev-change-me"),
)


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Explain free-tier routing in one line."
    completion = client.chat.completions.create(
        model="auto",  # orchestrator picks a verified free model
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"model used: {completion.model}")
    print(completion.choices[0].message.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
