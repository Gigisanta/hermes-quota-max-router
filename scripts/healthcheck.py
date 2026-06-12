"""QuotaMax Router self-test healthcheck.

Run modes:
  1. Manual smoke test:    python scripts/healthcheck.py
  2. One-shot CI run:      python scripts/healthcheck.py --once
  3. Long-running daemon:  python scripts/healthcheck.py --daemon
                           (runs every ROUTER_HEALTHCHECK_INTERVAL_S, default 6h)

What it does each cycle:
  1. GET  {base}/v1/router/health     — liveness + version
  2. POST {base}/v1/chat/completions  — trivial prompt, model=auto
  3. Verify the response is real (not a [stub:...] placeholder)
  4. On any failure, append a JSONL record to logs/alerts.jsonl

Exit codes:
  0  every check passed
  1  router unreachable
  2  router reachable but in stub mode (no upstream keys)
  3  chat completion returned an error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALERTS_PATH = REPO_ROOT / "logs" / "alerts.jsonl"
DEFAULT_BASE = os.environ.get("QUOTAMAX_BASE_URL", "http://127.0.0.1:8088/v1")
DEFAULT_INTERVAL_S = int(os.environ.get("ROUTER_HEALTHCHECK_INTERVAL_S", "21600"))  # 6h

log = logging.getLogger("quotamax.healthcheck")


def _get_base_url() -> str:
    return os.environ.get("QUOTAMAX_BASE_URL", DEFAULT_BASE).rstrip("/")


def _is_stub_response(body: dict) -> bool:
    content = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
    return content.startswith("[stub:") or content.startswith("[Stub mode")


def _alert(level: str, check: str, message: str, **details: object) -> None:
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "level": level,
        "check": check,
        "message": message,
        **details,
    }
    with ALERTS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    log.log(
        logging.ERROR if level == "error" else logging.WARNING,
        f"[{level}] {check}: {message}  {details}",
    )


def run_once(base_url: str | None = None) -> int:
    import httpx

    base = (base_url or _get_base_url()).rstrip("/")
    overall_ok = True

    # 1) Liveness check
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{base}/router/health")
            r.raise_for_status()
            health = r.json()
    except httpx.HTTPError as exc:
        _alert("error", "liveness", f"GET /router/health failed: {exc}")
        return 1
    log.info(
        "liveness OK: status=%s version=%s models=%d live=%s",
        health.get("status"),
        health.get("version"),
        health.get("models_count"),
        health.get("live_mode"),
    )
    if health.get("status") != "ok":
        _alert("error", "liveness", "router status not 'ok'", **health)
        overall_ok = False

    # 2) Trivial chat completion
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{base}/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                },
            )
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as exc:
        _alert("error", "chat", f"POST /chat/completions failed: {exc}")
        return 3
    content = (body.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    is_stub = _is_stub_response(body)
    used = body.get("model", "<unknown>")
    u = body.get("usage", {}) or {}
    log.info("chat OK: model=%s tokens=%d stub=%s", used, u.get("total_tokens", 0), is_stub)

    if is_stub:
        _alert(
            "warning",
            "chat",
            "Router is in STUB mode (no live upstream keys).",
            content_preview=content[:120],
            live_mode=health.get("live_mode"),
        )
        return 2  # distinct exit code so cron can alert differently

    if not content:
        _alert("error", "chat", "Empty response content", **body)
        return 3

    log.info("✓ all checks passed")
    return 0 if overall_ok else 4


def run_daemon(interval_s: int) -> int:
    log.info("starting healthcheck daemon, interval=%ds, base=%s", interval_s, _get_base_url())
    while True:
        rc = run_once()
        if rc not in (0, 2):  # 0=ok, 2=stub are not fatal in daemon
            log.warning("non-fatal failure rc=%d (continuing)", rc)
        time.sleep(interval_s)


def main() -> int:
    p = argparse.ArgumentParser(description="QuotaMax Router self-test")
    p.add_argument("--once", action="store_true", help="Run a single check and exit (default)")
    p.add_argument("--daemon", action="store_true", help="Run forever, checking every N seconds")
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        help=f"Interval in seconds (default: {DEFAULT_INTERVAL_S} = 6h)",
    )
    p.add_argument(
        "--base-url", default=None, help="Router base URL (default: $QUOTAMAX_BASE_URL or 127.0.0.1:8088)"
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.daemon:
        return run_daemon(args.interval)
    return run_once(args.base_url)


if __name__ == "__main__":
    raise SystemExit(main())
