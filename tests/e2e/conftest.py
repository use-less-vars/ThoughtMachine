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
from urllib.parse import urlparse

import httpx
import pytest

# The credential store is imported rather than re-derived: the token path and its
# parsing live in ``web_ui/backend/auth_token.py``, so this suite follows the
# server's own definition of "where the token is" and "what the token is".
from web_ui.backend.auth_token import STATE_DIRNAME as _TOKEN_STATE_DIRNAME
from web_ui.backend.auth_token import TOKEN_FILENAME as _TOKEN_FILENAME
from web_ui.backend.auth_token import read_token as _read_token

# The session cookie a browser context must carry is minted and verified by the
# boundary's own helpers -- never hand-rolled here: its value is
# ``"<issued-at>.<mac>"``, not the bearer token, and the format is the server's
# to define.
from web_ui.backend.auth_boundary import COOKIE_NAME as _COOKIE_NAME
from web_ui.backend.auth_boundary import session_credential as _session_credential
from web_ui.backend.auth_boundary import (
    session_credential_ok as _session_credential_ok,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "web_ui" / "frontend"
VITE_BIN = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"

_HEALTH_TIMEOUT_S = 60.0

# Bounded wait for ``<vault>/state/web_ui_token``.  The token is NOT minted at
# boot: the ingress gate writes it lazily, on the first non-exempt request (or
# on ``GET /``).  Never a skip -- a token that never appears is a failure of the
# server under test.
_TOKEN_WAIT_TIMEOUT_S = 30.0
_TOKEN_POLL_S = 0.25

#: Subdirectory of the throwaway ``HOME`` the backend under test is pointed at.
#: ``_backend_env`` hands the server ``THOUGHTMACHINE_VAULT_ROOT=<HOME>/.thoughtmachine``
#: (see ``thoughtmachine/vault.py:29-39``), so the vault the server writes its
#: token into is that subdirectory -- not the HOME itself.
_VAULT_SUBDIR = ".thoughtmachine"


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


def _vault_root(vault: str) -> Path:
    """The ``THOUGHTMACHINE_VAULT_ROOT`` the backend under test was handed."""
    return Path(vault) / _VAULT_SUBDIR


def _backend_env(vault: str, port: int) -> dict:
    """Environment for a backend subprocess: isolated vault, no API keys."""
    env = {
        **os.environ,
        "HOME": vault,
        "THOUGHTMACHINE_VAULT_ROOT": str(_vault_root(vault)),
        "PORT": str(port),
    }
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        env.pop(key, None)
    return env


def _mint_token(base_url: str) -> None:
    """Ask the backend for its app shell, which is what mints the token.

    ``AuthBoundaryMiddleware`` resolves the secret *lazily*
    (``auth_boundary.py:480-500``): booting the server writes nothing, and the
    first request that needs the gate is what calls ``ensure_token``.  The
    documented trigger is ``GET /`` -- an exempt path listed in
    ``COOKIE_MINT_PATHS`` (``auth_boundary.py:197``, ``:607-618``), whose
    response carries the session cookie.  Hitting it turns "read a token that
    no one has written yet" into a bounded wait rather than a hang.

    Failures are swallowed: the caller's deadline owns the diagnosis, and a
    backend that is still binding its port must not fail the request
    directly.
    """
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5):
            pass
    except Exception:  # noqa: BLE001 - retried by the caller's loop
        pass


def _vault_token(
    vault: str, base_url: str | None = None, timeout: float = _TOKEN_WAIT_TIMEOUT_S
) -> str:
    """Return the token the backend minted inside *vault*, retried until present.

    The location is composed from the server's own constants
    (``web_ui/backend/auth_token.py``: ``STATE_DIRNAME`` / ``TOKEN_FILENAME`` ->
    ``<vault>/state/web_ui_token``, where ``<vault>`` is this suite's
    ``THOUGHTMACHINE_VAULT_ROOT`` = ``<HOME>/.thoughtmachine``) and the value is
    parsed by the server's own
    ``read_token`` (which strips whitespace and fails closed on a broken store),
    so this suite cannot drift from the real credential store.

    ``token_path()`` is deliberately not used: it resolves through
    ``thoughtmachine.vault.vault_root()`` for *this* process, while the backend
    under test was handed ``THOUGHTMACHINE_VAULT_ROOT=<scratch vault>`` and a
    throwaway ``HOME``.  The vault the server was pointed at is the one to read.

    *base_url* is optional but practically required against a real backend:
    the token is minted lazily, so each pass of the wait loop first asks the
    server for its shell (:func:`_mint_token`) and then re-reads the file.
    """
    token_file = _vault_root(vault) / _TOKEN_STATE_DIRNAME / _TOKEN_FILENAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            token = _read_token(token_file)
        except RuntimeError as exc:  # unusable store: fail closed, do not retry
            raise AssertionError(f"web UI token store is unusable: {exc}") from exc
        if token:
            return token
        if base_url is not None:
            _mint_token(base_url)
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"backend never wrote a usable web UI token at {token_file} within "
                f"{timeout:.0f}s (file {'blank' if token == '' else 'absent'}). "
                "Every non-exempt API path and /ws requires 'Authorization: Bearer "
                "<token>', so there is no unauthenticated fallback to fall back to."
            )
        time.sleep(_TOKEN_POLL_S)


def _auth_headers(vault: str, base_url: str | None = None) -> dict:
    """The ``Authorization`` header the backend's ingress boundary requires."""
    return {"Authorization": f"Bearer {_vault_token(vault, base_url)}"}


def _start_backend(vault: str, port: int) -> subprocess.Popen:
    """Boot the backend with stdout/stderr redirected to a log file.

    Child output is written straight to a file descriptor (``stdout=PIPE``
    would leave the pipe undrained and wedge the backend once ~64KB of log
    output accumulates).  The log handle is attached to the Popen object and
    closed by ``_stop_proc`` so restarts can re-append to the same file.
    """
    log_path = Path(vault) / f"backend-{port}.log"
    log_handle = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
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
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    proc._log_handle = log_handle
    proc._log_path = log_path
    return proc


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
    # Close the attached log handle even when the child already exited (e.g.
    # a crash mid-test) so a restart can safely re-open the same log file.
    handle = getattr(proc, "_log_handle", None)
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


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
    """Boot the real FastAPI backend as a subprocess against a temp vault.

    Process state is held in a mutable local ``state`` dict that is yielded
    to tests.  ``restart_backend`` may replace ``state["proc"]`` with a
    fresh process; teardown always stops the process currently recorded in
    the dict, so a restarted backend is cleaned up at session end instead
    of being leaked.
    """
    vault = tempfile.mkdtemp(prefix="e2e_vault_")
    _vault_root(vault).mkdir(parents=True, exist_ok=True)
    port = _free_port(8000)
    state = {
        "base_url": f"http://127.0.0.1:{port}",
        "port": port,
        "vault": vault,
        "proc": _start_backend(vault, port),
        "log_path": str(Path(vault) / f"backend-{port}.log"),
    }
    try:
        _wait_http_ok(f"{state['base_url']}/health")
        yield state
    finally:
        _stop_proc(state["proc"])


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
    # Same stdout=PIPE-wedge guard as the backend: redirect to a log file so
    # an undrained pipe can never stall the dev server.
    log_path = Path(e2e_backend["vault"]) / f"vite-{port}.log"
    log_handle = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [node, str(VITE_BIN), "--host", "127.0.0.1", "--port", str(port)],
        cwd=FRONTEND_DIR,
        env={**os.environ, "VITE_BACKEND_PORT": str(e2e_backend["port"])},
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    proc._log_handle = log_handle
    proc._log_path = log_path
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_http_ok(f"{base_url}/")
        yield {
            "base_url": base_url,
            "port": port,
            "proc": proc,
            "log_path": str(log_path),
        }
    finally:
        _stop_proc(proc)


@pytest.fixture(scope="session")
def e2e_auth_headers(e2e_backend):
    """``Authorization`` header dict for the real backend under test.

    Every non-exempt API path (and ``/ws``) requires ``Authorization: Bearer
    <token>`` where the token is the one the backend minted into its own
    ``<vault>/state/web_ui_token``; tests that build their own ``httpx``
    clients pass this dict as ``headers=``.  Session scoped because the token
    file is never regenerated, not even by the ``restart_backend`` fixture
    (it restarts against the same vault).
    """
    return _auth_headers(e2e_backend["vault"], e2e_backend["base_url"])


@pytest.fixture(scope="session")
def workspace(e2e_backend):
    """Create a purpose=research workspace inside the e2e vault."""
    ws_root = Path(e2e_backend["vault"]) / "e2e_ws"
    ws_root.mkdir(parents=True, exist_ok=True)
    resp = httpx.post(
        f"{e2e_backend['base_url']}/api/workspace",
        json={"path": str(ws_root), "purpose": "research"},
        headers=_auth_headers(e2e_backend["vault"], e2e_backend["base_url"]),
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

    The replacement process is written back into ``e2e_backend``'s mutable
    state, and the session-scoped ``e2e_backend`` fixture owns final
    cleanup: this fixture deliberately has NO teardown of its own, so a
    restart never kills the shared session-scoped backend out from under
    later tests.
    """

    def _restart():
        _stop_proc(e2e_backend["proc"])
        new_proc = _start_backend(e2e_backend["vault"], e2e_backend["port"])
        e2e_backend["proc"] = new_proc
        _wait_http_ok(f"{e2e_backend['base_url']}/health")
        return new_proc

    yield _restart


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Launch Chromium with container-friendly flags (no sandbox)."""
    return {
        **browser_type_launch_args,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }


# ---------------------------------------------------------------------------
# auth-boundary session cookie -- injected into every browser context
# ---------------------------------------------------------------------------
# The boundary is CORRECT and is not worked around here: ``/api/*`` requires a
# credential, the cookie is minted on ``GET /`` only
# (``auth_boundary.COOKIE_MINT_PATHS``) and the server sends it as
# ``Max-Age=43200; HttpOnly; SameSite=Strict; Path=/``. A context that starts
# empty therefore gets 401 on every frontend fetch and the UI hangs on
# "Loading config...". Injecting the server's own cookie value into the context
# IS the missing login step -- nothing is exempted, no token reaches page JS and
# no route is bypassed.
#
# Cookies are host-scoped, NOT port-scoped, so one cookie per distinct hostname
# covers both the backend port and the Vite port.


@pytest.fixture(scope="session")
def e2e_session_cookies(e2e_backend, e2e_frontend, e2e_auth_headers):
    """``add_cookies()`` payload: one boundary cookie per distinct hostname."""
    # Reuse the fixture that already resolved the token; never read the store a
    # second way.
    token = e2e_auth_headers["Authorization"].split("Bearer ", 1)[1]
    value = _session_credential(token)
    # Self-check: a broken mint must fail here, loudly, not as a mystery hang.
    assert _session_credential_ok(value, token), (
        "auth_boundary.session_credential() minted a value its own verifier "
        "rejects; the browser cookie would be refused at the boundary and the "
        "UI would hang on 'Loading config...'"
    )
    hostnames = {
        urlparse(url).hostname
        for url in (e2e_backend["base_url"], e2e_frontend["base_url"])
    }
    return [
        {
            "name": _COOKIE_NAME,
            "value": value,
            "domain": hostname,
            "path": "/",
            "httpOnly": True,
            "sameSite": "Strict",
        }
        for hostname in sorted(hostnames)
        if hostname
    ]


@pytest.fixture(autouse=True)
def _e2e_authenticated_context(request):
    """Put the session cookie on the context before any page can navigate."""
    if "page" not in request.fixturenames and "context" not in request.fixturenames:
        return
    cookies = request.getfixturevalue("e2e_session_cookies")
    context = request.getfixturevalue("context")
    context.add_cookies(cookies)
