#!/bin/bash
#===============================================================================
# kill_thoughtmachine.sh
#
#  ⚠ SYNCED with kill_thoughtmachine.bat — keep in agreement.
#  ⚠ If you edit this file, mirror the same change in the batch file.
#===============================================================================
# Forcefully stop all ThoughtMachine processes
#
# Receipt first, kill second: the witness must not be the victim.  Before
# anything is signalled, every process listening on one of the ports below is
# named (pid + command line) and appended to logs/backend_death.log -- resolved
# from THIS script's own directory, never from $PWD:
#   kill_switch|<iso8601 local ts>|pids=<pid,pid>|cmds=<cmdline;cmdline>
# The kill itself stays absolute: every listener on these ports is killed.
# Exit codes: 0 = the ports were enumerated (any listener was signalled);
#             3 = no enumeration tool at all (neither fuser nor lsof) -- the
#                 script refuses, signals nothing, and says so in the receipt.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEATH_LOG="$SCRIPT_DIR/logs/backend_death.log"

# Ports to enumerate and kill.  HARD-CODED on purpose: this is the kill switch,
# and a variable here would let a caller narrow the port set -- the switch would
# then quietly do a SMALLER job while still printing its success message, which
# is exactly the lie this receipt exists to stop.
PORTS=(5173/tcp 5174/tcp 5175/tcp 5176/tcp 5177/tcp 8000/tcp)

# --- portable `ps` access ------------------------------------------------------
# DUPLICATED, not sourced: these scripts ship as standalone files next to the
# .bat twins, so a shared lib would be a new file to install on every platform.
# The two copies are kept byte-identical by a pin in tests/test_crash_receipt.py
# (PS_SHARED_HELPERS); edit one and that test fails until you edit the other.
#
# BSD/macOS ps and GNU ps do not agree on keyword spelling, and BSD answers an
# UNKNOWN keyword by printing its whole keyword table on stdout with a zero
# exit status -- which is how a forensic field once came to hold a keyword list
# instead of a command line.  Only `sess` (session id) and `command` (command
# line) are spelled the same way on both, while `sid` and `cmd` are GNU-only.
# So no spelling is trusted blindly: every value is probed and validated.
PS_KEYWORD_TABLE=""      # this platform's `ps` keyword dump (empty on GNU ps)
PS_KEYWORD_TABLE_READ=0

# Must be called directly (never inside "$( )"), so the cache survives the call.
ps_probe_keyword_table() {
    if [ "$PS_KEYWORD_TABLE_READ" = "1" ]; then
        return 0
    fi
    PS_KEYWORD_TABLE_READ=1
    # A deliberately bogus keyword: GNU ps fails and prints nothing, BSD answers
    # with its keyword table -- that difference is the probe.
    PS_KEYWORD_TABLE="$(ps -o tm_ps_probe_= -p "$$" 2>/dev/null | tr -d '\n' || true)"
    return 0
}

# ps_line <pid> <keyword>... -> the normalised line from the first keyword that
# answers a real value; a leaked keyword dump is rejected, never recorded.
ps_line() {
    local pid="$1" kw line
    shift
    for kw in "$@"; do
        line="$(ps -o "$kw=" -p "$pid" 2>/dev/null \
                | tr -s ' ' ' ' | sed 's/^ //;s/ $//' || true)"
        if [ -z "$line" ]; then
            continue
        fi
        if [ -n "$PS_KEYWORD_TABLE" ] && [ "$line" = "$PS_KEYWORD_TABLE" ]; then
            continue
        fi
        printf '%s' "$line"
        return 0
    done
    return 1
}

# --- portable listener enumeration --------------------------------------------
# `fuser` is psmisc, i.e. a Linux package; macOS does not have it at all and
# ships `lsof` instead.  So the ENUMERATION is what gets ported, and the kill
# no longer goes through any external tool: the pids resolved here are signalled
# by the shell itself (invariant: signalling must not depend on the tool that
# enumerated -- on macOS that dependency is what made this switch a no-op).
#
# Presence is probed ONCE per tool with `command -v`: it is a property of the
# machine, not of a port.  `command -v` also finds a shell FUNCTION, which is
# how the tests inject fakes -- a fake tool is a present tool.
HAVE_FUSER=0
HAVE_LSOF=0
command -v fuser >/dev/null 2>&1 && HAVE_FUSER=1
command -v lsof >/dev/null 2>&1 && HAVE_LSOF=1

# enumerate_port_pids <port> -> pids on stdout, one per line.
#   0 = the enumeration RAN; EMPTY stdout is a real answer: "I looked, there is
#       nothing here" (both tools exit non-zero when they match nothing, so the
#       exit status cannot be used for this -- the tool's existence is).
#   1 = NO tool could look.  That is NOT an empty result and must never be
#       reported as one.
# Per-call stderr is discarded: an ABSENT tool is detected up front by
# `command -v`, so suppressing it here cannot hide a missing dependency.
enumerate_port_pids() {
    # PORTS holds "<port>/tcp" specs.  fuser wants the spec ("5173/tcp"), lsof
    # wants the bare number ("tcp:5173"): passing one to the other matches
    # nothing, which would read as "no listener" -- the exact lie this guards.
    local spec="$1" port="${1%/tcp}" raw tok
    if [ "$HAVE_FUSER" = "1" ]; then
        raw="$(fuser "$spec" 2>/dev/null)"
    elif [ "$HAVE_LSOF" = "1" ]; then
        raw="$(lsof -ti "tcp:$port" 2>/dev/null)"
    else
        return 1
    fi
    # fuser prefixes its pid list with the port ("35507/tcp:  5800") and lsof -t
    # prefixes nothing, so only purely numeric tokens are pids.
    for tok in $raw; do
        case "$tok" in
            ''|*[!0-9]*) continue ;;
        esac
        printf '%s\n' "$tok"
    done
    return 0
}

# --- receipt: enumerate WITHOUT killing, then resolve each pid to its command line
KILL_PIDS=""
KILL_CMDS=""
ENUM_UNKNOWN_PORTS=""
# read once, up front: inside "$( )" the cached value would not survive
ps_probe_keyword_table
for _port in "${PORTS[@]}"; do
    if ! _port_pids="$(enumerate_port_pids "$_port")"; then
        ENUM_UNKNOWN_PORTS="$ENUM_UNKNOWN_PORTS ${_port%/tcp}"
        continue
    fi
    for _pid in $_port_pids; do
        case " $KILL_PIDS " in
            *" $_pid "*) continue ;;
        esac
        KILL_PIDS="$KILL_PIDS $_pid"
        _cmd="$(ps_line "$_pid" command || true)"
        if [ -z "$_cmd" ]; then _cmd="$_pid (gone)"; else _cmd="$_pid $_cmd"; fi
        KILL_CMDS="$KILL_CMDS;$_cmd"
    done
done
KILL_PIDS="${KILL_PIDS# }"
KILL_CMDS="${KILL_CMDS#;}"
KILL_PIDS_CSV="$(printf '%s' "$KILL_PIDS" | tr ' ' ',')"

mkdir -p "$SCRIPT_DIR/logs" 2>/dev/null
( umask 077; : >> "$DEATH_LOG" ) 2>/dev/null
chmod 600 "$DEATH_LOG" 2>/dev/null

# --- refusal: 'I could not look' is not 'there is nothing there' ----------------
# Signals NOTHING: a partial kill announced as success is the same lie this
# receipt exists to stop.  The record names what was visible, and says it was
# not signalled.
if [ -n "$ENUM_UNKNOWN_PORTS" ]; then
    printf 'kill_switch|%s|pids=%s|cmds=%s|enumerate=failed|ports=%s|killed=no|reason=neither fuser nor lsof is available\n' \
        "$(date +%Y-%m-%dT%H:%M:%S%z)" "$KILL_PIDS_CSV" "$KILL_CMDS" \
        "${ENUM_UNKNOWN_PORTS# }" >> "$DEATH_LOG"
    echo "Could not enumerate listening processes on port(s):${ENUM_UNKNOWN_PORTS} - neither 'fuser' nor 'lsof' is available on this machine, so this script cannot see or kill anything on them. Refusing to report success."
    echo "  Receipt: $DEATH_LOG (kill_switch enumerate=failed ports=${ENUM_UNKNOWN_PORTS# })"
    exit 3
fi

printf 'kill_switch|%s|pids=%s|cmds=%s\n' \
    "$(date +%Y-%m-%dT%H:%M:%S%z)" "$KILL_PIDS_CSV" "$KILL_CMDS" >> "$DEATH_LOG"

# --- kill: the pids this script itself enumerated -----------------------------
# The signal is the shell's own `kill`, not the tool that enumerated.
# Receipt first, kill second -- unchanged: the witness must not be the victim.
for _pid in $KILL_PIDS; do
    kill -9 "$_pid" 2>/dev/null
done
sleep 1

if [ -n "$KILL_PIDS" ]; then
    echo "All ThoughtMachine processes stopped."
    echo "  Receipt: $DEATH_LOG (kill_switch pids=$KILL_PIDS_CSV)"
else
    echo "No ThoughtMachine process was listening on ports: 5173 5174 5175 5176 5177 8000 - nothing to stop."
    echo "  Receipt: $DEATH_LOG (kill_switch pids=)"
fi
