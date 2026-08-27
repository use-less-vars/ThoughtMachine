#!/usr/bin/env python3
"""doctor_checks.py -- pure check/ensure helpers for the ThoughtMachine
one-command installer (install.sh) and the start doctor
(start_thoughtmachine.sh).

Stdlib only (no third-party imports), typing hints throughout. Every
``check_*`` function returns a dict with at least ``{"ok": bool,
"detail": str}``; every ``ensure_*`` function returns at least
``{"ok": bool, "changed": bool, "detail": str}``.

CLI (``main()``) -- one action flag per helper so the shell wrappers can
call it. Exactly one flag should be passed per invocation; each run prints
exactly one JSON object on stdout and exits 0 (ok) / 1 (not ok) / 2 (error):

    python3 scripts/doctor_checks.py --check-docker-group [--user NAME]
    python3 scripts/doctor_checks.py --check-docker
    python3 scripts/doctor_checks.py --check-port 8000
    python3 scripts/doctor_checks.py --check-node [--floor 18]
    python3 scripts/doctor_checks.py --check-venv [--path .venv]
    python3 scripts/doctor_checks.py --check-dotthoughtmachine [--path DIR]
    python3 scripts/doctor_checks.py --check-tools
    python3 scripts/doctor_checks.py --ensure-venv [--path .venv] [--requirements requirements.txt]
    python3 scripts/doctor_checks.py --ensure-docker-group [--user NAME]
    python3 scripts/doctor_checks.py --ensure-docker-daemon
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional


# ── small internal helpers ────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run ``cmd`` with captured text output (utf-8, errors replaced).

    Raises FileNotFoundError when the binary is missing; callers map that
    to a helpful result dict.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _sha256_of_file(path: str) -> Optional[str]:
    """Return the sha256 hex digest of ``path``, or None if unreadable."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return None


def _read_stamp(path: str) -> Optional[str]:
    """Read the stamp file content (stripped), or None when absent."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, IOError):
        return None


def _write_stamp(path: str, digest: str) -> None:
    """Write the dependency stamp file."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(digest + "\n")


# ── checks ────────────────────────────────────────────────────────────────────

def check_docker_group(username: Optional[str] = None) -> Dict[str, Any]:
    """Check whether the (default: current) user is a member of group docker."""
    user = username or getpass.getuser()
    try:
        proc = _run(["id", "-nG", user])
    except FileNotFoundError:
        return {"ok": False, "reason": "missing", "detail": "id: command not found", "user": user}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "reason": "other", "detail": str(exc), "user": user}
    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": "other",
            "detail": (proc.stderr or proc.stdout or "id -nG %s failed" % user).strip(),
            "user": user,
        }
    groups: List[str] = proc.stdout.split()
    if "docker" in groups:
        return {"ok": True, "detail": "user %s is a member of group docker" % user, "user": user, "groups": groups}
    return {
        "ok": False,
        "reason": "not_in_group",
        "detail": "user %s is NOT a member of group docker (run: sudo usermod -aG docker %s)" % (user, user),
        "user": user,
        "groups": groups,
    }


def check_docker_daemon() -> Dict[str, Any]:
    """Run ``docker info`` and classify the failure.

    reason: "permission_denied" | "daemon_down" | "other" | "lib_missing"
    (lib_missing = docker CLI not on PATH).
    """
    try:
        proc = _run(["docker", "info"])
    except FileNotFoundError:
        return {
            "ok": False,
            "reason": "lib_missing",
            "detail": "docker CLI not found on PATH — install Docker (https://docs.docker.com/engine/install/) and ensure it is on PATH",
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "reason": "other", "detail": str(exc)}
    if proc.returncode == 0:
        return {"ok": True, "detail": "docker info succeeded"}
    stderr = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "docker info exited %d" % proc.returncode
    low = stderr.lower()
    if "permission denied" in low:
        return {"ok": False, "reason": "permission_denied", "detail": stderr}
    if any(tok in low for tok in ("cannot connect", "connection refused", "daemon")):
        return {"ok": False, "reason": "daemon_down", "detail": stderr}
    return {"ok": False, "reason": "other", "detail": stderr}


def check_port_free(port: int) -> Dict[str, Any]:
    """Check whether ``port`` has a TCP listener, using ``ss -ltn``.

    On a busy port, ``ss -ltnp`` is tried to capture the process name.
    """
    needle = ":%d " % port
    try:
        proc = _run(["ss", "-ltn"])
    except FileNotFoundError:
        return {"ok": False, "reason": "other", "detail": "ss: command not found (install iproute2) — cannot verify port %d" % port}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "reason": "other", "detail": str(exc)}
    busy_lines = [ln for ln in proc.stdout.splitlines() if needle in ln]
    if not busy_lines:
        return {"ok": True, "detail": "port %d is free" % port}
    procname: Optional[str] = None
    try:
        p2 = _run(["ss", "-ltnp"])
        for ln in p2.stdout.splitlines():
            if needle in ln:
                m = re.search(r'users:\(\("([^"]+)"', ln)
                if m:
                    procname = m.group(1)
                break
    except Exception:  # pragma: no cover - best effort only
        procname = None
    detail = "%d is in use by %s" % (port, procname) if procname else "%d is in use" % port
    return {"ok": False, "reason": "in_use", "detail": detail}


def check_node_version(floor: str = "18") -> Dict[str, Any]:
    """Check the installed Node.js major version is >= ``floor``."""
    try:
        floor_major = int(floor)
    except ValueError:
        return {"ok": False, "reason": "other", "detail": "invalid floor value: %r (expected an integer)" % floor}
    try:
        proc = _run(["node", "--version"])
    except FileNotFoundError:
        return {
            "ok": False,
            "reason": "missing",
            "detail": "node: command not found on PATH — install Node.js >= %s (https://nodejs.org) and ensure it is on PATH" % floor,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "reason": "other", "detail": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "reason": "other", "detail": (proc.stderr or proc.stdout or "node --version failed").strip()}
    version = proc.stdout.strip()
    m = re.match(r"v?(\d+)", version)
    if not m:
        return {"ok": False, "reason": "other", "detail": "could not parse node version: %r" % version}
    major = int(m.group(1))
    if major >= floor_major:
        return {"ok": True, "detail": "node %s (major %d) meets the >= %s requirement" % (version, major, floor)}
    return {
        "ok": False,
        "reason": "too_old",
        "detail": "node %s is too old — install Node.js >= %s (https://nodejs.org) and ensure it is on PATH" % (version, floor),
    }


def _venv_python_executable(python: str) -> bool:
    """True when ``python`` is executable by the current user.

    os.access(X_OK) alone returns True for root on any file, so the mode
    bits are also required to keep the predicate reliable when running as
    root (e.g. in containers/CI).
    """
    try:
        return os.access(python, os.X_OK) and bool(os.stat(python).st_mode & 0o111)
    except OSError:
        return False


def _venv_broken_reason(path: str) -> Optional[str]:
    """Return a machine-readable broken-reason for the venv at ``path``.

    Structural predicates only (no subprocess). Each predicate is
    INDEPENDENT -- the venv is broken when ANY of them holds:

    * ``"python_missing"`` -- bin/python is absent (or the dir is missing)
    * ``"not_executable"`` -- bin/python is not executable by the current
      user (mode bits AND os.access, root-proof) or is a dead symlink
    * ``"no_pyvenv_cfg"``  -- pyvenv.cfg is missing (incomplete venv)

    Returns None when the structure looks acceptable; callers then decide
    between 'up to date' and 'needs update' (optionally probing pip).
    """
    if not os.path.isdir(path):
        return "python_missing"
    python = os.path.join(path, "bin", "python")
    if os.path.islink(python) and not os.path.exists(python):
        return "not_executable"
    if not os.path.isfile(python):
        return "python_missing"
    if not _venv_python_executable(python):
        return "not_executable"
    if not os.path.isfile(os.path.join(path, "pyvenv.cfg")):
        return "no_pyvenv_cfg"
    return None


def check_venv(path: str = ".venv") -> Dict[str, Any]:
    """Check that a virtualenv exists at ``path`` with an executable python.

    Shares the structural predicates with ensure_venv; a broken venv
    reports ok False with a machine-readable ``broken_reason``.
    """
    broken = _venv_broken_reason(path)
    if broken is None:
        return {"ok": True, "detail": "virtualenv %s exists with executable python" % path}
    if not os.path.isdir(path):
        return {
            "ok": False,
            "reason": "missing",
            "broken_reason": broken,
            "detail": "virtualenv %s does not exist — run: python3 -m venv %s" % (path, path),
        }
    if broken == "python_missing":
        return {
            "ok": False,
            "reason": "broken",
            "broken_reason": broken,
            "detail": "virtualenv %s has no bin/python (is it a real venv?)" % path,
        }
    if broken == "not_executable":
        return {
            "ok": False,
            "reason": "broken",
            "broken_reason": broken,
            "detail": "virtualenv python %s is not executable" % os.path.join(path, "bin", "python"),
        }
    return {
        "ok": False,
        "reason": "broken",
        "broken_reason": broken,
        "detail": "virtualenv %s has no pyvenv.cfg (incomplete venv)" % path,
    }


def check_dot_thoughtmachine_writable(path: Optional[str] = None) -> Dict[str, Any]:
    """Check ~/.thoughtmachine exists and is writable by the current user."""
    target = path or os.path.join(os.path.expanduser("~"), ".thoughtmachine")
    hint = "sudo chown -R $USER %s" % target
    if not os.path.isdir(target):
        return {"ok": False, "reason": "missing", "hint": hint, "detail": "%s does not exist — %s" % (target, hint)}
    if not os.access(target, os.W_OK):
        return {"ok": False, "reason": "not_writable", "hint": hint, "detail": "%s is not writable — %s" % (target, hint)}
    return {"ok": True, "hint": None, "detail": "%s exists and is writable" % target}


def check_tools() -> Dict[str, Any]:
    """Check the tools required to run ThoughtMachine are on PATH.

    Returns ONE JSON object with:
      * ``tools`` -- dict name -> {"present": bool, "critical": bool, "hint": str}
      * ``critical_missing`` -- list of missing CRITICAL tool names
      * ``docker_present`` / ``docker_hint`` -- convenience keys for the
        launcher's warn-only docker note

    Critical tools: python3, node, npm, ss, sg, apt-get. Docker is
    NON-critical: the project supports a docker-less degraded mode, so a
    missing docker CLI never fails the check (warn-only).
    """
    critical = {
        "python3": "sudo apt-get install python3",
        "node": "Node.js >= 18 required",
        "npm": "install Node.js >= 18 (includes npm)",
        "ss": "sudo apt-get install iproute2",
        "sg": "sudo apt-get install util-linux",
        "apt-get": "sudo apt-get update",
    }
    docker_hint = "sudo apt-get install docker.io (or install docker-ce: https://docs.docker.com/engine/install/)"
    tools: Dict[str, Any] = {}
    critical_missing: List[str] = []
    for name, hint in critical.items():
        present = shutil.which(name) is not None
        tools[name] = {"present": present, "critical": True, "hint": hint}
        if not present:
            critical_missing.append(name)
    docker_present = shutil.which("docker") is not None
    tools["docker"] = {"present": docker_present, "critical": False, "hint": docker_hint}
    ok = not critical_missing
    detail = "all required tools are present" if ok else "missing critical tools: %s" % ", ".join(critical_missing)
    return {
        "ok": ok,
        "detail": detail,
        "tools": tools,
        "critical_missing": critical_missing,
        "docker_present": docker_present,
        "docker_hint": docker_hint,
    }


# ── ensures ───────────────────────────────────────────────────────────────────

def _create_venv(path: str) -> Optional[str]:
    """Create a venv at ``path`` with ``python3 -m venv``.

    Returns None on success, or an error detail string on failure.
    """
    try:
        proc = _run(["python3", "-m", "venv", path])
    except FileNotFoundError:
        return "python3: command not found on PATH — install Python 3 (https://www.python.org/downloads/)"
    except Exception as exc:  # pragma: no cover - defensive
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "venv creation failed").strip()
    return None


def ensure_venv(path: str = ".venv", requirements: str = "requirements.txt") -> Dict[str, Any]:
    """Create the venv (if missing or broken) and install dependencies idempotently.

    Self-healing: a venv whose structure is broken (bin/python missing or
    not executable, pyvenv.cfg missing) or whose ``python -m pip --version``
    fails is removed and recreated from scratch, then dependencies are
    reinstalled; the result carries ``broken_reason`` and
    ``detail: "recreated: <reason>"``.

    Idempotency: on a healthy venv whose stamp file
    ``<path>/.tm-requirements.stamp`` matches the sha256 of the current
    ``requirements.txt`` nothing is done (changed=False). The pip health
    probe is only run when the stamp is outdated or the structure is
    suspect, so the healthy up-to-date path makes no subprocess calls.
    """
    python = os.path.join(path, "bin", "python")
    stamp = os.path.join(path, ".tm-requirements.stamp")
    digest = _sha256_of_file(requirements)
    if digest is None:
        return {"ok": False, "changed": False, "detail": "requirements file %s not found (run from the repo root)" % requirements}

    broken = _venv_broken_reason(path)
    created = False
    broken_reason: Optional[str] = None

    if broken is not None:
        # Existing-but-broken venv -> remove it and recreate from scratch.
        if os.path.isdir(path):
            broken_reason = broken
            try:
                shutil.rmtree(path)
            except OSError as exc:
                return {"ok": False, "changed": False, "detail": "cannot remove broken venv %s: %s" % (path, exc)}
        err = _create_venv(path)
        if err is not None:
            return {"ok": False, "changed": False, "detail": err}
        created = True
    else:
        if _read_stamp(stamp) == digest:
            return {"ok": True, "changed": False, "detail": "virtualenv %s ready; dependencies up to date (stamp matches %s)" % (path, requirements)}
        # Structure acceptable but the stamp is outdated/absent: probe pip
        # health (short timeout) before deciding between reinstall and
        # recreate. The healthy up-to-date path above never gets here.
        try:
            probe = _run([python, "-m", "pip", "--version"], timeout=15)
        except Exception:
            probe = None
        if probe is None or probe.returncode != 0:
            broken_reason = "pip_broken"
            try:
                shutil.rmtree(path)
            except OSError as exc:
                return {"ok": False, "changed": False, "detail": "cannot remove broken venv %s: %s" % (path, exc)}
            err = _create_venv(path)
            if err is not None:
                return {"ok": False, "changed": False, "detail": err}
            created = True

    pip = os.path.join(path, "bin", "pip")
    try:
        proc = _run([pip, "install", "-r", requirements], timeout=600)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "changed": created, "detail": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "changed": created, "detail": (proc.stderr or proc.stdout or "pip install failed").strip()}
    _write_stamp(stamp, digest)
    if broken_reason is not None:
        return {"ok": True, "changed": True, "detail": "recreated: %s" % broken_reason, "broken_reason": broken_reason}
    what = "created and populated" if created else "updated"
    return {"ok": True, "changed": True, "detail": "virtualenv %s %s; installed dependencies from %s and wrote stamp" % (path, what, requirements)}


def ensure_docker_group(username: Optional[str] = None) -> Dict[str, Any]:
    """Add the (default: current) user to group docker via sudo usermod."""
    user = username or getpass.getuser()
    check = check_docker_group(user)
    if check.get("ok"):
        return {"ok": True, "changed": False, "detail": "user %s is already a member of group docker" % user}
    try:
        proc = _run(["sudo", "usermod", "-aG", "docker", user])
    except FileNotFoundError:
        return {"ok": False, "changed": False, "detail": "sudo: command not found — cannot add %s to group docker" % user}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "changed": False, "detail": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "changed": False, "detail": (proc.stderr or proc.stdout or "sudo usermod failed").strip()}
    return {"ok": True, "changed": True, "detail": "added user %s to group docker — re-login or run: newgrp docker" % user}


def ensure_docker_daemon() -> Dict[str, Any]:
    """Start + enable the docker daemon via systemd when it is down."""
    check = check_docker_daemon()
    if check.get("ok"):
        return {"ok": True, "changed": False, "detail": "docker daemon is already running"}
    if check.get("reason") != "daemon_down":
        return {
            "ok": False,
            "changed": False,
            "detail": "cannot auto-start docker (%s): %s" % (check.get("reason"), check.get("detail")),
        }
    try:
        proc = _run(["sudo", "systemctl", "enable", "--now", "docker"])
    except FileNotFoundError:
        return {"ok": False, "changed": False, "detail": "sudo or systemctl: command not found — start the docker daemon manually"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "changed": False, "detail": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "changed": False, "detail": (proc.stderr or proc.stdout or "systemctl enable --now docker failed").strip()}
    return {"ok": True, "changed": True, "detail": "docker daemon enabled and started (sudo systemctl enable --now docker)"}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _emit(result: Dict[str, Any]) -> int:
    """Print the result as one JSON line and map ok -> exit code (0/1)."""
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer, got %r" % value)
    if not (0 <= port <= 65535):
        raise argparse.ArgumentTypeError("port out of range: %d" % port)
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doctor_checks.py",
        description=(
            "ThoughtMachine doctor: check/ensure helpers for install.sh and "
            "start_thoughtmachine.sh. Pass exactly one action flag; the script "
            "prints one JSON object on stdout and exits 0 (ok) / 1 (not ok) / "
            "2 (error)."
        ),
    )
    parser.add_argument("--check-docker-group", action="store_true", dest="check_docker_group",
                        help="check the current user is in the docker group")
    parser.add_argument("--check-docker", action="store_true", dest="check_docker",
                        help="check the docker daemon is reachable (docker info)")
    parser.add_argument("--check-port", type=_parse_port, metavar="PORT", dest="check_port",
                        help="check a TCP port is free (ss -ltn)")
    parser.add_argument("--check-node", action="store_true", dest="check_node",
                        help="check the Node.js major version meets the floor")
    parser.add_argument("--check-venv", action="store_true", dest="check_venv",
                        help="check the virtualenv exists with an executable python")
    parser.add_argument("--check-dotthoughtmachine", action="store_true", dest="check_dotthoughtmachine",
                        help="check ~/.thoughtmachine exists and is writable")
    parser.add_argument("--check-tools", action="store_true", dest="check_tools",
                        help="check required tools (python3, node, npm, ss, sg, apt-get) are on PATH; "
                             "docker is warn-only (degraded mode is supported)")
    parser.add_argument("--ensure-venv", action="store_true", dest="ensure_venv",
                        help="create the venv if missing and install requirements.txt idempotently")
    parser.add_argument("--ensure-docker-group", action="store_true", dest="ensure_docker_group",
                        help="add the current user to the docker group via sudo")
    parser.add_argument("--ensure-docker-daemon", action="store_true", dest="ensure_docker_daemon",
                        help="start + enable the docker daemon via systemd")
    parser.add_argument("--floor", default="18", metavar="MAJOR",
                        help="minimum Node.js major version for --check-node (default: 18)")
    parser.add_argument("--path", default=None, metavar="DIR",
                        help="directory for --check-venv/--ensure-venv (default: .venv) or "
                             "--check-dotthoughtmachine (default: ~/.thoughtmachine)")
    parser.add_argument("--requirements", default="requirements.txt", metavar="FILE",
                        help="requirements file for --ensure-venv (default: requirements.txt)")
    parser.add_argument("--user", default=None, metavar="NAME",
                        help="username for --check-docker-group/--ensure-docker-group "
                             "(default: current user)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.check_docker_group:
            return _emit(check_docker_group(args.user))
        if args.check_docker:
            return _emit(check_docker_daemon())
        if args.check_port is not None:
            return _emit(check_port_free(args.check_port))
        if args.check_node:
            return _emit(check_node_version(args.floor))
        if args.check_venv:
            return _emit(check_venv(args.path or ".venv"))
        if args.check_dotthoughtmachine:
            return _emit(check_dot_thoughtmachine_writable(args.path))
        if args.check_tools:
            return _emit(check_tools())
        if args.ensure_venv:
            return _emit(ensure_venv(args.path or ".venv", args.requirements))
        if args.ensure_docker_group:
            return _emit(ensure_docker_group(args.user))
        if args.ensure_docker_daemon:
            return _emit(ensure_docker_daemon())
    except Exception as exc:  # pragma: no cover - last-resort error path
        sys.stderr.write("error: %s\n" % exc)
        return 2
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
