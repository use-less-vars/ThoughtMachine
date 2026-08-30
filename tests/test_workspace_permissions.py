"""
Tests for workspace permission validation and the permissions REST endpoints.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.config.resource_catalog import validate_workspace_permissions


def test_workspace_permissions_validation_rejects_unknown():
    """Unknown resources and invalid levels are dropped and reported as errors."""
    normalized, errors = validate_workspace_permissions(
        {"git_read": "read", "not_a_resource": "write", "git_write": "banana"}
    )
    assert normalized == {"git_read": "read"}
    assert any("unknown resource 'not_a_resource'" in e for e in errors)
    assert any("invalid level 'banana' for resource 'git_write'" in e for e in errors)


def test_workspace_permissions_persist_and_load(tmp_path, monkeypatch):
    """PUT /permissions validates, persists to config.json, and GET reloads it."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    ws_dir = vault / "workspaces" / "ws-test"
    ws_dir.mkdir(parents=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"purpose": "general", "permissions": {"git_read": "read"}}),
        encoding="utf-8",
    )

    from web_ui.backend.workspace_routes import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        put = client.put(
            "/api/workspace/ws-test/permissions",
            json={"permissions": {"git_read": "read", "git_write": "ask", "host_bash": "banned"}},
        )
        assert put.status_code == 200
        data = put.json()
        assert data["permissions"] == {
            "git_read": "read",
            "git_write": "ask",
            "host_bash": "banned",
        }
        assert data["risk"]["level"] in ("low", "medium", "high")

        get = client.get("/api/workspace/ws-test/permissions")
        assert get.status_code == 200
        assert get.json()["permissions"] == {
            "git_read": "read",
            "git_write": "ask",
            "host_bash": "banned",
        }

        bad = client.put(
            "/api/workspace/ws-test/permissions", json={"permissions": {"nope": "write"}}
        )
        assert bad.status_code == 422
        assert bad.json()["detail"]["errors"] == ["unknown resource 'nope'"]

    saved = json.loads((ws_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["permissions"] == {
        "git_read": "read",
        "git_write": "ask",
        "host_bash": "banned",
    }
