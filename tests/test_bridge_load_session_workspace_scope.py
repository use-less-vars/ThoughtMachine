"""Regression tests: a session can never be loaded across workspace boundaries.

``WebAgentBridge.load_session`` used to adopt the loaded session's
``workspace_id`` unconditionally.  Because the session store's *scoped* lookup
also scans the legacy, workspace-agnostic ``sessions_dir`` — where a record can
still carry a foreign ``workspace_id`` in its content — a bridge bound to
workspace A could load a session belonging to workspace B, pivot itself into B,
and then write every subsequent save / container / permission lookup there.

Fix (C):
* ``load_session(..., workspace_id=None)`` accepts the caller's expected
  workspace, but the bridge's own binding is authoritative; a mismatch between
  the two is refused before any store read,
* a record whose stored ``workspace_id`` differs from the effective scope is
  refused after the read and *before* any bridge state is mutated,
* a workspace-less legacy record never unscopes the bridge,
* ``_cached_bridge_for`` only re-attaches a warm bridge from the same workspace.
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from session.models import Session
from session.store import FileSystemSessionStore
from web_ui.backend.bridge import WebAgentBridge


@pytest.fixture
def temp_store(tmp_path):
    """Return a FileSystemSessionStore rooted in the pytest tmp_path."""
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
    )


def _write_legacy_session(store, workspace_id, session_id=None):
    """Write a session into the legacy (workspace-agnostic) sessions_dir.

    ``store.save_session`` routes workspace-scoped records into
    ``workspaces/<ws>/sessions``; writing the file directly is the only way to
    reproduce the legacy layout where a foreign ``workspace_id`` sits inside a
    file that the scoped lookup still scans.
    """
    session = Session(workspace_id=workspace_id)
    if session_id is not None:
        session.session_id = session_id
    path = store._get_friendly_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.to_persistable_dict()), encoding="utf-8")
    return session.session_id


class TestLoadSessionWorkspaceScope:
    """``load_session`` refuses to pivot the bridge into another workspace."""

    def test_foreign_legacy_record_is_refused(self, temp_store, tmp_path, monkeypatch):
        """bridge ws-a + legacy record carrying ws-b -> False, state untouched."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        session_id = _write_legacy_session(temp_store, "ws-b")

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-a"

        assert bridge.load_session(session_id) is False, (
            "load_session must refuse a record belonging to another workspace"
        )
        assert bridge._workspace_id == "ws-a", (
            f"bridge workspace pivoted to {bridge._workspace_id!r}"
        )
        assert bridge._session is None
        assert bridge._loaded_session is None

    def test_caller_scope_refuses_foreign_record(self, temp_store, tmp_path, monkeypatch):
        """Unscoped bridge + workspace_id='ws-a' still refuses a ws-b record."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        session_id = _write_legacy_session(temp_store, "ws-b")

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge.load_session(session_id, workspace_id="ws-a") is False, (
            "load_session must honour the caller-supplied workspace scope"
        )
        assert bridge._workspace_id is None
        assert bridge._session is None

    def test_requested_workspace_mismatch_skips_store_read(
        self, temp_store, tmp_path, monkeypatch
    ):
        """bridge ws-a + workspace_id='ws-b' -> refused before the store is read."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        session_id = _write_legacy_session(temp_store, "ws-b")

        reads = []
        real_load = temp_store.load_session

        def spy_load(sid, workspace_id=None):
            reads.append((sid, workspace_id))
            return real_load(sid, workspace_id=workspace_id)

        monkeypatch.setattr(temp_store, "load_session", spy_load)

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-a"

        assert bridge.load_session(session_id, workspace_id="ws-b") is False
        assert reads == [], f"store must not be read on a scope mismatch, got {reads}"
        assert bridge._workspace_id == "ws-a"

    def test_unscoped_bridge_refuses_any_workspace(
        self, temp_store, tmp_path, monkeypatch
    ):
        """No bridge binding and no caller scope -> the load is REFUSED.

        D3 inversion: this used to be the control case (an unscoped bridge loaded
        any workspace's session and adopted its ``workspace_id``).  Nothing can be
        proven to belong to a scopeless connection, so it fails closed; the
        adapter (server load gate) resolves the session's own workspace and pins
        the bridge before calling, so legitimate bare loads still work.
        """
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        session_id = _write_legacy_session(temp_store, "ws-b")

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge.load_session(session_id) is False, (
            "a scopeless bridge must refuse to load a workspace-scoped session"
        )
        assert bridge._workspace_id is None, (
            f"bridge workspace became {bridge._workspace_id!r}"
        )
        assert bridge._session is None
        assert bridge._loaded_session is None

    def test_workspaceless_record_keeps_bridge_binding(
        self, temp_store, tmp_path, monkeypatch
    ):
        """A legacy record with no workspace_id must not unscope the bridge."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        session_id = _write_legacy_session(temp_store, None)

        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-a"
        assert bridge.load_session(session_id) is True, "load_session returned False"
        assert bridge._workspace_id == "ws-a", (
            f"bridge workspace became {bridge._workspace_id!r}"
        )


class TestCachedBridgeFor:
    """``_cached_bridge_for`` only hands back a same-workspace warm bridge."""

    def _cached(self, monkeypatch, session_id, workspace_id):
        import web_ui.backend.server as server

        bridge = WebAgentBridge(event_callback=lambda e: None)
        bridge._workspace_id = workspace_id
        # A reusable warm bridge has a loaded session; a sessionaless entry is a
        # miss (see test_sessionaless_cached_bridge_returns_none).
        bridge._session = Session(workspace_id=workspace_id)
        monkeypatch.setitem(server._session_bridges, session_id, bridge)
        return server, bridge

    def test_match_returns_cached_bridge(self, monkeypatch):
        server, bridge = self._cached(monkeypatch, "sess-cached", "ws-a")
        try:
            assert server._cached_bridge_for("sess-cached", "ws-a") is bridge
        finally:
            server._session_bridges.pop("sess-cached", None)

    def test_unknown_session_returns_none(self, monkeypatch):
        import web_ui.backend.server as server
        assert server._cached_bridge_for("sess-unknown", "ws-a") is None

    def test_mismatched_workspace_returns_none(self, monkeypatch):
        server, _bridge = self._cached(monkeypatch, "sess-other", "ws-b")
        try:
            assert server._cached_bridge_for("sess-other", "ws-a") is None
        finally:
            server._session_bridges.pop("sess-other", None)

    def test_sessionaless_cached_bridge_returns_none(self, monkeypatch):
        """A refused/failed load leaves a sessionaless bridge in the cache.

        The bridge is registered in ``_session_bridges`` *before* its
        ``load_session()`` runs, so a refusal (or a dead session id) leaves an
        entry whose ``_controller`` is set but whose ``session`` is None.  The
        reuse branch never calls ``load_session()``, so reusing that entry would
        deny the session to its own workspace until a restart: it must be a miss.
        """
        server, bridge = self._cached(monkeypatch, "sess-empty", "ws-a")
        try:
            bridge._session = None
            bridge._loaded_session = None
            bridge._controller = object()
            assert bridge.session is None, "expected a sessionaless cached bridge"
            assert server._cached_bridge_for("sess-empty", "ws-a") is None
        finally:
            server._session_bridges.pop("sess-empty", None)


class TestCreateSessionWorkspaceScope:
    """``create_session`` must not unscope (or re-scope) the calling bridge."""

    def test_workspaceless_create_keeps_bridge_binding(
        self, temp_store, tmp_path, monkeypatch
    ):
        """ws-b bridge + create_session(workspace_id=None) stays bound to ws-b.

        The session manager persists the record with ``workspace_id=None`` (the
        legacy dir), the scoped reload finds it, and its ``workspace_id`` stays
        ``None`` — which must not clobber the bridge's own binding.
        """
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge._workspace_id = "ws-b"

        session_id, _frontend_config = bridge.create_session()

        assert session_id
        assert bridge._workspace_id == "ws-b", (
            f"create_session clobbered the bridge workspace: {bridge._workspace_id!r}"
        )
