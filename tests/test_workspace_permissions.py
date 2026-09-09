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


def test_workspace_permissions_validation_docker_alias_write():
    """docker 'write' (legacy alias of container) normalises to container True
    with no errors and never survives into the result as a docker key."""
    normalized, errors = validate_workspace_permissions({"docker": "write"})
    assert errors == []
    assert normalized == {"container": True}
    assert "docker" not in normalized


def test_workspace_permissions_validation_docker_alias_non_write():
    """docker banned/read/ask map to container False (never a grant)."""
    for level in ("banned", "read", "ask"):
        normalized, errors = validate_workspace_permissions({"docker": level})
        assert errors == [], level
        assert normalized == {"container": False}, level


def test_workspace_permissions_validation_docker_alias_invalid_level():
    """docker with an invalid level errors naming the container alias -- it is
    NEVER reported as 'unknown resource docker'."""
    normalized, errors = validate_workspace_permissions({"docker": "banana"})
    assert normalized == {}
    assert len(errors) == 1
    assert "for resource 'docker'" in errors[0]
    assert "legacy alias of 'container'" in errors[0]
    assert "unknown resource" not in errors[0]


def test_workspace_permissions_validation_canonical_container_bool():
    """Canonical container booleans pass through verbatim."""
    normalized, errors = validate_workspace_permissions({"container": True})
    assert errors == []
    assert normalized == {"container": True}
    normalized, errors = validate_workspace_permissions({"container": False})
    assert errors == []
    assert normalized == {"container": False}


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


def test_workspace_permissions_validation_accepts_canonical_catalog_levels():
    """Every canonical catalog level is accepted verbatim by the PUT validator."""
    accepted = [
        {"git": "write_on_feature_branch"},
        {"git": "write"},
        {"git": "read"},
        {"git": "ask"},
        {"git": "banned"},
        {"network": "outbound"},
        {"network": "write"},
        {"network": "ask"},
        {"network": "banned"},
        {"mcp": "full"},
        {"mcp": "connect"},
        {"mcp": "banned"},
        {"host_bash": "allow"},
        {"host_bash": "ask"},
        {"host_bash": "banned"},
        {"container": True},
        {"container": False},
        {"filesystem": "write"},
        {"system": "read"},
        {"execution": "banned"},
        {"git_read": "read"},
        {"git_write": "write"},
    ]
    for entry in accepted:
        key, value = next(iter(entry.items()))
        normalized, errors = validate_workspace_permissions(entry)
        assert errors == [], (key, value, errors)
        assert normalized == entry, (key, value, normalized)


def test_workspace_permissions_validation_rejects_off_catalog_levels():
    """Levels outside each resource's canonical scale are rejected (PUT 422)."""
    rejected = [
        ({"network": "read"}, "invalid level 'read' for resource 'network'"),
        ({"mcp": "ask"}, "invalid level 'ask' for resource 'mcp'"),
        ({"mcp": "read"}, "invalid level 'read' for resource 'mcp'"),
        ({"mcp": "write"}, "invalid level 'write' for resource 'mcp'"),
        ({"git": "full"}, "invalid level 'full' for resource 'git'"),
        ({"filesystem": "full"}, "invalid level 'full' for resource 'filesystem'"),
        ({"system": "full"}, "invalid level 'full' for resource 'system'"),
        ({"execution": "full"}, "invalid level 'full' for resource 'execution'"),
        ({"git_read": "full"}, "invalid level 'full' for resource 'git_read'"),
        ({"host_bash": "read"}, "invalid level 'read' for resource 'host_bash'"),
        (
            {"git": "write_feature_branches"},
            "invalid level 'write_feature_branches' for resource 'git'",
        ),
    ]
    for entry, message in rejected:
        normalized, errors = validate_workspace_permissions(entry)
        assert normalized == {}, entry
        assert any(message in e for e in errors), (entry, errors)


def test_workspace_permissions_validation_container_rejects_all_string_forms():
    """container is a real-boolean ceiling: every string form is rejected
    (pre-catalog vocab and JSON-stringified booleans alike)."""
    for value in ("ask", "write", "banned", "read", "full", "true", "1"):
        normalized, errors = validate_workspace_permissions({"container": value})
        assert normalized == {}, value
        assert any(
            "invalid level %r for resource 'container' (expected a boolean)" % value in e
            for e in errors
        ), (value, errors)


from security.security_gate import apply_workspace_ceiling, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

from web_ui.backend import config_manager


class TestNormalizeLegacyWorkspaceCeiling:
    """normalize_legacy_workspace_ceiling rewrites pre-catalog stored ceilings to
    the canonical vocabulary the PUT validator and the security gate understand.
    Idempotent, never mutates the input."""

    def test_container_legacy_strings_become_bools(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        for legacy, canonical in [
            ("ask", False),
            ("read", False),
            ("banned", False),
            ("write", True),
            ("full", True),
        ]:
            assert n({"container": legacy}) == {"container": canonical}, legacy

    def test_container_stringified_booleans_become_real_bools(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"container": "True"}) == {"container": True}
        assert n({"container": "False"}) == {"container": False}

    def test_container_real_bools_pass_through(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"container": True}) == {"container": True}
        assert n({"container": False}) == {"container": False}

    def test_unknown_container_string_left_untouched(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"container": "bogus"}) == {"container": "bogus"}

    def test_docker_alias_emitted_under_container_bool(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"docker": "write"}) == {"container": True}
        assert n({"docker": "full"}) == {"container": True}
        for legacy in ("read", "ask", "banned", "False"):
            assert n({"docker": legacy}) == {"container": False}, legacy
        assert "docker" not in n({"docker": "write"})

    def test_network_read_maps_to_ask(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"network": "read"}) == {"network": "ask"}
        assert n({"network": "outbound"}) == {"network": "outbound"}

    def test_mcp_legacy_generic_scale_maps_to_canonical(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"mcp": "read"}) == {"mcp": "banned"}
        assert n({"mcp": "ask"}) == {"mcp": "banned"}
        assert n({"mcp": "write"}) == {"mcp": "full"}
        assert n({"mcp": "connect"}) == {"mcp": "connect"}

    def test_full_maps_to_write_on_write_scale_resources(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        for key in ("filesystem", "system", "git"):
            assert n({key: "full"}) == {key: "write"}, key
        # execution / git grains are outside the rewrite set: left untouched
        assert n({"execution": "full"}) == {"execution": "full"}
        assert n({"git_read": "full"}) == {"git_read": "full"}

    def test_git_write_feature_branches_maps_to_write_on_feature_branch(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        assert n({"git": "write_feature_branches"}) == {
            "git": "write_on_feature_branch"
        }
        assert n({"git": "write_on_feature_branch"}) == {
            "git": "write_on_feature_branch"
        }

    def test_unknown_keys_and_values_left_untouched(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        raw = {"unknown_key": "whatever", "host_bash": "read", "git_write": "mega"}
        assert n(dict(raw)) == raw
        assert n({"host_bash": "allow"}) == {"host_bash": "allow"}

    def test_normalization_is_idempotent_and_non_mutating(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        raw = {
            "docker": "write",
            "git": "write_feature_branches",
            "network": "read",
            "mcp": "write",
            "filesystem": "full",
            "container": "ask",
        }
        once = n(raw)
        assert n(once) == once
        assert raw == {
            "docker": "write",
            "git": "write_feature_branches",
            "network": "read",
            "mcp": "write",
            "filesystem": "full",
            "container": "ask",
        }

    def test_combined_legacy_map_rewrite(self):
        n = config_manager.normalize_legacy_workspace_ceiling
        result = n(
            {
                "docker": "write",
                "git": "write_feature_branches",
                "network": "read",
                "mcp": "write",
                "filesystem": "full",
                "system": "full",
                "container": "ask",
                "execution": "full",
            }
        )
        assert result == {
            "container": False,
            "git": "write_on_feature_branch",
            "network": "ask",
            "mcp": "full",
            "filesystem": "write",
            "system": "write",
            "execution": "full",
        }


class TestApplyWorkspaceCeiling:
    """Unit tests for apply_workspace_ceiling (pure dict reduction)."""

    def test_banned_caps_write(self):
        result = apply_workspace_ceiling({"filesystem": "banned"}, {"filesystem": "write"})
        assert result == {"filesystem": "banned"}

    def test_read_caps_write(self):
        result = apply_workspace_ceiling({"filesystem": "read"}, {"filesystem": "write"})
        assert result == {"filesystem": "read"}

    def test_ask_ceiling_caps_write_to_below_ask_tier(self):
        # An ask ceiling must never fabricate an effective ask grant: it caps
        # a more-permissive session value to the most permissive tier below
        # ask -- 'read' on scales that have one.
        result = apply_workspace_ceiling({"filesystem": "ask"}, {"filesystem": "write"})
        assert result == {"filesystem": "read"}

    def test_write_keeps_write(self):
        result = apply_workspace_ceiling({"filesystem": "write"}, {"filesystem": "write"})
        assert result == {"filesystem": "write"}

    def test_more_restrictive_session_stands(self):
        result = apply_workspace_ceiling({"filesystem": "write"}, {"filesystem": "read"})
        assert result == {"filesystem": "read"}

    def test_legacy_write_feature_branches_token_is_unknown_level_fail_open(self):
        # A raw legacy 'write_feature_branches' token is not part of any
        # canonical ceiling vocabulary (git uses 'write_on_feature_branch').
        # When such a token reaches the gate directly (loader bypassed) it is
        # an UNKNOWN level: fail-open with a WARN, so the session grant stands.
        for resource in ("filesystem", "git"):
            result = apply_workspace_ceiling(
                {resource: "write_feature_branches"}, {resource: "write"}
            )
            assert result == {resource: "write"}, resource

    def test_git_write_on_feature_branch_ceiling_caps_write_grant(self):
        # Normative DISPATCH-6 example: ceiling write_on_feature_branch
        # (rank 2.5) is more restrictive than a session write grant (3.0),
        # so the ceiling level itself becomes the effective git level.
        result = apply_workspace_ceiling(
            {"git": "write_on_feature_branch"}, {"git": "write"}
        )
        assert result == {"git": "write_on_feature_branch"}

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

    def test_host_bash_banned_caps_allow(self):
        # host_bash session vocabulary is banned/ask/allow (no legacy "write"
        # grain); a banned ceiling caps the strongest session grant (allow).
        result = apply_workspace_ceiling({"host_bash": "banned"}, {"host_bash": "allow"})
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

    def test_git_ask_ceiling_caps_write_to_read(self):
        result = apply_workspace_ceiling({"git": "ask"}, {"git": "write"})
        assert result == {"git": "read"}

    def test_git_write_ask_ceiling_caps_write_to_read(self):
        result = apply_workspace_ceiling({"git_write": "ask"}, {"git_write": "write"})
        assert result == {"git_write": "read"}

    def test_ask_ceiling_over_session_ask_stands(self):
        # A genuine session-level ask grant ranks AT the ask ceiling and
        # passes through unchanged (interactive prompt flow preserved).
        result = apply_workspace_ceiling({"filesystem": "ask"}, {"filesystem": "ask"})
        assert result == {"filesystem": "ask"}

    def test_ask_ceiling_over_session_read_stands(self):
        # A session read grant is already below the ask ceiling: it stands.
        result = apply_workspace_ceiling({"filesystem": "ask"}, {"filesystem": "read"})
        assert result == {"filesystem": "read"}

    def test_network_ask_ceiling_caps_write_to_banned(self):
        # network's scale (banned|ask|write|outbound) has no read tier below
        # ask, so an ask ceiling over a write grant caps to banned.
        result = apply_workspace_ceiling({"network": "ask"}, {"network": "write"})
        assert result == {"network": "banned"}

    def test_network_ask_ceiling_caps_outbound_to_banned(self):
        result = apply_workspace_ceiling({"network": "ask"}, {"network": "outbound"})
        assert result == {"network": "banned"}

    def test_host_bash_ask_ceiling_caps_allow_to_banned(self):
        # host_bash has no tier between banned and ask, so an ask ceiling
        # over an allow grant caps to banned (never a fabricated ask).
        result = apply_workspace_ceiling({"host_bash": "ask"}, {"host_bash": "allow"})
        assert result == {"host_bash": "banned"}

    def test_host_bash_ask_ceiling_over_ask_grant_stands(self):
        result = apply_workspace_ceiling({"host_bash": "ask"}, {"host_bash": "ask"})
        assert result == {"host_bash": "ask"}

    def test_git_write_ceiling_does_not_cap_write_on_feature_branch_grant(self):
        # Normative DISPATCH-6 example: a write ceiling (3.0) is NOT more
        # restrictive than a wofb grant (2.5) -- the feature-branch-restricted
        # grant stands unchanged.
        result = apply_workspace_ceiling(
            {"git": "write"}, {"git": "write_on_feature_branch"}
        )
        assert result == {"git": "write_on_feature_branch"}

    def test_write_on_feature_branch_ceiling_does_not_cap_wofb_grant(self):
        result = apply_workspace_ceiling(
            {"git": "write_on_feature_branch"}, {"git": "write_on_feature_branch"}
        )
        assert result == {"git": "write_on_feature_branch"}

    def test_write_on_feature_branch_ceiling_invalid_on_git_grains_fail_open(self):
        # wofb exists on the git aggregate scale only; git_read/git_write take
        # the four-level scale, so a wofb ceiling there is an unknown level:
        # fail-open with a WARN and the session grant stands.
        result = apply_workspace_ceiling(
            {"git_write": "write_on_feature_branch"}, {"git_write": "write"}
        )
        assert result == {"git_write": "write"}

    def test_read_ceiling_caps_write_on_feature_branch_grant(self):
        result = apply_workspace_ceiling({"git": "read"}, {"git": "write_on_feature_branch"})
        assert result == {"git": "read"}

    def test_network_write_ceiling_caps_outbound_grant(self):
        # Normative DISPATCH-6 example: outbound (3.5 grant / 4.0 ceiling
        # rank) sits above write, so a write ceiling pulls an outbound
        # grant down to the ceiling level 'write'.
        result = apply_workspace_ceiling({"network": "write"}, {"network": "outbound"})
        assert result == {"network": "write"}

    def test_network_outbound_ceiling_is_unlimited_for_lower_grants(self):
        # Normative DISPATCH-6 example: an outbound ceiling ranks 4.0
        # (unlimited tier) -- a plain write grant stands untouched.
        result = apply_workspace_ceiling({"network": "outbound"}, {"network": "write"})
        assert result == {"network": "write"}

    def test_git_read_ask_ceiling_caps_write_to_read(self):
        result = apply_workspace_ceiling({"git_read": "ask"}, {"git_read": "write"})
        assert result == {"git_read": "read"}

    def test_system_ask_ceiling_caps_write_to_read(self):
        result = apply_workspace_ceiling({"system": "ask"}, {"system": "write"})
        assert result == {"system": "read"}

    def test_execution_ask_ceiling_caps_write_to_read(self):
        result = apply_workspace_ceiling({"execution": "ask"}, {"execution": "write"})
        assert result == {"execution": "read"}

    def test_mcp_connect_ceiling_caps_full_grant(self):
        result = apply_workspace_ceiling({"mcp": "connect"}, {"mcp": "full"})
        assert result == {"mcp": "connect"}

    def test_mcp_full_ceiling_is_unlimited_for_connect_grant(self):
        result = apply_workspace_ceiling({"mcp": "full"}, {"mcp": "connect"})
        assert result == {"mcp": "connect"}

    def test_host_bash_allow_ceiling_keeps_ask_grant(self):
        result = apply_workspace_ceiling({"host_bash": "allow"}, {"host_bash": "ask"})
        assert result == {"host_bash": "ask"}

    def test_container_false_ceiling_caps_true_session(self):
        result = apply_workspace_ceiling({"container": False}, {"container": True})
        assert result == {"container": False}

    def test_container_true_ceiling_keeps_false_session(self):
        # A ceiling can only restrict; a disabled session container stays off
        # even under an unlimited (True) container ceiling.
        result = apply_workspace_ceiling({"container": True}, {"container": False})
        assert result == {"container": False}

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

    def test_git_ceiling_ask_read_splits_never_ask(self):
        # An ask ceiling over a session write grant caps git to read; the
        # derived git_write grain is banned -- no effective value is ask.
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"git": "ask"})
        assert eff["git"] == "read"
        assert eff["git_read"] == "read"
        assert eff["git_write"] == "banned"
        assert "ask" not in [str(v) for v in eff.values()]

    def test_filesystem_ceiling_ask_caps_write_to_read(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"filesystem": "ask"})
        assert eff["filesystem"] == "read"

    def test_network_ceiling_ask_caps_write_to_banned(self):
        # network has no read tier below ask, so the ask ceiling over the
        # write grant caps straight to banned.
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"network": "ask"})
        assert eff["network"] == "banned"

    def test_genuine_session_ask_git_preserved(self):
        # A session-level git ask grant ranks AT the ask ceiling and passes
        # through unchanged -- the interactive prompt flow is not regressed.
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="ask",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"git": "ask"})
        assert eff["git"] == "ask"
        assert eff["git_read"] == "ask"
        assert eff["git_write"] == "ask"

    def test_git_write_on_feature_branch_ceiling_wiring(self):
        # Normative DISPATCH-6 example end-to-end: ceiling wofb over a
        # session write grant -> effective git is write_on_feature_branch;
        # the split git_write grain carries the feature-branch restriction.
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(
            self._session(), workspace, {"git": "write_on_feature_branch"}
        )
        assert eff["git"] == "write_on_feature_branch"
        assert eff["git_read"] == "read"
        assert eff["git_write"] == "write_on_feature_branch"

    def test_git_write_ceiling_does_not_cap_wofb_grant_wiring(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write_on_feature_branch",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"git": "write"})
        assert eff["git"] == "write_on_feature_branch"
        assert eff["git_read"] == "read"
        assert eff["git_write"] == "write_on_feature_branch"

    def test_git_read_ceiling_caps_wofb_grant_wiring(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write_on_feature_branch",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"git": "read"})
        assert eff["git"] == "read"
        assert eff["git_write"] == "banned"

    def test_git_ask_ceiling_caps_wofb_grant_wiring(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write_on_feature_branch",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"git": "ask"})
        assert eff["git"] == "read"
        assert eff["git_write"] == "banned"

    def test_network_write_ceiling_caps_outbound_grant_wiring(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="outbound",
            container=True,
            git="write",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"network": "write"})
        assert eff["network"] == "write"

    def test_network_outbound_ceiling_is_unlimited_wiring(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"network": "outbound"})
        assert eff["network"] == "write"
        session = SessionPermissions(
            filesystem="write",
            network="outbound",
            container=True,
            git="write",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"network": "outbound"})
        assert eff["network"] == "outbound"

    def test_mcp_connect_ceiling_caps_full_grant_wiring(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write",
            system="read",
            mcp="full",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"mcp": "connect"})
        assert eff["mcp"] == "connect"

    def test_mcp_full_ceiling_is_unlimited_wiring(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write",
            system="read",
            mcp="connect",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"mcp": "full"})
        assert eff["mcp"] == "connect"

    def test_container_false_ceiling_blocks_container(self):
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(self._session(), workspace, {"container": False})
        assert eff["container"] is False

    def test_container_true_ceiling_keeps_disabled_session(self):
        workspace = WorkspaceCapabilities()
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=False,
            git="write",
            system="read",
            mcp="banned",
            execution="banned",
        )
        eff = get_effective_permissions(session, workspace, {"container": True})
        assert eff["container"] is False

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
        # coding preset's network 'ask' ceiling caps the factory network
        # 'write' grant; network has no read tier below ask, so the cap
        # lands on 'banned' (an ask ceiling never yields an effective ask).
        assert perms["network"] == "banned"

