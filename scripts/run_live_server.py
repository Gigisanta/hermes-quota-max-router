import os
import sys

sys.path.insert(0, "/Users/prueba/workspaces/hermes-quota-max-router")
# GEMINI_API_KEY must be provided via the environment or .env (gitignored).
# No key is hardcoded in this script.
os.environ.setdefault("GEMINI_API_KEY", "${GEMINI_API_KEY}")
# Force live mode explicitly
os.environ["ROUTER_LIVE"] = "1"
import uvicorn

uvicorn.run("server.app:app", host="127.0.0.1", port=8087, log_level="warning")
