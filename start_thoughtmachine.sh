#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# start_thoughtmachine.sh
# Single-command launcher for ThoughtMachine Web UI.
# Pre-requisite: run install_thoughtmachine.sh first.
#
# Usage
# ─────
#   ./start_thoughtmachine.sh           # Development mode (hot-reload)
#   ./start_thoughtmachine.sh --prod    # Production mode (serves from dist/)
# ──────────────────────────────────────────────────────────────────────────────

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

    # Start Vite dev server in background
    echo "  → Starting Vite dev server (port 5173)..."
    cd "$FRONTEND_DIR"
    npm run dev &
    VITE_PID=$!
    cd "$PROJECT_DIR"

    # Wait for Vite to start listening on port 5173 (up to 15 seconds)
    echo "  → Waiting for Vite to be ready..."
    VITE_READY=false
    for i in $(seq 1 15); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q :5173 || \
           lsof -i :5173 2>/dev/null | grep -q LISTEN; then
            VITE_READY=true
            break
        fi
    done
    if [ "$VITE_READY" = false ]; then
        echo ""
        echo "  [WARNING] Vite dev server may not have started."
        echo "            Check the terminal output above for errors."
        echo "            Try browsing to http://127.0.0.1:5173 manually."
        echo ""
    else
        echo "  → Vite is ready on http://127.0.0.1:5173"
    fi

    # Kill Vite when script exits
    cleanup() {
        echo ""
        echo "  → Shutting down Vite dev server..."
        kill $VITE_PID 2>/dev/null || true
        wait $VITE_PID 2>/dev/null || true
    }
    trap cleanup EXIT

    # Start backend (CORS already allows Vite dev server on any port)
    python -m web_ui.backend.server
fi
