"""
Regression tests: switching AWAY from a session (tab switch / workspace
switch / shutdown) must never collapse the FULL stored session permission
grants down to the workspace ceiling.

Scenario this guards (server.py load_session handler + bridge persistence):

1.  Session A is saved with FULL grants while the workspace ceiling is
    permissive (stored on disk: filesystem=write, container=true, ...).
2.  The workspace ceiling is later TIGHTENED (filesystem=read,
    container=false, network=banned).
3.  Operator switches to session A: ``bridge.load_session`` re-caps the
    LIVE in-memory config through the ceiling (correct — effective
    permissions must never exceed the ceiling) but leaves the STORED full
    grants intact (fix 114c75c protected only the load-time rewrite).
4.  Operator switches AWAY again.  server.py calls ``bridge.save_session()``
    before switching.  ``save_session`` merges the (CAPPED) in-memory dump
    over the stored full grants — and because merge_session_permissions lets
    explicit new values win, the stored full grants are permanently replaced
    by the capped values (filesystem=read, container=false, network=banned).

Fixed behavior:
1.  After load under a tightened ceiling the EFFECTIVE config stays capped
    (security invariant, unchanged).
2.  Switching away (save_session) after such a load never degrades the
    STORED full grants — disk still carries what the operator granted.
3.  Even after an operator re-grant + switch-away + switch-back cycle the
    stored grants remain full while the freshly loaded effective config is
    capped again.
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
    """Monkeypatch the workspace ceiling loader, binding the module at run time."""
    import web_ui.backend.config_manager as config_manager

    monkeypatch.setattr(config_manager, "_load_workspace_permission_ceiling", fake)


def _disk_permissions(temp_store, session_id):
    """Return the session_permissions dict stored in the session file metadata."""
    path = temp_store._find_session_path(session_id)
    assert path is not None, "session file not found on disk"
    with open(path, "r") as f:
        raw = json.load(f)
    return raw.get("metadata", {}).get("session_config", {}).get("session_permissions", {})


def _assert_perms(perms, expected, label):
    for key, value in expected.items():
        assert perms.get(key) == value, (
            f"{label}: expected {key}={value!r}, got {perms}"
        )


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

CAPPED_KEYS = ("filesystem", "container", "network")


class TestSessionSwitchPreservesGrants:
    """Switch-away saves must never collapse stored full grants."""

    def _save_full_session(self, temp_store, ws_id):
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = ws_id
        bridge.apply_config({"session_permissions": dict(FULL_PERMS)})
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        _assert_perms(_disk_permissions(temp_store, saved.session_id), FULL_PERMS,
                      "stored perms right after save")
        return saved.session_id

    def test_switch_away_after_capped_load_keeps_stored_full(self, temp_store, monkeypatch):
        """Load under a tightened ceiling (live capped), then switch away and
        save — the stored full grants must survive the save."""
        ceiling = {}

        def fake_ceiling(ws_id):
            return dict(ceiling)

        _patch_ceiling(monkeypatch, fake_ceiling)

        session_id = self._save_full_session(
            temp_store, "ws-switch-away-repro"
        )

        # Ceiling tightens after the session was saved.
        ceiling.update(TIGHTENED_CEILING)

        # Operator switches to the session: fresh bridge, fresh load.
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        effective = bridge2.get_config()["session_permissions"]
        # Security invariant: live effective config is capped.
        _assert_perms(effective, TIGHTENED_CEILING, "live effective after load")

        # Operator switches AWAY — server.py calls bridge.save_session()
        # before switching (and atexit shutdown does the same).
        assert bridge2.save_session() is not None, "switch-away save_session returned None"

        disk_perms = _disk_permissions(temp_store, session_id)
        for key in CAPPED_KEYS:
            assert disk_perms.get(key) == FULL_PERMS[key], (
                f"switch-away save collapsed stored {key}: expected "
                f"{FULL_PERMS[key]!r} on disk, got {disk_perms}"
            )

    def test_regrant_switch_away_switch_back_keeps_stored_full(self, temp_store, monkeypatch):
        """Full cycle: stored full -> capped load -> operator re-grants full
        (grant-time probe recorded) -> switch-away save -> switch back ->
        stored still full, freshly-loaded effective capped again."""
        ceiling = {}

        def fake_ceiling(ws_id):
            return dict(ceiling)

        _patch_ceiling(monkeypatch, fake_ceiling)

        session_id = self._save_full_session(
            temp_store, "ws-switch-cycle-repro"
        )
        ceiling.update(TIGHTENED_CEILING)

        # Load under tightened ceiling (live capped).
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        effective = bridge2.get_config()["session_permissions"]
        _assert_perms(effective, TIGHTENED_CEILING, "live effective after load")

        # Operator re-grants the full set while the session is live.
        regrant_result = bridge2.apply_config({"session_permissions": dict(FULL_PERMS)})
        grant_probe = regrant_result.get("permissions", {})
        # Grant-time probe mirrors the live (uncapped-by-design) config.
        _assert_perms(grant_probe, FULL_PERMS, "apply_config grant-time probe")

        # Switch away -> save.
        assert bridge2.save_session() is not None, "switch-away save_session returned None"
        _assert_perms(_disk_permissions(temp_store, session_id), FULL_PERMS,
                      "stored perms after switch-away save")

        # Switch back: fresh bridge, fresh load.
        bridge3 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge3.load_session(session_id), "load_session returned False"
        effective3 = bridge3.get_config()["session_permissions"]
        _assert_perms(effective3, TIGHTENED_CEILING, "live effective after switch-back")
        _assert_perms(_disk_permissions(temp_store, session_id), FULL_PERMS,
                      "stored perms after switch-back load")
