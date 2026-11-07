"""
Regression tests: workspace ``config.json`` must carry a top-level ``root``.

``root`` is the host directory the workspace owns.  Host execution reads it
live from ``config.json`` and refuses to run without one, so registration must
persist it.  The registry (``state/workspace_registry.json`` → ``root_path``)
owns the value; ``config.json`` stores a copy.

``git_identity`` is ALWAYS written, as a blank pair (``{"name": "",
"email": ""}``) when the caller supplies none, so the UI has a key to edit.
Blank or incomplete means "not configured": the git writer reads it exactly
like an absent key, so git's own identity resolution still applies and no
commit is ever attributed to an empty ident.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def vault_and_project(tmp_path, monkeypatch):
    """Hermetic vault plus a project dir accepted by the confinement check."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setattr(
        "web_ui.backend.workspace_routes._confine_to_home", lambda p: str(proj)
    )
    return vault, proj


def _client() -> TestClient:
    from web_ui.backend.workspace_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _saved_config(vault, ws_id: str) -> dict:
    path = vault / "workspaces" / ws_id / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_create_workspace_persists_registry_root(vault_and_project):
    vault, proj = vault_and_project
    with _client() as client:
        resp = client.post("/api/workspace", json={"path": str(proj)})
        assert resp.status_code == 201
        ws_id = resp.json()["workspace_id"]

    from thoughtmachine.workspace_registry import WorkspaceRegistry

    entry = WorkspaceRegistry.get_default().get_workspace(ws_id)
    assert entry is not None

    saved = _saved_config(vault, ws_id)
    assert saved["root"] == entry.root_path
    # The saved root points at the real project directory (host execution
    # realpath's it before anchoring there).
    assert os.path.realpath(saved["root"]) == os.path.realpath(str(proj))
    # No identity supplied → the key is seeded BLANK (the UI edits it); the
    # git writer reads that as "not configured", never as an empty ident.
    assert saved["git_identity"] == {"name": "", "email": ""}


def test_create_workspace_persists_supplied_git_identity(vault_and_project):
    vault, proj = vault_and_project
    identity = {"name": "Ada Lovelace", "email": "ada@example.com"}
    with _client() as client:
        resp = client.post(
            "/api/workspace",
            json={"path": str(proj), "settings": {"git_identity": identity}},
        )
        assert resp.status_code == 201
        ws_id = resp.json()["workspace_id"]

    saved = _saved_config(vault, ws_id)
    assert saved["git_identity"] == identity
    assert saved["root"]


@pytest.mark.parametrize(
    "blank_identity",
    [
        {"name": "", "email": ""},
        {"name": "", "email": "ada@example.com"},
        {"name": "Ada", "email": "   "},
    ],
)
def test_create_workspace_accepts_blank_git_identity(
    vault_and_project, blank_identity
):
    """A blank or half-filled pair is stored verbatim -- it is not an error.

    The UI needs a key it can edit from a blank state, and the git writer treats
    a blank/incomplete pair exactly like an absent one, so nothing is refused on
    the way in and nothing is pinned on the way out.
    """
    vault, proj = vault_and_project
    with _client() as client:
        resp = client.post(
            "/api/workspace",
            json={"path": str(proj), "settings": {"git_identity": blank_identity}},
        )
        assert resp.status_code == 201
        ws_id = resp.json()["workspace_id"]

    saved = _saved_config(vault, ws_id)
    assert saved["git_identity"] == blank_identity


@pytest.mark.parametrize(
    "bad_identity",
    [
        {"name": "Ada"},
        {"name": "Ada", "email": 42},
        {"name": "Ada\nInjected", "email": "ada@example.com"},
        {"name": "Ada", "email": "ada<@>example.com"},
        "ada@example.com",
    ],
)
def test_create_workspace_rejects_malformed_git_identity(
    vault_and_project, bad_identity
):
    vault, proj = vault_and_project
    with _client() as client:
        resp = client.post(
            "/api/workspace",
            json={"path": str(proj), "settings": {"git_identity": bad_identity}},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"]
    # Rejected before registration: nothing was created.
    registry_file = vault / "state" / "workspace_registry.json"
    if registry_file.exists():
        assert json.loads(registry_file.read_text(encoding="utf-8")) == {}


def test_ensure_workspace_dirs_seeds_root(vault_and_project):
    """Any creation path that bootstraps a registered workspace gets ``root``."""
    vault, proj = vault_and_project
    from thoughtmachine.workspace_capabilities import ensure_workspace_dirs
    from thoughtmachine.workspace_registry import WorkspaceRegistry

    entry = WorkspaceRegistry.get_default().register_by_root(str(proj))
    ensure_workspace_dirs(entry.id)

    saved = _saved_config(vault, entry.id)
    assert saved["root"] == entry.root_path
    assert "permissions" in saved
    # Bootstrapping seeds the blank identity too, so every creation path (not
    # just POST /api/workspace) leaves the UI an editable key.
    assert saved["git_identity"] == {"name": "", "email": ""}
