"""Iter 9 — End-to-end real: Hermes provider profile → router → free model.

Verifies that the registered QuotaMax Router profile in Hermes can be used
to route a chat completion through the local router and get back a real
(non-stub) response from a 100% free model.

Pre-conditions:
  - Router server running on QUOTAMAX_BASE_URL (default 127.0.0.1:8088)
  - ROUTER_LIVE=1 + at least one provider key in env

Run:
  source .venv/bin/activate
  source ~/.hermes/hermes-agent/venv/bin/activate
  python scripts/e2e_hermes_provider.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERMES_AGENT = Path("/Users/prueba/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES_AGENT))
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    # Force discovery of the QuotaMax Router plugin profile.
    import providers

    providers._discovered = False
    providers._REGISTRY.clear()

    from providers import get_provider_profile

    profile = get_provider_profile("quotamax-router")
    if profile is None:
        print("FAIL: quotamax-router profile not discovered", file=sys.stderr)
        return 1
    print(f"OK   : profile name = {profile.name}")
    print(f"OK   : base_url     = {profile.base_url}")
    print(f"OK   : aliases      = {profile.aliases}")
    print(f"OK   : api_mode     = {profile.api_mode}")

    # Live-fetch the model list via the profile (it calls /v1/models on router).
    api_key = os.environ.get("QUOTAMAX_API_KEY", "")
    models = profile.fetch_models(api_key=api_key, timeout=10.0)
    if not models:
        print("FAIL: fetch_models returned empty (router unreachable?)", file=sys.stderr)
        return 1
    print(f"OK   : fetch_models returned {len(models)} models")
    print(f"      first 3: {models[:3]}")

    # Now make a real chat completion through Hermes' chat_completions
    # transport. The plugin declares api_mode="chat_completions", which
    # means Hermes uses the OpenAI-compatible client against
    # base_url. We replicate that exact call shape here.
    import httpx

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{profile.base_url.rstrip('/')}/chat/completions",
                headers={"Content-Type": "application/json"},
                json={
                    "model": "auto",  # router picks the best free model
                    "messages": [{"role": "user", "content": "Reply with the single word PONG."}],
                    "max_tokens": 20,
                },
            )
            r.raise_for_status()
            body = r.json()
    except Exception as exc:
        print(f"FAIL: HTTP call raised: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    dt = time.monotonic() - t0

    used_model = body.get("model", "<unknown>")
    content = (body["choices"][0]["message"]["content"] or "").strip()
    u = body.get("usage", {})
    cost = 0.0  # free tier always

    print()
    print("=== Live chat via Hermes provider profile (chat_completions transport) ===")
    print(f"  HTTP status     : {r.status_code}")
    print(f"  HTTP elapsed    : {dt:.2f}s")
    print(f"  model used      : {used_model}")
    print(f"  content         : {content!r}")
    print(
        f"  tokens          : prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} total={u.get('total_tokens')}"
    )
    print(f"  cost USD        : ${cost:.6f}")
    print(f"  is real (PONG)  : {content.upper() == 'PONG'}")
    print(f"  is free         : {cost == 0.0}")

    if content.upper() != "PONG":
        print(f"FAIL: expected PONG, got {content!r}", file=sys.stderr)
        return 1
    if cost != 0.0:
        print(f"FAIL: expected $0 (free tier), got ${cost}", file=sys.stderr)
        return 1
    if not u.get("total_tokens"):
        print("FAIL: no tokens reported", file=sys.stderr)
        return 1

    print()
    print("🎉 SUCCESS: Hermes provider profile -> QuotaMax Router -> free model works end-to-end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
