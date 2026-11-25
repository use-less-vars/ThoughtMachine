#!/usr/bin/env bash
# forensics_after_reboot.sh -- read-only post-mortem report for the TM backend.
#
# Answers first, evidence second.  The default output leads with a one-line-per-
# question summary and then the witness ledger lines; the raw journal extracts
# are printed only with --full.  Every raw section is passed through the noise
# filter (see NOISE_DROP / NOISE_KEEP / denoise()) because this box's journal is
# saturated with audit and health-poll chatter.
#
# Safe to run after a machine restart: no sudo, no writes, every external
# command bounded by a timeout.  Nothing here modifies state.
#
# Usage:  bash scripts/forensics_after_reboot.sh [--quiet|-q] [--full]
#   (no argument)        compact: header, boots, summary, ledger lines
#   --quiet, -q          synonym for the compact default (kept for compatibility)
#   --full               the compact report PLUS the raw journal extracts
#   --filter             no report: copy stdin through the noise filter (test hook)
# Override the ledger with TM_WITNESS_LEDGER=/path/to/ledger.jsonl
# Exit: 0 report printed, 2 usage error, 3 witness ledger missing (the report
#       is STILL printed in full: see the ledger-MISSING note below).
# Skip every journalctl call with TM_FORENSICS_NO_JOURNAL=1 (used by the tests)

set -uo pipefail

# --------------------------------------------------------------------------- #
# noise filter -- the ONLY place drop/keep patterns live
# --------------------------------------------------------------------------- #
# Lines matching NOISE_DROP are discarded from every raw journal/kernel section.
# NOISE_KEEP wins over NOISE_DROP: a shutdown milestone is never noise.
NOISE_DROP='kauditd_printk_skb|audit: type=|GET /api/|workers/active|systemd\[1\]: (Stopped|Starting|Listening on)'
NOISE_KEEP='Reached target|Sending SIGTERM'

# The patterns reach awk through the ENVIRONMENT, never through `awk -v`: -v
# performs escape processing, so `systemd\[1\]` would arrive as the bracket
# expression `systemd[1]` (matching a bare "1"), the lines it is meant to drop
# would be printed instead, and awk would warn "escape sequence '\[' treated
# as plain '['".  ENVIRON is handed over verbatim, so the regexes above keep
# their single backslashes -- do NOT "tidy" them into -v.
denoise() {
    NOISE_KEEP="${NOISE_KEEP}" NOISE_DROP="${NOISE_DROP}" awk '
        BEGIN { keep = ENVIRON["NOISE_KEEP"]; drop = ENVIRON["NOISE_DROP"] }
        $0 ~ keep { print; next }
        $0 ~ drop { next }
        { print }
    '
}

FULL=0
for arg in "$@"; do
    case "${arg}" in
        -q|--quiet) ;;                  # synonym for the compact default
        --full|-v|--verbose) FULL=1 ;;
        --filter) denoise; exit 0 ;;
        -h|--help)
            cat <<'EOF'
usage: bash scripts/forensics_after_reboot.sh [--quiet|-q] [--full]

  (no argument)  compact: header, boots table, summary, witness ledger lines
                 (verdicts / exits / births, last 5 each)
  --quiet, -q    synonym for the compact default (kept for compatibility)
  --full         the compact report plus the raw journal extracts
  --filter       copy stdin through the noise filter and exit (test hook)

env:
  TM_WITNESS_LEDGER=/path/to/ledger.jsonl   override the witness ledger
  TM_FORENSICS_NO_JOURNAL=1                 skip every journalctl call (tests)
EOF
            exit 0 ;;
        *)
            printf 'forensics_after_reboot.sh: unknown argument %s\n' "${arg}" >&2
            exit 2 ;;
    esac
done

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
LEDGER=${TM_WITNESS_LEDGER:-${REPO_ROOT}/logs/witness/ledger.jsonl}
BUDGET=30   # seconds per external command

section() { printf '\n----- %s -----\n' "$1"; }

# Is journald usable at all?  Probed once, so every summary line can say "n/a"
# instead of lying with a 0 when there is no journal to read.
JOURNAL_OK=0
JOURNAL_NOTE='journalctl unavailable'
if [ "${TM_FORENSICS_NO_JOURNAL:-0}" = "1" ]; then
    JOURNAL_NOTE='skipped (TM_FORENSICS_NO_JOURNAL=1)'
elif timeout --foreground "${BUDGET}" journalctl --list-boots --no-pager >/dev/null 2>&1; then
    JOURNAL_OK=1
fi

# count of non-blank stdout lines of an arbitrary command string; "n/a" when the
# journal cannot be read, so a missing journal is never reported as "0 kills".
journal_count() {
    local out
    if [ "${JOURNAL_OK}" -ne 1 ]; then
        printf '%s' "${JOURNAL_NOTE}"
        return 0
    fi
    out=$(timeout --foreground "${BUDGET}" bash -c "$1" 2>/dev/null) || out=""
    printf '%s\n' "${out}" | grep -c '[^[:space:]]' || true
}

# yes/no plus the single quoted line that decides it
orderly_answer() {
    local out
    if [ "${JOURNAL_OK}" -ne 1 ]; then
        printf '%s' "${JOURNAL_NOTE}"
        return 0
    fi
    out=$(timeout --foreground "${BUDGET}" bash -c \
        "journalctl -b -1 --no-pager -g 'Reached target' 2>/dev/null | grep -E 'Reached target.*(Power-Off|Reboot)' | tail -1") || out=""
    if [ -n "${out}" ]; then
        printf 'yes -- %s' "${out}"
    else
        printf 'no -- no "Reached target (Power-Off|Reboot)" line in boot -1'
    fi
}

# "<N> -- reboot (x1) hard_kill_no_signal (x2)"
verdict_answer() {
    if [ ! -f "${LEDGER}" ]; then
        printf 'ledger MISSING (%s)' "${LEDGER}"
        return 0
    fi
    local n vals
    n=$(grep -c -F '"verdict"' "${LEDGER}" 2>/dev/null); n=${n:-0}
    vals=$(grep -oE '"verdict"[[:space:]]*:[[:space:]]*"[^"]*"' "${LEDGER}" 2>/dev/null \
        | sed -E 's/.*"([^"]*)"$/\1/' | sort | uniq -c | awk '{printf "%s (x%d) ", $2, $1}')
    vals=${vals% }
    if [ -n "${vals}" ]; then
        printf '%s -- %s' "${n}" "${vals}"
    else
        printf '%s' "${n}"
    fi
}

# raw journal section: bounded, de-noised, labelled
run_filtered() {
    local label=$1 cmd=$2 out
    section "${label}"
    if [ "${JOURNAL_OK}" -ne 1 ]; then
        printf '%s\n' "${JOURNAL_NOTE}"
        return 0
    fi
    out=$(timeout --foreground "${BUDGET}" bash -c "${cmd}" 2>&1 | denoise)
    if [ -n "${out}" ]; then
        printf '%s\n' "${out}"
    else
        printf '(nothing left after the noise filter)\n'
    fi
}

# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
echo "=== TM WITNESS FORENSICS (read-only) ==="
printf 'generated : %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'repo      : %s\n' "${REPO_ROOT}"
printf 'ledger    : %s\n' "${LEDGER}"
printf 'boot_id   : %s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
printf 'uptime_s  : %s\n' "$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo unknown)"

run_filtered "boots (last 3)" 'journalctl --list-boots --no-pager | tail -3'

# --------------------------------------------------------------------------- #
# ANSWERS FIRST -- one line per question, each a count or one quoted line
# --------------------------------------------------------------------------- #
section "summary"
printf 'prior boot ended orderly?: %s\n' "$(orderly_answer)"
printf 'kernel oom kills in prior boot: %s\n' "$(journal_count \
    "journalctl -b -1 -k --no-pager -g 'Out of memory: Killed process|oom-kill:'")"
printf 'cgroup memcg kills in prior boot: %s\n' "$(journal_count \
    "journalctl -b -1 -k --no-pager -g 'Memory cgroup out of memory'")"
printf 'segfaults/traps in prior boot: %s\n' "$(journal_count \
    "journalctl -b -1 -k --no-pager -g 'segfault at|traps:'")"
printf 'earlyoom kills in prior boot: %s\n' "$(journal_count \
    "journalctl -b -1 --no-pager -g earlyoom | grep -E 'sending SIGTERM|sending SIGKILL|low memory'")"
printf 'systemd-oomd kills in prior boot: %s\n' "$(journal_count \
    "journalctl -b -1 --no-pager -g 'systemd-oomd' | grep -E 'Killed /|due to memory pressure'")"
printf 'witness verdicts (from ledger): %s\n' "$(verdict_answer)"

# --------------------------------------------------------------------------- #
# witness ledger (evidence)
#
# A missing ledger is an ERROR (exit 3), but it is NOT a reason to stop working.
# The remaining sections -- above all the --full raw journal extracts -- have
# nothing to do with the ledger, and an early exit here would destroy evidence
# of one kind because evidence of another kind is absent.  So the condition is
# remembered in a flag, announced together with the path that was looked for
# (a reader must never mistake "no ledger" for "nothing happened"), and turned
# into an exit code only after every other section has been printed.
# --------------------------------------------------------------------------- #
LEDGER_MISSING=0
if [ ! -f "${LEDGER}" ]; then
    LEDGER_MISSING=1
    section "witness ledger MISSING: ${LEDGER}"
    printf 'cause: no regular file at that path -- the witness never wrote it,\n'
    printf '       or it was removed/moved before this report ran\n'
    printf 'looked for: %s\n' "${LEDGER}"
    printf 'override  : TM_WITNESS_LEDGER=/path/to/ledger.jsonl\n'
    printf 'no witness evidence exists for this boot sequence.  That says nothing\n'
    printf 'about the journal: the sections below are still produced in full.\n'
fi

if [ "${LEDGER_MISSING}" -eq 0 ]; then
    section "witness ledger: verdicts (last 5)"
    grep -F '"verdict"' "${LEDGER}" | tail -n 5 || printf '(no verdict records)\n'

    section "witness ledger: exits (last 5)"
    grep -F '"event":"exit"' "${LEDGER}" | tail -n 5 || printf '(no exit records)\n'

    section "witness ledger: births (last 5)"
    grep -F '"event":"birth"' "${LEDGER}" | tail -n 5 || printf '(no birth records)\n'
fi

# --------------------------------------------------------------------------- #
# raw evidence -- only with --full
# --------------------------------------------------------------------------- #
if [ "${FULL}" -eq 1 ]; then
    run_filtered "previous boot: kernel -p err (priority=err, last 200, noise-filtered)" \
        'journalctl -b -1 -k -p err --no-pager | tail -200'

    run_filtered "previous boot: oom / memcg / segfault / traps (last 100, noise-filtered)" \
        "journalctl -b -1 -k --no-pager -g 'oom|killed process|segfault|traps|Out of memory|Memory cgroup' | tail -100"

    run_filtered "previous boot: earlyoom + systemd-oomd (last 100, noise-filtered)" \
        "journalctl -b -1 --no-pager -g 'earlyoom|systemd-oomd' | tail -100"

    run_filtered "previous boot: shutdown timeline (last 60, noise-filtered)" \
        "journalctl -b -1 --no-pager -g 'systemd-shutdown|Sending SIGTERM|Stopped target' | tail -60"

    run_filtered "previous boot: orderly shutdown target reached? (last 5, noise-filtered)" \
        "journalctl -b -1 --no-pager -g 'Reached target' | grep -E 'Reached target.*(Power-Off|Reboot)' | tail -5"

    section "witness ledger: last 20 records"
    if [ "${LEDGER_MISSING}" -eq 0 ]; then
        tail -n 20 "${LEDGER}"
    else
        printf 'ledger MISSING (%s): no records to show\n' "${LEDGER}"
    fi

    section "witness ledger: files"
    if [ "${LEDGER_MISSING}" -eq 0 ]; then
        ls -l "${LEDGER}" 2>/dev/null || true
        ls -l "${LEDGER%.jsonl}".*.jsonl 2>/dev/null || printf '(no rotated archives)\n'
    else
        printf 'ledger MISSING (%s): no file, so no rotated archives either\n' "${LEDGER}"
    fi
fi

# The exit code is decided HERE, after all evidence of every kind is printed.
if [ "${LEDGER_MISSING}" -eq 1 ]; then
    exit 3
fi

exit 0
