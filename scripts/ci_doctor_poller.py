#!/usr/bin/env python3
"""CI helper: run the --doctor wrapper and assert graceful degradation without Docker.

Starts `bash start_thoughtmachine.sh --doctor` (or whatever --doctor-cmd points
at) as a subprocess, polls the health endpoint with urllib, validates the
degraded JSON payload, and ALWAYS prints logs/doctor_ci.log and
logs/backend_startup.log. Cleans up the backend + doctor process on every path.

Used exclusively by .github/workflows/first_start_ci.yml (step "Doctor mode
degrades gracefully without Docker"). Stdlib only - no external dependencies.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_DOCTOR_CMD = "bash start_thoughtmachine.sh --doctor"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/api/health/containers"
DEFAULT_TIMEOUT = 45.0
LOG_DIR = "logs"
DOCTOR_LOG = os.path.join(LOG_DIR, "doctor_ci.log")
BACKEND_LOG = os.path.join(LOG_DIR, "backend_startup.log")
PY_ERROR_TOKENS = ("ImportError", "ModuleNotFoundError", "Traceback")


def print_logs() -> None:
    """Always surface both logs, even when one is missing.

    The banners make it unmistakable that what follows is a dump of the log
    FILES (which is where the wrapper's stderr went), not leaked subprocess
    output on the step's stdout/stderr.
    """
    for label, path in (("doctor_ci.log", DOCTOR_LOG), ("backend_startup.log", BACKEND_LOG)):
        print(f"==================== full {path} (log file dump) ====================", flush=True)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                print(fh.read(), end="", flush=True)
        except OSError as exc:
            print(f"(could not read {path}: {exc})", flush=True)
        print(f"==================== end {path} ====================", flush=True)


def scan_logs_for_python_errors() -> bool:
    """Return True (and print the offending lines) if any log shows a Python error."""
    hits = []
    for path in (DOCTOR_LOG, BACKEND_LOG):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line_no, line in enumerate(fh, 1):
                    if any(token in line for token in PY_ERROR_TOKENS):
                        hits.append(f"{path}:{line_no}: {line.rstrip()}")
        except OSError:
            pass
    if hits:
        print("::error::backend startup / doctor logs contain Python errors", flush=True)
        for hit in hits:
            print(hit, flush=True)
        return True
    return False


def fetch_health(url: str, timeout: float) -> str | None:
    """GET the health endpoint; return the body on HTTP 200, else None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("utf-8", errors="replace").strip()
            return body or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def validate_payload(body: str) -> str:
    """Return the degraded reason, or raise AssertionError/JSONDecodeError."""
    data = json.loads(body)
    assert isinstance(data, dict), "health payload is not a JSON object"
    assert data.get("checked_at"), "checked_at missing from health payload"
    docker_info = data.get("docker")
    assert isinstance(docker_info, dict), "'docker' key missing from health payload"
    assert docker_info.get("available") is False, (
        "docker.available should be False (DOCKER_HOST deliberately broken)"
    )
    reason = docker_info.get("reason")
    assert reason in {"daemon_down", "lib_missing", "permission_denied"}, (
        f"unexpected degraded reason: {reason!r}"
    )
    return reason


def _kill_by_cmdline(pattern: str) -> None:
    """Kill matching processes via /proc when pkill is not installed."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join("/proc", pid, "cmdline"), "rb") as fh:
                cmdline = fh.read().decode("utf-8", errors="replace").replace("\x00", " ")
        except OSError:
            continue
        if pattern in cmdline and int(pid) != os.getpid():
            try:
                os.kill(int(pid), 15)  # SIGTERM
            except OSError:
                pass


def cleanup(proc: subprocess.Popen) -> None:
    """Stop the backend first (the --doctor wrapper waits on it), then the wrapper."""
    if shutil.which("pkill") is not None:
        subprocess.run(["pkill", "-f", "web_ui.backend.server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _kill_by_cmdline("web_ui.backend.server")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the --doctor wrapper and assert graceful degradation "
                    "without Docker.")
    parser.add_argument("--doctor-cmd", default=DEFAULT_DOCTOR_CMD,
                        help=f"doctor wrapper command (default: {DEFAULT_DOCTOR_CMD})")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL,
                        help=f"health endpoint to poll (default: {DEFAULT_HEALTH_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds to poll before failing (default: {DEFAULT_TIMEOUT:g})")
    args = parser.parse_args()

    # Same semantics as the old shell `command -v docker` graceful skip: with
    # no docker CLI there is no daemon to connect to at all.
    if shutil.which("docker") is None:
        print("::notice::docker CLI not present - skipping degraded-Docker doctor test", flush=True)
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    with open(DOCTOR_LOG, "wb") as out:
        # stdout AND stderr of the doctor wrapper both go to the log file, so
        # no subprocess output (e.g. the "Cannot connect to the Docker daemon"
        # message) can ever reach the step's stdout/stderr.
        proc = subprocess.Popen(shlex.split(args.doctor_cmd),
                                stdout=out, stderr=subprocess.STDOUT)
    print(f"doctor wrapper pid: {proc.pid}", flush=True)

    health_body = None
    wrapper_rc = None
    start = time.monotonic()
    deadline = start + args.timeout
    last_progress = -1
    while time.monotonic() < deadline:
        if health_body is None:
            health_body = fetch_health(args.health_url, timeout=2.0)
        if health_body is not None:
            break
        rc = proc.poll()
        if rc is not None and wrapper_rc is None:
            wrapper_rc = rc
            # Early exit is NOT fatal by itself: the backend can outlive the
            # wrapper, so keep polling for the rest of the window.
            print(f"doctor wrapper exited early (rc={wrapper_rc}) - "
                  f"continuing to poll for backend health until the deadline", flush=True)
        elapsed = int(time.monotonic() - start)
        if elapsed > last_progress and elapsed % 5 == 0:
            last_progress = elapsed
            state = f"exited rc={wrapper_rc}" if wrapper_rc is not None else "running"
            print(f"...polling health endpoint (elapsed {elapsed}s, wrapper {state})", flush=True)
        time.sleep(1)

    if wrapper_rc is not None:
        print(f"doctor wrapper final rc: {wrapper_rc}", flush=True)

    failed = False
    if health_body is not None:
        try:
            reason = validate_payload(health_body)
        except (json.JSONDecodeError, AssertionError) as exc:
            # Always show the exact payload the endpoint returned so the
            # failing field (e.g. docker.available / reason) is visible in
            # the step log without having to re-run with curl.
            print("health payload received from endpoint:", flush=True)
            print(health_body, flush=True)
            print(f"::error::degraded payload assertion failed: {exc}", flush=True)
            failed = True
        else:
            print(f"OK: health degraded as expected (reason={reason})", flush=True)
            if scan_logs_for_python_errors():
                failed = True
            else:
                print("OK: no import errors/tracebacks in backend startup logs", flush=True)
    else:
        if wrapper_rc is None:
            print(f"::error::GET {args.health_url} never returned HTTP 200 "
                  f"within {args.timeout:g}s", flush=True)
        else:
            print(f"::error::doctor wrapper exited early (rc={wrapper_rc}) and "
                  f"GET {args.health_url} never returned HTTP 200 within "
                  f"{args.timeout:g}s", flush=True)
        failed = True

    # Always surface the doctor run, then stop everything we started.
    print_logs()
    cleanup(proc)
    sys.stdout.flush()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
