"""Operational snapshot: quota, cost, and circuit-breaker state.

Usage:
    python examples/quota_status.py
"""

from __future__ import annotations

import os

import httpx

BASE_URL = os.environ.get("ROUTER_BASE_URL", "http://127.0.0.1:8080/v1")


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        health = client.get("/router/health").json()
        print(f"status: {health.get('status')}  models: {health.get('models_count')}")

        cost = client.get("/router/cost").json()
        print(f"cost so far: {cost}")

        quota = client.get("/router/quota").json()
        rows = quota if isinstance(quota, list) else quota.get("quotas", quota.get("data", []))
        if isinstance(rows, list):
            print(f"\ntop 10 quotas ({len(rows)} tracked):")
            for row in rows[:10]:
                print(f"  {row}")
        else:
            print(f"\nquota: {rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
