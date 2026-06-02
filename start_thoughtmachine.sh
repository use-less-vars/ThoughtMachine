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
    # ── Production mode (serve from dist/) ─────────────────────────────
    echo "  Mode:    PRODUCTION (serving pre-built dist/ files)"
    echo "  Server:  http://127.0.0.1:8000"
    echo "  Stop:    Ctrl+C"
    echo "  Note:    Rebuild frontend after changes:"
    echo "           cd web_ui/frontend && npm run build"
    echo ""
    python -m web_ui.backend.server --serve-frontend
else
    # ── Development mode (hot-reload via Vite) ─────────────────────────
    echo "  Mode:    DEVELOPMENT (hot-reload enabled)"
    echo ""
    echo "  Frontend: http://127.0.0.1:5173"
    echo "  Backend:  http://127.0.0.1:8000"
    echo "  Stop:    Ctrl+C"
    echo ""

    FRONTEND_DIR="$PROJECT_DIR/web_ui/frontend"

    # Start Vite dev server in background
    echo "  → Starting Vite dev server (port 5173)..."
    cd "$FRONTEND_DIR"
    npm run dev &
    VITE_PID=$!
    cd "$PROJECT_DIR"

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
