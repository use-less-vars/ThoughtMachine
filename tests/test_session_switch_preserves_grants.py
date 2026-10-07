"""
Regression tests: switching AWAY from a session (tab switch / workspace
switch / shutdown) must never collapse the FULL stored session permission
grants down to the workspace ceiling.

Scenario this guards (server.py load_session handler + bridge persistence):

1.  Session A is saved with FULL grants while the workspace ceiling is
    permissive (stored: filesystem=write, container=true, ...).
2.  The workspace ceiling is later TIGHTENED (filesystem=read,
    container=false, network=banned).
3.  Operator switches to session A: the LIVE effective config is re-capped
    through the ceiling (correct -- effective permissions must never exceed
    the ceiling) but the STORED full grants stay intact.
4.  Operator switches AWAY again.  server.py calls ``bridge.save_session()``
    before switching, and a naive merge of the (CAPPED) live dump over the
    stored full grants would permanently replace them with the capped values
    (filesystem=read, container=false, network=banned).

P1 model (mirrored by this migrated test): session grants live in the vault
permission-store sidecar
(``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json``,
``permission_store.write_session_permissions``), NOT in the session metadata;
the bridge never persists grants.  The effective profile is gate-computed
(``ConfigManager.resolve_effective_permissions``: sidecar grants capped by the
workspace ceiling from ``<vault>/workspaces/<ws>/config.json``).

Fixed behavior:
1.  After load under a tightened ceiling the EFFECTIVE config stays capped
    (security invariant, unchanged).
2.  Switching away (save_session) after such a load never degrades the
    STORED full grants -- the store still carries what the operator granted.
3.  Even after an operator re-grant + switch-away + switch-back cycle the
    stored grants remain full while the freshly loaded effective config is
    capped again.

Migration note: the retired P2 assertions read the stored grant from the
session-file metadata and the live effective from
``bridge.get_config()["session_permissions"]``.  Both are gone post-fix (the
bridge no longer projects or persists grants); the identical invariants are
now asserted through the canonical P1 store and the gate-computed effective
path.  The old "grant-time probe is uncapped-by-design" assertion likewise
encoded the retired in-memory projection; in P1 the grant write stores the full
grants (asserted) while the effective profile is always the capped gate result.
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


def _set_workspace_ceiling(workspace_id, ceiling):
    """Provision the synthetic workspace on disk and write its ceiling.

    P1: the workspace ceiling lives in ``<vault>/workspaces/<ws>/config.json``
    (``"permissions"``), which the security gate reads directly.
    ``ensure_workspace_dirs`` seeds a present-but-empty config (no workspace cap)
    plus a fully-permissive ``capabilities.json``.
    """
    from thoughtmachine import vault as _vault
    from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

    ensure_workspace_dirs(workspace_id)
    config_path = _vault.vault_root() / "workspaces" / workspace_id / "config.json"
    data = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data["permissions"] = dict(ceiling)
    config_path.write_text(json.dumps(data), encoding="utf-8")
    assert config_path.is_file(), f"workspace config.json not provisioned: {config_path}"


def _write_full(workspace_id, session_id):
    """Persist the FULL session grants via the canonical P1 permission store."""
    from thoughtmachine import vault as _vault
    from thoughtmachine.permission_store import write_session_permissions

    write_session_permissions(
        _vault.vault_root(), workspace_id, session_id, dict(FULL_PERMS)
    )


def _stored_permissions(workspace_id, session_id):
    """Read the STORED session grants from the P1 permission-store sidecar."""
    from thoughtmachine import vault as _vault
    from thoughtmachine.permission_store import read_session_permissions

    return read_session_permissions(_vault.vault_root(), workspace_id, session_id)


def _effective_permissions(workspace_id, session_id):
    """Gate-computed effective profile (sidecar grants capped by vault ceiling)."""
    from web_ui.backend.config_manager import ConfigManager

    return ConfigManager.resolve_effective_permissions(None, session_id, workspace_id)


def _assert_perms(perms, expected, label):
    for key, value in expected.items():
        assert perms.get(key) == value, (
            f"{label}: expected {key}={value!r}, got {perms}"
        )


FULL_PERMS = {
    "filesystem": "write",
    "container": True,
    "git": "write",
    "network": "ask",
    "mcp": "banned",
    "host_bash": "banned",
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
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id
        # Operator grant recorded in the canonical P1 store.
        _write_full(ws_id, session_id)
        _assert_perms(_stored_permissions(ws_id, session_id), FULL_PERMS,
                      "stored perms right after save")
        return session_id

    def test_switch_away_after_capped_load_keeps_stored_full(self, temp_store):
        """Load under a tightened ceiling (live capped), then switch away and
        save -- the stored full grants must survive the save."""
        ws_id = "ws-switch-away-repro"
        _set_workspace_ceiling(ws_id, {})

        session_id = self._save_full_session(temp_store, ws_id)

        # Ceiling tightens after the session was saved.
        _set_workspace_ceiling(ws_id, TIGHTENED_CEILING)

        # Operator switches to the session: fresh bridge, fresh load.
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        # Security invariant: live effective config is capped.
        _assert_perms(_effective_permissions(ws_id, session_id), TIGHTENED_CEILING,
                      "live effective after load")

        # Operator switches AWAY -- server.py calls bridge.save_session()
        # before switching (and atexit shutdown does the same).
        assert bridge2.save_session() is not None, "switch-away save_session returned None"

        disk_perms = _stored_permissions(ws_id, session_id)
        for key in CAPPED_KEYS:
            assert disk_perms.get(key) == FULL_PERMS[key], (
                f"switch-away save collapsed stored {key}: expected "
                f"{FULL_PERMS[key]!r} in store, got {disk_perms}"
            )

    def test_regrant_switch_away_switch_back_keeps_stored_full(self, temp_store):
        """Full cycle: stored full -> capped load -> operator re-grants full
        (stored full re-asserted) -> switch-away save -> switch back ->
        stored still full, freshly-loaded effective capped again."""
        ws_id = "ws-switch-cycle-repro"
        _set_workspace_ceiling(ws_id, {})

        session_id = self._save_full_session(temp_store, ws_id)
        _set_workspace_ceiling(ws_id, TIGHTENED_CEILING)

        # Load under tightened ceiling (live capped).
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        _assert_perms(_effective_permissions(ws_id, session_id), TIGHTENED_CEILING,
                      "live effective after load")

        # Operator re-grants the full set while the session is live: the
        # canonical store now carries the full grants again.
        _write_full(ws_id, session_id)
        _assert_perms(_stored_permissions(ws_id, session_id), FULL_PERMS,
                      "stored perms after re-grant")

        # Switch away -> save.
        assert bridge2.save_session() is not None, "switch-away save_session returned None"
        _assert_perms(_stored_permissions(ws_id, session_id), FULL_PERMS,
                      "stored perms after switch-away save")

        # Switch back: fresh bridge, fresh load.
        bridge3 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge3.load_session(session_id), "load_session returned False"
        _assert_perms(_effective_permissions(ws_id, session_id), TIGHTENED_CEILING,
                      "live effective after switch-back")
        _assert_perms(_stored_permissions(ws_id, session_id), FULL_PERMS,
                      "stored perms after switch-back load")
