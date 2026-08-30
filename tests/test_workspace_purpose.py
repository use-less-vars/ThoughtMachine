"""
Tests for workspace purpose presets and workspace registration with a purpose.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.config.resource_catalog import catalog_default_permissions
from agent.config.workspace_purpose import (
    WORKSPACE_PURPOSES,
    apply_purpose_preset,
    preset_default_permissions,
)


def test_purpose_presets_apply_default_permissions():
    """Presets resolve from catalog defaults with per-purpose overrides."""
    assert WORKSPACE_PURPOSES == ["coding", "research", "general"]

    coding = preset_default_permissions("coding")
    assert coding["git_read"] == "read"
    assert coding["git_write"] == "ask"
    assert coding["host_bash"] == "banned"
    assert coding["filesystem"] == "write"

    research = preset_default_permissions("research")
    assert research["git_write"] == "banned"
    assert research["container"] == "banned"
    assert research["network"] == "ask"

    assert preset_default_permissions("general") == catalog_default_permissions()

    # Custom overrides win over preset defaults.
    merged = apply_purpose_preset("coding", custom_permissions={"git_write": "write"})
    assert merged["git_write"] == "write"
    assert merged["git_read"] == "read"


def test_workspace_registration_with_purpose(tmp_path, monkeypatch):
    """POST /api/workspace registers a workspace with the selected purpose."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setattr(
        "web_ui.backend.workspace_routes._confine_to_home", lambda p: str(proj)
    )

    from web_ui.backend.workspace_routes import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        resp = client.post(
            "/api/workspace", json={"path": str(proj), "purpose": "coding"}
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["purpose"] == "coding"
        assert data["permissions"]["git_write"] == "ask"
        assert data["permissions"]["host_bash"] == "banned"
        assert data["risk"]["level"] in ("low", "medium", "high")

        ws_id = data["workspace_id"]
        summary = client.get(f"/api/workspace/{ws_id}")
        assert summary.status_code == 200
        assert summary.json()["purpose"] == "coding"

    saved = json.loads(
        (vault / "workspaces" / ws_id / "config.json").read_text(encoding="utf-8")
    )
    assert saved["purpose"] == "coding"
    assert saved["permissions"]["filesystem"] == "write"
    assert saved["permissions"]["git_write"] == "ask"
