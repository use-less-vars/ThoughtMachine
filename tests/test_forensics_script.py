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

import json
import os
import subprocess
from pathlib import Path

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


def _write_ledger(path: Path) -> Path:
    path.write_text(
        "\n".join(json.dumps(rec) for rec in (BIRTH, VERDICT)) + "\n",
        encoding="utf-8",
    )
    return path


def _run(args=(), ledger=None, stdin_text=None):
    env = dict(os.environ)
    env["TM_FORENSICS_NO_JOURNAL"] = "1"
    if ledger is not None:
        env["TM_WITNESS_LEDGER"] = str(ledger)
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
