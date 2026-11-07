"""WS ``get_open_sessions`` must report each open session's owning workspace.

End-to-end companion to ``tests/test_open_sessions_workspace_id.py``: that file
pins the store-level ``workspace_id`` contract, this one pins the **wire
payload** the browser actually receives.

Why it matters: ``open_sessions.json`` is a GLOBAL state file — it lists session
ids from every workspace the user has ever opened.  When the ``open_sessions``
frame carried no ``workspace_id``, the frontend had to guess which workspace each
id belonged to, and its only guesses were "the workspace I last saw" / "the
workspace on screen".  Switching workspaces therefore re-labelled every open tab
and the strip filled up with 50 tabs from unrelated workspaces.

Hermetic: temp HOME + patched ``Path.home`` + purged/re-imported server modules
(the harness snapshots and restores ``sys.modules`` / ``sys.path``).  No
network, no LLM, no Docker daemon.
"""

from __future__ import annotations

import importlib
import json
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
# Hermetic full-server harness (same shape as test_seeding_path_invariants.py)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def open_sessions_server():
    """Temp HOME + purged modules + fresh import of ``web_ui.backend.server``.

    Yields ``(app, vault_path, server_module)`` so a test can seed session files
    through the very store instance the websocket handler will use.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_open_sessions_ws_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()
    os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)

    mod_prefixes = (
        "web_ui.backend",
        "agent.config.provider_profile",
        "thoughtmachine.bootstrap",
        "session",
    )

    from tests.integration.harness import purged_sys_modules

    saved_sys_path = list(sys_mod.path)

    try:
        with purged_sys_modules(mod_prefixes):
            server_mod = importlib.import_module("web_ui.backend.server")
            app = server_mod.app
            vault_path = fake_home_path / ".thoughtmachine"
            (vault_path / "workspaces").mkdir(parents=True, exist_ok=True)
            (vault_path / "state").mkdir(parents=True, exist_ok=True)

            yield app, vault_path, server_mod

    finally:
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


def _write_session_file(vault: Path, workspace_id: str, session_id: str, name: str) -> None:
    """Write a saved session into the given workspace's scoped directory."""
    sessions_dir = vault / "workspaces" / workspace_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "metadata": {"name": name},
        "updated_at": "2024-01-01T00:00:00",
        "user_history": [{"role": "user", "content": f"hello from {name}"}],
        "session_size_bytes": 456,
    }
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _recv_until_type(ws, target, timeout_hint="", max_events=40):
    """Receive WS frames until ``target`` arrives; fail if the stream runs dry."""
    seen = []
    for _ in range(max_events):
        evt = ws.receive_json()
        seen.append(evt.get("type"))
        if evt.get("type") == target:
            return evt
    raise AssertionError(f"never received {target!r} {timeout_hint}; saw {seen}")


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════


def test_open_sessions_payload_carries_workspace_id_per_session(open_sessions_server):
    """Every ``open_sessions`` entry names the workspace its session lives in."""
    app, vault, server_mod = open_sessions_server
    store = server_mod._get_session_store()

    # Two saved sessions in two DIFFERENT workspaces, both marked open — the
    # exact state the bug produced "50 tabs in one strip" from.
    _write_session_file(vault, "ws-alpha", "alpha-1", "Alpha one")
    _write_session_file(vault, "ws-beta", "beta-1", "Beta one")
    store.add_open_session("alpha-1")
    store.add_open_session("beta-1")

    project = tempfile.mkdtemp(prefix="open_sessions_proj_")
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                # Bind the socket so the command loop will answer us.
                ws.send_json({"command": "set_project", "project": project})
                _recv_until_type(ws, "session_loaded", "(set_project)")

                ws.send_json({"command": "get_open_sessions"})
                reply = _recv_until_type(ws, "open_sessions", "(get_open_sessions)")
    finally:
        shutil.rmtree(project, ignore_errors=True)

    sessions = reply["sessions"]
    by_id = {entry["session_id"]: entry for entry in sessions}

    assert "alpha-1" in by_id, f"open session alpha-1 missing from reply: {sessions!r}"
    assert "beta-1" in by_id, f"open session beta-1 missing from reply: {sessions!r}"

    # The whole point: each entry states its OWN workspace, so the UI never has
    # to guess from "which workspace am I looking at".
    assert by_id["alpha-1"]["workspace_id"] == "ws-alpha"
    assert by_id["beta-1"]["workspace_id"] == "ws-beta"

    # Additive: the fields the UI already relied on are still there, and every
    # entry carries the key (never absent) so consumers can index it blindly.
    for sid, entry in by_id.items():
        assert "workspace_id" in entry, f"{sid} entry lacks workspace_id: {entry!r}"
        assert entry["name"], f"{sid} entry lost its name: {entry!r}"
        assert "updated_at" in entry
        assert "message_count" in entry


def test_open_sessions_reports_unknown_workspace_as_empty_string(open_sessions_server):
    """A legacy flat session reports ``""`` — the UI fails closed on it.

    Legacy sessions live in the un-scoped ``sessions_dir`` and genuinely have no
    owning workspace.  The wire contract is an empty string (not a guess), which
    the frontend treats as "unknown, do not bucket".
    """
    app, vault, server_mod = open_sessions_server
    store = server_mod._get_session_store()

    legacy_dir = vault / "sessions"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "legacy-1.json").write_text(
        json.dumps(
            {
                "session_id": "legacy-1",
                "metadata": {"name": "Legacy one"},
                "updated_at": "2024-01-01T00:00:00",
                "user_history": [{"role": "user", "content": "old session"}],
            }
        ),
        encoding="utf-8",
    )
    store.add_open_session("legacy-1")

    project = tempfile.mkdtemp(prefix="open_sessions_proj_legacy_")
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"command": "set_project", "project": project})
                _recv_until_type(ws, "session_loaded", "(set_project)")

                ws.send_json({"command": "get_open_sessions"})
                reply = _recv_until_type(ws, "open_sessions", "(get_open_sessions)")
    finally:
        shutil.rmtree(project, ignore_errors=True)

    by_id = {entry["session_id"]: entry for entry in reply["sessions"]}
    assert "legacy-1" in by_id, f"legacy session missing from reply: {reply!r}"
    assert by_id["legacy-1"]["workspace_id"] == ""
