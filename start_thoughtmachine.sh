#!/usr/bin/env bash
#===============================================================================
# start_thoughtmachine.sh
#
#  Preflight doctor + launcher for ThoughtMachine.
#  Runs all checks first; when they pass it starts the app:
#    * default (dev):  vite on 127.0.0.1:5173 in the background, plus the
#                      backend (.venv/bin/python -m web_ui.backend.server).
#                      The backend is health-checked (GET /api/health) BEFORE
#                      the frontend is started; when the backend exits, vite
#                      is stopped too.
#    * --prod / -p:    production mode, single process:
#                      .venv/bin/python -m web_ui.backend.server --serve-frontend
#    * --doctor:       preflight (tolerates a missing/unusable Docker daemon) +
#                      start the backend, verify /api/health, print
#                      BACKEND-HEALTHY and keep running.
#    * --check-only:   run ONLY the preflight checks, then exit 0 without
#                      starting anything (also honors TM_CHECK_ONLY=1).
#
#  The backend is always started in the background; its stdout+stderr are
#  mirrored to the console AND to logs/backend_startup.log (via tee). The
#  script polls http://127.0.0.1:8000/api/health for up to 30 s before
#  considering the backend up. The frontend is only started after the
#  backend is healthy.
#===============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Make sure standard system paths are on PATH for docker detection even in
# minimal environments (cron/systemd, non-login shells).
case ":$PATH:" in *:/usr/bin:*) ;; *) export PATH="/usr/bin:$PATH" ;; esac
case ":$PATH:" in *:/usr/sbin:*) ;; *) export PATH="/usr/sbin:$PATH" ;; esac
mkdir -p "$SCRIPT_DIR/logs"

DOCTOR="$SCRIPT_DIR/scripts/doctor_checks.py"
PROD_MODE=false
CHECK_ONLY=false
DOCTOR_MODE=false

for arg in "$@"; do
    case "$arg" in
        --prod|-p)          PROD_MODE=true ;;
        --check-only)       CHECK_ONLY=true ;;
        --doctor)           DOCTOR_MODE=true ;;
        --help|-h)
            echo "Usage: $0 [--prod] [--check-only] [--doctor]"
            echo "  --prod / -p   production mode: backend serves the built frontend"
            echo "  --check-only  run preflight checks only, then exit 0"
            echo "  --doctor      preflight (Docker problems only warn) + start the backend,"
            echo "                verify /api/health, print BACKEND-HEALTHY and keep running"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--prod] [--check-only] [--doctor]"
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

BACKEND_PID=""
VITE_PID=""
BACKEND_LOG="$SCRIPT_DIR/logs/backend_startup.log"

cleanup() {
    if [ -n "${VITE_PID:-}" ]; then
        kill "$VITE_PID" 2>/dev/null || true
        wait "$VITE_PID" 2>/dev/null || true
    fi
    if [ -n "${BACKEND_PID:-}" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
}
trap 'cleanup; exit 130' INT TERM

# Poll GET http://127.0.0.1:8000/api/health for up to 30 s (30 x 1 s).
# Returns 0 when the backend answers 200; 1 on timeout or early exit.
# Fallback: when /api/health returns 404 (route not registered in this build),
# probe /api/health/containers (the real readiness endpoint) instead.
wait_for_backend() {
    python3 - "$1" <<'PY'
import os
import sys
import time
import urllib.error
import urllib.request

pid = sys.argv[1]
have_proc = os.path.isdir("/proc")
deadline = time.time() + 30
while time.time() < deadline:
    if have_proc and not os.path.isdir("/proc/%s" % pid):
        break
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2) as resp:
            if resp.status == 200:
                sys.exit(0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000/api/health/containers", timeout=2) as resp2:
                    if resp2.status == 200:
                        sys.exit(0)
            except Exception:
                pass
    except Exception:
        pass
    time.sleep(1)
sys.exit(1)
PY
    return $?
}

# Start the backend in the background with stdout+stderr mirrored to the
# console and logs/backend_startup.log, then wait for it to become healthy.
# Exits 1 (with the log tail) on failure.
start_backend() {
    # $1 = backend command line (kept word-split on purpose)
    mkdir -p "$SCRIPT_DIR/logs"
    echo "  Starting backend; console output mirrored to logs/backend_startup.log ..."
    # shellcheck disable=SC2086
    # The subshell execs the backend, so $! is the backend's own PID (tee's
    # PID is not captured); tee truncates the log on start.
    ( exec $1 ) > >(tee "$BACKEND_LOG") 2>&1 &
    BACKEND_PID=$!
    echo "  Backend PID: $BACKEND_PID"
    if ! wait_for_backend "$BACKEND_PID"; then
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
        echo ""
        echo "Backend failed to start. See logs/backend_startup.log. Last lines:"
        tail -50 "$BACKEND_LOG" 2>/dev/null | sed 's/^/  /'
        exit 1
    fi
    echo "  Backend is healthy (/api/health)."
}

echo "============================================"
echo "  ThoughtMachine - preflight check"
echo "============================================"
echo ""

# ------------------------------------------- required tools (before all checks)
echo "Required tools ..."
TOOLS_OUT="$(doctor --check-tools 2>&1)"
TOOLS_RC=$?
if [ "$TOOLS_RC" -ne 0 ]; then
    echo "      FAILED: required tools are missing:"
    printf '%s' "$TOOLS_OUT" | python3 -c 'import json, sys
d = json.load(sys.stdin)
for name in d.get("critical_missing", []):
    print("    - %s: %s" % (name, d["tools"][name].get("hint", "")))' 2>/dev/null | sed 's/^/      /'
    echo "      Install the missing tools, then re-run this script."
    exit 1
fi
DOCKER_PRESENT="$(printf '%s' "$TOOLS_OUT" | json_get docker_present)"
if [ "$DOCKER_PRESENT" = "False" ]; then
    echo "      NOTE: docker not found - continuing without Docker (degraded mode)."
    echo "      Install it with:  $(printf '%s' "$TOOLS_OUT" | json_get docker_hint)"
fi
echo "      ok."

# system health table (informational; failures do not block startup)
python3 -m thoughtmachine.doctor || true
echo ""

# --------------------------------------------------------- [1/8] venv (critical)
echo "[1/8] Python virtual environment ..."
VENV_OUT="$(doctor --ensure-venv 2>&1)"
VENV_RC=$?
if [ "$VENV_RC" -ne 0 ]; then
    echo "      FAILED: virtual environment (.venv) could not be set up."
    printf '%s\n' "$VENV_OUT" | sed 's/^/      /'
    echo "      Run ./install.sh first"
    exit 1
fi
VENV_BROKEN="$(printf '%s' "$VENV_OUT" | json_get broken_reason)"
if [ -n "$VENV_BROKEN" ]; then
    echo "Venv is broken. Recreating automatically..."
fi
echo "      ok."

# ------------------------------------------------------------ [2/8] Docker access
echo "[2/8] Docker ..."
DOCKER_OUT="$(doctor --check-docker 2>&1)"
DOCKER_RC=$?
if [ "$DOCKER_RC" -ne 0 ]; then
    DOCKER_REASON="$(printf '%s' "$DOCKER_OUT" | json_get reason)"
    DOCKER_DETAIL="$(printf '%s' "$DOCKER_OUT" | json_get detail)"
    case "$DOCKER_REASON" in
        permission_denied)
            if $CHECK_ONLY; then
                echo "      WARNING: Docker permission problem (reason: permission_denied) - continuing in --check-only mode."
                [ -n "$DOCKER_DETAIL" ] && printf '%s\n' "$DOCKER_DETAIL" | sed 's/^/      /'
            elif [ "${TM_REEXEC:-}" = "1" ]; then
                if $DOCTOR_MODE; then
                    echo "      WARNING: Docker permission problem persists even inside the 'docker' group (continuing in --doctor mode)."
                else
                    echo "      FAILED: Docker permission problem persists even inside the 'docker' group."
                    echo "      Re-login or run: newgrp docker"
                    exit 1
                fi
            elif ! command -v sg >/dev/null 2>&1; then
                echo "      FAILED: 'sg' command not found - cannot re-run inside the docker group."
                echo "      Re-login or run: newgrp docker"
                exit 1
            else
                echo "      Re-running inside the 'docker' group ..."
                export TM_REEXEC=1
                exec sg docker -c "cd '$SCRIPT_DIR' && '$SCRIPT_DIR/$(basename "$0")' $*"
            fi
            ;;
        daemon_down|lib_missing)
            if $CHECK_ONLY; then
                echo "      WARNING: Docker is not usable (reason: $DOCKER_REASON) - continuing in --check-only mode."
                [ -n "$DOCKER_DETAIL" ] && printf '%s\n' "$DOCKER_DETAIL" | sed 's/^/      /'
            elif $DOCTOR_MODE; then
                echo "      WARNING: Docker is not usable (reason: $DOCKER_REASON) - continuing in --doctor mode."
                [ -n "$DOCKER_DETAIL" ] && printf '%s\n' "$DOCKER_DETAIL" | sed 's/^/      /'
            else
                echo "      FAILED: Docker is not usable (reason: ${DOCKER_REASON:-unknown})."
                [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
                if [ "$DOCKER_REASON" = "daemon_down" ]; then
                    echo "      Start it with:  sudo systemctl enable --now docker"
                fi
                exit 1
            fi
            ;;
        *)
            if $CHECK_ONLY; then
                echo "      WARNING: Docker check failed (reason: ${DOCKER_REASON:-unknown}) - continuing in --check-only mode."
                [ -n "$DOCKER_DETAIL" ] && printf '%s\n' "$DOCKER_DETAIL" | sed 's/^/      /'
            elif $DOCTOR_MODE; then
                echo "      WARNING: Docker check failed (reason: ${DOCKER_REASON:-unknown}) - continuing in --doctor mode."
                [ -n "$DOCKER_DETAIL" ] && printf '%s\n' "$DOCKER_DETAIL" | sed 's/^/      /'
            else
                echo "      FAILED: Docker is not usable (reason: ${DOCKER_REASON:-unknown})."
                [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
                exit 1
            fi
            ;;
    esac
else
    echo "      ok."
fi

# ---------------------------------------------- [3/8] Stale containers cleanup
echo "[3/8] Stale ThoughtMachine containers ..."
STALE_OUT="$(doctor --check-stale-containers 2>&1)"
STALE_RC=$?
STALE_COUNT="$(printf '%s' "$STALE_OUT" | json_get count)"
if [ "$STALE_RC" -eq 0 ]; then
    echo "      ok (no stale containers)."
elif [ -z "$STALE_COUNT" ]; then
    echo "      (stale-container check skipped)"
else
    STALE_DETAIL="$(printf '%s' "$STALE_OUT" | json_get detail)"
    echo "      found ${STALE_COUNT} stale container(s)."
    [ -n "$STALE_DETAIL" ] && printf '%s\n' "$STALE_DETAIL" | sed 's/^/      /'
    if $CHECK_ONLY; then
        echo "      (--check-only: stale containers reported, not removed)"
    else
        CLEAN_OUT="$(doctor --clean-stale-containers 2>&1)" || true
        CLEAN_DETAIL="$(printf '%s' "$CLEAN_OUT" | json_get detail)"
        if [ -n "$CLEAN_DETAIL" ]; then
            echo "      $CLEAN_DETAIL."
        else
            echo "      (cleanup output unavailable - ignoring)"
        fi
    fi
fi
echo ""

# ------------------------------------------------ [4/8] Ports 8000 (API) / 5173 (Vite)
echo "[4/8] Ports 8000 (backend) and 5173 (frontend) ..."
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

# -------------------------------------------------------------- [5/8] Node.js
echo "[5/8] Node.js >= 18 ..."
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

# ------------------------------------------------ [6/8] ~/.thoughtmachine writable
echo "[6/8] ~/.thoughtmachine writable ..."
TM_OUT="$(doctor --check-dotthoughtmachine 2>&1)"
TM_RC=$?
if [ "$TM_RC" -ne 0 ]; then
    if sudo -n true 2>/dev/null; then
        sudo chown -R "$USER" ~/.thoughtmachine 2>/dev/null || true
        TM_OUT="$(doctor --check-dotthoughtmachine 2>&1)"
        TM_RC=$?
    fi
fi
if [ "$TM_RC" -ne 0 ]; then
    echo "Vault not writable. Fix with: sudo chown -R $USER ~/.thoughtmachine"
    exit 1
fi
echo "      ok."

# ------------------------------------------------------------------ [7/8] Locale
echo "[7/8] Locale ..."
export LANG=C.UTF-8
echo "      LANG set to C.UTF-8 (avoids locale-related errors)."

# ------------------------------------------------------------ [8/8] Ready message
echo "[8/8] All checks passed."
echo ""

if $CHECK_ONLY; then
    echo "  (--check-only: preflight done, nothing was started)"
    exit 0
fi

# ------------------------------------------------------------------- launch
if $DOCTOR_MODE; then
    echo "============================================"
    echo "  ThoughtMachine - doctor mode"
    echo "============================================"
    echo ""
    start_backend ".venv/bin/python -m web_ui.backend.server"
    echo ""
    echo "BACKEND-HEALTHY"
    echo "  (--doctor: backend verified healthy; press Ctrl-C to stop)"
    wait "$BACKEND_PID"
    BACKEND_RC=$?
    echo "  Backend exited (rc=$BACKEND_RC)."
    exit "$BACKEND_RC"
fi

if $PROD_MODE; then
    echo "============================================"
    echo "  ThoughtMachine - production mode"
    echo "  http://localhost:8000  (backend serves the built frontend)"
    echo "============================================"
    echo ""
    export TM_NPM_CMD="$(command -v npm 2>/dev/null || true)"
    start_backend ".venv/bin/python -m web_ui.backend.server --serve-frontend"
    echo ""
    echo "  Backend is running (PID $BACKEND_PID); serving the frontend on http://localhost:8000."
    echo "  Stop it with Ctrl-C."
    wait "$BACKEND_PID"
    BACKEND_RC=$?
    exit "$BACKEND_RC"
fi

echo "============================================"
echo "  ThoughtMachine - starting (dev mode)"
echo "  Backend:   http://localhost:8000"
echo "  Frontend:  http://localhost:5173"
echo "============================================"
echo ""

start_backend ".venv/bin/python -m web_ui.backend.server"

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
        cleanup
        exit 1
    fi
    if (command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ':5173 ') || \
       (command -v lsof >/dev/null 2>&1 && lsof -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1); then
        break
    fi
    sleep 1
done

echo ""
echo "  Backend is running (PID $BACKEND_PID) with the frontend dev server on http://localhost:5173."
echo "  Stop it with Ctrl-C; the frontend dev server is stopped automatically."
echo ""
wait "$BACKEND_PID"
BACKEND_RC=$?

echo "  Backend exited (rc=$BACKEND_RC) - stopping the frontend dev server ..."
cleanup

exit "$BACKEND_RC"
