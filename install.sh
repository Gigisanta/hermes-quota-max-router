#!/usr/bin/env bash
# install.sh — "un plugin, una url, una api" one-shot installer.
#
# Idempotent. Can be re-run safely. Produces a running QuotaMax Router
# on http://127.0.0.1:8080 with the OpenAI-compatible API ready to use.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Gigisanta/hermes-quota-max-router/main/install.sh | bash
#   QUOTAMAX_HOME=/opt/qr curl -fsSL ... | bash   # custom install dir
#   ROUTER_PORT=9000 curl -fsSL ... | bash        # custom port
#
# What it does:
#   1. Verifies Python 3.11+ is on PATH.
#   2. Clones (or updates) the repo into ~/.local/share/quotamax-router.
#   3. Creates a venv and installs requirements.
#   4. Writes a .env from .env.example if not present.
#   5. Validates config (non-fatal if stub mode).
#   6. Starts the server in the foreground.
#
# iter 15: ROUTER_ALLOW_INSECURE_NO_AUTH=*** is set by default so the
# installer works out of the box. Operators are STRONGLY encouraged to
# set ROUTER_MASTER_KEY before deploying — see SECURITY.md.

set -euo pipefail

REPO_URL="${QUOTAMAX_REPO:-https://github.com/Gigisanta/hermes-quota-max-router.git}"
INSTALL_DIR="${QUOTAMAX_HOME:-$HOME/.local/share/quotamax-router}"
PORT="${ROUTER_PORT:-8080}"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Prereqs ---

command -v python3 >/dev/null || fail "python3 not found. Install Python 3.11+."
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)'; then
    fail "Python 3.11+ required (found $(python3 --version))."
fi
command -v git >/dev/null || fail "git not found."

log "Python:  $(python3 --version)"
log "Install: $INSTALL_DIR"
log "Port:    $PORT"

# --- 2. Clone or update ---

mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
    log "Updating existing install…"
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || log "  (update failed; using existing checkout)"
else
    log "Cloning $REPO_URL…"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# --- 3. venv + requirements ---

if [ ! -d "$INSTALL_DIR/.venv" ]; then
    log "Creating venv…"
    python3 -m venv "$INSTALL_DIR/.venv"
fi
# shellcheck disable=SC1091
source "$INSTALL_DIR/.venv/bin/activate"
log "Installing requirements (~90s)…"
pip install --quiet --upgrade pip
pip install --quiet -r "$INSTALL_DIR/requirements.txt"

# --- 4. .env ---

if [ ! -f "$INSTALL_DIR/.env" ]; then
    log "Writing .env from .env.example (edit to add real API keys)…"
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

# --- 5. iter 15: opt-in to unauthenticated dev mode by default ---
# Operators MUST set ROUTER_MASTER_KEY before production use.
grep -qE '^ROUTER_ALLOW_INSECURE_NO_AUTH=' "$INSTALL_DIR/.env" 2>/dev/null || \
    echo 'ROUTER_ALLOW_INSECURE_NO_AUTH=1' >> "$INSTALL_DIR/.env"

# --- 6. validate ---

log "Validating config…"
(cd "$INSTALL_DIR" && python -m scripts.validate_config) || log "  (validation warnings OK for stub mode)"

# --- 7. start ---

log "Starting server on http://127.0.0.1:$PORT …"
log "  Health:  curl http://127.0.0.1:$PORT/v1/router/health"
log "  Test:    curl -X POST http://127.0.0.1:$PORT/v1/chat/completions -H 'Content-Type: application/json' -d '{\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}'"
log "  Stop:    Ctrl-C"

cd "$INSTALL_DIR"
exec python -m server.app
