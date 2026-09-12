"""
Unit tests for the network 'outbound' -> 'bridge' mapping (Task 2).

Covers the single shared mapping ``security.gate_helpers.resolve_network_mode``
and its consumers:
  - ``docker_executor._resolve_container_config_via_gate`` (gate SSOT path;
    the former legacy session-permissions fallback now fail-closes),
  - ``security.security_gate.resolve_container_config``,
  - ``infra.container_registry._resolve_network_mode_via_gate``.

tests/security/conftest.py fixes ``sys.path`` (repo root first) so the real
``security`` package is imported, never a shadowing ``tests/security``.

Run:  pytest tests/security/test_network_mode_gate.py -q --tb=short
"""

from unittest.mock import patch

import pytest  # noqa: F401  (pytest fixtures/raises used across the file)

from security.gate_helpers import resolve_network_mode
from docker_executor import _resolve_container_config_via_gate
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from thoughtmachine.container_record import LIFECYCLE_PERSISTENT
from security.security_gate import resolve_container_config
from infra.container_registry import _resolve_network_mode_via_gate

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
# 2-4. docker_executor._resolve_container_config_via_gate
# ---------------------------------------------------------------------------


class TestResolveContainerConfigFromPermissions:
    def test_outbound_via_gate_gives_bridge_rw(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=PERMISSIVE_CAPS,
        ):
            result = _resolve_container_config_via_gate(
                "ws-1", PERMS_OUTBOUND
            )
        assert result == ("bridge", "rw")

    def test_outbound_capped_by_workspace_gives_none_ro(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=RESTRICTIVE_CAPS,
        ):
            result = _resolve_container_config_via_gate(
                "ws-1", PERMS_OUTBOUND
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
            result = _resolve_container_config_via_gate(
                "ws-1", PERMS_BANNED
            )
        assert result == ("none", "rw")

    def test_banned_with_restrictive_caps_gives_none_ro(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=RESTRICTIVE_CAPS,
        ):
            result = _resolve_container_config_via_gate(
                "ws-1", PERMS_BANNED
            )
        assert result == ("none", "ro")

    def test_legacy_fallback_path_outbound_gives_none_ro(self):
        # workspace_id=None no longer routes through a legacy session-permissions
        # fallback: the SSOT fail-closes to the most restrictive config.
        result = _resolve_container_config_via_gate(
            None, PERMS_OUTBOUND
        )
        assert result == ("none", "ro")


# ---------------------------------------------------------------------------
# 5. security.security_gate.resolve_container_config
# ---------------------------------------------------------------------------


class TestResolveContainerConfigSlice:
    def test_outbound_session_expected_bridge(self):
        cfg = resolve_container_config(PERMS_OUTBOUND, PERMISSIVE_CAPS, LIFECYCLE_PERSISTENT)
        assert cfg.network_mode == "bridge"

    def test_outbound_session_with_restrictive_caps_expected_none(self):
        cfg = resolve_container_config(PERMS_OUTBOUND, RESTRICTIVE_CAPS, LIFECYCLE_PERSISTENT)
        assert cfg.network_mode == "none"


# ---------------------------------------------------------------------------
# 6. infra.container_registry._resolve_network_mode_via_gate
# ---------------------------------------------------------------------------


class TestRegistryResolveNetworkMode:
    def test_outbound_bridge_and_banned_none(self):
        with patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=PERMISSIVE_CAPS,
        ):
            assert _resolve_network_mode_via_gate("ws-1", {"network": "outbound"}) == "bridge"
            assert _resolve_network_mode_via_gate("ws-1", {"network": "banned"}) == "none"
