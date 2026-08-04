"""Contract test — every session-creation path must yield a valid SessionConfig.

STAGE 1: this test is intentionally RED against the current code.  The routing
fixes (Tasks 1-2) are NOT applied yet:

  * REST POST /api/session/create and WS ``new_session`` both flow through
    ``SessionManager.create_session`` (web_ui/backend/session_manager.py:64),
    which initialises ``provider_id=""`` and ``model=""`` (lines 91-92) and only
    copies them from global defaults when truthy.  Under the hermetic vault the
    factory defaults carry empty provider/model, so those two assertions FAIL.
  * WS ``set_project`` (web_ui/backend/server.py:1750) builds a bare
    ``Session()`` with NO ``session_config`` metadata at all, so every
    assertion FAILS.

This is the point: the contract is non-empty provider_id / model / enabled_tools
and max_turns > 0, and we must prove the gap before fixing it.

Run (from repo root):
    python -m pytest tests/integration/test_session_creation_contract.py -v

Hermetic: temp HOME + patched ``Path.home()`` + purged/re-imported server
modules, exactly like tests/web_ui/backend/test_websocket_integration.py.
No network, no LLM, no Docker daemon involved.
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

pytestmark = pytest.mark.integration


# ══════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server.

    Module-scoped so all three paths share one hermetic vault/store, mirroring
    the `pathed_server` fixture in tests/web_ui/backend/test_websocket_integration.py.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_session_contract_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # Re-import server so module-level singletons (_session_store, registries)
    # are built against the temp HOME, not the real one.
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile", "thoughtmachine.bootstrap")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

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


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _server_session_store():
    """The store the WebSocket handler persists to (server module global)."""
    from web_ui.backend.server import _get_session_store
    return _get_session_store()


def _load_session(session_id: str, workspace_id):
    """Load a session from the server's store, tolerant of workspace scoping."""
    store = _server_session_store()
    session = store.load_session(session_id, workspace_id=workspace_id or None)
    if session is None:
        session = store.load_session(session_id, workspace_id=None)
    return session


def _assert_valid_session_config(session, path_label: str) -> None:
    """The contract: a persisted session must carry a complete SessionConfig.

    provider_id / model / enabled_tools must be non-empty; max_turns > 0;
    mode must be set.  Do NOT weaken these to match current behaviour.
    """
    assert session is not None, f"{path_label}: no session was persisted"

    cfg = session.metadata.get("session_config") or {}
    assert isinstance(cfg, dict), (
        f"{path_label}: session_config metadata is not a dict (got {type(cfg).__name__})"
    )

    assert cfg.get("mode"), f"{path_label}: mode missing/empty in session_config (got {cfg.get('mode')!r})"

    assert cfg.get("provider_id"), (
        f"{path_label}: provider_id missing/empty in session_config (got {cfg.get('provider_id')!r})"
    )

    assert cfg.get("model"), (
        f"{path_label}: model missing/empty in session_config (got {cfg.get('model')!r})"
    )

    enabled_tools = cfg.get("enabled_tools") or []
    assert enabled_tools, f"{path_label}: enabled_tools empty/missing in session_config"

    max_turns = cfg.get("max_turns", 0)
    assert isinstance(max_turns, int) and max_turns > 0, (
        f"{path_label}: max_turns not > 0 (got {max_turns!r})"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Path 1: REST POST /api/session/create
# ══════════════════════════════════════════════════════════════════════════════

def test_rest_create_session_produces_valid_config(contract_server):
    """POST /api/session/create must persist a session with a valid SessionConfig."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        body = resp.json()
        session_id = body["session_id"]
        assert body["mode"] == "custom"

        # The REST route persists via FileSystemSessionStore.get_instance()
        from session.store import FileSystemSessionStore
        session = FileSystemSessionStore.get_instance().load_session(session_id, workspace_id=None)

    _assert_valid_session_config(session, "REST POST /api/session/create")


# ══════════════════════════════════════════════════════════════════════════════
# Path 2: WS new_session command (routed via bridge.create_session → SessionManager)
# ══════════════════════════════════════════════════════════════════════════════

def test_ws_new_session_produces_valid_config(contract_server):
    """WS `new_session` must persist a session with a valid SessionConfig."""
    app, _ = contract_server

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "new_session", "mode": "custom"})
            first = ws.receive_json()
            assert first["type"] == "session_loaded", (
                f"expected session_loaded first, got {first.get('type')!r}"
            )
            session_id = first["session_id"]
            workspace_id = first.get("workspace_id") or None

        session = _load_session(session_id, workspace_id)

    _assert_valid_session_config(session, "WS new_session")


# ══════════════════════════════════════════════════════════════════════════════
# Path 3: WS set_project command (server.py:1750 — direct Session(), un-routed)
# ══════════════════════════════════════════════════════════════════════════════

def test_ws_set_project_produces_valid_config(contract_server):
    """WS `set_project` creates a session; it must also carry a valid SessionConfig."""
    app, _ = contract_server
    project_dir = tempfile.mkdtemp(prefix="contract_project_")
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"command": "set_project", "project": project_dir})
                first = ws.receive_json()
                assert first["type"] == "session_loaded", (
                    f"expected session_loaded first, got {first.get('type')!r}"
                )
                session_id = first["session_id"]
                workspace_id = first.get("workspace_id") or None

            session = _load_session(session_id, workspace_id)
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)

    _assert_valid_session_config(session, "WS set_project")
