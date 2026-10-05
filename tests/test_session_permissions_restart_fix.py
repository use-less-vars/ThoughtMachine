"""
Regression tests: a server restart must never collapse the FULL stored
session permission grants down to the workspace ceiling.

Bug being fixed: ``WebAgentBridge.load_session`` re-capped the stored
``session_permissions`` through the current workspace permission ceiling
(correct for the *effective* config) but then persisted the CAPPED dump back
into the session metadata (``save_config_to_session``).  After exactly one
restart the stored full grants (filesystem=write, container=true,
git=write, network=ask, ...) were permanently replaced by the capped
values (filesystem=read, container=false, network=banned, ...) -- the grant
is lost even after the ceiling is later relaxed.

P1 model (mirrored by this migrated test): session grants live in the
vault permission-store sidecar
(``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json``, written by
``permission_store.write_session_permissions``), NOT in the session metadata.
The bridge never persists grants.  The *effective* profile is always
gate-computed (``ConfigManager.resolve_effective_permissions``: sidecar grants
capped by the workspace ceiling read from ``<vault>/workspaces/<ws>/config.json``).

Fixed behavior:
1.  effective permissions after load are still capped through the ceiling
    (security invariant, unchanged), and
2.  the STORED session grants keep the full values (restart-safe persistence).

Migration note: the retired P2 assertions read the stored grant from the
session-file metadata (``metadata.session_config.session_permissions``) and the
live effective from ``bridge.get_config()["session_permissions"]``.  Both are
gone post-fix (the bridge no longer projects or persists grants), so the
identical invariants are now asserted through the canonical P1 store and the
gate-computed effective path.
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


WS = "ws-restart-repro"


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


# Canonical resource-catalog shape (P1 store normalises legacy grains:
# ``git_write`` collapses onto ``git``; ``system``/``execution`` are dropped).
FULL_PERMS = {
    "filesystem": "write",
    "container": True,
    "git": "write",
    "network": "ask",
}

TIGHTENED_CEILING = {
    "filesystem": "read",
    "container": False,
    "network": "banned",
}


class TestSessionPermissionsRestartFix:
    """Loading a saved session caps the EFFECTIVE config but never the STORED one."""

    def test_restart_keeps_stored_full_grants_while_effective_is_capped(self, temp_store):
        """Save full grants, tighten the ceiling, restart -> effective capped, storage intact."""
        # ── save: workspace set, ceiling permissive -> full grants stored ──────
        _set_workspace_ceiling(WS, {})
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = WS
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        _write_full(WS, session_id)
        stored = _stored_permissions(WS, session_id)
        for key, value in FULL_PERMS.items():
            assert stored.get(key) == value, (
                f"expected {key}={value!r} in store before load, got {stored}"
            )

        # ── the workspace ceiling tightens after the session was saved ─────────
        _set_workspace_ceiling(WS, TIGHTENED_CEILING)

        # ── restart (new bridge, same store): effective capped, storage intact ─
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        # security invariant: the effective config is capped
        effective = _effective_permissions(WS, session_id)
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
        stored = _stored_permissions(WS, session_id)
        for key, value in FULL_PERMS.items():
            assert stored.get(key) == value, (
                f"stored {key} must survive a restart (expected {value!r}); "
                f"got {stored}"
            )

    def test_repeated_restarts_keep_stored_full_grants(self, temp_store):
        """Two consecutive restarts must not erode the stored grants either."""
        _set_workspace_ceiling(WS, {})
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = WS
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        _write_full(WS, session_id)
        _set_workspace_ceiling(WS, TIGHTENED_CEILING)

        # restart 1
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"

        # restart 2 (simulates another server start after the fix)
        bridge3 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge3.load_session(session_id), "load_session returned False"

        stored = _stored_permissions(WS, session_id)
        for key, value in FULL_PERMS.items():
            assert stored.get(key) == value, (
                f"stored {key} must survive repeated restarts (expected {value!r}); "
                f"got {stored}"
            )
