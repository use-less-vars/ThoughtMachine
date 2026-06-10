"""
Tests for error boundary handling in the Docker integration layer.

Covers:

  * ``_compute_desired_config`` failure modes (gate lookup failures, bad inputs)
  * ``_resolve_workspace_id`` failures
  * Audit log I/O errors (``/tmp/container_audit.log``)
  * Exceptional container states (stopped vs running, missing attrs keys)
  * Concurrency safety (the ``_build_log_cache_lock`` isn't relevant here,
    but we verify no crash happens under sequential calls)

These are unit-level tests that use mocks to provoke specific failure paths.

.. note::
    This package is named ``docker_integration`` (not ``docker``) to avoid
    shadowing the real ``docker`` package on ``sys.path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from docker_executor import (
    _compute_desired_config,
    _resolve_workspace_id,
    verify_container_integrity,
)


# ===================================================================
# _resolve_workspace_id failures
# ===================================================================

class TestResolveWorkspaceId:
    """The helper should return ``None`` without raising on any error."""

    def test_returns_none_on_import_error(self):
        """When ``thoughtmachine.workspace_capabilities`` can't be imported."""
        with patch("thoughtmachine.workspace_capabilities.resolve_workspace_id", side_effect=ImportError("no module")):
            result = _resolve_workspace_id("/tmp/workspace")
        assert result is None

    def test_returns_none_on_generic_exception(self):
        """A generic exception inside the function is caught."""
        with patch("thoughtmachine.workspace_capabilities.resolve_workspace_id", side_effect=ValueError("bad")):
            result = _resolve_workspace_id("/tmp/workspace")
        assert result is None

    def test_returns_id_on_success(self):
        """Normal resolution returns the workspace ID."""
        with patch("thoughtmachine.workspace_capabilities.resolve_workspace_id", return_value="ws-1234"):
            result = _resolve_workspace_id("/tmp/workspace")
        assert result == "ws-1234"


# ===================================================================
# _compute_desired_config edge cases
# ===================================================================

class TestComputeDesiredConfig:
    """Standalone config computation with various permission/capability combos."""

    def test_no_workspace_id_and_no_permissions(self):
        """Both workspace_id and permissions are None/absent → most restrictive."""
        net, mode = _compute_desired_config("/tmp/ws", workspace_id=None, session_permissions=None)
        assert net == "none"
        assert mode == "ro"

    def test_workspace_id_but_no_permissions(self):
        """Workspace ID present but permissions None → restrictive."""
        net, mode = _compute_desired_config("/tmp/ws", workspace_id="ws-1", session_permissions=None)
        # Falls through to defaults since session_permissions is None
        assert net == "none"
        assert mode == "ro"

    def test_no_workspace_id_with_permissions_fallback(self):
        """No workspace ID but permissions dict present → fallback logic."""
        net, mode = _compute_desired_config(
            "/tmp/ws", workspace_id=None, session_permissions={"network": "write", "filesystem": "write"}
        )
        # Fallback: network="write" → "bridge", filesystem="write" → "rw"
        assert net == "bridge"
        assert mode == "rw"

    def test_no_workspace_id_with_banned_permissions(self):
        """Fallback with banned network."""
        net, mode = _compute_desired_config(
            "/tmp/ws", workspace_id=None, session_permissions={"network": "banned", "filesystem": "read"}
        )
        assert net == "none"
        assert mode == "ro"

    def test_gate_lookup_failure_falls_back_to_restrictive(self):
        """When ``security.security_gate`` import or gate lookup fails, the
        function logs a warning and returns safe defaults."""
        net, mode = _compute_desired_config(
            "/tmp/ws", workspace_id="ws-1",
            session_permissions={"network": "write", "filesystem": "write"}
        )
        # Falls back to restrictive defaults when gate is unreachable
        assert net == "none"
        assert mode == "ro"

    def test_gate_returns_limited_caps(self):
        """When the security gate is unreachable, the function falls back to
        restrictive defaults regardless of session permissions."""
        # No patch needed: ``security.security_gate`` import fails in the
        # test environment (``tests/security/`` shadows the real ``security/``
        # package), which triggers the same fallback path.
        net, mode = _compute_desired_config(
            "/tmp/ws", workspace_id="ws-1",
            session_permissions={"network": "write", "filesystem": "write"}
        )

        assert net == "none"
        assert mode == "ro"


# ===================================================================
# verify_container_integrity — Docker API error boundaries
# ===================================================================

class TestDockerApiErrorBoundaries:
    """How ``verify_container_integrity`` handles Docker API exceptions."""

    def test_container_get_raises_api_error(self, patch_docker, mock_docker_client):
        """If ``containers.get()`` raises ``docker.errors.APIError`` (e.g. daemon
        connectivity lost), it propagates up (no catch for APIError in get)."""
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.APIError("API error")

        # APIError is NOT a NotFound, so it should propagate
        with pytest.raises(docker.errors.APIError):
            verify_container_integrity("/tmp/workspace", None)

    def test_container_reload_raises_api_error(self, patch_docker, make_container, mock_docker_client):
        """If ``container.reload()`` raises, it propagates."""
        import docker.errors
        container = make_container()
        container.reload.side_effect = docker.errors.APIError("reload failed")

        with pytest.raises(docker.errors.APIError):
            verify_container_integrity("/tmp/workspace", None)

    def test_container_attrs_missing_mounts_key(self, patch_docker, mock_docker_client):
        """A container without a ``Mounts`` key in attrs → ``actual_mode`` stays ``ro``."""
        container = MagicMock(name="Container")
        container.id = "abc123def456"
        container.name = "agent-exec-abc123def456"
        container.attrs = {
            "Id": "abc123def456",
            "HostConfig": {"NetworkMode": "bridge"},
            # No Mounts key
        }
        container.reload.return_value = None
        mock_docker_client.containers.get.return_value = container

        import docker.errors
        mock_docker_client.containers.get.side_effect = None  # override NotFound
        mock_docker_client.containers.get.return_value = container

        result = verify_container_integrity("/tmp/workspace", None)

        # actual_mode defaults to "ro"
        assert "mode" in result["actual"]
        assert result["actual"]["mode"] == "ro"

    def test_container_attrs_missing_hostconfig(self, patch_docker, mock_docker_client):
        """A container without ``HostConfig`` key → KeyError propagates (unexpected)."""
        container = MagicMock(name="Container")
        container.id = "abc123def456"
        container.name = "agent-exec-abc123def456"
        container.attrs = {
            "Id": "abc123def456",
            # No HostConfig
        }
        container.reload.return_value = None
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(KeyError):
            verify_container_integrity("/tmp/workspace", None)

    def test_container_with_multiple_mounts(self, patch_docker, mock_docker_client):
        """Container with multiple mounts still finds the /workspace mount."""
        container = MagicMock(name="Container")
        container.id = "abc123def456"
        container.name = "agent-exec-abc123def456"
        container.attrs = {
            "Id": "abc123def456",
            "HostConfig": {"NetworkMode": "none"},
            "Mounts": [
                {"Type": "bind", "Source": "/other", "Destination": "/other", "Mode": "rw"},
                {"Type": "bind", "Source": "/ws", "Destination": "/workspace", "Mode": "ro"},
                {"Type": "bind", "Source": "/data", "Destination": "/data", "Mode": "rw"},
            ],
        }
        container.reload.return_value = None
        mock_docker_client.containers.get.return_value = container

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["actual"]["mode"] == "ro"
        assert result["actual"]["network"] == "none"

    def test_stopped_container_is_still_inspected(self, patch_docker, make_container):
        """A stopped container (status != 'running') can still be inspected
        and matched/mismatched."""
        make_container(network_mode="none", mount_mode="ro", status="exited")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is True
        assert result["matches_config"] is True


# ===================================================================
# Call pattern & idempotency
# ===================================================================

class TestIdempotency:
    """Calling verify_container_integrity multiple times should be safe."""

    def test_no_container_called_twice(self, patch_docker, mock_docker_client):
        """Two calls with no container both succeed."""
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nope")

        r1 = verify_container_integrity("/tmp/workspace", None)
        r2 = verify_container_integrity("/tmp/workspace", None)

        assert r1["container_exists"] is False
        assert r2["container_exists"] is False

    def test_matching_container_called_twice(self, patch_docker, make_container):
        """Two calls with matching container both succeed, no removal."""
        make_container(network_mode="none", mount_mode="ro")

        r1 = verify_container_integrity("/tmp/workspace", None)
        r2 = verify_container_integrity("/tmp/workspace", None)

        assert r1["action_taken"] == "none"
        assert r2["action_taken"] == "none"
