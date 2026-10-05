"""Seeding-path invariants (r2b / A6).

Pins the ABSENT-ONLY permission-sidecar seed on every session-creation /
workspace-switch path that binds a session to a workspace.  The sidecar
(``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json``) is the SOLE
grants source; without it a disk-mode permission read fails CLOSED on a
*missing* file.  Each such path must therefore seed an EMPTY sidecar so the
gate resolves to fail-closed DEFAULTS capped by the workspace ceiling — and the
seed must be ABSENT-ONLY so re-selecting a workspace that already holds this
session's grants never clobbers them.

Paths pinned
------------
1. REST  POST /api/session/create {workspace_path}   → sidecar present-empty
2. WS    {"command":"set_project"}                    → sidecar present-empty
3. WS    {"command":"apply_config"} workspace-switch → sidecar present-empty
4. permission_store.seed_session_permissions_if_absent (shared primitive) →
   absent ⇒ writes {} and returns True; present ⇒ returns False, value intact.

Hermetic: temp HOME + patched ``Path.home`` + purged/re-imported server modules
(the fixture SNAPSHOTS and restores the exact pre-harness ``sys.modules``
prefix entries and ``sys.path``, so no module object leaks to later tests).
No network, no LLM, no Docker daemon.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import sys as sys_mod
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.integration


# ══════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def seed_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server.

    Module-scoped so every pinned path shares ONE hermetic vault/store (the
    workspace registry and session store singletons must stay coherent for the
    WS workspace-switch path).  Yields ``(app, vault_path)``.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_seed_invariants_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    # The vault resolves through Path.home(); pin it to the temp HOME.  (The
    # root conftest already redirects HOME/Path.home globally; this narrows it
    # to OUR tmp dir and keeps vault_root() == tmp_home/.thoughtmachine.)
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()
    os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)

    mod_prefixes = (
        "web_ui.backend",
        "agent.config.provider_profile",
        "thoughtmachine.bootstrap",
        "session",
    )

    # Exact save/restore: snapshot EVERY module object under our prefixes plus
    # the interpreter's sys.path BEFORE the harness mutates interpreter state,
    # so teardown can put the process back byte-for-byte (no half-restored
    # module objects left for later tests / other importers to trip over).
    saved_modules = {
        name: mod
        for name, mod in sys_mod.modules.items()
        if any(name.startswith(p) for p in mod_prefixes)
    }
    saved_sys_path = list(sys_mod.path)

    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    vault_path = fake_home_path / ".thoughtmachine"

    yield app, vault_path

    # ── Exact restoration ────────────────────────────────────────────────
    # 1. Drop every prefixes-module the harness (or its imports) added, so no
    #    freshly-imported module object leaks past the fixture.
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]
    # 2. Re-insert the EXACT module objects that were live before the harness,
    #    giving later importers the same identities they saw before.
    sys_mod.modules.update(saved_modules)
    # 3. Restore sys.path verbatim.
    sys_mod.path[:] = saved_sys_path

    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val
    shutil.rmtree(tmp_home, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sidecar_path(vault, workspace_id, session_id):
    from thoughtmachine.permission_store import session_grants_path
    return session_grants_path(vault, workspace_id, session_id)


def _read_grants(vault, workspace_id, session_id):
    from thoughtmachine.permission_store import read_session_permissions
    return read_session_permissions(vault, workspace_id, session_id)


def _recv_until_type(ws, target, timeout_hint="", max_events=40):
    """Receive WS events until ``target`` type arrives; fail if exhausted."""
    seen = []
    for _ in range(max_events):
        evt = ws.receive_json()
        seen.append(evt.get("type"))
        if evt.get("type") == target:
            return evt
    raise AssertionError(
        f"never received {target!r} {timeout_hint}; saw {seen}"
    )


def _recv_collect_until_type(ws, target, timeout_hint="", max_events=40):
    """Like ``_recv_until_type`` but also RECORDS every frame received.

    The command loop processes commands in order, so once ``target`` arrives
    every frame emitted by the earlier command has already been read.  Returns
    ``(target_event, all_events)``; ``all_events`` is the full ordered list
    (target event last), letting a caller inspect outbound frames the plain
    helper would have discarded.
    """
    events = []
    for _ in range(max_events):
        evt = ws.receive_json()
        events.append(evt)
        if evt.get("type") == target:
            return evt, events
    raise AssertionError(
        f"never received {target!r} {timeout_hint}; saw "
        f"{[e.get('type') for e in events]}"
    )


def _new_project(tmp_home, name):
    d = Path(tempfile.mkdtemp(prefix=f"seed_{name}_"))
    return str(d)


# ══════════════════════════════════════════════════════════════════════════════
# Path 1 — REST POST /api/session/create {workspace_path}
# ══════════════════════════════════════════════════════════════════════════════

def test_rest_create_with_workspace_path_seeds_empty_sidecar(seed_server):
    app, vault = seed_server
    proj = _new_project(vault, "rest")

    with TestClient(app) as client:
        resp = client.post(
            "/api/session/create",
            json={"mode": "custom", "workspace_path": proj},
        )
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        body = resp.json()

    sid = body["session_id"]
    wsid = body.get("workspace_id")
    assert wsid, f"REST create must bind a workspace_id, got {body!r}"

    path = _sidecar_path(vault, wsid, sid)
    assert path.exists(), (
        f"REST create did not seed a permission sidecar at {path}"
    )
    assert _read_grants(vault, wsid, sid) == {}, (
        "seeded sidecar must be present-and-EMPTY (fail-closed defaults)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Path 2 — WS set_project
# ══════════════════════════════════════════════════════════════════════════════

def test_ws_set_project_seeds_empty_sidecar(seed_server):
    app, vault = seed_server
    proj = _new_project(vault, "setproj")

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "set_project", "project": proj})
            evt = _recv_until_type(ws, "session_loaded", "(set_project)")

    sid = evt["session_id"]
    wsid = evt["workspace_id"]
    assert wsid, f"set_project session_loaded must carry workspace_id: {evt!r}"

    path = _sidecar_path(vault, wsid, sid)
    assert path.exists(), (
        f"set_project did not seed a permission sidecar at {path}"
    )
    assert _read_grants(vault, wsid, sid) == {}


# ══════════════════════════════════════════════════════════════════════════════
# Path 3 — WS apply_config workspace-switch
# ══════════════════════════════════════════════════════════════════════════════

def test_ws_apply_config_workspace_switch_seeds_empty_sidecar(seed_server):
    app, vault = seed_server
    proj_a = _new_project(vault, "a")
    proj_b = _new_project(vault, "b")

    with TestClient(app) as client:
        # Bind a session to workspace A.
        r = client.post("/api/session/create", json={"mode": "custom", "workspace_path": proj_a})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        # Pre-register workspace B so the switch resolves it from the registry.
        r = client.post("/api/session/create", json={"mode": "custom", "workspace_path": proj_b})
        assert r.status_code == 200, r.text
        ws_b = r.json()["workspace_id"]
        assert ws_b

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": sid})
            _recv_until_type(ws, "session_loaded", "(load session A)")

            # Switching the empty session's workspace A → B triggers the
            # workspace-switch branch (workspace_changed=True).  The session had
            # no conversation, so the SAME session id is re-bound to B and the
            # seed runs for (ws_b, sid).
            ws.send_json({
                "command": "apply_config",
                "config": {"mode": "custom", "workspace_path": proj_b},
            })
            # A follow-up command fences the switch: commands are processed in
            # order, so its reply proves the apply_config handler completed.
            # NOTE: the get_open_sessions reply type is ``open_sessions`` (server.py
            # emits {"type": "open_sessions", ...}); the module docstring's
            # ``open_sessions_list`` is stale, and waiting on it blocks forever.
            ws.send_json({"command": "get_open_sessions"})
            _recv_until_type(ws, "open_sessions", "(fence after apply_config)")

    path = _sidecar_path(vault, ws_b, sid)
    assert path.exists(), (
        f"apply_config workspace-switch did not seed a sidecar at {path}"
    )
    assert _read_grants(vault, ws_b, sid) == {}


def test_ws_apply_config_workspace_switch_sends_permission_reset_notice(seed_server):
    """The apply_config workspace-switch must emit the operator-facing
    permission-context RESET notice on the existing ``status_message``
    channel (the same frame the switch already sends): ``permissions_reset``
    is True, ``permission_context`` == "defaults_capped_by_ceiling", a
    non-empty ``workspace_id``, and ``text`` NAMING the new workspace and
    stating that the permission context reset to defaults / is capped by the
    workspace's ceiling.  This pins the signal that a switch dropped the live
    permission context to fail-closed DEFAULTS capped by the new ceiling (the
    sidecar-seed invariant above covers the on-disk half of the same switch).
    """
    app, vault = seed_server
    proj_a = _new_project(vault, "notice_a")
    proj_b = _new_project(vault, "notice_b")

    with TestClient(app) as client:
        # Bind a session to workspace A.
        r = client.post("/api/session/create", json={"mode": "custom", "workspace_path": proj_a})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        # Pre-register workspace B so the switch resolves it from the registry.
        r = client.post("/api/session/create", json={"mode": "custom", "workspace_path": proj_b})
        assert r.status_code == 200, r.text
        ws_b = r.json()["workspace_id"]
        assert ws_b

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": sid})
            _recv_until_type(ws, "session_loaded", "(load session A)")

            ws.send_json({
                "command": "apply_config",
                "config": {"mode": "custom", "workspace_path": proj_b},
            })
            # Fence the switch AND record every frame it emitted: commands are
            # processed in order, so the open_sessions reply proves the
            # apply_config handler ran to completion (and the notice landed).
            ws.send_json({"command": "get_open_sessions"})
            _fence, events = _recv_collect_until_type(
                ws, "open_sessions", "(fence after apply_config)",
            )

    notices = [
        e for e in events
        if e.get("type") == "status_message" and e.get("permissions_reset") is True
    ]
    assert notices, (
        "apply_config workspace-switch emitted no permission-reset "
        f"status_message; saw {[e.get('type') for e in events]}"
    )
    notice = notices[-1]

    assert notice["permission_context"] == "defaults_capped_by_ceiling", (
        f"switch notice carried wrong permission_context: {notice!r}"
    )
    assert notice.get("workspace_id"), (
        f"switch notice must carry a non-empty workspace_id: {notice!r}"
    )

    text = notice["text"]
    assert ws_b in text, (
        f"switch notice text must name the new workspace {ws_b!r}: {text!r}"
    )
    low = text.lower()
    for token in ("reset", "defaults", "ceiling"):
        assert token in low, (
            f"switch notice text must state permission context {token!r}: {text!r}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Path 4 — shared primitive: seed_session_permissions_if_absent
# ══════════════════════════════════════════════════════════════════════════════

def test_seed_primitive_is_absent_only(seed_server):
    """absent ⇒ write {} & True; present ⇒ NO-OP & False; absent read raises."""
    _app, vault = seed_server
    from thoughtmachine.permission_store import (
        PermissionStoreError,
        read_session_permissions,
        seed_session_permissions_if_absent,
        session_grants_path,
        write_session_permissions,
    )

    # (a) ABSENT pair ⇒ seed writes {} and reports True.
    ws_new, sid_new = "seed_ws_absent", "seed_sid_absent"
    with pytest.raises(PermissionStoreError):
        # sanity: before seeding, a missing sidecar is an error (fail-closed).
        read_session_permissions(vault, ws_new, sid_new)

    assert seed_session_permissions_if_absent(vault, ws_new, sid_new, {}) is True
    assert session_grants_path(vault, ws_new, sid_new).exists()
    assert read_session_permissions(vault, ws_new, sid_new) == {}

    # (b) PRESENT pair with real grants ⇒ seed is a NO-OP (returns False) and
    #     the existing grants survive verbatim.
    ws_pres, sid_pres = "seed_ws_present", "seed_sid_present"
    write_session_permissions(vault, ws_pres, sid_pres, {"git": "write"})
    before = read_session_permissions(vault, ws_pres, sid_pres)
    assert before, "precondition: real grants must be non-empty after write"

    assert seed_session_permissions_if_absent(vault, ws_pres, sid_pres, {}) is False
    assert read_session_permissions(vault, ws_pres, sid_pres) == before, (
        "absent-only violation: an existing sidecar was clobbered by the seed"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Transport round-trip — WS reset notice ⇄ REST HTTP read ⇄ refresh re-read
# ══════════════════════════════════════════════════════════════════════════════

def test_transport_roundtrip_ws_notice_rest_read_and_refresh_agree(seed_server):
    """After a workspace switch the post-switch permission-context state must
    AGREE across the three transports:

      1. WS   — the ``status_message`` permission-reset notice the switch emits;
      2. REST — the HTTP read surface (``GET /api/session/{id}`` for the bound
                workspace + ``GET /api/session/{id}/permissions`` for raw/effective);
      3. REFRESH — a re-read over BOTH transports (REST re-GET + WS ``get_config``
                projection) after the switch has settled.

    Every transport must name the SAME post-switch workspace and report the SAME
    reset permission context: RAW == {} (fail-closed defaults) with a STABLE
    effective (ceiled) profile, and the WS refresh projection must equal the REST
    effective profile (both are computed by the same disk-authoritative gate).
    """
    app, vault = seed_server
    proj_a = _new_project(vault, "rt_a")
    proj_b = _new_project(vault, "rt_b")

    with TestClient(app) as client:
        # Bind a session to workspace A (empty session → the switch keeps the id).
        r = client.post(
            "/api/session/create",
            json={"mode": "custom", "workspace_path": proj_a},
        )
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]

        # Pre-register workspace B so the switch resolves it from the registry.
        r = client.post(
            "/api/session/create",
            json={"mode": "custom", "workspace_path": proj_b},
        )
        assert r.status_code == 200, r.text
        ws_b = r.json()["workspace_id"]
        assert ws_b

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": sid})
            _recv_until_type(ws, "session_loaded", "(load session A)")

            # Workspace switch A → B on the empty session (same id re-bound).
            ws.send_json({
                "command": "apply_config",
                "config": {"mode": "custom", "workspace_path": proj_b},
            })
            # Fence the switch AND record every frame it emitted.
            ws.send_json({"command": "get_open_sessions"})
            _fence, switch_events = _recv_collect_until_type(
                ws, "open_sessions", "(fence after apply_config)",
            )

            # REFRESH re-read over WS: get_config re-projects the permission
            # context through the disk-authoritative gate.
            ws.send_json({"command": "get_config"})
            refresh_evt = _recv_until_type(
                ws, "config_changed", "(refresh get_config)",
            )

        # ── Surface 1 — WS permission-reset notice ──────────────────────────
        notices = [
            e for e in switch_events
            if e.get("type") == "status_message"
            and e.get("permissions_reset") is True
        ]
        assert notices, (
            "workspace switch emitted no permission-reset status_message; saw "
            f"{[e.get('type') for e in switch_events]}"
        )
        notice = notices[-1]
        assert notice["permission_context"] == "defaults_capped_by_ceiling", (
            f"WS notice carried wrong permission_context: {notice!r}"
        )
        notice_ws = notice["workspace_id"]
        assert notice_ws == ws_b, (
            f"WS notice workspace_id {notice_ws!r} != switched workspace {ws_b!r}"
        )

        # ── Surface 2 — REST HTTP read + Surface 3 — refresh re-read ────────
        with TestClient(app) as client:
            rest_detail_resp = client.get(f"/api/session/{sid}")
            assert rest_detail_resp.status_code == 200, rest_detail_resp.text
            rest_detail = rest_detail_resp.json()

            rest_perms_resp = client.get(f"/api/session/{sid}/permissions")
            assert rest_perms_resp.status_code == 200, rest_perms_resp.text
            rest_body = rest_perms_resp.json()

            # REFRESH re-read (REST side).
            refresh_detail_resp = client.get(f"/api/session/{sid}")
            assert refresh_detail_resp.status_code == 200, refresh_detail_resp.text
            refresh_detail = refresh_detail_resp.json()

            refresh_perms_resp = client.get(f"/api/session/{sid}/permissions")
            assert refresh_perms_resp.status_code == 200, refresh_perms_resp.text
            refresh_body = refresh_perms_resp.json()

    # AGREE — every transport names the SAME post-switch workspace.
    assert notice_ws == ws_b
    assert rest_detail["workspace_id"] == ws_b, (
        f"REST read workspace {rest_detail['workspace_id']!r} != notice {ws_b!r}"
    )
    assert refresh_detail["workspace_id"] == ws_b, (
        f"refresh workspace {refresh_detail['workspace_id']!r} != notice {ws_b!r}"
    )

    # AGREE — the post-switch permission context is fail-closed DEFAULTS
    # (raw == {}) on every REST read, and the effective profile is STABLE.
    assert rest_body["raw"] == {}, (
        f"post-switch raw permission grants must be defaults: {rest_body['raw']!r}"
    )
    assert refresh_body["raw"] == {}, refresh_body["raw"]
    assert rest_body["effective"] == refresh_body["effective"], (
        "effective permissions drifted between the REST read and the refresh "
        f"re-read: {rest_body['effective']!r} != {refresh_body['effective']!r}"
    )

    # AGREE across TRANSPORTS — the WS refresh projection equals the REST
    # effective profile (same disk-authoritative gate computation).
    ws_refresh_perms = refresh_evt.get("permissions")
    assert ws_refresh_perms, (
        f"WS refresh projection must be non-empty: {refresh_evt!r}"
    )
    assert ws_refresh_perms == rest_body["effective"], (
        "WS config_changed.permissions disagrees with the REST effective "
        f"profile: ws={ws_refresh_perms!r} rest={rest_body['effective']!r}"
    )

