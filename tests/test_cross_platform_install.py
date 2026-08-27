"""Cross-platform installer tests: install.sh (Linux) via shims.

The real install.sh / start_thoughtmachine.sh are copied into a repo-local
scratch dir (REPO_ROOT/.tmp-test -- /tmp may be noexec) and run under
`bash` with a fake PATH (shim `python3` + fake `sg`) so no real Docker
daemon, Python or Node is required.

Shim outputs are stateful where the real doctor is stateful: the
--ensure-venv branch records a stamp file in the CWD so a second install
run reports "up to date" (idempotency).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXEC_TMP = REPO_ROOT / ".tmp-test"


@pytest.fixture()
def exec_tmp():
    if EXEC_TMP.exists():
        shutil.rmtree(EXEC_TMP)
    EXEC_TMP.mkdir(parents=True)
    yield EXEC_TMP
    if EXEC_TMP.exists():
        shutil.rmtree(EXEC_TMP)


INSTALL_SHIM_TEMPLATE = """\
import json
import os
import sys


def _emit(payload):
    print(json.dumps(payload))
    sys.exit(0 if payload.get("ok") else 1)


args = sys.argv[1:]
if args and args[0].endswith("doctor_checks.py"):
    flag = args[1]
    if flag == "--check-python":
        _emit({"ok": True, "reason": "", "detail": "python3 3.11.9 meets the >= 3.11 requirement", "version": "3.11.9"})
    elif flag == "--check-docker":
        _emit({"ok": True, "reason": "", "detail": "docker info succeeded"})
    elif flag == "--ensure-docker-daemon":
        _emit({"ok": True, "changed": False, "detail": "docker daemon is already running"})
    elif flag == "--check-docker-group":
        _emit({"ok": True, "detail": "user is a member of group docker"})
    elif flag == "--ensure-docker-group":
        _emit({"ok": True, "changed": False, "detail": "user is already a member of group docker"})
    elif flag == "--ensure-venv":
        stamp = ".tm-shim-venv-stamp"
        if os.path.exists(stamp):
            _emit({"ok": True, "changed": False, "broken_reason": "", "detail": "virtualenv .venv ready; dependencies up to date"})
        else:
            with open(stamp, "w") as fh:
                fh.write("shim\\n")
            _emit({"ok": True, "changed": True, "broken_reason": "", "detail": "virtualenv .venv created and populated"})
    elif flag == "--check-node":
        _emit({"ok": True, "reason": "", "detail": "node v20.0.0 (major 20) meets the >= 18 requirement", "version": "20.0.0"})
    sys.exit(1)

if args and args[0] == "-c":
    sys.argv = ["-c"] + args[2:]
    exec(args[1], globals())
    sys.exit(0)

sys.exit(1)
"""


def _write_shim(base, template):
    shim_dir = base / "bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "python3"
    shim.write_text(f"#!{sys.executable}\n" + template)
    shim.chmod(0o755)
    return shim_dir


def _write_fake_sg(base):
    shim_dir = base / "bin"
    shim_dir.mkdir(exist_ok=True)
    sg = shim_dir / "sg"
    sg.write_text('#!/bin/sh\nexec /bin/sh -c "$3"\n')
    sg.chmod(0o755)
    return shim_dir


def _write_fake_uname(base, kernel, machine):
    shim_dir = base / "bin"
    shim_dir.mkdir(exist_ok=True)
    uname = shim_dir / "uname"
    uname.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-s" ]; then echo "%s"\n'
        'elif [ "$1" = "-m" ]; then echo "%s"\n'
        'else exit 1\n'
        'fi\n' % (kernel, machine)
    )
    uname.chmod(0o755)
    return shim_dir


def _write_fake_sed(base, distro_id):
    shim_dir = base / "bin"
    shim_dir.mkdir(exist_ok=True)
    sed = shim_dir / "sed"
    # Mimics `sed -n 's/^ID=//p' /etc/os-release` output for the gate.
    sed.write_text('#!/bin/sh\necho "%s"\n' % distro_id)
    sed.chmod(0o755)
    return shim_dir


def _run_installer(base):
    repo = base / "repo"
    repo.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "install.sh", repo / "install.sh")
    shim_dir = _write_shim(base, INSTALL_SHIM_TEMPLATE)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(base)
    return subprocess.run(
        ["bash", str(repo / "install.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def _run_installer_platform(base, kernel, machine, distro_id):
    repo = base / "repo"
    repo.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "install.sh", repo / "install.sh")
    shim_dir = _write_shim(base, INSTALL_SHIM_TEMPLATE)
    _write_fake_uname(base, kernel, machine)
    if distro_id is not None:
        _write_fake_sed(base, distro_id)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(base)
    return subprocess.run(
        ["bash", str(repo / "install.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def test_install_sh_is_idempotent(exec_tmp):
    first = _run_installer(exec_tmp)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Next step: ./start_thoughtmachine.sh" in first.stdout
    assert "created and populated" in first.stdout
    assert first.stdout.count("[ok]") >= 4, first.stdout

    second = _run_installer(exec_tmp)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Next step: ./start_thoughtmachine.sh" in second.stdout
    assert "up to date" in second.stdout
    assert "created and populated" not in second.stdout
    assert second.stdout.count("[ok]") >= 4, second.stdout


def test_install_sh_rejects_macos(exec_tmp):
    result = _run_installer_platform(exec_tmp, "Darwin", "x86_64", None)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "macOS is not supported" in result.stdout


def test_install_sh_rejects_windows(exec_tmp):
    result = _run_installer_platform(exec_tmp, "MINGW64_NT-10.0", "x86_64", None)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "use install_thoughtmachine.bat" in result.stdout


def test_install_sh_rejects_unsupported_arch(exec_tmp):
    result = _run_installer_platform(exec_tmp, "Linux", "arm64", None)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unsupported architecture" in result.stdout


def test_install_sh_rejects_unsupported_distro(exec_tmp):
    result = _run_installer_platform(exec_tmp, "Linux", "x86_64", "archlinux")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unsupported distribution" in result.stdout


START_SHIM_TEMPLATE = """\
import json
import sys


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
            "tools": {"docker": {"present": True, "critical": False, "hint": "sudo apt-get install docker.io"}},
            "docker_present": True,
            "docker_hint": "sudo apt-get install docker.io",
        })
    elif flag == "--ensure-venv":
        _emit({"ok": True, "changed": False, "broken_reason": "", "detail": "up to date"})
    elif flag == "--check-docker":
        _emit({"ok": False, "reason": "permission_denied", "detail": "Got permission denied while trying to connect to the Docker daemon socket"})
    elif flag == "--check-port":
        _emit({"ok": True, "detail": "port is free"})
    elif flag == "--check-node":
        _emit({"ok": True, "reason": "", "detail": "", "version": "20.0.0"})
    elif flag == "--check-dotthoughtmachine":
        _emit({"ok": True})
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


def _run_start_script(base, extra_args=("--check-only",), with_sg=False):
    repo = base / "repo"
    repo.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "start_thoughtmachine.sh", repo / "start_thoughtmachine.sh")
    shim_dir = _write_shim(base, START_SHIM_TEMPLATE)
    if with_sg:
        _write_fake_sg(base)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(base)
    return subprocess.run(
        ["bash", str(repo / "start_thoughtmachine.sh")] + list(extra_args),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def test_start_script_check_only_tolerates_docker_permission_denied(exec_tmp):
    result = _run_start_script(exec_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: Docker permission problem" in result.stdout
    assert "continuing in --check-only mode" in result.stdout
    assert "[8/8] All checks passed." in result.stdout
    assert "(--check-only: preflight done, nothing was started)" in result.stdout


def test_start_script_reexec_via_sg_fails_without_group(exec_tmp):
    result = _run_start_script(exec_tmp, extra_args=(), with_sg=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED: Docker permission problem persists" in result.stdout
    assert "newgrp docker" in result.stdout
