"""Regression tests for scripts/doctor_checks.py (start-script helpers).

All tests mock subprocess execution (_run); no real docker daemon, `ss`,
or pip is required. scripts/ has no __init__.py, so it is a namespace
package importable from the repo root.
"""

import subprocess

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


def test_installer_script_is_idempotent(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "pip").write_text("#!/bin/sh\n")
    requirements = tmp_path / "requirements.txt"
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
