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

# --- receipt: enumerate WITHOUT -k, then resolve each pid to its command line --
KILL_PIDS=""
KILL_CMDS=""
# read once, up front: inside "$( )" the cached value would not survive
ps_probe_keyword_table
for _port in "${PORTS[@]}"; do
    # No 2>/dev/null here on purpose: a failing enumeration must stay visible.
    for _pid in $(fuser "$_port"); do
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
printf 'kill_switch|%s|pids=%s|cmds=%s\n' \
    "$(date +%Y-%m-%dT%H:%M:%S%z)" "$KILL_PIDS_CSV" "$KILL_CMDS" >> "$DEATH_LOG"

# --- kill: absolute, exactly as before -----------------------------------------
fuser -k "${PORTS[@]}" 2>/dev/null
sleep 1

if [ -n "$KILL_PIDS" ]; then
    echo "All ThoughtMachine processes stopped."
    echo "  Receipt: $DEATH_LOG (kill_switch pids=$KILL_PIDS_CSV)"
else
    echo "No ThoughtMachine process was listening on ports: 5173 5174 5175 5176 5177 8000 - nothing to stop."
    echo "  Receipt: $DEATH_LOG (kill_switch pids=)"
fi
