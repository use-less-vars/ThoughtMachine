#!/bin/bash
#===============================================================================
# kill_thoughtmachine.sh
#
#  ⚠ SYNCED with kill_thoughtmachine.bat — keep in agreement.
#  ⚠ If you edit this file, mirror the same change in the batch file.
#===============================================================================
# Forcefully stop all ThoughtMachine processes

#-------------------------------------------------------------------------------
# Forensic receipt. Written BEFORE the kill, and deliberately FAIL-OPEN: this
# script used to leave no trace whatsoever, so "was the kill switch used?" was
# unanswerable after a SIGKILL. Everything here is best-effort -- the block ends
# in "|| true", nothing in it can abort the script, and it must finish BEFORE
# fuser runs (a receipt written afterwards could be killed mid-write). If the
# file cannot be written the kill still happens on the very next line.
# Append-only JSONL; bash + tr only (no python, no jq, no sudo, no journal).
# The signal fuser sends is NOT changed here: "fuser -k" == SIGKILL.
#-------------------------------------------------------------------------------
KF_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
KF_LOG="${KF_DIR:-.}/logs/crash/kill-switch.jsonl"
_kf_sanitize() { printf '%s' "$1" | tr -d '"\\' | tr '\n' ' '; }
{
    mkdir -p "$(dirname "$KF_LOG")"
    printf '{"event":"kill-switch","ts":"%s","user":"%s","pid":%s,"ppid":%s,"cwd":"%s","args":"%s","ports":"5173,5174,5175,5176,5177,8000","signal":"SIGKILL (fuser -k default)","tool":"kill_thoughtmachine.sh"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(_kf_sanitize "$(id -un 2>/dev/null || echo UNKNOWN)")" \
        "$$" "${PPID:-0}" \
        "$(_kf_sanitize "${PWD:-UNKNOWN}")" \
        "$(_kf_sanitize "$*")" \
        >> "$KF_LOG"
} 2>/dev/null || true

fuser -k 5173/tcp 5174/tcp 5175/tcp 5176/tcp 5177/tcp 8000/tcp 2>/dev/null
sleep 1
echo "All ThoughtMachine processes stopped."
