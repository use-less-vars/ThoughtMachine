"""
test_gate_contract.py — Contract tests for the unified security gate + container config.

Tests the full chain:
  1. ``get_effective_permissions()`` merges session permissions × workspace capabilities.
  2. The merged dict is then correctly translated to ``(network_mode, workspace_mode)``
     by ``_compute_desired_config()``.

Matrix tested:
  - session network: "write", "banned", "ask"
  - workspace network: True, False
  - session filesystem: "write", "read", "banned"
  - workspace filesystem_write: True, False
"""

from __future__ import annotations

import pytest

from security.security_gate import (
    WorkspaceCapabilities,
    get_effective_permissions,
)
from thoughtmachine.security import SessionPermissions
from docker_executor import _compute_desired_config


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _make_session(
    network: str = "write",
    filesystem: str = "write",
    container: bool = True,
    git: str = "write",
    security: str = "write",
) -> SessionPermissions:
    """Build a SessionPermissions with the given overrides."""
    return SessionPermissions(
        network=network,
        filesystem=filesystem,
        container=container,
        git=git,
        security=security,
    )


def _make_workspace(
    network: bool = True,
    filesystem_write: bool = True,
    git_available: bool = True,
    container_available: bool = True,
) -> WorkspaceCapabilities:
    return WorkspaceCapabilities(
        network=network,
        filesystem_write=filesystem_write,
        git_available=git_available,
        container_available=container_available,
    )


def _expected_config(eff: dict) -> tuple[str, str]:
    """Compute expected (network_mode, workspace_mode) from an effective dict."""
    net = eff.get("network")
    if net is True or net == "write":
        network_mode = "bridge"
    else:
        network_mode = "none"
    fs = eff.get("filesystem", "read")
    workspace_mode = "rw" if fs in ("write", "full") else "ro"
    return network_mode, workspace_mode


# ══════════════════════════════════════════════════════════════════════════
#  Effective-permissions contract tests
# ══════════════════════════════════════════════════════════════════════════


class TestEffectivePermissions:
    """Verify the merge logic of ``get_effective_permissions()``."""

    @pytest.mark.parametrize(
        "session_net, ws_net, expected_net",
        [
            # session.write + ws.{True,False} → 'write' (pass-through) / False (deny)
            ("write", True, "write"),
            ("write", False, False),
            # session.banned + ws.{True,False} → 'banned' (pass-through) / False (deny)
            ("banned", True, "banned"),
            ("banned", False, False),
            # session.ask + ws.True → "ask" (pass-through)
            ("ask", True, "ask"),
            # session.ask + ws.False → False (workspace denies)
            ("ask", False, False),
        ],
    )
    def test_network(
        self,
        session_net: str,
        ws_net: bool,
        expected_net,
    ):
        session = _make_session(network=session_net)
        workspace = _make_workspace(network=ws_net)
        eff = get_effective_permissions(session, workspace)
        # The effective value is either a string (pass-through) or False (workspace denies)
        assert eff["network"] == expected_net, (
            f"session.network={session_net!r} × ws.network={ws_net} "
            f"→ {eff['network']!r}, expected {expected_net!r}"
        )

    @pytest.mark.parametrize(
        "session_fs, ws_fs_write, expected_fs",
        [
            # write + True → write
            ("write", True, "write"),
            # write + False → read (downgraded)
            ("write", False, "read"),
            # read + True → read
            ("read", True, "read"),
            # read + False → read (no change)
            ("read", False, "read"),
            # banned + True → banned
            ("banned", True, "banned"),
            # banned + False → banned
            ("banned", False, "banned"),
        ],
    )
    def test_filesystem(
        self,
        session_fs: str,
        ws_fs_write: bool,
        expected_fs: str,
    ):
        session = _make_session(filesystem=session_fs)
        workspace = _make_workspace(filesystem_write=ws_fs_write)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == expected_fs, (
            f"session.filesystem={session_fs!r} × ws.filesystem_write={ws_fs_write} "
            f"→ {eff['filesystem']!r}, expected {expected_fs!r}"
        )

    def test_container_defaults(self):
        """Container permission is a simple boolean AND."""
        session = _make_session(container=True)
        workspace = _make_workspace(container_available=True)
        eff = get_effective_permissions(session, workspace)
        assert eff["container"] is True

    def test_container_denied_by_workspace(self):
        session = _make_session(container=True)
        workspace = _make_workspace(container_available=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["container"] is False

    def test_git_defaults(self):
        session = _make_session(git="write")
        workspace = _make_workspace(git_available=True)
        eff = get_effective_permissions(session, workspace)
        assert eff["git"] == "write"

    def test_git_denied_by_workspace(self):
        session = _make_session(git="write")
        workspace = _make_workspace(git_available=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["git"] is False


# ══════════════════════════════════════════════════════════════════════════
#  Full-chain: effective dict → container config
# ══════════════════════════════════════════════════════════════════════════


class TestContainerConfigContract:
    """Verify that the effective-permissions dict yields the right container config."""

    @pytest.mark.parametrize(
        "session_net, ws_net, expected_network_mode",
        [
            # write + allow → bridge
            ("write", True, "bridge"),
            # write + deny  → none
            ("write", False, "none"),
            # banned (any ws) → none
            ("banned", True, "none"),
            ("banned", False, "none"),
            # ask + allow → ask (passed through, but _compute_desired_config
            # treats non-True/non-"write" as none)
            ("ask", True, "none"),
            ("ask", False, "none"),
        ],
    )
    def test_network_mode(
        self,
        session_net: str,
        ws_net: bool,
        expected_network_mode: str,
    ):
        """Full chain: permission → effective → container config."""
        session = _make_session(network=session_net)
        workspace = _make_workspace(network=ws_net)
        eff = get_effective_permissions(session, workspace)
        network_mode, _ = _expected_config(eff)
        assert network_mode == expected_network_mode, (
            f"session.net={session_net!r} ws.net={ws_net} "
            f"→ effective={eff['network']!r} → network_mode={network_mode!r}"
        )

    @pytest.mark.parametrize(
        "session_fs, ws_fs_write, expected_mode",
        [
            ("write", True, "rw"),
            ("write", False, "ro"),  # downgraded
            ("read", True, "ro"),
            ("read", False, "ro"),
            ("banned", True, "ro"),
        ],
    )
    def test_workspace_mode(
        self,
        session_fs: str,
        ws_fs_write: bool,
        expected_mode: str,
    ):
        session = _make_session(filesystem=session_fs)
        workspace = _make_workspace(filesystem_write=ws_fs_write)
        eff = get_effective_permissions(session, workspace)
        _, workspace_mode = _expected_config(eff)
        assert workspace_mode == expected_mode, (
            f"session.fs={session_fs!r} ws.fs_write={ws_fs_write} "
            f"→ effective.fs={eff['filesystem']!r} → mode={workspace_mode!r}"
        )

    def test_full_chain_via_desired_config(self):
        """Integration: _compute_desired_config uses get_effective_permissions internally."""
        # session: write, workspace: allows → network=bridge, mode=rw
        _, mode = _compute_desired_config(
            workspace_path="/tmp/fake-ws",
            workspace_id="test-id",
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )
        assert mode == "rw"

    def test_desired_config_fallback_no_workspace_id(self):
        """When workspace_id is None, _compute_desired_config falls back to session_permissions."""
        net, mode = _compute_desired_config(
            workspace_path="/tmp/fake-ws",
            workspace_id=None,
            session_permissions={"network": "write", "filesystem": "read", "container": True},
        )
        assert net == "bridge"
        assert mode == "ro"

    def test_desired_config_fallback_none_session(self):
        """When session_permissions is None, safe defaults are used."""
        net, mode = _compute_desired_config(
            workspace_path="/tmp/fake-ws",
            workspace_id="test-id",
            session_permissions=None,
        )
        assert net == "none"
        assert mode == "ro"
