"""Distro-agnostic tool/package-manager contract tests.

Covers the fix that removes the Debian/Ubuntu bias from the installer:

  * ``scripts/doctor_checks.py::check_tools`` -- the package manager is
    ADVISORY, never critical: ``ok`` / ``critical_missing`` must not depend on
    it, and it is reported only under the additive ``advisory`` key.
  * ``install.sh`` -- the package manager is detected by CAPABILITY (the first
    available manager from a fixed candidate order), never by distro; the
    detected manager selects the docker-install command.

The install.sh tests reuse the fake-PATH approach of
tests/test_cross_platform_install.py, but with a FULLY controlled PATH (only
the shim dir) so ``command -v <manager>`` reflects exactly the managers the
test chooses -- the host's real apt-get is deliberately NOT reachable.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.doctor_checks as doctor_checks

REPO_ROOT = Path(__file__).resolve().parents[1]
EXEC_TMP = REPO_ROOT / ".tmp-test-pm"
BASH = shutil.which("bash") or "/bin/bash"

_CRITICAL_TOOLS = ("python3", "node", "npm", "ss", "sg")
_MANAGERS = ("apt-get", "dnf", "pacman", "zypper", "apk")


# ---------------------------------------------------------------------------
# check_tools: the package manager is advisory, never critical
# ---------------------------------------------------------------------------

def test_check_tools_package_manager_is_advisory_not_critical(monkeypatch):
    # Every critical tool present; apt-get is ABSENT and dnf is the only
    # package manager on PATH.
    def fake_which(name):
        if name in _CRITICAL_TOOLS:
            return "/usr/bin/" + name
        if name == "dnf":
            return "/usr/bin/dnf"
        return None

    monkeypatch.setattr(doctor_checks.shutil, "which", fake_which)

    result = doctor_checks.check_tools()

    assert result["ok"] is True
    assert result["critical_missing"] == []
    # A package manager is NEVER part of the critical tool table...
    for manager in _MANAGERS:
        assert manager not in result["tools"]
    # ...it is reported only in the additive advisory category.
    pm = result["advisory"]["package_manager"]
    assert pm["present"] is True
    assert pm["name"] == "dnf"
    assert pm["hint"]


def test_check_tools_without_any_package_manager_still_ok(monkeypatch):
    def fake_which(name):
        if name in _CRITICAL_TOOLS:
            return "/usr/bin/" + name
        return None  # no apt-get / dnf / pacman / zypper / apk anywhere

    monkeypatch.setattr(doctor_checks.shutil, "which", fake_which)

    result = doctor_checks.check_tools()

    assert result["ok"] is True
    assert result["critical_missing"] == []
    pm = result["advisory"]["package_manager"]
    assert pm["present"] is False
    assert pm["name"] is None
    assert "no package manager" in pm["hint"].lower()


def test_check_tools_payload_is_superset_of_old_contract(monkeypatch):
    # A host with every critical tool: all old keys remain, tools keeps its
    # {present, critical, hint} shape, docker stays non-critical, and the only
    # addition is the advisory category.
    def fake_which(name):
        return "/usr/bin/" + name if name in _CRITICAL_TOOLS else None

    monkeypatch.setattr(doctor_checks.shutil, "which", fake_which)

    result = doctor_checks.check_tools()

    assert set(result) >= {
        "ok", "detail", "tools", "critical_missing",
        "docker_present", "docker_hint", "advisory",
    }
    for name in _CRITICAL_TOOLS:
        entry = result["tools"][name]
        assert set(entry) == {"present", "critical", "hint"}
        assert entry["present"] is True
        assert entry["critical"] is True
    assert result["tools"]["docker"]["critical"] is False


# ---------------------------------------------------------------------------
# install.sh: capability detection + manager-keyed docker-install command
# ---------------------------------------------------------------------------

# Fake doctor: python passes, docker CLI is MISSING (lib_missing) so the
# docker-install fallback path is reached; `-c` serves install.sh's json_get.
PM_SHIM_TEMPLATE = """\
import json
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
        _emit({"ok": False, "reason": "lib_missing", "detail": "docker CLI not found on PATH"})
    sys.exit(1)

if args and args[0] == "-c":
    sys.argv = ["-c"] + args[2:]
    exec(args[1], globals())
    sys.exit(0)

sys.exit(1)
"""


@pytest.fixture()
def exec_tmp():
    if EXEC_TMP.exists():
        shutil.rmtree(EXEC_TMP)
    EXEC_TMP.mkdir(parents=True)
    yield EXEC_TMP
    if EXEC_TMP.exists():
        shutil.rmtree(EXEC_TMP)


def _make_install_repo(base):
    repo = base / "repo"
    repo.mkdir()
    shutil.copy(REPO_ROOT / "install.sh", repo / "install.sh")
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "doctor_checks.py", scripts / "doctor_checks.py")
    return repo


def _controlled_path(base, managers):
    """A PATH dir holding only: the shim ``python3``, fake ``uname``/``sudo``,
    the requested package-manager binaries, and symlinks to the real
    ``sed``/``dirname`` that install.sh needs.

    Setting ``PATH`` to THIS directory alone makes ``command -v <manager>``
    reflect exactly ``managers`` -- the host's real apt-get is not reachable,
    so the detection order can be tested deterministically.
    """
    bindir = base / "bin"
    bindir.mkdir()

    py = bindir / "python3"
    py.write_text("#!%s\n" % sys.executable + PM_SHIM_TEMPLATE)
    py.chmod(0o755)

    uname = bindir / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-s" ]; then echo Linux\n'
        'elif [ "$1" = "-m" ]; then echo x86_64\n'
        "else exit 1\n"
        "fi\n"
    )
    uname.chmod(0o755)

    sudo = bindir / "sudo"
    sudo.write_text("#!/bin/sh\nexit 0\n")  # passwordless sudo available
    sudo.chmod(0o755)

    for manager in managers:
        m = bindir / manager
        m.write_text("#!/bin/sh\nexit 0\n")
        m.chmod(0o755)

    for real in ("sed", "dirname"):
        src = shutil.which(real)
        assert src, "%s must be available to run install.sh" % real
        os.symlink(src, bindir / real)

    return bindir


def _run_pm_installer(base, managers):
    repo = _make_install_repo(base)
    bindir = _controlled_path(base, managers)
    env = dict(os.environ)
    env["PATH"] = str(bindir)          # ONLY the controlled dir
    env["HOME"] = str(base)
    env["CI"] = ""                     # normal-machine path
    env["TM_REQUIRE_DOCKER"] = "1"     # force the docker-install fallback path
    return subprocess.run(
        [BASH, str(repo / "install.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def test_install_sh_detects_only_manager_present(exec_tmp):
    # Only pacman present among the candidates -> pacman is chosen and its
    # docker-install command is the one actually printed.
    sub = exec_tmp / "pacman_only"
    sub.mkdir()
    result = _run_pm_installer(sub, managers=("pacman",))

    assert "sudo pacman -S --noconfirm docker" in result.stdout, result.stdout
    assert "apt-get" not in result.stdout, result.stdout
    assert "dnf" not in result.stdout, result.stdout


def test_install_sh_prefers_first_manager_in_fixed_order(exec_tmp):
    # Both dnf and pacman present -> the FIRST in the fixed order (dnf) wins.
    sub = exec_tmp / "dnf_and_pacman"
    sub.mkdir()
    result = _run_pm_installer(sub, managers=("dnf", "pacman"))

    assert "sudo dnf install -y docker" in result.stdout, result.stdout
    assert "pacman" not in result.stdout, result.stdout


def test_install_sh_no_manager_errors_clearly(exec_tmp):
    # No candidate manager present -> a clear error NAMING the candidates, and
    # the installer does NOT silently proceed.
    sub = exec_tmp / "no_manager"
    sub.mkdir()
    result = _run_pm_installer(sub, managers=())

    assert result.returncode != 0, result.stdout + result.stderr
    assert "no supported package manager" in result.stdout, result.stdout
    assert "apt-get, dnf, pacman, zypper, apk" in result.stdout, result.stdout
    assert "unsupported distribution" not in result.stdout, result.stdout
    # it must not have pretended to install anything
    assert "pacman -S" not in result.stdout, result.stdout
    assert "docker.io" not in result.stdout, result.stdout
