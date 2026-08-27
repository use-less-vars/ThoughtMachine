#!/usr/bin/env python3
"""thoughtmachine/doctor.py -- system health table for ThoughtMachine.

Runnable from the repo root with ``python3 -m thoughtmachine.doctor``.
Stdlib only; reuses the check helpers from scripts/doctor_checks.py where
possible (check_node_version, check_docker_daemon, check_docker_group,
check_venv, check_dot_thoughtmachine_writable).

Prints a table:

      Check                     Status     Fix
      Python >=3.11             PASS
      Node >=18                 PASS
      ...

and exits 1 when any check FAILs, else 0.

The module has no side effects at import time; the small ``_check_*``
helpers and ``_which``/``_run``/``_python_version_ok``/``_port_free`` are
module-level so tests can monkeypatch them.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from typing import Any, Dict, List, Optional

from scripts import doctor_checks


# ---------------------------------------------------------------------------
# monkeypatchable primitives
# ---------------------------------------------------------------------------


def _which(name: str) -> Optional[str]:
    """Return the path to ``name`` on PATH, or None (shutil.which)."""
    return shutil.which(name)


def _run(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run ``cmd`` with captured text output (subprocess.run)."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _python_version_ok() -> bool:
    """True when the running python3 is >= 3.11.

    Parses the "major minor" pair printed by ``python3 -c``.
    """
    try:
        proc = _run(
            [sys.executable, "-c", "import sys; print('%d %d' % sys.version_info[:2])"],
            timeout=15,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    parts = (proc.stdout or "").split()
    if len(parts) < 2:
        return False
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return (major, minor) >= (3, 11)


def _port_free(port: int) -> bool:
    """True when nothing listens on 127.0.0.1:``port`` (socket.bind probe)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# individual checks -> {"check": str, "status": "PASS"|"WARN"|"FAIL", "fix": str}
# ---------------------------------------------------------------------------


def _check_python() -> Dict[str, Any]:
    name = "Python >=3.11"
    if _which("python3") is None:
        return {"check": name, "status": "FAIL", "fix": "sudo apt-get install python3"}
    if _python_version_ok():
        return {"check": name, "status": "PASS", "fix": ""}
    return {"check": name, "status": "FAIL", "fix": "install Python >= 3.11 (https://www.python.org/downloads/)"}


def _check_node() -> Dict[str, Any]:
    name = "Node >=18"
    result = doctor_checks.check_node_version("18")
    if result.get("ok"):
        return {"check": name, "status": "PASS", "fix": ""}
    return {"check": name, "status": "FAIL", "fix": "install Node.js >= 18 (https://nodejs.org)"}


def _check_docker_daemon() -> Dict[str, Any]:
    name = "Docker daemon"
    result = doctor_checks.check_docker_daemon()
    if result.get("ok"):
        return {"check": name, "status": "PASS", "fix": ""}
    reason = result.get("reason")
    if reason == "daemon_down":
        fix = "sudo systemctl enable --now docker"
    elif reason == "lib_missing":
        fix = "install Docker (https://docs.docker.com/engine/install/)"
    elif reason == "permission_denied":
        fix = "add your user to the docker group and re-login"
    else:
        fix = "see: %s" % (result.get("detail") or "docker info failed")
    return {"check": name, "status": "FAIL", "fix": fix}


def _check_docker_group() -> Dict[str, Any]:
    name = "Docker group"
    result = doctor_checks.check_docker_group()
    if result.get("ok"):
        return {"check": name, "status": "PASS", "fix": ""}
    return {"check": name, "status": "FAIL", "fix": "sudo usermod -aG docker $USER"}


def _check_port(port: int) -> Dict[str, Any]:
    name = "Port %d free" % port
    if _port_free(port):
        return {"check": name, "status": "PASS", "fix": ""}
    return {"check": name, "status": "FAIL", "fix": "free port %d (stop the process using it)" % port}


def _check_venv() -> Dict[str, Any]:
    name = "Venv healthy"
    result = doctor_checks.check_venv(".venv")
    if result.get("ok"):
        return {"check": name, "status": "PASS", "fix": ""}
    return {"check": name, "status": "FAIL", "fix": "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"}


def _check_vault() -> Dict[str, Any]:
    name = "Vault writable"
    result = doctor_checks.check_dot_thoughtmachine_writable()
    if result.get("ok"):
        return {"check": name, "status": "PASS", "fix": ""}
    return {"check": name, "status": "FAIL", "fix": "sudo chown -R $USER ~/.thoughtmachine"}


def run_checks() -> List[Dict[str, Any]]:
    """Run all checks in order and return the list of result dicts."""
    return [
        _check_python(),
        _check_node(),
        _check_docker_daemon(),
        _check_docker_group(),
        _check_port(8000),
        _check_port(5173),
        _check_venv(),
        _check_vault(),
    ]


def main(argv: Optional[List[str]] = None) -> int:
    """Print the health table; return 1 when any check FAILed, else 0."""
    print("  " + "Check".ljust(24) + "Status".ljust(10) + "Fix")
    failed = False
    for result in run_checks():
        name = result.get("check", "")
        status = result.get("status", "FAIL")
        fix = result.get("fix", "")
        if status == "FAIL":
            failed = True
        print("  " + name.ljust(24) + status.ljust(10) + fix)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
