# Examples

Working examples against a running router. Start one first:

```bash
python scripts/run_router_live.py     # router on :8088 + dashboard on :7860
# or: python -m server.app            # router on :8080 (stub mode unless ROUTER_LIVE=1)
```

All examples read two environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `ROUTER_BASE_URL` | `http://127.0.0.1:8080/v1` | Router base URL (use `:8088` with `run_router_live.py`) |
| `ROUTER_MASTER_KEY` | `sk-router-dev-change-me` | Bearer key (`ROUTER_MASTER_KEY` from your `.env`) |

| Example | What it shows |
|---|---|
| [`curl_quickstart.sh`](curl_quickstart.sh) | Health, model list, and one chat completion — pure curl |
| [`chat_httpx.py`](chat_httpx.py) | Chat completion with `httpx` (already a project dependency) |
| [`streaming_chat.py`](streaming_chat.py) | Server-sent-events streaming, delta by delta |
| [`openai_sdk_chat.py`](openai_sdk_chat.py) | Drop-in usage from the official `openai` SDK (`pip install openai`) |
| [`quota_status.py`](quota_status.py) | Quota, cost, and circuit-breaker status from the `/v1/router/*` endpoints |
