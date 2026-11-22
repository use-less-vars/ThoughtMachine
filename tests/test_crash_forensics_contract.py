"""Contract tests for the launcher's crash forensics.

The crash this instrument exists for destroyed its own evidence: the backend was
killed by SIGKILL, ``start_thoughtmachine.sh`` mirrored the console through
``tee`` (which TRUNCATES the log on the next launch), ``kill_thoughtmachine.sh``
wrote nothing at all, and the surviving log started *after* the death. So these
tests pin two separate things.

1. **Behaviour** of ``scripts/crash_forensics.py``: a log is COPIED (the original
   is never touched), records are APPENDED (never truncated), a signal death is
   decoded and snapshotted, and the heartbeat records the death of the pid it
   watches and then stops.

2. **Wiring** of the launcher shell scripts -- because the module is dead code
   the moment the launcher stops calling it, and a missing call is invisible:
   nothing fails, the next crash is simply unexplained again. The wiring checks
   are deliberately written as a *finding-producing* scan with a negative
   control (``test_the_wiring_scan_is_not_vacuous``): the scan has to go red when
   the source is broken, or it is only proving that it can count to zero.

The one non-negotiable property is pinned too: forensics must never be able to
gate a boot. ``main()`` returns 0 on every failure unless the tests pass the
``--strict`` flag, which the launcher never does.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "crash_forensics.py"
LAUNCHER = REPO_ROOT / "start_thoughtmachine.sh"
KILL_SWITCH = REPO_ROOT / "kill_thoughtmachine.sh"


def _load_module():
    """Import ``scripts/crash_forensics.py`` without requiring it to be a package."""
    spec = importlib.util.spec_from_file_location("crash_forensics", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cf = _load_module()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _dead_pid() -> int:
    """A pid that is certainly not alive (spawned, then reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --------------------------------------------------------------------- archive-log
def test_archive_log_copies_the_log_and_leaves_the_original_alone(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "backend_startup.log"
    log.parent.mkdir(parents=True)
    payload = b"first line\nsecond line\n"
    log.write_bytes(payload)
    crash = tmp_path / "logs" / "crash"

    assert cf.main(["archive-log", "--log", str(log), "--dir", str(crash), "--keep", "10"]) == 0

    assert log.read_bytes() == payload, "the live log must be COPIED, never moved or truncated"
    archives = sorted(crash.glob("backend-*.log"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == payload, "the archive must be byte-for-byte"


def test_archive_log_is_a_no_op_for_a_missing_or_empty_log(tmp_path: Path) -> None:
    crash = tmp_path / "crash"
    empty = tmp_path / "backend_startup.log"
    empty.write_bytes(b"")

    assert cf.main(["archive-log", "--log", str(tmp_path / "nope.log"), "--dir", str(crash)]) == 0
    assert cf.main(["archive-log", "--log", str(empty), "--dir", str(crash)]) == 0
    assert sorted(crash.glob("backend-*.log")) == [], "an empty log is not worth archiving"


def test_archive_log_never_overwrites_an_existing_archive(tmp_path: Path) -> None:
    log = tmp_path / "backend_startup.log"
    log.write_text("run one\n")
    crash = tmp_path / "crash"

    assert cf.main(["archive-log", "--log", str(log), "--dir", str(crash)]) == 0
    first = sorted(crash.glob("backend-*.log"))[0].read_bytes()
    log.write_text("run two\n")
    assert cf.main(["archive-log", "--log", str(log), "--dir", str(crash)]) == 0

    archives = sorted(crash.glob("backend-*.log"))
    assert len(archives) == 2, "copy2 preserves mtime, so the second name must be de-duplicated"
    assert first in [path.read_bytes() for path in archives]
    assert b"run two\n" in [path.read_bytes() for path in archives]


def test_pruning_keeps_the_newest_archives(tmp_path: Path) -> None:
    crash = tmp_path / "crash"
    crash.mkdir()
    for index in range(5):
        path = crash / f"backend-1970010{index + 1}T000000Z.log"
        path.write_text(f"run {index}\n")
        os.utime(path, (1000 + index, 1000 + index))

    removed = cf.prune_archives(crash, 2)

    kept = sorted(path.name for path in crash.glob("backend-*.log"))
    assert kept == ["backend-19700104T000000Z.log", "backend-19700105T000000Z.log"]
    assert len(removed) == 3


# --------------------------------------------------------------------- record-exit
def test_ordinary_exit_codes_are_not_read_as_signal_deaths() -> None:
    for rc in (0, 1, 15, 128):
        decoded = cf.decode_rc(rc)
        assert decoded["signal"] is None, f"rc={rc} is an ordinary exit code, not a death"


def test_signal_deaths_are_decoded_from_the_bash_convention() -> None:
    assert cf.decode_rc(137)["signal"] == 9
    assert cf.decode_rc(137)["signal_name"] == "SIGKILL"
    assert cf.decode_rc(143)["signal"] == 15
    assert cf.decode_rc(143)["signal_name"] == "SIGTERM"
    # python's subprocess convention (-N) must decode the same way.
    assert cf.decode_rc(-9)["signal"] == 9


def test_exit_137_is_documented_as_ambiguous() -> None:
    """137 means SIGKILL *or* a deliberate ``exit 137`` -- say which reading won."""
    decoded = cf.decode_rc(137)
    assert decoded["interpretation"] == "rc>128 read as 128+signal (bash wait convention)"
    assert decoded["rc"] == 137, "the raw rc must stay in the record so a human can re-read it"


def test_a_signal_death_snapshots_the_log_at_reaping_time(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "backend_startup.log"
    log.parent.mkdir(parents=True)
    payload = b"the only surviving evidence\n"
    log.write_bytes(payload)
    crash = tmp_path / "logs" / "crash"

    assert cf.main(["record-exit", "--rc", "137", "--log", str(log), "--dir", str(crash)]) == 0

    snapshots = sorted(crash.glob("crashed-*.log"))
    assert len(snapshots) == 1, "a SIGKILL must copy the log while those bytes still exist"
    assert snapshots[0].read_bytes() == payload
    assert log.read_bytes() == payload
    receipt = _read_jsonl(crash / "receipts.jsonl")[-1]
    assert receipt["event"] == "exit"
    assert receipt["signal_name"] == "SIGKILL"
    assert receipt["archived"] == str(snapshots[0])
    assert receipt["archived_reason"] == "copied at reaping time"


def test_an_ordinary_exit_takes_no_snapshot_but_still_records(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "backend_startup.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"clean shutdown\n")
    crash = tmp_path / "logs" / "crash"

    assert cf.main(["record-exit", "--rc", "0", "--log", str(log), "--dir", str(crash)]) == 0

    assert sorted(crash.glob("crashed-*.log")) == []
    receipt = _read_jsonl(crash / "receipts.jsonl")[-1]
    assert receipt["signal"] is None
    assert receipt["archived"] is None
    assert receipt["archived_reason"] == "no snapshot: ordinary exit"


# ------------------------------------------------------------------------ heartbeat
def test_heartbeat_records_the_death_and_returns_promptly(tmp_path: Path) -> None:
    pid = _dead_pid()
    started = time.time()

    assert cf.main(["heartbeat", "--pid", str(pid), "--dir", str(tmp_path),
                    "--interval", "0.1"]) == 0

    assert time.time() - started < 5, "the heartbeat must stop, not spin, once the pid is gone"
    records = _read_jsonl(tmp_path / "heartbeat.jsonl")
    assert records, "a heartbeat that watches a dead pid still owes one record"
    assert records[-1]["event"] == "death"
    assert records[-1]["pid"] == pid
    assert records[-1]["last_seen_ts"] is None, "it was never seen alive, so nothing is claimed"


def test_heartbeat_follows_a_live_process_until_it_dies(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    try:
        assert cf.main(["heartbeat", "--pid", str(proc.pid), "--dir", str(tmp_path),
                        "--interval", "0.1"]) == 0
    finally:
        proc.wait()

    records = _read_jsonl(tmp_path / "heartbeat.jsonl")
    assert records[0]["event"] == "beat"
    assert records[0]["alive"] is True
    assert records[-1]["event"] == "death"
    beats = [record for record in records if record["event"] == "beat"]
    assert len(beats) >= 2, "the interval must actually be sampled, not assumed"
    assert records[-1]["last_seen_ts"] is not None
    assert records[-1]["beats"] == len(beats)


def test_heartbeat_never_spins_below_the_floor(tmp_path: Path) -> None:
    pid = _dead_pid()
    started = time.time()
    assert cf.main(["heartbeat", "--pid", str(pid), "--dir", str(tmp_path),
                    "--interval", "0"]) == 0
    assert time.time() - started < 5


# --------------------------------------------------------------------------- header
def test_header_names_why_a_field_is_unknown(tmp_path: Path) -> None:
    assert cf.main(["header", "--dir", str(tmp_path)]) == 0

    header = _read_jsonl(tmp_path / "receipts.jsonl")[-1]
    assert header["event"] == "header"
    assert header["backend_pid"] == cf.UNKNOWN
    assert "before the launch" in header["degraded"]["backend_pid"]
    assert header["argv"] == cf.UNKNOWN
    assert "--argv" in header["degraded"]["argv"]


def test_header_carries_the_launch_context(tmp_path: Path) -> None:
    command = ".venv/bin/python -m web_ui.backend.server --port 8000"
    assert cf.main(["header", "--dir", str(tmp_path), "--pid", "4242", "--argv", command]) == 0

    header = _read_jsonl(tmp_path / "receipts.jsonl")[-1]
    assert header["backend_pid"] == 4242
    assert header["argv"] == command
    assert header["cwd"] and header["host"] and header["python_version"]
    assert "degraded" not in header or "backend_pid" not in header["degraded"]


# --------------------------------------------------------------- append-only + caps
def test_receipts_are_appended_never_truncated(tmp_path: Path) -> None:
    assert cf.main(["header", "--dir", str(tmp_path)]) == 0
    assert cf.main(["header", "--dir", str(tmp_path)]) == 0

    lines = (tmp_path / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "a second launch must append, not replace the previous record"


def test_the_jsonl_self_cap_keeps_the_newest_records_and_says_so(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    for index in range(6):
        cf.append_jsonl(path, {"event": "header", "index": index}, 4)

    records = _read_jsonl(path)
    assert len(records) == 4, "an unbounded append-only file is its own outage"
    assert records[0]["event"] == "trim"
    # The cap is re-applied on every append, so the trim record reports the loss
    # made by the LAST append (5 lines before it, 3 kept).
    assert records[0]["dropped"] == 2
    assert [record["index"] for record in records[1:]] == [3, 4, 5]


# ------------------------------------------------------------------------ fail-open
def test_a_forensic_failure_never_propagates(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way\n")
    target = blocker / "crash"
    args = ["record-exit", "--rc", "1", "--log", str(blocker), "--dir", str(target)]

    assert cf.main(list(args)) == 0, "forensics must NEVER be able to fail a boot"

    assert cf.main(["--strict"] + args) == 1, "the tests need to see the failure the launcher hides"


def test_the_module_never_aims_at_the_vault() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "Path.home()" not in source, "forensics must never write below the user's home"
    assert "expanduser" not in source


# -------------------------------------------------------------------- launcher wiring
FORENSIC_CALLS = {
    "archive-log": ("--log", "--dir", "--keep"),
    "header": ("--dir", "--argv"),
    "record-exit": ("--rc", "--log", "--dir"),
    "heartbeat": ("--pid", "--dir", "--interval"),
}

#: Every place that waits for the backend, minus the function definition itself.
EXPECTED_REAP_SITES = 4


def _functions(text: str) -> Dict[str, List[str]]:
    """Naive shell function splitter: ``name() {`` ... a ``}`` in column 0."""
    found: Dict[str, List[str]] = {}
    name: Optional[str] = None
    body: List[str] = []
    for line in text.splitlines():
        if name is None:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", line)
            if match:
                name, body = match.group(1), [line]
            continue
        body.append(line)
        if line == "}":
            found[name] = body
            name = None
    return found


def _wiring_findings(text: str) -> List[str]:
    """Everything wrong with how the launcher calls the forensic module."""
    findings: List[str] = []
    lines = text.splitlines()
    functions = _functions(text)

    for subcommand, flags in FORENSIC_CALLS.items():
        hits = [line for line in lines
                if f"forensic {subcommand}" in line and not line.lstrip().startswith("#")]
        if not hits:
            findings.append(f"the launcher never calls 'forensic {subcommand}'")
        elif not any(all(flag in line for flag in flags) for line in hits):
            findings.append(f"'forensic {subcommand}' is called without {flags}")

    # The archive must be taken BEFORE tee truncates the log, or it archives nothing.
    archive_at = next((index for index, line in enumerate(lines)
                       if "forensic archive-log" in line), None)
    tee_at = next((index for index, line in enumerate(lines)
                   if 'tee "$BACKEND_LOG"' in line), None)
    if archive_at is not None and tee_at is not None and archive_at > tee_at:
        findings.append("archive-log runs after tee truncated the log it was meant to save")

    # The heartbeat must not be able to delay the health gate.
    beats = [line for line in lines if "forensic heartbeat" in line
             and not line.lstrip().startswith("#")]
    if beats and not all(line.rstrip().endswith("&") for line in beats):
        findings.append("the heartbeat is not backgrounded, so it would block the boot")

    reaps = [line for line in lines
             if re.match(r"^\s*reap_backend\b", line) and not line.rstrip().endswith("{")]
    if len(reaps) != EXPECTED_REAP_SITES:
        findings.append(f"expected {EXPECTED_REAP_SITES} reap_backend call sites, "
                        f"found {len(reaps)}: an unrecorded backend death is invisible")

    reap_body = "\n".join(functions.get("reap_backend", []))
    if 'wait "$BACKEND_PID"' not in reap_body:
        findings.append("reap_backend does not wait for the backend")
    if "forensic record-exit" not in reap_body:
        findings.append("reap_backend does not record the exit code")

    clean_body = "\n".join(functions.get("cleanup", []))
    if "forensic" in clean_body:
        findings.append("cleanup() must stay uninstrumented: it runs inside a signal trap")
    if clean_body and 'wait "$BACKEND_PID"' not in clean_body:
        findings.append("cleanup() no longer waits for the backend it killed")

    waits = [line for line in lines if 'wait "$BACKEND_PID"' in line]
    if len(waits) != 2:
        findings.append(f"expected 2 backend waits (reap_backend + cleanup), found {len(waits)}: "
                        "a bare wait reaps the backend without recording why it died")

    wrapper = functions.get("forensic", [])
    if not wrapper:
        findings.append("the launcher has no forensic() wrapper")
    else:
        if not any("python3" in line for line in wrapper):
            findings.append("the forensic wrapper no longer invokes the module")
        if not any(line.strip() == "return 0" for line in wrapper):
            findings.append("the forensic wrapper must fail open (return 0 on every path)")
    return findings


def test_the_launcher_calls_the_forensics_module() -> None:
    findings = _wiring_findings(LAUNCHER.read_text(encoding="utf-8"))
    assert findings == [], "launcher wiring is incomplete: " + "; ".join(findings)


def test_the_wiring_scan_is_not_vacuous(tmp_path: Path) -> None:
    """A scanner that cannot fail proves nothing: break the source and watch it go red."""
    source = LAUNCHER.read_text(encoding="utf-8")
    mutations = {
        "record-exit dropped": source.replace(
            'forensic record-exit --rc "$rc" --log "$BACKEND_LOG" --dir "$CRASH_DIR"', ":"),
        "archive-log dropped": source.replace(
            'forensic archive-log --log "$BACKEND_LOG" --dir "$CRASH_DIR" --keep "$CRASH_KEEP"', ":"),
        "heartbeat not backgrounded": source.replace(
            '--interval "$HEARTBEAT_SECS" &', '--interval "$HEARTBEAT_SECS"'),
        "one reap site reverted": source.replace("    reap_backend\n    BACKEND_RC=$?",
                                                 '    wait "$BACKEND_PID"\n    BACKEND_RC=$?'),
    }
    for label, mutated in mutations.items():
        assert mutated != source, f"mutation {label!r} did not apply; the test is stale"
        assert _wiring_findings(mutated) != [], f"the scan missed a broken launcher ({label})"


def test_the_launcher_scripts_are_valid_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed in this container")
    for script in (LAUNCHER, KILL_SWITCH):
        result = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name} is not valid bash: {result.stderr}"


# --------------------------------------------------------------------- kill switch
def test_the_kill_switch_records_before_it_kills() -> None:
    lines = KILL_SWITCH.read_text(encoding="utf-8").splitlines()

    receipt_at = next(index for index, line in enumerate(lines) if "kill-switch.jsonl" in line)
    fuser_at = next(index for index, line in enumerate(lines) if "fuser -k 5173/tcp" in line)
    assert receipt_at < fuser_at, "the receipt must be written BEFORE the kill, not after"

    assert any('>> "$KF_LOG"' in line for line in lines), "the receipt is not appended"
    assert any(line.strip() == "} 2>/dev/null || true" for line in lines), (
        "the receipt block must be fail-open: a failed write cannot block or delay the kill")


def test_the_kill_switch_changes_nothing_about_the_kill() -> None:
    text = KILL_SWITCH.read_text(encoding="utf-8")
    assert "fuser -k 5173/tcp 5174/tcp 5175/tcp 5176/tcp 5177/tcp 8000/tcp" in text

    # Only EXECUTED lines are checked: the comments are allowed to name the tools
    # this script must not reach for ("no sudo, no journal") -- that is the doc.
    executable = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "-SIG" not in executable, "the signal fuser sends must not be changed"
    assert "set -e" not in executable, "a forensic failure must never abort the kill"
    for forbidden in ("sudo", "journalctl"):
        assert forbidden not in executable, f"{forbidden} is not allowed in the kill switch"


# ------------------------------------------------------------------- git hygiene
def test_crash_artifacts_are_gitignored() -> None:
    patterns = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "logs/" in patterns, "logs/crash/ must never be committable"
    assert "*.log" in patterns, "archived logs must never be committable"

    if shutil.which("git") is None:
        return
    for relative in ("logs/crash/receipts.jsonl",
                     "logs/crash/heartbeat.jsonl",
                     "logs/crash/kill-switch.jsonl",
                     "logs/crash/backend-20240101T000000Z.log",
                     "logs/crash/crashed-20240101T000000Z.log"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", relative],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{relative} is not ignored by .gitignore"
