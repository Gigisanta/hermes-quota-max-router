# Operator Runbook — Hermes QuotaMax Router

> **Audience:** on-call engineer paged for a QuotaMax Router incident.
> **TL;DR for the page:** go to [TL;DR](#tldr) first, then jump to the
> flowchart for the symptom you see.

The router is an OpenAI-compatible HTTP proxy in front of free-tier LLM
providers. It runs as a FastAPI process (port **8088**), an optional
Gradio dashboard (port **7860**), and a Hermes cron job that
self-checks every 6 hours. Live-mode is gated on
`ROUTER_LIVE=1` *and* at least one upstream key in the env
(`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, or `OPENROUTER_API_KEY`).
Without live mode, the router returns deterministic stub responses
prefixed `[stub:...]`.

See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the design and
[docs/HERMES_INTEGRATION.md](HERMES_INTEGRATION.md) for the
plugin + sub-agent wiring.

---

## TL;DR

The 12 commands you will type 90% of the time. Copy-paste blocks are
safe to run from a fresh shell at the repo root.

```bash
# 0. Activate the venv (or skip if `python` already points at .venv)
cd ~/workspaces/hermes-quota-max-router && source .venv/bin/activate

# 1. Is the router alive?
curl -s http://127.0.0.1:8088/v1/router/health | jq .

# 2. Is it in live mode (real Gemini/DeepSeek/OpenRouter) or stub?
curl -s -X POST http://127.0.0.1:8088/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}],"max_tokens":4}' | jq -r '.choices[0].message.content'

# 3. Run the self-test (exit codes documented below)
python scripts/healthcheck.py --once
echo "exit=$?"   # 0=ok  1=unreachable  2=stub  3=chat error  4=degraded

# 4. Validate config + Redis connectivity + registry
python -m scripts.validate_config

# 5. Force-quota reset
python -m scripts.operations reset-quotas

# 6. Per-model usage summary from the JSONL log
python -m scripts.operations usage-report

# 7. Refresh the registry from a feed file (or `live` for auto-discovery)
python -m scripts.operations auto-update live

# 8. Start the router (live mode; reads GEMINI_API_KEY from .env)
python scripts/run_router_live.py &
# Dashboard (separate terminal):
DASHBOARD_PORT=7860 python dashboard/app.py &

# 9. Install / verify the Hermes plugin + sub-agent wiring
python scripts/install_hermes_plugin.py

# 10. Inspect the last 10 alerts
tail -n 10 logs/alerts.jsonl | jq .

# 11. Find the router PID bound to port 8088 (for kill -9 scenarios)
lsof -nP -iTCP:8088 -sTCP:LISTEN

# 12. List Hermes cron jobs (find `quotamax_healthcheck`)
hermes cron list --all | jq '.jobs[] | select(.name | test("quotamax"))'
```

---

## How the system starts

Three long-lived processes plus one scheduled job. Treat the router
process as the source of truth — the dashboard and cron only talk to
it over HTTP, never to providers directly.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Live runtime topology (production)                                   │
│                                                                       │
│  ┌────────────────────────┐       HTTP /v1/*                          │
│  │  router (uvicorn)      │◀──────────────────────────────────┐      │
│  │  server/app.py         │                                   │      │
│  │  port 8088             │                                   │      │
│  │  $ROUTER_LIVE=1        │                                   │      │
│  └─┬───────────┬──────────┘                                   │      │
│    │           │                                              │      │
│    │ logs      │  reads                                       │      │
│    ▼           ▼                                              │      │
│  logs/router.jsonl    Gemini/DeepSeek/OpenRouter (LiteLLM)    │      │
│                                                                       │
│  ┌────────────────────────┐       HTTP /v1/* (GET)                  │
│  │  dashboard (gradio)    │◀──────────────────────────────────┘      │
│  │  dashboard/app.py      │  (Read-only Chat / Registry / Updater)    │
│  │  port 7860             │                                            │
│  └────────────────────────┘                                            │
│                                                                       │
│  ┌────────────────────────┐                                            │
│  │  Hermes cron           │  every 360 min (6h)                       │
│  │  quotamax_healthcheck  │  python scripts/healthcheck.py --daemon   │
│  │  ID 18a9f875185b       │  → /v1/router/health, /v1/chat/completions│
│  │                        │  → appends to logs/alerts.jsonl on fail   │
│  └────────────────────────┘                                            │
└──────────────────────────────────────────────────────────────────────┘
```

### What each process writes to disk

| Process | Files written | Notes |
|---|---|---|
| **router** (`server/app.py`) | `logs/router.jsonl` (append), `data/registry.sqlite` (curated), `data/registry_discovered.sqlite` (discovered), `registry/discovered.json`, `registry/merged.json` (on auto-update) | One JSONL line per `RouterCallResult`. Latency, model, tokens, fallback flag, error string. |
| **dashboard** (`dashboard/app.py`) | none persistent (reads only) | Talks to router over HTTP. The Updater tab triggers `RegistryUpdater.apply_feed()` which writes `registry/models.json` and `registry/merged.json`. |
| **cron / healthcheck** (`scripts/healthcheck.py`) | `logs/alerts.jsonl` (append on failure only) | One JSONL line per failed check. Successful runs are silent. |
| **Auto-Updater** (every 48h, `scripts/operations.py auto-update`) | `registry/discovered.json`, `registry/merged.json`, `data/registry_discovered.sqlite` | Never mutates the curated `registry/models.json` seed. |
| **Quota reset loop** (in-process task, default 1h) | `data/registry.sqlite` (via fakeredis or Redis) | Idempotent. Disable with `ROUTER_QUOTA_RESET_DISABLED=1`. |

> The QuotaManager uses Redis if `REDIS_URL` is reachable, otherwise
> an **in-process fakeredis** fallback. fakeredis state is lost on
> process restart, so daily quota totals silently "reset" if the
> router crashes mid-day. To make it durable, run `docker compose up -d redis`.

---

## Troubleshooting flowcharts

Each section is `### "Symptom" → 1-3 numbered steps`. Pick the symptom
that matches the user's report; do not skip to the bottom.

### "curl returns `[stub:...]`"

Router is in **stub mode** — no live upstream calls will go out.
Either `ROUTER_LIVE` is unset/0, or no upstream key is in the env.

1. Check `live_mode` in the health response:
   `curl -s http://127.0.0.1:8088/v1/router/health | jq .live_mode`
   — if `false`, the router is built without live mode.
2. If `live_mode=true` but responses are still stubs, the runtime
   picked live mode at startup but no key is present for the
   chosen provider. Check `echo "${GEMINI_API_KEY:-MISSING}"` (and
   `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`) in the **router's
   process env** (`lsof -nP -iTCP:8088 -sTCP:LISTEN` → `ps -p <PID> -Eww`
   to see the actual env, since `lsof` doesn't show it).
3. Restart with `python scripts/run_router_live.py` after sourcing
   the env (or `set -a; source .env; set +a`) so the keys are in
   the new process's env.

### "curl fails with 401"

The server requires a Bearer token when `ROUTER_MASTER_KEY` is set.

1. Confirm the master key is in your shell:
   `echo "match=$([ "$ROUTER_MASTER_KEY" = "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ROUTER_MASTER_KEY" http://127.0.0.1:8088/v1/models)" = expected ] && echo yes || echo no)"`
   — easier: just retest with the header:
   `curl -H "Authorization: Bearer $ROUTER_MASTER_KEY" http://127.0.0.1:8088/v1/models`.
2. The `ROUTER_MASTER_KEY` is **read once at app build time** (it's
   captured into a local in `server/app.py:231`), not per-request.
   If you set it in the env *after* the router started, **restart the
   router process** to pick it up.
3. If you don't want auth (dev mode), unset `ROUTER_MASTER_KEY` in
   the router's env and restart. Auth is disabled when empty.

### "curl fails with 429"

Rate limit. The router has an in-memory token bucket
(`capacity=60`, `refill=1.0/s`) keyed by client IP.

1. Look at the metrics: `curl -s http://127.0.0.1:8088/v1/router/metrics`
   — find `router_rate_limited_total`. If it's growing, the
   legitimate client is bursting.
2. Spread traffic with a queue, or raise the bucket by editing
   `core/security.py:TokenBucket` defaults (capacity / refill_rate)
   and restarting the router.
3. The bucket is per-process; it resets on every router restart.

### "Hermes chat with `provider=quotamax-router` errors with 'plugin not found'"

The plugin symlink or discovery step is missing.

1. Run `python scripts/install_hermes_plugin.py` from the repo
   root. It (a) symlinks `scripts/hermes_plugin/quotamax-router/`
   into `~/.hermes/plugins/model-providers/`, and (b) patches
   `~/.hermes/config.yaml` with the sub-agent entries.
2. Verify the symlink exists:
   `ls -la ~/.hermes/plugins/model-providers/quotamax-router`
   (should resolve into the repo's `scripts/hermes_plugin/quotamax-router/`).
3. Verify Hermes sees it:
   `hermes plugins list --plain | grep quotamax` should show
   `enabled   user   0.1.0   quotamax-router`. If "disabled",
   `hermes plugins enable quotamax-router`.

### "Hermes config has `quotamax_subagent` but `--provider` 404s"

Config is patched, but the running Hermes process hasn't reloaded it.

1. **Restart Hermes** (the config is read on agent boot). The
   sub-agent registry is not hot-reloaded.
2. After restart, test: `hermes chat --provider quotamax-router --model auto -z "PONG"`.
3. If still 404, run `python scripts/install_hermes_plugin.py` again
   to confirm both the symlink and the YAML keys are present.

### "Cron says 'Last run: error'"

Hermes's `quotamax_healthcheck` job (id `18a9f875185b`, every 360 min).

1. `tail -n 20 logs/alerts.jsonl | jq .` — read the most recent
   failure record. The `check` field is one of `liveness`, `chat`,
   `stub`; the `message` field has the reason.
2. The most common cause is the **router process is down** —
   `lsof -nP -iTCP:8088 -sTCP:LISTEN`. If empty, restart with
   `python scripts/run_router_live.py &`.
3. Verify the script path the cron job points at:
   `hermes cron list --all | jq '.jobs[] | select(.id=="18a9f875185b")'`
   — `script` should resolve to `scripts/healthcheck.py` in this repo.

### "A free model is down (e.g. deepseek 503)"

The router auto-falls back to the next-best free model with remaining
quota. No human action required, but you should verify the fallback
fired.

1. Watch live traffic: `tail -f logs/router.jsonl | jq -c 'select(.error) | {ts:.timestamp,model:.model_used,fallback:.fallback_used,err:.error}'`
2. Confirm the **fallback** is also healthy: `curl -s -X POST http://127.0.0.1:8088/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"gemini/gemini-2.5-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' | jq .choices[0].message.content`
3. If the orchestrator can find no healthy free model with remaining
   quota, the response content will be `[blocked] Quota exhausted for X and no fallback.`
   — see [Recovery: drop fakeredis cache](#recovery-procedures) to
   clear stale zero-quota entries in dev.

### "Dashboard chat shows STUB MODE"

The dashboard calls the router over HTTP, then the router hits a stub
because the **router** has no live keys in its env (the dashboard
itself doesn't need keys).

1. Check the router's health:
   `curl -s http://127.0.0.1:8088/v1/router/health | jq '{live_mode, models_count}'`
2. If `live_mode: false`, set `ROUTER_LIVE=1` in the router's env and
   restart it. The dashboard will auto-detect on the next refresh.
3. Confirm the dashboard is pointed at the right URL:
   `echo $QUOTAMAX_BASE_URL` (default `http://127.0.0.1:8088/v1`).
   If running on a different host/port, set it before launching the
   dashboard: `QUOTAMAX_BASE_URL=http://host:8088/v1 python dashboard/app.py`.

### "Tests are slow"

The bench test dominates. It's a 100-routing-decision SLO bound
(`<5s`); it runs in ~1s on a warm laptop but adds up in CI.

```bash
python -m pytest tests/ \
  --deselect tests/test_integration_e2e.py::test_bench_100_routing_decisions_under_5s
```

The deselect target is stable — that test is `test_bench_100_routing_decisions_under_5s`
in `tests/test_integration_e2e.py`. Don't delete the test; deselect it.

### "Tests fail with n==1 in quota scheduler"

Already fixed in commit `ddaf655` ("freeze 'now' in quota_scheduler
tests to avoid UTC-midnight flakiness"). If you still see this on a
checkout, the fixture in `tests/test_quota_scheduler.py:qm` pins
`frozen_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)`.
Make sure the file is not behind an old rebase:

```bash
grep -n "frozen_now" tests/test_quota_scheduler.py
# → should show the midday-UTC literal
```

If the literal is missing, your branch is missing commit `ddaf655`;
rebase or cherry-pick it. Do not "fix" the test by weakening the
assertion — the freeze is the correct fix and keeps CI green at
midnight UTC.

---

## Recovery procedures

### Kill a stuck router

```bash
# 1. Find the listener
lsof -nP -iTCP:8088 -sTCP:LISTEN
#    COMMAND  PID   USER   FD   TYPE ...
#    Python   2649  prueba ...

# 2. Graceful (SIGTERM — uvicorn drains in-flight requests)
kill 2649
sleep 3
lsof -nP -iTCP:8088 -sTCP:LISTEN  # confirm gone

# 3. Hard kill only if step 2 didn't work
kill -9 2649

# 4. Restart
cd ~/workspaces/hermes-quota-max-router && source .venv/bin/activate
python scripts/run_router_live.py &> logs/router.stdout.log &
```

> If the listener is actually held by `lsof` showing a different
> command (e.g. `com.docker.backend` for a Docker mapping), the
> router is running inside a container. Use `docker ps | grep router`
> and `docker restart <name>`.

### Drop the fakeredis cache

When running **without** Redis, the QuotaManager stores counters
in-process in fakeredis. State is lost on restart, but if you want
to wipe it without a restart (e.g. for dev), the QuotaManager has
no `FLUSHDB`; just reset via the operations script:

```bash
# Hard reset every model's `remaining` to its `total`
python -m scripts.operations reset-quotas
```

This works for both backends (it issues `HMSET` per `quota:<id>`).
If you also need to drop the **registry SQLite** (curated models
table) — e.g. tests contaminated the seed — do:

```bash
# Re-seed from the curated JSON. Wipes any in-DB model mutations.
rm -f data/registry.sqlite data/registry_discovered.sqlite
# The next router start will recreate the SQLite files from
# registry/models.json + registry/discovered.json.
```

### Rebuild the registry

If `registry/merged.json` looks stale or you've manually edited
`registry/models.json`:

```bash
# Option A — re-merge from the curated seed + discovered JSON
python -m scripts.operations auto-update
# (defaults to registry/feed_sample.json when no argument)

# Option B — pull fresh discovered models from OpenRouter/HuggingFace
python -m scripts.operations auto-update live
# (uses RemoteFeedProvider; writes registry/discovered.json + merged.json)

# Option C — restore the curated seed from git
git checkout -- registry/models.json
rm -f data/registry.sqlite
```

After any of these, **restart the router** so the in-memory
`LayeredRegistry` re-loads from disk.

### Drain Redis cleanly

If the router is talking to a real Redis and quota state looks
corrupted (negative remaining, mismatched totals), drop just the
quota hash namespace — the registry stays intact:

```bash
redis-cli -u "$REDIS_URL" --scan --pattern 'quota:*' | xargs -r redis-cli -u "$REDIS_URL" DEL
# On the next request, the QuotaManager will re-seed from the
# registry's `daily_quota_tokens`.
```

---

## Observability

### JSONL log schema — `logs/router.jsonl`

One line per `RouterCallResult` (see `core/router_engine.py:RouterCallResult.to_dict`).
Example:

```json
{
  "timestamp": "2026-06-10T05:45:41.391491+00:00",
  "decision_strategy": "direct",
  "model_used": "deepseek/deepseek-r1-0528",
  "input_tokens": 7,
  "output_tokens": 20,
  "total_tokens": 27,
  "duration_s": 0.0027,
  "fallback_used": false,
  "preserve_paid_quota": true,
  "confidence": 0.55,
  "error": null,
  "task_type": "code",
  "tags": ["coding_sota", "refactoring_god"]
}
```

Fields:

| Field | Type | Meaning |
|---|---|---|
| `timestamp` | ISO-8601 UTC | When `RouterCallResult` was constructed (post-call). |
| `decision_strategy` | string | One of `direct`, `free_first`, `paid_only`, `moa`. |
| `model_used` | string | The actual model that answered. `""` if no model was selected. |
| `input_tokens` / `output_tokens` / `total_tokens` | int | From `litellm` `usage`. 0 on error. |
| `duration_s` | float | Wall time from `completion()` entry to result, in seconds. |
| `fallback_used` | bool | True if the primary failed and the orchestrator's `fallback_model` answered. |
| `preserve_paid_quota` | bool | From the routing decision; `false` means we paid for this call. |
| `confidence` | float 0–1 | Orchestrator's match confidence. |
| `error` | string\|null | Truncated exception string on failure. |
| `task_type` | string | From the task analyzer; absent if analysis didn't run. |
| `tags` | string[] | Required tags the analyzer inferred; absent if analysis didn't run. |

### JSONL log schema — `logs/alerts.jsonl`

One line **per failed check** (successful healthchecks write nothing).
Example:

```json
{
  "ts": "2026-06-12T02:09:51.463801+00:00",
  "level": "error",
  "check": "liveness",
  "message": "GET /router/health failed: [Errno 61] Connection refused"
}
```

Fields: `ts` (ISO-8601 UTC), `level` (`error`|`warning`), `check`
(`liveness`|`chat`), `message`, plus any `**details` captured from
the failing response (e.g. the full body on empty-content chat).

### `jq` recipes

All examples assume you've `cd`'d to the repo root.

```bash
# (1) Calls per model in the last 1000 lines
tail -n 1000 logs/router.jsonl \
  | jq -r '.model_used' \
  | sort | uniq -c | sort -rn

# (2) Error rate over the last 500 calls (count / total)
tail -n 500 logs/router.jsonl \
  | jq -s '{total: length, errors: [.[] | select(.error) | .model_used] | length}'

# (3) p50 / p95 latency in milliseconds
tail -n 1000 logs/router.jsonl \
  | jq -s '[.[].duration_s] | sort as $s
           | "p50=\(($s[length/2|floor]*1000)|round)ms  p95=\(($s[length*95/100|floor]*1000)|round)ms"'

# (4) Fallback rate (what % of calls used the orchestrator's fallback)
tail -n 1000 logs/router.jsonl \
  | jq -s '{fb: [.[] | select(.fallback_used)] | length,
            total: length,
            pct: ((([.[] | select(.fallback_used)] | length) / length) * 100 | round)}'

# (5) Per-model error breakdown (top 10)
jq -r 'select(.error) | .model_used' logs/router.jsonl \
  | sort | uniq -c | sort -rn | head -10

# (6) Last 20 alerts (human-readable)
tail -n 20 logs/alerts.jsonl | jq -r '"\(.ts)  \(.level|ascii_upcase)  \(.check)  — \(.message)"'

# (7) Live Prometheus metrics from the router
curl -s http://127.0.0.1:8088/v1/router/metrics

# (8) Live quota snapshot (per-model remaining/total/% remaining)
curl -s http://127.0.0.1:8088/v1/router/quota \
  | jq '.data | map({model_id, remaining, total, pct_remaining}) | sort_by(-.pct_remaining)'
```

### Healthcheck exit codes

| Code | Meaning | Cron treatment |
|---|---|---|
| `0` | Liveness OK **and** chat returned a real, non-stub body | OK |
| `1` | Liveness failed (router unreachable, connection refused) | Error — investigate |
| `2` | Liveness OK but chat body was `[stub:...]` | Warning — set upstream keys |
| `3` | Chat call returned an error / empty body (real LLM failure) | Error — check provider status |
| `4` | Liveness reported `status != "ok"` (degraded) | Error — check `/v1/router/health` JSON |

---

## Upgrade — add a new free model

Three workflows, ordered from "safest" to "most invasive".

### 1. Auto-discovery (no code change)

Run the live auto-updater. It hits OpenRouter + HuggingFace public
catalogs, filters for free, and appends to
`registry/discovered.json`. The merged registry is rebuilt on the
next router start.

```bash
# 1. Pull and merge
python -m scripts.operations auto-update live

# 2. Inspect the diff
jq '.added' registry/merged.json  # newly-merged model ids

# 3. Restart the router so the in-memory registry reloads
```

### 2. Promote a discovered model to the curated seed

Use this when you want a model to survive an `auto-update live`
(discovery rewrites `discovered.json`, not the curated seed).

```bash
# 1. Open registry/models.json and append a new model object with
#    the same shape as the existing entries (model_id, provider,
#    strength_tags, tier_rank, daily_quota_tokens, ...).

# 2. Validate
python -m scripts.validate_config

# 3. Restart the router
```

### 3. Brand-new provider (needs an env var)

The orchestrator skips models whose provider's env key is unset
(`core/orchestrator.py:has_key_for_model`). To add a new provider:

1. Add the new env var to `core/orchestrator.py:_PROVIDER_KEY_ENV`
   mapping the provider name to its env-var tuple.
2. Add the model to `registry/models.json` with
   `provider: <new_name>`.
3. Set the new env var in `.env` (e.g. `HUGGINGFACE_API_KEY=***`)
   and `set -a; source .env; set +a` before launching the router.
4. `python -m scripts.validate_config` and restart.

---

## When to escalate

Seven conditions where you should page a second human or hand off
rather than DIY. Each is signed off with the role that should pick
it up.

| # | Sign | Hand off to |
|---|---|---|
| 1 | **All free models are 429/5xx for >30 min and the router is also failing paid-tier (orchestrator flips `preserve_paid_quota=false` repeatedly in `logs/router.jsonl`).** | Provider-relations on-call (DeepSeek / Google AI Studio / OpenRouter status pages). |
| 2 | **The healthcheck cron (`quotamax_healthcheck`, id `18a9f875185b`) has been red for >1h and a router restart doesn't recover it.** | Infrastructure on-call (the box itself, port 8088 firewall, disk full, or OOM). |
| 3 | **`logs/router.jsonl` is silent for >15 min during business hours** while upstream traffic is expected. | Router on-call — could be a hung uvicorn worker or the router is up but `RouterEngine._log` is failing on a full disk. |
| 4 | **The registry loses a curated model after an `auto-update`** (the curated seed is supposed to be immutable — investigate before re-running). | Release manager. Confirm the merge logic in `core/auto_updater.py` and consider pinning the `version` in `registry/models.json`. |
| 5 | **A user reports the router leaked a Bearer token or upstream API key** in a response body, log line, or dashboard panel. | Security on-call **immediately**. Rotate `ROUTER_MASTER_KEY` and any leaked upstream key. |
| 6 | **Cost tracker shows paid-tier spend in `/v1/router/cost` > $0** in a day. | FinOps on-call. This should be $0 unless `preserve_paid_quota=false` was deliberately set. Audit `logs/router.jsonl` for `preserve_paid_quota: false` entries. |
| 7 | **Dashboard is down but router health is OK** for >30 min and a `DASHBOARD_PORT=7860 python dashboard/app.py &` doesn't bring it back. | Dashboard maintainer. Likely a Gradio/httpx incompatibility after a `pip install` — pin the version, don't churn the dashboard in production. |

> If you're unsure whether to escalate, escalate. The cost of a
> second pair of eyes on a free-tier LLM proxy is much lower than
> the cost of a silent outage.
