#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# start_thoughtmachine.sh
# Single-command launcher for ThoughtMachine Web UI.
# Pre-requisite: run install_thoughtmachine.sh first.
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# ── Ensure virtual environment exists ─────────────────────────────────────────
VENV_DIR="$PROJECT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "✗ Virtual environment not found at $VENV_DIR"
    echo ""
    echo "  Run the install script first:"
    echo "    ./install_thoughtmachine.sh"
    exit 1
fi

source "$VENV_DIR/bin/activate"

# ── Start the Web UI backend ──────────────────────────────────────────────────
echo "============================================"
echo "  ThoughtMachine — Starting Web UI"
echo "============================================"
echo ""
echo "  Server:  http://127.0.0.1:8000"
echo "  Stop:    Ctrl+C"
echo ""

python -m web_ui.backend.server --serve-frontend
