"""Deterministic routing among account-verified, editorial-quality free models."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from free_router.config import WORKLOADS, ModelSpec, load_models
from free_router.provider import ProviderClient, ProviderFailure
from free_router.quota import QuotaStore


@dataclass(frozen=True)
class Attempt:
    response: dict | None
    retry_after: int
    reason: str


class EditorialRouter:
    def __init__(
        self,
        quota: QuotaStore,
        provider: ProviderClient,
        catalog_path: Path,
        daily_peak: dict[str, int | dict[str, int]],
        production_workloads: tuple[str, ...] | None = None,
    ):
        self.quota = quota
        self.provider = provider
        self.catalog_path = catalog_path
        self.daily_peak: dict[str, int] = {}
        self.request_tokens: dict[str, int] = {}
        for workload, peak in daily_peak.items():
            if workload not in WORKLOADS:
                continue
            if type(peak) is int and peak > 0:
                self.daily_peak[workload] = peak
            elif isinstance(peak, dict):
                requests = peak.get("requests")
                tokens = peak.get("tokens_per_request")
                if type(requests) is int and requests > 0 and type(tokens) is int and tokens > 0:
                    self.daily_peak[workload] = requests
                    self.request_tokens[workload] = tokens
        configured = (
            production_workloads
            if production_workloads is not None
            else tuple(os.getenv("ROUTER_PRODUCTION_WORKLOADS", "").split(","))
        )
        self.production_workloads = {
            name.strip() for name in configured if name.strip() in WORKLOADS
        }
        self.models: list[ModelSpec] = []
        self.rejected: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self.models, self.rejected = load_models(self.catalog_path)

    def _provider_cap(self, provider: str) -> tuple[int, int, int, int, int | None, int | None]:
        rows = [m.quota for m in self.models if m.provider == provider]
        base = tuple(min(getattr(q, k) for q in rows) for k in ("rpm", "rpd", "tpm", "tpd"))
        hourly = tuple(
            min(values)
            if (values := [getattr(q, key) for q in rows if getattr(q, key, None)])
            else None
            for key in ("rph", "tph")
        )
        return (*base, *hourly)

    def _workload_share(self, workload: str) -> float:
        peaks = [self.daily_peak.get(w, 0) for w in WORKLOADS]
        if not all(peaks):
            return 0.5 if workload == "cactus-brief" else 0.25
        total = sum(peaks)
        cactus_share = max(0.25, self.daily_peak["cactus-brief"] / total)
        if workload == "cactus-brief":
            return cactus_share
        other_total = self.daily_peak["journal"] + self.daily_peak["simon-news"]
        return (1 - cactus_share) * self.daily_peak[workload] / other_total

    def _next_window(self, workload: str) -> int:
        waits = []
        try:
            for spec in self.models:
                if workload not in spec.workloads:
                    continue
                if (
                    workload in self.request_tokens
                    and self.request_tokens[workload] > spec.context_tokens
                ):
                    continue
                cooldown = self.quota.cooldown_remaining(spec)
                if cooldown:
                    waits.append(cooldown)
                limits = self._provider_cap(spec.provider)
                if (
                    self.quota.remaining_hourly_requests(
                        spec,
                        provider_rph=limits[4],
                        provider_tph=limits[5],
                        request_tokens=self.request_tokens.get(workload),
                    )
                    == 0
                ):
                    waits.append(self.quota.seconds_until_hourly_reset())
                if (
                    self.quota.remaining_daily_requests(
                        spec,
                        provider_rpd=limits[1],
                        provider_tpd=limits[3],
                        provider_rph=limits[4],
                        provider_tph=limits[5],
                        workload=workload,
                        workload_share=self._workload_share(workload),
                        request_tokens=self.request_tokens.get(workload),
                    )
                    == 0
                ):
                    waits.append(self.quota.seconds_until_daily_reset(spec))
        except RuntimeError:
            return 60
        return max(5, min(waits, default=300))

    def readiness(self, workload: str) -> dict:
        planned_tokens = self.request_tokens.get(workload)
        try:
            eligible = [
                m
                for m in self.models
                if workload in m.workloads
                and (planned_tokens is None or planned_tokens <= m.context_tokens)
                and self.quota.cooldown_remaining(m) == 0
                and (m.provider != "simplellm" or self.quota.provider_slot_available(m.provider))
                and self.quota.remaining_hourly_requests(
                    m,
                    provider_rph=self._provider_cap(m.provider)[4],
                    provider_tph=self._provider_cap(m.provider)[5],
                    request_tokens=planned_tokens,
                )
                > 0
                and self.quota.remaining_daily_requests(
                    m,
                    provider_rpd=self._provider_cap(m.provider)[1],
                    provider_tpd=self._provider_cap(m.provider)[3],
                    provider_rph=self._provider_cap(m.provider)[4],
                    provider_tph=self._provider_cap(m.provider)[5],
                    workload=workload,
                    workload_share=self._workload_share(workload),
                    request_tokens=planned_tokens,
                )
                > 0
            ]
        except RuntimeError:
            return {
                "ready": False,
                "providers": 0,
                "active": 0,
                "standby": 0,
                "verified_daily_requests": 0,
                "measured_peak_requests": self.daily_peak.get(workload),
                "planned_tokens_per_request": planned_tokens,
                "reasons": ["quota_store_unavailable"],
            }
        providers = {
            m.provider
            for m in eligible
            if any(other.provider == m.provider and "author" in other.stages for other in eligible)
            and any(
                other.provider == m.provider and "reviewer" in other.stages for other in eligible
            )
        }
        active = {m.provider for m in eligible if m.provider in providers and not m.standby}
        standby = {m.provider for m in eligible if m.provider in providers and m.standby}
        peak = self.daily_peak.get(workload)
        try:
            remaining = {
                p: self.quota.remaining_daily_requests(
                    next(m for m in eligible if m.provider == p),
                    provider_rpd=self._provider_cap(p)[1],
                    provider_tpd=self._provider_cap(p)[3],
                    provider_rph=self._provider_cap(p)[4],
                    provider_tph=self._provider_cap(p)[5],
                    workload=workload,
                    workload_share=self._workload_share(workload),
                    request_tokens=planned_tokens,
                )
                for p in providers
            }
            shared_tokens = (
                max(self.request_tokens.values())
                if len(self.request_tokens) == len(WORKLOADS)
                else None
            )
            full_remaining = {}
            for p in providers:
                spec = next(m for m in eligible if m.provider == p)
                full_remaining[p] = (
                    self.quota.remaining_daily_requests(
                        spec,
                        provider_rpd=self._provider_cap(p)[1],
                        provider_tpd=self._provider_cap(p)[3],
                        provider_rph=self._provider_cap(p)[4],
                        provider_tph=self._provider_cap(p)[5],
                        request_tokens=shared_tokens,
                    )
                    if shared_tokens is None or shared_tokens <= spec.context_tokens
                    else 0
                )
        except RuntimeError:
            return {
                "ready": False,
                "providers": 0,
                "active": 0,
                "standby": 0,
                "verified_daily_requests": 0,
                "measured_peak_requests": self.daily_peak.get(workload),
                "reasons": ["quota_store_unavailable"],
            }
        capacity = sum(remaining.values())
        reasons = []
        if len(providers) < 4 or len(active) < 3 or not (standby - active):
            reasons.append("less_than_four_independent_providers")
        if len(providers) < 2:
            reasons.append("insufficient_author_reviewer_diversity")
        if type(peak) is not int or peak <= 0:
            reasons.append("missing_measured_daily_peak")
        elif capacity < 2 * peak:
            reasons.append("insufficient_daily_headroom")
        all_peaks = [self.daily_peak.get(w, 0) for w in WORKLOADS]
        if not all(v > 0 for v in all_peaks):
            reasons.append("missing_shared_daily_peak")
        else:
            shared_capacity = sum(full_remaining.values())
            if shared_capacity < 2 * sum(all_peaks):
                reasons.append("insufficient_shared_daily_headroom")
        return {
            "ready": not reasons,
            "providers": len(providers),
            "active": len(active),
            "standby": len(standby),
            "verified_daily_requests": capacity,
            "measured_peak_requests": peak,
            "planned_tokens_per_request": planned_tokens,
            "reasons": reasons,
        }

    async def attempt(
        self,
        request: dict,
        *,
        workload: str,
        stage: str,
        author_provider: str | None = None,
        pilot: bool = False,
    ) -> Attempt:
        self.reload()  # Expired attestations stop routing without a restart.
        if not pilot:
            if workload not in self.production_workloads:
                return Attempt(None, 300, "production_not_adopted")
            try:
                activated = self.quota.production_activated(workload)
            except RuntimeError:
                return Attempt(None, 60, "quota_store_unavailable")
            if not activated:
                if not self.readiness(workload)["ready"]:
                    return Attempt(None, self._next_window(workload), "reserve_not_ready")
                try:
                    self.quota.activate_production(workload)
                except RuntimeError:
                    return Attempt(None, 60, "quota_store_unavailable")
        messages = request["messages"]
        max_tokens = request["max_tokens"]
        # UTF-8 byte count is a conservative token estimate for multilingual text.
        input_tokens = max(1, sum(len(m["content"].encode("utf-8")) + 64 for m in messages))
        candidates = [
            m
            for m in self.models
            if workload in m.workloads
            and stage in m.stages
            and (not author_provider or m.provider != author_provider)
            and input_tokens + max_tokens <= m.context_tokens
        ]
        if not candidates:
            return Attempt(None, 300, "no_eligible_model_or_context")
        ranked = []
        waits: list[int] = []
        try:
            for m in candidates:
                cooldown = self.quota.cooldown_remaining(m)
                if cooldown:
                    waits.append(cooldown)
                    continue
                if m.provider == "simplellm" and not self.quota.provider_slot_available(m.provider):
                    waits.append(5)
                    continue
                limits = self._provider_cap(m.provider)
                if (
                    self.quota.remaining_hourly_requests(
                        m,
                        provider_rph=limits[4],
                        provider_tph=limits[5],
                        request_tokens=input_tokens + max_tokens,
                    )
                    == 0
                ):
                    waits.append(self.quota.seconds_until_hourly_reset())
                    continue
                remaining = self.quota.remaining_daily_requests(
                    m,
                    provider_rpd=limits[1],
                    provider_tpd=limits[3],
                    provider_rph=limits[4],
                    provider_tph=limits[5],
                    request_tokens=input_tokens + max_tokens,
                )
                ranked.append((m.standby, -remaining / max(1, m.quota.rpd), m.provider, m))
        except RuntimeError:
            return Attempt(None, 60, "quota_store_unavailable")
        for _, _, _, spec in sorted(ranked, key=lambda x: x[:3]):
            try:
                reservation = self.quota.reserve(
                    spec,
                    workload,
                    input_tokens,
                    max_tokens,
                    provider_limits=self._provider_cap(spec.provider),
                    workload_share=self._workload_share(workload),
                )
            except RuntimeError:
                return Attempt(None, 60, "quota_store_unavailable")
            if not reservation.keys:
                waits.append(reservation.retry_after)
                continue
            try:
                result = await self.provider.complete(
                    spec, messages, max_tokens, request["temperature"]
                )
            except ProviderFailure as exc:
                if exc.reason == "provider_concurrency_busy":
                    try:
                        self.quota.refund_unsent(reservation)
                    except RuntimeError:
                        return Attempt(None, 60, "quota_store_unavailable")
                    waits.append(5)
                    continue
                try:
                    self.quota.cool_down(
                        spec,
                        exc.retry_after,
                        permanent=exc.permanent,
                        account_wide=exc.reason == "upstream_rate_limited",
                    )
                except RuntimeError:
                    return Attempt(None, 60, "quota_store_unavailable")
                waits.append(exc.retry_after)
                continue
            if result["prompt_tokens"] is not None and result["completion_tokens"] is not None:
                self.quota.reconcile(
                    reservation,
                    input_tokens,
                    max_tokens,
                    result["prompt_tokens"],
                    result["completion_tokens"],
                )
            prompt = (
                result["prompt_tokens"] if result["prompt_tokens"] is not None else input_tokens
            )
            completion = (
                result["completion_tokens"]
                if result["completion_tokens"] is not None
                else max_tokens
            )
            response = {
                "id": "chatcmpl-" + uuid4().hex,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": spec.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result["content"]},
                        "finish_reason": result["finish_reason"],
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
                "router": {
                    "provider": spec.provider,
                    "model": spec.model,
                    "upstream_model": result["actual_model"],
                    "usage_estimated": result["prompt_tokens"] is None,
                },
            }
            return Attempt(response, 0, "ok")
        return Attempt(None, max(5, min(waits, default=60)), "all_free_routes_unavailable")

    def status(self) -> dict:
        self.reload()
        try:
            activated = {w: self.quota.production_activated(w) for w in WORKLOADS}
        except RuntimeError:
            activated = {w: False for w in WORKLOADS}
        return {
            "workloads": {w: self.readiness(w) for w in ("journal", "simon-news", "cactus-brief")},
            "production": {
                w: {"adopted": w in self.production_workloads, "activated": activated[w]}
                for w in WORKLOADS
            },
            "verified_models": len(self.models),
            "rejected": self.rejected,
            "checked_at": datetime.now(UTC).isoformat(),
        }
