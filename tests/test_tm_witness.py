"""Tests for scripts/tm_witness.py.

Each test fails if the requirement it covers breaks:
  1 signal record carries the real sender (si_pid/si_uid) from /proc
  2 the child actually RECEIVED the forwarded signal and exited gracefully
    (this is the inherited-sigmask bug detector)
  3 exit record present with the right returncode
  4 a SIGKILLed child produces verdict "hard_kill_no_signal" on the next start
  5 boot_id comparison: different boot_id -> "reboot" (no real reboot needed)
  6 the ledger is append-only: earlier bytes are byte-identical after a rerun
"""

import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import traceback

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WITNESS = REPO_ROOT / "scripts" / "tm_witness.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tm_witness  # noqa: E402  (needs scripts/ on sys.path)

pytestmark = pytest.mark.timeout(60)

#: Why THIS host cannot supervise, or None when it can -- the module's own probe,
#: the same one the witness refuses on, so the skip verdict and the runtime
#: verdict can never disagree.  Probed by capability, not by a platform name.
UNSUPPORTED = tm_witness.unsupported_reason()

#: Every test that drives the real witness needs the real capability: without
#: signal.sigwaitinfo there is no sender provenance to assert on, and without a
#: readable /proc/sys/kernel/random/boot_id there is no boot identity to compare.
#: The refusal test below must run everywhere (a green skip would prove nothing),
#: so it is the one test that does not carry this mark.
LINUX_ONLY = pytest.mark.skipif(
    UNSUPPORTED is not None,
    reason="tm_witness needs signal.sigwaitinfo and /proc (Linux); %s" % (UNSUPPORTED,),
)

CHILD_SOURCE = '''
import pathlib
import signal
import sys
import time

marker = pathlib.Path(sys.argv[1])


def _graceful(signo, _frame):
    marker.write_text("graceful %d" % signo)
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _graceful)
signal.signal(signal.SIGINT, _graceful)
signal.signal(signal.SIGHUP, _graceful)
pathlib.Path(str(marker) + ".ready").write_text("ready")
while True:
    time.sleep(0.02)
'''

QUICK_SOURCE = "raise SystemExit(0)\n"


def _write_script(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source)
    return script


def _start(tmp_path, ledger=None, target=None):
    ledger = pathlib.Path(ledger) if ledger is not None else tmp_path / "ledger.jsonl"
    if target is None:
        target = _write_script(tmp_path, "child.py", CHILD_SOURCE)
    marker = tmp_path / "marker.txt"
    argv = [
        sys.executable, str(WITNESS),
        "--ledger", str(ledger),
        "--",
        sys.executable, str(target), str(marker),
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc, ledger, marker


def _records(path):
    try:
        text = pathlib.Path(path).read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _events(records, name):
    return [r for r in records if r.get("event") == name]


def _wait_for(predicate, timeout=20.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _term_scenario(tmp_path):
    """Start the witness, wait for the child to install its handlers, then send
    SIGTERM *from this test process* to the witness (which must forward it)."""
    proc, ledger, marker = _start(tmp_path)
    try:
        ready = _wait_for(lambda: pathlib.Path(str(marker) + ".ready").exists())
        assert ready, "child never became ready; stderr=%r" % (proc.stderr.read() if proc.poll() is not None else "")
        assert _wait_for(lambda: _events(_records(ledger), "birth")), "no birth record"
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    return proc, ledger, marker, _records(ledger)


@LINUX_ONLY
def test_signal_record_captures_sender_provenance(tmp_path):
    proc, ledger, marker, records = _term_scenario(tmp_path)
    signals = _events(records, "signal")
    assert signals, "no signal record in ledger: %r" % (records,)
    record = signals[0]
    assert record["signo"] == int(signal.SIGTERM)
    assert record["signame"] == "SIGTERM"
    assert record["si_pid"] == os.getpid(), "si_pid is not the test process"
    assert record["si_uid"] == os.getuid()
    assert record["origin"] == "proc"
    assert record["sender_comm"], "sender_comm not resolved from /proc"
    assert isinstance(record["ts"], str) and record["ts"].endswith("+00:00")


@LINUX_ONLY
def test_child_received_forwarded_signal_and_exited_gracefully(tmp_path):
    proc, ledger, marker, records = _term_scenario(tmp_path)
    assert marker.exists(), "child never ran its SIGTERM handler (signal was not delivered)"
    assert marker.read_text().strip() == "graceful %d" % int(signal.SIGTERM)
    exits = _events(records, "exit")
    assert exits, "no exit record"
    assert exits[-1]["signaled_by"] is None, "child was killed instead of exiting gracefully"
    assert proc.returncode == 0


@LINUX_ONLY
def test_exit_record_has_correct_returncode(tmp_path):
    proc, ledger, marker, records = _term_scenario(tmp_path)
    exits = _events(records, "exit")
    assert len(exits) == 1, "expected exactly one exit record, got %r" % (exits,)
    assert exits[0]["returncode"] == 0
    assert exits[0]["returncode"] == proc.returncode
    assert exits[0]["signaled_by"] is None
    assert exits[0]["dirty_shutdown"] is False
    assert isinstance(exits[0]["duration_s"], float)
    snapshots = _events(records, "snapshot")
    assert len(snapshots) == 1, "no death-time snapshot record"
    assert "live_children" in snapshots[0]
    assert "backend_startup_log" in snapshots[0]


@LINUX_ONLY
def test_sigkilled_child_yields_hard_kill_verdict(tmp_path):
    ledger = tmp_path / "killed.jsonl"
    proc, _ledger, _marker = _start(tmp_path, ledger=ledger)
    try:
        birth = _wait_for(lambda: _events(_records(ledger), "birth"))
        assert birth, "no birth record"
        os.kill(birth[0]["pid"], signal.SIGKILL)
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    records = _records(ledger)
    exits = _events(records, "exit")
    assert exits, "no exit record after SIGKILL"
    assert exits[-1]["signaled_by"] == int(signal.SIGKILL)
    assert not _events(records, "signal"), "unexpected signal record for an unobserved SIGKILL"

    quick = _write_script(tmp_path, "quick.py", QUICK_SOURCE)
    proc2, _l, _m = _start(tmp_path, ledger=ledger, target=quick)
    try:
        proc2.wait(timeout=25)
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=10)

    verdicts = _events(_records(ledger), "verdict")
    assert verdicts, "restart emitted no verdict record"
    assert verdicts[-1]["verdict"] == "hard_kill_no_signal"
    assert verdicts[-1]["prior_run_id"] == birth[0]["run_id"]


@LINUX_ONLY
def test_boot_id_comparison_classifies_reboot_without_rebooting(tmp_path):
    current_boot_id = tm_witness.read_boot_id()
    assert current_boot_id, "/proc/sys/kernel/random/boot_id unreadable"
    stale_boot_id = "00000000-0000-0000-0000-00000000dead"
    assert stale_boot_id != current_boot_id

    ledger = tmp_path / "fabricated.jsonl"
    birth = {
        "event": "birth", "ts": "2026-01-01T00:00:00+00:00", "run_id": "stale-run",
        "boot_id": stale_boot_id, "pid": 4242, "ppid": 1, "sid": 4242, "pgid": 4242,
        "argv": ["fake"], "cwd": "/", "git_head": None, "python": "3.12.3", "uptime_s": 1.0,
    }
    ledger.write_text(json.dumps(birth) + "\n")
    record = tm_witness.evaluate_previous_run(tm_witness.load_records(ledger), current_boot_id, "new-run")
    assert record is not None, "no verdict for an unfinished run"
    assert record["verdict"] == "reboot"
    assert record["prior_run_id"] == "stale-run"
    assert record["event"] == "verdict"

    same_boot_birth = dict(birth, run_id="same-boot-run", boot_id=current_boot_id)
    ledger.write_text(json.dumps(same_boot_birth) + "\n")
    record2 = tm_witness.evaluate_previous_run(tm_witness.load_records(ledger), current_boot_id, "new-run")
    assert record2["verdict"] == "hard_kill_no_signal"

    exited = dict(same_boot_birth, event="exit", returncode=0, signaled_by=None,
                  duration_s=1.0, dirty_shutdown=False, run_id="same-boot-run")
    ledger.write_text(json.dumps(same_boot_birth) + "\n" + json.dumps(exited) + "\n")
    assert tm_witness.evaluate_previous_run(tm_witness.load_records(ledger), current_boot_id, "new-run") is None


@LINUX_ONLY
def test_ledger_is_append_only_across_runs(tmp_path):
    quick = _write_script(tmp_path, "quick.py", QUICK_SOURCE)
    ledger = tmp_path / "append.jsonl"

    proc1, _l, _m = _start(tmp_path, ledger=ledger, target=quick)
    try:
        proc1.wait(timeout=25)
    finally:
        if proc1.poll() is None:
            proc1.kill()
            proc1.wait(timeout=10)
    first = ledger.read_bytes()
    assert first, "first run wrote nothing"

    proc2, _l, _m = _start(tmp_path, ledger=ledger, target=quick)
    try:
        proc2.wait(timeout=25)
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=10)
    second = ledger.read_bytes()

    assert len(second) > len(first), "second run appended nothing"
    assert second[: len(first)] == first, "earlier bytes changed: ledger is not append-only"
    assert hashlib.sha256(second[: len(first)]).hexdigest() == hashlib.sha256(first).hexdigest()

    records = _records(ledger)
    assert records[0]["event"] == "birth", "record 1 is no longer the first birth record"
    assert len(_events(records, "birth")) == 2

# --------------------------------------------------------------------------- #
# TASK A: the child must die with the supervisor (PR_SET_PDEATHSIG)
# --------------------------------------------------------------------------- #
def _pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@LINUX_ONLY
def test_child_dies_with_the_witness_even_on_sigkill(tmp_path):
    """If kill_thoughtmachine.sh SIGKILLs the witness, the backend must not be
    left behind holding the port.  Remove the prctl() call and this fails."""
    ledger = tmp_path / "pdeath.jsonl"
    argv = [
        sys.executable, str(WITNESS),
        "--ledger", str(ledger),
        "--",
        sys.executable, "-c", "import time; time.sleep(120)",
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        birth = _wait_for(lambda: _events(_records(ledger), "birth"))
        assert birth, "no birth record for the sleep child"
        child_pid = birth[0]["pid"]
        assert not _pid_gone(child_pid), "child was already dead before the test"
        proc.kill()  # SIGKILL the supervisor: nothing catchable, no forwarding
        proc.wait(timeout=10)
        gone = _wait_for(lambda: _pid_gone(child_pid), timeout=3.0, interval=0.05)
        assert gone, (
            "child %d survived the supervisor's SIGKILL (PR_SET_PDEATHSIG missing): "
            "it would orphan the backend and hold the port" % child_pid
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# TASK B: size rotation must not lose or reorder a single record
# --------------------------------------------------------------------------- #
@LINUX_ONLY
def test_rotation_by_size_keeps_every_record_and_prunes_archives(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = tm_witness.Ledger(str(path))
    payload = "x" * 65536
    first_bytes = None
    written = 0
    total = 0
    try:
        while total <= 9 * 1024 * 1024:
            total += ledger.append({"event": "probe", "seq": written, "payload": payload})
            written += 1
            if first_bytes is None:
                first_bytes = path.read_bytes()
    finally:
        ledger.close()

    assert written > 100, "test did not write enough data to rotate: %d records" % written
    archives = sorted(p for p in path.parent.iterdir() if p.name != "ledger.jsonl")
    assert archives, "no rotated archive appeared after %d bytes" % total
    for archive in archives:
        assert archive.name.startswith("ledger.") and archive.name.endswith(".jsonl"), archive.name

    live = path.read_bytes()
    ordered = [a.read_bytes() for a in archives] + [live]
    assert ordered[0].startswith(first_bytes), (
        "the oldest archive does not start with the very first record: "
        "rotation lost or rewrote the beginning of the ledger"
    )
    seqs = []
    for chunk in ordered:
        for line in chunk.splitlines():  # a partial record would fail json.loads
            record = json.loads(line)
            if record.get("event") == "probe":
                seqs.append(record["seq"])
    assert seqs == list(range(written)), (
        "records lost/reordered across rotation boundaries: %d of %d survived"
        % (len(seqs), written)
    )

    # pruning keeps exactly N archives
    pruned_dir = tmp_path / "prune"
    pruned_dir.mkdir()
    pruned = tm_witness.Ledger(str(pruned_dir / "ledger.jsonl"),
                               rotate_bytes=4096, keep_archives=2)
    try:
        for seq in range(40):
            pruned.append({"event": "probe", "seq": seq, "payload": "y" * 1024})
    finally:
        pruned.close()
    kept = sorted(p.name for p in pruned_dir.iterdir() if p.name != "ledger.jsonl")
    assert len(kept) == 2, "expected exactly 2 kept archives, got %r" % (kept,)
    # The newest record may legitimately be the one that triggered the last
    # rotation, leaving the live file empty; what must never happen is losing
    # the newest record entirely.  Check the survivors, not the live size.
    survivors = []
    for name in kept + ["ledger.jsonl"]:
        for line in (pruned_dir / name).read_bytes().splitlines():
            if line.strip():
                survivors.append(json.loads(line)["seq"])
    assert max(survivors) == 39, "the newest record was lost by pruning: %r" % (survivors,)



# --------------------------------------------------------------------------- #
# TASK B: fork() must happen while the process is still SINGLE-THREADED
#
# The waiter thread used to be started before os.fork().  On Python 3.12 that
# makes os.fork() emit
#     DeprecationWarning: This process (pid=...) is multi-threaded, use of
#     fork() may lead to deadlocks in the child.
# A signal that arrives in that window is also recorded before the child pid is
# published, so it can only be delivered by sigwaitinfo() once the waiter runs.
# Moving waiter.start() back above the fork makes this test fail on the
# "multi-threaded" assertion.
# --------------------------------------------------------------------------- #
SLEEP_SOURCE = "import time\ntime.sleep(120)\n"


def _queued_signal_probe(ledger_path):
    """Fork-side half of block (c): block SIGTERM, queue one at *this* process,
    then prove the waiter sees it and forwards it to the child.

    This may only ever run in a forked child -- the pending SIGTERM is aimed at
    getpid(), and the pytest process must never be that pid.  Success is a
    normal return; every failure raises, for the child to report and then leave
    through os._exit().
    """
    queue = tm_witness.Ledger(ledger_path)
    dummy = None
    mask = {int(signal.SIGTERM)}
    try:
        # Spawn the dummy BEFORE installing the mask: an exec'd child inherits
        # the blocking mask, so a dummy forked after the block would ignore the
        # very SIGTERM this probe proves is forwarded.
        dummy = subprocess.Popen([sys.executable, "-c", SLEEP_SOURCE])
        signal.pthread_sigmask(signal.SIG_BLOCK, mask)
        os.kill(os.getpid(), signal.SIGTERM)  # blocked -> only queued
        assert signal.SIGTERM in signal.sigpending(), "signal was not queued"
        state = {"lock": threading.Lock(), "child_pid": dummy.pid, "pending": []}
        threading.Thread(
            target=tm_witness._signal_waiter,
            args=(queue, "queued-run", mask, state),
            name="tm-witness-sigwait-test",
            daemon=True,
        ).start()
        recorded = _wait_for(lambda: _events(_records(ledger_path), "signal"))
        assert recorded, "the waiter never saw the queued SIGTERM"
        assert recorded[0]["signo"] == int(signal.SIGTERM), recorded[0]
        assert recorded[0]["si_pid"] == os.getpid(), recorded[0]
        assert _wait_for(lambda: dummy.poll() is not None, timeout=15), (
            "the queued SIGTERM was recorded but never forwarded to the child"
        )
        assert dummy.returncode == -int(signal.SIGTERM), dummy.returncode
    finally:
        # Drain before unblocking: a still-pending SIGTERM kills whoever
        # unblocks the mask.  Here that is a throwaway child, and an empty
        # sigpending() also proves the waiter CONSUMED the queued signal.
        while signal.SIGTERM in signal.sigpending():
            signal.sigtimedwait(mask, 0.1)
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        if dummy is not None and dummy.poll() is None:
            dummy.kill()
            dummy.wait(timeout=10)
        queue.close()


@LINUX_ONLY
def test_fork_happens_before_the_waiter_thread_starts(tmp_path):
    # --- (a) a normal run must not fork from a multi-threaded process -------
    ledger = tmp_path / "single_threaded.jsonl"
    quick = _write_script(tmp_path, "quick.py", QUICK_SOURCE)
    proc, _ledger, _marker = _start(tmp_path, ledger=ledger, target=quick)
    try:
        _out, err = proc.communicate(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == 0, "witness failed: %r" % (err,)
    assert "multi-threaded" not in err, (
        "os.fork() ran in a multi-threaded process (waiter thread started "
        "before the fork): %r" % (err,)
    )

    # --- (b) SIGTERM racing the fork must still be recorded AND forwarded ---
    race_ledger = tmp_path / "race.jsonl"
    sleepy = _write_script(tmp_path, "sleepy.py", SLEEP_SOURCE)
    argv = [
        sys.executable, str(WITNESS),
        "--ledger", str(race_ledger),
        "--",
        sys.executable, str(sleepy),
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        birth = _wait_for(lambda: _events(_records(race_ledger), "birth"))
        assert birth, "no birth record"
        os.kill(proc.pid, signal.SIGTERM)  # immediately after the birth record
        _out, err = proc.communicate(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert "multi-threaded" not in err, "fork() ran multi-threaded: %r" % (err,)
    race_records = _records(race_ledger)
    signals = _events(race_records, "signal")
    assert signals, "the racy SIGTERM was never recorded: %r" % (race_records,)
    assert signals[0]["signo"] == int(signal.SIGTERM)
    assert signals[0]["si_pid"] == os.getpid(), "sender provenance lost in the race"
    exits = _events(race_records, "exit")
    assert exits, "no exit record after the racy SIGTERM"
    assert exits[-1]["signaled_by"] == int(signal.SIGTERM), (
        "the racy SIGTERM was recorded but not forwarded to the child"
    )
    assert proc.returncode == 128 + int(signal.SIGTERM)

    # --- (c) deterministic proof that a QUEUED signal is still delivered ----
    # The signal is blocked in the main thread and sent to ourselves while NO
    # waiter exists yet, so it can only sit pending in the kernel.  The waiter,
    # started afterwards, must see it and forward it -- exactly the case the old
    # ordering dropped on the floor.
    #
    # It runs in a fork()ed CHILD, never in the pytest process.  A still-pending
    # SIGTERM is a live grenade: the in-process version killed the whole run
    # with exit 143 as soon as the block unwound with the signal still queued,
    # so a broken waiter looked like a crashed CI job instead of a failed test.
    # The child is the only getpid() that signal can reach, it always leaves
    # through os._exit(), and the parent re-derives the verdict from the wait
    # status and then from the ledger.
    queue_ledger = tmp_path / "queued.jsonl"
    report = tmp_path / "queued-child-report.txt"
    queued_pid = os.fork()
    if queued_pid == 0:
        # ---- child: the ONLY target of the self-directed SIGTERM -----------
        rc = 1
        try:
            _queued_signal_probe(queue_ledger)
            rc = 0
        except BaseException:  # noqa: BLE001 -- report, then leave for good
            try:
                report.write_text("forked probe failed:\n" + traceback.format_exc())
            except BaseException:  # noqa: BLE001 -- the exit code says it anyway
                pass
        finally:
            os._exit(rc)  # never unwind into the parent's stack
    # ---- parent: no signal was ever sent to THIS process -------------------
    deadline = time.time() + 30
    status = None
    while time.time() < deadline:
        reaped, raw_status = os.waitpid(queued_pid, os.WNOHANG)
        if reaped == queued_pid:
            status = raw_status
            break
        time.sleep(0.02)
    if status is None:
        os.kill(queued_pid, signal.SIGKILL)
        os.waitpid(queued_pid, 0)
    assert status is not None, "the forked queued-signal probe never finished"
    detail = report.read_text() if report.exists() else "<no report written>"
    assert os.WIFEXITED(status), (
        "the forked probe died on a signal (wait status %r) instead of exiting: "
        "the queued SIGTERM was not consumed by the waiter.\n%s" % (status, detail)
    )
    assert os.WEXITSTATUS(status) == 0, (
        "the forked probe failed (exit %d).\n%s" % (os.WEXITSTATUS(status), detail)
    )
    queued_records = _records(queue_ledger)
    queued_signals = _events(queued_records, "signal")
    assert queued_signals, (
        "the waiter never saw the queued SIGTERM: %r" % (queued_records,)
    )
    assert queued_signals[0]["signo"] == int(signal.SIGTERM)
    assert queued_signals[0]["si_pid"] == queued_pid, (
        "sender provenance lost: the queued SIGTERM came from the forked probe "
        "(pid %d), not from %r" % (queued_pid, queued_signals[0]["si_pid"])
    )



# --------------------------------------------------------------------------- #
# FIX A: `shutdown` is decided by PROVENANCE, not by "was it signalled?"
#
# `dirty_shutdown = signaled_by is not None` calls every signalled death dirty.
# The backend we supervise is uvicorn, whose capture_signals() restores the
# original handlers and then re-raises the signal, so a SIGTERM we forwarded
# ourselves ends in a *signalled* death that is the orderly outcome.  Each test
# below pins one branch of classify_shutdown().
# --------------------------------------------------------------------------- #
def _send_and_wait(tmp_path, ledger_name, target, signo, to_child=False):
    """Run the witness on `target`, send `signo` (to the witness, or straight to
    the child pid from the birth record), and return the exit record."""
    ledger = tmp_path / ledger_name
    proc, _ledger, _marker = _start(tmp_path, ledger=ledger, target=target)
    try:
        birth = _wait_for(lambda: _events(_records(ledger), "birth"))
        assert birth, "no birth record"
        os.kill(birth[0]["pid"] if to_child else proc.pid, signo)
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    records = _records(ledger)
    exits = _events(records, "exit")
    assert exits, "no exit record; records=%r" % (records,)
    return proc, records, exits[-1]


@LINUX_ONLY
def test_forwarded_sigterm_is_an_orderly_shutdown(tmp_path):
    """A SIGTERM WE forwarded, answered by a clean exit: orderly, not dirty."""
    proc, ledger, marker, records = _term_scenario(tmp_path)
    exit_record = _events(records, "exit")[-1]
    assert exit_record["signaled_by"] is None
    assert exit_record["shutdown"] == "orderly", (
        "a SIGTERM this witness forwarded is reported as %r" % (exit_record["shutdown"],)
    )
    assert exit_record["dirty_shutdown"] is False


@LINUX_ONLY
def test_forwarded_sighup_is_unhandled_signal(tmp_path):
    """uvicorn's HANDLED_SIGNALS is (SIGINT, SIGTERM): it installs NO SIGHUP
    handler, so a HUP we forward kills the backend.  That is not orderly."""
    sleepy = _write_script(tmp_path, "sleepy.py", SLEEP_SOURCE)
    proc, records, exit_record = _send_and_wait(
        tmp_path, "hup.jsonl", sleepy, signal.SIGHUP,
    )
    assert [r["signo"] for r in _events(records, "signal")] == [int(signal.SIGHUP)]
    assert exit_record["signaled_by"] == int(signal.SIGHUP)
    assert exit_record["shutdown"] == "unhandled_signal"
    assert exit_record["dirty_shutdown"] is True
    assert proc.returncode == 128 + int(signal.SIGHUP)


@LINUX_ONLY
def test_sigterm_we_never_forwarded_is_unattributed(tmp_path):
    """Somebody killed the child behind our back: that must not be reported as
    the orderly stop we perform, and it must be dirty."""
    sleepy = _write_script(tmp_path, "sleepy.py", SLEEP_SOURCE)
    proc, records, exit_record = _send_and_wait(
        tmp_path, "direct-term.jsonl", sleepy, signal.SIGTERM, to_child=True,
    )
    assert not _events(records, "signal"), "the witness never saw that SIGTERM"
    assert exit_record["signaled_by"] == int(signal.SIGTERM)
    assert exit_record["shutdown"] == "unattributed_signal", (
        "an unwitnessed kill is reported as %r" % (exit_record["shutdown"],)
    )
    assert exit_record["dirty_shutdown"] is True


@LINUX_ONLY
def test_direct_sigkill_is_reported_as_killed(tmp_path):
    """SIGKILL can never be forwarded, so it is always somebody else's doing."""
    sleepy = _write_script(tmp_path, "sleepy.py", SLEEP_SOURCE)
    proc, records, exit_record = _send_and_wait(
        tmp_path, "direct-kill.jsonl", sleepy, signal.SIGKILL, to_child=True,
    )
    assert exit_record["signaled_by"] == int(signal.SIGKILL)
    assert exit_record["shutdown"] == "killed"
    assert exit_record["dirty_shutdown"] is True
    assert proc.returncode == 128 + int(signal.SIGKILL)


@LINUX_ONLY
def test_nonzero_exit_is_a_crash_not_a_clean_exit(tmp_path):
    """The child chose to fail: exit code 3 is a crash, and dirty."""
    crash = _write_script(tmp_path, "crash.py", "raise SystemExit(3)\n")
    ledger = tmp_path / "crash.jsonl"
    proc, _ledger, _marker = _start(tmp_path, ledger=ledger, target=crash)
    try:
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    exits = _events(_records(ledger), "exit")
    assert exits, "no exit record"
    exit_record = exits[-1]
    assert exit_record["returncode"] == 3
    assert exit_record["signaled_by"] is None
    assert exit_record["shutdown"] == "crash"
    assert exit_record["dirty_shutdown"] is True, (
        "exit code 3 was reported as a clean shutdown"
    )
    assert proc.returncode == 3


# --------------------------------------------------------------------------- #
# FIX B: live_children -- who is STILL ALIVE when the child dies
#
# `probe_leftover_children()` only reaped and returned the pids of processes
# that had ALREADY EXITED, i.e. it reported the opposite of a leftover.  The
# cases below need a survivor that stays parented to the witness: a bash
# process substitution is forked by bash BEFORE `exec "$@"`, so after the exec
# the /bin/sleep is a child of the witness, with no PR_SET_PDEATHSIG of its own.
# --------------------------------------------------------------------------- #
def _start_under_bash(tmp_path, survivor):
    """Run the witness with `survivor` set up as a process substitution."""
    ledger = tmp_path / "survivor.jsonl"
    quick = _write_script(tmp_path, "quick.py", QUICK_SOURCE)
    argv = [
        "bash", "-c", 'exec "$@" > >(%s)' % (survivor,), "bash",
        sys.executable, str(WITNESS),
        "--ledger", str(ledger),
        "--",
        sys.executable, str(quick),
    ]
    # Both streams go to /dev/null: the survivor inherits the write ends, so a
    # pipe here would never reach EOF and communicate() would hang.
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return proc, ledger


@LINUX_ONLY
def test_live_child_is_reported_in_the_death_snapshot(tmp_path):
    """A process still running when the backend dies is a real leftover."""
    proc, ledger = _start_under_bash(tmp_path, "/bin/sleep 60")
    try:
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == 0, "witness failed with %r" % (proc.returncode,)

    snapshots = _events(_records(ledger), "snapshot")
    assert snapshots, "no death-time snapshot record"
    live = snapshots[0]["live_children"]
    survivors = [child for child in live if child.get("comm") == "sleep"]
    try:
        assert survivors, (
            "the surviving /bin/sleep 60 was not reported in live_children: %r" % (live,)
        )
        assert survivors[0]["cmdline"] == ["/bin/sleep", "60"], survivors[0]
        assert isinstance(survivors[0]["pid"], int)
    finally:
        for child in survivors:
            try:
                os.kill(child["pid"], signal.SIGKILL)
            except OSError:
                pass


@LINUX_ONLY
def test_exited_child_is_not_reported_as_a_leftover(tmp_path):
    """A child that already exited is a zombie, not a leftover: reporting the
    pids of processes we merely have not reaped yet is exactly the old bug."""
    proc, ledger = _start_under_bash(tmp_path, "/bin/sh -c 'exit 0'")
    try:
        proc.wait(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == 0, "witness failed with %r" % (proc.returncode,)
    snapshots = _events(_records(ledger), "snapshot")
    assert snapshots, "no death-time snapshot record"
    assert snapshots[0]["live_children"] == [], (
        "an already-exited child was reported as a leftover: %r"
        % (snapshots[0]["live_children"],)
    )


@LINUX_ONLY
def test_read_git_head_matches_git_rev_parse():
    """read_git_head() parses git's own files instead of spawning git, so it
    must still agree with the answer git itself gives for this checkout."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    try:
        git = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # no git installed
        pytest.skip("git is unavailable: %r" % (exc,))
    if git.returncode != 0:
        pytest.skip("git cannot resolve HEAD here: %s" % git.stderr.strip()[:200])
    assert tm_witness.read_git_head() == git.stdout.strip()


def test_unsupported_host_is_refused_before_anything_is_forked(tmp_path, capsys):
    """A host that cannot attribute a signal must be REFUSED, not half-started.

    Runs on every platform and is deliberately NOT marked LINUX_ONLY: the
    interesting case is precisely the host that cannot supervise, and a skipped
    test would make that case unobservable.  Both capabilities are simulated in
    turn -- deleting ``signal.sigwaitinfo`` is the real absence, and stubbing the
    boot-id read covers the other probe without needing a fake /proc.
    """
    marker = tmp_path / "child-ran"
    command = [sys.executable, "-c", "open(%r, 'w').write('ran')" % str(marker)]

    cases = [
        (
            "sigwaitinfo",
            tmp_path / "no-sigwaitinfo.jsonl",
            # raising=False: on a host where the capability is ALREADY absent this
            # test must still run (that is the host it exists for), so deleting it
            # must be a no-op rather than an AttributeError.
            lambda mp: mp.delattr(signal, "sigwaitinfo", raising=False),
        ),
        (
            "boot_id",
            tmp_path / "no-boot-id.jsonl",
            lambda mp: mp.setattr(tm_witness, "read_boot_id", lambda: None),
        ),
    ]

    for capability, ledger, break_capability in cases:
        # The capability must stay broken for the whole run, not merely while it is
        # asserted on: the detector and ``main`` are both read inside this block.
        # (Collecting the cases first and running them afterwards would silently
        # restore the capability and turn this into a test of a working host.)
        with pytest.MonkeyPatch.context() as mp:
            break_capability(mp)
            assert tm_witness.unsupported_reason() is not None, (
                "the detector did not notice that %s is unusable" % (capability,)
            )
            rc = tm_witness.main(["--ledger", str(ledger), "--"] + command)
            err = capsys.readouterr().err

            assert rc == tm_witness.EX_UNSUPPORTED, (
                "refusal used exit %r, not the documented %r (EX_CONFIG)"
                % (rc, tm_witness.EX_UNSUPPORTED)
            )
            # The refusal must name the capability that is really missing.  On a
            # host where signal.sigwaitinfo is genuinely absent the boot-id case
            # can never be reached -- the first probe refuses -- so sigwaitinfo is
            # then the only honest name, and demanding the exact sentence keeps
            # that escape hatch from swallowing a real misattribution.
            named = capability in err or (
                capability == "boot_id"
                and "signal.sigwaitinfo is missing" in err
                and not hasattr(signal, "sigwaitinfo")
            )
            assert named, (
                "stderr does not name the missing capability %r: %r" % (capability, err)
            )
            assert "provenance" in err, (
                "stderr does not say what is impossible: %r" % (err,)
            )

            # (c) nothing was forked: no ledger at all, therefore no birth record,
            # and the command never ran.
            assert not ledger.exists(), (
                "the ledger was created although the witness refused to supervise: %r"
                % (ledger,)
            )
            births = _events(_records(ledger), "birth") if ledger.exists() else []
            assert births == [], "a birth record was written: %r" % (births,)
            assert not marker.exists(), (
                "the command ran although the witness cannot supervise: %r" % (marker,)
            )
