"""
Workspace detail page core (Layer 2) — backend permission persistence.

Covers PUT /api/workspace/{ws_id}/permissions accepting an optional
``allow_host_resources`` flag:

  - explicit ``allow_host_resources: true`` is persisted to config.json and
    reflected in the response;
  - omitting the field leaves the previously saved value untouched;
  - GET /api/workspace/{ws_id}/permissions reflects the persisted value;
  - unknown resource names still reject with 422.

Run:  python3 -m pytest web_ui/backend/tests/test_workspace_routes.py -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.workspace_routes as workspace_routes
from web_ui.backend.server import app

client = TestClient(app)

VALID_PERMISSIONS = {"filesystem": "read", "host_bash": "banned"}


@pytest.fixture
def fake_ws_dir(monkeypatch, tmp_path):
    """Point the permissions endpoints at a throwaway workspace directory."""
    ws_dir = tmp_path / "ws-test"
    ws_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: ws_dir)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)
    return ws_dir


def _seed_config(ws_dir, **overrides):
    """Write a starting config.json via the module's own saver."""
    cfg = {
        "purpose": "general",
        "allow_host_resources": False,
        "permissions": dict(VALID_PERMISSIONS),
    }
    cfg.update(overrides)
    workspace_routes._save_workspace_config("ws-test", cfg)


# ══════════════════════════════════════════════════════════════════════════════
# PUT /api/workspace/{ws_id}/permissions — allow_host_resources handling
# ══════════════════════════════════════════════════════════════════════════════


class TestPutPermissionsAllowHostResources:
    def test_explicit_true_persists_and_is_reflected(self, fake_ws_dir):
        _seed_config(fake_ws_dir, allow_host_resources=False)

        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={
                "permissions": dict(VALID_PERMISSIONS),
                "allow_host_resources": True,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["allow_host_resources"] is True
        assert data["permissions"] == VALID_PERMISSIONS

        # Persisted to config.json
        cfg = workspace_routes._load_workspace_config("ws-test")
        assert cfg["allow_host_resources"] is True
        assert cfg["permissions"] == VALID_PERMISSIONS

    def test_explicit_false_persists(self, fake_ws_dir):
        _seed_config(fake_ws_dir, allow_host_resources=True)

        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={
                "permissions": dict(VALID_PERMISSIONS),
                "allow_host_resources": False,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["allow_host_resources"] is False
        cfg = workspace_routes._load_workspace_config("ws-test")
        assert cfg["allow_host_resources"] is False

    def test_omitted_field_leaves_saved_value_untouched(self, fake_ws_dir):
        _seed_config(fake_ws_dir, allow_host_resources=False)

        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={"permissions": dict(VALID_PERMISSIONS)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["allow_host_resources"] is False
        cfg = workspace_routes._load_workspace_config("ws-test")
        assert cfg["allow_host_resources"] is False

        # And the inverse: a previously True value survives an omission too.
        _seed_config(fake_ws_dir, allow_host_resources=True)
        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={"permissions": dict(VALID_PERMISSIONS)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["allow_host_resources"] is True
        assert workspace_routes._load_workspace_config("ws-test")[
            "allow_host_resources"
        ] is True

    def test_omitted_field_still_updates_permissions(self, fake_ws_dir):
        _seed_config(fake_ws_dir, allow_host_resources=True)

        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={"permissions": {"git": "ask"}},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["permissions"] == {"git": "ask"}
        assert data["allow_host_resources"] is True
        cfg = workspace_routes._load_workspace_config("ws-test")
        assert cfg["permissions"] == {"git": "ask"}
        assert cfg["allow_host_resources"] is True


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/workspace/{ws_id}/permissions — reflects persisted value
# ══════════════════════════════════════════════════════════════════════════════


class TestGetPermissionsAllowHostResources:
    def test_get_reflects_persisted_value(self, fake_ws_dir):
        _seed_config(fake_ws_dir, allow_host_resources=True)

        resp = client.get("/api/workspace/ws-test/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["allow_host_resources"] is True
        assert data["permissions"] == VALID_PERMISSIONS
        assert data["workspace_id"] == "ws-test"


# ══════════════════════════════════════════════════════════════════════════════
# Validation still enforced
# ══════════════════════════════════════════════════════════════════════════════


class TestPutPermissionsValidation:
    def test_unknown_resource_rejected_with_422(self, fake_ws_dir):
        _seed_config(fake_ws_dir)

        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={"permissions": {"not_a_resource": "read"}},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json().get("detail", {})
        errors = detail.get("errors", []) if isinstance(detail, dict) else []
        assert isinstance(errors, list) and errors

    def test_invalid_level_rejected_with_422(self, fake_ws_dir):
        _seed_config(fake_ws_dir)

        resp = client.put(
            "/api/workspace/ws-test/permissions",
            json={"permissions": {"filesystem": "superuser"}},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json().get("detail", {})
        errors = detail.get("errors", []) if isinstance(detail, dict) else []
        assert isinstance(errors, list) and errors
