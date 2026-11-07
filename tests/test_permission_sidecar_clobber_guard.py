"""Regression test: a config save must not clobber a REST-written permission sidecar.

Scenario (sidecar-clobber guard):

  1. A workspace-scoped session is persisted with a ``session_config`` that
     carries NO ``session_permissions`` dict.
  2. The REST permissions endpoint writes a non-default grant
     (``git=write``) to the session's permission sidecar
     (``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json``).
  3. A later save through ``SessionManager`` reloads the same session (whose
     ``session_config`` STILL omits ``session_permissions``) and persists it
     again.  ``SessionManager._sync_session_permissions_sidecar`` must NOT
     mirror an absent source dict into the sidecar, or it would overwrite the
     REST-written ``git=write`` with ``{}`` and silently drop the grant.
  4. The disk-mode security gate (the runtime source of truth) must still
     report ``git=write`` for the session.

Under the pre-guard implementation (``if not isinstance(perms, dict): perms = {}``
then unconditionally seed) step 3 writes ``{}`` and step 4 reports the default
``git=read`` -- this test goes RED.
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from session.models import Session
from session.store import FileSystemSessionStore
from security.security_gate import get_effective_permissions
from thoughtmachine.permission_store import session_grants_path
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.server import app
from web_ui.backend.session_manager import SessionManager


def test_config_save_does_not_clobber_rest_written_sidecar(
    hermetic_vault, tmp_path, monkeypatch
):
    vault_path = hermetic_vault  # == tmp_path/.thoughtmachine (fixture-built vault)
    ws_id = "ws-roundtrip"

    # Workspace dir + coding ceiling (git=write) so the disk-mode gate can
    # admit a write grant.
    ws_dir = vault_path / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps(
            {"purpose": "coding", "permissions": {"filesystem": "write", "git": "write"}}
        )
    )

    store = FileSystemSessionStore(
        sessions_dir=str(vault_path / "sessions"),
        state_dir=str(vault_path / "state"),
    )

    # 1) Persist a workspace-scoped session with NO session_permissions in its
    #    session_config (so the guard's early-return path is exercised).
    sess = Session(
        workspace_id=ws_id,
        metadata={
            "name": "roundtrip",
            "session_config": {"name": "roundtrip", "mode": "agent"},
        },
    )
    store.save_session(sess, workspace_id=ws_id)
    sid = sess.session_id

    # 2) REST endpoint writes a non-default grant to the sidecar.
    import web_ui.backend.session_routes as sr

    monkeypatch.setattr(sr, "_get_store", lambda: store)
    client = TestClient(app)
    resp = client.put(f"/api/session/{sid}/permissions", json={"git": "write"})
    assert resp.status_code == 200, resp.text

    sidecar = session_grants_path(vault_path, ws_id, sid)
    assert sidecar.exists(), f"REST PUT did not write a sidecar at {sidecar}"
    assert json.loads(sidecar.read_text())["git"] == "write"

    # 3) A later SessionManager save of the reloaded session (session_config
    #    still has no session_permissions -> guard must early-return).
    reloaded = store.load_session(sid, workspace_id=ws_id)
    assert reloaded is not None, "session not found after save"
    assert reloaded.workspace_id == ws_id, (
        f"reloaded session lost workspace_id: {reloaded.workspace_id!r}"
    )
    SessionManager(store, ConfigManager()).save_session(reloaded)

    # Capture BOTH observations up-front -- the REST-written sidecar value AND
    # the disk-mode gate value -- BEFORE asserting either one.  Taking both
    # measurements first keeps the two checks independent: a sidecar
    # disagreement can no longer stop the gate read from being taken (the
    # previous order asserted the sidecar before the gate was even read, so a
    # nulled guard only ever reddened the sidecar line).
    after = json.loads(sidecar.read_text())
    sidecar_git = after.get("git")
    effective = get_effective_permissions(
        SessionPermissions(),
        WorkspaceCapabilities(
            git_available=True,
            filesystem_write=True,
            allow_docker=True,
            allow_network=True,
        ),
        None,
        session_id=sid,
        workspace_id=ws_id,
    )
    gate_git = effective["git"]

    # 4) GATE assertion FIRST: the disk-mode security gate is the runtime
    #    source of truth the acceptance criterion names ("read it back THROUGH
    #    THE GATE"), so a nulled guard must redden THIS assertion, naming the
    #    gate's observed value (expected 'write', observed 'read').
    assert gate_git == "write", (
        "disk-mode gate lost the git grant after config save: "
        f"expected 'write', observed {gate_git!r} (full effective: {effective})"
    )

    # CORE sidecar assertion: the guard held; the REST-written grant survives.
    assert sidecar_git == "write", (
        "config save clobbered the REST-written sidecar: "
        f"expected 'write', observed {sidecar_git!r} (full sidecar: {after})"
    )


def test_stale_nonempty_p2_does_not_clobber_rest_written_sidecar(
    hermetic_vault, tmp_path, monkeypatch
):
    """Second-order guard: a STALE-but-NON-EMPTY P2 must not clobber the P1.

    The empty-source guard only early-returns when the mirrored source dict is
    ABSENT/empty.  A legacy session record can however carry a full, non-empty
    ``session_permissions`` dict (here: ``git=read``) that is STALE relative to
    the REST-owned sidecar.  Once the REST endpoint has written ``git=write`` to
    the sidecar (P1), a later ``SessionManager`` save must NOT mirror that stale
    P2 over P1.  The P1-existence guard reads P1 first and skips the mirror when
    it already holds grants.

    Under the pre-guard implementation the stale P2 (``git=read``) is mirrored
    onto P1, so the disk-mode gate reports ``git=read`` and the GATE assertion
    below goes RED.
    """
    vault_path = hermetic_vault  # == tmp_path/.thoughtmachine (fixture-built vault)
    ws_id = "ws-stale-p2"

    # Workspace dir + coding ceiling (git=write) so the disk-mode gate can
    # admit a write grant.
    ws_dir = vault_path / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps(
            {"purpose": "coding", "permissions": {"filesystem": "write", "git": "write"}}
        )
    )

    store = FileSystemSessionStore(
        sessions_dir=str(vault_path / "sessions"),
        state_dir=str(vault_path / "state"),
    )

    # 1) Persist a workspace-scoped session whose session_config carries a
    #    NON-EMPTY but STALE grant dict (git=read).  This exercises the
    #    P1-existence guard rather than the absent/empty-source guard.
    stale_perms = {
        "filesystem": "read",
        "git": "read",
        "network": "banned",
        "container": False,
        "mcp": "banned",
        "host_bash": "banned",
    }
    sess = Session(
        workspace_id=ws_id,
        metadata={
            "name": "stale",
            "session_config": {
                "name": "stale",
                "mode": "agent",
                "session_permissions": stale_perms,
            },
        },
    )
    store.save_session(sess, workspace_id=ws_id)
    sid = sess.session_id

    # 2) REST endpoint writes the authoritative grant (git=write) to the
    #    sidecar -- P1 now exists and is non-empty.
    import web_ui.backend.session_routes as sr

    monkeypatch.setattr(sr, "_get_store", lambda: store)
    client = TestClient(app)
    resp = client.put(f"/api/session/{sid}/permissions", json={"git": "write"})
    assert resp.status_code == 200, resp.text

    sidecar = session_grants_path(vault_path, ws_id, sid)
    assert sidecar.exists(), f"REST PUT did not write a sidecar at {sidecar}"
    assert json.loads(sidecar.read_text())["git"] == "write"

    # 3) Reload the session: P2 is unchanged and STILL carries the stale
    #    git=read, so the P1-existence guard must skip the mirror.
    reloaded = store.load_session(sid, workspace_id=ws_id)
    assert reloaded is not None, "session not found after save"
    assert reloaded.workspace_id == ws_id, (
        f"reloaded session lost workspace_id: {reloaded.workspace_id!r}"
    )
    persisted_p2 = reloaded.metadata["session_config"]["session_permissions"]
    assert persisted_p2.get("git") == "read", (
        "expected the session record (P2) to still hold the stale git=read, "
        f"observed {persisted_p2.get('git')!r}"
    )
    SessionManager(store, ConfigManager()).save_session(reloaded)

    # Capture BOTH observations up-front -- the REST-written sidecar value AND
    # the disk-mode gate value -- BEFORE asserting either one, so a sidecar
    # disagreement cannot stop the gate read from being taken.
    after = json.loads(sidecar.read_text())
    sidecar_git = after.get("git")
    effective = get_effective_permissions(
        SessionPermissions(),
        WorkspaceCapabilities(
            git_available=True,
            filesystem_write=True,
            allow_docker=True,
            allow_network=True,
        ),
        None,
        session_id=sid,
        workspace_id=ws_id,
    )
    gate_git = effective["git"]

    # 4) GATE assertion FIRST: the disk-mode security gate is the runtime source
    #    of truth the acceptance criterion names ("read it back THROUGH THE
    #    GATE"), so a guard that mirrors the stale P2 must redden THIS assertion,
    #    naming the gate's observed value (expected 'write', observed 'read').
    assert gate_git == "write", (
        "disk-mode gate lost the git grant after a stale-P2 config save: "
        f"expected 'write', observed {gate_git!r} (full effective: {effective})"
    )

    # CORE sidecar assertion: the guard held; the REST-written grant survives.
    assert sidecar_git == "write", (
        "stale P2 clobbered the REST-written sidecar: "
        f"expected 'write', observed {sidecar_git!r} (full sidecar: {after})"
    )
