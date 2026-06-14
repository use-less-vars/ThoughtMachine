"""
test_gate_contract.py — Trust-level contract tests for the security gate.

Extends the docker-level ``tests/docker/test_gate_contract.py`` with
additional trust assertions that verify the gate behaves correctly for
edge values not covered in the original parametrized matrix:

Gaps filled here:
  - filesystem="full"  → "rw"   (original only tests write/read/banned)
  - filesystem="ask"   → "ro"   (ask is treated restrictively at config level)
  - Container config with invalid session values → safe defaults

Also tests the new ``get_expected_container_config()`` function as the
canonical reference for container config derivation.
"""

from __future__ import annotations

import pytest

from security.security_gate import (
    check_required_categories,
    get_effective_permissions,
    get_expected_container_config,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from thoughtmachine.security import SessionPermissions


# ══════════════════════════════════════════════════════════════════════════
#  Helpers (mirror tests/docker/test_gate_contract.py)
# ══════════════════════════════════════════════════════════════════════════


def _make_session(
    network: str = "write",
    filesystem: str = "write",
    container: bool = True,
    git: str = "write",
    security: str = "write",
) -> SessionPermissions:
    return SessionPermissions(
        network=network,
        filesystem=filesystem,
        container=container,
        git=git,
        security=security,
    )


def _make_workspace(
    allow_network: bool = True,
    filesystem_write: bool = True,
    git_available: bool = True,
    allow_docker: bool = True,
) -> WorkspaceCapabilities:
    return WorkspaceCapabilities(
        allow_network=allow_network,
        filesystem_write=filesystem_write,
        git_available=git_available,
        allow_docker=allow_docker,
    )


def _expected_config(eff: dict) -> tuple[str, str]:
    """Compute expected (network_mode, workspace_mode) from an effective dict."""
    net = eff.get("network")
    network_mode = "bridge" if (net is True or net == "write") else "none"
    fs = eff.get("filesystem", "read")
    workspace_mode = "rw" if fs in ("write", "full") else "ro"
    return network_mode, workspace_mode


# ══════════════════════════════════════════════════════════════════════════
#  Missing effective-permissions values
# ══════════════════════════════════════════════════════════════════════════


class TestEffectivePermissionsEdgeValues:
    """Cover values missing from the original parametrized matrix."""

    def test_filesystem_full(self):
        """filesystem='full' passes through unchanged (highest level)."""
        session = _make_session(filesystem="full")
        workspace = _make_workspace(filesystem_write=True)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "full"

    def test_filesystem_full_workspace_denies_write_no_downgrade(self):
        """filesystem='full' is NOT downgraded by workspace write deny
        (only 'write' is downgraded, not 'full')."""
        session = _make_session(filesystem="full")
        workspace = _make_workspace(filesystem_write=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "full", (
            f"full should not be downgraded by ws deny, got {eff['filesystem']!r}"
        )

    def test_filesystem_ask(self):
        """filesystem='ask' passes through unchanged."""
        session = _make_session(filesystem="ask")
        workspace = _make_workspace(filesystem_write=True)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "ask"

    def test_filesystem_ask_with_workspace_deny(self):
        """filesystem='ask' stays 'ask' even when workspace denies write."""
        session = _make_session(filesystem="ask")
        workspace = _make_workspace(filesystem_write=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "ask"


# ══════════════════════════════════════════════════════════════════════════
#  Missing container-config translations
# ══════════════════════════════════════════════════════════════════════════


class TestContainerConfigEdgeValues:
    """Verify edge-value translations that the original matrix skips."""

    @pytest.mark.parametrize(
        "session_fs, ws_fs_write, expected_mode",
        [
            # Original covers: write→rw, write+deny→ro, read→ro, banned→ro
            # Gaps: full  → rw (full is write-level), ask → ro
            ("full", True, "rw"),
            ("full", False, "rw"),  # full not downgraded by workspace
            ("ask", True, "ro"),
            ("ask", False, "ro"),
        ],
    )
    def test_workspace_mode_edge(
        self,
        session_fs: str,
        ws_fs_write: bool,
        expected_mode: str,
    ):
        """filesystem='full' produces 'rw'; 'ask' produces 'ro'."""
        session = _make_session(filesystem=session_fs)
        workspace = _make_workspace(filesystem_write=ws_fs_write)
        eff = get_effective_permissions(session, workspace)
        _, workspace_mode = _expected_config(eff)
        assert workspace_mode == expected_mode, (
            f"session.fs={session_fs!r} ws.fs_write={ws_fs_write} "
            f"→ effective.fs={eff['filesystem']!r} → mode={workspace_mode!r}"
        )


# ══════════════════════════════════════════════════════════════════════════
#  get_expected_container_config — canonical reference
# ══════════════════════════════════════════════════════════════════════════


class TestGetExpectedContainerConfig:
    """Tests for the canonical container-config resolver."""

    def test_write_all_allowed(self):
        """write permissions + fully-permissive workspace → bridge + rw."""
        result = get_expected_container_config(
            {"network": "write", "filesystem": "write", "container": True},
        )
        assert result["network_mode"] == "bridge"
        assert result["workspace_mode"] == "rw"
        assert result["effective"]["network"] == "write"
        assert result["effective"]["filesystem"] == "write"

    def test_write_downgraded_by_workspace(self):
        """write + workspace denies filesystem_write → bridge + ro."""
        ws = _make_workspace(filesystem_write=False)
        result = get_expected_container_config(
            {"network": "write", "filesystem": "write", "container": True},
            workspace_caps=ws,
        )
        assert result["network_mode"] == "bridge"
        assert result["workspace_mode"] == "ro"
        assert result["effective"]["filesystem"] == "read"

    def test_banned_network(self):
        """banned + fully-permissive → none + rw."""
        result = get_expected_container_config(
            {"network": "banned", "filesystem": "write", "container": True},
        )
        assert result["network_mode"] == "none"
        assert result["workspace_mode"] == "rw"

    def test_full_filesystem(self):
        """full filesystem → rw."""
        result = get_expected_container_config(
            {"network": "write", "filesystem": "full", "container": True},
        )
        assert result["network_mode"] == "bridge"
        assert result["workspace_mode"] == "rw"
        assert result["effective"]["filesystem"] == "full"

    def test_ask_filesystem(self):
        """ask filesystem → ro (ask is not write-level)."""
        result = get_expected_container_config(
            {"network": "write", "filesystem": "ask", "container": True},
        )
        assert result["network_mode"] == "bridge"
        assert result["workspace_mode"] == "ro"

    def test_ask_network(self):
        """ask network → none."""
        result = get_expected_container_config(
            {"network": "ask", "filesystem": "write", "container": True},
        )
        assert result["network_mode"] == "none"
        assert result["workspace_mode"] == "rw"

    def test_full_filesystem_not_downgraded_by_workspace(self):
        """full filesystem stays rw even when workspace denies write."""
        ws = _make_workspace(filesystem_write=False)
        result = get_expected_container_config(
            {"network": "write", "filesystem": "full", "container": True},
            workspace_caps=ws,
        )
        assert result["workspace_mode"] == "rw"
        assert result["effective"]["filesystem"] == "full"

    def test_workspace_denies_network(self):
        """Workspace network deny overrides session write."""
        ws = _make_workspace(allow_network=False)
        result = get_expected_container_config(
            {"network": "write", "filesystem": "write", "container": True},
            workspace_caps=ws,
        )
        assert result["network_mode"] == "none"
        assert result["effective"]["network"] is False

    def test_workspace_denies_container(self):
        """Workspace container deny overrides session container."""
        ws = _make_workspace(allow_docker=False)
        result = get_expected_container_config(
            {"network": "write", "filesystem": "write", "container": True},
            workspace_caps=ws,
        )
        assert result["effective"]["container"] is False

    def test_default_workspace_caps(self):
        """When workspace_caps is None, fully-permissive defaults are used."""
        result = get_expected_container_config(
            {"network": "write", "filesystem": "write", "container": True},
            workspace_caps=None,
        )
        assert result["network_mode"] == "bridge"
        assert result["workspace_mode"] == "rw"

    def test_matches_expected_config_helper(self):
        """get_expected_container_config must match _expected_config helper."""
        test_cases = [
            ({"network": "write", "filesystem": "write", "container": True}, "bridge", "rw"),
            ({"network": "banned", "filesystem": "read", "container": True}, "none", "ro"),
            ({"network": "ask", "filesystem": "full", "container": True}, "none", "rw"),
            ({"network": "write", "filesystem": "ask", "container": True}, "bridge", "ro"),
        ]
        for sp, exp_net, exp_fs in test_cases:
            result = get_expected_container_config(sp)
            assert result["network_mode"] == exp_net, (
                f"network_mode: {sp} → {result['network_mode']!r}, expected {exp_net!r}"
            )
            assert result["workspace_mode"] == exp_fs, (
                f"workspace_mode: {sp} → {result['workspace_mode']!r}, expected {exp_fs!r}"
            )

    def test_invalid_network_value_returns_safe_defaults(self):
        """Unknown network value (not in SessionPermissions enum) → safe defaults."""
        result = get_expected_container_config(
            {"network": "invalid", "filesystem": "write", "container": True},
        )
        assert result["network_mode"] == "none"
        assert result["workspace_mode"] == "ro"

    def test_invalid_filesystem_value_returns_safe_defaults(self):
        """Unknown filesystem value → safe defaults."""
        result = get_expected_container_config(
            {"network": "write", "filesystem": "bogus", "container": True},
        )
        assert result["network_mode"] == "none"
        assert result["workspace_mode"] == "ro"

    def test_empty_session_permissions_returns_safe_defaults(self):
        """Empty dict for session_permissions → safe defaults."""
        result = get_expected_container_config({})
        assert result["network_mode"] == "none"
        assert result["workspace_mode"] == "ro"


# ══════════════════════════════════════════════════════════════════════════
#  Worker-permissions overlay on check_required_categories
# ══════════════════════════════════════════════════════════════════════════


class TestCheckRequiredCategoriesWorkerPermissions:
    """
    Verify that the optional ``worker_permissions`` dict further restricts
    the effective permission dict using string-level permission values.

    ``worker_permissions`` uses the same string hierarchy as session/workspace
    permissions (``"banned"``, ``"read"``, ``"write"``, ``"full"``).
    ``_min_permission`` compares levels and returns the more restrictive one.
    """

    def test_worker_read_narrows_write(self):
        """
        Worker has ``"read"`` where session+workspace have ``"write"``.
        Effective ``{network: "write", filesystem: "write"}``,
        worker ``{network: "read"}``
        → network narrowed to ``"read"``, filesystem stays ``"write"``.
        """
        ok, msg = check_required_categories(
            ["network:read"],
            {"network": "write", "filesystem": "write"},
            "ReadTool",
            {},
            "",
            None,
            worker_permissions={"network": "read"},
        )
        assert ok is True, f"network:read should still be allowed: {msg}"

        # network:write should now be denied (narrowed from write→read)
        ok2, msg2 = check_required_categories(
            ["network:write"],
            {"network": "write", "filesystem": "write"},
            "WriteTool",
            {},
            "",
            None,
            worker_permissions={"network": "read"},
        )
        assert ok2 is False, msg2
        assert "denied" in msg2.lower()

        # filesystem:write should still pass (not in worker_permissions)
        ok3, _ = check_required_categories(
            ["filesystem:write"],
            {"network": "write", "filesystem": "write"},
            "WriteTool",
            {},
            "",
            None,
            worker_permissions={"network": "read"},
        )
        assert ok3 is True

    def test_worker_banned_hard_denies(self):
        """
        Worker has ``{"filesystem": "banned"}`` → hard deny even for
        read-level access.
        """
        ok, msg = check_required_categories(
            ["filesystem:read"],
            {"filesystem": "read"},
            "ReadTool",
            {},
            "",
            None,
            worker_permissions={"filesystem": "banned"},
        )
        assert ok is False
        assert "denied" in msg.lower()

    def test_worker_same_level_passthrough(self):
        """
        Worker has same level as effective → effective passes through.
        """
        ok, msg = check_required_categories(
            ["filesystem:write"],
            {"filesystem": "write"},
            "WriteTool",
            {},
            "",
            None,
            worker_permissions={"filesystem": "write"},
        )
        assert ok is True

        # read also passes when worker matches
        ok2, _ = check_required_categories(
            ["filesystem:read"],
            {"filesystem": "read"},
            "ReadTool",
            {},
            "",
            None,
            worker_permissions={"filesystem": "read"},
        )
        assert ok2 is True

    def test_worker_multi_category_restriction(self):
        """
        Worker restricts multiple categories at once.
        Effective ``{network: "write", filesystem: "write"}``,
        worker ``{network: "read", filesystem: "banned"}``
        → ``network:read`` allowed, ``network:write`` denied,
          ``filesystem:read`` denied (banned).
        """
        eff = {"network": "write", "filesystem": "write"}
        wp = {"network": "read", "filesystem": "banned"}

        # network:read still allowed
        ok, _ = check_required_categories(
            ["network:read"], eff, "Tool", {}, "", None, worker_permissions=wp
        )
        assert ok is True

        # network:write denied (read < write)
        ok2, msg2 = check_required_categories(
            ["network:write"], eff, "Tool", {}, "", None, worker_permissions=wp
        )
        assert ok2 is False, msg2

        # filesystem:read denied (banned)
        ok3, msg3 = check_required_categories(
            ["filesystem:read"], eff, "Tool", {}, "", None, worker_permissions=wp
        )
        assert ok3 is False, msg3

    def test_worker_missing_key_falls_back(self):
        """
        Worker only defines ``{"execution": "read"}``.
        Other keys (filesystem, network) fall back to the effective
        session+workspace value unchanged.
        """
        eff = {
            "filesystem": "write",
            "network": True,
            "execution": "write",
        }
        wp = {"execution": "read"}

        # filesystem:write still passes (not in worker)
        ok, _ = check_required_categories(
            ["filesystem:write"], eff, "Tool", {}, "", None, worker_permissions=wp
        )
        assert ok is True

        # network:true still passes
        ok2, _ = check_required_categories(
            ["network:true"], eff, "Tool", {}, "", None, worker_permissions=wp
        )
        assert ok2 is True

        # execution:write now denied (worker narrowed to read)
        ok3, msg3 = check_required_categories(
            ["execution:write"], eff, "Tool", {}, "", None, worker_permissions=wp
        )
        assert ok3 is False, msg3
        assert "denied" in msg3.lower()
