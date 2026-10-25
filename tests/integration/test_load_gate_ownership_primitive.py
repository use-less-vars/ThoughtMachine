"""The WS load gate and new_session consult ONE ownership primitive (D3).

`web_ui/backend/bridge.py::_ownership_refusal_ids(target_workspace_id,
bound_workspace_id)` is the single id-form ownership predicate.  The two server
sites that decide whether a socket may act on another workspace must call it
with `(requested, socket-scope)` and must not re-implement the comparison:

  * the `load_session` gate (`web_ui/backend/server.py`): a socket already
    bound to workspace A that asks for workspace B is refused with
    `{"type": "session_loaded", "load_error": True, ...}`;
  * `new_session`: the same socket asking to create a session in workspace B is
    refused with a `status_message` naming both workspaces.

These tests spy on the primitive itself (the bridge module attribute — both
sites import it at call time) so they assert the ARGUMENTS, not just the
verdict: a site that hand-rolled the comparison, or swapped the argument order,
would refuse with the right shape for the wrong reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_INTEGRATION_DIR = (
    _HERE / "integration" if (_HERE / "integration" / "harness.py").is_file() else _HERE
)
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

import harness as H  # noqa: E402

pytestmark = pytest.mark.integration


# ──────────────────────────────────────────────────────────────────────────────
# Hermetic full-server fixture (temp HOME + purged/re-imported backend)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gate_server():
    app, tmp_home, stop_fn = H.start_backend()
    try:
        import web_ui.backend.server as server_mod

        H.register_mock_provider(server_mod)
        yield app, tmp_home, server_mod
    finally:
        stop_fn(rmtree=True, restore_env=True)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ws_id(info):
    return info.get("id") or info.get("workspace_id")


def _two_workspaces(client, tmp_home, tag):
    """Create (and register) workspaces A and B; return their ids."""
    info_a = H.create_workspace(client, tmp_home, name=f"{tag}_a")
    info_b = H.create_workspace(client, tmp_home, name=f"{tag}_b")
    id_a, id_b = _ws_id(info_a), _ws_id(info_b)
    assert id_a and id_b, f"workspace ids missing: A={info_a!r} B={info_b!r}"
    assert id_a != id_b, f"workspaces must differ: {id_a!r}"
    return id_a, id_b


def _fence(ws, max_events=60):
    """Send the fence command and return every frame received before its reply."""
    ws.send_json({"command": "get_open_sessions"})
    events = []
    for _ in range(max_events):
        evt = H._receive_json(ws, timeout=20.0)
        if evt.get("type") == "open_sessions":
            return events
        events.append(evt)
    pytest.fail(
        "fence (get_open_sessions -> open_sessions) never arrived; saw "
        f"{[e.get('type') for e in events]}"
    )


def _bind(ws, session_id, workspace_id):
    """Bind this socket to `workspace_id` via load_session, then drain the tail."""
    ws.send_json(
        {"command": "load_session", "session_id": session_id, "workspace_id": workspace_id}
    )
    evt, _events = H.receive_until_type(ws, "session_loaded", timeout=20.0)
    assert evt.get("workspace_id") == workspace_id and not evt.get("load_error"), (
        f"could not bind socket to workspace {workspace_id!r}: {evt!r}"
    )
    _fence(ws)  # drop the bind tail so it cannot be mistaken for command output


def _spy_on_primitive(monkeypatch):
    """Record every `_ownership_refusal_ids(target, bound)` call; keep behaviour."""
    import web_ui.backend.bridge as bridge_mod

    real = bridge_mod._ownership_refusal_ids
    calls = []

    def spy(target_workspace_id, bound_workspace_id):
        calls.append((target_workspace_id, bound_workspace_id))
        return real(target_workspace_id, bound_workspace_id)

    monkeypatch.setattr(bridge_mod, "_ownership_refusal_ids", spy)
    assert bridge_mod._ownership_refusal_ids is spy, (
        "the spy must land on the bridge module attribute: both server sites "
        "import the primitive at call time"
    )
    return calls


# ──────────────────────────────────────────────────────────────────────────────
# 1 — the load gate
# ──────────────────────────────────────────────────────────────────────────────

def test_load_gate_calls_ownership_primitive_with_requested_and_socket_scope(
    gate_server, monkeypatch
):
    """socket bound to A + load_session(workspace_id=B) -> primitive(B, A), refused."""
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, id_b = _two_workspaces(client, tmp_home, "prim_load")
    sid = H.create_session(client, workspace_id=id_a)

    with H.ws_connect(client) as ws:
        _bind(ws, sid, id_a)
        calls = _spy_on_primitive(monkeypatch)
        ws.send_json(
            {"command": "load_session", "session_id": sid, "workspace_id": id_b}
        )
        evt, events = H.receive_until_type(ws, "session_loaded", timeout=20.0)

    assert (id_b, id_a) in calls, (
        "the load gate must decide ownership with "
        f"_ownership_refusal_ids(requested={id_b!r}, socket_scope={id_a!r}); "
        f"the spy saw {calls!r}"
    )
    assert calls[-1] == (id_b, id_a), (
        f"the gate's decision must be the LAST primitive call; saw {calls!r}"
    )
    assert evt.get("load_error") is True, (
        f"a cross-workspace load must answer with load_error=True: {evt!r}"
    )
    assert evt.get("workspace_id") is None, (
        f"a refused load must not report a workspace: {evt!r}"
    )
    assert evt.get("workspace_path") == "", (
        f"a refused load must not report a workspace path: {evt!r}"
    )
    assert [e.get("type") for e in events][-1:] == ["session_loaded"], (
        f"unexpected frames before the reply: {[e.get('type') for e in events]!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2 — new_session
# ──────────────────────────────────────────────────────────────────────────────

def test_new_session_calls_ownership_primitive_with_requested_and_socket_scope(
    gate_server, monkeypatch
):
    """socket bound to A + new_session(workspace_id=B) -> primitive(B, A), refused."""
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, id_b = _two_workspaces(client, tmp_home, "prim_new")
    sid = H.create_session(client, workspace_id=id_a)

    with H.ws_connect(client) as ws:
        _bind(ws, sid, id_a)
        calls = _spy_on_primitive(monkeypatch)
        ws.send_json(
            {"command": "new_session", "mode": "custom", "workspace_id": id_b}
        )
        evt, _events = H.receive_until_type(ws, "status_message", timeout=20.0)

    assert (id_b, id_a) in calls, (
        "new_session must decide ownership with "
        f"_ownership_refusal_ids(requested={id_b!r}, socket_scope={id_a!r}); "
        f"the spy saw {calls!r}"
    )
    text = evt.get("text") or ""
    assert "refused" in text.lower(), (
        f"new_session across workspaces must be refused: {evt!r}"
    )
    assert id_a in text and id_b in text, (
        f"the refusal must name both workspaces ({id_a!r}, {id_b!r}): {text!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3 — the D3 class rule: the gate is a scope-CHANGE check, not a
#     scopeless-request check.  An unbound connection is the bootstrap class
#     (exactly one transition), and a request that does not change scope must
#     never be refused — three pins, one per site the rule touches.
# ──────────────────────────────────────────────────────────────────────────────

def _spy_on_path_primitive(monkeypatch):
    """Record every `_ownership_refusal(target_path, bound_id)` call; keep behaviour."""
    import web_ui.backend.bridge as bridge_mod

    real = bridge_mod._ownership_refusal
    calls = []

    def spy(target_path, bound_workspace_id):
        calls.append((target_path, bound_workspace_id))
        return real(target_path, bound_workspace_id)

    monkeypatch.setattr(bridge_mod, "_ownership_refusal", spy)
    assert bridge_mod._ownership_refusal is spy, (
        "the spy must land on the bridge module attribute: apply_config imports "
        "the primitive at call time"
    )
    return calls


def _two_workspaces_with_roots(client, tmp_home, tag):
    """Create workspaces A and B; return (id_a, root_a, id_b, root_b)."""
    root_a = os.path.join(tmp_home, f"{tag}_a")
    root_b = os.path.join(tmp_home, f"{tag}_b")
    info_a = H.create_workspace(client, tmp_home, name=f"{tag}_a")
    info_b = H.create_workspace(client, tmp_home, name=f"{tag}_b")
    id_a, id_b = _ws_id(info_a), _ws_id(info_b)
    assert id_a and id_b and id_a != id_b, f"bad workspaces: A={info_a!r} B={info_b!r}"
    return id_a, root_a, id_b, root_b


def _status_texts(*frames):
    out = []
    for group in frames:
        for evt in group:
            if evt.get("type") == "status_message":
                out.append(evt.get("text") or "")
    return out


def test_bare_new_session_on_bound_socket_is_allowed_and_stays_in_scope(
    gate_server, monkeypatch
):
    """socket bound to A + new_session WITHOUT workspace_id -> allowed, stays in A.

    This is the bare "New Chat" path: it requests no scope change, so the
    ownership gate must neither refuse it nor let the `_project_path` fallback
    re-scope it.
    """
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, root_a, id_b, _root_b = _two_workspaces_with_roots(client, tmp_home, "prim_bare")
    sid = H.create_session(client, workspace_id=id_a)

    with H.ws_connect(client) as ws:
        _bind(ws, sid, id_a)
        calls = _spy_on_primitive(monkeypatch)
        ws.send_json({"command": "new_session", "mode": "custom"})
        evt, events = H.receive_until_type(ws, "session_loaded", timeout=20.0)
        tail = _fence(ws)

    texts = _status_texts(events, tail, [evt])
    assert not evt.get("load_error"), (
        f"a bare new_session must not be refused by the load gate: {evt!r}"
    )
    assert evt.get("workspace_id") == id_a, (
        "a bare new_session on an A-bound socket must stay in A "
        f"({id_a!r}); got {evt.get('workspace_id')!r} (root_a={root_a!r})"
    )
    assert not any("refused" in t.lower() for t in texts), (
        f"a bare new_session must not produce a refusal message: {texts!r}"
    )
    foreign = [c for c in calls if c[1] is not None and c[0] != id_a and c[0] is not None]
    assert not foreign and (id_b, id_a) not in calls, (
        "a bare new_session must not consult ownership against another "
        f"workspace: {calls!r}"
    )


def test_apply_config_on_unscoped_connection_is_bootstrap_allowed(
    gate_server, monkeypatch
):
    """apply_config on an UNSCOPED connection -> bootstrap: allowed, predicate unused.

    The connection names a workspace for the first time here.  D3 says that is
    the one legal transition, so the ownership predicate must not be consulted
    at all (no bound workspace exists to compare against).
    """
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    _id_a, _root_a, id_b, root_b = _two_workspaces_with_roots(client, tmp_home, "prim_cfg")

    with H.ws_connect(client) as ws:
        calls = _spy_on_path_primitive(monkeypatch)
        ws.send_json({"command": "apply_config", "config": {"workspace_path": root_b}})
        evt, events = H.receive_until_type(ws, "session_loaded", timeout=30.0)
        tail = _fence(ws)

    texts = _status_texts(events, tail, [evt])
    assert not evt.get("load_error"), (
        f"bootstrap apply_config must not be refused by the load gate: {evt!r}"
    )
    assert evt.get("workspace_id") == id_b, (
        "bootstrap apply_config must bind the connection to the requested "
        f"workspace {id_b!r}; got {evt.get('workspace_id')!r}"
    )
    assert not any("refused" in t.lower() for t in texts), (
        f"bootstrap apply_config must not produce a refusal message: {texts!r}"
    )
    assert calls == [], (
        "D3: on an unbound connection the ownership predicate must not be "
        f"consulted at all (the bootstrap IS the transition); saw {calls!r}"
    )


def test_apply_config_switch_on_bound_socket_is_refused(gate_server, monkeypatch):
    """socket bound to A + apply_config(workspace_path=B) -> primitive(path, A), refused."""
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, _root_a, id_b, root_b = _two_workspaces_with_roots(client, tmp_home, "prim_cfg_b")
    sid = H.create_session(client, workspace_id=id_a)

    with H.ws_connect(client) as ws:
        _bind(ws, sid, id_a)
        calls = _spy_on_path_primitive(monkeypatch)
        ws.send_json({"command": "apply_config", "config": {"workspace_path": root_b}})
        evt, _events = H.receive_until_type(ws, "status_message", timeout=20.0)
        _fence(ws)  # drain the reply tail so it cannot leak into the next drive

    text = evt.get("text") or ""
    assert "refused" in text.lower(), (
        f"apply_config across workspaces must be refused: {evt!r}"
    )
    assert root_b in text and id_a in text, (
        "the refusal must name the bound workspace and the requested root "
        f"({id_a!r}, {root_b!r}): {text!r}"
    )
    assert calls and calls[-1][1] == id_a, (
        "the switch guard must decide ownership with "
        f"_ownership_refusal(target_path, socket_scope={id_a!r}); saw {calls!r}"
    )
    assert os.path.realpath(str(calls[-1][0])) == os.path.realpath(root_b), (
        f"the predicate must be asked about the requested root {root_b!r}: {calls!r}"
    )


def test_scopeless_cold_load_binds_to_the_sessions_workspace(gate_server, monkeypatch):
    """load_session WITHOUT workspace_id on a fresh socket -> resolves to A, allowed.

    The load gate is a scope-CHANGE check: with no bound scope there is nothing
    to change away from, so the request is admitted and the socket adopts the
    workspace recorded on the session.
    """
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, _root_a, _id_b, _root_b = _two_workspaces_with_roots(client, tmp_home, "prim_cold")
    sid = H.create_session(client, workspace_id=id_a)

    with H.ws_connect(client) as ws:
        calls = _spy_on_primitive(monkeypatch)
        ws.send_json({"command": "load_session", "session_id": sid})
        evt, events = H.receive_until_type(ws, "session_loaded", timeout=20.0)
        tail = _fence(ws)

    texts = _status_texts(events, tail, [evt])
    assert not evt.get("load_error"), (
        f"a scopeless cold load must not be refused: {evt!r}"
    )
    assert evt.get("workspace_id") == id_a, (
        "a scopeless cold load must adopt the session's own workspace "
        f"{id_a!r}; got {evt.get('workspace_id')!r}"
    )
    assert not any("refused" in t.lower() for t in texts), (
        f"a scopeless cold load must not produce a refusal message: {texts!r}"
    )

