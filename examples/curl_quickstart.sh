#!/usr/bin/env bash
# Health, models, and one chat completion — no SDK required.
set -euo pipefail

BASE_URL="${ROUTER_BASE_URL:-http://127.0.0.1:8080/v1}"
KEY="${ROUTER_MASTER_KEY:-sk-router-dev-change-me}"

echo "== health =="
curl -sf "$BASE_URL/router/health" | python3 -m json.tool

echo
echo "== first 5 models =="
curl -sf "$BASE_URL/models" | python3 -c \
  'import json,sys; [print(" -", m["id"]) for m in json.load(sys.stdin)["data"][:5]]'

echo
echo "== chat completion (model=auto lets the orchestrator pick) =="
curl -sf "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "auto",
        "messages": [{"role": "user", "content": "Say hello in five words."}]
      }' | python3 -m json.tool
