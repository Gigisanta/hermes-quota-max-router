"""Live E2E verification — uses real API keys passed via stdin.

Run via:
  echo "$GEMINI_KEY" | python scripts/live_e2e.py
  # OR
  python scripts/live_e2e.py --gemini KEY --openrouter KEY --deepseek KEY

Tests 3 free tiers end-to-end with the actual API. Measures latency,
token consumption, and response quality.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--gemini", default=os.environ.get("GEMINI_API_KEY", ""))
    p.add_argument("--deepseek", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    p.add_argument("--openrouter", default=os.environ.get("OPENROUTER_API_KEY", ""))
    args = p.parse_args()

    if not args.gemini and not args.openrouter:
        print("Provide --gemini KEY or --openrouter KEY", file=sys.stderr)
        return 1

    # Set into os.environ for litellm
    if args.gemini:
        os.environ["GEMINI_API_KEY"] = args.gemini
    if args.deepseek:
        os.environ["DEEPSEEK_API_KEY"] = args.deepseek
    if args.openrouter:
        os.environ["OPENROUTER_API_KEY"] = args.openrouter

    from litellm import completion

    cases: list[tuple[str, str]] = []
    if args.gemini:
        cases.append(("gemini/gemini-2.5-flash", "Reply with the single word PONG."))
    if args.openrouter:
        cases.append((
            "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            "Reply with the single word PONG.",
        ))
    if args.deepseek:
        cases.append(("deepseek/deepseek-chat", "Reply with the single word PONG."))

    results = []
    for model, prompt in cases:
        print(f"=== {model} ===")
        t = time.monotonic()
        try:
            r = completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=20,
                timeout=30,
            )
            dt = time.monotonic() - t
            content = (r.choices[0].message.content or "").strip()
            u = r.usage
            cost = float(r._hidden_params.get("response_cost", 0) or 0)
            results.append({
                "model": model, "ok": True, "latency_s": round(dt, 3),
                "in": u.prompt_tokens, "out": u.completion_tokens,
                "total": u.total_tokens, "cost": cost,
                "content": content,
            })
            print(f"  ok: {dt:.2f}s | tokens: {u.total_tokens} | cost: ${cost:.6f}")
            print(f"  content: {content[:80]!r}")
        except Exception as e:
            results.append({"model": model, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            print(f"  FAILED: {type(e).__name__}: {str(e)[:200]}")
        print()

    ok = sum(1 for r in results if r["ok"])
    total = len(results)
    print(f"=== SUMMARY: {ok}/{total} OK ===")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
