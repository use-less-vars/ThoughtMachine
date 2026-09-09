"""
Tests for workspace permission vocabulary unification.

Covers the unified per-resource vocabularies accepted by the workspace
permission validator (``validate_workspace_permissions``), the unified
ceiling rankings applied by the security gate
(``apply_workspace_ceiling`` / ``get_effective_permissions``), the boolean
``container`` ceiling everywhere (catalog defaults, purpose presets,
validator), and a full PUT /permissions roundtrip proving a legacy
``docker`` string grant persists as the canonical ``container`` boolean.

The ceiling surface is the six canonical workspace resources
``container | network | filesystem | git | mcp | host_bash``: the legacy
grains ``git_read`` / ``git_write`` / ``system`` / ``execution`` were
removed and are rejected fail-closed by the validator (``git_read`` /
``git_write`` with a hint pointing at ``git``); the security gate no
longer recognises them as ceiling keys.
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
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.config.resource_catalog import validate_workspace_permissions
from agent.config.workspace_purpose import apply_purpose_preset
from security.security_gate import apply_workspace_ceiling, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities


# --------------------------------------------------------------------------
# Validator: unified vocabulary acceptance
# --------------------------------------------------------------------------

def test_validator_accepts_container_booleans():
    """Canonical container booleans pass through verbatim (no errors)."""
    for value in (True, False):
        normalized, errors = validate_workspace_permissions({"container": value})
        assert errors == []
        assert normalized == {"container": value}


def test_validator_accepts_docker_alias_normalisation():
    """docker 'write' -> {'container': True}; docker 'ask' -> {'container': False}."""
    normalized, errors = validate_workspace_permissions({"docker": "write"})
    assert errors == []
    assert normalized == {"container": True}
    normalized, errors = validate_workspace_permissions({"docker": "ask"})
    assert errors == []
    assert normalized == {"container": False}


def test_validator_accepts_own_scale_levels():
    """mcp 'connect', git 'write_on_feature_branch', host_bash 'allow' and
    network 'outbound' are accepted on their per-resource scales."""
    normalized, errors = validate_workspace_permissions({"mcp": "connect"})
    assert errors == []
    assert normalized == {"mcp": "connect"}

    normalized, errors = validate_workspace_permissions({"git": "write_on_feature_branch"})
    assert errors == []
    assert normalized == {"git": "write_on_feature_branch"}

    normalized, errors = validate_workspace_permissions({"host_bash": "allow"})
    assert errors == []
    assert normalized == {"host_bash": "allow"}

    normalized, errors = validate_workspace_permissions({"network": "outbound"})
    assert errors == []
    assert normalized == {"network": "outbound"}


# --------------------------------------------------------------------------
# Validator: rejection with canonical per-resource level lists
# --------------------------------------------------------------------------

def test_validator_rejects_mcp_level_outside_own_scale():
    normalized, errors = validate_workspace_permissions({"mcp": "banana"})
    assert normalized == {}
    assert len(errors) == 1
    assert "expected one of ['banned', 'connect', 'full']" in errors[0]


def test_validator_rejects_git_level_outside_own_scale():
    """git's canonical ceiling scale includes the branch-restricted write
    tier but no split grains: a level outside it is rejected."""
    normalized, errors = validate_workspace_permissions({"git": "banana"})
    assert normalized == {}
    assert len(errors) == 1
    assert "expected one of ['ask', 'banned', 'read', 'write', 'write_on_feature_branch']" in errors[0]


def test_validator_rejects_legacy_split_git_grains_fail_closed():
    """The removed git_read/git_write ceiling grains are rejected with a
    precise legacy hint naming the canonical replacement -- never accepted
    as workspace ceilings."""
    for grain in ("git_write", "git_read"):
        normalized, errors = validate_workspace_permissions({grain: "write"})
        assert normalized == {}
        assert len(errors) == 1
        assert f"legacy permission resource '{grain}' is no longer supported; use 'git'" in errors[0]


def test_validator_rejects_container_string_booleans_only():
    """container accepts booleans only: any string level -- even a legacy
    'ask'/'full' form -- is rejected fail-closed (the docker alias is the
    only legacy string path, and it normalises to a container boolean)."""
    for value in ("banana", "ask", "full"):
        normalized, errors = validate_workspace_permissions({"container": value})
        assert normalized == {}
        assert len(errors) == 1
        assert f"invalid level '{value}' for resource 'container'" in errors[0]
        assert "expected a boolean" in errors[0]


def test_validator_rejects_unknown_resource():
    normalized, errors = validate_workspace_permissions({"nope": "write"})
    assert normalized == {}
    assert errors == ["unknown resource 'nope'"]


# --------------------------------------------------------------------------
# Gate: workspace ceilings for mcp (banned < connect < full)
# --------------------------------------------------------------------------

class TestWorkspaceCeilingMcp:
    """apply_workspace_ceiling over the mcp ceiling key."""

    def test_mcp_banned_ceiling_caps_full(self):
        result = apply_workspace_ceiling({"mcp": "banned"}, {"mcp": "full"})
        assert result == {"mcp": "banned"}

    def test_mcp_connect_ceiling_caps_full(self):
        result = apply_workspace_ceiling({"mcp": "connect"}, {"mcp": "full"})
        assert result == {"mcp": "connect"}

    def test_mcp_full_ceiling_keeps_full(self):
        result = apply_workspace_ceiling({"mcp": "full"}, {"mcp": "full"})
        assert result == {"mcp": "full"}

    def test_mcp_full_ceiling_keeps_connect_session_grant(self):
        # The more restrictive session grant (connect) stands under a full
        # ceiling -- a ceiling never raises a session value.
        result = apply_workspace_ceiling({"mcp": "full"}, {"mcp": "connect"})
        assert result == {"mcp": "connect"}

    def test_mcp_unknown_ceiling_level_fail_open(self):
        result = apply_workspace_ceiling({"mcp": "read"}, {"mcp": "full"})
        assert result == {"mcp": "full"}


def test_ceiling_key_mcp_emits_no_warning(caplog):
    """mcp is a recognised ceiling key: no 'ignoring workspace ceiling'
    warning is emitted for it."""
    with caplog.at_level(logging.WARNING, logger="security.security_gate"):
        apply_workspace_ceiling({"mcp": "banned"}, {"mcp": "full"})
    warnings = [
        r.getMessage()
        for r in caplog.records
        if "ignoring workspace ceiling" in r.getMessage()
    ]
    assert warnings == []


# --------------------------------------------------------------------------
# get_effective_permissions wiring for the unified vocabulary
# --------------------------------------------------------------------------

class TestEffectivePermissionsUnifiedVocabWiring:
    """get_effective_permissions caps canonical-resource session grants."""

    @staticmethod
    def _session():
        return SessionPermissions(
            mcp="connect",
            filesystem="write",
        )

    def test_mcp_and_filesystem_ceilings_applied(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(
            self._session(),
            workspace,
            {"mcp": "banned", "filesystem": "read"},
        )
        assert eff["mcp"] == "banned"
        assert eff["filesystem"] == "read"
        # Effective permissions always carry the full six-key canonical set.
        assert set(eff.keys()) == {
            "container", "network", "filesystem", "git", "mcp", "host_bash",
        }

    def test_mcp_connect_ceiling_passthrough(self):
        # A genuine session-level connect grant ranks AT the connect ceiling
        # and passes through unchanged.
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(
            self._session(), workspace, {"mcp": "connect"}
        )
        assert eff["mcp"] == "connect"

    def test_mcp_full_ceiling_leaves_connect_grant_standing(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"mcp": "full"})
        assert eff["mcp"] == "connect"


# --------------------------------------------------------------------------
# Purpose presets: boolean container False everywhere
# --------------------------------------------------------------------------

def test_purpose_presets_container_is_false():
    """coding/research/general all resolve container to the boolean False."""
    for purpose in ("coding", "research", "general"):
        merged = apply_purpose_preset(purpose)
        assert merged["container"] is False, purpose


# --------------------------------------------------------------------------
# PUT /permissions roundtrip: docker string grant -> container boolean
# --------------------------------------------------------------------------

def test_put_permissions_docker_write_roundtrip(tmp_path, monkeypatch):
    """PUT /permissions {'docker': 'write'} persists and serves back the
    canonical {'container': True}; invalid unified-vocab levels 422."""
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
            "/api/workspace", json={"path": str(proj), "purpose": "general"}
        )
        assert resp.status_code == 201
        data = resp.json()
        ws_id = data["workspace_id"]
        assert data["permissions"]["container"] is False

        put = client.put(
            f"/api/workspace/{ws_id}/permissions",
            json={"permissions": {"docker": "write"}},
        )
        assert put.status_code == 200
        put_data = put.json()
        assert put_data["permissions"] == {"container": True}
        assert put_data["risk"]["level"] in ("low", "medium", "high")

        get = client.get(f"/api/workspace/{ws_id}/permissions")
        assert get.status_code == 200
        assert get.json()["permissions"] == {"container": True}

        bad = client.put(
            f"/api/workspace/{ws_id}/permissions",
            json={"permissions": {"mcp": "banana"}},
        )
        assert bad.status_code == 422
        errors = bad.json()["detail"]["errors"]
        assert len(errors) == 1
        assert "expected one of ['banned', 'connect', 'full']" in errors[0]

    saved = json.loads(
        (vault / "workspaces" / ws_id / "config.json").read_text(encoding="utf-8")
    )
    assert saved["permissions"] == {"container": True}
