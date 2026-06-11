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

    # ── Pre-flight checks for Vite ──────────────────────────────────────
    if [ ! -d "$FRONTEND_DIR" ]; then
        echo "[ERROR] Frontend directory not found at:"
        echo "  $FRONTEND_DIR"
        echo ""
        echo "  Make sure you ran install_thoughtmachine.sh first."
        exit 1
    fi
    if [ ! -f "$FRONTEND_DIR/package.json" ]; then
        echo "[ERROR] package.json not found in frontend directory:"
        echo "  $FRONTEND_DIR"
        echo ""
        echo "  The installation appears incomplete. Run install_thoughtmachine.sh again."
        exit 1
    fi
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        echo "[WARNING] node_modules not found — installing dependencies..."
        echo ""
        (cd "$FRONTEND_DIR" && npm install)
        if [ $? -ne 0 ]; then
            echo ""
            echo "[FAIL] npm install failed. Try running manually:"
            echo "  cd $FRONTEND_DIR"
            echo "  npm install"
            exit 1
        fi
        echo "  [+] npm packages installed"
        echo ""
    fi

    # Verify Vite binary exists
    if [ ! -f "$FRONTEND_DIR/node_modules/.bin/vite" ]; then
        echo "[ERROR] Vite binary not found in node_modules."
        echo "  The npm install may have failed or was interrupted."
        echo "  Try: cd $FRONTEND_DIR && npm install"
        exit 1
    fi

    VITE_BIN="$FRONTEND_DIR/node_modules/.bin/vite"

    # Start Vite FIRST in background
    echo "  → Starting Vite dev server (port 5173)..."
    cd "$FRONTEND_DIR"
    $VITE_BIN --host 127.0.0.1 &
    VITE_PID=$!
    cd "$PROJECT_DIR"

    # Wait for Vite to start (up to 15 seconds)
    echo "  → Waiting for Vite to start..."
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
        echo "  [WARNING] Vite may not have started in time."
        echo ""
    else
        echo "  → Vite is ready on http://127.0.0.1:5173"
    fi

    # Start backend in foreground (blocking)
    echo "  → Starting backend server (port 8000)..."
    python -m web_ui.backend.server

    # When backend exits, kill Vite
    echo ""
    echo "  → Shutting down Vite dev server..."
    kill $VITE_PID 2>/dev/null || true
fi
