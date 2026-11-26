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
# Every journal query is bounded TWICE, because an unbounded one is not slow --
# it is a hang:
#   * in TIME, with --since/--until around the last W hours of boot -1.  Boot -1
#     on this box ran for twelve days (~3.1 GB); a scan of it takes hours.
#   * in READ DIRECTION, with journalctl's own -n N, which reads the N newest
#     entries from the end and can stop early, instead of streaming a whole
#     forward scan into `tail -N` and discarding everything but the tail.
# The header always states the window that was used and the span of boot -1, and
# says so explicitly when the boot is longer than the window: "last 3 h" must
# never be misread as "the whole boot".
#
# Usage:  bash scripts/forensics_after_reboot.sh [--quiet|-q] [--full] [--hours N]
#   (no argument)        compact: header, boots, summary, ledger lines
#   --quiet, -q          synonym for the compact default (kept for compatibility)
#   --full               the compact report PLUS the raw journal extracts
#   --filter             no report: copy stdin through the noise filter (test hook)
#   --hours N            scan only the last N hours of boot -1 (default 3, and 6
#                        with --full; N must be a positive integer)
# Override the ledger with TM_WITNESS_LEDGER=/path/to/ledger.jsonl
# Override the window with TM_FORENSICS_WINDOW_HOURS=N (--hours N wins)
# Exit: 0 report printed, 2 usage error, 3 witness ledger missing (the report
#       is STILL printed in full: see the ledger-MISSING note below).
# Skip every journalctl call with TM_FORENSICS_NO_JOURNAL=1 (used by the tests)
# BOUND the per-command budget with TM_FORENSICS_BUDGET_S=N (used by the tests)
#
# Every number in the summary is either a count of what was actually read or the
# literal UNMEASURED (<reason>): a query that timed out, failed or could not run
# must never print 0, because "0 kills" and "could not look" are different
# statements.  The header and the summary both state how many answers were
# unmeasured.  Kill counts come from journalctl unit selectors (-t), never from
# a MESSAGE grep: `-g earlyoom` matches the word, not the process.

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
WINDOW_HOURS=''      # '' = not given on the command line; resolved just below
WINDOW_EXPLICIT=0    # 1 once --hours N was given

# A bad value is a usage error, exactly like an unknown argument: message on
# stderr, exit 2.
usage_error() {
    printf 'forensics_after_reboot.sh: %s\n' "$1" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    arg=$1
    case "${arg}" in
        -q|--quiet) shift ;;             # synonym for the compact default
        --full|-v|--verbose) FULL=1; shift ;;
        --filter) denoise; exit 0 ;;
        --hours)
            [ "$#" -ge 2 ] || usage_error '--hours needs a value (a number of hours)'
            WINDOW_HOURS=$2
            WINDOW_EXPLICIT=1
            shift 2 ;;
        --hours=*)
            WINDOW_HOURS=${arg#--hours=}
            WINDOW_EXPLICIT=1
            shift ;;
        -h|--help)
            cat <<'EOF'
usage: bash scripts/forensics_after_reboot.sh [--quiet|-q] [--full] [--hours N]

  (no argument)  compact: header, boots table, summary, witness ledger lines
                 (verdicts / exits / births, last 5 each)
  --quiet, -q    synonym for the compact default (kept for compatibility)
  --full         the compact report plus the raw journal extracts (default 6 h)
  --filter       copy stdin through the noise filter and exit (test hook)
  --hours N      scan only the last N hours of boot -1 (default 3 h)

  Every journal query is scoped to that window and reads backwards from its
  end; the header states the window and the span of boot -1.

env:
  TM_WITNESS_LEDGER=/path/to/ledger.jsonl   override the witness ledger
  TM_FORENSICS_NO_JOURNAL=1                 skip every journalctl call (tests)
  TM_FORENSICS_BUDGET_S=N                   seconds per external command (tests)
  TM_FORENSICS_WINDOW_HOURS=N               default window in hours (--hours wins)
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
# seconds per external command.  TM_FORENSICS_BUDGET_S is a TEST hook: the
# timing-out path is asserted against a real short budget, not a 30 s one.
BUDGET=${TM_FORENSICS_BUDGET_S:-30}
KILL_GRACE=5  # seconds between that SIGTERM and the follow-up SIGKILL
case "${BUDGET}" in
    ''|*[!0-9]*) usage_error "TM_FORENSICS_BUDGET_S must be a positive integer, got '${BUDGET}'" ;;
esac
if [ "${BUDGET}" -lt 1 ]; then
    usage_error "TM_FORENSICS_BUDGET_S must be a positive integer, got '${BUDGET}'"
fi

# --------------------------------------------------------------------------- #
# the window: which slice of boot -1 the queries may look at
#
# Precedence: --hours N, then TM_FORENSICS_WINDOW_HOURS, then 3 (6 with --full).
# A window is not a micro-optimisation here, it is what makes the report finite:
# boot -1 on this box ran 2026-11-13 14:20:56 .. 2026-11-25 02:52:15 (twelve
# days, ~3.1 GB), and an unbounded `journalctl -b -1` over that is hours.
# --------------------------------------------------------------------------- #
if [ "${WINDOW_EXPLICIT}" -eq 0 ]; then
    if [ -n "${TM_FORENSICS_WINDOW_HOURS:-}" ]; then
        WINDOW_HOURS=${TM_FORENSICS_WINDOW_HOURS}
    elif [ "${FULL}" -eq 1 ]; then
        WINDOW_HOURS=6
    else
        WINDOW_HOURS=3
    fi
fi
case "${WINDOW_HOURS}" in
    ''|*[!0-9]*) usage_error "window hours must be a positive integer, got '${WINDOW_HOURS}'" ;;
esac
if [ "${WINDOW_HOURS}" -lt 1 ]; then
    usage_error "window hours must be a positive integer, got '${WINDOW_HOURS}'"
fi

section() { printf '\n----- %s -----\n' "$1"; }

# Is journald usable at all?  Probed once, so every summary line can say "n/a"
# instead of lying with a 0 when there is no journal to read.
JOURNAL_OK=0
JOURNAL_NOTE='journalctl unavailable'
if [ "${TM_FORENSICS_NO_JOURNAL:-0}" = "1" ]; then
    JOURNAL_NOTE='skipped (TM_FORENSICS_NO_JOURNAL=1)'
elif timeout -k "${KILL_GRACE}" "${BUDGET}" journalctl --list-boots --no-pager >/dev/null 2>&1; then
    JOURNAL_OK=1
fi

# --------------------------------------------------------------------------- #
# where does boot -1 end?  --since/--until need an absolute instant, and the
# cheapest source of one is `journalctl --list-boots` -- a boot INDEX, not a
# scan of the journal.  Its -o short-iso rows are
#   IDX  BOOTID  Ddd YYYY-MM-DD HH:MM:SS TZ  Ddd YYYY-MM-DD HH:MM:SS TZ
# so the previous boot's first entry is $4 " " $5 and its last entry is
# $8 " " $9.  date(1) parses both in the local zone, which is exactly how
# journalctl reads a naive --since/--until, so the two agree to the second.
#
# --until is EXCLUSIVE of the instant it names: a bound of 02:52:15 hides every
# entry stamped 02:52:15.500, and on this box that is the entire orderly
# shutdown (systemd only reaches reboot.target in that final second).  The bound
# passed is therefore end + 1s, while the header keeps reporting the real end.
# --------------------------------------------------------------------------- #
BOOT_START=''       # first entry of boot -1, '' when unknown
BOOT_END=''         # last entry of boot -1, '' when unknown
BOOT_SPAN_S=''      # boot -1 duration in seconds, '' when unknown
WINDOW_SINCE=''     # absolute --since instant
WINDOW_UNTIL=''     # absolute --until instant, '' only when even the end is unknown
TIME_BOUND=''       # the --since/--until fragment spliced into every journal query
BOOT_SPAN_TEXT='unknown (no boot index readable)'
WINDOW_TEXT=''
COVERAGE_WARNING=''
SUMMARY_SCOPE="prior boot (-1), last ${WINDOW_HOURS} h (boot span unknown)"

# "12d 12h" / "5h 3m" / "7m" -- the boot span, for a human reading the header
span_human() {
    local s=$1 d h m
    d=$((s / 86400))
    h=$(((s % 86400) / 3600))
    m=$(((s % 3600) / 60))
    if [ "${d}" -gt 0 ]; then
        printf '%sd %sh' "${d}" "${h}"
    elif [ "${h}" -gt 0 ]; then
        printf '%sh %sm' "${h}" "${m}"
    else
        printf '%sm' "${m}"
    fi
}

detect_window() {
    local boots line start_s end_s

    if [ "${JOURNAL_OK}" -ne 1 ]; then
        WINDOW_TEXT="not read: ${JOURNAL_NOTE}"
        SUMMARY_SCOPE="prior boot (-1), not read: ${JOURNAL_NOTE}"
        return 0
    fi

    boots=$(timeout -k "${KILL_GRACE}" "${BUDGET}" \
        journalctl --list-boots --no-pager -o short-iso 2>/dev/null) || boots=''
    line=$(printf '%s\n' "${boots}" | awk '$1 == "-1" { print; exit }')
    if [ -n "${line}" ]; then
        BOOT_START=$(printf '%s\n' "${line}" | awk '{ print $4 " " $5 }')
        BOOT_END=$(printf '%s\n' "${line}" | awk '{ print $8 " " $9 }')
    fi
    if [ -z "${BOOT_END}" ]; then
        # Fallback: the newest entry of boot -1.  `-n 1` reads from the END, so
        # this stays cheap even on a twelve-day boot.
        BOOT_END=$(timeout -k "${KILL_GRACE}" "${BUDGET}" \
            journalctl -b -1 -n 1 --no-pager -o short-iso 2>/dev/null \
            | head -1 | awk '{ print $1 }') || BOOT_END=''
    fi

    end_s=''
    if [ -n "${BOOT_END}" ]; then
        end_s=$(date -d "${BOOT_END}" +%s 2>/dev/null) || end_s=''
    fi
    if [ -n "${end_s}" ]; then
        WINDOW_UNTIL=$(date -d "@$((end_s + 1))" '+%Y-%m-%d %H:%M:%S')
        WINDOW_SINCE=$(date -d "@$((end_s - WINDOW_HOURS * 3600))" '+%Y-%m-%d %H:%M:%S')
    else
        # Last resort: a relative, NOW-anchored bound.  It may match nothing at
        # all (boot -1 is in the past), but it still terminates -- which an
        # unbounded scan does not.
        WINDOW_SINCE="-${WINDOW_HOURS} hours"
    fi

    TIME_BOUND="--since \"${WINDOW_SINCE}\""
    if [ -n "${WINDOW_UNTIL}" ]; then
        TIME_BOUND="${TIME_BOUND} --until \"${WINDOW_UNTIL}\""
    fi

    if [ -n "${BOOT_START}" ] && [ -n "${end_s}" ]; then
        start_s=$(date -d "${BOOT_START}" +%s 2>/dev/null) || start_s=''
        if [ -n "${start_s}" ] && [ "${end_s}" -ge "${start_s}" ]; then
            BOOT_SPAN_S=$((end_s - start_s))
        fi
    fi

    if [ -n "${BOOT_START}" ] && [ -n "${BOOT_END}" ]; then
        BOOT_SPAN_TEXT="${BOOT_START} .. ${BOOT_END}"
        if [ -n "${BOOT_SPAN_S}" ]; then
            BOOT_SPAN_TEXT="${BOOT_SPAN_TEXT} (span $(span_human "${BOOT_SPAN_S}"))"
        fi
    elif [ -n "${BOOT_END}" ]; then
        BOOT_SPAN_TEXT="? .. ${BOOT_END} (first entry unknown)"
    fi

    if [ -n "${WINDOW_UNTIL}" ]; then
        WINDOW_TEXT="last ${WINDOW_HOURS} h of boot -1: ${WINDOW_SINCE} .. ${BOOT_END}"
    else
        WINDOW_TEXT="last ${WINDOW_HOURS} h up to now (boot -1 end unknown): since ${WINDOW_SINCE}"
    fi

    if [ -n "${BOOT_SPAN_S}" ]; then
        if [ "${BOOT_SPAN_S}" -gt "$((WINDOW_HOURS * 3600))" ]; then
            COVERAGE_WARNING="boot -1 lasted $(span_human "${BOOT_SPAN_S}") but only its last ${WINDOW_HOURS} h were scanned -- this report is NOT the whole boot"
            SUMMARY_SCOPE="prior boot (-1), last ${WINDOW_HOURS} h only (the boot lasted $(span_human "${BOOT_SPAN_S}")) -- earlier evidence is NOT in this report"
        else
            SUMMARY_SCOPE="prior boot (-1), last ${WINDOW_HOURS} h -- covers the whole boot"
        fi
    fi
}

detect_window

# One bounded journalctl query, counted.
#
#   journal_count <command string> [<message regex>]
#
# A count is an ANSWER, so a query that did not run must never produce one: a
# timeout, a failure and an absent journalctl all print UNMEASURED (<reason>).
# But "no entries" is the OTHER way round: journalctl exits 1 when nothing
# matched and announces it on stderr, and that is a measured 0 -- reporting it
# as UNMEASURED would make "measurably none" look like "could not look".
# The query keeps its --since/--until window and its `timeout -k` bound; the
# optional regex is applied here, with the pattern handed to awk through the
# ENVIRONMENT (see denoise()) so no backslash is ever eaten.

# The first non-empty line of a capture, unindented: the reason string for a
# query that really failed.  "" when the capture was empty.
first_line() {
    printf '%s\n' "$1" | sed -n 's/^[[:space:]]*//; /^$/d; p' | head -n 1
}

# Did journalctl say it found nothing?  Exit 1 is returned for that AND for a
# real failure; only the message tells them apart.
said_no_entries() {
    printf '%s\n' "$1" | grep -q 'No entries'
}

journal_count() {
    local cmd=$1 pattern=${2:-} out rc reason
    if [ "${JOURNAL_OK}" -ne 1 ]; then
        printf 'UNMEASURED (%s)' "${JOURNAL_NOTE}"
        return 0
    fi
    # stdout AND stderr in ONE variable: `-- No entries --` arrives on stderr,
    # so a stdout-only capture cannot tell an empty result from a broken read.
    # Nothing is written to disk -- no temp file, least of all in the repo.
    out=$(timeout -k "${KILL_GRACE}" "${BUDGET}" bash -c "${cmd}" 2>&1)
    rc=$?
    case "${rc}" in
        0) ;;
        1)
            if said_no_entries "${out}"; then
                printf '0'
                return 0
            fi
            reason=$(first_line "${out}")
            printf 'UNMEASURED (journalctl failed: exit 1: "%s")' "${reason:-no stderr}"
            return 0 ;;
        124|137)
            printf 'UNMEASURED (journalctl timed out after %s s)' "${BUDGET}"
            return 0 ;;
        126|127)
            printf 'UNMEASURED (journalctl could not be executed: exit %s)' "${rc}"
            return 0 ;;
        *)
            reason=$(first_line "${out}")
            printf 'UNMEASURED (journalctl failed: exit %s: "%s")' "${rc}" "${reason:-no stderr}"
            return 0 ;;
    esac
    # journalctl's own `-- ` markers ("-- No entries --", "-- Logs begin at")
    # are not journal entries: an entry line starts with a timestamp, never
    # with `-- `, so a marker can never be counted as a kill.
    printf '%s\n' "${out}" | COUNT_PATTERN="${pattern}" awk '
        BEGIN { pat = ENVIRON["COUNT_PATTERN"] }
        /^[[:space:]]*$/ { next }
        /^-- / { next }
        pat != "" && $0 !~ pat { next }
        { n++ }
        END { print n + 0 }
    '
}

# yes/no plus the single quoted line that decides it
orderly_answer() {
    local out rc reason
    if [ "${JOURNAL_OK}" -ne 1 ]; then
        printf 'UNMEASURED (%s)' "${JOURNAL_NOTE}"
        return 0
    fi
    # -n 1: the newest matching entry, read from the end of the window.  The
    # grep moves into -g (PCRE2) so journalctl can stop as soon as it has one.
    out=$(timeout -k "${KILL_GRACE}" "${BUDGET}" bash -c \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -g 'Reached target.*(Power-Off|Reboot)' -n 1 2>&1")
    rc=$?
    case "${rc}" in
        0) ;;
        1)
            # "no entries" here MEANS something: no shutdown marker was found in
            # boot -1.  That is an answer about the boot, not a failed read.
            if said_no_entries "${out}"; then
                printf 'no -- no shutdown target entry (Reached target Power-Off|Reboot) in the window'
                return 0
            fi
            reason=$(first_line "${out}")
            printf 'UNMEASURED (journalctl failed: exit 1: "%s")' "${reason:-no stderr}"
            return 0 ;;
        124|137)
            printf 'UNMEASURED (journalctl timed out after %s s)' "${BUDGET}"
            return 0 ;;
        *)
            # "unreadable" is not "orderly": say which one happened.
            reason=$(first_line "${out}")
            printf 'UNMEASURED (journalctl failed: exit %s: "%s")' "${rc}" "${reason:-no stderr}"
            return 0 ;;
    esac
    out=$(printf '%s\n' "${out}" | grep -v '^-- ' || true)
    if [ -n "${out}" ]; then
        printf 'yes -- %s' "${out}"
    else
        printf 'no -- journalctl matched nothing for boot -1'
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

# --------------------------------------------------------------------------- #
# signals: WHO signalled WHOM, from the witness ledger
#
# A signal record -- "kind" (or "event") == "signal" -- is the only evidence in
# the ledger that names the SENDER of a kill, so it is reported per event and
# not merely counted.  The witness NAMES THE SENDER, NOT THE VICTIM: the fields
# its records actually carry are si_pid, si_uid, sender_comm, signame (with
# signo), origin and run_id -- there is no target_pid and no victim pid of any
# kind.  The section is therefore titled "(sender -> run)", and a line says
# "target=run <run_id>" instead of promising a pid the data cannot supply.
# The count and the lines come from the SAME single scan, so "signals: N" can
# never disagree with the lines printed underneath it.  A ledger that is absent
# or not JSONL is UNMEASURED, never 0.
# --------------------------------------------------------------------------- #
SIG_COUNT=''    # "N", or UNMEASURED (<reason>)
SIG_EVENTS=''   # formatted per-event lines, '' when there are none

sig_load() {
    local scan count records json
    if [ ! -f "${LEDGER}" ]; then
        SIG_COUNT="UNMEASURED (ledger MISSING: ${LEDGER})"
        SIG_EVENTS=''
        return 0
    fi
    if ! scan=$(awk '
        BEGIN { q = sprintf("%c", 34) }
        function f(line, key,   pat, rest, c) {
            # No optional opening quote: consuming it would put the quoted
            # branch below out of reach, and the bare-token scan then stops at
            # '+' -- truncating "...T02:51:58.123456+00:00" at the offset.
            pat = q key q "[[:space:]]*:[[:space:]]*"
            if (!match(line, pat)) return ""
            rest = substr(line, RSTART + RLENGTH)
            if (substr(rest, 1, 1) == q) {
                rest = substr(rest, 2)
                c = index(rest, q)
                return c ? substr(rest, 1, c - 1) : ""
            }
            c = 0
            while (substr(rest, c + 1, 1) ~ /[-0-9A-Za-z_.:]/) c++
            return substr(rest, 1, c)
        }
        # f() returns the raw token: a JSON null is spelled "null" and must not
        # be printed as if it were a value.
        function val(line, key,   v) { v = f(line, key); return (v == "null") ? "" : v }
        tolower($0) ~ (q "(kind|event)" q "[[:space:]]*:[[:space:]]*" q "signal" q) {
            ts   = val($0, "ts");         if (ts   == "") ts   = val($0, "timestamp")
            sign = val($0, "signame");   if (sign == "") sign = val($0, "signo")
            spid = val($0, "si_pid");     if (spid == "") spid = val($0, "sender_pid")
            comm = val($0, "sender_comm")
            orig = val($0, "origin")
            # NO fallback to a bare `pid`: these records name the SENDER
            # (si_pid) and the witnessed run (run_id), and a `pid` field, where
            # one exists at all, is the sender pid -- falling back to it would
            # print the aggressor as the victim.  A target pid is printed only
            # when the record really carries one; otherwise the target is the
            # run named by run_id, and the line says so.
            tpid = val($0, "target_pid")
            run  = val($0, "run_id")
            if (tpid != "")   tgt = "target_pid=" tpid
            else if (run != "") tgt = "target=run " run
            else              tgt = "target not recorded"
            printf "%s  %s  sender=%s%s  origin=%s  ->  %s\n", (ts == "" ? "unknown" : ts), (sign == "" ? "signal=unknown" : "signal=" sign), (spid == "" ? "unknown" : spid), (comm == "" ? "" : " (" comm ")"), (orig == "" ? "unknown" : orig), tgt
            found++
            next
        }
        {
            records++
            if (index($0, "{") > 0) json++
        }
        END {
            printf "#meta count %d\n",   found + 0
            printf "#meta records %d\n", records + 0
            printf "#meta json %d\n",    json + 0
        }
    ' "${LEDGER}" 2>/dev/null); then
        SIG_COUNT="UNMEASURED (ledger unreadable: awk failed on ${LEDGER})"
        SIG_EVENTS=''
        return 0
    fi
    count=$(printf '%s\n' "${scan}" | sed -n 's/^#meta count //p')
    records=$(printf '%s\n' "${scan}" | sed -n 's/^#meta records //p')
    json=$(printf '%s\n' "${scan}" | sed -n 's/^#meta json //p')
    SIG_EVENTS=$(printf '%s\n' "${scan}" | grep -v '^#meta ' || true)
    if [ "${records:-0}" -gt 0 ] && [ "${json:-0}" -eq 0 ]; then
        SIG_COUNT="UNMEASURED (ledger is not JSONL: no '{' in ${records} record(s))"
        SIG_EVENTS=''
        return 0
    fi
    SIG_COUNT=${count:-0}
}

# --------------------------------------------------------------------------- #
# ANSWERS, computed once and BEFORE the header, so the header can state how many
# of them are UNMEASURED instead of the report only admitting it line by line.
# (A counter bumped inside journal_count would be lost: it runs inside a command
# substitution, which is a subshell.)
# --------------------------------------------------------------------------- #
JOURNAL_ANSWERS=6
A_ORDERLY=''; A_KERNEL_OOM=''; A_MEMCG=''; A_SEGFAULT=''; A_EARLYOOM=''; A_OOMD=''
A_VERDICTS=''
A_EARLYOOM_GREP=''   # CROSS-CHECK only, never an answer: see answers_load
UNMEASURED_ANSWERS=0

answers_load() {
    local tally
    A_ORDERLY=$(orderly_answer)
    A_KERNEL_OOM=$(journal_count \
        "journalctl ${TIME_BOUND} -b -1 -k --no-pager -g 'Out of memory: Killed process|oom-kill:'")
    A_MEMCG=$(journal_count \
        "journalctl ${TIME_BOUND} -b -1 -k --no-pager -g 'Memory cgroup out of memory'")
    A_SEGFAULT=$(journal_count \
        "journalctl ${TIME_BOUND} -b -1 -k --no-pager -g 'segfault at|traps:'")
    # -t selects the earlyoom UNIT.  `-g earlyoom` only greps the MESSAGE text: a
    # message that merely mentions the word then counts as a kill, and a kill
    # worded differently does not.  The unit selector is the count.
    A_EARLYOOM=$(journal_count \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -t earlyoom" \
        'sending SIGTERM|sending SIGKILL')
    A_OOMD=$(journal_count \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -t systemd-oomd" \
        'Killed /|due to memory pressure')
    # CROSS-CHECK, not an answer: whether `-t earlyoom` matches this systemd is
    # a claim no test here can settle.  If the unit query reports a measured 0
    # while the MESSAGE grep sees lines, the selector -- not the machine -- is
    # the suspect, and the summary says so.  The answer itself is NOT rewritten:
    # silently substituting one number for another is the bug this report exists
    # to prevent, so the contradiction is stated and the untrustworthy number is
    # named.  Same window, same budget, same UNMEASURED discipline.
    A_EARLYOOM_GREP=$(journal_count \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -g 'earlyoom'" \
        'sending SIGTERM|sending SIGKILL')
    A_VERDICTS=$(verdict_answer)
    tally=$(printf '%s\n' "${A_ORDERLY}" "${A_KERNEL_OOM}" "${A_MEMCG}" \
        "${A_SEGFAULT}" "${A_EARLYOOM}" "${A_OOMD}" | grep -c '^UNMEASURED')
    UNMEASURED_ANSWERS=${tally:-0}
}

sig_load
answers_load

# raw journal section: bounded, de-noised, labelled
run_filtered() {
    local label=$1 cmd=$2 out
    section "${label}"
    if [ "${JOURNAL_OK}" -ne 1 ]; then
        printf '%s\n' "${JOURNAL_NOTE}"
        return 0
    fi
    # `timeout -k` WITHOUT --foreground.  --foreground signals only the direct
    # child, so journalctl -- a grandchild, through bash -c -- survives the
    # budget, keeps the pipe's write end open, and leaves denoise (and therefore
    # this command substitution) waiting for an EOF that never comes.  Without
    # --foreground, timeout puts the child in its own process group and signals
    # the WHOLE pipeline; -k 5 turns a wedged SIGTERM into a SIGKILL.
    out=$(timeout -k "${KILL_GRACE}" "${BUDGET}" bash -c "${cmd}" 2>&1 | denoise)
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
printf 'boot -1   : %s\n' "${BOOT_SPAN_TEXT}"
printf 'window    : %s\n' "${WINDOW_TEXT}"
printf 'unmeasured: %s of %s journal answers could not be measured (see summary)\n' \
    "${UNMEASURED_ANSWERS}" "${JOURNAL_ANSWERS}"
if [ -n "${COVERAGE_WARNING}" ]; then
    printf 'WARNING   : %s\n' "${COVERAGE_WARNING}"
fi

# --list-boots reads the boot INDEX, not the journal: four lines here, and it
# is the call that says where boot -1 ends.  It needs no time window (there is
# nothing to scan) and is bounded by the same timeout as everything else.
run_filtered "boots (last 3)" 'journalctl --list-boots --no-pager | tail -3'

# --------------------------------------------------------------------------- #
# ANSWERS FIRST -- one line per question, each a count or one quoted line
# --------------------------------------------------------------------------- #
section "summary"
printf 'scope     : %s\n' "${SUMMARY_SCOPE}"
printf 'unmeasured: %s of %s journal answers could not be measured (marked UNMEASURED below)\n' \
    "${UNMEASURED_ANSWERS}" "${JOURNAL_ANSWERS}"
printf 'prior boot ended orderly?: %s\n' "${A_ORDERLY}"
printf 'kernel oom kills in prior boot: %s\n' "${A_KERNEL_OOM}"
printf 'cgroup memcg kills in prior boot: %s\n' "${A_MEMCG}"
printf 'segfaults/traps in prior boot: %s\n' "${A_SEGFAULT}"
printf 'earlyoom kills in prior boot: %s\n' "${A_EARLYOOM}"
if [ "${A_EARLYOOM}" = '0' ]; then
    case "${A_EARLYOOM_GREP}" in
        ''|*[!0-9]*) ;;   # no clean cross-check number: nothing to contradict
        *)
            if [ "${A_EARLYOOM_GREP}" -gt 0 ]; then
                printf 'WARNING   : earlyoom cross-check: the unit query said 0 but %s journal lines mention earlyoom -- the -t selector may not match this systemd; treat the kill count as UNMEASURED\n' "${A_EARLYOOM_GREP}"
            fi ;;
    esac
fi
printf 'systemd-oomd kills in prior boot: %s\n' "${A_OOMD}"
printf 'witness verdicts (from ledger): %s\n' "${A_VERDICTS}"
printf 'signals: %s\n' "${SIG_COUNT}"

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

# Unconditional: when the ledger is missing or unreadable this section still
# prints, saying UNMEASURED and why.  An absent file is not "no signals".
section "witness ledger: signals (sender -> run)"
if [ -n "${SIG_EVENTS}" ]; then
    printf '%s\n' "${SIG_EVENTS}"
else
    printf '(no signal records)\n'
fi
printf 'signals: %s\n' "${SIG_COUNT}"

# --------------------------------------------------------------------------- #
# raw evidence -- only with --full
# --------------------------------------------------------------------------- #
if [ "${FULL}" -eq 1 ]; then
    run_filtered "previous boot: kernel -p err (priority=err, last 200, noise-filtered)" \
        "journalctl ${TIME_BOUND} -b -1 -k -p err --no-pager -n 200"

    run_filtered "previous boot: oom / memcg / segfault / traps (last 100, noise-filtered)" \
        "journalctl ${TIME_BOUND} -b -1 -k --no-pager -g 'oom|killed process|segfault|traps|Out of memory|Memory cgroup' -n 100 | tac"

    run_filtered "previous boot: earlyoom + systemd-oomd (last 100, noise-filtered)" \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -g 'earlyoom|systemd-oomd' -n 100 | tac"

    run_filtered "previous boot: shutdown timeline (last 60, noise-filtered)" \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -g 'systemd-shutdown|Sending SIGTERM|Stopped target' -n 60 | tac"

    run_filtered "previous boot: orderly shutdown target reached? (last 5, noise-filtered)" \
        "journalctl ${TIME_BOUND} -b -1 --no-pager -g 'Reached target.*(Power-Off|Reboot)' -n 5 | tac"

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
