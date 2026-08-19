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
#  _compute_container_config_from_permissions — standalone function
# ══════════════════════════════════════════════════════════════════════════


class TestComputeContainerConfigFromPermissions:
    """Tests for the standalone ``_compute_container_config_from_permissions``
    function in ``docker_executor.py``.

    Three code paths:
      1. workspace_id + session_permissions → security gate
      2. no workspace_id + session_permissions → fallback to raw perms
      3. no workspace_id + no session_permissions → safe defaults ("none", "ro")

    Uses ``monkeypatch`` (built-in pytest) rather than ``pytest-mock`` because
    ``pytest-mock`` is not installed in the CI/test environment.
    """

    # ── Path 1: workspace_id + session_permissions → security gate ────

    def test_gate_write_all_allowed(self, monkeypatch):
        """workspace + write perms → bridge + rw."""
        import security.security_gate as sg
        monkeypatch.setattr(sg, "get_workspace_capabilities", lambda wid: _make_workspace())
        monkeypatch.setattr(
            sg, "get_effective_permissions",
            lambda s, w: {"network": "write", "filesystem": "write", "container": True},
        )
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "write", "filesystem": "write"},
        )
        assert net == "bridge"
        assert mode == "rw"

    def test_gate_write_workspace_denies_network(self, monkeypatch):
        """workspace denies network → none."""
        import security.security_gate as sg
        monkeypatch.setattr(
            sg, "get_workspace_capabilities",
            lambda wid: _make_workspace(allow_network=False),
        )
        monkeypatch.setattr(
            sg, "get_effective_permissions",
            lambda s, w: {"network": False, "filesystem": "write", "container": True},
        )
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "write", "filesystem": "write"},
        )
        assert net == "none"
        assert mode == "rw"

    def test_gate_workspace_denies_fs_write(self, monkeypatch):
        """workspace denies filesystem write → bridge + ro."""
        import security.security_gate as sg
        monkeypatch.setattr(
            sg, "get_workspace_capabilities",
            lambda wid: _make_workspace(filesystem_write=False),
        )
        monkeypatch.setattr(
            sg, "get_effective_permissions",
            lambda s, w: {"network": "write", "filesystem": "read", "container": False},
        )
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "write", "filesystem": "write"},
        )
        assert net == "bridge"
        assert mode == "ro"

    def test_gate_ask_filesystem(self, monkeypatch):
        """ask filesystem → ro."""
        import security.security_gate as sg
        monkeypatch.setattr(sg, "get_workspace_capabilities", lambda wid: _make_workspace())
        monkeypatch.setattr(
            sg, "get_effective_permissions",
            lambda s, w: {"network": "write", "filesystem": "ask", "container": True},
        )
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "write", "filesystem": "ask"},
        )
        assert net == "bridge"
        assert mode == "ro"

    def test_gate_full_filesystem(self, monkeypatch):
        """full filesystem → rw."""
        import security.security_gate as sg
        monkeypatch.setattr(sg, "get_workspace_capabilities", lambda wid: _make_workspace())
        monkeypatch.setattr(
            sg, "get_effective_permissions",
            lambda s, w: {"network": "write", "filesystem": "full", "container": True},
        )
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "write", "filesystem": "full"},
        )
        assert net == "bridge"
        assert mode == "rw"

    def test_gate_banned_network(self, monkeypatch):
        """banned network → none."""
        import security.security_gate as sg
        monkeypatch.setattr(sg, "get_workspace_capabilities", lambda wid: _make_workspace())
        monkeypatch.setattr(
            sg, "get_effective_permissions",
            lambda s, w: {"network": "banned", "filesystem": "write", "container": True},
        )
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "banned", "filesystem": "write"},
        )
        assert net == "none"
        assert mode == "rw"

    def test_gate_gate_lookup_exception(self, monkeypatch):
        """If the security gate raises, safe defaults are returned."""
        import security.security_gate as sg
        def _raise(*a):
            raise RuntimeError("gate down")
        monkeypatch.setattr(sg, "get_workspace_capabilities", _raise)
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", "ws-123", {"network": "write", "filesystem": "write"},
        )
        assert net == "none"
        assert mode == "ro"

    # ── Path 2: no workspace_id + session_permissions → fallback ───

    def test_fallback_write_network_and_fs(self, monkeypatch):
        """No workspace_id, write perms → bridge + rw."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {"network": "write", "filesystem": "write"},
        )
        assert net == "bridge"
        assert mode == "rw"

    def test_fallback_banned_network(self, monkeypatch):
        """No workspace_id, banned network → none."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {"network": "banned", "filesystem": "write"},
        )
        assert net == "none"
        assert mode == "rw"

    def test_fallback_ask_network(self, monkeypatch):
        """No workspace_id, ask network → none."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {"network": "ask", "filesystem": "write"},
        )
        assert net == "none"
        assert mode == "rw"

    def test_fallback_read_filesystem(self, monkeypatch):
        """No workspace_id, read filesystem → ro."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {"network": "write", "filesystem": "read"},
        )
        assert net == "bridge"
        assert mode == "ro"

    def test_fallback_ask_filesystem_ro(self, monkeypatch):
        """No workspace_id, ask filesystem → ro."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {"network": "write", "filesystem": "ask"},
        )
        assert net == "bridge"
        assert mode == "ro"

    def test_fallback_full_filesystem(self, monkeypatch):
        """No workspace_id, full filesystem → rw."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {"network": "write", "filesystem": "full"},
        )
        assert net == "bridge"
        assert mode == "rw"

    def test_fallback_defaults_when_missing_keys(self, monkeypatch):
        """No workspace_id, empty perms dict → defaults (none + ro)."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, {},
        )
        assert net == "none"
        assert mode == "ro"

    # ── Path 3: no workspace_id + no session_permissions → safe defaults ──

    def test_safe_defaults_no_permissions(self, monkeypatch):
        """Both None → none + ro."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, None,
        )
        assert net == "none"
        assert mode == "ro"

    def test_safe_defaults_no_workspace_id_no_permissions(self, monkeypatch):
        """workspace_id None + permissions None → none + ro."""
        from docker_executor import _compute_container_config_from_permissions
        net, mode = _compute_container_config_from_permissions(
            "/ws/test", None, None,
        )
        assert net == "none"
        assert mode == "ro"


# ══════════════════════════════════════════════════════════════════════════
#  Permission-footprint overlay on check_required_categories
# ══════════════════════════════════════════════════════════════════════════


class TestCheckRequiredCategoriesPermissionFootprint:
    """
    Verify that the optional ``permission_footprint`` dict further restricts
    the effective permission dict using string-level permission values.

    ``permission_footprint`` values are validated per-category against the
    allowed PERMISSION_SCHEMA levels (network: ``"banned"``/``"ask"``/
    ``"write"``/``"outbound"``; filesystem: ``"banned"``/``"ask"``/
    ``"read"``/``"write"``).  An unknown category or level fails closed
    (hard deny).  Valid footprint levels are merged restrictively with the
    effective level; a footprint can never grant a category the session does
    not expose.
    """

    def test_worker_read_narrows_write(self):
        """
        Worker has ``"read"`` where session+workspace have ``"write"``.
        Effective ``{network: "write", filesystem: "write"}``,
        worker ``{filesystem: "read"}``
        → filesystem narrowed to ``"read"``, network stays ``"write"``.
        """
        # NOTE: "read" is not a valid network level (network schema is
        # banned/ask/write/outbound), so the footprint uses filesystem:read
        # to exercise the narrowing (read < write).
        ok, msg = check_required_categories(
            ["filesystem:read"],
            {"network": "write", "filesystem": "write"},
            "ReadTool",
            {},
            "",
            None,
            permission_footprint={"filesystem": "read"},
        )
        assert ok is True, f"filesystem:read should still be allowed: {msg}"

        # filesystem:write should now be denied (narrowed from write→read)
        ok2, msg2 = check_required_categories(
            ["filesystem:write"],
            {"network": "write", "filesystem": "write"},
            "WriteTool",
            {},
            "",
            None,
            permission_footprint={"filesystem": "read"},
        )
        assert ok2 is False, msg2
        assert "denied" in msg2.lower()

        # network:write should still pass (not in permission_footprint)
        ok3, _ = check_required_categories(
            ["network:write"],
            {"network": "write", "filesystem": "write"},
            "WriteTool",
            {},
            "",
            None,
            permission_footprint={"filesystem": "read"},
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
            permission_footprint={"filesystem": "banned"},
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
            permission_footprint={"filesystem": "write"},
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
            permission_footprint={"filesystem": "read"},
        )
        assert ok2 is True

    def test_worker_multi_category_restriction(self):
        """
        Worker restricts multiple categories at once.
        Effective ``{network: "write", filesystem: "write"}``,
        worker ``{filesystem: "read", network: "banned"}``
        → ``filesystem:read`` allowed, ``filesystem:write`` denied,
          ``network:true`` denied (banned).
        """
        # NOTE: "read" is not a valid network level, so the narrowing is
        # exercised via filesystem (read < write) and the hard denial via
        # network:banned.
        eff = {"network": "write", "filesystem": "write"}
        wp = {"filesystem": "read", "network": "banned"}

        # filesystem:read still allowed (narrowed from write to read)
        ok, _ = check_required_categories(
            ["filesystem:read"], eff, "Tool", {}, "", None, permission_footprint=wp
        )
        assert ok is True

        # filesystem:write denied (read < write)
        ok2, msg2 = check_required_categories(
            ["filesystem:write"], eff, "Tool", {}, "", None, permission_footprint=wp
        )
        assert ok2 is False, msg2

        # network:true denied (banned)
        ok3, msg3 = check_required_categories(
            ["network:true"], eff, "Tool", {}, "", None, permission_footprint=wp
        )
        assert ok3 is False, msg3

    def test_worker_missing_key_falls_back(self):
        """
        Worker only defines ``{"container": "read"}``.
        Other keys (filesystem, network) fall back to the effective
        session+workspace value unchanged.
        """
        eff = {
            "filesystem": "write",
            "network": True,
            "container": "write",
        }
        wp = {"container": "read"}

        # filesystem:write still passes (not in worker)
        ok, _ = check_required_categories(
            ["filesystem:write"], eff, "Tool", {}, "", None, permission_footprint=wp
        )
        assert ok is True

        # network:true still passes
        ok2, _ = check_required_categories(
            ["network:true"], eff, "Tool", {}, "", None, permission_footprint=wp
        )
        assert ok2 is True

        # container:write now denied (worker narrowed to read)
        ok3, msg3 = check_required_categories(
            ["container:write"], eff, "Tool", {}, "", None, permission_footprint=wp
        )
        assert ok3 is False, msg3
        assert "denied" in msg3.lower()
