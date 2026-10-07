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
live.  The cap is applied to the live config only and is never persisted:
the stored full grants must survive reloads untouched (a persisted cap would
permanently collapse them after a single restart).

These tests cover the three relevant scenarios:
1.  save with a permissive ceiling -> tighten ceiling -> load -> permissions capped
2.  control: no ceiling -> load -> permissions preserved unchanged
3.  control: no workspace -> load -> grants are NOT persisted (the canonical
    permission store is workspace-scoped), so the session falls back to the
    read-only default; the ceiling loader is never invoked with a falsy
    workspace id
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


def _disk_permissions(session_id, workspace_id):
    """Return the session's raw grants from the canonical permission sidecar (P1).

    The session record is no longer a grants holder (its ``session_permissions``
    metadata is stripped on every write); the canonical permission store
    (``write_session_permissions`` / the REST
    ``PUT /api/session/{id}/permissions`` path) is the source of truth.
    """
    from thoughtmachine.permission_store import read_session_permissions
    from thoughtmachine.vault import vault_root

    return read_session_permissions(vault_root(), workspace_id, session_id)


class TestSessionLoadCeiling:
    """Loading a saved session re-caps stored permissions through the ceiling."""

    def test_load_recaps_through_tightened_ceiling(self, temp_store, tmp_path, monkeypatch):
        """Save filesystem=write, tighten the ceiling to banned, reload -> banned."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        ceiling = {}

        def fake_ceiling(ws_id):
            return dict(ceiling)

        _patch_ceiling(monkeypatch, fake_ceiling)

        # ── save: workspace set, ceiling permissive -> write stored ──────────
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-ceiling-regression"
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id
        # Seed the grant through the canonical permission store (the REST-PUT path).
        from thoughtmachine.permission_store import write_session_permissions
        from thoughtmachine.vault import vault_root
        write_session_permissions(
            vault_root(), "ws-ceiling-regression", session_id, {"filesystem": "write"}
        )

        disk_perms = _disk_permissions(session_id, "ws-ceiling-regression")
        assert disk_perms.get("filesystem") == "write", (
            f"expected filesystem=write in the sidecar before load, got {disk_perms}"
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

        # the cap is in-memory only: the sidecar keeps the full grant
        disk_perms = _disk_permissions(session_id, "ws-ceiling-regression")
        assert disk_perms.get("filesystem") == "write", (
            f"expected filesystem=write preserved in the sidecar after a capped load, got {disk_perms}"
        )

    def test_load_without_ceiling_preserves_permissions(self, temp_store, tmp_path, monkeypatch):
        """Control: no ceiling -> stored write survives the roundtrip unchanged."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        _patch_ceiling(monkeypatch, lambda ws_id: {})

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-ceiling-control"
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id
        # Seed the grant through the canonical permission store (the REST-PUT path).
        from thoughtmachine.permission_store import write_session_permissions
        from thoughtmachine.vault import vault_root
        write_session_permissions(
            vault_root(), "ws-ceiling-control", session_id, {"filesystem": "write"}
        )

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        assert cfg["session_permissions"]["filesystem"] == "write", (
            f"expected filesystem=write preserved, got {cfg['session_permissions']}"
        )

    def test_load_without_workspace_preserves_permissions(self, temp_store, monkeypatch):
        """Control: no workspace -> ceiling loader never called; grants not persisted.

        The canonical permission store is workspace-scoped, so a session with
        no workspace has no grant store to write to; the restored session falls
        back to the read-only default.
        """
        calls = []

        def spy_ceiling(ws_id):
            calls.append(ws_id)
            return {"filesystem": "banned"}

        _patch_ceiling(monkeypatch, spy_ceiling)

        # bridge with no workspace id at all
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        # Without a workspace there is no grant store (P1 is workspace-scoped),
        # so the session falls back to the safe default (the write grant must not
        # survive).  ``get_config`` may omit the key entirely when the model
        # default (None) is dropped by ``exclude_none=True``.
        perms = cfg.get("session_permissions") or {}
        assert perms.get("filesystem") != "write", (
            f"without a workspace the write grant must not survive, got {perms}"
        )
        # the ceiling loader must never be invoked with a falsy workspace id
        assert all(calls), f"ceiling loader called with falsy workspace id: {calls}"

    def test_b28_unreadable_sidecar_yields_deny_all_sentinel(
        self, temp_store, tmp_path, monkeypatch
    ):
        """P1 (B-28): an unreadable sidecar must yield the deny-all sentinel.

        BEFORE the fix an unreadable grants read left ``sc.session_permissions``
        at the permissive Pydantic default (``filesystem='read'``); AFTER it is
        the deny-all sentinel (``filesystem='banned'``).
        """
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        import thoughtmachine.permission_store as ps

        def _raise(*_args, **_kwargs):
            raise ps.PermissionStoreError("forced unreadable sidecar")

        monkeypatch.setattr(ps, "read_session_permissions", _raise)

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-p1-deny"
        saved = bridge.save_session()
        assert saved is not None
        session_id = saved.session_id

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        perms = cfg["session_permissions"]
        assert perms["filesystem"] == "banned", perms
        assert perms["git"] == "banned", perms
        assert perms["network"] == "banned", perms
        assert perms["container"] is False, perms

    def test_no_workspace_yields_designed_default_not_sentinel(self, temp_store):
        """P2 (CONTRACT REVISION, D1=A): a truly id-less session gets the
        designed pydantic default, NOT the deny-all sentinel.

        D1=A narrows the helper's id-less branch: the deny-all sentinel is for
        an *unreadable* workspace-scoped store (both ids present, read fails),
        not for the absence of a store.  A session with no workspace has no
        grants path to be unreadable -- id-less is not the same as unreadable
        -- so the designed default applies.  This test previously pinned the
        deny-all sentinel for the id-less case; that was the pre-D1 semantics
        and this is a mandate-driven CONTRACT REVISION.
        """
        from thoughtmachine.permission_store import read_grants_or_deny_all
        from thoughtmachine.security import SessionPermissions

        default = SessionPermissions().model_dump()
        # helper-level: either id absent -> designed default, never sentinel
        assert read_grants_or_deny_all(None, None) == default
        assert read_grants_or_deny_all("some-session", None) == default
        assert read_grants_or_deny_all(None, "some-ws") == default
        assert read_grants_or_deny_all(None, None).get("filesystem") != "banned"
        assert read_grants_or_deny_all("some-session", None).get("git") != "banned"

        # bridge-level: a workspace-less session resolves to the designed default
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        saved = bridge.save_session()
        assert saved is not None
        session_id = saved.session_id

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id), "load_session returned False"
        cfg = bridge2.get_config()
        assert cfg is not None
        perms = cfg.get("session_permissions") or {}
        assert perms.get("filesystem") == "read", perms
        assert perms.get("git") == "read", perms
