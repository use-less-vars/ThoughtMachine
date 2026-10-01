"""Regression test: the live F5 / WebSocket ``load_session`` path must not
clobber a REST-written permission sidecar (P1) with a stale, non-empty
session-record grant dict (P2).

Transport-level reproduction of the operator's F5/WebSocket sequence:

  1. A workspace-scoped session is persisted (real ``FileSystemSessionStore``)
     whose ``session_config`` carries a STALE, NON-EMPTY
     ``session_permissions`` dict (``git=read`` plus the other five keys).
  2. The REST permissions endpoint writes the authoritative grant
     (``git=write``) to the session's permission sidecar P1.  Its bytes are
     captured.
  3. Over the REAL per-session WebSocket (``/ws``) the client sends the F5
     ``{"command": "load_session", "session_id": ...}`` message.  The handler
     drives ``websocket_endpoint`` -> ``WebAgentBridge.save_session()`` ->
     ``SessionManager.save_session()`` -> ``_sync_session_permissions_sidecar``
     (server.py:1927/1934-1935), NOT a direct ``SessionManager`` call.
  4. The disk-mode security gate (the runtime source of truth) must still
     report ``git=write``, and the P1 sidecar must be byte-identical to the
     bytes captured after step 2.

Under the pre-guard implementation the stale P2 (``git=read``) is mirrored onto
P1, so the GATE assertion below goes RED (expected 'write', observed 'read').
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
from web_ui.backend.server import app


def _recv_until(ws, want_type, max_msgs=40):
    """Read JSON messages until one of type *want_type* arrives."""
    for _ in range(max_msgs):
        event = ws.receive_json()
        if event.get("type") == want_type:
            return event
    raise AssertionError(
        f"did not observe a {want_type!r} message within {max_msgs} frames"
    )


def test_ws_load_session_refresh_does_not_clobber_rest_written_sidecar(
    hermetic_vault, tmp_path, monkeypatch
):
    vault_path = hermetic_vault  # == tmp_path/.thoughtmachine (fixture-built vault)
    ws_id = "ws-ws-refresh"

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
    #    NON-EMPTY but STALE grant dict (git=read) -- the record-level P2 the
    #    WS save must never mirror over an existing P1.
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
            "name": "refresh",
            "session_config": {
                "name": "refresh",
                "mode": "agent",
                "session_permissions": stale_perms,
            },
        },
    )
    store.save_session(sess, workspace_id=ws_id)
    sid = sess.session_id

    # 2) REST endpoint writes the authoritative grant (git=write) to the sidecar
    #    -- P1 now exists and is non-empty.
    import web_ui.backend.session_routes as sr

    monkeypatch.setattr(sr, "_get_store", lambda: store)
    client = TestClient(app)
    resp = client.put(f"/api/session/{sid}/permissions", json={"git": "write"})
    assert resp.status_code == 200, resp.text

    sidecar = session_grants_path(vault_path, ws_id, sid)
    assert sidecar.exists(), f"REST PUT did not write a sidecar at {sidecar}"
    after_rest = sidecar.read_bytes()
    assert json.loads(after_rest)["git"] == "write"

    # The WS handler uses the shared module-level session store singleton; point
    # it at this test's store and start from an empty bridge cache so the F5
    # sequence below runs against this session only.
    import web_ui.backend.server as server

    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_session_bridges", {})

    # 3) Drive the REAL F5 path over the per-session WebSocket: connect, then
    #    send the onopen `load_session` message.  A second load_session on the
    #    same connection is the message that finds a bridge already holding a
    #    session and therefore calls bridge.save_session() (server.py:1934-1935).
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"command": "load_session", "session_id": sid})
        _recv_until(ws, "session_loaded")

        ws.send_json({"command": "load_session", "session_id": sid})
        _recv_until(ws, "session_loaded")

    # Capture BOTH observations up-front -- the disk-mode gate value AND the
    # sidecar bytes -- BEFORE asserting either one, so a sidecar disagreement
    # cannot stop the gate read from being taken.
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
    after_ws = sidecar.read_bytes()

    # 4) GATE assertion FIRST: the disk-mode security gate is the runtime source
    #    of truth the acceptance criterion names ("read it back THROUGH THE
    #    GATE"), so a nulled guard must redden THIS assertion, naming the gate's
    #    observed value (expected 'write', observed 'read').
    assert gate_git == "write", (
        "disk-mode gate lost the git grant after a WebSocket load_session "
        f"refresh: expected 'write', observed {gate_git!r} (full effective: {effective})"
    )

    # CORE sidecar assertion: the guard held; the REST-written P1 survives
    # byte-for-byte through the WebSocket save.
    assert after_ws == after_rest, (
        "WebSocket load_session refresh clobbered the REST-written sidecar: "
        f"expected bytes {after_rest!r}, observed {after_ws!r}"
    )
