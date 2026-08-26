#!/usr/bin/env bash
#===============================================================================
# start_thoughtmachine.sh
#
#  Preflight doctor + launcher for ThoughtMachine.
#  Runs all checks first; when they pass it starts the app:
#    * default (dev):  vite on 127.0.0.1:5173 in the background, then the
#                      backend (.venv/bin/python -m web_ui.backend.server)
#                      in the foreground. When the backend exits, vite is
#                      stopped too.
#    * --prod / -p:    production mode, single foreground process:
#                      .venv/bin/python -m web_ui.backend.server --serve-frontend
#    * --check-only:   run ONLY the preflight checks, then exit 0 without
#                      starting anything (also honors TM_CHECK_ONLY=1).
#===============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DOCTOR="$SCRIPT_DIR/scripts/doctor_checks.py"
PROD_MODE=false
CHECK_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --prod|-p)          PROD_MODE=true ;;
        --check-only)       CHECK_ONLY=true ;;
        --help|-h)
            echo "Usage: $0 [--prod] [--check-only]"
            echo "  --prod / -p   production mode: backend serves the built frontend"
            echo "  --check-only  run preflight checks only, then exit 0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--prod] [--check-only]"
            exit 1
            ;;
    esac
done
if [ "${TM_CHECK_ONLY:-}" = "1" ]; then
    CHECK_ONLY=true
fi

doctor() {
    python3 "$DOCTOR" "$@"
}

# Extract a field from a JSON document passed on stdin. Prints "" on any error.
json_get() {
    python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get(sys.argv[1], "") if isinstance(d, dict) else "")' "$1" 2>/dev/null || true
}

echo "============================================"
echo "  ThoughtMachine - preflight check"
echo "============================================"
echo ""

# --------------------------------------------------------- [1/7] venv (critical)
echo "[1/7] Python virtual environment ..."
VENV_OUT="$(doctor --ensure-venv 2>&1)"
VENV_RC=$?
if [ "$VENV_RC" -ne 0 ]; then
    echo "      FAILED: virtual environment (.venv) could not be set up."
    printf '%s\n' "$VENV_OUT" | sed 's/^/      /'
    echo "      Run ./install.sh first"
    exit 1
fi
echo "      ok."

# ------------------------------------------------------------ [2/7] Docker access
echo "[2/7] Docker ..."
DOCKER_OUT="$(doctor --check-docker 2>&1)"
DOCKER_RC=$?
if [ "$DOCKER_RC" -ne 0 ]; then
    DOCKER_REASON="$(printf '%s' "$DOCKER_OUT" | json_get reason)"
    DOCKER_DETAIL="$(printf '%s' "$DOCKER_OUT" | json_get detail)"
    case "$DOCKER_REASON" in
        permission_denied)
            if [ "${TM_REEXEC:-}" = "1" ]; then
                echo "      FAILED: Docker permission problem persists even inside the 'docker' group."
                echo "      Re-login or run: newgrp docker"
                exit 1
            fi
            if ! command -v sg >/dev/null 2>&1; then
                echo "      FAILED: 'sg' command not found - cannot re-run inside the docker group."
                echo "      Re-login or run: newgrp docker"
                exit 1
            fi
            echo "      Re-running inside the 'docker' group ..."
            export TM_REEXEC=1
            exec sg docker -c "cd '$SCRIPT_DIR' && '$SCRIPT_DIR/$(basename "$0")'"
            ;;
        daemon_down)
            echo "      FAILED: Docker daemon is not running."
            echo "      Start it with:  sudo systemctl enable --now docker"
            exit 1
            ;;
        *)
            echo "      FAILED: Docker is not usable (reason: ${DOCKER_REASON:-unknown})."
            [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
            exit 1
            ;;
    esac
fi
echo "      ok."

# ------------------------------------------------ [3/7] Ports 8000 (API) / 5173 (Vite)
echo "[3/7] Ports 8000 (backend) and 5173 (frontend) ..."
for port in 8000 5173; do
    PORT_OUT="$(doctor --check-port "$port" 2>&1)"
    PORT_RC=$?
    if [ "$PORT_RC" -ne 0 ]; then
        PORT_DETAIL="$(printf '%s' "$PORT_OUT" | json_get detail)"
        echo "      FAILED: ${PORT_DETAIL:-port $port is in use}."
        echo "      Stop the other process, then re-run this script."
        exit 1
    fi
done
echo "      ok."

# -------------------------------------------------------------- [4/7] Node.js
echo "[4/7] Node.js >= 18 ..."
NODE_OUT="$(doctor --check-node 2>&1)"
NODE_RC=$?
if [ "$NODE_RC" -ne 0 ]; then
    NODE_REASON="$(printf '%s' "$NODE_OUT" | json_get reason)"
    NODE_DETAIL="$(printf '%s' "$NODE_OUT" | json_get detail)"
    echo "      FAILED: Node.js >= 18 is required but not available (reason: ${NODE_REASON:-unknown})."
    [ -n "$NODE_DETAIL" ] && echo "      $NODE_DETAIL"
    echo "      Install Node.js 18+ (e.g. https://nodejs.org)"
    exit 1
fi
echo "      ok."

# ------------------------------------------------ [5/7] ~/.thoughtmachine writable
echo "[5/7] ~/.thoughtmachine writable ..."
TM_OUT="$(doctor --check-dotthoughtmachine 2>&1)"
TM_RC=$?
if [ "$TM_RC" -ne 0 ]; then
    TM_HINT="$(printf '%s' "$TM_OUT" | json_get hint)"
    echo "      FAILED: ~/.thoughtmachine is not writable."
    echo "      Fix with:  ${TM_HINT:-sudo chown -R \$USER ~/.thoughtmachine}"
    exit 1
fi
echo "      ok."

# ------------------------------------------------------------------ [6/7] Locale
echo "[6/7] Locale ..."
export LANG=C.UTF-8
echo "      LANG set to C.UTF-8 (avoids locale-related errors)."

# ------------------------------------------------------------ [7/7] Ready message
echo "[7/7] All checks passed."
echo ""

if $CHECK_ONLY; then
    echo "  (--check-only: preflight done, nothing was started)"
    exit 0
fi

# ------------------------------------------------------------------- launch
if $PROD_MODE; then
    echo "============================================"
    echo "  ThoughtMachine - production mode"
    echo "  http://localhost:8000  (backend serves the built frontend)"
    echo "============================================"
    echo ""
    export TM_NPM_CMD="$(command -v npm 2>/dev/null || true)"
    exec .venv/bin/python -m web_ui.backend.server --serve-frontend
fi

echo "============================================"
echo "  ThoughtMachine - starting (dev mode)"
echo "  Backend:   http://localhost:8000"
echo "  Frontend:  http://localhost:5173"
echo "============================================"
echo ""

FRONTEND_DIR="$SCRIPT_DIR/web_ui/frontend"
VITE_BIN="$FRONTEND_DIR/node_modules/.bin/vite"

if [ -x "$VITE_BIN" ]; then
    echo "  Starting vite ($VITE_BIN --host 127.0.0.1) ..."
    (cd "$FRONTEND_DIR" && "$VITE_BIN" --host 127.0.0.1) &
    VITE_PID=$!
else
    echo "  vite binary not found - using 'npm run dev' ..."
    (cd "$FRONTEND_DIR" && npm run dev) &
    VITE_PID=$!
fi

# Wait briefly (max ~10s) until the frontend answers on port 5173.
for i in 1 2 3 4 5 6 7 8 9 10; do
    if ! kill -0 "$VITE_PID" 2>/dev/null; then
        echo "  FAILED: the frontend dev server exited during startup."
        exit 1
    fi
    if (command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ':5173 ') || \
       (command -v lsof >/dev/null 2>&1 && lsof -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1); then
        break
    fi
    sleep 1
done

echo ""
echo "  Starting backend (.venv/bin/python -m web_ui.backend.server) ..."
echo "  Stop it with Ctrl-C; the frontend dev server is stopped automatically."
echo ""
.venv/bin/python -m web_ui.backend.server
BACKEND_RC=$?

echo "  Backend exited (rc=$BACKEND_RC) - stopping the frontend dev server ..."
kill "$VITE_PID" 2>/dev/null || true
wait "$VITE_PID" 2>/dev/null || true

exit "$BACKEND_RC"
