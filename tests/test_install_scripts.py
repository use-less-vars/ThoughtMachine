"""Regression tests for scripts/doctor_checks.py (start-script helpers).

All tests mock subprocess execution (_run); no real docker daemon, `ss`,
or pip is required. scripts/ has no __init__.py, so it is a namespace
package importable from the repo root.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.doctor_checks as doctor_checks


def test_start_script_doctor_detects_missing_docker_group(monkeypatch):
    def fake_run(cmd, timeout=60):
        assert cmd == ["docker", "info"]
        return subprocess.CompletedProcess(
            cmd,
            1,
            "",
            "Got permission denied while trying to connect to the "
            "Docker daemon socket at unix:///var/run/docker.sock: "
            "Get http://%2Fvar%2Frun%2Fdocker.sock/v1.45/info: "
            "dial unix /var/run/docker.sock: connect: permission denied",
        )

    monkeypatch.setattr(doctor_checks, "_run", fake_run)

    result = doctor_checks.check_docker_daemon()

    assert result["ok"] is False
    assert result["reason"] == "permission_denied"
    assert "permission denied" in result["detail"]


def test_start_script_doctor_detects_busy_port(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd == ["ss", "-ltn"]:
            stdout = (
                "State   Recv-Q  Send-Q  Local Address:Port  Peer Address:Port\n"
                "LISTEN  0       128     0.0.0.0:8000         0.0.0.0:*\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        if cmd == ["ss", "-ltnp"]:
            stdout = (
                "State   Recv-Q  Send-Q  Local Address:Port  Peer Address:Port  Process\n"
                'LISTEN  0       128     0.0.0.0:8000         0.0.0.0:*          '
                'users:(("uvicorn",pid=1234,fd=3))\n'
            )
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(doctor_checks, "_run", fake_run)

    result = doctor_checks.check_port_free(8000)

    assert result["ok"] is False
    assert result["reason"] == "in_use"
    assert "8000" in result["detail"]
    assert calls == [["ss", "-ltn"], ["ss", "-ltnp"]]


def test_installer_script_is_idempotent(exec_tmp, monkeypatch):
    venv = exec_tmp / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    # Fixture divergence: doctor_checks now classifies a venv as BROKEN when
    # bin/python is not executable or pyvenv.cfg is missing (un-gated
    # predicates), so the fake venv gets +x and a pyvenv.cfg to stay
    # structurally healthy and exercise the 'updated' path as before.
    (venv / "bin" / "python").chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "pip").write_text("#!/bin/sh\n")
    requirements = exec_tmp / "requirements.txt"
    requirements.write_text("fastapi>=0.100\n")

    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(doctor_checks, "_run", fake_run)

    first = doctor_checks.ensure_venv(str(venv), str(requirements))
    assert first["ok"] is True
    assert first["changed"] is True

    second = doctor_checks.ensure_venv(str(venv), str(requirements))
    assert second["ok"] is True
    assert second["changed"] is False
    assert "up to date" in second["detail"]

    pip_installs = [c for c in calls if c[0].endswith("pip") and c[1] == "install"]
    assert len(pip_installs) == 1


# ---------------------------------------------------------------------------
# start_thoughtmachine.sh integration tests (shim-based)
#
# A fake `python3` is prepended to PATH so every `python3` invocation from the
# shell script (doctor_checks.py calls, `python3 -m thoughtmachine.doctor`,
# json_get's `-c` snippet) is answered by the shim without touching the real
# interpreter/daemon. The shim's shebang is the REAL test-runner interpreter
# (absolute path), so there is no PATH recursion.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
EXEC_TMP = REPO_ROOT / ".tmp-test"


@pytest.fixture()
def exec_tmp():
    """Repo-local scratch dir (REPO_ROOT/.tmp-test).

    /tmp (and TMPDIR) may be mounted noexec on this host, so scripts and
    shims that must be *executed* by the shell cannot live there. A
    repo-local dir sits on an executable filesystem and is git-ignored.
    """
    if EXEC_TMP.exists():
        shutil.rmtree(EXEC_TMP)
    EXEC_TMP.mkdir(parents=True)
    yield EXEC_TMP
    if EXEC_TMP.exists():
        shutil.rmtree(EXEC_TMP)

DOCTOR_SHIM_TEMPLATE = """\
import json
import sys

REASON = "__REASON__"


def _emit(payload):
    print(json.dumps(payload))
    sys.exit(0 if payload.get("ok") else 1)


args = sys.argv[1:]

if args and args[0].endswith("doctor_checks.py"):
    flag = args[1]
    if flag == "--check-tools":
        _emit({
            "ok": True,
            "critical_missing": [],
            "tools": {
                "docker": {
                    "present": False,
                    "critical": False,
                    "hint": "apt-get install docker.io",
                }
            },
            "docker_present": False,
            "docker_hint": "apt-get install docker.io",
        })
    elif flag == "--ensure-venv":
        _emit({"ok": True, "changed": False, "broken_reason": "", "detail": "up to date"})
    elif flag == "--check-docker":
        _emit({"ok": False, "reason": REASON, "detail": "Cannot connect to the Docker daemon"})
    elif flag == "--check-port":
        _emit({"ok": True})
    elif flag == "--check-node":
        _emit({"ok": True, "reason": "", "detail": "", "version": "20.0.0"})
    elif flag == "--check-dotthoughtmachine":
        _emit({"ok": True})
    elif flag == "--check-python":
        _emit({"ok": True, "reason": "", "detail": "python3 3.11.9 meets the >= 3.11 requirement", "version": "3.11.9"})
    elif flag == "--check-stale-containers":
        _emit({"ok": True, "count": 0, "containers": [], "detail": "no stale ThoughtMachine containers"})
    elif flag == "--clean-stale-containers":
        _emit({"ok": True, "changed": False, "removed": [], "failed": [], "detail": "no stale ThoughtMachine containers to remove"})
    sys.exit(1)

if args[:2] == ["-m", "thoughtmachine.doctor"]:
    print("ThoughtMachine system health table")
    print("  Docker daemon    | FAIL")
    sys.exit(0)

if args and args[0] == "-c":
    sys.argv = ["-c"] + args[2:]
    exec(args[1], globals())
    sys.exit(0)

sys.exit(1)
"""


def _make_repo(base):
    """Copy the real start script into an isolated repo dir."""
    repo = base / "repo"
    repo.mkdir()
    shutil.copy(REPO_ROOT / "start_thoughtmachine.sh", repo / "start_thoughtmachine.sh")
    return repo


def _write_shim(base, reason):
    shim_dir = base / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(
        f"#!{sys.executable}\n" + DOCTOR_SHIM_TEMPLATE.replace("__REASON__", reason)
    )
    shim.chmod(0o755)
    return shim_dir


def _run_script(repo, base, reason, extra_args=("--check-only",)):
    shim_dir = _write_shim(base, reason)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(base)
    return subprocess.run(
        ["bash", str(repo / "start_thoughtmachine.sh")] + list(extra_args),
        capture_output=True,
        text=True,
        env=env,
    )


def test_check_only_tolerates_docker_daemon_down(exec_tmp):
    repo = _make_repo(exec_tmp)
    result = _run_script(repo, exec_tmp, "daemon_down")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: Docker is not usable" in result.stdout
    assert "continuing in --check-only mode" in result.stdout
    assert "(--check-only: preflight done, nothing was started)" in result.stdout


def test_check_only_tolerates_docker_lib_missing(exec_tmp):
    repo = _make_repo(exec_tmp)
    result = _run_script(repo, exec_tmp, "lib_missing")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: Docker is not usable" in result.stdout
    assert "NOTE: docker not found" in result.stdout


def test_check_only_doctor_table_still_reports_docker_fail(exec_tmp):
    repo = _make_repo(exec_tmp)
    result = _run_script(repo, exec_tmp, "daemon_down")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" in result.stdout


def test_normal_mode_still_fails_without_docker(exec_tmp):
    repo = _make_repo(exec_tmp)
    result = _run_script(repo, exec_tmp, "lib_missing", extra_args=())
    assert result.returncode == 1
    assert "FAILED" in result.stdout


def test_script_prepends_system_paths_for_docker_detection():
    source = (REPO_ROOT / "start_thoughtmachine.sh").read_text()
    assert 'export PATH="/usr/bin:$PATH"' in source
    assert 'export PATH="/usr/sbin:$PATH"' in source
