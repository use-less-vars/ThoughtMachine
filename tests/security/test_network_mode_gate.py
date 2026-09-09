"""
Unit tests for the network 'outbound' -> 'bridge' mapping (Task 2).

Covers the single shared mapping ``security.gate_helpers.resolve_network_mode``
and its consumers:
  - ``docker_executor._compute_container_config_from_permissions`` (gate path
    + legacy session-permissions fallback path),
  - ``security.security_gate.get_expected_container_config``,
  - ``infra.container_registry.ContainerRegistry.resolve_network_mode``.

tests/security/conftest.py fixes ``sys.path`` (repo root first) so the real
``security`` package is imported, never a shadowing ``tests/security``.

Run:  pytest tests/security/test_network_mode_gate.py -q --tb=short
"""

from unittest.mock import patch

import pytest  # noqa: F401  (pytest fixtures/raises used across the file)

from security.gate_helpers import resolve_network_mode
from docker_executor import _compute_container_config_from_permissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from security.security_gate import get_expected_container_config
from infra.container_registry import ContainerRegistry

PERMS_OUTBOUND = {"network": "outbound", "filesystem": "write"}
PERMS_BANNED = {"network": "banned", "filesystem": "write"}

PERMISSIVE_CAPS = WorkspaceCapabilities(
    allow_network=True, filesystem_write=True, allow_docker=True
)
RESTRICTIVE_CAPS = WorkspaceCapabilities(
    allow_network=False, filesystem_write=False, allow_docker=False
)


# ---------------------------------------------------------------------------
# 1. Shared mapping table (security.gate_helpers.resolve_network_mode)
# ---------------------------------------------------------------------------


class TestResolveNetworkModeMapping:
    def test_bridge_grants(self):
        for value in (True, "write", "Write", "outbound", "OUTBOUND", "Outbound"):
            assert resolve_network_mode(value) == "bridge", repr(value)

    def test_none_values(self):
        for value in (
            False, "banned", "read", "ask", "none", "", "full", "connect",
            "deny", None, {},
        ):
            assert resolve_network_mode(value) == "none", repr(value)


# ---------------------------------------------------------------------------
# 2-4. docker_executor._compute_container_config_from_permissions
# ---------------------------------------------------------------------------


class TestComputeContainerConfigFromPermissions:
    def test_outbound_via_gate_gives_bridge_rw(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=PERMISSIVE_CAPS,
        ):
            result = _compute_container_config_from_permissions(
                "/tmp/ws", "ws-1", PERMS_OUTBOUND
            )
        assert result == ("bridge", "rw")

    def test_outbound_capped_by_workspace_gives_none_ro(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=RESTRICTIVE_CAPS,
        ):
            result = _compute_container_config_from_permissions(
                "/tmp/ws", "ws-1", PERMS_OUTBOUND
            )
        assert result == ("none", "ro")

    def test_banned_denies_network_but_keeps_fs_rw_via_gate(self):
        # network and filesystem are independent axes: a banned network grant
        # kills bridge but a filesystem=write session (permissive caps) keeps
        # the rw workspace mode.
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=PERMISSIVE_CAPS,
        ):
            result = _compute_container_config_from_permissions(
                "/tmp/ws", "ws-1", PERMS_BANNED
            )
        assert result == ("none", "rw")

    def test_banned_with_restrictive_caps_gives_none_ro(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=RESTRICTIVE_CAPS,
        ):
            result = _compute_container_config_from_permissions(
                "/tmp/ws", "ws-1", PERMS_BANNED
            )
        assert result == ("none", "ro")

    def test_legacy_fallback_path_outbound_gives_bridge_rw(self):
        # workspace_id=None routes through the session-permissions fallback,
        # which previously only recognised "write" for bridge.
        result = _compute_container_config_from_permissions(
            "/tmp/ws", None, PERMS_OUTBOUND
        )
        assert result == ("bridge", "rw")


# ---------------------------------------------------------------------------
# 5. security.security_gate.get_expected_container_config
# ---------------------------------------------------------------------------


class TestGetExpectedContainerConfig:
    def test_outbound_session_expected_bridge(self):
        cfg = get_expected_container_config(PERMS_OUTBOUND, PERMISSIVE_CAPS)
        assert cfg["network_mode"] == "bridge"

    def test_outbound_session_with_restrictive_caps_expected_none(self):
        cfg = get_expected_container_config(PERMS_OUTBOUND, RESTRICTIVE_CAPS)
        assert cfg["network_mode"] == "none"


# ---------------------------------------------------------------------------
# 6. infra.container_registry.ContainerRegistry.resolve_network_mode
# ---------------------------------------------------------------------------


class TestRegistryResolveNetworkMode:
    def test_outbound_bridge_and_banned_none(self):
        assert ContainerRegistry.resolve_network_mode({"network": "outbound"}) == "bridge"
        assert ContainerRegistry.resolve_network_mode({"network": "banned"}) == "none"
