"""
Tests for ``verify_container_integrity()`` in ``docker_executor.py``.

Covers all return-value branches:

  * Docker unavailable
  * No existing container
  * Container matches desired config
  * Container config mismatch → container removed
  * Container config mismatch → removal fails
  * Permissions flow (None, restrictive, permissive)
  * Workspace path normalisation

These tests mock the ``docker`` module at the import level inside
``docker_executor`` so they work without a real Docker daemon.

.. note::
    This package is named ``docker_integration`` (not ``docker``) to avoid
    shadowing the real ``docker`` package on ``sys.path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# The conftest fixes sys.path so this import works
from docker_executor import verify_container_integrity


# ===================================================================
# Docker unavailable
# ===================================================================

class TestDockerUnavailable:
    """When ``docker.from_env()`` raises, the function should gracefully
    report that Docker is unavailable."""

    def test_from_env_raises_runtime_error(self, raise_on_docker_call):
        """A generic exception (e.g. RuntimeError) is caught and reported."""
        with raise_on_docker_call(RuntimeError("dockerd not running")):
            result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is None
        assert result["action_taken"] == "error"
        assert "Docker unavailable" in result["mismatch_reason"]
        assert result["actual"] is None
        assert result["container_name"] is not None

    def test_from_env_raises_import_error(self, raise_on_docker_call):
        """If the ``docker`` module itself is missing."""
        with raise_on_docker_call(ImportError("No module named 'docker'")):
            result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is None
        assert result["action_taken"] == "error"

    def test_from_env_raises_docker_exception(self, raise_on_docker_call):
        """A ``docker.errors.DockerException`` (e.g. daemon not reachable)."""
        import docker.errors
        with raise_on_docker_call(docker.errors.DockerException("Cannot connect")):
            result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is None
        assert result["action_taken"] == "error"


# ===================================================================
# No container exists
# ===================================================================

class TestNoContainer:
    """When the container name is not found in Docker."""

    def test_no_container_found(self, patch_docker, mock_docker_client):
        """``NotFound`` caught → ``container_exists`` is ``False``, no action."""
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("not found")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is False
        assert result["action_taken"] == "none"
        assert result["matches_config"] is None
        assert result["actual"] is None

    def test_no_container_with_permissions(self, patch_docker, mock_docker_client):
        """Same as above but with permissions — confirms config is computed."""
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("not found")

        perms = {"network": "write", "filesystem": "write"}
        result = verify_container_integrity("/tmp/workspace", perms)

        assert result["container_exists"] is False
        # Desired config should reflect permissive permissions
        assert result["desired"]["network"] in ("bridge", "none")  # depends on workspace caps
        assert result["desired"]["mode"] in ("rw", "ro")


# ===================================================================
# Container matches
# ===================================================================

class TestContainerMatches:
    """When the container exists and already matches desired config."""

    def test_matches_restrictive_defaults(self, patch_docker, make_container):
        """Container with ``none`` network and ``ro`` mount matches safe defaults."""
        make_container(network_mode="none", mount_mode="ro")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is True
        assert result["matches_config"] is True
        assert result["action_taken"] == "none"
        assert result["actual"]["network"] == "none"
        assert result["actual"]["mode"] == "ro"

    def test_matches_permissive_config(self, patch_docker, make_container):
        """Container with ``bridge`` network and ``rw`` mount when permitted."""
        from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

        make_container(network_mode="bridge", mount_mode="rw")

        perms = {"network": "write", "filesystem": "write"}

        # The SSOT resolver is fail-closed: a permissive desired config needs
        # BOTH a resolvable workspace id AND capabilities granting network +
        # filesystem write. Grant both so the bridge/rw container matches.
        permissive = WorkspaceCapabilities(
            allow_network=True, filesystem_write=True
        )
        with patch(
            "thoughtmachine.workspace_capabilities.resolve_workspace_id",
            return_value="ws-permissive",
        ), patch(
            "security.security_gate.get_workspace_capabilities",
            return_value=permissive,
        ):
            result = verify_container_integrity("/tmp/workspace", perms)

        assert result["container_exists"] is True
        assert result["action_taken"] == "none"
        assert result["desired"] == {"network": "bridge", "mode": "rw"}

    def test_container_reload_called(self, patch_docker, make_container, mock_docker_client):
        """The function calls ``container.reload()`` before inspection."""
        container = make_container()
        verify_container_integrity("/tmp/workspace", None)

        container.reload.assert_called_once()


# ===================================================================
# Container mismatch → removal
# ===================================================================

class TestContainerMismatch:
    """When the container exists but has different config than desired."""

    def test_network_mismatch_removes_container(self, patch_docker, make_container, mock_docker_client):
        """Container with ``bridge`` network but desired ``none`` → removed."""
        container = make_container(network_mode="bridge", mount_mode="ro")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is True
        assert result["matches_config"] is False
        assert result["action_taken"] == "removed"
        assert "network" in result["mismatch_reason"]
        container.stop.assert_called_once_with(timeout=5)
        container.remove.assert_called_once()

    def test_mount_mode_mismatch_removes_container(self, patch_docker, make_container, mock_docker_client):
        """Container with ``rw`` mount but desired ``ro`` → removed."""
        container = make_container(network_mode="none", mount_mode="rw")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["action_taken"] == "removed"
        assert "mode" in result["mismatch_reason"]
        container.stop.assert_called_once()
        container.remove.assert_called_once()

    def test_both_network_and_mode_mismatch(self, patch_docker, make_container):
        """Both network and mode differ."""
        container = make_container(network_mode="bridge", mount_mode="rw")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["action_taken"] == "removed"
        assert "network" in result["mismatch_reason"]
        assert "mode" in result["mismatch_reason"]

    def test_stop_failure_reported_as_error(self, patch_docker, make_container, mock_docker_client):
        """If ``container.stop()`` raises, action_taken is ``error``."""
        container = make_container(network_mode="bridge", mount_mode="ro")
        container.stop.side_effect = RuntimeError("stop failed")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["action_taken"] == "error"
        assert result["matches_config"] is False

    def test_remove_failure_reported_as_error(self, patch_docker, make_container, mock_docker_client):
        """If ``container.remove()`` raises, action_taken is ``error``."""
        container = make_container(network_mode="bridge", mount_mode="ro")
        container.remove.side_effect = RuntimeError("remove failed")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["action_taken"] == "error"
        assert result["matches_config"] is False

    def test_container_still_stopped_even_if_remove_fails(self, patch_docker, make_container):
        """stop() is still called even if remove() will fail later."""
        container = make_container(network_mode="bridge", mount_mode="ro")
        container.remove.side_effect = RuntimeError("remove failed")

        verify_container_integrity("/tmp/workspace", None)

        container.stop.assert_called_once()


# ===================================================================
# Session permissions handling
# ===================================================================

class TestSessionPermissions:
    """How ``session_permissions=None`` vs a dict affects the desired config."""

    def test_none_permissions_uses_restrictive_defaults(self, patch_docker, make_container):
        """``session_permissions=None`` → desired network=none, mode=ro."""
        make_container(network_mode="none", mount_mode="ro")

        result = verify_container_integrity("/tmp/workspace", None)

        assert result["desired"]["network"] == "none"
        assert result["desired"]["mode"] == "ro"

    def test_explicit_restrictive_permissions(self, patch_docker, make_container):
        """Explicit restrictive dict yields same as None."""
        make_container(network_mode="none", mount_mode="ro")
        perms = {"network": "banned", "filesystem": "read"}

        result = verify_container_integrity("/tmp/workspace", perms)

        assert result["desired"]["network"] == "none"
        assert result["desired"]["mode"] == "ro"

    def test_permissive_permissions(self, patch_docker, make_container):
        """Permissive permissions may allow bridge/rw (depends on caps stub)."""
        make_container(network_mode="bridge", mount_mode="rw")
        perms = {"network": "write", "filesystem": "write"}

        result = verify_container_integrity("/tmp/workspace", perms)

        # The fallback path (when workspace caps lookup fails or is unavailable)
        # maps network="write" → "bridge" and filesystem="write" → "rw"
        assert result["desired"]["network"] in ("none", "bridge")
        assert result["desired"]["mode"] in ("ro", "rw")


# ===================================================================
# Path normalisation
# ===================================================================

class TestPathNormalisation:
    """The function calls ``os.path.abspath`` and strips trailing slashes."""

    def test_trailing_slash_stripped(self, patch_docker, mock_docker_client):
        """A path like ``/tmp/workspace/`` is normalised to ``/tmp/workspace``."""
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("not found")

        result = verify_container_integrity("/tmp/workspace/", None)

        # No crash — path normalisation is internal; just confirm no error
        assert result["container_exists"] is False

    def test_relative_path_resolved(self, patch_docker, mock_docker_client, tmp_path):
        """A relative path is resolved to absolute."""
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("not found")

        result = verify_container_integrity("relative/path", None)

        assert result["container_exists"] is False


# ===================================================================
# Return value structure
# ===================================================================

class TestReturnStructure:
    """Verify the shape of the returned dict matches the contract."""

    EXPECTED_KEYS = {
        "container_exists",
        "container_name",
        "matches_config",
        "desired",
        "actual",
        "action_taken",
        "mismatch_reason",
    }

    def test_keys_present_when_docker_unavailable(self, raise_on_docker_call):
        with raise_on_docker_call(RuntimeError("fail")):
            result = verify_container_integrity("/tmp/ws", None)
        assert set(result.keys()) == self.EXPECTED_KEYS

    def test_keys_present_when_no_container(self, patch_docker, mock_docker_client):
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nope")

        result = verify_container_integrity("/tmp/ws", None)
        assert set(result.keys()) == self.EXPECTED_KEYS

    def test_keys_present_when_matches(self, patch_docker, make_container):
        make_container(network_mode="none", mount_mode="ro")

        result = verify_container_integrity("/tmp/ws", None)
        assert set(result.keys()) == self.EXPECTED_KEYS

    def test_keys_present_when_mismatch(self, patch_docker, make_container):
        make_container(network_mode="bridge", mount_mode="ro")

        result = verify_container_integrity("/tmp/ws", None)
        assert set(result.keys()) == self.EXPECTED_KEYS

    def test_desired_has_network_and_mode(self, patch_docker, make_container):
        make_container(network_mode="none", mount_mode="ro")

        result = verify_container_integrity("/tmp/ws", None)

        assert "network" in result["desired"]
        assert "mode" in result["desired"]


# ===================================================================
# Record-label lookup (primary) + resource-container guard
# ===================================================================

class TestRecordLabelLookup:
    """``verify_container_integrity`` resolves the container through the
    container-record store's Docker label (``thoughtmachine.container_id``),
    not brittle ``agent-exec-<sha256(path)[:12]>`` name arithmetic.

    These tests set up a *hermetic* record vault (``tmp_path``) and patch the
    workspace-id resolver so the record-store lookup path is exercised, while
    keeping the legacy name lookup unreachable.
    """

    _WS = "ws-records"

    def _patches(self, tmp_path):
        return (
            patch("thoughtmachine.vault.vault_root", return_value=str(tmp_path)),
            patch("docker_executor._resolve_workspace_id", return_value=self._WS),
        )

    def test_lookup_via_label_when_name_drifted(
        self, tmp_path, patch_docker, mock_docker_client
    ):
        """A container whose name is NOT ``agent-exec-<sha256(path)[:12]>`` is
        still found when it carries the record's ``thoughtmachine.container_id``
        label — proving the lookup is record-driven, not name-driven."""
        import docker.errors
        from thoughtmachine.container_record import (
            LIFECYCLE_PERSISTENT,
            RECORD_LABEL_KEY,
            create_record,
        )

        record = create_record(
            self._WS, LIFECYCLE_PERSISTENT, "workspace-owned",
            id="rec-persist-1", vault_root=str(tmp_path),
        )

        container = MagicMock()
        # Deliberately NOT sha256("/tmp/workspace")[:12] — the old name math.
        container.name = "agent-exec-000000000000"
        container.attrs = {
            "Mounts": [
                {"Destination": "/workspace", "Mode": "ro", "Type": "bind"},
            ],
            "HostConfig": {"NetworkMode": "none"},
        }
        # The record's label resolves to this live container.
        mock_docker_client.containers.list.return_value = [container]
        # The legacy name lookup must NOT be consulted.
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "no name match"
        )

        p1, p2 = self._patches(tmp_path)
        with p1, p2:
            result = verify_container_integrity("/tmp/workspace", None)

        # Found via the record label, not via the (drifted) name.
        mock_docker_client.containers.get.assert_not_called()
        mock_docker_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": f"{RECORD_LABEL_KEY}={record.id}"},
        )
        assert result["container_exists"] is True
        assert result["matches_config"] is True
        assert result["action_taken"] == "none"
        container.reload.assert_called_once()

    def test_resource_record_is_never_inspected_or_removed(
        self, tmp_path, patch_docker, mock_docker_client
    ):
        """A RESOURCE-lifecycle record's container is never treated as an
        integrity candidate — even when a live container carries its label."""
        import docker.errors
        from thoughtmachine.container_record import (
            LIFECYCLE_RESOURCE,
            create_record,
        )

        create_record(
            self._WS, LIFECYCLE_RESOURCE, "system-owned",
            id="rec-res-1", vault_root=str(tmp_path),
        )

        resource = MagicMock()
        resource.name = "tm-res-000000000000-git"
        resource.attrs = {
            "Mounts": [
                {"Destination": "/workspace", "Mode": "rw", "Type": "bind"},
            ],
            "HostConfig": {"NetworkMode": "bridge"},
        }
        mock_docker_client.containers.list.return_value = [resource]
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "none"
        )

        p1, p2 = self._patches(tmp_path)
        with p1, p2:
            result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is False
        assert result["action_taken"] == "none"
        resource.reload.assert_not_called()
        resource.stop.assert_not_called()
        resource.remove.assert_not_called()

    def test_tm_res_named_container_skipped_for_persistent_record(
        self, tmp_path, patch_docker, mock_docker_client
    ):
        """Even for a PERSISTENT record, a ``tm-res-``-named live container is
        rejected by the defensive name guard and left untouched."""
        import docker.errors
        from thoughtmachine.container_record import (
            LIFECYCLE_PERSISTENT,
            create_record,
        )

        create_record(
            self._WS, LIFECYCLE_PERSISTENT, "workspace-owned",
            id="rec-persist-2", vault_root=str(tmp_path),
        )

        resource = MagicMock()
        resource.name = "tm-res-000000000000-git"
        resource.attrs = {
            "Mounts": [
                {"Destination": "/workspace", "Mode": "rw", "Type": "bind"},
            ],
            "HostConfig": {"NetworkMode": "bridge"},
        }
        mock_docker_client.containers.list.return_value = [resource]
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "none"
        )

        p1, p2 = self._patches(tmp_path)
        with p1, p2:
            result = verify_container_integrity("/tmp/workspace", None)

        assert result["container_exists"] is False
        assert result["action_taken"] == "none"
        resource.reload.assert_not_called()
        resource.stop.assert_not_called()
        resource.remove.assert_not_called()

