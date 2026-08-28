#!/usr/bin/env bash
#===============================================================================
# install.sh - one-command installer for ThoughtMachine (Linux / x86_64)
#
# Supported platforms: Debian/Ubuntu on x86_64 (amd64). Other OSes/arches
# exit early with a pointer to the right installer (install_thoughtmachine.bat
# on Windows; manual setup on macOS).
#
# Runs the doctor checks and fixes what it can:
#   [1/5] Python >= 3.11  (CRITICAL: abort with exit 1 on failure)
#   [2/5] Docker daemon   (CRITICAL: install or start, abort with exit 1 on failure)
#   [3/5] Docker group    (non-critical: add user, then "Re-login or run: newgrp docker")
#   [4/5] Python venv     (CRITICAL: abort with exit 1 on failure)
#   [5/5] Node.js >= 18   (CRITICAL: abort with exit 1 on failure)
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

# ---------------------------------------------------------------- CI handling
# GitHub Actions runners have no usable Docker daemon and no passwordless
# sudo, so on CI the Docker steps must be optional. When CI is set, the
# Docker install/daemon/group checks below are skipped; on a normal machine
# (CI unset) the original behavior is preserved unchanged.
CI="${CI:-}"
if [ -n "$CI" ]; then
    DOCKER_NONFATAL=1
else
    DOCKER_NONFATAL=0
fi

echo "============================================"
echo "  ThoughtMachine - Install"
echo "============================================"
echo ""

# ---------------------------------------------------------------- platform gate
# The Linux installer targets Debian/Ubuntu on x86_64. Anything else exits
# early with a pointer to the right path.
UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
case "$UNAME_S" in
    Darwin)
        echo "ERROR: macOS is not supported by this installer."
        echo "       Install Docker Desktop, Python >= 3.11 and Node.js >= 18,"
        echo "       then run ./start_thoughtmachine.sh directly."
        exit 1
        ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "ERROR: this is the Linux installer; on Windows use install_thoughtmachine.bat."
        exit 1
        ;;
    Linux) ;;
    *)
        echo "ERROR: unsupported operating system: $UNAME_S (expected Linux)."
        exit 1
        ;;
esac

UNAME_M="$(uname -m 2>/dev/null || echo unknown)"
case "$UNAME_M" in
    x86_64|amd64) ;;
    *)
        echo "ERROR: unsupported architecture: $UNAME_M (expected x86_64/amd64)."
        exit 1
        ;;
esac

DISTRO_ID="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | tr -d '"' | head -n1)"
case "$DISTRO_ID" in
    debian|ubuntu) ;;
    *)
        echo "ERROR: unsupported distribution: ${DISTRO_ID:-unknown} (expected debian or ubuntu)."
        exit 1
        ;;
esac

# ------------------------------------------------------------------ [1/5] Python (critical)
echo "[1/5] Python >= 3.11 ..."
PY_OUT="$(doctor --check-python 2>&1)"
PY_RC=$?
if [ "$PY_RC" -ne 0 ]; then
    PY_REASON="$(printf '%s' "$PY_OUT" | json_get reason)"
    PY_DETAIL="$(printf '%s' "$PY_OUT" | json_get detail)"
    echo "      FAILED: Python >= 3.11 is required but not available (reason: ${PY_REASON:-unknown})."
    [ -n "$PY_DETAIL" ] && echo "      $PY_DETAIL"
    echo "      Install Python >= 3.11 (e.g. https://www.python.org/downloads/), then re-run ./install.sh"
    echo ""
    echo "  Installation aborted."
    exit 1
fi
PY_VERSION="$(printf '%s' "$PY_OUT" | json_get version)"
echo "      ok (python3 ${PY_VERSION:-version unknown})."
DONE_OK+=("Python >= 3.11 (${PY_VERSION:-unknown})")
echo ""

if [ "$DOCKER_NONFATAL" -eq 1 ]; then
    echo "NOTE: CI environment detected — Docker checks skipped."
    echo ""
else
# ------------------------------------------------------------------ [2/5] Docker daemon (critical)
echo "[2/5] Docker daemon ..."
DOCKER_OUT="$(doctor --check-docker 2>&1)"
DOCKER_RC=$?
if [ "$DOCKER_RC" -ne 0 ]; then
    DOCKER_REASON="$(printf '%s' "$DOCKER_OUT" | json_get reason)"
    DOCKER_DETAIL="$(printf '%s' "$DOCKER_OUT" | json_get detail)"
    case "$DOCKER_REASON" in
        lib_missing)
            if sudo -n true 2>/dev/null; then
                echo "      docker CLI not found - installing docker.io via apt-get (may take a moment)..."
                sudo apt-get update && sudo apt-get install -y docker.io 2>&1 | sed 's/^/      /' || true
            else
                echo "      FAILED: Docker is not installed and this installer needs sudo to install it."
                echo "      Run this yourself, then re-run ./install.sh:"
                echo "      sudo apt-get update && sudo apt-get install -y docker.io"
                echo ""
                echo "  Installation aborted."
                exit 1
            fi
            DOCKER_OUT="$(doctor --check-docker 2>&1)"
            DOCKER_RC=$?
            if [ "$DOCKER_RC" -ne 0 ]; then
                DOCKER_DETAIL="$(printf '%s' "$DOCKER_OUT" | json_get detail)"
                echo "      FAILED: Docker still not usable after installation."
                [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
                echo "      Start it with:  sudo systemctl enable --now docker"
                echo ""
                echo "  Installation aborted."
                exit 1
            fi
            ;;
        daemon_down)
            echo "      daemon not running - trying to start it (may prompt for sudo password)..."
            doctor --ensure-docker-daemon 2>&1 | sed 's/^/      /' || true
            DOCKER_OUT="$(doctor --check-docker 2>&1)"
            DOCKER_RC=$?
            if [ "$DOCKER_RC" -ne 0 ]; then
                DOCKER_DETAIL="$(printf '%s' "$DOCKER_OUT" | json_get detail)"
                echo "      FAILED: Docker daemon could not be started."
                [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
                echo "      Start it with:  sudo systemctl enable --now docker"
                echo ""
                echo "  Installation aborted."
                exit 1
            fi
            ;;
        permission_denied)
            echo "      WARNING: Docker permission problem (reason: permission_denied)."
            [ -n "$DOCKER_DETAIL" ] && printf '%s\n' "$DOCKER_DETAIL" | sed 's/^/      /'
            echo "      Non-critical here: the next step adds your user to the 'docker' group."
            DONE_SKIP+=("Docker daemon (permission problem - fixed by the group step)")
            ;;
        *)
            echo "      FAILED: Docker is not usable (reason: ${DOCKER_REASON:-unknown})."
            [ -n "$DOCKER_DETAIL" ] && echo "      $DOCKER_DETAIL"
            echo ""
            echo "  Installation aborted."
            exit 1
            ;;
    esac
fi
if [ "$DOCKER_RC" -eq 0 ]; then
    echo "      ok."
    DONE_OK+=("Docker daemon")
fi
echo ""

# ------------------------------------------------------------------ [3/5] Docker group
echo "[3/5] Docker group ..."
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
fi

# ------------------------------------------------------------------ [4/5] venv (critical)
echo "[4/5] Python virtual environment ..."
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

# ------------------------------------------------------------------ [5/5] node (critical)
echo "[5/5] Node.js ..."
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
NODE_VERSION="$(printf '%s' "$NODE_OUT" | json_get version)"
echo "      ok (node ${NODE_VERSION:-version unknown})."
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
echo "Next step: ./start_thoughtmachine.sh"
exit 0
