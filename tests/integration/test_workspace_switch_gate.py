"""Workspace-switch ownership gate (integration).

A `/ws` connection BOUND to workspace A may move to a brand-new path outside
every registered workspace root, but never onto another registered workspace
(nor into its tree, nor through a symlink pointing at it).  The rule lives in
one shared predicate — `web_ui/backend/bridge.py::_ownership_refusal` — and is
called by BOTH workspace-switch commands:

  * `apply_config` (`web_ui/backend/server.py`, workspace_changed branch), status
    text `\u26a0 Workspace switch refused: this connection is scoped to
    workspace {X} and cannot switch to {Y}.`
  * `set_project` (`web_ui/backend/server.py`), status text
    `\u26a0 Project switch refused: this connection is scoped to workspace {X}
    and cannot switch to {Y}.`

Three tests only:

  1. `test_apply_config_refuses_symlink_to_other_workspace_root`
  2. `test_apply_config_allows_fresh_path_outside_all_roots`
  3. `test_set_project_and_apply_config_use_same_ownership_rule` (matrix a–e:
     both commands, one freshly bound socket EACH, identical verdict — and, for
     the allowed cases, positive evidence that each command actually acted)

Frames are fenced with `get_open_sessions` (commands are processed in order, so
its `open_sessions` reply proves the preceding command ran to completion — no
sleeps, no racing reads).
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
    """Create (and register) workspaces A and B; return ids + roots."""
    info_a = H.create_workspace(client, tmp_home, name=f"{tag}_a")
    info_b = H.create_workspace(client, tmp_home, name=f"{tag}_b")
    id_a, id_b = _ws_id(info_a), _ws_id(info_b)
    assert id_a and id_b, f"workspace ids missing: A={info_a!r} B={info_b!r}"
    root_a = info_a.get("root") or os.path.join(tmp_home, f"{tag}_a")
    root_b = info_b.get("root") or os.path.join(tmp_home, f"{tag}_b")
    return id_a, root_a, id_b, root_b


def _is_same_or_inside(path, root):
    path = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(os.path.abspath(root))
    if path == root:
        return True
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


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


def _command(ws, payload):
    """Send one command, then return exactly the frames it emitted."""
    ws.send_json(payload)
    return _fence(ws)


def _command_on_fresh_binding(client, workspace_id, payload):
    """Run `payload` on its OWN socket, freshly bound to `workspace_id`.

    Every command gets a new session in that workspace and a new socket, so the
    binding is always a plain `load_session`.  Sharing one socket would let an
    ALLOWED switch rebind it (apply_config re-points the bound session in
    place, set_project replaces it) and the next command would then be judged
    against a bound state the previous command already moved.

    Returns `(events, session_id)`.
    """
    sid = H.create_session(client, workspace_id=workspace_id)
    with H.ws_connect(client) as ws:
        _bind(ws, sid, workspace_id)
        events = _command(ws, payload)
    return events, sid


def _verdict(events):
    """(verdict, status_texts) where verdict is 'refused' or 'allowed'.

    'refused' == the command emitted a status_message whose text says
    "refused"; anything else means the command did not refuse the target.
    """
    texts = [
        evt.get("text") or ""
        for evt in events
        if evt.get("type") == "status_message"
    ]
    refused = [t for t in texts if "refused" in t.lower()]
    return ("refused" if refused else "allowed"), texts


def _apply_config(target):
    return {"command": "apply_config", "config": {"mode": "custom", "workspace_path": target}}


def _set_project(target):
    return {"command": "set_project", "project": target}


# ──────────────────────────────────────────────────────────────────────────────
# 1 — symlink into another workspace's root
# ──────────────────────────────────────────────────────────────────────────────

def test_apply_config_refuses_symlink_to_other_workspace_root(gate_server):
    """A symlink whose target is workspace B's root IS workspace B.

    Roots are realpath-normalised before comparison (bridge.py
    `_normalise_workspace_root` / `_resolve_workspace_id`), so the switch must
    be refused exactly as if the client had sent B's root literally.
    """
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, root_a, id_b, root_b = _two_workspaces(client, tmp_home, "gate_symlink")
    assert id_a != id_b
    sid = H.create_session(client, workspace_id=id_a)

    link = os.path.join(tmp_home, "gate_symlink_into_b")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(root_b, link)
    assert os.path.realpath(link) == os.path.realpath(root_b)

    with H.ws_connect(client) as ws:
        _bind(ws, sid, id_a)
        events = _command(ws, _apply_config(link))

    verdict, texts = _verdict(events)
    assert verdict == "refused", (
        f"apply_config switched onto a symlink into workspace B ({root_b!r}) "
        f"instead of refusing it; status_message texts={texts!r} "
        f"event_types={[e.get('type') for e in events]}"
    )
    joined = " ".join(texts)
    assert id_a in joined, (
        f"refusal must name the bound workspace {id_a!r}: {joined!r}"
    )
    assert link in joined or os.path.realpath(link) in joined, (
        f"refusal must name the refused target {link!r}: {joined!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2 — brand-new path outside every registered root
# ──────────────────────────────────────────────────────────────────────────────

def test_apply_config_allows_fresh_path_outside_all_roots(gate_server):
    """The legitimate case: a brand-new folder outside every workspace root.

    The switch must be ALLOWED and must actually land in a NEW workspace (the
    permission-reset notice names it) — "no refusal frame" alone would also
    cover a silent no-op.
    """
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, root_a, id_b, root_b = _two_workspaces(client, tmp_home, "gate_fresh")
    sid = H.create_session(client, workspace_id=id_a)

    fresh = os.path.join(tmp_home, "gate_fresh_outside_all_roots")
    os.makedirs(fresh, exist_ok=True)
    assert not _is_same_or_inside(fresh, root_a), f"{fresh!r} is inside A's root"
    assert not _is_same_or_inside(fresh, root_b), f"{fresh!r} is inside B's root"

    with H.ws_connect(client) as ws:
        _bind(ws, sid, id_a)
        events = _command(ws, _apply_config(fresh))

    verdict, texts = _verdict(events)
    assert verdict == "allowed", (
        f"apply_config refused a fresh path outside every workspace root "
        f"({fresh!r}): {texts!r}"
    )

    notices = [
        evt for evt in events
        if evt.get("type") == "status_message" and evt.get("permissions_reset") is True
    ]
    assert notices, (
        "an allowed workspace switch must emit the permission-reset notice; "
        f"saw {[e.get('type') for e in events]}"
    )
    new_ws = notices[-1].get("workspace_id")
    assert new_ws, f"switch notice must carry the new workspace_id: {notices[-1]!r}"
    assert new_ws != id_a, (
        f"switch to {fresh!r} must land in a NEW workspace, not the bound {id_a!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3 — both commands, same rule
# ──────────────────────────────────────────────────────────────────────────────

_CASES = (
    ("a", "own-root", "allowed"),
    ("b", "other-root", "refused"),
    ("c", "subdir-inside-other-root", "refused"),
    ("d", "symlink-to-other-root", "refused"),
    ("e", "fresh-path-outside-all-roots", "allowed"),
)


def _matrix_target(kind, tmp_home, case, root_a, root_b, tag):
    """Build the case target for ONE command (`tag` = "ac" or "sp").

    The two commands must not share a path: an ALLOWED `apply_config`
    auto-registers the fresh path it switched to, so re-using that path would
    hand `set_project` an already-registered workspace instead of a fresh path
    — a different state, not a different rule.
    """
    if kind == "own-root":
        return root_a
    if kind == "other-root":
        return root_b
    if kind == "subdir-inside-other-root":
        sub = os.path.join(root_b, f"nested_{case}_{tag}")
        os.makedirs(sub, exist_ok=True)
        return sub
    if kind == "symlink-to-other-root":
        link = os.path.join(tmp_home, f"gate_matrix_link_{case}_{tag}")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(root_b, link)
        return link
    if kind == "fresh-path-outside-all-roots":
        # Must EXIST: set_project confines with require_dir=True (apply_config
        # does not), so an existing directory keeps both commands comparable.
        fresh = os.path.join(tmp_home, f"gate_matrix_fresh_{case}_{tag}")
        os.makedirs(fresh, exist_ok=True)
        return fresh
    raise AssertionError(f"unknown matrix target kind {kind!r}")


def _frames(events, type_name):
    return [evt for evt in events if evt.get("type") == type_name]


def _switch_notice(events):
    """The permission-reset notice a REAL workspace switch emits (None if none).

    Both switch paths — apply_config's workspace_changed branch and
    set_project — send it only AFTER they have stopped the old bridge, resolved
    the workspace and re-pointed the session, so its presence is evidence that
    a switch actually ran (see the `✅ Switched to project:` status_message).
    """
    notices = [
        evt for evt in _frames(events, "status_message")
        if evt.get("permissions_reset") is True
    ]
    return notices[-1] if notices else None


def _assert_apply_config_took_effect(events, target, root_a, id_a, case, kind):
    """An 'allowed' apply_config must have APPLIED something.

    `_verdict` reads the absence of a refusal text as "allowed", which a
    silent no-op also satisfies.  The ordinary (non-switch) apply path answers
    with a `config_changed` frame carrying the applied config; the switch path
    adds the permission-reset notice on top of it.
    """
    frame_types = [evt.get("type") for evt in events]
    failures = [
        t for t in frame_types
        if t in ("config_apply_failed", "config_queued", "error")
    ]
    assert not failures, (
        f"case ({case}) {kind}: apply_config did not apply cleanly: {frame_types!r}"
    )
    changed = _frames(events, "config_changed")
    assert changed, (
        f"case ({case}) {kind}: apply_config was 'allowed' but emitted no "
        f"config_changed frame — a silent no-op is not an allowed apply; "
        f"frames={frame_types!r}"
    )
    notice = _switch_notice(events)
    if _is_same_or_inside(target, root_a):
        # The target IS the bound root, so the config is applied in place and
        # the answer must still report a config for THIS workspace.
        paths = [
            (evt.get("config") or {}).get("workspace_path") for evt in changed
        ]
        assert any(p and _is_same_or_inside(p, root_a) for p in paths), (
            f"case ({case}) {kind}: apply_config on the bound root {root_a!r} must "
            f"report the applied config for it; config_changed "
            f"config.workspace_path(s)={paths!r}"
        )
        if notice is not None:
            assert notice.get("workspace_id") in (id_a, ""), (
                f"case ({case}) {kind}: apply_config on the bound root must stay in "
                f"workspace {id_a!r}: {notice!r}"
            )
    else:
        assert notice is not None, (
            f"case ({case}) {kind}: apply_config was 'allowed' but emitted no "
            f"permission-reset notice — a silent no-op is not an allowed switch; "
            f"frames={frame_types!r}"
        )
        new_ws = notice.get("workspace_id")
        assert new_ws and new_ws != id_a, (
            f"case ({case}) {kind}: an allowed switch to {target!r} must land in a "
            f"NEW workspace, not the bound {id_a!r}: {notice!r}"
        )
        text = notice.get("text") or ""
        assert target in text or os.path.realpath(target) in text, (
            f"case ({case}) {kind}: the switch notice must name {target!r}: {text!r}"
        )


def _assert_set_project_took_effect(
    events, target, root_a, id_a, bound_session, case, kind
):
    """An 'allowed' set_project must have SWITCHED.

    set_project never no-ops: past the ownership gate it stops the old bridge,
    resolves/auto-registers the workspace, and answers with a `session_loaded`
    for a BRAND-NEW session (`replacement: True`) plus the permission-reset
    notice.
    """
    frame_types = [evt.get("type") for evt in events]
    loaded = _frames(events, "session_loaded")
    assert loaded, (
        f"case ({case}) {kind}: set_project was 'allowed' but emitted no "
        f"session_loaded frame — a silent no-op is not an allowed switch; "
        f"frames={frame_types!r}"
    )
    load = loaded[-1]
    assert load.get("replacement") is True, (
        f"case ({case}) {kind}: an allowed set_project must announce a "
        f"replacement session: {load!r}"
    )
    new_session = load.get("session_id")
    assert new_session and new_session != bound_session, (
        f"case ({case}) {kind}: an allowed set_project must create a FRESH session, "
        f"not re-load the bound {bound_session!r}: {new_session!r}"
    )
    landed = load.get("workspace_path")
    assert landed and os.path.realpath(landed) == os.path.realpath(target), (
        f"case ({case}) {kind}: set_project must land on {target!r}: {landed!r}"
    )
    landed_ws = load.get("workspace_id")
    assert landed_ws, (
        f"case ({case}) {kind}: session_loaded must carry a workspace_id: {load!r}"
    )
    if _is_same_or_inside(target, root_a):
        assert landed_ws == id_a, (
            f"case ({case}) {kind}: set_project on the bound root must stay in "
            f"workspace {id_a!r}: {landed_ws!r}"
        )
    else:
        assert landed_ws != id_a, (
            f"case ({case}) {kind}: an allowed switch to {target!r} must land in a "
            f"NEW workspace, not the bound {id_a!r}: {landed_ws!r}"
        )
    notice = _switch_notice(events)
    assert notice is not None, (
        f"case ({case}) {kind}: an allowed set_project must emit the "
        f"permission-reset notice; frames={frame_types!r}"
    )
    assert notice.get("workspace_id") == landed_ws, (
        f"case ({case}) {kind}: the reset notice must name the workspace it "
        f"switched to {landed_ws!r}: {notice!r}"
    )


@pytest.mark.parametrize(
    "case,kind,expected",
    _CASES,
    ids=[f"{c[0]}-{c[1]}" for c in _CASES],
)
def test_set_project_and_apply_config_use_same_ownership_rule(
    gate_server, case, kind, expected
):
    """One shared predicate (`bridge._ownership_refusal`) drives both commands.

    Each command runs on its OWN socket, freshly bound to workspace A (a second
    session in A, so the binding is a plain `load_session`): an allowed switch
    rebinds the socket it ran on, so a command judged on a socket a previous
    command already moved would be answering a different question.  Each
    command also gets its own target, for the same reason on the registry side
    — an allowed `apply_config` auto-registers the fresh path it switched to.
    The two verdicts must be identical and equal to the expected rule outcome,
    and an "allowed" verdict must be backed by real work, not by silence; a
    failure prints both verdicts and both status-message texts.
    """
    app, tmp_home, _server_mod = gate_server
    client = H.make_client(app)

    id_a, root_a, id_b, root_b = _two_workspaces(client, tmp_home, f"gate_mx_{case}")
    assert id_a != id_b

    apply_target = _matrix_target(kind, tmp_home, case, root_a, root_b, "ac")
    project_target = _matrix_target(kind, tmp_home, case, root_a, root_b, "sp")

    apply_events, apply_sid = _command_on_fresh_binding(
        client, id_a, _apply_config(apply_target)
    )
    project_events, project_sid = _command_on_fresh_binding(
        client, id_a, _set_project(project_target)
    )

    apply_verdict, apply_texts = _verdict(apply_events)
    project_verdict, project_texts = _verdict(project_events)

    # A command that answered with NOTHING is not an allowed command: the
    # verdict must come from frames the server actually sent.
    for label, events in (("apply_config", apply_events), ("set_project", project_events)):
        assert events, (
            f"case ({case}) {kind}: {label} emitted no frames at all on a freshly "
            f"bound socket — silence is not an allowed verdict"
        )

    detail = (
        f"case ({case}) {kind}: bound_workspace={id_a!r} "
        f"A_root={root_a!r} B_root={root_b!r}\n"
        f"    apply_config target={apply_target!r} session={apply_sid!r} "
        f"verdict={apply_verdict!r}; status_message texts={apply_texts!r}\n"
        f"    set_project  target={project_target!r} session={project_sid!r} "
        f"verdict={project_verdict!r}; status_message texts={project_texts!r}"
    )
    assert apply_verdict == project_verdict, (
        "apply_config and set_project disagree on the workspace-ownership rule:\n" + detail
    )
    assert apply_verdict == expected, (
        f"workspace-ownership verdict for {(case)} {kind} should be {expected!r}:\n" + detail
    )

    if expected == "allowed":
        _assert_apply_config_took_effect(
            apply_events, apply_target, root_a, id_a, case, kind
        )
        _assert_set_project_took_effect(
            project_events, project_target, root_a, id_a, project_sid, case, kind
        )
