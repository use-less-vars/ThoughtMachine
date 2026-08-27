#!/usr/bin/env bash
#===============================================================================
# clean_install_test.sh - clean-slate install smoke test (local fallback to CI)
#
# Usage:
#   docker run --rm -v "$PWD":/src -w /src debian:13 bash tests/clean_install_test.sh
#
# What it verifies on a pristine Debian 13 container:
#   [1] The documented apt prerequisites install cleanly.
#   [2] ./install.sh completes successfully (exit 0) - the Docker steps
#       warn+skip (non-critical), while the venv and Node.js checks
#       (critical) must pass.
#   [3] ./start_thoughtmachine.sh --doctor boots the backend even though
#       Docker is unavailable, and GET /api/health/containers answers
#       HTTP 200 with the expected "degraded" payload
#       (docker.available=false, reason from the degraded taxonomy) -
#       i.e. the health layer degrades gracefully instead of crashing.
#   [4] Backend startup logs contain no Python import errors / tracebacks.
#
# NOT covered here (by design):
#   * ./start_thoughtmachine.sh --check-only is NOT run: that mode REQUIRES a
#     working Docker daemon (doctor check [2/7] treats daemon_down /
#     lib_missing as fatal outside --doctor mode). This container has no
#     Docker socket, so the full check-only path is exercised in CI instead
#     (.github/workflows/first_start_ci.yml), where GitHub Actions mounts the
#     host socket into the job container.
#
# Notes:
#   * docker.io is deliberately NOT installed: this script exercises the
#     genuinely docker-less path. The backend's Docker SDK (inside the venv
#     created by install.sh) talks to the unix socket directly and therefore
#     fails too -> the health endpoint must report degraded (reason
#     daemon_down; lib_missing / permission_denied are also accepted).
#   * Debian 13 ships Python 3.13 (>= the 3.11 floor) and Node.js 20.x
#     (>= the 18 floor); python3-venv / python3-dev / build-essential are
#     installed so `python3 -m venv` and any pip sdists work.
#   * This script itself never touches Docker, so it is safe to run inside
#     any container or on any host with the prerequisites.
#
# Style: `set -u` only, no global `set -e` (repo style) - every critical
# command's exit status is checked explicitly and failures exit non-zero
# with a clear message.
#===============================================================================
set -u

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Resolve the repo root from this script's location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "============================================"
echo "  clean_install_test.sh - $(pwd)"
echo "============================================"

if command -v docker >/dev/null 2>&1; then
    echo "NOTE: a docker CLI is present, but this test targets docker-less"
    echo "      environments; the degraded-health assertions may fail if a"
    echo "      daemon is reachable from here."
fi

# ------------------------------------------------------------------ [1/5] prereqs
echo "[1/5] Installing apt prerequisites (docker.io deliberately omitted) ..."
apt-get update >/dev/null || fail "apt-get update failed"
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev build-essential \
    nodejs npm curl git iproute2 util-linux >/dev/null || fail "apt-get install failed"
echo "      ok."

# ------------------------------------------------------------------ [2/5] ~/.thoughtmachine
# start_thoughtmachine.sh check [5/7] (~/.thoughtmachine writable) is fatal in
# ALL modes - including --doctor - so it must exist before the launcher runs.
echo "[2/5] Pre-creating \$HOME/.thoughtmachine ..."
mkdir -p "$HOME/.thoughtmachine" || fail "could not create $HOME/.thoughtmachine"
echo "      ok ($HOME/.thoughtmachine)."

# ------------------------------------------------------------------ [3/5] install.sh
echo "[3/5] Running ./install.sh (docker steps warn+skip; venv+node are critical) ..."
bash install.sh
rc=$?
[ "$rc" -eq 0 ] || fail "./install.sh exited with rc=$rc (venv or Node.js check failed)"
echo "      ok (exit 0)."

# ------------------------------------------------------------------ [4/5] check-only skipped
echo "[4/5] SKIPPING ./start_thoughtmachine.sh --check-only"
echo "      Reason: that mode requires a working Docker daemon (daemon_down/"
echo "      lib_missing is fatal outside --doctor mode); this container has no"
echo "      Docker socket. The check-only path is covered by CI"
echo "      (.github/workflows/first_start_ci.yml, host socket mounted in)."

# ------------------------------------------------------------------ [5/5] doctor mode
echo "[5/5] Running ./start_thoughtmachine.sh --doctor (Docker unavailable) ..."
mkdir -p logs
bash start_thoughtmachine.sh --doctor > logs/doctor_local.log 2>&1 &
DOCTOR_PID=$!
echo "      doctor wrapper pid: $DOCTOR_PID"

# Poll /api/health/containers (NOT /api/health - that route does not exist)
# for up to 30s; the backend writes logs/backend_startup.log itself.
HEALTH_BODY=""
i=0
while [ "$i" -lt 30 ]; do
    i=$((i + 1))
    HEALTH_BODY="$(curl -fsS --max-time 2 http://127.0.0.1:8000/api/health/containers 2>/dev/null || true)"
    if [ -n "$HEALTH_BODY" ]; then
        break
    fi
    if ! kill -0 "$DOCTOR_PID" 2>/dev/null; then
        echo "      doctor wrapper exited early - tail of logs/doctor_local.log:"
        tail -n 40 logs/doctor_local.log 2>/dev/null || true
        break
    fi
    sleep 1
done

if [ -z "$HEALTH_BODY" ]; then
    fail "GET /api/health/containers never returned HTTP 200 within 30s (see logs/doctor_local.log)"
fi

# Assert the degraded payload shape. The backend uses the Docker SDK (venv)
# which talks to the unix socket directly; with no socket present it reports
# daemon_down. lib_missing / permission_denied are accepted as well.
python3 - "$HEALTH_BODY" <<'PY' || fail "health payload assertions failed (see output above)"
import json, sys
data = json.loads(sys.argv[1])
assert isinstance(data, dict), "health payload is not a JSON object"
assert data.get("checked_at"), "checked_at missing from health payload"
docker_info = data.get("docker")
assert isinstance(docker_info, dict), "'docker' key missing from health payload"
assert docker_info.get("available") is False, \
    "docker.available should be False in a docker-less container"
reason = docker_info.get("reason")
assert reason in {"daemon_down", "lib_missing", "permission_denied"}, \
    "unexpected degraded reason: %r" % (reason,)
print("      OK: health degraded as expected (reason=%s)" % reason)
PY

# The backend must have started cleanly - no import errors/tracebacks.
if grep -nE 'ImportError|ModuleNotFoundError|Traceback' \
    logs/backend_startup.log logs/doctor_local.log; then
    fail "backend startup / doctor logs contain Python import errors or tracebacks (see above)"
fi
echo "      OK: no import errors/tracebacks in logs/backend_startup.log or logs/doctor_local.log"

# Cleanup: stop the backend first (the --doctor wrapper waits on it), then
# the wrapper itself.
pkill -f 'web_ui.backend.server' 2>/dev/null || true
kill "$DOCTOR_PID" 2>/dev/null || true
sleep 1
if kill -0 "$DOCTOR_PID" 2>/dev/null; then
    kill -9 "$DOCTOR_PID" 2>/dev/null || true
fi

echo ""
echo "ALL CHECKS PASSED."
exit 0
