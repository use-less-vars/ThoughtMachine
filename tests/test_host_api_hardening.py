"""
Host API hardening tests for web_ui/backend/server.py.

Covers the three host-facing fixes:

1. CLI binds to 127.0.0.1 by default (was 0.0.0.0) — the Web UI must not
   expose an unauthenticated API to the whole network.
2. /api/browse and /api/browse/create are confined to $HOME minus the vault
   root (~/.thoughtmachine): paths that resolve outside the home directory,
   into the vault, or (for create) escape the parent via ../ are rejected.
3. The container lifecycle endpoints validate an explicit ``workspace_path``:
   it must be an existing directory under $HOME, outside the vault
   (~/.thoughtmachine), AND within the registered workspace root for the
   requested workspace id. Violations are HTTP 400 (invalid path or
   unregistered workspace) or HTTP 403 (path outside the registered root).
   An empty ``workspace_path`` still falls back to the workspace registry
   unchanged (404 for an unknown workspace).
"""
from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import sys as sys_mod
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient
from thoughtmachine.workspace_registry import WorkspaceRegistry


@pytest.fixture(scope="module")
def clean_home():
    """Create temp HOME, patch Path.home() + HOME env, clear API-key/HOST env vars."""
    # ── 1. Create temp home ────────────────────────────────────────────────
    tmp_home = tempfile.mkdtemp(prefix="test_webui_home_")
    fake_home_path = Path(tmp_home)

    # ── 2. Set HOME env var ────────────────────────────────────────────────
    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    # ── 3. Clear API key + server env vars ─────────────────────────────────
    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY",
                "ANTHROPIC_API_KEY", "HOST", "PORT", "RELOAD"):
        saved_env[key] = os.environ.pop(key, None)

    # ── 4. Start persistent Path.home() patch ──────────────────────────────
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # ── 5. Remove affected modules from cache & re-import ──────────────────
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile",
                    "thoughtmachine.bootstrap")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    # Ensure the project root is on sys.path so web_ui can be found
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys_mod.path:
        sys_mod.path.insert(0, _project_root)

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield server_mod, app, tmp_home

    # ── 6. Cleanup ─────────────────────────────────────────────────────────
    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val

    shutil.rmtree(tmp_home, ignore_errors=True)


@pytest.fixture
def client(clean_home):
    """Yield a TestClient wrapping the patched server app."""
    _, app, _ = clean_home
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def registered_ws(clean_home):
    """Register a test workspace inside the temp home and expose its dirs.

    The registry default instance lives under the patched HOME
    (~/.thoughtmachine/state/workspace_registry.json), so registering here
    persists for the whole module and is visible to the server app.
    """
    _, _, tmp_home = clean_home
    root = os.path.join(tmp_home, "ws-root")
    sub = os.path.join(root, "sub")
    sibling = os.path.join(tmp_home, "sibling")
    os.makedirs(sub, exist_ok=True)
    os.makedirs(sibling, exist_ok=True)
    WorkspaceRegistry.get_default().register_workspace(
        "ws-hardening", root, label="host-api-hardening-test"
    )
    return {
        "ws_id": "ws-hardening",
        "root": root,
        "sub": sub,
        "sibling": sibling,
        "home": tmp_home,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 — default bind host
# ══════════════════════════════════════════════════════════════════════════════

def test_cli_default_host_is_loopback(clean_home):
    """The server must bind to 127.0.0.1 by default, not 0.0.0.0."""
    server_mod, _, _ = clean_home
    args = server_mod.build_parser().parse_args([])
    assert args.host == "127.0.0.1"


def test_cli_host_env_override(clean_home, monkeypatch):
    """The HOST env var still overrides the default."""
    server_mod, _, _ = clean_home
    monkeypatch.setenv("HOST", "0.0.0.0")
    args = server_mod.build_parser().parse_args([])
    assert args.host == "0.0.0.0"


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 — /api/browse confinement (home minus vault)
# ══════════════════════════════════════════════════════════════════════════════

def test_browse_home_ok(client, clean_home):
    """Browsing $HOME (default and explicit) works and returns the home dir."""
    _, _, tmp_home = clean_home
    expected = os.path.realpath(tmp_home)

    resp = client.get("/api/browse")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["current_path"] == expected

    resp = client.get("/api/browse", params={"path": tmp_home})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["current_path"] == expected


def test_browse_rejects_root(client):
    """The filesystem root is outside $HOME and must be rejected."""
    resp = client.get("/api/browse", params={"path": "/"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "outside the allowed home" in data["error"]


def test_browse_rejects_etc(client):
    """A system directory outside $HOME must be rejected."""
    resp = client.get("/api/browse", params={"path": "/etc"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "outside the allowed home" in data["error"]


def test_browse_rejects_vault(client, clean_home):
    """~/.thoughtmachine (vault root) must be rejected even though it lives
    under $HOME."""
    _, _, tmp_home = clean_home
    vault = os.path.join(tmp_home, ".thoughtmachine")
    resp = client.get("/api/browse", params={"path": vault})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "vault" in data["error"].lower()


def test_browse_nonexistent_dir(client, clean_home):
    """An in-home path that does not exist yields the usual 'Not a directory'."""
    _, _, tmp_home = clean_home
    resp = client.get("/api/browse",
                      params={"path": os.path.join(tmp_home, "does-not-exist")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "Not a directory" in data["error"]


def test_browse_rejects_symlink_escape(client, clean_home):
    """A symlink inside $HOME pointing outside must be rejected after
    resolution (no symlink-based escape from the confinement)."""
    _, _, tmp_home = clean_home
    outside = tempfile.mkdtemp(prefix="test_outside_home_")
    try:
        link = os.path.join(tmp_home, "escape-link")
        os.symlink(outside, link)
        resp = client.get("/api/browse", params={"path": link})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "outside the allowed home" in data["error"]
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 — /api/browse/create confinement
# ══════════════════════════════════════════════════════════════════════════════

def test_create_directory_ok(client, clean_home):
    """Creating a directory inside $HOME works and it becomes browsable."""
    _, _, tmp_home = clean_home
    resp = client.post("/api/browse/create",
                       json={"parent_path": tmp_home, "name": "subdir"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert os.path.isdir(os.path.join(tmp_home, "subdir"))

    resp = client.get("/api/browse",
                      params={"path": os.path.join(tmp_home, "subdir")})
    assert resp.json()["success"] is True


def test_create_rejects_traversal(client, clean_home):
    """A name containing ../ must not escape the parent directory."""
    _, _, tmp_home = clean_home
    resp = client.post("/api/browse/create",
                       json={"parent_path": tmp_home, "name": "../../escape"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert not os.path.exists(os.path.join(os.path.dirname(tmp_home), "escape"))


def test_create_rejects_outside_parent(client):
    """A parent path outside $HOME must be rejected."""
    resp = client.post("/api/browse/create",
                       json={"parent_path": "/tmp", "name": "x"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False


def test_create_rejects_vault_parent(client, clean_home):
    """A parent path inside the vault must be rejected."""
    _, _, tmp_home = clean_home
    vault = os.path.join(tmp_home, ".thoughtmachine")
    resp = client.post("/api/browse/create",
                       json={"parent_path": vault, "name": "x"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "vault" in data["error"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3 — container lifecycle workspace_path validation
# ══════════════════════════════════════════════════════════════════════════════

def test_container_start_rejects_vault_path(client, clean_home):
    """workspace_path inside the vault → HTTP 400."""
    _, _, tmp_home = clean_home
    vault = os.path.join(tmp_home, ".thoughtmachine")
    resp = client.post("/api/workspace/w1/containers/c1/start",
                       params={"workspace_path": vault})
    assert resp.status_code == 400
    assert "vault" in resp.json()["error"].lower()


def test_container_start_rejects_above_home(client):
    """workspace_path outside $HOME → HTTP 400."""
    resp = client.post("/api/workspace/w1/containers/c1/start",
                       params={"workspace_path": "/etc"})
    assert resp.status_code == 400
    assert "outside the allowed home" in resp.json()["error"]


def test_container_start_rejects_root(client):
    """workspace_path = / (filesystem root) → HTTP 400."""
    resp = client.post("/api/workspace/w1/containers/c1/start",
                       params={"workspace_path": "/"})
    assert resp.status_code == 400


def test_container_start_rejects_nonexistent(client, clean_home):
    """workspace_path that is not an existing directory → HTTP 400."""
    _, _, tmp_home = clean_home
    resp = client.post("/api/workspace/w1/containers/c1/start",
                       params={"workspace_path": os.path.join(tmp_home, "nope")})
    assert resp.status_code == 400
    assert "not an existing directory" in resp.json()["error"]


def test_container_start_registered_root_and_subdir_pass_validation(client, registered_ws):
    """workspace_path = the registered root or a subdir of it is legitimate:
    the request proceeds into ContainerManager (409/503 without a Docker
    daemon) — never a 400/403 path-validation failure."""
    for path in (registered_ws["root"], registered_ws["sub"]):
        resp = client.post(
            f"/api/workspace/{registered_ws['ws_id']}/containers/c1/start",
            params={"workspace_path": path},
        )
        assert resp.status_code not in (400, 403)
        error = resp.json().get("error", "")
        assert "outside the allowed home" not in error
        assert "vault" not in error.lower()
        assert "registered workspace root" not in error


def test_container_start_rejects_sibling_dir_403(client, registered_ws):
    """An in-home directory that is NOT within the registered workspace root
    must be rejected with HTTP 403 (no arbitrary home dirs in containers)."""
    resp = client.post(
        f"/api/workspace/{registered_ws['ws_id']}/containers/c1/start",
        params={"workspace_path": registered_ws["sibling"]},
    )
    assert resp.status_code == 403
    assert "registered workspace root" in resp.json()["error"]


def test_container_start_unknown_ws_explicit_path_400(client, registered_ws):
    """Explicit workspace_path with an unregistered ws_id → HTTP 400."""
    resp = client.post(
        "/api/workspace/not-a-registered-ws/containers/c1/start",
        params={"workspace_path": registered_ws["root"]},
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["error"]


def test_container_start_empty_path_falls_back_to_registry(client):
    """No explicit workspace_path: registry lookup for an unknown workspace
    → 404 (unchanged behavior, not a validation error)."""
    resp = client.post("/api/workspace/w1/containers/c1/start")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


def test_container_list_rejects_vault_path(client, clean_home):
    """Validation is centralized: non-start lifecycle endpoints reject an
    explicit vault workspace_path with HTTP 400 too."""
    _, _, tmp_home = clean_home
    vault = os.path.join(tmp_home, ".thoughtmachine")
    resp = client.get("/api/workspace/w1/containers",
                      params={"workspace_path": vault})
    assert resp.status_code == 400
    assert "vault" in resp.json()["error"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4 — /api/workspace/resolve registration confinement
# ══════════════════════════════════════════════════════════════════════════════

def test_resolve_registers_new_path_under_home(client, clean_home):
    """A NEW registration under $HOME (outside the vault) succeeds and is
    idempotent: a second resolve returns the same workspace id."""
    _, _, tmp_home = clean_home
    ws_dir = os.path.join(tmp_home, "ws-new")
    os.makedirs(ws_dir, exist_ok=True)

    resp = client.post("/api/workspace/resolve", json={"path": ws_dir})
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_id"]
    assert data["root"] == os.path.realpath(ws_dir)

    resp2 = client.post("/api/workspace/resolve", json={"path": ws_dir})
    assert resp2.status_code == 200
    assert resp2.json()["workspace_id"] == data["workspace_id"]


def test_resolve_rejects_root_403(client):
    """The filesystem root is outside $HOME → HTTP 403."""
    resp = client.post("/api/workspace/resolve", json={"path": "/"})
    assert resp.status_code == 403
    assert "outside the allowed home" in resp.json()["detail"]


def test_resolve_rejects_etc_403(client):
    """A system directory outside $HOME → HTTP 403."""
    resp = client.post("/api/workspace/resolve", json={"path": "/etc"})
    assert resp.status_code == 403
    assert "outside the allowed home" in resp.json()["detail"]


def test_resolve_rejects_vault_403(client, clean_home):
    """~/.thoughtmachine (the trust anchor) must never be registered → 403."""
    _, _, tmp_home = clean_home
    vault = os.path.join(tmp_home, ".thoughtmachine")
    resp = client.post("/api/workspace/resolve", json={"path": vault})
    assert resp.status_code == 403
    assert "vault" in resp.json()["detail"].lower()


def test_resolve_rejects_nonexistent_400(client, clean_home):
    """A path that is not an existing directory → HTTP 400."""
    _, _, tmp_home = clean_home
    resp = client.post("/api/workspace/resolve",
                       json={"path": os.path.join(tmp_home, "does-not-exist")})
    assert resp.status_code == 400
    assert "not an existing directory" in resp.json()["detail"]


def test_resolve_rejects_empty_path_400(client):
    """An empty path is a client error, not a registration attempt."""
    resp = client.post("/api/workspace/resolve", json={"path": ""})
    assert resp.status_code == 400
    assert "path is required" in resp.json()["detail"]


def test_resolve_rejects_symlink_escape_403(client, clean_home):
    """A symlink inside $HOME pointing outside must be rejected after
    resolution (no symlink-based escape from the confinement)."""
    _, _, tmp_home = clean_home
    outside = tempfile.mkdtemp(prefix="test_outside_home_")
    try:
        link = os.path.join(tmp_home, "resolve-escape-link")
        os.symlink(outside, link)
        resp = client.post("/api/workspace/resolve", json={"path": link})
        assert resp.status_code == 403
        assert "outside the allowed home" in resp.json()["detail"]
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_resolve_registered_under_home_returns_existing(client, registered_ws):
    """An already-registered workspace under $HOME resolves to its id."""
    resp = client.post("/api/workspace/resolve",
                       json={"path": registered_ws["root"]})
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == registered_ws["ws_id"]


def test_resolve_pre_registered_outside_home_still_resolves(client, clean_home):
    """Registry entries written by trusted code (bootstrap/server startup)
    resolve even when outside $HOME — only NEW registrations are confined to
    home-minus-vault (the registry itself lives in the vault/trust anchor)."""
    outside = tempfile.mkdtemp(prefix="test_pre_registered_")
    try:
        WorkspaceRegistry.get_default().register_workspace(
            "ws-pre-registered", outside, label="pre-registered-outside"
        )
        resp = client.post("/api/workspace/resolve", json={"path": outside})
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == "ws-pre-registered"
    finally:
        shutil.rmtree(outside, ignore_errors=True)

