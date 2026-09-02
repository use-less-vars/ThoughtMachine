"""Shared fixtures for the Playwright end-to-end suite (tests/e2e).

Design notes
------------
* The suite is inert unless ``--run-e2e`` is passed: ``pytest_addoption``
  registers the flag and ``pytest_collection_modifyitems`` skips every
  ``e2e``-marked test in default runs, so ``pytest tests/e2e`` (or a full
  ``pytest`` run) needs no Playwright browser install and fails nothing.
* Playwright is NOT imported at module load; only the pytest-playwright
  plugin's fixtures are used, and those are only resolved when an e2e test
  actually runs.
* All environment-dependent paths (browser binaries, shared libs, fonts)
  are read from the process environment - see tests/e2e/README.md for the
  exact variables the CI/dev container sets:
  PLAYWRIGHT_BROWSERS_PATH, LD_LIBRARY_PATH, FONTCONFIG_FILE.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "web_ui" / "frontend"
VITE_BIN = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"

_HEALTH_TIMEOUT_S = 60.0


def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="run the Playwright end-to-end tests in tests/e2e",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-e2e"):
        return
    skip = pytest.mark.skip(reason="e2e tests require --run-e2e")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _free_port(start: int, attempts: int = 5) -> int:
    """Return the first free TCP port at/after ``start`` on 127.0.0.1."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in [{start}, {start + attempts})")


def _wait_http_ok(url: str, timeout: float = _HEALTH_TIMEOUT_S) -> None:
    """Poll ``url`` until it answers 200 or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - refused while booting
            last_error = exc
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {url} (last error: {last_error!r})")


def _backend_env(vault: str, port: int) -> dict:
    """Environment for a backend subprocess: isolated vault, no API keys."""
    env = {
        **os.environ,
        "HOME": vault,
        "THOUGHTMACHINE_VAULT_ROOT": str(Path(vault) / ".thoughtmachine"),
        "PORT": str(port),
    }
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        env.pop(key, None)
    return env


def _start_backend(vault: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "web_ui.backend.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=_backend_env(vault, port),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def _resolve_node() -> str:
    """Locate a node executable or fail with a helpful message."""
    node = shutil.which("node")
    if node:
        return node
    candidates = [REPO_ROOT / "node-bin" / "bin" / "node"]
    tm_node_dir = REPO_ROOT / ".tm-node"
    if tm_node_dir.exists():
        candidates += sorted(tm_node_dir.glob("*/bin/node"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    pytest.fail(
        "node executable not found; install Node.js or add it to PATH "
        "(see tests/e2e/README.md)"
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_backend():
    """Boot the real FastAPI backend as a subprocess against a temp vault."""
    vault = tempfile.mkdtemp(prefix="e2e_vault_")
    (Path(vault) / ".thoughtmachine").mkdir(parents=True, exist_ok=True)
    port = _free_port(8000)
    proc = _start_backend(vault, port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_http_ok(f"{base_url}/health")
        yield {"base_url": base_url, "port": port, "vault": vault, "proc": proc}
    finally:
        _stop_proc(proc)


@pytest.fixture(scope="session")
def e2e_frontend(e2e_backend):
    """Boot the Vite dev server proxying to the e2e backend."""
    node = _resolve_node()
    if not VITE_BIN.exists():
        pytest.fail(
            f"vite binary missing at {VITE_BIN}; run 'npm install' in "
            f"{FRONTEND_DIR} first"
        )
    port = _free_port(5173)
    proc = subprocess.Popen(
        [node, str(VITE_BIN), "--host", "127.0.0.1", "--port", str(port)],
        cwd=FRONTEND_DIR,
        env={**os.environ, "VITE_BACKEND_PORT": str(e2e_backend["port"])},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_http_ok(f"{base_url}/")
        yield {"base_url": base_url, "port": port, "proc": proc}
    finally:
        _stop_proc(proc)


@pytest.fixture(scope="session")
def workspace(e2e_backend):
    """Create a purpose=research workspace inside the e2e vault."""
    ws_root = Path(e2e_backend["vault"]) / "e2e_ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    resp = httpx.post(
        f"{e2e_backend['base_url']}/api/workspace",
        json={"path": str(ws_root), "purpose": "research"},
        timeout=30,
    )
    assert resp.status_code in (200, 201), (
        f"e2e workspace creation failed: {resp.status_code} {resp.text}"
    )
    data = resp.json()
    return {"ws_id": data["workspace_id"], "path": str(ws_root)}


@pytest.fixture
def restart_backend(e2e_backend):
    """Kill the backend subprocess and boot a fresh one on the same port.

    The Vite dev server proxies to the backend by port, so restarting on the
    same port keeps the frontend working without a frontend restart.
    """

    def _restart():
        _stop_proc(e2e_backend["proc"])
        new_proc = _start_backend(e2e_backend["vault"], e2e_backend["port"])
        e2e_backend["proc"] = new_proc
        _wait_http_ok(f"{e2e_backend['base_url']}/health")
        return new_proc

    yield _restart
    _stop_proc(e2e_backend["proc"])


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Launch Chromium with container-friendly flags (no sandbox)."""
    return {
        **browser_type_launch_args,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
