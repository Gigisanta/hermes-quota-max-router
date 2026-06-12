"""Run the router in live mode on port 8088 (matching the Hermes plugin default).

This is the production-style launcher: it points at the real Gemini key
already in the env (or hardcoded fallback), so chat completions go through
the actual LLM provider instead of the stub path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Live Gemini key (rotated by user; safe in local-scope script, never logged).
os.environ.setdefault(
    "GEMINI_API_KEY",
    "__REDACTED_GEMINI_KEY__",
)
# Force live mode.
os.environ["ROUTER_LIVE"] = "1"
# Match the plugin default port.
os.environ.setdefault("ROUTER_PORT", "8088")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("ROUTER_PORT", "8088"))
    uvicorn.run("server.app:app", host="127.0.0.1", port=port, log_level="warning")
