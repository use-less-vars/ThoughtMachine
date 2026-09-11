#!/usr/bin/env bash
# =============================================================================
# reproduce_p0.sh - HOST-RUN orchestrator for the P0 bug reproducer.
# =============================================================================
#
# RAW OUTPUT INSTRUCTION (read this first)
# ----------------------------------------
# Run this on the HOST (where the REAL docker daemon lives), from the repo root:
#
#     bash .thoughtmachine/working_docs/reproduce_p0.sh 2>&1 | tee /tmp/p0_evidence.txt
#
# Then PASTE THE ENTIRE FILE /tmp/p0_evidence.txt BACK - every line, including
# tracebacks and the SUMMARY table. Do NOT summarise or trim it. The raw
# evidence is the whole point of this reproducer.
#
# NOTE: this script CANNOT run inside the worker container - there is no docker
# daemon / docker socket in there. It must be run on the HOST.
#
# SAFETY: it touches ONLY containers whose name starts with `tm-p0-`, and uses a
# throwaway vault ($P0_VAULT, default /tmp/tm-p0-vault) which is deleted on exit.
# This script is intentionally NOT `set -e`: every stage runs even if an earlier
# one fails, so the operator gets the complete picture.
# =============================================================================

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || { echo "FATAL: cannot cd to repo root"; exit 1; }

P0_VENV="${P0_VENV:-./.venv}"
P0_VAULT="${P0_VAULT:-/tmp/tm-p0-vault}"
P0_IMAGE="${P0_IMAGE:-alpine:latest}"
P0_IMAGE_B="${P0_IMAGE_B:-}"
P0_WS="${P0_WS:-}"
P0_REAL_VAULT_ROOT="${P0_REAL_VAULT_ROOT:-}"
export P0_VAULT P0_IMAGE P0_IMAGE_B P0_WS P0_REAL_VAULT_ROOT THOUGHTMACHINE_VAULT_ROOT="$P0_VAULT"

echo "==================================================================="
echo " P0 REPRODUCER  (HOST run)"
echo " repo root : $REPO_ROOT"
echo " venv      : $P0_VENV"
echo " vault     : $P0_VAULT"
echo " image     : $P0_IMAGE"
echo " image_B   : ${P0_IMAGE_B:-<unset>}"
echo " P0_WS     : ${P0_WS:-<unset> (synthetic-vault mode only)}"
echo " real vault: ${P0_REAL_VAULT_ROOT:-<framework default>}"
echo "==================================================================="
echo ">> paste the FULL raw output back (tee to /tmp/p0_evidence.txt helps)."

# ---------------------------------------------------------------------------
echo
echo "=== [1/6] PREFLIGHT ==="
if [ -x "$P0_VENV/bin/python" ]; then
  PY="$P0_VENV/bin/python"
else
  echo "WARNING: $P0_VENV/bin/python not found; falling back to 'python3'"
  PY="python3"
fi
echo "using PY=$PY"
"$PY" -V

# ---------------------------------------------------------------------------
echo
echo "=== [2/6] DOCKER DAEMON ==="
if command -v docker >/dev/null 2>&1; then
  timeout 30 docker version 2>&1 || echo "WARNING: 'docker version' failed (daemon down?)"
else
  echo "WARNING: docker CLI not found (docker-py may still work)"
fi

# ---------------------------------------------------------------------------
echo
echo "=== [3/6] LOCAL IMAGE ==="
if command -v docker >/dev/null 2>&1; then
  if docker image inspect "$P0_IMAGE" >/dev/null 2>&1; then
    echo "image $P0_IMAGE present"
  else
    echo "WARNING: image $P0_IMAGE MISSING - BUG1/BUG3 need it."
    echo "         run:  docker pull $P0_IMAGE"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "=== [4/6] RUN DRIVER (timeout 120s) ==="
if timeout 120 "$PY" .thoughtmachine/working_docs/reproduce_p0.py; then
  RC=0
else
  RC=$?
  if [ "$RC" -eq 124 ]; then
    echo "TIMEOUT (120s) - driver exceeded its budget; continuing to cleanup."
  else
    echo "driver exited with code $RC; continuing to cleanup."
  fi
fi
echo "driver exit code: $RC"

# ---------------------------------------------------------------------------
echo
echo "=== [5/6] DRIVER (as fallback) / LEFT-OVER CONTAINERS ==="
# re-run only if the driver produced nothing above is NOT done here; this stage
# just lists what is left so cleanup is auditable.
if command -v docker >/dev/null 2>&1; then
  echo "-- tm-p0- containers BEFORE cleanup --"
  docker ps -a --filter name=tm-p0- --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
fi

# ---------------------------------------------------------------------------
echo
echo "=== [6/6] CLEANUP ==="
if command -v docker >/dev/null 2>&1; then
  for n in $(docker ps -a --filter name=tm-p0- --format '{{.Names}}'); do
    case "$n" in
      tm-p0-*) echo "removing $n"; docker rm -f "$n" || true ;;
      *)       echo "SKIP non-tm-p0- name: $n" ;;
    esac
  done
  echo "-- tm-p0- containers AFTER cleanup --"
  docker ps -a --filter name=tm-p0- --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
fi
rm -rf "$P0_VAULT" || true
echo "cleanup done."

echo
echo "==================================================================="
echo " DONE. Paste the FULL raw output above (tracebacks + SUMMARY table)."
echo "==================================================================="
