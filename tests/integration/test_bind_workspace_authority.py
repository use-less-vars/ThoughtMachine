"""D2: one binding authority, and the WS handshake pins the connection's scope.

`web_ui/backend/bridge.py::WebAgentBridge._bind_workspace(workspace_id, mode)`
is the ONLY place that may set `self._workspace_id`.  The load / create /
switch flow routes its six mutation sites through it:

  * `server.py` handshake binding (bootstrap at cold-load construction),
  * the three `WorkspaceRegistry.register_by_root()` creation transitions
    (bootstrap: apply_config, new_session, set_project),
  * the cold-load probe (`server.py:2516`, switch: the probe may not re-scope
    a connection whose declared workspace is different),
  * load-path site 1 (switch, `inverted-guard`: resolve + bind BEFORE the
    project-path update, so a refusal cannot leak a half-switched path),
and `bridge.py` site 2 (bootstrap, `bootstrap-bypass`: the behaviour of the old
`if self._workspace_id is None` guard, now named).

These tests grade the authority (unit) and the wiring (integration, in-process
TestClient).  They assert behaviour observed through the WebSocket protocol and
the server module's own bridge table, plus one source-order pin for the
ordering requirement (a refused bind must not precede the path update, and the
path update lives in a closure local the protocol cannot reach).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

_HERE = Path(__file__).resolve().parent
_INTEGRATION_DIR = (
    _HERE / "integration" if (_HERE / "integration" / "harness.py").is_file() else _HERE
)
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

import harness as H  # noqa: E402

pytestmark = pytest.mark.integration


# ────────────────────────────────────────────────────────────────────────────
# Hermetic full-server fixture (temp HOME + purged/re-imported backend)
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bind_server():
    app, tmp_home, stop_fn = H.start_backend()
    try:
        import web_ui.backend.server as server_mod

        H.register_mock_provider(server_mod)
        yield app, tmp_home, server_mod
    finally:
        stop_fn(rmtree=True, restore_env=True)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _ws_id(info):
    return info.get("id") or info.get("workspace_id")


def _workspace(client, tmp_home, tag):
    """Create (and register) one workspace; return ``(id, root)``."""
    info = H.create_workspace(client, tmp_home, name=tag)
    ws_id = _ws_id(info)
    assert ws_id, f"workspace id missing: {info!r}"
    return ws_id, os.path.join(tmp_home, tag)


def _ws_connect_project(client, root):
    """Open a socket declaring ``?project=<root>`` (the handshake binding input)."""
    return client.websocket_connect(f"/ws?project={quote(str(root))}")


def _spy_on_bind(monkeypatch):
    """Record every `_bind_workspace(workspace_id, mode)` call; keep behaviour.

    Patched on the CLASS: the server sites call it on the instance, so the spy
    sees exactly the arguments the flow passes.
    """
    import web_ui.backend.bridge as bridge_mod

    real = bridge_mod.WebAgentBridge._bind_workspace
    calls = []

    def spy(self, workspace_id, mode):
        refusal = real(self, workspace_id, mode)
        calls.append(
            {
                "workspace_id": workspace_id,
                "mode": mode,
                "refusal": refusal,
                "bound_after": self._workspace_id,
            }
        )
        return refusal

    monkeypatch.setattr(bridge_mod.WebAgentBridge, "_bind_workspace", spy)
    assert bridge_mod.WebAgentBridge._bind_workspace is spy, (
        "the spy must land on the class: server.py calls bridge._bind_workspace(..)"
    )
    return calls


def _spy_on_primitive(monkeypatch):
    """Record every `_ownership_refusal_ids(target, bound)` call; keep behaviour."""
    import web_ui.backend.bridge as bridge_mod

    real = bridge_mod._ownership_refusal_ids
    calls = []

    def spy(target_workspace_id, bound_workspace_id):
        calls.append((target_workspace_id, bound_workspace_id))
        return real(target_workspace_id, bound_workspace_id)

    monkeypatch.setattr(bridge_mod, "_ownership_refusal_ids", spy)
    return calls


def _status_texts(events):
    return [e.get("text") or "" for e in events if e.get("type") == "status_message"]


def _receive_refusal(ws, timeout=20.0, max_events=40):
    """Skip already-queued tail frames; return the first refusal status_message."""
    events = []
    for _ in range(max_events):
        evt = H._receive_json(ws, timeout=timeout)
        events.append(evt)
        if evt.get("type") == "status_message" and "refused" in (evt.get("text") or "").lower():
            return evt, events
    pytest.fail(
        "no refusal status_message received; status texts: "
        f"{_status_texts(events)!r}"
    )


def _bridge_class():
    from web_ui.backend.bridge import WebAgentBridge

    return WebAgentBridge


# ────────────────────────────────────────────────────────────────────────────
# 1 — the handshake pins the connection's declared workspace
# ────────────────────────────────────────────────────────────────────────────

def test_handshake_with_project_binds_connection(bind_server, monkeypatch):
    """`?project=A` binds the connection to A, so a load that would adopt B is refused.

    The bare (workspace_id-less) load makes the server probe the SESSION's
    workspace — B.  With the handshake binding in place that is a scope change
    and the bind authority refuses it.  Before D2 the probe simply won.
    """
    app, tmp_home, _server_mod = bind_server
    client = H.make_client(app)

    id_a, root_a = _workspace(client, tmp_home, "d2_hs_a")
    id_b, _root_b = _workspace(client, tmp_home, "d2_hs_b")
    sid_b = H.create_session(client, workspace_id=id_b)

    with _ws_connect_project(client, root_a) as ws:
        calls = _spy_on_bind(monkeypatch)
        ws.send_json({"command": "load_session", "session_id": sid_b})
        evt, events = H.receive_until_type(ws, "status_message", timeout=20.0)

    assert [c for c in calls if c["mode"] == "bootstrap" and c["workspace_id"] == id_a], (
        "the handshake must bootstrap the bridge to the declared workspace "
        f"{id_a!r}; the spy saw {calls!r}"
    )
    switch_calls = [c for c in calls if c["mode"] == "switch"]
    assert switch_calls and switch_calls[-1]["workspace_id"] == id_b, (
        f"the load must attempt a switch to the session's workspace {id_b!r}: {calls!r}"
    )
    assert switch_calls[-1]["refusal"] is not None, (
        "a switch away from the handshake binding must be refused: "
        f"{switch_calls[-1]!r}"
    )
    assert switch_calls[-1]["bound_after"] == id_a, (
        f"a refused switch must leave the binding intact: {switch_calls[-1]!r}"
    )
    text = evt.get("text") or ""
    assert "refused" in text.lower(), f"the refusal must be reported: {evt!r}"
    assert id_a in text and id_b in text, (
        f"the refusal must name both workspaces ({id_a!r}, {id_b!r}): {text!r}"
    )
    assert not [e for e in events if e.get("type") == "session_loaded"], (
        f"a refused bind must not answer with session_loaded: "
        f"{[e.get('type') for e in events]!r}"
    )


def test_handshake_without_project_arrives_unbound(bind_server, monkeypatch):
    """No `?project=` -> no handshake binding; the load's own scope bootstraps.

    Nothing bootstraps the bridge before the load, and the first (and only)
    binding is the load's `switch`, which an unbound bridge adopts.
    """
    app, tmp_home, _server_mod = bind_server
    client = H.make_client(app)

    id_a, _root_a = _workspace(client, tmp_home, "d2_np_a")
    sid_a = H.create_session(client, workspace_id=id_a)

    with client.websocket_connect("/ws") as ws:
        calls = _spy_on_bind(monkeypatch)
        ws.send_json({"command": "load_session", "session_id": sid_a})
        evt, _events = H.receive_until_type(ws, "session_loaded", timeout=20.0)

    assert not [c for c in calls if c["mode"] == "bootstrap"], (
        "a connection with no declared project must arrive unbound: nothing may "
        f"bootstrap it at handshake time; the spy saw {calls!r}"
    )
    switches = [c for c in calls if c["mode"] == "switch"]
    assert switches and switches[0]["workspace_id"] == id_a, (
        f"the load must bind the session's workspace via switch mode: {calls!r}"
    )
    assert switches[0]["refusal"] is None and switches[0]["bound_after"] == id_a, (
        f"an unbound bridge must adopt the load's workspace: {switches[0]!r}"
    )
    assert evt.get("workspace_id") == id_a and not evt.get("load_error"), (
        f"the load must succeed on an unbound connection: {evt!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 2 — the creation transition bootstraps through the authority
# ────────────────────────────────────────────────────────────────────────────

def test_register_by_root_binds_connection(bind_server, monkeypatch):
    """`register_by_root()` creation binds the connection to the NEW workspace.

    The folder is unregistered, so the handshake resolves nothing; the
    `new_session` fallback registers it and the connection adopts the new id
    through `_bind_workspace(..., "bootstrap")` — proven by the fact that the
    socket is afterwards scoped and refuses another workspace.
    """
    app, tmp_home, _server_mod = bind_server
    client = H.make_client(app)

    id_b, _root_b = _workspace(client, tmp_home, "d2_reg_b")
    fresh_root = os.path.join(tmp_home, "d2_reg_fresh")
    os.makedirs(fresh_root, exist_ok=True)

    with _ws_connect_project(client, fresh_root) as ws:
        calls = _spy_on_bind(monkeypatch)
        ws.send_json({"command": "new_session", "mode": "custom"})
        evt, _events = H.receive_until_type(ws, "session_loaded", timeout=20.0)

        bootstraps = [c for c in calls if c["mode"] == "bootstrap"]
        assert bootstraps, (
            "register_by_root() must bootstrap the connection through the bind "
            f"authority; the spy saw {calls!r}"
        )
        new_id = bootstraps[-1]["workspace_id"]
        assert new_id and new_id != id_b, f"unexpected bootstrap target: {bootstraps[-1]!r}"
        assert bootstraps[-1]["refusal"] is None, (
            f"a fresh bridge must accept the bootstrap: {bootstraps[-1]!r}"
        )
        assert bootstraps[-1]["bound_after"] == new_id, (
            f"the connection must adopt the registered workspace: {bootstraps[-1]!r}"
        )
        assert evt.get("workspace_id") == new_id, (
            f"new_session must report the registered workspace: {evt!r}"
        )

        # Behavioural confirmation: the socket is scoped now, so another
        # workspace is a scope change and is refused.
        ws.send_json({"command": "new_session", "mode": "custom", "workspace_id": id_b})
        # The first command's tail (…, "Ready. Type a query to start.") is still
        # queued here, so receive until the refusal itself instead of taking the
        # very next status_message.
        refusal, _events2 = _receive_refusal(ws, timeout=20.0)

    text = refusal.get("text") or ""
    assert "refused" in text.lower(), (
        f"a socket bound by register_by_root must refuse another workspace: {refusal!r}"
    )
    assert new_id in text and id_b in text, (
        f"the refusal must name both workspaces ({new_id!r}, {id_b!r}): {text!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 3 — D1 contract: the authority itself
# ────────────────────────────────────────────────────────────────────────────

def test_bind_workspace_never_accepts_none(bind_server):
    """`None` is refused in every mode and never becomes a binding."""
    bridge = _bridge_class()()
    assert bridge.workspace_id is None

    for mode in ("bootstrap", "switch"):
        refusal = bridge._bind_workspace(None, mode)
        assert refusal, f"binding None in mode {mode!r} must be refused"
        assert bridge.workspace_id is None, (
            f"a refused None bind must not bind anything (mode={mode!r}): "
            f"{bridge.workspace_id!r}"
        )

    # A bound bridge: None is refused and the existing binding survives.
    assert bridge._bind_workspace("ws-none-a", "bootstrap") is None
    assert bridge.workspace_id == "ws-none-a"
    assert bridge._bind_workspace(None, "switch") is not None
    assert bridge.workspace_id == "ws-none-a", (
        f"a refused None bind must leave the binding alone: {bridge.workspace_id!r}"
    )

    # An unknown mode is refused even for a perfectly good id.
    assert bridge._bind_workspace("ws-none-b", "rebind") is not None
    assert bridge.workspace_id == "ws-none-a", (
        f"an unknown mode must not bind: {bridge.workspace_id!r}"
    )


def test_bind_workspace_never_resets(bind_server, monkeypatch):
    """The authority bootstraps, no-ops on equality, and never unsets or re-scopes."""
    bridge = _bridge_class()()

    # bootstrap an unbound bridge
    assert bridge._bind_workspace("ws-reset-a", "bootstrap") is None
    assert bridge.workspace_id == "ws-reset-a"

    # idempotent-equal in both modes
    assert bridge._bind_workspace("ws-reset-a", "bootstrap") is None
    assert bridge._bind_workspace("ws-reset-a", "switch") is None
    assert bridge.workspace_id == "ws-reset-a"

    # bootstrap cannot switch
    refusal = bridge._bind_workspace("ws-reset-b", "bootstrap")
    assert refusal is not None, "bootstrap must refuse a different workspace"
    assert bridge.workspace_id == "ws-reset-a", (
        f"a refused bootstrap must not rebind: {bridge.workspace_id!r}"
    )

    # switch delegates the verdict to the ownership authority
    calls = _spy_on_primitive(monkeypatch)
    refusal = bridge._bind_workspace("ws-reset-b", "switch")
    assert calls == [("ws-reset-b", "ws-reset-a")], (
        "switch mode must delegate to _ownership_refusal_ids(target, bound); "
        f"the spy saw {calls!r}"
    )
    assert refusal is not None, "switch mode must inherit the authority's refusal"
    assert bridge.workspace_id == "ws-reset-a", (
        f"the authority must never be reset or overwritten on refusal: "
        f"{bridge.workspace_id!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 4 — the cross-bind is closed
# ────────────────────────────────────────────────────────────────────────────

def test_cross_workspace_load_refused(bind_server, monkeypatch):
    """An explicit load into another workspace cannot re-scope a handshake-bound socket."""
    app, tmp_home, server_mod = bind_server
    client = H.make_client(app)

    id_a, root_a = _workspace(client, tmp_home, "d2_cross_a")
    id_b, _root_b = _workspace(client, tmp_home, "d2_cross_b")
    sid_b = H.create_session(client, workspace_id=id_b)

    with _ws_connect_project(client, root_a) as ws:
        calls = _spy_on_bind(monkeypatch)
        ws.send_json(
            {"command": "load_session", "session_id": sid_b, "workspace_id": id_b}
        )
        evt, events = H.receive_until_type(ws, "status_message", timeout=20.0)

    text = evt.get("text") or ""
    assert "refused" in text.lower(), (
        f"loading another workspace must be refused: {evt!r}"
    )
    assert id_a in text and id_b in text, (
        f"the refusal must name both workspaces ({id_a!r}, {id_b!r}): {text!r}"
    )
    switch_calls = [c for c in calls if c["mode"] == "switch" and c["workspace_id"] == id_b]
    assert switch_calls and switch_calls[-1]["refusal"] is not None, (
        f"the refusal must come from the bind authority, not a bypass: {calls!r}"
    )
    assert switch_calls[-1]["bound_after"] == id_a, (
        f"the connection must stay bound to {id_a!r}: {switch_calls[-1]!r}"
    )
    assert not [e for e in events if e.get("type") == "session_loaded"], (
        f"a refused cross-workspace load must not answer with session_loaded: "
        f"{[e.get('type') for e in events]!r}"
    )
    assert sid_b not in server_mod._session_bridges, (
        "a refused load must not cache a bridge for the other workspace's session"
    )


# ────────────────────────────────────────────────────────────────────────────
# 5 — ordering: a refused bind must not leak the project path
# ────────────────────────────────────────────────────────────────────────────

def test_site1_refused_bind_does_not_leak_path(bind_server, monkeypatch):
    """`inverted-guard` site: refusal leaves `bridge._workspace_path` (and the path update) alone."""
    app, tmp_home, server_mod = bind_server
    client = H.make_client(app)

    id_a, root_a = _workspace(client, tmp_home, "d2_leak_a")
    id_c, root_c = _workspace(client, tmp_home, "d2_leak_c")
    assert id_a != id_c
    sid_a = H.create_session(client, workspace_id=id_a)

    # Rewrite A's on-disk config root so the load's auto-switch block tries to
    # move the connection to C — a workspace it is NOT bound to.
    cfg_path = (
        Path(tmp_home) / ".thoughtmachine" / "workspaces" / id_a / "config.json"
    )
    data = {}
    if cfg_path.is_file():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["root"] = root_c
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    with _ws_connect_project(client, root_a) as ws:
        ws.send_json({"command": "load_session", "session_id": sid_a})
        evt, events = H.receive_until_type(ws, "status_message", timeout=20.0)
        bridge = server_mod._session_bridges.get(sid_a)
        assert bridge is not None, (
            "the load must have bound a bridge for the session before the "
            f"auto-switch block ran; frames: {[e.get('type') for e in events]!r}"
        )
        bound_path = bridge._workspace_path
        bound_id = bridge.workspace_id

    text = evt.get("text") or ""
    assert "refused" in text.lower(), (
        f"the project switch to another workspace must be refused: {evt!r}"
    )
    assert bound_id == id_a, f"the bridge must stay bound to {id_a!r}: {bound_id!r}"
    assert bound_path == root_a, (
        "a refused bind must not leak the switched path into bridge._workspace_path: "
        f"expected {root_a!r}, got {bound_path!r}"
    )

    # `_project_path` is a closure local, so the ordering is pinned at the
    # source: the bind decision must precede the path update.
    src = Path(server_mod.__file__).read_text(encoding="utf-8")
    start = src.index("Auto-switch project path if session belongs")
    end = src.index("Ensure workspace_path is set on bridge config", start)
    block = src[start:end]
    assert 'bridge._bind_workspace(resolved, "switch")' in block, (
        "site 1 must route the switch through the bind authority"
    )
    assert block.index('bridge._bind_workspace(resolved, "switch")') < block.index(
        "_project_path = ws_project_path"
    ), (
        "the bind decision must precede `_project_path = ws_project_path` "
        "(the ordering requirement: a refused bind must not leak the path)"
    )
