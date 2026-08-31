"""
Regression tests: a restored session can never exceed the workspace permission ceiling.

A saved session's ``session_config.session_permissions`` used to be restored
verbatim in ``WebAgentBridge.load_session`` (via ``extract_session_config``).
That left a residual permission-bypass: a session saved while the workspace
ceiling was permissive would come back with permissions above the *current*
ceiling after the workspace had been tightened.

Fix: ``load_session`` re-caps the stored session permissions through the current
workspace permission ceiling — exactly like a fresh config apply
(``config_manager.resolve_full_config``) — before the restored config becomes
live, and persists the capped config back into the session metadata.

These tests cover the three relevant scenarios:
1.  save with a permissive ceiling -> tighten ceiling -> load -> permissions capped
2.  control: no ceiling -> load -> permissions preserved unchanged
3.  control: no workspace -> load -> permissions preserved, ceiling loader never
    invoked with a falsy workspace id
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
    )


def _patch_ceiling(monkeypatch, fake):
    """Monkeypatch the workspace ceiling loader, binding the module at run time.

    Some test modules purge ``web_ui.backend.*`` from ``sys.modules`` and
    re-import ``web_ui.backend.server``, so a module imported at collection time
    can go stale.  ``WebAgentBridge.load_session`` resolves the ceiling loader
    lazily via ``sys.modules`` at call time, so the patch must target the module
    object that is current *during* the test — hence the import here, inside the
    helper (same idiom as ``tests/test_workspace_summary.py``).
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


class TestSessionLoadCeiling:
    """Loading a saved session re-caps stored permissions through the ceiling."""

    def test_load_recaps_through_tightened_ceiling(self, temp_store, monkeypatch):
        """Save filesystem=write, tighten the ceiling to banned, reload -> banned."""
        ceiling = {}

        def fake_ceiling(ws_id):
            return dict(ceiling)

        _patch_ceiling(monkeypatch, fake_ceiling)

        # ── save: workspace set, ceiling permissive -> write stored ──────────
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-ceiling-regression"
        bridge.apply_config({"session_permissions": {"filesystem": "write"}})
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        disk_perms = _disk_permissions(temp_store, session_id)
        assert disk_perms.get("filesystem") == "write", (
            f"expected filesystem=write on disk before load, got {disk_perms}"
        )

        # ── the workspace ceiling tightens after the session was saved ───────
        ceiling.update({"filesystem": "banned"})

        # ── load: the stored write must be capped to banned ──────────────────
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        assert cfg["session_permissions"]["filesystem"] == "banned", (
            f"expected filesystem=banned after load, got {cfg['session_permissions']}"
        )

        # the capped config is persisted back into the session metadata
        disk_perms = _disk_permissions(temp_store, session_id)
        assert disk_perms.get("filesystem") == "banned", (
            f"expected capped filesystem=banned on disk, got {disk_perms}"
        )

    def test_load_without_ceiling_preserves_permissions(self, temp_store, monkeypatch):
        """Control: no ceiling -> stored write survives the roundtrip unchanged."""
        _patch_ceiling(monkeypatch, lambda ws_id: {})

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-ceiling-control"
        bridge.apply_config({"session_permissions": {"filesystem": "write"}})
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        assert cfg["session_permissions"]["filesystem"] == "write", (
            f"expected filesystem=write preserved, got {cfg['session_permissions']}"
        )

    def test_load_without_workspace_preserves_permissions(self, temp_store, monkeypatch):
        """Control: no workspace -> ceiling loader never called, perms preserved."""
        calls = []

        def spy_ceiling(ws_id):
            calls.append(ws_id)
            return {"filesystem": "banned"}

        _patch_ceiling(monkeypatch, spy_ceiling)

        # bridge with no workspace id at all
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge.apply_config({"session_permissions": {"filesystem": "write"}})
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        assert cfg["session_permissions"]["filesystem"] == "write", (
            f"expected filesystem=write preserved, got {cfg['session_permissions']}"
        )
        # the ceiling loader must never be invoked with a falsy workspace id
        assert all(calls), f"ceiling loader called with falsy workspace id: {calls}"
