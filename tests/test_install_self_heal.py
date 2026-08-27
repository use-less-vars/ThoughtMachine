"""Tests for the installer self-healing venv logic and the doctor command.

Two modules under test:
  * scripts/doctor_checks.py  -- ensure_venv (self-healing), check_tools,
    check_dot_thoughtmachine_writable
  * thoughtmachine/doctor.py  -- the `python3 -m thoughtmachine.doctor`
    health table (stdlib-only, reuses doctor_checks helpers)

All tests are mock-only: subprocess execution (_run), shutil.which,
os.access and the doctor check helpers are monkeypatched, so nothing real
is executed and nothing outside tmp_path is touched. The conftest.py
hermetic guard redirects HOME at import time, so the real vault is never
written.
"""

import os
import shutil as _shutil
import subprocess

import scripts.doctor_checks as doctor_checks
import thoughtmachine.doctor as tm_doctor


def _patch_exec_access(monkeypatch):
    """Make os.access report X_OK as granted for any file.

    Sandboxes often mount /tmp with the noexec flag, where os.access(X_OK)
    returns False even for mode 0o755 files. The venv tests classify
    executability from the mode bits (root-proof), so X_OK is forced True
    and the predicate depends only on the mode, as intended.
    """
    real_access = os.access
    monkeypatch.setattr(
        os, "access",
        lambda path, mode: True if mode == os.X_OK else real_access(path, mode),
    )


# ---------------------------------------------------------------------------
# ensure_venv self-healing
# ---------------------------------------------------------------------------


def _write_healthy_fake_venv(venv):
    """Create a fake venv that is structurally healthy (exec python + cfg)."""
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")


def _requirements(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi>=0.100\n")
    return req


def _fake_run_recreating_venv(venv, calls, probe_rc=0):
    """fake _run: records calls, re-mkdirs venv/bin on venv-create so the
    stamp file can be written after the broken venv was rmtree'd."""
    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd[:3] == ["python3", "-m", "venv"]:
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["-m", "pip"]:  # health probe
            return subprocess.CompletedProcess(cmd, probe_rc, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


def test_ensure_venv_recreates_when_python_missing(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)  # bin dir only -- no bin/python
    requirements = _requirements(tmp_path)

    calls = []
    monkeypatch.setattr(
        doctor_checks, "_run", _fake_run_recreating_venv(venv, calls)
    )

    result = doctor_checks.ensure_venv(str(venv), str(requirements))

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["broken_reason"] == "python_missing"
    assert result["detail"] == "recreated: python_missing"
    assert ["python3", "-m", "venv", str(venv)] in calls


def test_ensure_venv_recreates_when_no_pyvenv_cfg(tmp_path, monkeypatch):
    _patch_exec_access(monkeypatch)
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)  # executable, but NO pyvenv.cfg
    requirements = _requirements(tmp_path)

    calls = []
    monkeypatch.setattr(
        doctor_checks, "_run", _fake_run_recreating_venv(venv, calls)
    )

    result = doctor_checks.ensure_venv(str(venv), str(requirements))

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["broken_reason"] == "no_pyvenv_cfg"
    assert result["detail"] == "recreated: no_pyvenv_cfg"
    assert ["python3", "-m", "venv", str(venv)] in calls


def test_ensure_venv_recreates_when_python_not_executable(tmp_path, monkeypatch):
    _patch_exec_access(monkeypatch)
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\n")  # default mode 0o644 -> not executable
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    requirements = _requirements(tmp_path)

    calls = []
    monkeypatch.setattr(
        doctor_checks, "_run", _fake_run_recreating_venv(venv, calls)
    )

    result = doctor_checks.ensure_venv(str(venv), str(requirements))

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["broken_reason"] == "not_executable"
    assert result["detail"] == "recreated: not_executable"
    assert ["python3", "-m", "venv", str(venv)] in calls


def test_ensure_venv_recreates_when_pip_broken(tmp_path, monkeypatch):
    _patch_exec_access(monkeypatch)
    venv = tmp_path / ".venv"
    _write_healthy_fake_venv(venv)  # healthy structure, no stamp
    requirements = _requirements(tmp_path)

    calls = []
    # probe (python -m pip --version) exits 1 -> pip_broken; venv-create ok
    monkeypatch.setattr(
        doctor_checks, "_run", _fake_run_recreating_venv(venv, calls, probe_rc=1)
    )

    result = doctor_checks.ensure_venv(str(venv), str(requirements))

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["broken_reason"] == "pip_broken"
    assert result["detail"] == "recreated: pip_broken"
    assert ["python3", "-m", "venv", str(venv)] in calls


def test_ensure_venv_healthy_fresh_stamp_makes_no_calls(tmp_path, monkeypatch):
    _patch_exec_access(monkeypatch)
    venv = tmp_path / ".venv"
    _write_healthy_fake_venv(venv)
    requirements = _requirements(tmp_path)
    digest = doctor_checks._sha256_of_file(str(requirements))
    (venv / ".tm-requirements.stamp").write_text(digest + "\n")

    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(doctor_checks, "_run", fake_run)

    result = doctor_checks.ensure_venv(str(venv), str(requirements))

    assert result["ok"] is True
    assert result["changed"] is False
    assert "up to date" in result["detail"]
    assert calls == []


# ---------------------------------------------------------------------------
# check_tools
# ---------------------------------------------------------------------------


def test_check_tools_ss_missing_is_critical(monkeypatch):
    real_which = _shutil.which

    def fake_which(name):
        if name == "ss":
            return None
        return real_which(name)

    monkeypatch.setattr(doctor_checks.shutil, "which", fake_which)

    result = doctor_checks.check_tools()

    assert result["ok"] is False
    assert "ss" in result["critical_missing"]
    assert "iproute2" in result["tools"]["ss"]["hint"]


def test_check_tools_docker_missing_is_warn_only(monkeypatch):
    real_which = _shutil.which

    def fake_which(name):
        # Fake presence of every critical tool so the result depends only on
        # docker's NON-critical status (environment-independent); docker is
        # the one tool reported missing.
        if name == "docker":
            return None
        if name in ("python3", "node", "npm", "ss", "sg", "apt-get"):
            return "/usr/bin/" + name
        return real_which(name)

    monkeypatch.setattr(doctor_checks.shutil, "which", fake_which)

    result = doctor_checks.check_tools()

    assert result["ok"] is True
    assert result["tools"]["docker"]["present"] is False
    assert result["tools"]["docker"]["critical"] is False
    assert result["docker_present"] is False


# ---------------------------------------------------------------------------
# check_dot_thoughtmachine_writable
# ---------------------------------------------------------------------------


def test_check_dot_thoughtmachine_not_writable(tmp_path, monkeypatch):
    vault = tmp_path / ".thoughtmachine"
    vault.mkdir()
    real_access = doctor_checks.os.access

    def fake_access(path, mode):
        if path == str(vault) and mode == doctor_checks.os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(doctor_checks.os, "access", fake_access)

    result = doctor_checks.check_dot_thoughtmachine_writable(str(vault))

    assert result["ok"] is False
    assert result["reason"] == "not_writable"
    assert "sudo chown -R" in result["detail"]
    assert "sudo chown -R" in result["hint"]


# ---------------------------------------------------------------------------
# thoughtmachine.doctor table
# ---------------------------------------------------------------------------


def test_doctor_reports_docker_daemon_fail_with_fix(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_checks,
        "check_docker_daemon",
        lambda: {"ok": False, "reason": "daemon_down", "detail": "cannot connect"},
    )
    pass_result = {"status": "PASS", "fix": ""}

    def fake_pass(*args, **kwargs):
        return dict(pass_result)

    for helper in (
        "_check_python",
        "_check_node",
        "_check_docker_group",
        "_check_port",
        "_check_venv",
        "_check_vault",
    ):
        monkeypatch.setattr(tm_doctor, helper, fake_pass)

    results = tm_doctor.run_checks()
    docker = [r for r in results if r.get("check") == "Docker daemon"][0]
    assert docker["status"] == "FAIL"
    assert docker["fix"] == "sudo systemctl enable --now docker"

    rc = tm_doctor.main([])
    assert rc == 1

    out = capsys.readouterr().out
    assert "Docker daemon" in out
    assert "sudo systemctl enable --now docker" in out


def test_doctor_all_pass_prints_table(monkeypatch, capsys):
    def pass_result(check):
        return {"check": check, "status": "PASS", "fix": ""}

    monkeypatch.setattr(tm_doctor, "_check_python", lambda: pass_result("Python >=3.11"))
    monkeypatch.setattr(tm_doctor, "_check_node", lambda: pass_result("Node >=18"))
    monkeypatch.setattr(tm_doctor, "_check_docker_daemon", lambda: pass_result("Docker daemon"))
    monkeypatch.setattr(tm_doctor, "_check_docker_group", lambda: pass_result("Docker group"))
    monkeypatch.setattr(tm_doctor, "_check_port", lambda port: pass_result("Port %d free" % port))
    monkeypatch.setattr(tm_doctor, "_check_venv", lambda: pass_result("Venv healthy"))
    monkeypatch.setattr(tm_doctor, "_check_vault", lambda: pass_result("Vault writable"))

    rc = tm_doctor.main([])
    assert rc == 0

    out = capsys.readouterr().out
    for token in (
        "Check",
        "Status",
        "Fix",
        "Python >=3.11",
        "Node >=18",
        "Docker daemon",
        "Docker group",
        "Port 8000 free",
        "Port 5173 free",
        "Venv healthy",
        "Vault writable",
    ):
        assert token in out
