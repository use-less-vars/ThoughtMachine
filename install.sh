#!/usr/bin/env bash
#===============================================================================
# install.sh - one-command installer for ThoughtMachine
#
# Runs the doctor checks and fixes what it can:
#   [1/4] Docker daemon   (non-critical: warn + continue if it cannot start)
#   [2/4] Docker group    (non-critical: add user, then "Re-login or run: newgrp docker")
#   [3/4] Python venv     (CRITICAL: abort with exit 1 on failure)
#   [4/4] Node.js >= 18   (CRITICAL: abort with exit 1 on failure)
#
# Every step is idempotent - re-running is safe.
# No global `set -e`: failures are handled explicitly step by step.
#===============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
DOCTOR="$SCRIPT_DIR/scripts/doctor_checks.py"

doctor() {
    python3 "$DOCTOR" "$@"
}

# Extract a field from a JSON document passed on stdin. Prints "" on any error.
json_get() {
    python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get(sys.argv[1], "") if isinstance(d, dict) else "")' "$1" 2>/dev/null || true
}

DONE_OK=()
DONE_SKIP=()

echo "============================================"
echo "  ThoughtMachine - Install"
echo "============================================"
echo ""

# ------------------------------------------------------------------ [1/4] Docker daemon
echo "[1/4] Docker daemon ..."
DOCKER_OUT="$(doctor --check-docker 2>&1)"
DOCKER_RC=$?
if [ "$DOCKER_RC" -ne 0 ]; then
    DOCKER_REASON="$(printf '%s' "$DOCKER_OUT" | json_get reason)"
    if [ "$DOCKER_REASON" = "daemon_down" ]; then
        echo "      daemon not running - trying to start it (may prompt for sudo password)..."
        doctor --ensure-docker-daemon 2>&1 | sed 's/^/      /' || true
        DOCKER_OUT="$(doctor --check-docker 2>&1)"
        DOCKER_RC=$?
    fi
    if [ "$DOCKER_RC" -ne 0 ]; then
        DOCKER_DETAIL="$(printf '%s' "$DOCKER_OUT" | json_get detail)"
        echo "      WARNING: Docker is not usable (reason: ${DOCKER_REASON:-unknown})."
        [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
        echo "      Non-critical: continuing, but containers will not work until Docker runs."
        DONE_SKIP+=("Docker daemon (not running)")
    else
        echo "      ok."
        DONE_OK+=("Docker daemon")
    fi
else
    echo "      ok."
    DONE_OK+=("Docker daemon")
fi
echo ""

# ------------------------------------------------------------------ [2/4] Docker group
echo "[2/4] Docker group ..."
GROUP_OUT="$(doctor --check-docker-group 2>&1)"
GROUP_RC=$?
if [ "$GROUP_RC" -ne 0 ]; then
    echo "      user not in the 'docker' group - adding (may prompt for sudo password)..."
    doctor --ensure-docker-group 2>&1 | sed 's/^/      /' || true
    echo "      Re-login or run: newgrp docker"
    DONE_SKIP+=("Docker group (re-login required)")
else
    echo "      ok."
    DONE_OK+=("Docker group")
fi
echo ""

# ------------------------------------------------------------------ [3/4] venv (critical)
echo "[3/4] Python virtual environment ..."
VENV_OUT="$(doctor --ensure-venv 2>&1)"
VENV_RC=$?
if [ "$VENV_RC" -ne 0 ]; then
    echo "      FAILED to set up the virtual environment:"
    printf '%s\n' "$VENV_OUT" | sed 's/^/      /'
    echo ""
    echo "  Installation aborted."
    exit 1
fi
VENV_CHANGED="$(printf '%s' "$VENV_OUT" | json_get changed)"
VENV_DETAIL="$(printf '%s' "$VENV_OUT" | json_get detail)"
echo "      ${VENV_DETAIL:-virtual environment ready}."
if [ "$VENV_CHANGED" = "True" ]; then
    DONE_OK+=("Python venv (installed/updated)")
else
    DONE_OK+=("Python venv (up to date)")
fi
echo ""

# ------------------------------------------------------------------ [4/4] node (critical)
echo "[4/4] Node.js ..."
NODE_OUT="$(doctor --check-node 2>&1)"
NODE_RC=$?
if [ "$NODE_RC" -ne 0 ]; then
    NODE_REASON="$(printf '%s' "$NODE_OUT" | json_get reason)"
    NODE_DETAIL="$(printf '%s' "$NODE_OUT" | json_get detail)"
    echo "      FAILED: Node.js >= 18 is required but not available (reason: ${NODE_REASON:-unknown})."
    [ -n "$NODE_DETAIL" ] && echo "      $NODE_DETAIL"
    echo "      Install Node.js 18+ (e.g. https://nodejs.org), then re-run ./install.sh"
    echo ""
    echo "  Installation aborted."
    exit 1
fi
echo "      ok."
DONE_OK+=("Node.js")
echo ""

# ------------------------------------------------------------------ summary
echo "============================================"
echo "  Summary"
echo "============================================"
if [ "${#DONE_OK[@]}" -gt 0 ]; then
    printf '  [ok]  %s\n' "${DONE_OK[@]}"
fi
if [ "${#DONE_SKIP[@]}" -gt 0 ]; then
    printf '  [--]  %s\n' "${DONE_SKIP[@]}"
fi
echo ""
echo "Starte mit ./start_thoughtmachine.sh"
exit 0
