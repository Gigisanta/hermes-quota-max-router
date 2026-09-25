"""Actual provider calls. There is no stub mode and no paid fallback."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from free_router.config import ModelSpec
from free_router.quota import QuotaStore


@dataclass(frozen=True)
class ProviderFailure(Exception):
    reason: str
    retry_after: int = 60
    permanent: bool = False


class ProviderClient:
    def __init__(self, client: httpx.AsyncClient | None = None, *, quota: QuotaStore | None = None):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(90, connect=10), trust_env=False
        )
        self.quota = quota

    async def close(self) -> None:
        await self.client.aclose()

    async def complete(
        self, spec: ModelSpec, messages: list[dict], max_tokens: int, temperature: float
    ) -> dict:
        key = os.getenv(spec.api_key_env, "")
        if not key:
            raise ProviderFailure("missing_provider_key", permanent=True)
        body = {
            "model": spec.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        body["max_tokens"] = max_tokens
        slot_token = None
        if spec.provider == "simplellm":
            if self.quota is None:
                raise ProviderFailure("quota_store_unavailable", 60)
            try:
                slot_token = self.quota.acquire_provider_slot(spec.provider)
            except RuntimeError as exc:
                raise ProviderFailure("quota_store_unavailable", 60) from exc
            if slot_token is None:
                raise ProviderFailure("provider_concurrency_busy", 5)
        try:
            request = self.client.post(
                spec.api_base.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
            )
            response = (
                await asyncio.wait_for(request, timeout=120)
                if spec.provider == "simplellm"
                else await request
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderFailure("upstream_network_error", 60) from exc
        finally:
            if slot_token is not None:
                assert self.quota is not None
                self.quota.release_provider_slot(spec.provider, slot_token)
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after", "60")
            try:
                delay = int(float(retry_after))
            except ValueError:
                try:
                    delay = int(
                        (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = 60
            delay = max(1, min(86400, delay))
            raise ProviderFailure("upstream_rate_limited", delay)
        if response.status_code in (401, 403, 404):
            raise ProviderFailure("upstream_access_revoked", permanent=True)
        if response.status_code >= 500:
            raise ProviderFailure("upstream_unavailable", 60)
        if response.status_code >= 400:
            raise ProviderFailure("upstream_rejected_request", permanent=True)
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty_content")
            finish_reason = choice["finish_reason"]
            if finish_reason != "stop":
                raise ValueError("incomplete_upstream_response")
            actual_model = data["model"]
            if not isinstance(actual_model, str) or actual_model != spec.model:
                raise ValueError("unverified_upstream_model")
            usage = data.get("usage") or {}
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if not isinstance(prompt, int) or prompt < 0:
                prompt = None
            if not isinstance(completion, int) or completion < 0:
                completion = None
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderFailure("malformed_upstream_response", 120) from exc
        return {
            "content": content,
            "finish_reason": finish_reason,
            "actual_model": actual_model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "headers": {
                "remaining_requests": response.headers.get("x-ratelimit-remaining-requests"),
                "remaining_tokens": response.headers.get("x-ratelimit-remaining-tokens"),
            },
        }
