"""
start_windows.py - Windows launcher for the ThoughtMachine Web UI.

Launches the FastAPI backend (and, in dev mode, the Vite dev server) with the
correct working directories, then shuts everything down cleanly on Ctrl+C.

Why this exists:
    - start_thoughtmachine.bat hardcodes a Python path and relies on the
      user's current working directory. This script uses its own directory
      as the backend cwd and the absolute web_ui/frontend path for Vite,
      so it works from anywhere.
    - On Windows, child processes are created with CREATE_NO_WINDOW so no
      extra console windows pop up.

Usage:
    python start_windows.py            # dev mode (backend + Vite)
    python start_windows.py --prod     # prod mode (backend serves dist/)

This file is intentionally pure ASCII (no non-ASCII characters).
"""

import argparse
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

PYTHON_MIN = (3, 11)
NODE_MIN = 18
BACKEND_PORT = 8000
FRONTEND_PORT = 5173
HEALTH_URL = "http://127.0.0.1:8000/health"
HEALTH_TIMEOUT = 60.0
PORT_WAIT_TIMEOUT = 30.0
DOCKER_WARNING = (
    "Docker Desktop is required for full functionality. Some features will be disabled."
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(SCRIPT_DIR, "web_ui", "frontend")
VENV_DIR = os.path.join(SCRIPT_DIR, ".venv")

_processes = []


def _run(cmd, timeout=30, **kwargs):
    """Run a subprocess and return a CompletedProcess (captured output)."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, timeout=timeout, **kwargs)


def venv_python_path():
    """Return the venv python executable for the current platform."""
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def _major_version(version):
    """Return the first run of digits in a version string ('v18.20.4' -> 18)."""
    if not version:
        return None
    for i, char in enumerate(version):
        if char.isdigit():
            end = i
            while end < len(version) and version[end].isdigit():
                end += 1
            try:
                return int(version[i:end])
            except ValueError:
                return None
    return None


def _parse_netstat_pids(output, port):
    """Parse PIDs from `netstat -ano` output for a given local port."""
    needle = ":%d" % port
    pids = []
    if not output:
        return pids
    for line in output.splitlines():
        if needle not in line:
            continue
        if "LISTENING" not in line and "LISTEN" not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.append(int(parts[-1]))
    return pids


def check_port_free(port, runner=None):
    """Return (free, pid). Uses netstat when available, else a bind probe."""
    run = runner or _run
    result = None
    try:
        result = run(["netstat", "-ano"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        pids = _parse_netstat_pids(result.stdout or "", port)
        if pids:
            return (False, pids[0])
        return (True, None)
    return _port_free_by_bind(port)


def _port_free_by_bind(port):
    """Probe whether the port is free by binding to 127.0.0.1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return (True, None)
    except OSError:
        return (False, None)
    finally:
        sock.close()


def kill_process_tree(pid, runner=None):
    """Kill the process tree rooted at pid; return True on success."""
    if pid is None:
        return False
    run = runner or _run
    if os.name == "nt":
        try:
            result = run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=15)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def wait_for_health(url=HEALTH_URL, timeout=HEALTH_TIMEOUT, interval=2.0, urlopen=None):
    """Poll *url* until it returns HTTP 200 or *timeout* elapses."""
    opener = urlopen or urllib.request.urlopen
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener(url, timeout=min(interval, 5.0)) as resp:
                if getattr(resp, "status", 200) == 200:
                    return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def is_docker_desktop_running(runner=None):
    """Return True when `docker info` exits successfully."""
    run = runner or _run
    try:
        result = run(["docker", "info"], timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_node_version(runner=None):
    """Return (ok, version) for the Node.js on PATH (requires >= NODE_MIN)."""
    run = runner or _run
    try:
        result = run(["node", "--version"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return (False, "")
    if result.returncode != 0:
        return (False, "")
    version = (result.stdout or "").strip()
    major = _major_version(version)
    return (major is not None and major >= NODE_MIN, version)


def check_venv_python_version(venv_python, runner=None):
    """Return (ok, version) for the venv python interpreter (>= PYTHON_MIN)."""
    run = runner or _run
    code = "import sys; print('%d.%d' % sys.version_info[:2])"
    try:
        result = run([venv_python, "-c", code], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return (False, "")
    if result.returncode != 0:
        return (False, "")
    version = (result.stdout or "").strip()
    parts = version.split(".")
    try:
        actual = (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return (False, version)
    return (actual >= PYTHON_MIN, version)


def resolve_vite_command(frontend_dir):
    """Return [node, vite.js] argv when Vite is installed, else None."""
    vite_js = os.path.join(frontend_dir, "node_modules", "vite", "bin", "vite.js")
    if not os.path.isfile(vite_js):
        return None
    node_exe = shutil.which("node")
    if not node_exe:
        return None
    return [node_exe, vite_js]


def _wait_for_port_busy(port, timeout=PORT_WAIT_TIMEOUT, interval=1.0):
    """Wait until something listens on *port*; return True when it does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        free, _pid = check_port_free(port)
        if not free:
            return True
        time.sleep(interval)
    return False


def _terminate_all():
    """Force-kill every spawned child process tree."""
    for proc in _processes:
        if proc.poll() is None:
            try:
                kill_process_tree(proc.pid)
            except Exception:
                pass
            try:
                proc.terminate()
            except OSError:
                pass


def _handle_sigint(signum, frame):
    """Ctrl+C handler - let the backend save its session, then shut down."""
    print("\n[start_windows] Ctrl+C received - shutting down backend and frontend...")
    time.sleep(2.0)
    _terminate_all()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="start_windows.py")
    parser.add_argument(
        "--prod",
        action="store_true",
        help="serve the built frontend from web_ui/frontend/dist (no Vite)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    # 0. The venv must exist (created by install_thoughtmachine.bat).
    venv_python = venv_python_path()
    if not os.path.isfile(venv_python):
        print(
            "[start_windows] ERROR: virtual environment not found at: %s" % venv_python,
            file=sys.stderr,
        )
        print("[start_windows] Run install_thoughtmachine.bat first.", file=sys.stderr)
        return 1

    # 1. venv Python version.
    py_ok, py_ver = check_venv_python_version(venv_python)
    if not py_ok:
        print(
            "[start_windows] ERROR: venv Python %s does not meet the >= 3.11 requirement"
            % (py_ver or "?"),
            file=sys.stderr,
        )
        return 1
    print("[start_windows] venv Python %s detected." % py_ver)

    # 2. Docker Desktop is optional (warn only, never fatal).
    if not is_docker_desktop_running():
        print(DOCKER_WARNING)

    # 3. Node.js is required for the dev frontend only.
    if not args.prod:
        node_ok, node_ver = check_node_version()
        if not node_ok:
            print(
                "[start_windows] ERROR: Node.js 18+ is required for the dev frontend "
                "(found '%s')." % node_ver,
                file=sys.stderr,
            )
            print(
                "[start_windows] Install Node.js LTS from https://nodejs.org/ "
                "then rerun install_thoughtmachine.bat.",
                file=sys.stderr,
            )
            return 1
        print("[start_windows] Node.js %s detected." % node_ver)

    # 4. Free the ports we need (kill stale listeners where possible).
    for port, label in ((BACKEND_PORT, "backend"), (FRONTEND_PORT, "frontend")):
        free, pid = check_port_free(port)
        if not free:
            print("[start_windows] Port %d (%s) is in use (PID %s)." % (port, label, pid))
            if pid is not None and kill_process_tree(pid):
                print("[start_windows] Killed stale process %d on port %d." % (pid, port))
            else:
                print(
                    "[start_windows] WARNING: could not free port %d; continuing anyway." % port
                )

    # 5. Start the backend.
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    backend_cmd = [venv_python, "-m", "web_ui.backend.server"]
    if args.prod:
        backend_cmd.append("--serve-frontend")
    print("[start_windows] Starting backend: %s (cwd: %s)" % (" ".join(backend_cmd), SCRIPT_DIR))
    try:
        backend = subprocess.Popen(backend_cmd, cwd=SCRIPT_DIR, creationflags=creationflags)
    except OSError as exc:
        print("[start_windows] ERROR: could not start backend: %s" % exc, file=sys.stderr)
        return 1
    _processes.append(backend)

    # 6. Wait for the backend health endpoint.
    health_url = "http://%s:%d/health" % (args.host, BACKEND_PORT)
    print("[start_windows] Waiting for backend at %s ..." % health_url)
    if not wait_for_health(health_url, timeout=HEALTH_TIMEOUT, interval=2.0):
        print("[start_windows] ERROR: backend not healthy in time.", file=sys.stderr)
        _terminate_all()
        return 1
    print("[start_windows] Backend is healthy.")

    # 7. Start Vite (dev mode only).
    vite = None
    if not args.prod:
        vite_argv = resolve_vite_command(FRONTEND_DIR)
        if vite_argv is None:
            print(
                "[start_windows] ERROR: Vite is not installed in %s" % FRONTEND_DIR,
                file=sys.stderr,
            )
            print(
                "[start_windows] Run install_thoughtmachine.bat (or `npm install` "
                "in web_ui/frontend) first.",
                file=sys.stderr,
            )
            _terminate_all()
            return 1
        try:
            vite = subprocess.Popen(
                vite_argv + ["--host", args.host],
                cwd=FRONTEND_DIR,
                creationflags=creationflags,
            )
        except OSError as exc:
            print("[start_windows] ERROR: could not start Vite: %s" % exc, file=sys.stderr)
            _terminate_all()
            return 1
        _processes.append(vite)
        if _wait_for_port_busy(FRONTEND_PORT):
            print("[start_windows] Frontend ready at http://%s:%d" % (args.host, FRONTEND_PORT))
        else:
            print("[start_windows] WARNING: frontend did not open port %d in time." % FRONTEND_PORT)

    # 8. Run until Ctrl+C or a child exits.
    print("[start_windows] Running. Press Ctrl+C to stop all servers.")
    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handle_sigint)
        except (ValueError, OSError):
            pass
    try:
        backend.wait()
        if vite is not None:
            vite.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _terminate_all()
        for proc in _processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
