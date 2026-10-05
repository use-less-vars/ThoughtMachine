"""Per-session permission persistence across a simulated server restart (Gap B).

Runtime ``session_permissions`` grants are persisted as a **sidecar file**, not
in the session record: ``thoughtmachine.permission_store.write_session_permissions``
writes ``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json`` and
``read_session_permissions`` reads it back (normalising legacy ``git_read`` /
``git_write`` grains and dropping non-catalog keys).  The session record itself
no longer carries a grants map -- ``create_session`` emits no
``session_permissions`` under ``metadata["session_config"]`` and there is no
``agent_config`` fallback on the write path.

The workspace permission ceiling is still re-applied *after* the raw sidecar
grants are read, via ``ConfigManager.resolve_effective_permissions`` (the
runtime order) and via the ``GET /api/workspace/{ws_id}/effective_permissions``
route (``workspace_routes.get_effective_permissions``).

The regression this suite guards: ``workspace_routes._load_session_permissions``
used to read only ``metadata["agent_config"]``, so after a restart
``GET /api/workspace/{ws_id}/effective_permissions`` fell back to safe defaults
even though the grants were on disk.  ``_load_session_permissions`` is now the
*legacy* fallback only (reached when the canonical sidecar read raises).

All session storage is redirected to the pytest ``tmp_path``; a "restart" is a
brand-new ``FileSystemSessionStore`` / ``SessionManager`` over the same
directories.
"""

import asyncio
import json

from session import store as session_store_mod
from session.store import FileSystemSessionStore
from thoughtmachine.permission_store import (
    read_session_permissions,
    session_grants_path,
    write_session_permissions,
)
from web_ui.backend import workspace_routes
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager

#: Valid SessionPermissions grains (must round-trip through the sidecar).
GRANTS = {
    "container": False,
    "network": "banned",
    "filesystem": "write",
    "git": "write",
    "mcp": "banned",
    "host_bash": "banned",
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
    silently resolves to the hermetic vault and returns None.
    """
    import sys as _sys

    store = _make_store(tmp_path)
    factory = lambda *a, **k: store
    monkeypatch.setattr(session_store_mod, "FileSystemSessionStore", factory)
    current = _sys.modules.get("session.store")
    if current is not None and current is not session_store_mod:
        monkeypatch.setattr(current, "FileSystemSessionStore", factory)
    return store


def _write_workspace_config(vault, ws_id, permissions):
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(json.dumps({
        "purpose": "general",
        "permissions": permissions,
    }))


# ── Store round-trip (the runtime persistence path) ──────────────────────────


def test_session_permissions_survive_store_roundtrip(tmp_path):
    """Grants written to the sidecar persist across a store/manager restart."""
    vault = tmp_path / ".thoughtmachine"
    store1, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")

    write_session_permissions(vault, "ws-1", session_id, GRANTS)
    assert session_grants_path(vault, "ws-1", session_id).exists()

    # The session record is NOT a grants holder: neither the canonical
    # session_config key nor the legacy agent_config key carries them.
    session = store1.load_session(session_id)
    assert session is not None
    assert "session_permissions" not in (session.metadata.get("session_config") or {})
    assert "agent_config" not in session.metadata

    # ── simulated restart: brand-new store + manager over the same dirs ──
    store2 = _make_store(tmp_path)
    manager2 = SessionManager(session_store=store2, config_manager=ConfigManager())
    loaded = manager2.load_session(session_id)
    assert loaded is not None

    # The sidecar is the source of truth and round-trips the full grants map.
    assert read_session_permissions(vault, "ws-1", session_id) == GRANTS


def test_session_permissions_roundtrip_with_ceiling_reapplication(
    tmp_path, monkeypatch, hermetic_vault
):
    """After restart the sidecar returns the raw saved grants while
    resolve_effective_permissions re-applies the workspace permission ceiling
    (the exact order the runtime follows)."""
    vault = hermetic_vault
    _write_workspace_config(
        vault, "ws-1", {"filesystem": "read", "git": "read"}
    )
    grants = {
        "container": False,
        "network": "banned",
        "filesystem": "write",  # above ceiling
        "git": "write",         # above ceiling
        "mcp": "banned",
        "host_bash": "banned",
    }
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    write_session_permissions(vault, "ws-1", session_id, grants)

    # ── simulated restart ──
    _patch_store_factory(monkeypatch, tmp_path)
    store2 = _make_store(tmp_path)
    manager2 = SessionManager(session_store=store2, config_manager=ConfigManager())
    loaded = manager2.load_session(session_id)
    assert loaded is not None

    # Raw sidecar grants are uncapped...
    assert read_session_permissions(vault, "ws-1", session_id) == grants
    assert read_session_permissions(vault, "ws-1", session_id)["filesystem"] == "write"

    # ...and the ceiling is re-applied by the runtime resolver.
    sp = ConfigManager.resolve_effective_permissions(
        None, session_id=session_id, workspace_id="ws-1"
    )
    assert sp["filesystem"] == "read"  # capped by the saved workspace ceiling
    assert sp["git"] == "read"         # capped by the saved workspace ceiling
    assert sp["network"] == "banned"


# ── workspace_routes._load_session_permissions (the legacy fallback) ─────────


def test_load_session_permissions_reads_session_config(tmp_path, hermetic_vault):
    """The route helper reads the canonical permission sidecar (P1).

    The session record is no longer a grants holder; the grants live in
    ``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json`` written by
    ``write_session_permissions``.
    """
    vault = hermetic_vault
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    write_session_permissions(vault, "ws-1", session_id, GRANTS)

    assert workspace_routes._load_session_permissions(session_id, "ws-1") == GRANTS


def test_load_session_permissions_missing_session_returns_none(tmp_path, hermetic_vault):
    """Unknown session id still yields None (route falls back to safe defaults)."""
    assert workspace_routes._load_session_permissions("does-not-exist", "ws-1") is None


# ── GET .../effective_permissions after restart ──────────────────────────────


def test_effective_permissions_restores_grants_after_restart(
    tmp_path, monkeypatch, hermetic_vault
):
    """Full route round-trip: grants saved before a restart are returned by the
    effective-permissions endpoint afterwards (not the safe defaults)."""
    vault = hermetic_vault
    # Permissive ceiling so the raw grants (write/write) survive uncapped,
    # preserving the original endpoint assertions.
    _write_workspace_config(
        vault, "ws-1", {"filesystem": "write", "git": "write"}
    )
    store, manager = _make_manager(tmp_path)
    session_id, _ = manager.create_session(mode="agent")
    write_session_permissions(vault, "ws-1", session_id, GRANTS)

    # ── simulated restart: fresh store factory + hermetic workspace dirs ──
    _patch_store_factory(monkeypatch, tmp_path)
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
    assert eff["network"] == "banned"
    assert eff["mcp"] == "banned"
    assert eff["host_bash"] == "banned"
