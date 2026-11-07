"""``open_sessions`` must carry each session's owning workspace.

Regression tests for the "50 session tabs from every workspace crammed into one
strip" bug.

``open_sessions.json`` is GLOBAL: the state file lists session ids from every
workspace the user has ever opened.  The websocket ``get_open_sessions`` reply
is built from that list, so each entry HAS to tell the UI which workspace its
session belongs to.  Before this contract existed the reply carried only
``session_id``/``name``/``updated_at``/``message_count``, and the frontend had
to guess — its only guesses were "the workspace I last saw" and "the workspace
on screen", which mis-bucketed every foreign open tab into whichever workspace
happened to be displayed.

Contract pinned here
--------------------
1. ``load_sessions_metadata_batch`` reports ``workspace_id`` for sessions that
   live in ``<vault>/workspaces/<ws>/sessions/``.
2. Sessions still in the legacy flat ``sessions_dir`` report
   ``workspace_id`` = ``None`` — the UI fails closed (drops the tab) instead of
   guessing a workspace for them.
3. A workspace-scoped call reports exactly the requested workspace, and never
   resolves a session that lives in a *different* workspace.
4. Every metadata dict carries the ``workspace_id`` key, present or ``None``,
   so consumers can index it unconditionally.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session.store as store_mod  # noqa: E402  (path setup must run first)
from session.store import FileSystemSessionStore  # noqa: E402

WS_ALPHA = "ws-alpha"
WS_BETA = "ws-beta"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_session(
    path: Path,
    session_id: str,
    name: str,
    *,
    messages: int = 1,
) -> None:
    """Write a session JSON file at ``path`` with the given identity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    user_history = [{"role": "user", "content": f"question {i}"} for i in range(messages)]
    payload = {
        "session_id": session_id,
        "metadata": {"name": name},
        "updated_at": "2024-01-01T00:00:00",
        "user_history": user_history,
        "session_size_bytes": 123,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Store whose workspace root is ``tmp_path`` (dirs under ``tmp_path/workspaces``)."""
    # ``session.store`` imports ``vault_root`` by name, so patching the module
    # attribute is what actually redirects ``_base_dir``.
    monkeypatch.setattr(store_mod, "vault_root", lambda: tmp_path)
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
    )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_batch_metadata_reports_owning_workspace(store, tmp_path):
    """Each workspace-scoped session reports the workspace it was found in."""
    _write_session(
        tmp_path / "workspaces" / WS_ALPHA / "sessions" / "alpha-1.json",
        "alpha-1",
        "Alpha one",
    )
    _write_session(
        tmp_path / "workspaces" / WS_BETA / "sessions" / "beta-1.json",
        "beta-1",
        "Beta one",
        messages=2,
    )
    _write_session(tmp_path / "sessions" / "legacy-1.json", "legacy-1", "Legacy one")

    metas = store.load_sessions_metadata_batch(["alpha-1", "beta-1", "legacy-1"])

    assert metas["alpha-1"]["workspace_id"] == WS_ALPHA
    assert metas["beta-1"]["workspace_id"] == WS_BETA

    # Legacy flat sessions have no owning workspace.  ``None`` (not a guess,
    # not the workspace on screen) is what lets the UI fail closed.
    assert metas["legacy-1"]["workspace_id"] is None

    # The pre-existing fields are unchanged — the new key is purely additive.
    assert metas["beta-1"]["name"] == "Beta one"
    assert metas["beta-1"]["message_count"] == 2
    assert metas["beta-1"]["session_id"] == "beta-1"
    assert metas["beta-1"]["session_size_bytes"] == 123


def test_every_metadata_dict_carries_the_key(store, tmp_path):
    """``workspace_id`` is always present, so consumers can index it blindly."""
    _write_session(
        tmp_path / "workspaces" / WS_ALPHA / "sessions" / "alpha-1.json",
        "alpha-1",
        "Alpha one",
    )
    _write_session(tmp_path / "sessions" / "legacy-1.json", "legacy-1", "Legacy one")

    metas = store.load_sessions_metadata_batch(["alpha-1", "legacy-1", "missing-1"])

    assert "workspace_id" in metas["alpha-1"]
    assert "workspace_id" in metas["legacy-1"]
    # Unknown ids stay ``None`` entries — no fabricated metadata.
    assert metas["missing-1"] is None


def test_scoped_call_reports_requested_workspace_and_never_crosses(store, tmp_path):
    """A scoped lookup reports its own workspace and cannot see other ones."""
    _write_session(
        tmp_path / "workspaces" / WS_ALPHA / "sessions" / "alpha-1.json",
        "alpha-1",
        "Alpha one",
    )
    _write_session(
        tmp_path / "workspaces" / WS_BETA / "sessions" / "beta-1.json",
        "beta-1",
        "Beta one",
    )

    scoped = store.load_sessions_metadata_batch(["alpha-1", "beta-1"], workspace_id=WS_ALPHA)

    assert scoped["alpha-1"]["workspace_id"] == WS_ALPHA
    assert scoped["beta-1"] is None, "a scoped lookup must not resolve a foreign workspace"


def test_metadata_sidecar_files_are_skipped(store, tmp_path):
    """``_meta_*.json`` sidecars never masquerade as sessions."""
    sessions_dir = tmp_path / "workspaces" / WS_ALPHA / "sessions"
    _write_session(sessions_dir / "alpha-1.json", "alpha-1", "Alpha one")
    (sessions_dir / "_meta_alpha-1.json").write_text(
        json.dumps({"session_id": "alpha-1", "name": "sidecar"}), encoding="utf-8"
    )

    metas = store.load_sessions_metadata_batch(["alpha-1"])

    assert metas["alpha-1"]["name"] == "Alpha one"
    assert metas["alpha-1"]["workspace_id"] == WS_ALPHA


def test_empty_request_is_a_no_op(store):
    """An empty open-sessions list costs nothing and returns an empty map."""
    assert store.load_sessions_metadata_batch([]) == {}
