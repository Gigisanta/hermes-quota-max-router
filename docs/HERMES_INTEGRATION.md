# Hermes Integration

QuotaMax Router is installed as a **model-provider plugin** for Hermes Agent,
plus a single **sub-agent registration** in `~/.hermes/config.yaml`. With both
in place, any Hermes sub-agent can be told to use free-tier models through
this router.

## 1. Plugin (auto-discovered)

**Path:** `~/.hermes/plugins/model-providers/quotamax-router/`

Files:
- `plugin.yaml` — manifest (`kind: model-provider`)
- `__init__.py` — `QuotaMaxRouterProfile(ProviderProfile)` with live
  `fetch_models()` that hits `GET {QUOTAMAX_BASE_URL}/v1/models`

The plugin is auto-discovered by `providers._discover_providers()` on
Hermes startup. Enable it once:

```bash
hermes plugins enable quotamax-router
hermes plugins list --plain | grep quotamax
# → enabled   user   0.1.0   quotamax-router
```

Discovery is verified at runtime:

```python
import providers
providers._discovered = False
providers._REGISTRY.clear()
from providers import get_provider_profile
profile = get_provider_profile("quotamax-router")
print(profile.name, profile.aliases)
# quotamax-router ('quotamax', 'qmr', 'free-tier', 'free_models')
```

## 2. Sub-agent registration (config-side)

Add the following entries to `~/.hermes/config.yaml`.

### a. Under `auxiliary:` (parallel to `vision`, `web_extract`)

```yaml
auxiliary:
  quotamax_subagent:
    provider: quotamax-router
    model: auto
    base_url: ${QUOTAMAX_BASE_URL:-http://127.0.0.1:8088/v1}
    api_key: ${QUOTAMAX_API_KEY:-}
    api_mode: chat_completions
    timeout: 60
    extra_body: {}
```

### b. Under `delegation.subagent_models:` (adds a `quotamax` role)

```yaml
delegation:
  subagent_models:
    # ... existing roles ...
    quotamax: quotamax-router/auto
```

This exposes QuotaMax Router as a **selectable sub-agent provider** so callers
can pass `provider='quotamax-router/auto'` to a sub-agent (or set the
`quotamax` role explicitly in `subagent_models`).

## 3. Environment variables

Set in `~/.hermes/.env`:

```bash
# Point at the local router. Default: http://127.0.0.1:8088/v1
QUOTAMAX_BASE_URL=http://127.0.0.1:8088/v1

# Match the router's ROUTER_MASTER_KEY. Leave empty to disable auth.
QUOTAMAX_API_KEY=
```

The router itself needs at least one upstream key in its env
(`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, etc.) for live
mode. See [RUNBOOK.md](RUNBOOK.md) for upstream key onboarding.

## 4. Verifying the integration

```bash
# 1) Start the router in live mode
cd ~/workspaces/hermes-quota-max-router
source .venv/bin/activate
python scripts/run_router_live.py &

# 2) Wait for it to come up
curl http://127.0.0.1:8088/v1/router/health
# {"status":"ok", "version":"0.1.0", "models_count": 546, "live_mode": true, ...}

# 3) Run the end-to-end test (uses the Hermes profile + chat_completions transport)
python scripts/e2e_hermes_provider.py
# 🎉 SUCCESS: Hermes provider profile -> QuotaMax Router -> free model works end-to-end
```

## 5. Using it from a sub-agent

```python
# Via delegate_task
delegate_task(
    goal="Summarize the diff in plain English",
    provider="quotamax-router/auto",   # routes to a free model
    # ... rest of args
)

# Or via CLI
hermes chat --provider quotamax-router --model auto -z "Reply with PONG"
```

`auto` lets the router pick the best free model for the task (rule-based
scoring against the registry). Specific models are also routable:

```bash
hermes chat --provider quotamax-router --model gemini/gemini-2.5-flash-lite
hermes chat --provider quotamax-router --model deepseek/deepseek-r1-0528
hermes chat --provider quotamax-router --model openrouter/qwen/qwen3-235b-a22b-thinking-2507
```
