#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# start_thoughtmachine.sh
# Starts the ThoughtMachine Web UI server.
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"

# ── Activate venv ─────────────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    echo "✗ Venv not found at $VENV_DIR"
    echo "  Run ./install_thoughtmachine.sh first."
    exit 1
fi

source "$VENV_DIR/bin/activate"

# ── Defaults ──────────────────────────────────────────────────────────────────
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "============================================"
echo "  ThoughtMachine Web UI"
echo "  → http://$HOST:$PORT"
echo "============================================"

exec python -m web_ui.backend.server \
    --serve-frontend \
    --host "$HOST" \
    --port "$PORT"
