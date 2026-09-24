"""Atomic account/model reservations; Redis outage means no inference."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import redis

from free_router.config import ModelSpec

_RESERVE = """
for i=1,#KEYS do
  local p=(i-1)*3
  local current=tonumber(redis.call('GET',KEYS[i]) or '0')
  if current+tonumber(ARGV[p+1])>tonumber(ARGV[p+2]) then return i end
end
for i=1,#KEYS do
  local p=(i-1)*3
  redis.call('INCRBY',KEYS[i],ARGV[p+1])
  if redis.call('TTL',KEYS[i])<0 then redis.call('EXPIRE',KEYS[i],ARGV[p+3]) end
end
return 0
"""
_ADJUST = """
for i=1,#KEYS do
  local current=tonumber(redis.call('GET',KEYS[i]) or '0')
  local delta=tonumber(ARGV[i])
  redis.call('SET',KEYS[i],math.max(0,current+delta),'KEEPTTL')
end
return 1
"""


@dataclass(frozen=True)
class Reservation:
    keys: tuple[str, ...]
    costs: tuple[int, ...]
    retry_after: int = 0


def _day_window(now: datetime, tz: str) -> tuple[str, int]:
    local = now.astimezone(ZoneInfo(tz))
    next_day = datetime.combine(
        local.date() + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(tz)
    )
    return local.date().isoformat(), max(1, math.ceil(next_day.timestamp() - now.timestamp()))


class QuotaStore:
    def __init__(self, client: redis.Redis):
        self.client: Any = client

    @classmethod
    def from_url(cls, url: str) -> QuotaStore:
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
        client.ping()
        return cls(client)

    def reserve(
        self,
        spec: ModelSpec,
        workload: str,
        input_tokens: int,
        output_tokens: int,
        *,
        provider_limits: tuple[int, int, int, int] | None = None,
        workload_share: float | None = None,
        now: datetime | None = None,
    ) -> Reservation:
        now = now or datetime.now(UTC)
        q = spec.quota
        minute = int(now.timestamp() // 60)
        day, day_ttl = _day_window(now, q.reset_tz)
        minute_ttl = max(1, 61 - int(now.timestamp()) % 60)
        total = input_tokens + output_tokens
        provider_limits = provider_limits or (q.rpm, q.rpd, q.tpm, q.tpd)
        if workload_share is None:
            workload_share = 0.5 if workload == "cactus-brief" else 0.25
        if not 0 < workload_share <= 1:
            raise ValueError("invalid_workload_share")

        # Keep ten percent below the account's verified ceilings for uncertainty.
        def cap(n: int) -> int:
            return max(1, int(n * 0.9))

        account = f"fr:v1:{spec.provider}"
        model = f"{account}:{spec.model}"
        windows: list[tuple[str, int, int, int]] = [
            (f"{account}:rpm:{minute}", 1, cap(provider_limits[0]), minute_ttl),
            (f"{account}:rpd:{day}", 1, cap(provider_limits[1]), day_ttl),
            (f"{account}:tpm:{minute}", total, cap(provider_limits[2]), minute_ttl),
            (f"{account}:tpd:{day}", total, cap(provider_limits[3]), day_ttl),
            (f"{model}:rpm:{minute}", 1, cap(q.rpm), minute_ttl),
            (f"{model}:rpd:{day}", 1, cap(q.rpd), day_ttl),
            (f"{model}:tpm:{minute}", total, cap(q.tpm), minute_ttl),
            (f"{model}:tpd:{day}", total, cap(q.tpd), day_ttl),
        ]
        # Every workload has its own ceiling. The deadline brief retains a
        # guaranteed share even when the other editorial queues are busy.
        windows += [
            (
                f"{account}:{workload}:rpd:{day}",
                1,
                max(1, int(cap(provider_limits[1]) * workload_share)),
                day_ttl,
            ),
            (
                f"{account}:{workload}:tpd:{day}",
                total,
                max(1, int(cap(provider_limits[3]) * workload_share)),
                day_ttl,
            ),
        ]
        if q.daily_neurons is not None:
            assert q.neurons_per_million_input is not None
            assert q.neurons_per_million_output is not None
            neurons = math.ceil(
                input_tokens * q.neurons_per_million_input / 1_000_000
                + output_tokens * q.neurons_per_million_output / 1_000_000
            )
            windows.append((f"{account}:neurons:{day}", neurons, cap(q.daily_neurons), day_ttl))
        keys = [x[0] for x in windows]
        args = [v for _, cost, limit, ttl in windows for v in (cost, limit, ttl)]
        try:
            blocked = int(self.client.eval(_RESERVE, len(keys), *keys, *args))
        except redis.RedisError as exc:
            raise RuntimeError("quota_store_unavailable") from exc
        if blocked:
            return Reservation((), (), windows[blocked - 1][3])
        return Reservation(tuple(keys), tuple(x[1] for x in windows))

    def reconcile(
        self,
        reservation: Reservation,
        estimated_input: int,
        estimated_output: int,
        actual_input: int,
        actual_output: int,
    ) -> None:
        if not reservation.keys:
            return
        estimated = estimated_input + estimated_output
        actual = max(0, actual_input) + max(0, actual_output)
        delta = actual - estimated
        adjustments = [0] * len(reservation.keys)
        for i, key in enumerate(reservation.keys):
            if ":tpm:" in key or ":tpd:" in key:
                adjustments[i] = delta
        if any(adjustments):
            try:
                self.client.eval(_ADJUST, len(reservation.keys), *reservation.keys, *adjustments)
            except redis.RedisError:
                # Keep the larger reservation if Redis is unavailable.
                pass

    def remaining_daily_requests(
        self,
        spec: ModelSpec,
        *,
        provider_rpd: int | None = None,
        provider_tpd: int | None = None,
        workload: str | None = None,
        workload_share: float | None = None,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        day, _ = _day_window(now, spec.quota.reset_tz)
        try:
            used = int(self.client.get(f"fr:v1:{spec.provider}:rpd:{day}") or 0)
            model_used = int(self.client.get(f"fr:v1:{spec.provider}:{spec.model}:rpd:{day}") or 0)
            tokens_used = int(self.client.get(f"fr:v1:{spec.provider}:tpd:{day}") or 0)
            model_tokens_used = int(
                self.client.get(f"fr:v1:{spec.provider}:{spec.model}:tpd:{day}") or 0
            )
            neuron_used = (
                int(self.client.get(f"fr:v1:{spec.provider}:neurons:{day}") or 0)
                if spec.quota.daily_neurons is not None
                else 0
            )
            workload_used = (
                int(self.client.get(f"fr:v1:{spec.provider}:{workload}:rpd:{day}") or 0)
                if workload
                else 0
            )
            workload_tokens_used = (
                int(self.client.get(f"fr:v1:{spec.provider}:{workload}:tpd:{day}") or 0)
                if workload
                else 0
            )
        except redis.RedisError as exc:
            raise RuntimeError("quota_store_unavailable") from exc
        ceiling = min(spec.quota.rpd, provider_rpd or spec.quota.rpd)
        remaining = min(int(ceiling * 0.9) - used, int(spec.quota.rpd * 0.9) - model_used)
        remaining = min(
            remaining,
            max(0, int((provider_tpd or spec.quota.tpd) * 0.9) - tokens_used)
            // spec.context_tokens,
            max(0, int(spec.quota.tpd * 0.9) - model_tokens_used) // spec.context_tokens,
        )
        if spec.quota.daily_neurons is not None:
            # Worst-case cost for a request filling the verified context window.
            # This keeps the free Neuron budget in the readiness calculation.
            assert spec.quota.neurons_per_million_input is not None
            assert spec.quota.neurons_per_million_output is not None
            neuron_cost = max(
                1,
                math.ceil(
                    spec.context_tokens
                    * max(
                        spec.quota.neurons_per_million_input,
                        spec.quota.neurons_per_million_output,
                    )
                    / 1_000_000
                ),
            )
            remaining = min(
                remaining,
                max(0, int(spec.quota.daily_neurons * 0.9) - neuron_used) // neuron_cost,
            )
        if workload:
            if workload_share is None:
                workload_share = 0.5 if workload == "cactus-brief" else 0.25
            if not 0 < workload_share <= 1:
                raise ValueError("invalid_workload_share")
            remaining = min(
                remaining,
                int(int((provider_rpd or spec.quota.rpd) * 0.9) * workload_share) - workload_used,
                max(
                    0,
                    int(int((provider_tpd or spec.quota.tpd) * 0.9) * workload_share)
                    - workload_tokens_used,
                )
                // spec.context_tokens,
            )
        return max(0, remaining)

    def cooldown_remaining(self, spec: ModelSpec) -> int:
        try:
            return max(
                0,
                self.client.ttl(f"fr:v1:cooldown:{spec.id}"),
                self.client.ttl(f"fr:v1:cooldown:provider:{spec.provider}"),
            )
        except redis.RedisError as exc:
            raise RuntimeError("quota_store_unavailable") from exc

    def seconds_until_daily_reset(self, spec: ModelSpec, *, now: datetime | None = None) -> int:
        _, seconds = _day_window(now or datetime.now(UTC), spec.quota.reset_tz)
        return seconds

    def cool_down(
        self,
        spec: ModelSpec,
        seconds: int,
        *,
        permanent: bool = False,
        account_wide: bool = False,
    ) -> None:
        # Permanent upstream access failure requires a fresh verification record.
        ttl = 7 * 86400 if permanent else max(1, seconds)
        try:
            with self.client.pipeline(transaction=True) as transaction:
                transaction.setex(
                    f"fr:v1:cooldown:{spec.id}", ttl, "revoked" if permanent else "cooldown"
                )
                if account_wide:
                    transaction.setex(f"fr:v1:cooldown:provider:{spec.provider}", ttl, "cooldown")
                transaction.execute()
        except redis.RedisError as exc:
            raise RuntimeError("quota_store_unavailable") from exc
