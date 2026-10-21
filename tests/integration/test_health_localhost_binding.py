"""F30 — the health routes answer localhost callers only.

``web_ui/backend/server.py`` binds ``127.0.0.1`` by default and the three
health routes (``GET /health``, ``GET /api/health``,
``GET /api/health/containers``) reject non-local callers with ``403``.

These tests drive the REAL app with a hand-built ASGI ``scope`` so the peer
address (``scope["client"]``) is chosen explicitly.  ``TestClient(client=...)``
would be the shorter route, but that keyword only exists in starlette
>= 0.37.2 and this repo does not pin starlette; a raw scope works with any
version and additionally lets us exercise ``scope["client"] = None``, which no
``TestClient`` variant can produce.

Hermetic harness mirrors tests/integration/test_server_health.py: temp HOME +
patched ``Path.home()`` + purged/re-imported ``web_ui.backend`` modules so the
module-level singletons are built against the temp HOME.  No network, no LLM,
no Docker daemon.

Run (from repo root):
    python -m pytest tests/integration/test_health_localhost_binding.py -v
"""

from __future__ import annotations

import asyncio
import importlib
import os
import pathlib
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from tests.integration.harness import SysModulesSnapshot

pytestmark = pytest.mark.integration


REMOTE_CLIENT = ("10.1.2.3", 5555)
LOOPBACK_CLIENT = ("127.0.0.1", 5555)
LOOPBACK_CLIENT_V6 = ("::1", 5555)


# ════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness (mirror of test_server_health.py)
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server."""
    tmp_home = tempfile.mkdtemp(prefix="test_health_localhost_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # Re-import server so module-level singletons (_session_store, registries,
    # _SERVER_REVISION) are built against the temp HOME, not the real one.
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile", "thoughtmachine.bootstrap", "session")
    snapshot = SysModulesSnapshot(mod_prefixes)
    snapshot.snapshot()
    snapshot.purge()

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val
    shutil.rmtree(tmp_home, ignore_errors=True)
    snapshot.restore()


# ════════════════════════════════════════════════════════════════════════════
# Raw-ASGI request driver
# ════════════════════════════════════════════════════════════════════════════

def _asgi_get(app, path: str, client):
    """GET *path* against *app*, with an explicit ASGI peer address.

    *client* is placed verbatim into ``scope["client"]``; ``None`` models an
    ASGI server that cannot report a peer address.  Returns ``(status, body)``.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8000")],
        "client": client,
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


# ════════════════════════════════════════════════════════════════════════════
# 1 — remote callers are refused
# ════════════════════════════════════════════════════════════════════════════

def test_health_rejects_remote_client(contract_server):
    """GET /health from a non-local peer -> 403, no health payload."""
    app, _ = contract_server
    status, body = _asgi_get(app, "/health", REMOTE_CLIENT)

    assert status == 403
    assert b"thoughtmachine-web-ui" not in body
    assert b"revision" not in body


def test_health_rejection_body_does_not_echo_client_address(contract_server):
    """The 403 body must not leak the caller's address (or port)."""
    app, _ = contract_server
    status, body = _asgi_get(app, "/health", REMOTE_CLIENT)

    assert status == 403
    assert b"10.1.2.3" not in body
    assert b"5555" not in body


def test_api_health_rejects_remote_client(contract_server):
    """GET /api/health from a non-local peer -> 403 (alias shares the gate)."""
    app, _ = contract_server
    status, body = _asgi_get(app, "/api/health", REMOTE_CLIENT)

    assert status == 403
    assert b"ok" not in body


def test_health_containers_rejects_remote_client(contract_server):
    """GET /api/health/containers from a non-local peer -> 403."""
    app, _ = contract_server
    status, _body = _asgi_get(app, "/api/health/containers", REMOTE_CLIENT)

    assert status == 403


# ════════════════════════════════════════════════════════════════════════════
# 2 — unknown peer address fails closed
# ════════════════════════════════════════════════════════════════════════════

def test_health_rejects_missing_client_address(contract_server):
    """scope["client"] = None (server cannot report a peer) -> 403, fail closed."""
    app, _ = contract_server
    status, _body = _asgi_get(app, "/health", None)

    assert status == 403


# ════════════════════════════════════════════════════════════════════════════
# 3 — loopback callers keep the original contract
# ════════════════════════════════════════════════════════════════════════════

def test_health_allows_ipv4_loopback(contract_server):
    """GET /health from 127.0.0.1 -> 200 with the identity payload."""
    app, _ = contract_server
    status, body = _asgi_get(app, "/health", LOOPBACK_CLIENT)

    assert status == 200
    assert b"thoughtmachine-web-ui" in body


def test_health_allows_ipv6_loopback(contract_server):
    """GET /health from ::1 -> 200 (IPv6 loopback is a local caller too)."""
    app, _ = contract_server
    status, body = _asgi_get(app, "/health", LOOPBACK_CLIENT_V6)

    assert status == 200
    assert b"thoughtmachine-web-ui" in body


def test_api_health_allows_loopback(contract_server):
    """GET /api/health from 127.0.0.1 -> 200."""
    app, _ = contract_server
    status, body = _asgi_get(app, "/api/health", LOOPBACK_CLIENT)

    assert status == 200
    assert b"thoughtmachine-web-ui" in body


# ════════════════════════════════════════════════════════════════════════════
# 4 — the in-process TestClient still works
# ════════════════════════════════════════════════════════════════════════════

def test_api_health_allows_test_client(contract_server):
    """TestClient (peer 'testclient') is treated as local so harnesses keep working."""
    app, _ = contract_server
    resp = TestClient(app).get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
