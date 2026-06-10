#!/usr/bin/env bash
#===============================================================================
# start_thoughtmachine.sh
#
#  ⚠ SYNCED with start_thoughtmachine.bat — keep in agreement.
#  ⚠ If you edit this file, mirror the same change in the batch file.
#===============================================================================
# Single-command launcher for ThoughtMachine Web UI.
# Pre-requisite: run install_thoughtmachine.sh first.
#
# Usage:
#   ./start_thoughtmachine.sh           # Development mode (hot-reload)
#   ./start_thoughtmachine.sh --prod    # Production mode (serves from dist/)
#===============================================================================

# Ensure clean startup by killing any leftover processes
"$(dirname "$0")/kill_thoughtmachine.sh"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# ── Parse flags ────────────────────────────────────────────────────────────
PROD_MODE=false
for arg in "$@"; do
    case "$arg" in
        --prod|-p)
            PROD_MODE=true
            ;;
        --help|-h)
            echo "Usage: $0 [--prod]"
            echo ""
            echo "  --prod    Production mode (serves pre-built dist/ files)"
            echo "            Default is development mode with hot-reload."
            echo "  --help    Show this help"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--prod]"
            exit 1
            ;;
    esac
done

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

# ── Start ─────────────────────────────────────────────────────────────────────
echo "============================================"
echo "  ThoughtMachine — Starting Web UI"
echo "============================================"
echo ""

if $PROD_MODE; then
    # ── Production mode (auto-build fresh, then serve) ────────────────
    echo "  Mode:    PRODUCTION (fresh build from source)"
    echo "  Server:  http://127.0.0.1:8000"
    echo "  Stop:    Ctrl+C"
    echo ""
    export TM_NPM_CMD="$(command -v npm)"
    python -m web_ui.backend.server --serve-frontend
else
    # ── Development mode (hot-reload via Vite) ─────────────────────────
    echo "  Mode:    DEVELOPMENT (hot-reload enabled)"
    echo ""
    echo "  Frontend: http://127.0.0.1:5173  <-- USE THIS URL"
    echo "  Backend:  http://127.0.0.1:8000   (API only, not the app)"
    echo "  Stop:    Ctrl+C"
    echo ""

    FRONTEND_DIR="$PROJECT_DIR/web_ui/frontend"

    # Verify npm is available
    if ! command -v npm &>/dev/null; then
        echo "[ERROR] npm not found. Install Node.js from https://nodejs.org/"
        exit 1
    fi
    export TM_NPM_CMD="$(command -v npm)"

    # Start backend FIRST so Vite's proxy never hits ECONNREFUSED
    echo "  → Starting backend server (port 8000)..."
    python -m web_ui.backend.server &
    BACKEND_PID=$!

    # Wait for backend to start listening on port 8000 (up to 15 seconds)
    echo "  → Waiting for backend to be ready..."
    BACKEND_READY=false
    for i in $(seq 1 15); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q :8000 || \
           lsof -i :8000 2>/dev/null | grep -q LISTEN; then
            BACKEND_READY=true
            break
        fi
    done
    if [ "$BACKEND_READY" = false ]; then
        echo ""
        echo "  [WARNING] Backend server may not have started."
        echo ""
    else
        echo "  → Backend is ready on http://127.0.0.1:8000"
    fi

    # Kill backend when script exits (Ctrl+C on Vite)
    cleanup() {
        echo ""
        echo "  → Shutting down backend server..."
        kill $BACKEND_PID 2>/dev/null || true
        wait $BACKEND_PID 2>/dev/null || true
    }
    trap cleanup EXIT

    # Start Vite dev server in foreground (blocking)
    echo "  → Starting Vite dev server (port 5173)..."
    cd "$FRONTEND_DIR"
    npm run dev
    cd "$PROJECT_DIR"
fi
