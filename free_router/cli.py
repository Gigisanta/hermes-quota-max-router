"""Operational entrypoint; service is deliberately loopback-only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import redis
import uvicorn

from free_router.config import load_models
from free_router.discovery import audit_all
from free_router.provider import ProviderClient
from free_router.queue import JobQueue
from free_router.quota import QuotaStore
from free_router.router import EditorialRouter


def main() -> None:
    parser = argparse.ArgumentParser(prog="quotamax")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Serve on 127.0.0.1:8123 only")
    sub.add_parser("status", help="Show verified reserve without secrets")
    sub.add_parser("audit", help="Check official catalogs and save candidates")
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("free_router.app:app", host="127.0.0.1", port=8123, workers=1)
        return
    client = redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True, socket_timeout=2
    )
    quota = QuotaStore(client)
    models, rejected = load_models()
    if args.command == "audit":

        async def run_audit() -> dict:
            provider = ProviderClient()
            try:
                return await audit_all(models, quota, provider)
            finally:
                await provider.close()

        result = asyncio.run(run_audit())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    redis_ok = quota.healthy()
    try:
        peak = json.loads(
            Path(os.getenv("ROUTER_DAILY_PEAK_FILE", "var/daily-peak.json")).read_text()
        )
    except (OSError, ValueError):
        peak = {}
    router = EditorialRouter(
        quota,
        ProviderClient(),
        Path(os.getenv("ROUTER_VERIFIED_MODELS", "var/verified-models.json")),
        peak,
    )
    queue = JobQueue(Path(os.getenv("ROUTER_QUEUE_DB", "var/queue.sqlite3")))
    print(
        json.dumps(
            {
                "redis": redis_ok,
                "reserve": router.status(),
                "queue": queue.counts(),
                "rejected": rejected,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
