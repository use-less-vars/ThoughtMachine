"""Windows Phase-1 regression tests: install/kill batch scripts, the
start_windows.py launcher, and scripts/smoke_windows.ps1.

Static tests read the real files from the repo and assert structural
properties (pure ASCII, explicit venv paths, the mandatory Docker warning,
taskkill logic). Unit tests exercise start_windows.py helpers with fake
subprocess runners so no real Python venv, Node or Docker is required.

All tests run on Linux with the system python3.
"""

import os
import py_compile
import signal
import subprocess
from pathlib import Path

import pytest

import start_windows as sw

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_BAT = REPO_ROOT / "install_thoughtmachine.bat"
START_PY = REPO_ROOT / "start_windows.py"
KILL_BAT = REPO_ROOT / "kill_thoughtmachine.bat"
SMOKE_PS1 = REPO_ROOT / "scripts" / "smoke_windows.ps1"

DOCKER_WARNING = (
    "Docker Desktop is required for full functionality. Some features will be disabled."
)


def _is_pure_ascii(path):
    return all(b < 0x80 for b in path.read_bytes())


# ---------------------------------------------------------------------------
# Static file checks
# ---------------------------------------------------------------------------


def test_install_bat_is_pure_ascii():
    assert _is_pure_ascii(INSTALL_BAT)


def test_kill_bat_is_pure_ascii():
    assert _is_pure_ascii(KILL_BAT)


def test_smoke_ps1_is_pure_ascii():
    assert _is_pure_ascii(SMOKE_PS1)


def test_start_windows_py_is_pure_ascii():
    assert _is_pure_ascii(START_PY)


def test_install_bat_uses_explicit_venv_python():
    for line in INSTALL_BAT.read_text(encoding="utf-8").splitlines():
        if "python.exe" in line and " -m pip" in line:
            assert "%VENV_DIR%\\Scripts\\python.exe" in line


def test_install_bat_contains_docker_warning():
    assert DOCKER_WARNING in INSTALL_BAT.read_text(encoding="utf-8")


def test_install_bat_has_node_18_check():
    assert "GEQ 18" in INSTALL_BAT.read_text(encoding="utf-8")


def test_install_bat_final_message_mentions_start_windows():
    text = INSTALL_BAT.read_text(encoding="utf-8")
    assert "python start_windows.py" in text
    assert "python start_windows.py --prod" in text


def test_start_windows_py_no_shell_or_os_system():
    text = START_PY.read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert "os.system" not in text


def test_start_windows_py_has_venv_and_backend_details():
    text = START_PY.read_text(encoding="utf-8")
    assert "python.exe" in text
    assert "Scripts" in text
    assert '"web_ui.backend.server"' in text


def test_start_windows_py_contains_docker_warning():
    assert DOCKER_WARNING in START_PY.read_text(encoding="utf-8")


def test_start_windows_py_compiles():
    py_compile.compile(str(START_PY), doraise=True)


def test_smoke_ps1_has_venv_and_join_path():
    text = SMOKE_PS1.read_text(encoding="utf-8")
    assert "Scripts\\python.exe" in text
    assert "Join-Path" in text


def test_smoke_ps1_has_health_check():
    text = SMOKE_PS1.read_text(encoding="utf-8")
    assert "/health" in text
    assert "Invoke-WebRequest" in text


def test_smoke_ps1_contains_docker_warning():
    assert DOCKER_WARNING in SMOKE_PS1.read_text(encoding="utf-8")


def test_kill_bat_keeps_taskkill_logic():
    text = KILL_BAT.read_text(encoding="utf-8")
    assert "taskkill /f" in text
    assert "Get-NetTCPConnection" in text


# ---------------------------------------------------------------------------
# start_windows.py unit tests
# ---------------------------------------------------------------------------


def test_major_version():
    assert sw._major_version("v18.20.4") == 18
    assert sw._major_version("") is None
    assert sw._major_version(None) is None
    assert sw._major_version("abc") is None


def test_parse_netstat_pids_windows_format():
    output = (
        "  TCP    0.0.0.0:8000     0.0.0.0:0    LISTENING       1234\n"
        "  TCP    0.0.0.0:8001     0.0.0.0:0    LISTENING       9999\n"
        "  TCP    1.2.3.4:54321    5.6.7.8:80   ESTABLISHED     5555\n"
        "  TCP    [::]:8000        [::]:0       LISTENING       4321\n"
    )
    assert sw._parse_netstat_pids(output, 8000) == [1234, 4321]


def test_parse_netstat_pids_empty():
    assert sw._parse_netstat_pids("", 8000) == []
    assert sw._parse_netstat_pids(None, 8000) == []


def test_check_port_free_uses_netstat():
    def fake_runner(cmd, timeout=15):
        assert cmd == ["netstat", "-ano"]
        return subprocess.CompletedProcess(
            cmd, 0, "  TCP    0.0.0.0:8000   ...  LISTENING       4321\n", ""
        )

    free, pid = sw.check_port_free(8000, runner=fake_runner)
    assert free is False
    assert pid == 4321


def test_check_port_free_netstat_no_listener():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(
            cmd, 0, "  TCP    0.0.0.0:8000   ...  TIME_WAIT       4321\n", ""
        )

    assert sw.check_port_free(8000, runner=fake_runner) == (True, None)


def test_check_port_free_fallback_when_netstat_raises(monkeypatch):
    def raising_runner(cmd, timeout=15):
        raise OSError("no netstat")

    monkeypatch.setattr(sw, "_port_free_by_bind", lambda port: (True, None))
    assert sw.check_port_free(8000, runner=raising_runner) == (True, None)


def test_check_port_free_fallback_when_netstat_nonzero(monkeypatch):
    def nonzero_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 1, "", "error")

    monkeypatch.setattr(sw, "_port_free_by_bind", lambda port: (False, None))
    assert sw.check_port_free(8000, runner=nonzero_runner) == (False, None)


def test_kill_process_tree_none():
    assert sw.kill_process_tree(None) is False


def test_kill_process_tree_windows(monkeypatch):
    calls = []

    def fake_runner(cmd, timeout=15):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sw.os, "name", "nt")
    assert sw.kill_process_tree(1234, runner=fake_runner) is True
    assert calls == [["taskkill", "/F", "/T", "/PID", "1234"]]


def test_kill_process_tree_posix(monkeypatch):
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(sw.os, "name", "posix")
    monkeypatch.setattr(sw.os, "kill", fake_kill)
    assert sw.kill_process_tree(42) is True
    assert killed == [(42, signal.SIGTERM)]


def test_kill_process_tree_posix_error(monkeypatch):
    def boom(pid, sig):
        raise OSError("no such process")

    monkeypatch.setattr(sw.os, "name", "posix")
    monkeypatch.setattr(sw.os, "kill", boom)
    assert sw.kill_process_tree(42) is False


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_wait_for_health_ok():
    calls = []

    def fake_urlopen(url, timeout=2.0):
        calls.append(url)
        return _FakeResp()

    assert (
        sw.wait_for_health(
            url="http://127.0.0.1:8000/health",
            timeout=1.0,
            interval=0.05,
            urlopen=fake_urlopen,
        )
        is True
    )
    assert calls == ["http://127.0.0.1:8000/health"]


def test_wait_for_health_failure():
    def raising_urlopen(url, timeout=2.0):
        raise OSError("connection refused")

    assert (
        sw.wait_for_health(timeout=0.1, interval=0.05, urlopen=raising_urlopen)
        is False
    )


def test_check_node_version_ok():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 0, "v20.11.1\n", "")

    ok, ver = sw.check_node_version(runner=fake_runner)
    assert ok is True
    assert ver == "v20.11.1"


def test_check_node_version_too_old():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 0, "v16.14.0\n", "")

    ok, ver = sw.check_node_version(runner=fake_runner)
    assert ok is False
    assert ver == "v16.14.0"


def test_check_node_version_missing():
    def missing_runner(cmd, timeout=15):
        raise FileNotFoundError("node not found")

    assert sw.check_node_version(runner=missing_runner) == (False, "")


def test_check_node_version_nonzero_exit():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    assert sw.check_node_version(runner=fake_runner) == (False, "")


def test_check_venv_python_version_ok():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 0, "3.11.9\n", "")

    ok, ver = sw.check_venv_python_version("C:/x/python.exe", runner=fake_runner)
    assert ok is True
    assert ver == "3.11.9"


def test_check_venv_python_version_too_old():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 0, "3.9.7\n", "")

    ok, ver = sw.check_venv_python_version("C:/x/python.exe", runner=fake_runner)
    assert ok is False
    assert ver == "3.9.7"


def test_check_venv_python_version_unparseable():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 0, "weird\n", "")

    ok, ver = sw.check_venv_python_version("C:/x/python.exe", runner=fake_runner)
    assert ok is False
    assert ver == "weird"


def test_resolve_vite_command_missing_vite(tmp_path):
    assert sw.resolve_vite_command(str(tmp_path)) is None


def test_resolve_vite_command_missing_node(tmp_path, monkeypatch):
    vite_js = tmp_path / "node_modules" / "vite" / "bin" / "vite.js"
    vite_js.parent.mkdir(parents=True)
    vite_js.write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(sw.shutil, "which", lambda name: None)
    assert sw.resolve_vite_command(str(tmp_path)) is None


def test_resolve_vite_command_ok(tmp_path, monkeypatch):
    vite_js = tmp_path / "node_modules" / "vite" / "bin" / "vite.js"
    vite_js.parent.mkdir(parents=True)
    vite_js.write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/node")
    assert sw.resolve_vite_command(str(tmp_path)) == ["/usr/bin/node", str(vite_js)]


def test_venv_python_path_windows(monkeypatch):
    monkeypatch.setattr(sw.os, "name", "nt")
    assert sw.venv_python_path().endswith(os.path.join("Scripts", "python.exe"))


def test_venv_python_path_posix(monkeypatch):
    monkeypatch.setattr(sw.os, "name", "posix")
    assert sw.venv_python_path().endswith(os.path.join("bin", "python"))


def test_is_docker_desktop_running_true():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert sw.is_docker_desktop_running(runner=fake_runner) is True


def test_is_docker_desktop_running_false():
    def fake_runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, 1, "", "error")

    assert sw.is_docker_desktop_running(runner=fake_runner) is False


def test_is_docker_desktop_running_missing():
    def missing_runner(cmd, timeout=15):
        raise FileNotFoundError("docker not found")

    assert sw.is_docker_desktop_running(runner=missing_runner) is False


def test_wait_for_port_busy_true(monkeypatch):
    monkeypatch.setattr(sw, "check_port_free", lambda port: (False, 4321))
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    assert sw._wait_for_port_busy(8000, timeout=1.0, interval=0.05) is True


def test_wait_for_port_busy_timeout(monkeypatch):
    monkeypatch.setattr(sw, "check_port_free", lambda port: (True, None))
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    assert sw._wait_for_port_busy(8000, timeout=0.05, interval=0.01) is False


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def test_terminate_all(monkeypatch):
    procs = [_FakeProc(111), _FakeProc(222)]
    monkeypatch.setattr(sw, "_processes", procs)
    killed = []

    def fake_kill(pid, runner=None):
        killed.append(pid)
        return True

    monkeypatch.setattr(sw, "kill_process_tree", fake_kill)
    sw._terminate_all()
    assert killed == [111, 222]
    assert all(p.terminated for p in procs)


def test_main_returns_1_when_venv_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sw, "venv_python_path", lambda: str(tmp_path / "missing" / "python.exe")
    )
    assert sw.main([]) == 1
    assert "virtual environment not found" in capsys.readouterr().err
