"""
Tests for workspace permission validation and the permissions REST endpoints.
"""

from __future__ import annotations

import sys

_bad_prefix = "/workspace/tests"
sys.path = [p for p in sys.path if not p.startswith(_bad_prefix)]
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _stubs_path)
sys.path.insert(1, "/workspace")

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


from security.security_gate import apply_workspace_ceiling, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

from web_ui.backend import config_manager


class TestApplyWorkspaceCeiling:
    """Unit tests for apply_workspace_ceiling (pure dict reduction)."""

    def test_banned_caps_write(self):
        result = apply_workspace_ceiling({"filesystem": "banned"}, {"filesystem": "write"})
        assert result == {"filesystem": "banned"}

    def test_read_caps_write(self):
        result = apply_workspace_ceiling({"filesystem": "read"}, {"filesystem": "write"})
        assert result == {"filesystem": "read"}

    def test_ask_caps_write(self):
        result = apply_workspace_ceiling({"filesystem": "ask"}, {"filesystem": "write"})
        assert result == {"filesystem": "ask"}

    def test_write_keeps_write(self):
        result = apply_workspace_ceiling({"filesystem": "write"}, {"filesystem": "write"})
        assert result == {"filesystem": "write"}

    def test_more_restrictive_session_stands(self):
        result = apply_workspace_ceiling({"filesystem": "write"}, {"filesystem": "read"})
        assert result == {"filesystem": "read"}

    def test_write_feature_branches_unlimited(self):
        result = apply_workspace_ceiling(
            {"filesystem": "write_feature_branches"}, {"filesystem": "write"}
        )
        assert result == {"filesystem": "write"}

    def test_missing_resource_not_capped(self):
        # A ceiling for a resource the session has not granted is not injected.
        result = apply_workspace_ceiling({"network": "read"}, {"filesystem": "write"})
        assert result == {"filesystem": "write"}

    def test_docker_banned_blocks_container_true(self):
        result = apply_workspace_ceiling({"docker": "banned"}, {"container": True})
        assert result == {"container": False}

    def test_docker_write_allows_container_true(self):
        result = apply_workspace_ceiling({"docker": "write"}, {"container": True})
        assert result == {"container": True}

    def test_docker_read_blocks_container_true(self):
        result = apply_workspace_ceiling({"docker": "read"}, {"container": True})
        assert result == {"container": False}

    def test_docker_banned_blocks_container_ask(self):
        result = apply_workspace_ceiling({"docker": "banned"}, {"container": "ask"})
        assert result == {"container": False}

    def test_docker_write_allows_container_write(self):
        result = apply_workspace_ceiling({"docker": "write"}, {"container": "write"})
        assert result == {"container": True}

    def test_host_bash_banned_caps_write(self):
        result = apply_workspace_ceiling({"host_bash": "banned"}, {"host_bash": "write"})
        assert result == {"host_bash": "banned"}

    def test_git_read_read_caps_write(self):
        result = apply_workspace_ceiling({"git_read": "read"}, {"git_read": "write"})
        assert result == {"git_read": "read"}

    def test_filesystem_read_caps_full(self):
        result = apply_workspace_ceiling({"filesystem": "read"}, {"filesystem": "full"})
        assert result == {"filesystem": "read"}

    def test_unknown_ceiling_level_fail_open(self):
        result = apply_workspace_ceiling({"filesystem": "mega"}, {"filesystem": "write"})
        assert result == {"filesystem": "write"}

    def test_unknown_resource_ignored(self):
        result = apply_workspace_ceiling({"not_a_resource": "banned"}, {"filesystem": "write"})
        assert result == {"filesystem": "write"}

    def test_empty_workspace_permissions_copies_session(self):
        session = {"filesystem": "write", "network": "banned"}
        result = apply_workspace_ceiling({}, session)
        assert result == session
        assert result is not session

    def test_original_session_dict_not_mutated(self):
        session = {"filesystem": "write", "container": True}
        apply_workspace_ceiling({"filesystem": "read", "docker": "banned"}, session)
        assert session == {"filesystem": "write", "container": True}


class TestEffectivePermissionsCeilingWiring:
    """get_effective_permissions applies workspace ceilings on top of session perms."""

    @staticmethod
    def _session():
        return SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write",
            system="read",
            mcp="banned",
            execution="banned",
        )

    def test_filesystem_ceiling_read(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"filesystem": "read"})
        assert eff["filesystem"] == "read"

    def test_docker_ceiling_banned_blocks_container(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"docker": "banned"})
        assert eff["container"] is False

    def test_network_ceiling_banned(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"network": "banned"})
        assert eff["network"] == "banned"

    def test_git_ceiling_read_splits(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"git": "read"})
        assert eff["git"] == "read"
        assert eff["git_read"] == "read"
        assert eff["git_write"] == "banned"

    def test_no_ceiling_keeps_session_perms(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, None)
        assert eff["filesystem"] == "write"
        assert eff["network"] == "write"
        assert eff["container"] is True
        assert eff["git"] == "write"


class TestResolveFullConfigCeiling:
    """resolve_full_config applies the workspace permission ceiling to merged config."""

    @pytest.fixture(autouse=True)
    def _stub_layers(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            config_manager,
            "_load_factory_defaults",
            lambda: {
                "session_permissions": {
                    "filesystem": "write",
                    "network": "write",
                    "container": True,
                    "git": "write",
                }
            },
        )
        monkeypatch.setattr(config_manager, "_load_global_defaults_layer", lambda: {})
        monkeypatch.setattr(config_manager, "_load_agent_config_layer", lambda: {})
        monkeypatch.setattr(
            config_manager,
            "_resolve_provider_layer",
            lambda merged, provider_id=None, fallback_any=True: merged,
        )
        monkeypatch.setattr(
            config_manager,
            "_get_workspace_defaults_path",
            lambda ws_id: tmp_path / "no-workspace" / "defaults.json",
        )

    def test_ceiling_applied_to_merged_permissions(self, monkeypatch):
        monkeypatch.setattr(
            config_manager,
            "_load_workspace_permission_ceiling",
            lambda ws_id: {
                "filesystem": "read",
                "docker": "banned",
                "network": "banned",
                "git": "read",
            },
        )
        merged = config_manager.resolve_full_config(workspace_id="ws1")
        assert merged["session_permissions"] == {
            "filesystem": "read",
            "network": "banned",
            "container": False,
            "git": "read",
        }

    def test_worker_overrides_still_capped(self, monkeypatch):
        monkeypatch.setattr(
            config_manager,
            "_load_workspace_permission_ceiling",
            lambda ws_id: {"filesystem": "read"},
        )
        merged = config_manager.resolve_full_config(
            workspace_id="ws1",
            worker_overrides={"session_permissions": {"filesystem": "write", "network": "write"}},
        )
        perms = merged["session_permissions"]
        assert perms["filesystem"] == "read"
        assert perms["network"] == "write"
        assert perms["container"] is True

    def test_no_workspace_skips_ceiling(self, monkeypatch):
        calls = []

        def spy(ws_id):
            calls.append(ws_id)
            return {}

        monkeypatch.setattr(config_manager, "_load_workspace_permission_ceiling", spy)
        merged = config_manager.resolve_full_config()
        assert merged["session_permissions"]["filesystem"] == "write"
        assert merged["session_permissions"]["container"] is True
        assert calls == []

    def test_no_session_permissions_key_no_crash(self, monkeypatch):
        monkeypatch.setattr(config_manager, "_load_factory_defaults", lambda: {"model": "gpt-x"})
        monkeypatch.setattr(
            config_manager,
            "_load_workspace_permission_ceiling",
            lambda ws_id: {"filesystem": "read"},
        )
        merged = config_manager.resolve_full_config(workspace_id="ws1")
        assert "session_permissions" not in merged

    def test_real_vault_config_file(self, monkeypatch, tmp_path):
        import thoughtmachine.workspace_capabilities as wc_mod

        monkeypatch.setattr(
            wc_mod, "_workspace_dir", lambda ws_id: tmp_path / "workspaces" / ws_id
        )
        ws_dir = tmp_path / "workspaces" / "ws1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "config.json").write_text(
            json.dumps({"purpose": "general", "permissions": {"filesystem": "read", "docker": "banned"}}),
            encoding="utf-8",
        )
        merged = config_manager.resolve_full_config(workspace_id="ws1")
        perms = merged["session_permissions"]
        assert perms["filesystem"] == "read"
        assert perms["container"] is False

    def test_purpose_preset_applied(self, monkeypatch, tmp_path):
        import thoughtmachine.workspace_capabilities as wc_mod

        monkeypatch.setattr(
            wc_mod, "_workspace_dir", lambda ws_id: tmp_path / "workspaces" / ws_id
        )
        ws_dir = tmp_path / "workspaces" / "ws1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "config.json").write_text(
            json.dumps({"purpose": "coding"}), encoding="utf-8"
        )
        merged = config_manager.resolve_full_config(workspace_id="ws1")
        perms = merged["session_permissions"]
        assert perms["container"] is False
        assert perms["network"] == "ask"

