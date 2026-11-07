#!/usr/bin/env bash
#===============================================================================
# assert_check_only_not_vacuous.sh - fail a CI lane when a
# `start_thoughtmachine.sh --check-only` run proves NOTHING about Docker.
#
# Verbatim anchors in start_thoughtmachine.sh (line numbers at this commit):
#   :238 header "[2/8] Docker ..."; :307 usable outcome "      ok.";
#   :247 / :269 / :291 the three --check-only Docker WARNING lines.
#
# Usage:
#   bash scripts/assert_check_only_not_vacuous.sh [--expect-docker|--expect-no-docker] <logfile>
#
# Two modes over the [2/8] Docker section:
#   (default / --expect-docker) - Docker is expected USABLE in this lane.
#     PASS iff the [2/8] section is present AND carries the usable outcome
#     "      ok." (six leading spaces, exactly) AND NO line matches
#     WARNING.*Docker. Any Docker WARNING is NOT a pass: nothing was proven.
#   (--expect-no-docker) - this lane is Docker-less BY CONTRACT (e.g. a job
#     container that ships no docker CLI / socket). PASS iff the [2/8] section
#     is present AND carries NO usable outcome ("      ok.") AND at least one
#     WARNING.*Docker line NAMES a specific reason
#     (lib_missing|daemon_down|permission_denied|other). A usable outcome here
#     means the contract is stale -> flip the lane back to --expect-docker.
#
# A missing [2/8] section is a failure in BOTH modes.
# Exit: 0 pass, 1 assertion failed, 2 usage error (bad args / missing or
# unreadable logfile).
#===============================================================================
set -u

usage() { echo "usage: $0 [--expect-docker|--expect-no-docker] <logfile>" >&2; }

MODE=docker
while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --expect-docker) MODE=docker; shift ;;
        --expect-no-docker) MODE=no_docker; shift ;;
        *) break ;;
    esac
done
[ "$#" -eq 1 ] || { usage; exit 2; }
case "$1" in
    -*) echo "unknown option: $1" >&2; usage; exit 2 ;;
esac
LOG="$1"
if [ ! -f "$LOG" ] || [ ! -r "$LOG" ]; then
    echo "error: logfile not found or unreadable: $LOG" >&2; usage; exit 2
fi

# [2/8] section = header excluded, up to (not including) the next "[N/8]" header.
SECTION="$(awk '
    /^\[[0-9]+\/8\]/ { if (seen) exit; if (substr($0,1,5) == "[2/8]") seen = 1; next }
    seen { print }
' "$LOG")"
if grep -q '^\[2/8\]' "$LOG"; then HAVE_SECTION=1; else HAVE_SECTION=0; fi
WARN_LINES="$(printf '%s\n' "$SECTION" | grep 'WARNING.*Docker' || true)"
WARN_COUNT="$(printf '%s\n' "$WARN_LINES" | grep -c 'WARNING.*Docker' || true)"
printf '%s\n' "$SECTION" | grep -qxF '      ok.' && OK_SEEN=1 || OK_SEEN=0
NAMED_REASON_LINES="$(printf '%s\n' "$SECTION" | grep -E 'reason: (lib_missing|daemon_down|permission_denied|other)([^A-Za-z0-9_]|$)' || true)"
NAMED_REASON_COUNT="$(printf '%s\n' "$NAMED_REASON_LINES" | grep -c . || true)"

fail() {
    echo "FAIL: $1" >&2
    if [ "$HAVE_SECTION" -eq 1 ]; then
        echo "----- [2/8] Docker section (verbatim) -----" >&2
        printf '%s\n' "$SECTION" >&2
        echo "------------------------------------------" >&2
    else
        echo "(no [2/8] Docker section found in $LOG)" >&2
    fi
    [ -z "$WARN_LINES" ] || { echo "matched Docker WARNING line(s):" >&2; printf '%s\n' "$WARN_LINES" | sed 's/^/    /' >&2; }
    exit 1
}

[ "$HAVE_SECTION" -eq 1 ] || fail "the [2/8] Docker section is missing - the lane never reached it"

if [ "$MODE" = "no_docker" ]; then
    [ "$OK_SEEN" -eq 0 ] || fail "the [2/8] section carries '      ok.' - this lane is Docker-less BY CONTRACT; if Docker is now usable, flip the lane to --expect-docker"
    [ "$NAMED_REASON_COUNT" -ge 1 ] || fail "no Docker WARNING in the [2/8] section names a specific reason (lib_missing|daemon_down|permission_denied|other) - nothing was proven about Docker"
    echo "PASS: [2/8] present, named Docker-absent reason, no usable outcome."
    exit 0
fi

[ "$WARN_COUNT" -eq 0 ] || fail "$WARN_COUNT Docker WARNING(s) although Docker is expected usable - vacuous pass"
[ "$OK_SEEN" -eq 1 ] || fail "no usable outcome ('      ok.') in the [2/8] section - nothing was verified"
echo "PASS: [2/8] present, usable outcome, no Docker WARNING."
exit 0
