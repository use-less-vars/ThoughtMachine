"""
Regression tests: a server restart must never collapse the FULL stored
session permission grants down to the workspace ceiling.

Bug being fixed: ``WebAgentBridge.load_session`` re-caps the stored
``session_permissions`` through the current workspace permission ceiling
(correct for the *effective* config) but then persists the CAPPED dump back
into the session metadata (``save_config_to_session``).  After exactly one
restart the stored full grants (filesystem=write, container=true,
git_write=write, network=ask, ...) are permanently replaced by the capped
values (filesystem=read, container=false, network=banned, ...) — the grant
is lost even after the ceiling is later relaxed.

Fixed behavior:
1.  effective permissions after load are still capped through the ceiling
    (security invariant, unchanged), and
2.  the STORED session_config.session_permissions keeps the full grants
    (restart-safe persistence).
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore


@pytest.fixture
def temp_store(tmp_path):
    """Return a FileSystemSessionStore rooted in the pytest tmp_path."""
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
        enable_session_history_pruning=False,
    )


def _patch_ceiling(monkeypatch, fake):
    """Monkeypatch the workspace ceiling loader, binding the module at run time.

    Same idiom as tests/test_session_load_ceiling.py — WebAgentBridge.load_session
    resolves the ceiling loader lazily via sys.modules at call time, so the
    patch must target the module object that is current during the test.
    """
    import web_ui.backend.config_manager as config_manager

    monkeypatch.setattr(config_manager, "_load_workspace_permission_ceiling", fake)


def _disk_permissions(temp_store, session_id):
    """Return the session_permissions dict stored in the session file metadata."""
    path = temp_store._find_session_path(session_id)
    assert path is not None, "session file not found on disk"
    with open(path, "r") as f:
        raw = json.load(f)
    return raw.get("metadata", {}).get("session_config", {}).get("session_permissions", {})


FULL_PERMS = {
    "filesystem": "write",
    "container": True,
    "git_write": "write",
    "network": "ask",
    "system": "read",
}

TIGHTENED_CEILING = {
    "filesystem": "read",
    "container": False,
    "network": "banned",
}


class TestSessionPermissionsRestartFix:
    """Loading a saved session caps the EFFECTIVE config but never the STORED one."""

    def test_restart_keeps_stored_full_grants_while_effective_is_capped(self, temp_store, monkeypatch):
        """Save full grants, tighten the ceiling, restart -> effective capped, storage intact."""
        ceiling = {}

        def fake_ceiling(ws_id):
            return dict(ceiling)

        _patch_ceiling(monkeypatch, fake_ceiling)

        # ── save: workspace set, ceiling permissive -> full grants stored ──────
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-restart-repro"
        bridge.apply_config({"session_permissions": dict(FULL_PERMS)})
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        disk_perms = _disk_permissions(temp_store, session_id)
        for key, value in FULL_PERMS.items():
            assert disk_perms.get(key) == value, (
                f"expected {key}={value!r} on disk before load, got {disk_perms}"
            )

        # ── the workspace ceiling tightens after the session was saved ─────────
        ceiling.update(TIGHTENED_CEILING)

        # ── restart (new bridge, same store): effective capped, storage intact ──
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        effective = cfg["session_permissions"]
        # security invariant: the effective config is capped
        assert effective["filesystem"] == "read", (
            f"expected effective filesystem=read after load, got {effective}"
        )
        assert effective["container"] is False, (
            f"expected effective container=False after load, got {effective}"
        )
        assert effective["network"] == "banned", (
            f"expected effective network=banned after load, got {effective}"
        )
        # restart-safety: the stored full grants survive the load
        disk_perms = _disk_permissions(temp_store, session_id)
        for key, value in FULL_PERMS.items():
            assert disk_perms.get(key) == value, (
                f"stored {key} must survive a restart (expected {value!r}); "
                f"got {disk_perms}"
            )

    def test_repeated_restarts_keep_stored_full_grants(self, temp_store, monkeypatch):
        """Two consecutive restarts must not erode the stored grants either."""
        ceiling = {}

        def fake_ceiling(ws_id):
            return dict(ceiling)

        _patch_ceiling(monkeypatch, fake_ceiling)

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-restart-repro"
        bridge.apply_config({"session_permissions": dict(FULL_PERMS)})
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        ceiling.update(TIGHTENED_CEILING)

        # restart 1
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"

        # restart 2 (simulates another server start after the fix)
        bridge3 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge3.load_session(session_id), "load_session returned False"

        disk_perms = _disk_permissions(temp_store, session_id)
        for key, value in FULL_PERMS.items():
            assert disk_perms.get(key) == value, (
                f"stored {key} must survive repeated restarts (expected {value!r}); "
                f"got {disk_perms}"
            )
