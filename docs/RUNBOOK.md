# Operator Runbook

## TL;DR

```bash
# Validate config (run before anything else)
python -m scripts.validate_config

# Run the test suite
python -m pytest tests/

# Start the HTTP server (port 8080)
python -m server.app

# Or start the dashboard (port 7860)
python -m dashboard.app

# Reset quotas (cron: midnight UTC)
python -m scripts.operations reset-quotas

# Auto-update registry from feed (cron: every 48h)
python -m scripts.operations auto-update

# Generate usage report (cron: hourly)
python -m scripts.operations usage-report
```

## Common Tasks

### "A user reports a 401"
The server requires a Bearer token if `ROUTER_MASTER_KEY` is set.
```bash
curl -H "Authorization: Bearer $ROUTER_MASTER_KEY" http://localhost:8080/v1/chat/completions ...
```

### "A user reports 429"
Rate limit hit. Either:
- Increase the bucket in `server/app.py` (TokenBucket capacity/refill_rate)
- Tell the user to slow down
- Check `router_rate_limited_total` in metrics

### "I want to add a new free model"
1. Edit `registry/models.json` directly, OR
2. Drop a feed at `registry/feed_sample.json` and run:
   ```bash
   python -m scripts.operations auto-update registry/feed_sample.json
   ```
3. The model appears in `/v1/models` immediately (no restart needed)
4. Verify with `python -m scripts.validate_config`

### "DeepSeek is down / returning errors"
1. Check `/v1/router/quota` — if `remaining: 0` for deepseek, it's quota-exhausted (not network).
2. The router auto-falls back to the next-best model (qwen or gemini usually).
3. To check: `tail -f logs/router.jsonl | jq 'select(.model_used | contains("deepseek")) | .error'`

### "I want to see which model is being used for X"
```bash
python -m scripts.operations usage-report
```
Shows per-model calls, tokens, errors, fallbacks, paid-quota violations.

### "Tests are slow"
The bench test (`test_bench_100_routing_decisions_under_5s`) is the
slowest at ~1s. To skip it during dev:
```bash
python -m pytest tests/ --deselect tests/test_integration_e2e.py::test_bench_100_routing_decisions_under_5s
```

### "Seed is contaminated with `x/y` / `new/z` / `test/newcomer`"
Tests from earlier iterations may have leaked into the real seed. Reset:
```bash
# The seed ships at the spec-defined 7 models. If contaminated, restore:
git checkout registry/models.json
rm -f data/registry.sqlite
```

## Cron Setup (macOS launchd)

Create `~/Library/LaunchAgents/ai.hermes.quotamax.reset.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.hermes.quotamax.reset</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/prueba/workspaces/hermes-quota-max-router/.venv/bin/python</string>
    <string>-m</string>
    <string>scripts.operations</string>
    <string>reset-quotas</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/prueba/workspaces/hermes-quota-max-router</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>0</integer></dict>
</dict>
</plist>
```
Then: `launchctl load ~/Library/LaunchAgents/ai.hermes.quotamax.reset.plist`

## Monitoring Queries

```bash
# How many calls per model in the last hour?
tail -n 1000 logs/router.jsonl | python -c "
import json, sys, collections
c = collections.Counter()
for line in sys.stdin:
    try: c[json.loads(line).get('model_used','')] += 1
    except: pass
for m, n in c.most_common(): print(f'{m}: {n}')"

# What's the p50 latency?
python -c "
import json
samples = [json.loads(l)['duration_s'] for l in open('logs/router.jsonl')]
samples.sort()
print(f'p50: {samples[len(samples)//2]*1000:.1f}ms')"

# Any errors in the last 100 calls?
tail -n 100 logs/router.jsonl | python -c "
import json, sys
for line in sys.stdin:
    try:
        rec = json.loads(line)
        if rec.get('error'): print(rec['timestamp'], rec['model_used'], rec['error'])
    except: pass"
```

## When Things Break

| Symptom | First Check |
|---|---|
| All requests 500 | `python -m scripts.validate_config` |
| `RouterEngine` hangs | Check `live=True` requires API keys; if no keys, use `live=False` (stubs) |
| `consume()` returns False unexpectedly | `quota_manager.snapshot(model_id)` — quota may be exhausted |
| Auto-update not running | Check `scripts/crontab.example` lines + `cron` daemon |
| Dashboard won't load | `pip install gradio` then `python -m dashboard.app` |
| Quotas never reset | The cron at midnight needs Redis OR the fakeredis fallback is ephemeral (quota resets on restart) |
