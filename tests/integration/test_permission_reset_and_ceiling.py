"""Workspace permission reset and ceiling enforcement across surfaces.

Five tests verify that a workspace permission ceiling set via
``PUT /api/workspace/{ws_id}/permissions`` (persisted to config.json) is
enforced by every surface that reports effective permissions:

[a] REST ``effective_permissions`` without a session id must reflect the
    ceiling (expected FAIL on current code: the endpoint serves hardcoded
    read-only defaults, ignoring the ceiling).
[b] REST ``effective_permissions`` with a session id whose stored session
    permissions are a raw write grant must be capped by the ceiling
    (expected FAIL on current code: the endpoint applies capabilities only,
    never the ceiling, so the raw write grant leaks through).
[c] After a restart, the persisted ceiling must still be enforced by the
    REST endpoint (expected FAIL on current code: same root cause as [a]).
[d] A self-contained subprocess test proving persistence of the ceiling
    across a real process boundary (child writes the ceiling, parent reads
    it back from the summary endpoint - expected PASS - and then from the
    effective_permissions endpoint - expected FAIL, same root cause as
    [a]).
[e] The WS ``load_session`` path, including a same-process reconnect on a
    second websocket connection, must report ceiling-capped session
    permissions (expected possibly FAIL on current code: the cached-bridge
    reload path may send uncapped live state).

Findings locked into the expected failures:

* ``GET /api/workspace/{ws_id}/effective_permissions``
  (web_ui/backend/workspace_routes.py) never passes the workspace ceiling
  to ``security_gate.get_effective_permissions`` (third parameter,
  ``workspace_permissions``, defaults to None) and, without a session id,
  serves hardcoded read-only defaults instead of the stored ceiling.
* The WS path (bridge.load_session + cached bridge config) DOES apply the
  ceiling via ``security_gate.apply_workspace_ceiling``.

Expected log noise (not failures): docker.verify_integrity connection
errors, Pydantic V1 deprecations, ObservableList auto-wrap warnings,
"Could not backfill workspace_path from registry".
"""
import importlib
import os
import subprocess
import sys
import tempfile

import pytest

try:
    from tests.integration import harness
except ImportError:  # non-package layout: tests/integration on sys.path
    import harness

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env():
    app, tmp_home, stop_fn = harness.start_backend()
    harness.register_mock_provider()
    client = harness.make_client(app)
    yield client, tmp_home, stop_fn
    stop_fn()


def _restart(tmp_home, stop_fn):
    """Keep the fake HOME, purge modules, import a fresh backend."""
    stop_fn(rmtree=False, restore_env=False)
    app2 = harness.restart(tmp_home)
    harness.register_mock_provider()
    return harness.make_client(app2)


def _apply_config_dict(ws_root, session_permissions):
    return {
        "workspace_path": ws_root,
        "mode": "custom",
        "provider": "mock",
        "model": "mock-model",
        "session_permissions": session_permissions,
        "temperature": 0.2,
        "max_turns": 20,
        "enabled_tools": [],
    }


def _load_session(wsock, sid):
    wsock.send_json({"command": "load_session", "session_id": sid})
    evt, _ = harness.receive_until_session_loaded(wsock)
    assert not evt.get("load_error", False), f"session load failed: {evt}"
    return evt


def _inject_raw_session_permissions(sid, ws_id):
    """Write a raw (uncapped) write grant straight into the session store."""
    server_mod = importlib.import_module("web_ui.backend.server")
    store = server_mod._get_session_store()
    session = store.load_session(sid, workspace_id=ws_id)
    assert session is not None, f"session {sid} missing from store"
    session.metadata.setdefault("session_config", {})["session_permissions"] = {
        "filesystem": "write",
        "git": "write",
        "network": "write",
        "container": True,
        "system": "write",
        "execution": "write",
    }
    store.save_session(session, workspace_id=ws_id)
    # Sanity: the injection is visible on a reload.
    reloaded = store.load_session(sid, workspace_id=ws_id)
    stored = reloaded.metadata["session_config"]["session_permissions"]
    assert stored["filesystem"] == "write", stored


# ---------------------------------------------------------------------------
# [a] REST effective_permissions without a session id must honor the ceiling
# ---------------------------------------------------------------------------

def test_effective_permissions_without_session_reflects_workspace_ceiling(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="pr_a")
    harness.put_permissions(client, ws["workspace_id"], {"filesystem": "banned"})

    ep = harness.get_effective_permissions(client, ws["workspace_id"])
    actual = ep["effective_permissions"]["filesystem"]
    assert actual == "banned", (
        f"ceiling filesystem='banned' NOT applied without session; "
        f"actual filesystem={actual!r}; full response: {ep}"
    )


# ---------------------------------------------------------------------------
# [b] REST effective_permissions with a raw session grant must be capped
# ---------------------------------------------------------------------------

def test_effective_permissions_caps_session_grant_by_ceiling(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="pr_b")
    harness.put_permissions(client, ws["workspace_id"], {"filesystem": "banned"})
    sid = harness.create_session(client, workspace_path=ws["root"])

    _inject_raw_session_permissions(sid, ws["workspace_id"])

    ep = harness.get_effective_permissions(
        client, ws["workspace_id"], session_id=sid
    )
    actual = ep["effective_permissions"]["filesystem"]
    assert actual == "banned", (
        f"ceiling filesystem='banned' NOT applied to raw write grant; "
        f"actual filesystem={actual!r}; full response: {ep}"
    )


# ---------------------------------------------------------------------------
# [c] Restart: the persisted ceiling must still be enforced by REST
# ---------------------------------------------------------------------------

def test_effective_permissions_capped_after_restart(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="pr_c")
    harness.put_permissions(client, ws["workspace_id"], {"filesystem": "banned"})

    client2 = _restart(tmp_home, stop_fn)
    ep = harness.get_effective_permissions(client2, ws["workspace_id"])
    actual = ep["effective_permissions"]["filesystem"]
    assert actual == "banned", (
        f"persisted ceiling filesystem='banned' NOT applied after restart; "
        f"actual filesystem={actual!r}; full response: {ep}"
    )


# ---------------------------------------------------------------------------
# [e] WS load_session (first connection and same-process reconnect)
# ---------------------------------------------------------------------------

def test_same_process_ws_reconnect_live_state_is_ceiled(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="pr_e")
    harness.put_permissions(
        client, ws["workspace_id"], {"filesystem": "banned", "git": "read"}
    )
    sid = harness.create_session(client, workspace_path=ws["root"])

    with harness.ws_connect(client) as wsock:
        evt1 = _load_session(wsock, sid)
        wsock.send_json(
            {
                "command": "apply_config",
                "config": _apply_config_dict(
                    ws["root"], {"filesystem": "write", "git": "write"}
                ),
            }
        )
        cevt, _ = harness.receive_until_type(wsock, "config_changed")

    # Same backend instance, same client, same session: second websocket.
    with harness.ws_connect(client) as wsock2:
        evt2 = _load_session(wsock2, sid)

    perms1 = evt1["config"]["session_permissions"]
    perms2 = evt2["config"]["session_permissions"]
    actual = perms2.get("filesystem")
    assert actual == "banned", (
        f"reconnect session_permissions NOT ceiled (filesystem='banned' "
        f"expected); actual filesystem={actual!r}; perms2={perms2}; "
        f"first-connection filesystem={perms1.get('filesystem')!r}; "
        f"config_changed perms={cevt.get('permissions')!r}; evt2={evt2}"
    )


# ---------------------------------------------------------------------------
# [d] Subprocess: ceiling persists across a real process boundary
# ---------------------------------------------------------------------------

def test_workspace_permission_persists_across_full_process_restart():
    tmp_home = tempfile.mkdtemp(prefix="pr_d_")
    ws_id_file = os.path.join(tmp_home, "pr_d_ws_id.txt")
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    child_script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from harness import start_backend, make_client, create_workspace, "
        "put_permissions, register_mock_provider\n"
        "app, tmp_home, stop = start_backend(tmp_home=%r)\n"
        "try:\n"
        "    register_mock_provider()\n"
        "    client = make_client(app)\n"
        "    ws = create_workspace(client, tmp_home, name=\"pr_d\")\n"
        "    put_permissions(client, ws[\"workspace_id\"], "
        "{\"filesystem\": \"banned\"})\n"
        "    with open(%r, \"w\") as fh:\n"
        "        fh.write(ws[\"workspace_id\"])\n"
        "finally:\n"
        "    stop(rmtree=False)\n"
    ) % (os.path.join(repo_root, "tests", "integration"), tmp_home, ws_id_file)

    proc = subprocess.run(
        [sys.executable, "-c", child_script],
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONPATH": repo_root + os.pathsep + os.environ.get("PYTHONPATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(
            "child process failed:\n--- stdout ---\n"
            f"{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )

    app, _tmp_home2, stop_fn = harness.start_backend(tmp_home=tmp_home)
    try:
        harness.register_mock_provider()
        client = harness.make_client(app)
        with open(ws_id_file) as fh:
            ws_id = fh.read().strip()
        assert ws_id, "child did not write a workspace id"

        s = harness.get_summary(client, ws_id)
        disk_actual = s["permissions"]["filesystem"]
        assert disk_actual == "banned", (
            f"persisted ceiling lost on disk after process restart; "
            f"actual filesystem={disk_actual!r}; summary={s}"
        )

        ep = harness.get_effective_permissions(client, ws_id)
        actual = ep["effective_permissions"]["filesystem"]
        assert actual == "banned", (
            f"persisted ceiling filesystem='banned' NOT applied to REST "
            f"effective_permissions after process restart; "
            f"actual filesystem={actual!r}; full response: {ep}"
        )
    finally:
        stop_fn()
