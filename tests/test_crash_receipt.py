"""Crash-receipt tests: the witness must not be the victim.

T1  ``decode_wait_status()`` maps a raw bash wait status to a cause
    (0 -> "exit 0", 143 -> "signal 15 SIGTERM", 137 -> "signal 9 SIGKILL").
T2  the launcher-shaped wait path records a SIGKILLed backend: the exact
    original shape ``( exec cmd ) > >(tee) &; PID=$!; wait; RC=$?`` against a
    stub that kills itself with -9 must leave a ``cause=signal 9 SIGKILL``
    receipt *plus* the tail of the startup log.
T3  rotate-then-fresh: the previous startup log survives under a rotated name
    and only the newest five rotations are kept.
T4  ``kill_thoughtmachine.sh`` writes the ``kill_switch`` receipt (pid + command
    line) from its OWN directory for a real listener on a port the test injects
    into a COPY of the script (the shipped switch stays hard-coded to all six
    real ports), before the (still absolute) kill; with no listener it says so
    instead of claiming to have stopped something.

Every test drives the REAL scripts, copied into ``tmp_path``; nothing here
writes to the repository's own ``logs/`` directory.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
START_SH = REPO_ROOT / "start_thoughtmachine.sh"
KILL_SH = REPO_ROOT / "kill_thoughtmachine.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

BACKEND_EXIT_RE = re.compile(
    r"^backend_exit\|ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}"
    r"\|pid=\d+\|status=(\d+)\|cause=(.+)\|launcher=\d+"
    r"\|pgid=\d+\|sid=\d+\|cwd=.*$"
)


def _app_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the launcher, so SCRIPT_DIR (and logs/) is tmp_path."""
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy2(START_SH, app / "start_thoughtmachine.sh")
    return app


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 60)
    return subprocess.run(cmd, **kw)


def _source(app: Path, body: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``body`` in bash with the launcher's receipt library already sourced.

    Sourcing is self-detecting (``${BASH_SOURCE[0]} != $0``), so it defines the
    functions and returns; there is no environment variable that could make the
    launcher skip launching, which is exactly what these tests rely on.
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    script = app / "start_thoughtmachine.sh"
    return _run([BASH, "-c", 'source "%s"\n%s' % (script, body)], env=env, cwd=str(app))


def _receipt(app: Path) -> Path:
    return app / "logs" / "backend_death.log"


# --------------------------------------------------------------------------- T1
def test_t1_decode_wait_status(tmp_path):
    app = _app_dir(tmp_path)
    proc = _source(
        app,
        'for s in 0 1 130 137 143 255; do\n'
        '    printf "%s => %s\\n" "$s" "$(decode_wait_status "$s")"\n'
        "done\n",
    )
    assert proc.returncode == 0, proc.stderr
    lines = dict(
        line.split(" => ", 1) for line in proc.stdout.strip().splitlines() if " => " in line
    )
    assert lines == {
        "0": "exit 0",
        "1": "exit 1",
        "130": "signal 2 SIGINT",
        "137": "signal 9 SIGKILL",
        "143": "signal 15 SIGTERM",
        "255": "exit 255",
    }


# --------------------------------------------------------------------------- T2
def test_t2_launcher_shaped_wait_records_sigkill(tmp_path):
    app = _app_dir(tmp_path)
    stub = app / "stub_backend.sh"
    stub.write_text(
        "#!/bin/bash\n"
        'echo "stub: hello from stub"\n'
        'echo "stub: about to die" >&2\n'
        "sleep 1\n"
        "kill -9 $$\n"
    )
    stub.chmod(0o755)

    # The original launch/wait shape, verbatim, plus the receipt calls.  The stub
    # is started *through* bash: some sandboxes mount tmp noexec and the shape
    # under test (a child that wait() reaps) is identical either way.
    fixture = app / "fixture.sh"
    fixture.write_text(
        "set -u\n"
        'source "%s"\n'
        'BACKEND_LOG="$SCRIPT_DIR/logs/backend_startup.log"\n'
        'mkdir -p "$SCRIPT_DIR/logs"\n'
        "trap 'record_launcher_exit $?' EXIT\n"
        '( exec "%s" "%s" ) > >(tee -a "$BACKEND_LOG") 2>&1 &\n'
        "BACKEND_PID=$!\n"
        "record_topology\n"
        'wait "$BACKEND_PID"\n'
        "BACKEND_RC=$?\n"
        'record_backend_exit "$BACKEND_RC"\n'
        'exit "$BACKEND_RC"\n' % (app / "start_thoughtmachine.sh", BASH, stub)
    )

    proc = _run([BASH, str(fixture)], env=dict(os.environ), cwd=str(app))

    assert proc.returncode == 137, proc.stdout + proc.stderr
    log = _receipt(app)
    assert log.is_file(), proc.stdout + proc.stderr
    text = log.read_text()
    exits = [ln for ln in text.splitlines() if ln.startswith("backend_exit|")]
    assert len(exits) == 1, text  # idempotent: the EXIT trap did not add a second
    m = BACKEND_EXIT_RE.match(exits[0])
    assert m, exits[0]
    assert m.group(1) == "137"
    assert m.group(2) == "signal 9 SIGKILL"
    # the startup log tail is captured, so the receipt explains itself
    tail = [ln[len("tail|"):] for ln in text.splitlines() if ln.startswith("tail|")]
    assert "stub: hello from stub" in tail, text
    assert "stub: about to die" in tail, text
    # once per launch: who launched whom
    topo = [ln for ln in text.splitlines() if ln.startswith("topology|")]
    assert any("|role=launcher|" in ln for ln in topo), text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- T3
def test_t3_rotation_preserves_previous_log_and_keeps_five(tmp_path):
    app = _app_dir(tmp_path)
    body = [
        "set -u",
        'BACKEND_LOG="$SCRIPT_DIR/logs/backend_startup.log"',
        'printf "run-one-line\\n" > "$BACKEND_LOG"',
        'touch -t 202001010000.01 "$BACKEND_LOG"',
        "rotate_backend_log",
        # captured now: the later rotations prune this file away
        'first="$(ls -1 "$SCRIPT_DIR"/logs/backend_startup.*.log)"',
        'echo "after-first=$(printf "%s\\n" "$first" | wc -l)"',
        'echo "first-name=$(basename "$first")"',
        'echo "first-content=$(cat "$first")"',
        'echo "current-empty=$([ -s "$BACKEND_LOG" ] && echo no || echo yes)"',
        'echo "current-exists=$([ -e "$BACKEND_LOG" ] && echo yes || echo no)"',
        'for i in 2 3 4 5 6 7; do',
        '    printf "run-%s\\n" "$i" > "$BACKEND_LOG"',
        '    touch -t "202001010000.0$i" "$BACKEND_LOG"',
        "    rotate_backend_log",
        "done",
        'echo "after-seven=$(ls -1 "$SCRIPT_DIR"/logs/backend_startup.*.log | wc -l)"',
        'echo "survivors=$(for f in "$SCRIPT_DIR"/logs/backend_startup.*.log; do cat "$f"; done | tr "\\n" ",")"',
    ]
    proc = _source(app, "\n".join(body) + "\n")
    assert proc.returncode == 0, proc.stderr
    out = dict(
        line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line
    )

    # (a) rotate-then-fresh: the first log survives, byte for byte, under a new name
    assert out["after-first"] == "1"
    assert out["first-name"].startswith("backend_startup.2")  # UTC ISO-8601 stamp
    assert out["first-content"] == "run-one-line"
    assert out["current-empty"] == "yes"
    assert out["current-exists"] == "yes"  # $BACKEND_LOG keeps its exact path

    # (b) the newest five survive, the two oldest are pruned
    assert out["after-seven"] == "5"
    assert out["survivors"] == "".join("run-%d," % i for i in (3, 4, 5, 6, 7))


# --------------------------------------------------------------------------- T4
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("listener on port %d never came up" % port)


def _fuser_env(match_port, pid: int) -> dict:
    """A ``fuser`` shim needing neither psmisc nor an executable file.

    Some sandboxes ship no psmisc and mount tmp noexec, so a PATH script could
    not run there; bash imports this function from the environment instead.  It
    emulates exactly what kill_thoughtmachine.sh uses: "<port>/tcp" -> the
    listening pid, and "-k <port>/tcp" -> the same pid plus the kill.
    """
    if match_port is None:
        return {"BASH_FUNC_fuser%%": "() { return 0; }"}
    body = (
        "() {\n"
        "    local kill_mode=0 matched= a\n"
        '    for a in "$@"; do\n'
        '        case "$a" in\n'
        "            -k) kill_mode=1 ;;\n"
        '            "%(port)d/tcp") matched=1 ;;\n'
        "        esac\n"
        "    done\n"
        '    [ -n "$matched" ] || return 0\n'
        '    echo " %(pid)d"\n'
        '    if [ "$kill_mode" = "1" ]; then kill -9 %(pid)d 2>/dev/null; fi\n'
        "    return 0\n"
        "}"
    ) % {"port": match_port, "pid": pid}
    return {"BASH_FUNC_fuser%%": body}


# The shipped kill switch must stay absolute: six real ports, no override.
SHIPPED_PORTS = (5173, 5174, 5175, 5176, 5177, 8000)
SHIPPED_PORTS_LINE = "PORTS=(%s)" % " ".join("%d/tcp" % p for p in SHIPPED_PORTS)


def _kill_script_app(tmp_path: Path, port: int) -> Path:
    """Copy the kill switch, rewriting ONLY its hard-coded port array.

    The shipped script has no port override on purpose (a narrower kill switch
    would be a different control), so the test rewrites that one array line in
    its own copy and asserts the copy differs from the shipped file in exactly
    that line -- and that the shipped file still names all six real ports.  One
    of those ports cannot be bound in a sandbox, so the copy's array becomes a
    single free port; the shipped file is never touched.
    """
    app = tmp_path / "app"
    app.mkdir()
    shipped = KILL_SH.read_text()
    assert SHIPPED_PORTS_LINE in shipped, shipped
    assert "TM_KILL_PORTS" not in shipped, "the kill switch must not be narrowable"
    for p in SHIPPED_PORTS:
        assert "%d/tcp" % p in shipped, p

    replacement = "PORTS=(%d/tcp)" % port
    (app / "kill_thoughtmachine.sh").write_text(
        shipped.replace(SHIPPED_PORTS_LINE, replacement)
    )

    before = shipped.splitlines()
    after = (app / "kill_thoughtmachine.sh").read_text().splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 1, differing
    assert before[differing[0]] == SHIPPED_PORTS_LINE
    assert after[differing[0]] == replacement
    return app


def test_t4_kill_switch_receipt_names_the_listener(tmp_path):
    port = _free_port()
    app = _kill_script_app(tmp_path, port)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    listener = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(elsewhere),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(port)
        env = dict(os.environ)
        env.update(_fuser_env(port, listener.pid))
        # cwd is deliberately NOT the script's directory
        proc = _run([BASH, str(app / "kill_thoughtmachine.sh")], env=env, cwd=str(elsewhere))
        assert proc.returncode == 0, proc.stdout + proc.stderr

        log = app / "logs" / "backend_death.log"  # resolved from the SCRIPT's dir
        assert log.is_file(), proc.stdout + proc.stderr
        assert not (elsewhere / "logs").exists()
        assert stat.S_IMODE(log.stat().st_mode) == 0o600

        records = [ln for ln in log.read_text().splitlines() if ln.startswith("kill_switch|")]
        assert len(records) == 1, log.read_text()
        rec = records[0]
        assert re.match(r"^kill_switch\|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}\|pids=", rec), rec
        assert "pids=%d" % listener.pid in rec, rec
        assert "cmds=" in rec and "http.server" in rec, rec
        assert "All ThoughtMachine processes stopped." in proc.stdout

        # the kill stays absolute: the named listener is really gone
        assert listener.wait(timeout=10) == -9
    finally:
        if listener.poll() is None:
            listener.kill()
            listener.wait()


def test_t4_kill_switch_is_truthful_when_nothing_listens(tmp_path):
    port = _free_port()
    app = _kill_script_app(tmp_path, port)

    env = dict(os.environ)
    env.update(_fuser_env(None, 999999))
    proc = _run([BASH, str(app / "kill_thoughtmachine.sh")], env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert "All ThoughtMachine processes stopped." not in proc.stdout
    assert (
        "No ThoughtMachine process was listening on ports: "
        "5173 5174 5175 5176 5177 8000 - nothing to stop." in proc.stdout
    )
    records = [
        ln for ln in (app / "logs" / "backend_death.log").read_text().splitlines()
        if ln.startswith("kill_switch|")
    ]
    assert len(records) == 1
    assert records[0].endswith("|pids=|cmds=")
