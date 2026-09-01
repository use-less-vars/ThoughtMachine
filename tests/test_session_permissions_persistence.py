"""Per-session permission persistence across a simulated server restart (Gap B).

Runtime ``session_permissions`` grants are persisted by ``SessionManager`` /
``Bridge.save_session`` under ``session.metadata["session_config"]`` (and the
legacy ``agent_config`` key is DELETED).  After a restart, ``bridge.load_session``
re-extracts them via ``SessionManager.extract_session_config`` and re-applies the
workspace permission ceiling.

The regression this suite guards: ``workspace_routes._load_session_permissions``
used to read only ``metadata["agent_config"]``, so after a restart
``GET /api/workspace/{ws_id}/effective_permissions`` fell back to safe defaults
even though the grants were on disk under ``session_config``.

All session storage is redirected to the pytest ``tmp_path``; a "restart" is a
brand-new ``FileSystemSessionStore`` / ``SessionManager`` over the same
directories.
"""

import asyncio
import json

from agent.config.session_config import SessionConfig
from session import store as session_store_mod
from session.store import FileSystemSessionStore
from web_ui.backend import workspace_routes
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager

#: Valid SessionPermissions grains (must round-trip through the store).
GRANTS = {
    "container": False,
    "network": "banned",
    "filesystem": "write",
    "system": "read",
    "git": "write",
    "execution": "read",
    "git_read": "read",
    "git_write": "write",
    "mcp": "banned",
}


def _make_store(tmp_path):
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
        enable_session_history_pruning=False,
    )


def _make_manager(tmp_path):
    store = _make_store(tmp_path)
    return store, SessionManager(session_store=store, config_manager=ConfigManager())


def _save_grants(manager, session, grants):
    """Simulate ``bridge.apply_config(session_permissions=...)`` + ``save_session``:
    the grants land in ``metadata.session_config`` and the legacy key is removed."""
    sc = SessionConfig(**session.metadata["session_config"])
    sc.session_permissions = dict(grants)
    manager.save_config_to_session(session, sc)
    return sc


def _patch_store_factory(monkeypatch, tmp_path):
    """Point every no-arg ``FileSystemSessionStore()`` constructed inside
    ``workspace_routes`` / ``config_manager`` at the tmp_path store.

    Patches the class on BOTH the module object this file imported at
    collection time and the module currently registered under
    ``sys.modules["session.store"]``.  Integration fixtures (e.g. the
    contract-server hermetic bootstrap) purge ``session.store`` from
    ``sys.modules`` and re-import it without restoring the original, and
    ``workspace_routes._load_session_permissions`` resolves the class via a
    function-local ``from session.store import ...`` at call time, so it sees
    whichever module object is registered *then*.  Patching only the imported
    module leaves the real (default-dir) class in the live module, which
    silently resolves to the hermetic vault and returns None."""
    import sys as _sys

    store = _make_store(tmp_path)
    factory = lambda *a, **k: store
    monkeypatch.setattr(session_store_mod, "FileSystemSessionStore", factory)
    current = _sys.modules.get("session.store")
    if current is not None and current is not session_store_mod:
        monkeypatch.setattr(current, "FileSystemSessionStore", factory)
    return store


# ── Store round-trip (the runtime persistence path) ──────────────────────────


def test_session_permissions_survive_store_roundtrip(tmp_path):
    """Grants applied at runtime persist in metadata.session_config and are
    re-extracted by a fresh store/manager (simulated restart)."""
    store1, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    session = store1.load_session(session_id)
    assert session is not None
    _save_grants(manager, session, GRANTS)

    # The writer stores under session_config and removes the legacy key.
    assert "agent_config" not in session.metadata
    assert session.metadata["session_config"]["session_permissions"] == GRANTS

    # ── simulated restart: brand-new store + manager over the same dirs ──
    store2 = _make_store(tmp_path)
    manager2 = SessionManager(session_store=store2, config_manager=ConfigManager())
    loaded = manager2.load_session(session_id)
    assert loaded is not None
    sc = manager2.extract_session_config(loaded)
    assert sc is not None
    assert sc.session_permissions == GRANTS


def test_session_permissions_roundtrip_with_ceiling_reapplication(tmp_path, monkeypatch):
    """After restart, extract_session_config returns the raw saved grants while
    resolve_full_config re-applies the workspace permission ceiling (the exact
    order bridge.load_session follows)."""
    import thoughtmachine.workspace_capabilities as wcap

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(json.dumps({
        "purpose": "general",
        "permissions": {"filesystem": "read", "git": "read"},
    }))
    monkeypatch.setattr(wcap, "_workspace_dir", lambda ws_id: ws_dir)

    grants = {
        "container": False,
        "network": "banned",
        "filesystem": "write",  # above ceiling
        "system": "read",
        "git": "write",         # above ceiling
        "execution": "banned",
    }
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    session = store.load_session(session_id)
    _save_grants(manager, session, grants)

    # ── simulated restart ──
    _patch_store_factory(monkeypatch, tmp_path)
    store2 = _make_store(tmp_path)
    manager2 = SessionManager(session_store=store2, config_manager=ConfigManager())
    loaded = manager2.load_session(session_id)
    sc = manager2.extract_session_config(loaded)
    assert sc.session_permissions["filesystem"] == "write"  # raw, uncapped

    merged = ConfigManager.resolve_full_config(
        workspace_id="ws-1", session_id=session_id
    )
    sp = merged["session_permissions"]
    assert sp["filesystem"] == "read"  # capped by the saved workspace ceiling
    assert sp["git"] == "read"         # capped by the saved workspace ceiling
    assert sp["network"] == "banned"


# ── workspace_routes._load_session_permissions (the regression) ──────────────


def test_load_session_permissions_reads_session_config(tmp_path, monkeypatch):
    """The route helper reads ``session_config`` — the key every writer uses.
    Pre-fix it read only ``agent_config`` and returned None after a restart."""
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    session = store.load_session(session_id)
    _save_grants(manager, session, GRANTS)
    assert "agent_config" not in session.metadata  # writer deleted the legacy key

    _patch_store_factory(monkeypatch, tmp_path)
    assert workspace_routes._load_session_permissions(session_id) == GRANTS


def test_load_session_permissions_legacy_agent_config_fallback(tmp_path, monkeypatch):
    """Sessions saved by older backend versions (agent_config key only) still
    resolve after the fix."""
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    session = store.load_session(session_id)
    del session.metadata["session_config"]
    session.metadata["agent_config"] = {"session_permissions": {"git": "read"}}
    store.save_session(session)

    _patch_store_factory(monkeypatch, tmp_path)
    loaded = workspace_routes._load_session_permissions(session_id)
    assert isinstance(loaded, dict)
    # ``Session.from_persistable_dict`` coerces partial maps by merging with
    # the safe defaults, so the legacy grant must be preserved as a key/value.
    assert loaded["git"] == "read"
    assert loaded["execution"] == "banned"  # safe default untouched


def test_load_session_permissions_missing_session_returns_none(tmp_path, monkeypatch):
    """Unknown session id still yields None (route falls back to safe defaults)."""
    _patch_store_factory(monkeypatch, tmp_path)
    assert workspace_routes._load_session_permissions("does-not-exist") is None


# ── GET .../effective_permissions after restart ──────────────────────────────


def test_effective_permissions_restores_grants_after_restart(tmp_path, monkeypatch):
    """Full route round-trip: grants saved before a restart are returned by the
    effective-permissions endpoint afterwards (not the safe defaults)."""
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    session = store.load_session(session_id)
    _save_grants(manager, session, GRANTS)

    # ── simulated restart: fresh store factory + hermetic workspace dirs ──
    _patch_store_factory(monkeypatch, tmp_path)
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path / "ws")
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)
    monkeypatch.setattr(workspace_routes, "load_workspace_capabilities",
                        lambda ws_id: None)

    result = asyncio.run(
        workspace_routes.get_effective_permissions("ws-1", session_id=session_id)
    )
    eff = result["effective_permissions"]
    # Pre-fix these would be the safe defaults (read / read / banned).
    assert eff["filesystem"] == "write"
    assert eff["git"] == "write"
    assert eff["execution"] == "read"
    assert eff["network"] == "banned"
