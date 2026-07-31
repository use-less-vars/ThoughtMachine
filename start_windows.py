"""
start_windows.py — Windows launcher for the ThoughtMachine Web UI.

Launches both the FastAPI backend and the Vite dev server with the correct
working directories, then shuts both down cleanly on Ctrl+C (SIGINT).

Why this exists:
    - ``start_thoughtmachine.bat`` hardcodes a Python path (TM_PYTHON) and
      relies on the user's current working directory.  This script uses the
      script's own directory as the backend cwd and the absolute
      ``web_ui/frontend`` path for Vite, so it works from anywhere.
    - On Windows, the Vite dev server needs to run through the shell
      (``vite.cmd``) and must not pop up an extra console window, hence
      ``shell=True`` + ``CREATE_NO_WINDOW``.

Usage:
    python start_windows.py
"""

import os
import signal
import subprocess
import sys

# ── Paths (absolute — independent of the user's current working directory) ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(SCRIPT_DIR, "web_ui", "frontend")

_processes = []


def _terminate_all():
    """Politely terminate every spawned child process."""
    for proc in _processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


def _handle_sigint(signum, frame):
    """Ctrl+C handler — forward the shutdown to both child processes."""
    print("\n[start_windows] Ctrl+C received — shutting down backend and Vite...")
    _terminate_all()


def main():
    # ── 1. Backend: python -m web_ui.backend.server (cwd = project root) ──
    backend_cmd = [sys.executable, "-m", "web_ui.backend.server"]
    print(f"[start_windows] Starting backend: {' '.join(backend_cmd)}  (cwd: {SCRIPT_DIR})")
    try:
        backend = subprocess.Popen(
            backend_cmd,
            cwd=SCRIPT_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        print("[start_windows] ERROR: python not found on PATH.", file=sys.stderr)
        return 1
    _processes.append(backend)

    # ── 2. Vite dev server (cwd = web_ui/frontend) ──
    # shell=True is required on Windows so `vite` resolves through vite.cmd.
    vite_cmd = "vite --host 127.0.0.1"
    print(f"[start_windows] Starting Vite dev server: {vite_cmd}  (cwd: {FRONTEND_DIR})")
    try:
        vite = subprocess.Popen(
            vite_cmd,
            cwd=FRONTEND_DIR,
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        print("[start_windows] ERROR: vite not found. Run `npm install` in web_ui/frontend first.", file=sys.stderr)
        _terminate_all()
        return 1
    _processes.append(vite)

    # ── 3. Wait for shutdown (Ctrl+C or child exit) ──
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        backend.wait()
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
