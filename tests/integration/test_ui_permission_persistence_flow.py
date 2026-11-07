"""UI permission persistence flow - REST-level mirror of the e2e scenario.

These tests replicate, at the REST level, the exact sequence the browser
e2e test performs (apply ``filesystem: banned`` in the Permissions &
Resources tab, restart the backend, reload), so the persistence contract is
covered even when Playwright is not installed:

1. ``test_ui_flow_apply_restart_reload_persists`` - apply a permission via
   the same REST endpoint the UI uses (``PUT /api/workspace/{id}/permissions``
   with a payload shaped like the UI's ``handleApply``: the full permission
   map from the summary with one value changed), then simulate a process
   restart and assert the value survives on every read surface (summary,
   saved permissions map, effective-permissions ceiling).
2. ``test_session_grants_capped_after_restart`` - after a restart, a raw
   session write grant must still be capped by the on-disk ceiling.
"""
import importlib
import os
import sys

import pytest

try:
    from tests.integration import harness
except ImportError:  # non-package layout: tests/integration on sys.path
    import harness

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env():
    app, tmp_home, stop_fn = harness.start_backend()
    _restore_bootstrap_module()
    harness.register_mock_provider()
    client = harness.make_client(app)
    yield client, tmp_home, stop_fn
    stop_fn()


def _restore_bootstrap_module():
    """Re-seed ``thoughtmachine.bootstrap`` after harness purges it.

    ``harness._purge_modules()`` deletes ``thoughtmachine.bootstrap`` from
    ``sys.modules`` for a cold backend import, but the parent package keeps a
    stale ``bootstrap`` attribute.  ``TestVaultBootstrap`` then binds that
    stale object via ``from thoughtmachine import bootstrap`` and its
    ``importlib.reload()`` raises ``ImportError: module thoughtmachine.bootstrap
    not in sys.modules``.  Re-importing the module here (when absent) keeps the
    ``sys.modules`` entry alive so the vault-bootstrap suite still reloads.
    """
    if "thoughtmachine.bootstrap" not in sys.modules:
        importlib.import_module("thoughtmachine.bootstrap")


def _restart(tmp_home, stop_fn):
    """Keep the fake HOME, purge modules, import a fresh backend."""
    stop_fn(rmtree=False, restore_env=False)
    app2 = harness.restart(tmp_home)
    _restore_bootstrap_module()
    harness.register_mock_provider()
    return harness.make_client(app2)


def _create_research_workspace(client, tmp_home, name):
    """POST /api/workspace exactly like the e2e fixture does (research)."""
    root = os.path.join(tmp_home, name)
    os.makedirs(root, exist_ok=True)
    resp = client.post("/api/workspace", json={"path": root, "purpose": "research"})
    assert resp.status_code in (200, 201), (
        f"create workspace failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


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


def test_ui_flow_apply_restart_reload_persists(env):
    client, tmp_home, stop_fn = env
    ws = _create_research_workspace(client, tmp_home, name="ui_flow_ws")

    summary = harness.get_summary(client, ws["workspace_id"])
    assert isinstance(summary["permissions"], dict) and summary["permissions"], (
        summary
    )
    assert summary["permissions"]["filesystem"] == "read", summary["permissions"]

    # Mirror the UI handleApply payload: full map from the summary, one change.
    payload = dict(summary["permissions"])
    payload["filesystem"] = "banned"
    put = harness.put_permissions(client, ws["workspace_id"], payload)
    assert put["permissions"]["filesystem"] == "banned", put
    # Other permission keys are preserved (merged, not replaced).
    for key, value in payload.items():
        if key != "filesystem":
            assert put["permissions"].get(key) == value, (key, put["permissions"])

    summary2 = harness.get_summary(client, ws["workspace_id"])
    assert summary2["permissions"]["filesystem"] == "banned", summary2["permissions"]

    # Simulate the browser reload after a backend restart.
    client2 = _restart(tmp_home, stop_fn)
    summary3 = harness.get_summary(client2, ws["workspace_id"])
    assert summary3["permissions"]["filesystem"] == "banned", summary3["permissions"]

    saved = client2.get(f"/api/workspace/{ws['workspace_id']}/permissions")
    assert saved.status_code == 200, saved.text
    saved_map = saved.json()["permissions"]
    assert saved_map["filesystem"] == "banned", saved_map

    ep = harness.get_effective_permissions(client2, ws["workspace_id"])
    assert ep["effective_permissions"]["filesystem"] == "banned", ep


def test_session_grants_capped_after_restart(env):
    client, tmp_home, stop_fn = env
    ws = _create_research_workspace(client, tmp_home, name="ui_ceiling_ws")
    harness.put_permissions(client, ws["workspace_id"], {"filesystem": "banned"})

    client2 = _restart(tmp_home, stop_fn)
    sid = harness.create_session(client2, workspace_path=ws["root"])
    _inject_raw_session_permissions(sid, ws["workspace_id"])

    ep = harness.get_effective_permissions(client2, ws["workspace_id"], session_id=sid)
    actual = ep["effective_permissions"]["filesystem"]
    assert actual == "banned", (
        f"on-disk ceiling filesystem='banned' NOT applied to raw write grant "
        f"after restart; actual filesystem={actual!r}; full response: {ep}"
    )
