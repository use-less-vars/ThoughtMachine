"""Contract tests for scripts/forensics_after_reboot.sh.

The script answers first and shows evidence second: the compact report must lead
with the witness-ledger verdict, the raw journal extracts may only be printed
with --full, and every raw line must survive the noise filter (this box's
journal is saturated with audit and health-poll chatter).

Journal access is disabled for every test (TM_FORENSICS_NO_JOURNAL=1) so the
tests are hermetic: they never read the machine's journal.  The noise filter
itself is exercised through the --filter mode, which pipes stdin through the
exact same denoise() the raw sections use.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "forensics_after_reboot.sh"

BIRTH = {
    "event": "birth",
    "ts": "2026-11-25T00:21:16.500000+00:00",
    "run_id": "run-1",
    "boot_id": "boot-0",
    "pid": 4242,
    "ppid": 4200,
    "sid": 4242,
    "pgid": 4242,
    "argv": ["python", "-m", "thoughtmachine.backend"],
    "cwd": "/home/jojo/PycharmProjects/ThoughtMachine-dev",
    "git_head": None,
    "python": "3.12.3",
    "uptime_s": 1000.0,
}

VERDICT = {
    "event": "verdict",
    "run_id": "run-2",
    "prior_run_id": "run-1",
    "verdict": "reboot",
    "evidence": None,
}

# Synthetic journal text: one dropped line per NOISE_DROP alternative plus the
# shutdown/oom lines that NOISE_KEEP must preserve.
NOISY = """\
audit: type=1400 audit(1700000000.000:42): apparmor="DENIED" operation="open"
kauditd_printk_skb: 12 callbacks suppressed
systemd[1]: Starting Daily apt upgrade and clean activities...
systemd[1]: Listening on Docker Socket for the API.
systemd[1]: Stopped target Multi-User System.
10.0.0.5 - - "GET /api/health HTTP/1.1" 200 -
10.0.0.5 - - "GET /api/workspace/x/workers/active HTTP/1.1" 200 -
Reached target Reboot.
Sending SIGTERM to remaining processes...
kernel: Out of memory: Killed process 4242 (firefox) total-vm:1000kB
earlyoom[900]: sending SIGTERM to process 4242.
"""

NOISE_ONLY = [
    "kauditd_printk_skb",
    "audit: type=",
    "GET /api/health",
    "GET /api/workspace/x/workers/active",
    "Stopped target Multi-User System.",
    "Starting Daily apt upgrade",
    "Listening on Docker Socket",
]

KEEP = [
    "Reached target Reboot.",
    "Sending SIGTERM to remaining processes...",
    "Out of memory: Killed process 4242 (firefox)",
    "earlyoom[900]: sending SIGTERM to process 4242.",
]


def _write_records(path: Path, records) -> Path:
    path.write_text(
        "\n".join(json.dumps(rec) for rec in records) + "\n",
        encoding="utf-8",
    )
    return path


def _write_ledger(path: Path) -> Path:
    return _write_records(path, (BIRTH, VERDICT))


def _run(args=(), ledger=None, stdin_text=None, env_extra=None):
    env = dict(os.environ)
    env["TM_FORENSICS_NO_JOURNAL"] = "1"
    if ledger is not None:
        env["TM_WITNESS_LEDGER"] = str(ledger)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _sections(text: str) -> list:
    """The ordered list of `----- label -----` section headers."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-----") and stripped.endswith("-----"):
            out.append(stripped.strip("-").strip())
    return out


# --------------------------------------------------------------------------- #
# the answer comes first
# --------------------------------------------------------------------------- #
def test_summary_leads_with_the_witness_verdicts(tmp_path):
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"))

    assert result.returncode == 0, result.stderr
    text = result.stdout

    assert "----- summary -----" in text
    assert "witness verdicts (from ledger): 1" in text
    assert "reboot" in text

    # the verdict is an answer: it must be printed before the ledger evidence
    assert text.index("----- summary -----") < text.index(
        "witness ledger: verdicts"
    )


# --------------------------------------------------------------------------- #
# the noise filter
# --------------------------------------------------------------------------- #
def test_noise_filter_drops_audit_and_health_poll_lines():
    assert "NOISE_DROP=" in SCRIPT.read_text(encoding="utf-8")

    result = _run(["--filter"], stdin_text=NOISY)

    assert result.returncode == 0, result.stderr
    # The patterns must reach awk un-escaped.  Handing them to `awk -v` makes
    # awk eat the backslashes, so `systemd\[1\]` collapses into the bracket
    # expression `systemd[1]`: the lines it should drop survive AND awk warns
    # "escape sequence '\[' treated as plain '['" on stderr.  No warning may
    # ever escape to the operator's terminal.
    assert "awk: warning" not in result.stderr, result.stderr
    for noise in NOISE_ONLY:
        assert noise not in result.stdout, f"noise line survived: {noise!r}"
    for keep in KEEP:
        assert keep in result.stdout, f"signal line was dropped: {keep!r}"


# --------------------------------------------------------------------------- #
# a missing ledger is an error, but the summary must already be on screen
# --------------------------------------------------------------------------- #
def test_missing_ledger_exits_3_with_the_summary_already_printed(tmp_path):
    missing = tmp_path / "nope" / "ledger.jsonl"
    result = _run(ledger=missing)

    assert result.returncode == 3
    assert "----- summary -----" in result.stdout
    assert "witness verdicts (from ledger): ledger MISSING" in result.stdout


# --------------------------------------------------------------------------- #
# a missing ledger must never remove evidence of a DIFFERENT kind
# --------------------------------------------------------------------------- #
RAW_JOURNAL_SECTIONS = [
    "previous boot: kernel -p err",
    "previous boot: oom / memcg / segfault / traps",
    "previous boot: earlyoom + systemd-oomd",
    "previous boot: shutdown timeline",
    "previous boot: orderly shutdown target reached?",
]


def _section_body(text: str, needle: str):
    """The non-blank lines printed under the header that contains `needle`."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("-----") and stripped.endswith("-----") and needle in stripped:
            body = []
            for following in lines[index + 1:]:
                marker = following.strip()
                if marker.startswith("-----") and marker.endswith("-----"):
                    break
                if marker:
                    body.append(following)
            return body
    return None


def test_full_with_a_missing_ledger_still_prints_the_raw_journal_evidence(tmp_path):
    """A missing ledger used to `exit 3` BEFORE the `--full` block ran, so
    `--full` without a ledger silently dropped the raw journal evidence -- the one
    body of evidence that has nothing whatever to do with the ledger.  A missing
    ledger may cost us the ledger sections; it may never cost us the journal.

    Journal access is off (TM_FORENSICS_NO_JOURNAL=1), but that is the script's
    own documented hook and NOT a mock of journalctl: the sections are still
    requested, still labelled and still print their body ("skipped ...").  The
    defect is a control-flow defect, and it is visible in the section list.
    """
    missing = tmp_path / "definitely-no-such-ledger.jsonl"
    assert not missing.exists()

    result = _run(["--full"], ledger=missing)
    text = result.stdout

    # (c) the exit code for a missing ledger is unchanged
    assert result.returncode == 3, text + result.stderr

    # (a) still detected, still announced, and the announcement names the cause
    #     (the exact path it looked for) so "no ledger" cannot read as "nothing
    #     happened"
    assert "witness ledger MISSING" in text
    assert str(missing) in text, "the announcement must name the path it looked for"
    assert "witness verdicts (from ledger): ledger MISSING" in text

    # (b) the --full raw journal evidence is still extracted and still printed:
    #     all five sections, each with a body, and still after the answers
    sections = _sections(text)
    for needle in RAW_JOURNAL_SECTIONS:
        assert needle in text, (
            "a missing ledger removed the raw journal evidence from --full; "
            "sections printed: %r" % (sections,)
        )
        body = _section_body(text, needle)
        assert body, "section %r printed a header but no body" % (needle,)
    assert text.index("----- summary -----") < text.index(RAW_JOURNAL_SECTIONS[0])

    # the ledger sections are still present, saying WHY they are empty
    assert "witness ledger: last 20 records" in text
    assert "witness ledger: files" in text


# --------------------------------------------------------------------------- #
# --quiet is the compact default


# --------------------------------------------------------------------------- #
# --quiet is the compact default
# --------------------------------------------------------------------------- #
def test_quiet_is_a_synonym_for_the_compact_default(tmp_path):
    ledger = _write_ledger(tmp_path / "ledger.jsonl")

    compact = _run(ledger=ledger)
    quiet = _run(["--quiet"], ledger=ledger)

    assert compact.returncode == 0, compact.stderr
    assert quiet.returncode == 0, quiet.stderr
    assert _sections(quiet.stdout) == _sections(compact.stdout)
    assert "previous boot: kernel -p err" not in quiet.stdout


# --------------------------------------------------------------------------- #
# --full is the only way to see raw journal text
# --------------------------------------------------------------------------- #
def test_full_adds_the_raw_sections_and_states_the_priority(tmp_path):
    ledger = _write_ledger(tmp_path / "ledger.jsonl")

    compact = _run(ledger=ledger)
    full = _run(["--full"], ledger=ledger)

    assert compact.returncode == 0, compact.stderr
    assert full.returncode == 0, full.stderr

    assert "previous boot: kernel -p err" not in compact.stdout
    assert "previous boot: kernel -p err" in full.stdout
    assert "priority=err" in full.stdout

    # evidence still comes after the answers
    assert full.stdout.index("----- summary -----") < full.stdout.index(
        "previous boot: kernel -p err"
    )


# --------------------------------------------------------------------------- #
# honest counts: the unit selector, and UNMEASURED instead of a lie
# --------------------------------------------------------------------------- #
# A journalctl that answers the script's queries from a table instead of from the
# machine's journal.  It answers DIFFERENTLY for the unit selector (-t earlyoom)
# and for the MESSAGE grep (-g ...earlyoom...), so a count taken from the wrong
# one cannot pass.
FAKE_JOURNALCTL = r"""#!/usr/bin/env bash
# FAKE_UNIT_LINES: lines emitted for `-t earlyoom`
# FAKE_GREP_LINES: lines emitted for `-g ...earlyoom...`
# FAKE_UNIT_SLEEP=1: `-t earlyoom` never answers (exercises the timeout path)
args=" $* "
case "${args}" in
    *" --list-boots "*)
        printf 'IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n'
        printf ' -1 89abcdef0123456789abcdef012345  Mon 2026-11-24 14:20:56 UTC Tue 2026-11-25 02:52:15 UTC\n'
        printf '  0 0123456789abcdef0123456789abcd  Tue 2026-11-25 03:00:00 UTC Tue 2026-11-25 12:00:00 UTC\n'
        exit 0 ;;
esac
# FAKE_FAIL_RC=1 + FAKE_FAIL_STDERR: every journal query fails the way a real
# journalctl does -- exit 1 -- optionally announcing "-- No entries --" on
# stderr, which is an ANSWER (nothing matched), not a failure.
if [ "${FAKE_FAIL_RC:-0}" != "0" ]; then
    printf '%s\n' "${FAKE_FAIL_STDERR:-}" >&2
    exit "${FAKE_FAIL_RC}"
fi
case "${args}" in
    *" -t earlyoom "*)
        # FAKE_UNIT_NO_ENTRIES=1: a unit selector that matches NOTHING (a -t that
        # does not fit this systemd), while the -g message grep still sees lines.
        if [ "${FAKE_UNIT_NO_ENTRIES:-0}" = "1" ]; then
            printf '%s\n' '-- No entries --' >&2
            exit 1
        fi
        if [ "${FAKE_UNIT_SLEEP:-0}" = "1" ]; then
            sleep 60
            exit 0
        fi
        i=0
        while [ "${i}" -lt "${FAKE_UNIT_LINES:-0}" ]; do
            printf 'Nov 25 02:51:58 box earlyoom[900]: sending SIGKILL to process 4242\n'
            i=$((i + 1))
        done
        exit 0 ;;
    *" -g "*earlyoom*)
        i=0
        while [ "${i}" -lt "${FAKE_GREP_LINES:-0}" ]; do
            printf 'Nov 25 02:51:58 box earlyoom[900]: sending SIGKILL to process 4242\n'
            i=$((i + 1))
        done
        exit 0 ;;
esac
exit 0
"""


def _probe_exec(base):
    """Can a chmod +x file in `base` actually be run?"""
    probe = base / "exec-probe"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    probe.chmod(0o755)
    try:
        return subprocess.run([str(probe)], capture_output=True).returncode == 0
    except OSError:
        return False
    finally:
        probe.unlink()


_EXEC_DIRS: list[Path] = []
_SESSION_EXEC_DIR: Path | None = None


def _exec_dir(tmp_path):
    """A writable dir whose files may be EXECUTED -- and that never litters the tree.

    pytest's tmp_path lives under /tmp, which some containers mount noexec: a
    chmod +x fake journalctl there cannot be run, so PATH-injecting it silently
    degrades to the script's "journalctl unavailable" branch and the count
    assertions fail for a reason that has nothing to do with the script.  So
    probe -- but only in tmp_path and in the repo's ALREADY-UNTRACKED .scratch/,
    never in the repo root, where a dot-directory named like a fixture is
    exactly what someone stages by accident.  Every directory made here is
    removed by _cleanup_exec_dirs(), which runs after the last test whatever
    the assertions did.
    """
    global _SESSION_EXEC_DIR
    if _SESSION_EXEC_DIR is not None:
        return _SESSION_EXEC_DIR
    tried = []
    for parent in (tmp_path, REPO_ROOT / ".scratch"):
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:                      # read-only checkout
            tried.append("%s (%s)" % (parent, exc))
            continue
        base = Path(tempfile.mkdtemp(prefix=".tm-fakebin-", dir=str(parent)))
        _EXEC_DIRS.append(base)
        if _probe_exec(base):
            _SESSION_EXEC_DIR = base
            atexit.register(shutil.rmtree, base, True)   # belt and braces
            return base
        shutil.rmtree(base, ignore_errors=True)
        _EXEC_DIRS.remove(base)
        tried.append(str(base))
    raise RuntimeError("no executable directory available: " + ", ".join(tried))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_exec_dirs():
    """Unconditional teardown: a failing assertion leaves nothing behind."""
    yield
    for base in list(_EXEC_DIRS):
        shutil.rmtree(base, ignore_errors=True)
    _EXEC_DIRS.clear()


def _fake_journalctl_env(tmp_path, **fake_vars):
    """Env whose PATH starts with the fake journalctl (the real one is unused)."""
    bin_dir = _exec_dir(tmp_path) / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "journalctl"
    if not exe.exists():
        exe.write_text(FAKE_JOURNALCTL, encoding="utf-8")
        exe.chmod(0o755)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "TM_FORENSICS_NO_JOURNAL": "0",
        "TM_FORENSICS_BUDGET_S": "5",
        "FAKE_UNIT_LINES": "0",
        "FAKE_GREP_LINES": "0",
        "FAKE_UNIT_SLEEP": "0",
    }
    env.update({key: str(value) for key, value in fake_vars.items()})
    return env


def _path_without_journalctl(tmp_path):
    """A PATH with every ordinary tool on it and no journalctl at all."""
    bin_dir = tmp_path / "nojournal-bin"
    bin_dir.mkdir()
    for source in ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"):
        try:
            names = os.listdir(source)
        except OSError:
            continue
        for name in names:
            if name == "journalctl":
                continue
            target = bin_dir / name
            if target.exists() or target.is_symlink():
                continue
            try:
                target.symlink_to(os.path.join(source, name))
            except OSError:
                pass
    return str(bin_dir)


def test_earlyoom_kill_count_comes_from_the_unit_not_a_message_grep(tmp_path):
    """`-g earlyoom` greps the MESSAGE: it counts the word, not the process.

    The fake answers 3 lines for the unit selector and 0 for the message grep, so
    only a unit-scoped query (journalctl -t earlyoom) can produce the 3; and it
    answers 2 vs 5 to pin that the -g count cannot come back.
    """
    ledger = _write_ledger(tmp_path / "ledger.jsonl")

    unit_only = _run(
        ledger=ledger, env_extra=_fake_journalctl_env(tmp_path, FAKE_UNIT_LINES=3)
    )
    assert unit_only.returncode == 0, unit_only.stderr
    assert "earlyoom kills in prior boot: 3" in unit_only.stdout

    both = _run(
        ledger=ledger,
        env_extra=_fake_journalctl_env(tmp_path, FAKE_UNIT_LINES=2, FAKE_GREP_LINES=5),
    )
    assert both.returncode == 0, both.stderr
    assert "earlyoom kills in prior boot: 2" in both.stdout
    assert "earlyoom kills in prior boot: 5" not in both.stdout


def test_a_timed_out_journal_query_is_unmeasured_and_never_zero(tmp_path):
    env = _fake_journalctl_env(tmp_path, FAKE_UNIT_SLEEP=1, TM_FORENSICS_BUDGET_S=2)
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"), env_extra=env)

    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "earlyoom kills in prior boot: UNMEASURED (journalctl timed out after 2 s)" in text
    assert "earlyoom kills in prior boot: 0" not in text
    # the header and the summary both own up to it
    assert "unmeasured: 1 of 6 journal answers" in text
    # the other five queries really did run, so their 0s are measurements
    assert "kernel oom kills in prior boot: 0" in text


def test_a_missing_journalctl_is_unmeasured_and_never_zero(tmp_path):
    env = {
        "PATH": _path_without_journalctl(tmp_path),
        "TM_FORENSICS_NO_JOURNAL": "0",
    }
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"), env_extra=env)

    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "kernel oom kills in prior boot: UNMEASURED (journalctl unavailable)" in text
    assert "earlyoom kills in prior boot: UNMEASURED (journalctl unavailable)" in text
    assert "unmeasured: 6 of 6 journal answers" in text
    assert "kills in prior boot: 0" not in text
    assert "segfaults/traps in prior boot: 0" not in text


def test_the_no_journal_hook_still_skips_every_query_and_says_so(tmp_path):
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"))

    assert result.returncode == 0, result.stderr
    assert (
        "kernel oom kills in prior boot: UNMEASURED (skipped (TM_FORENSICS_NO_JOURNAL=1))"
        in result.stdout
    )
    assert "unmeasured: 6 of 6 journal answers" in result.stdout
    assert "signals: 0" in result.stdout


# --------------------------------------------------------------------------- #
# signals: WHO signalled WHOM
# --------------------------------------------------------------------------- #
SIGNAL_KIND = {
    "kind": "signal",
    "ts": "2026-11-25T02:51:58.123456+00:00",
    "si_pid": 1111,
    "sender_comm": "earlyoom",
    "origin": "userspace",
    "target_pid": 4242,
}

SIGNAL_EVENT = {
    "event": "signal",
    "ts": "2026-11-25T02:52:01.000000+00:00",
    "si_pid": 2222,
    "sender_comm": "systemd",
    "origin": "kernel",
    "target_pid": 4243,
}


def test_signals_section_names_the_sender_and_the_target(tmp_path):
    ledger = _write_records(
        tmp_path / "ledger.jsonl", (BIRTH, VERDICT, SIGNAL_KIND, SIGNAL_EVENT)
    )
    result = _run(ledger=ledger)

    assert result.returncode == 0, result.stderr
    text = result.stdout

    assert "signals: 2" in text

    body = _section_body(text, "signals (sender -> run)")
    assert body, "the signals section printed a header but no body"
    lines = [line for line in body if "target_pid=" in line]
    assert len(lines) == 2, body

    first, second = lines
    assert SIGNAL_KIND["ts"] in first
    assert "sender=1111 (earlyoom)" in first
    assert "origin=userspace" in first
    assert "target_pid=4242" in first

    assert SIGNAL_EVENT["ts"] in second
    assert "sender=2222 (systemd)" in second
    assert "origin=kernel" in second
    assert "target_pid=4243" in second


def test_signals_are_unmeasured_when_the_ledger_is_missing_or_not_jsonl(tmp_path):
    missing = tmp_path / "no-such-ledger.jsonl"
    absent = _run(ledger=missing)

    assert absent.returncode == 3
    assert "signals: UNMEASURED (ledger MISSING" in absent.stdout

    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("this is not jsonl\nnor is this\n", encoding="utf-8")
    unparseable = _run(ledger=garbage)

    assert unparseable.returncode == 0, unparseable.stderr
    assert "signals: UNMEASURED (ledger is not JSONL" in unparseable.stdout


# --------------------------------------------------------------------------- #
# "no entries" is an answer, not a failure
# --------------------------------------------------------------------------- #
def test_a_query_that_matches_nothing_is_a_measured_zero(tmp_path):
    """journalctl exits 1 when nothing matched, and says so on stderr.

    A boot with no OOM kills must report 0 -- a zero an operator can trust --
    and not UNMEASURED: "measurably none" and "could not look" are different
    statements, and a report that confuses them is no better than one that
    prints 0 for a failed read.
    """
    env = _fake_journalctl_env(
        tmp_path, FAKE_FAIL_RC=1, FAKE_FAIL_STDERR="-- No entries --"
    )
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"), env_extra=env)

    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "kernel oom kills in prior boot: 0" in text
    assert "earlyoom kills in prior boot: 0" in text
    assert "kills in prior boot: UNMEASURED" not in text
    # "no shutdown marker" is likewise an answer about boot -1
    assert "prior boot ended orderly?: no -- no shutdown target entry" in text
    assert "unmeasured: 0 of 6 journal answers" in text


def test_a_query_that_really_fails_is_unmeasured_with_its_own_stderr(tmp_path):
    """Exit 1 WITHOUT "no entries" is a failed read; the reason quotes stderr.

    Same returncode as the test above, opposite meaning: only the message
    distinguishes them, so the message has to survive into the report.
    """
    stderr_line = "Failed to open /var/log/journal: Permission denied"
    env = _fake_journalctl_env(
        tmp_path, FAKE_FAIL_RC=1, FAKE_FAIL_STDERR=stderr_line
    )
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"), env_extra=env)

    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "earlyoom kills in prior boot: UNMEASURED (journalctl failed: exit 1" in text
    assert 'exit 1: "%s"' % stderr_line in text
    assert "kernel oom kills in prior boot: UNMEASURED (journalctl failed: exit 1" in text
    assert "kills in prior boot: 0" not in text
    assert "segfaults/traps in prior boot: 0" not in text
    assert "unmeasured: 6 of 6 journal answers" in text


def test_a_sender_pid_is_never_printed_as_the_target(tmp_path):
    """The ledger names the SENDER (si_pid) and the run; it has no target pid.

    A record that also carries a bare `pid` must not have that pid shown as the
    victim: it is the sender's, and printing it would name the aggressor as the
    target.  The line must say what the data actually supports.
    """
    record = {
        "event": "signal",
        "ts": "2026-11-25T02:51:58.123456+00:00",
        "run_id": "run-1",
        "signo": 15,
        "signame": "SIGTERM",
        "si_pid": 1111,
        "pid": 1111,
        "sender_comm": None,
        "origin": "proc",
    }
    ledger = _write_records(tmp_path / "ledger.jsonl", (record,))
    result = _run(ledger=ledger)

    assert result.returncode == 0, result.stderr
    assert "signals: 1" in result.stdout

    body = _section_body(result.stdout, "signals (sender -> run)")
    lines = [line for line in body if "sender=" in line]
    assert len(lines) == 1, body
    line = lines[0]

    assert "sender=1111" in line
    assert "target_pid=1111" not in line
    assert "target=run run-1" in line
    assert "signal=SIGTERM" in line
    assert "(null)" not in line



# --------------------------------------------------------------------------- #
# the one claim no test can settle: does `-t earlyoom` match this systemd?
# --------------------------------------------------------------------------- #
def test_a_unit_selector_that_matches_nothing_is_caught_by_the_cross_check(tmp_path):
    """A `-t` that fits nothing answers "No entries" -- a confident measured 0.

    If the unit query says 0 while the MESSAGE grep sees lines, the selector is
    the suspect, and the report must say so rather than present the 0 as fact.
    The answer itself is NOT rewritten: silently swapping one number for another
    is the very bug being fixed.
    """
    env = _fake_journalctl_env(
        tmp_path, FAKE_UNIT_NO_ENTRIES=1, FAKE_GREP_LINES=3
    )
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"), env_extra=env)

    assert result.returncode == 0, result.stderr
    text = result.stdout
    # the unit answer is a measured 0 -- and stays one, on the record
    assert "earlyoom kills in prior boot: 0" in text
    assert "unmeasured: 0 of 6 journal answers" in text
    assert (
        "WARNING   : earlyoom cross-check: the unit query said 0 but 3 journal lines"
        " mention earlyoom -- the -t selector may not match this systemd; treat the"
        " kill count as UNMEASURED"
    ) in text


def test_the_cross_check_is_silent_when_the_unit_agrees(tmp_path):
    """A unit answer above 0 is not contradicted, so nothing is printed."""
    env = _fake_journalctl_env(tmp_path, FAKE_UNIT_LINES=3, FAKE_GREP_LINES=3)
    result = _run(ledger=_write_ledger(tmp_path / "ledger.jsonl"), env_extra=env)

    assert result.returncode == 0, result.stderr
    assert "earlyoom kills in prior boot: 3" in result.stdout
    assert "earlyoom cross-check" not in result.stdout

