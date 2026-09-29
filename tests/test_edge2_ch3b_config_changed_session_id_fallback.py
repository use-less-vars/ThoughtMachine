"""Edge2 / Change 3b — the six ``resolve_effective_permissions`` push sites now
pass a GUARDED fallback session id instead of the raw ``_session_id``.

There is a window in which a bridge is *loaded but its id is unset*:
``create_session`` / ``load_session`` populate ``bridge._loaded_session`` +
``_session_config`` + ``_workspace_id`` but do NOT set ``bridge._session_id``
(only ``start`` / ``_restart_controller`` / ``_on_controller_event`` capture it).
Any push site that passed ``bridge._session_id`` directly therefore carried
``None`` into ``ConfigManager.resolve_effective_permissions`` during that window,
and (because ``resolve_effective_permissions`` fails CLOSED when either
identifier is missing) reported a FALSE deny-all profile while a real session
actually existed on disk.

The fix passes the guarded canonical fallback:

    bridge._session_id or (bridge._loaded_session.session_id if bridge._loaded_session else None)

at every one of the six sites.  The gate itself is untouched, the resolved
profile remains ceiling-capped, and no banned input becomes allowed.

TRUTHING criterion (this change is a truthing, not a widening):
"A change that moves a reported permission value from false-deny-all to
true-narrower-set, where the gate is untouched, the profile remains
ceiling-capped, and no banned input becomes allowed, is a truthing, not a
widening."

Sites are identified STRUCTURALLY, never by line number.  Each site is the
n-th ``resolve_effective_permissions`` call (in source order, via a pre-order
AST walk) inside its enclosing function/class scope, and for the server sites
that ordinal is tied to the surrounding ``command == '<name>'`` branch.  A site
is reached by ``(path, enclosing scope, ordinal)``; moving code around (which
shifts lines) does not break the tests, whereas adding / removing / reordering
a site does.

Each behavioural test evaluates the SECOND argument *as actually written at the
site* (extracted from the source with the ``ast`` module) against a bridge in
the loaded-but-id-unset state, then feeds it to
``resolve_effective_permissions``.  Reverting a site to the raw ``_session_id``
makes that site's test FAIL (the raw id is ``None`` -> deny-all), so the suite
is a real mutation detector, not a mirror of hand-written logic.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from web_ui.backend import config_manager as cm  # noqa: E402

CANONICAL_KEYS = {
    "container",
    "filesystem",
    "git",
    "host_bash",
    "mcp",
    "network",
}

DENY_ALL = {
    "container": False,
    "filesystem": "banned",
    "git": "banned",
    "host_bash": "banned",
    "mcp": "banned",
    "network": "banned",
}

# The real, gate-computed, ceiling-capped profile the six sites must be able to
# report for the loaded-but-id-unset session below: the on-disk session grants
# (filesystem=write, git=write, network=outbound) capped by the workspace
# ceiling (filesystem=read, git=read, network=banned, container=False,
# host_bash=banned).
EXPECTED_REAL_PROFILE = {
    "container": False,
    "filesystem": "read",
    "git": "read",
    "host_bash": "banned",
    "mcp": "banned",
    "network": "banned",
}

SERVER_PATH = _ROOT / "web_ui" / "backend" / "server.py"
BRIDGE_PATH = _ROOT / "web_ui" / "backend" / "bridge.py"

# The enclosing scope each file's site(s) live in (a function/method name).
SERVER_SCOPE = "websocket_endpoint"
BRIDGE_SCOPE = "apply_config"

SERVER_GUARDED = (
    "bridge._session_id or "
    "(bridge._loaded_session.session_id if bridge._loaded_session else None)"
)
SERVER_RAW = "bridge._session_id"
BRIDGE_GUARDED = (
    "self._session_id or "
    "(self._loaded_session.session_id if self._loaded_session else None)"
)
BRIDGE_RAW = "self._session_id"

# (path, enclosing scope, 1-based ordinal within that scope, label, receiver
#  name, guarded expr, raw expr).  NOTE: no line numbers — a site is reached by
#  (path, scope, ordinal) only.
SITES = [
    (SERVER_PATH, SERVER_SCOPE, 1, "server.get_config", "bridge", SERVER_GUARDED, SERVER_RAW),
    (SERVER_PATH, SERVER_SCOPE, 2, "server.apply_config_failure", "bridge", SERVER_GUARDED, SERVER_RAW),
    (SERVER_PATH, SERVER_SCOPE, 3, "server.load_session", "bridge", SERVER_GUARDED, SERVER_RAW),
    (SERVER_PATH, SERVER_SCOPE, 4, "server.new_session", "bridge", SERVER_GUARDED, SERVER_RAW),
    (SERVER_PATH, SERVER_SCOPE, 5, "server.set_project", "bridge", SERVER_GUARDED, SERVER_RAW),
    (BRIDGE_PATH, BRIDGE_SCOPE, 1, "bridge.apply_config", "self", BRIDGE_GUARDED, BRIDGE_RAW),
]

# label -> the ``command == '<name>'`` branch the site must sit inside, where the
# AST walk can see one (the server sites); ``None`` == not a command branch (the
# bridge method).  Ties every planned ordinal to its documented label
# structurally, so a reordering of the sites is caught.
EXPECTED_COMMAND = {
    "server.get_config": "get_config",
    "server.apply_config_failure": "apply_config",
    "server.load_session": "load_session",
    "server.new_session": "new_session",
    "server.set_project": "set_project",
    "bridge.apply_config": None,
}


# ── Fake bridge in the loaded-but-id-unset state ──────────────────────────────


class _FakeSessionConfig:
    """Minimal duck-typed stand-in for ``SessionConfig``."""

    def __init__(self, session_permissions=None, workspace_id=None):
        self.session_permissions = session_permissions or {}
        self.workspace_id = workspace_id


class _FakeLoadedSession:
    """A real loaded session carries a ``.session_id``."""

    def __init__(self, session_id):
        self.session_id = session_id


class _FakeBridge:
    """A bridge that has a loaded session but whose ``_session_id`` is unset.

    This is exactly the window ``create_session`` / ``load_session`` leave the
    bridge in until the first controller event captures the id.
    """

    def __init__(self, session_config, session_id, loaded_session, workspace_id):
        self._session_config = session_config
        self._session_id = session_id
        self._loaded_session = loaded_session
        self._workspace_id = workspace_id


def _loaded_but_id_unset_bridge():
    sc = _FakeSessionConfig(
        {"filesystem": "write", "git": "write", "network": "outbound"},
        workspace_id="ws-x",
    )
    bridge = _FakeBridge(
        session_config=sc,
        session_id=None,  # <-- the unset id (loaded-but-id-unset window)
        loaded_session=_FakeLoadedSession("sess-x"),
        workspace_id="ws-x",
    )
    return bridge, sc


# ── Structural (line-number-free) source extraction ──────────────────────────


def _rel(path):
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def _iter_source_order(node):
    """Pre-order DFS: yields nodes in document (source) order."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _iter_source_order(child)


def _parent_map(tree):
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _is_resolve_call(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = getattr(func, "attr", None) or getattr(func, "id", None)
    return name == "resolve_effective_permissions"


def _scope_of(node, parent):
    """Nearest enclosing function/method/class name, or None."""
    p = parent.get(node)
    while p is not None:
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return p.name
        p = parent.get(p)
    return None


def _command_literal(test):
    """Return the ``'<name>'`` of a ``command == '<name>'`` test, else None."""
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "command"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and isinstance(test.comparators[0].value, str)
    ):
        return test.comparators[0].value
    return None


def _enclosing_command(node, parent):
    """Nearest enclosing ``command == '<name>'`` branch name, or None."""
    p = parent.get(node)
    while p is not None:
        if isinstance(p, ast.If):
            name = _command_literal(p.test)
            if name is not None:
                return name
        p = parent.get(p)
    return None


def _resolve_calls(path):
    """Every ``resolve_effective_permissions`` call in source order.

    Returns ``[(scope_name, command_or_None, call_node), ...]`` — no line
    numbers are consulted anywhere.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parent = _parent_map(tree)
    out = []
    for node in _iter_source_order(tree):
        if _is_resolve_call(node):
            out.append((_scope_of(node, parent), _enclosing_command(node, parent), node))
    return out


def _summary(path, calls):
    sites = ", ".join(
        f"{scope}#{i}" for i, (scope, _cmd, _node) in enumerate(calls, start=1)
    )
    return f"{_rel(path)}: {len(calls)} resolve site(s) [{sites}]"


def _site_desc(path, scope, ordinal):
    return f"{_rel(path)}::{scope} site#{ordinal}"


def _site(path, scope, ordinal):
    """Reach a site structurally: the ordinal-th call within ``scope``.

    Returns ``(command, node)``.  Raises if the scope has no such ordinal.
    """
    in_scope = [
        (cmd, node) for (sc, cmd, node) in _resolve_calls(path) if sc == scope
    ]
    if not 1 <= ordinal <= len(in_scope):
        raise AssertionError(
            f"{_rel(path)}::{scope}: no resolve site #{ordinal} "
            f"(scope contains {len(in_scope)})"
        )
    return in_scope[ordinal - 1]


def _second_arg_source(node):
    assert len(node.args) == 3, "expected 3 args at resolve site"
    return ast.unparse(node.args[1])


def _eval_args(node, obj_name, obj):
    ns = {"__builtins__": {}, obj_name: obj}
    return (
        eval(ast.unparse(node.args[0]), ns),
        eval(ast.unparse(node.args[1]), ns),
        eval(ast.unparse(node.args[2]), ns),
    )


def _write_workspace(tmp_path, workspace_id, ceiling):
    wdir = tmp_path / "workspaces" / workspace_id
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "config.json").write_text(json.dumps({"permissions": ceiling}))


@pytest.fixture
def disk_store(monkeypatch, tmp_path):
    """A real vault whose session grants + workspace ceiling are readable.

    With this store, ``resolve_effective_permissions(sc, "sess-x", "ws-x")``
    returns the real, ceiling-capped ``EXPECTED_REAL_PROFILE`` (NOT deny-all).
    """
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    from thoughtmachine import permission_store as ps
    from thoughtmachine.vault import vault_root

    ps.write_session_permissions(
        vault_root(),
        "ws-x",
        "sess-x",
        {"filesystem": "write", "git": "write", "network": "outbound"},
    )
    _write_workspace(
        tmp_path,
        "ws-x",
        {
            "filesystem": "read",
            "container": False,
            "git": "read",
            "network": "banned",
            "host_bash": "banned",
        },
    )
    return tmp_path


# ── Completeness: exactly the six known sites, no more ────────────────────────


def test_server_has_exactly_five_resolve_sites():
    calls = _resolve_calls(SERVER_PATH)
    assert len(calls) == 5, _summary(SERVER_PATH, calls)
    # all five live in the websocket endpoint ...
    assert [scope for scope, _cmd, _node in calls] == [SERVER_SCOPE] * 5, (
        _summary(SERVER_PATH, calls)
    )
    # ... and their source-order ordinals sit in these command branches (this is
    # the structural stand-in for the old line-number list).
    assert [cmd for _scope, cmd, _node in calls] == [
        "get_config",
        "apply_config",
        "load_session",
        "new_session",
        "set_project",
    ], _summary(SERVER_PATH, calls)


def test_bridge_has_exactly_one_resolve_site():
    calls = _resolve_calls(BRIDGE_PATH)
    assert len(calls) == 1, _summary(BRIDGE_PATH, calls)
    assert [scope for scope, _cmd, _node in calls] == [BRIDGE_SCOPE], (
        _summary(BRIDGE_PATH, calls)
    )
    assert [cmd for _scope, cmd, _node in calls] == [None], _summary(BRIDGE_PATH, calls)


# ── Source contract: every site's 2nd argument is the guarded fallback ─────────
#    (ordinal -> label -> enclosing command is asserted structurally here)


@pytest.mark.parametrize(
    "path,scope,ordinal,label,obj_name,guarded,raw",
    SITES,
    ids=[s[3] for s in SITES],
)
def test_site_second_argument_is_guarded_fallback(
    path, scope, ordinal, label, obj_name, guarded, raw
):
    command, node = _site(path, scope, ordinal)
    assert command == EXPECTED_COMMAND[label], (
        f"{_site_desc(path, scope, ordinal)}: enclosing command {command!r} "
        f"!= expected {EXPECTED_COMMAND[label]!r} for label {label!r}"
    )
    actual = _second_arg_source(node)
    assert actual == guarded, (
        f"{_site_desc(path, scope, ordinal)}: 2nd arg = {actual!r} "
        f"(expected guarded form)"
    )
    assert actual != raw, (
        f"{_site_desc(path, scope, ordinal)}: 2nd arg = {actual!r} (still the raw id)"
    )


@pytest.mark.parametrize(
    "path,scope,ordinal,label,obj_name,guarded,raw",
    SITES,
    ids=[s[3] for s in SITES],
)
def test_site_guarded_argument_handles_missing_loaded_session(
    path, scope, ordinal, label, obj_name, guarded, raw
):
    """The guarded form must not raise when ``_loaded_session`` is None."""
    empty = _FakeBridge(
        session_config=_FakeSessionConfig({}, workspace_id="ws-x"),
        session_id=None,
        loaded_session=None,
        workspace_id="ws-x",
    )
    _command, node = _site(path, scope, ordinal)
    _, resolved_id, _ = _eval_args(node, obj_name, empty)
    assert resolved_id is None, (
        f"{_site_desc(path, scope, ordinal)}: expected None id, got {resolved_id!r}"
    )


# ── Behaviour: the six sites now report the true profile in that window ────────


@pytest.mark.parametrize(
    "path,scope,ordinal,label,obj_name,guarded,raw",
    SITES,
    ids=[s[3] for s in SITES],
)
def test_site_reports_true_profile_when_loaded_but_id_unset(
    path, scope, ordinal, label, obj_name, guarded, raw, disk_store
):
    bridge, sc = _loaded_but_id_unset_bridge()
    _command, node = _site(path, scope, ordinal)
    sc_arg, resolved_id, ws_arg = _eval_args(node, obj_name, bridge)

    # the guarded id recovers the real session id despite ``_session_id`` unset
    assert resolved_id == "sess-x", (
        f"{_site_desc(path, scope, ordinal)}: resolved id {resolved_id!r}"
    )

    out = cm.ConfigManager.resolve_effective_permissions(sc_arg, resolved_id, ws_arg)
    assert set(out.keys()) == CANONICAL_KEYS, _site_desc(path, scope, ordinal)
    assert out == EXPECTED_REAL_PROFILE, (
        f"{_site_desc(path, scope, ordinal)}: profile {out!r}"
    )
    assert out != DENY_ALL, _site_desc(path, scope, ordinal)


@pytest.mark.parametrize(
    "path,scope,ordinal,label,obj_name,guarded,raw",
    SITES,
    ids=[s[3] for s in SITES],
)
def test_site_raw_id_would_have_reported_false_deny_all(
    path, scope, ordinal, label, obj_name, guarded, raw, disk_store
):
    """The BEFORE state, exercised at the site: the raw ``_session_id`` is None
    and resolves to a FALSE deny-all even though a real session exists."""
    bridge, sc = _loaded_but_id_unset_bridge()

    raw_id = eval(raw, {"__builtins__": {}}, {obj_name: bridge})
    assert raw_id is None, f"{_site_desc(path, scope, ordinal)}: raw id {raw_id!r}"
    assert (
        cm.ConfigManager.resolve_effective_permissions(sc, raw_id, "ws-x") == DENY_ALL
    ), _site_desc(path, scope, ordinal)


# ── Truthing (not widening): fail-closed preserved, ceiling still caps ────────


def test_raw_id_is_none_in_loaded_but_id_unset_state():
    bridge, _ = _loaded_but_id_unset_bridge()
    assert bridge._session_id is None
    assert bridge._loaded_session is not None
    assert bridge._loaded_session.session_id == "sess-x"
    assert bridge._session_config is not None
    assert bridge._workspace_id == "ws-x"


def test_missing_id_still_fails_closed_deny_all(disk_store):
    """Fail-closed is PRESERVED: with no id at all the gate still denies all."""
    sc = _FakeSessionConfig({"filesystem": "write"}, workspace_id="ws-x")
    out = cm.ConfigManager.resolve_effective_permissions(sc, None, "ws-x")
    assert out == DENY_ALL


def test_guarded_fallback_reports_ceiling_capped_profile_not_deny_all(disk_store):
    """AFTER state: the guarded id reports the true, ceiling-capped profile.

    This is the truthing: false-deny-all -> true-narrower-set."""
    bridge, sc = _loaded_but_id_unset_bridge()
    guarded_id = bridge._session_id or (
        bridge._loaded_session.session_id if bridge._loaded_session else None
    )
    out = cm.ConfigManager.resolve_effective_permissions(sc, guarded_id, "ws-x")
    assert out == EXPECTED_REAL_PROFILE
    # narrower than a permissive request, not wider: the ceiling caps it
    assert out["filesystem"] == "read"  # granted "write", capped to "read"
    assert out["git"] == "read"  # granted "write", capped to "read"
    assert out["network"] == "banned"  # granted "outbound", capped to "banned"


def test_truthing_does_not_allow_any_ceiling_banned_category(disk_store):
    """No banned input becomes allowed: everything the ceiling bans stays banned."""
    bridge, sc = _loaded_but_id_unset_bridge()
    guarded_id = bridge._session_id or (
        bridge._loaded_session.session_id if bridge._loaded_session else None
    )
    out = cm.ConfigManager.resolve_effective_permissions(sc, guarded_id, "ws-x")
    assert out["container"] is False
    assert out["host_bash"] == "banned"
    assert out["mcp"] == "banned"
    assert out["network"] == "banned"
    # and the resolved profile is never more permissive than a category the
    # ceiling refuses outright
    assert out != DENY_ALL  # it is a real profile, not the deny-all placeholder
