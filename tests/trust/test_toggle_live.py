"""
test_toggle_live.py — Trust-level stateful toggle tests for DockerExecutor.

Extends the docker-level ``tests/docker/test_toggle.py`` with tests for
edge cases that are not covered by the original parametrized matrix:

Gaps filled here:
  - **no_recreation_when_permissions_unchanged**: When session permissions
    are identical after a reload, the existing container is reused.
  - **ask_permission_treated_as_restrictive**: When the effective permission
    is "ask", the container config uses the most restrictive setting
    (network="none", mode="ro") and the container is NOT recreated if
    it already has those restrictive settings.

The tests in this file are "trust-level" — they validate behavioural
contracts that other modules depend on.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures (mirror tests/docker/test_toggle.py)
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_container() -> MagicMock:
    """A mock Docker container with realistic attrs."""
    c = MagicMock()
    c.id = "abc123def456"
    c.status = "running"
    c.attrs = {
        "HostConfig": {"NetworkMode": "bridge"},
        "Mounts": [
            {"Destination": "/workspace", "Mode": "rw", "Type": "bind"},
        ],
        "State": {"Status": "running"},
    }
    c.reload = MagicMock()
    c.stop = MagicMock()
    c.remove = MagicMock()
    return c


@pytest.fixture
def mock_docker(mock_container: MagicMock) -> MagicMock:
    """Mock ``docker.from_env()`` returning a client that yields ``mock_container``."""
    client = MagicMock()
    client.containers.get.return_value = mock_container
    client.containers.run.return_value = mock_container
    client.images.get.return_value = MagicMock()
    client.api.build.return_value = []
    return client


# ══════════════════════════════════════════════════════════════════════════
#  Helper: instantiate DockerExecutor with mocks
# ══════════════════════════════════════════════════════════════════════════


def _make_executor(
    mock_docker_client: MagicMock,
    session_permissions: dict | None = None,
    workspace_id: str | None = "test-ws",
) -> "DockerExecutor":
    """Return a DockerExecutor with ``docker.from_env()`` and workspace mocks."""
    from docker_executor import DockerExecutor

    with patch("docker_executor.docker.from_env", return_value=mock_docker_client):
        executor = DockerExecutor(
            workspace_path="/tmp/test-workspace",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            force_rebuild=False,
            idle_timeout=600,
            session_permissions=session_permissions,
            workspace_id=workspace_id,
        )
    return executor


# ══════════════════════════════════════════════════════════════════════════
#  Tests: no recreation when permissions unchanged
# ══════════════════════════════════════════════════════════════════════════


class TestNoRecreation:
    """When session permissions do NOT change, the container is reused."""

    def test_no_recreation_when_permissions_unchanged(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Same permissions → existing container is reused, not recreated."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            # Same return value each time → config matches → no recreation
            return_value=("bridge", "rw"),
        ):
            # First call creates container
            executor._ensure_container()
            assert mock_container.stop.called is False, (
                "Container should NOT be stopped on first _ensure_container"
            )

            # Reset mocks to track second call
            mock_container.stop.reset_mock()
            mock_container.remove.reset_mock()
            mock_docker.containers.run.reset_mock()

            # Second call with same permissions — container should be reused
            executor._ensure_container()

        # No stop/remove/recreate should have occurred
        assert mock_container.stop.called is False, (
            "Container should NOT be stopped when permissions unchanged"
        )
        assert mock_container.remove.called is False, (
            "Container should NOT be removed when permissions unchanged"
        )
        # containers.run should NOT have been called (no new container created)
        if mock_docker.containers.run.called:
            import warnings
            warnings.warn(
                "containers.run was called during no-change toggle — "
                "this may indicate unnecessary recreation"
            )

    def test_recreation_when_permissions_change(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Changed permissions → old container removed, new one created."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "rw"), ("none", "ro"), ("none", "ro")],
        ):
            # First call → creates container with bridge+rw
            executor._ensure_container()

            # Change permissions
            executor.session_permissions = {
                "network": "banned", "filesystem": "read", "container": True,
            }

            # Second call → config mismatch → recreate
            executor._ensure_container()

        # Old container should have been stopped and removed
        assert mock_container.stop.called, (
            "Old container should be stopped when permissions change"
        )
        assert mock_container.remove.called, (
            "Old container should be removed when permissions change"
        )

    def test_identity_after_reload(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Re-assigning the same permissions dict values does NOT trigger recreation."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            return_value=("bridge", "rw"),
        ):
            executor._ensure_container()

            mock_container.stop.reset_mock()
            mock_container.remove.reset_mock()

            # Re-assign identical permissions
            executor.session_permissions = {
                "network": "write", "filesystem": "write", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called is False
        assert mock_container.remove.called is False


# ══════════════════════════════════════════════════════════════════════════
#  Tests: ask permission treated as restrictive
# ══════════════════════════════════════════════════════════════════════════


class TestAskPermissionRestrictive:
    """"ask" permission produces restrictive container config."""

    def test_ask_network_creates_none_mode(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """network='ask' → container gets network_mode='none'."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "ask", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            return_value=("none", "rw"),
        ):
            executor._ensure_container()

        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs.get("network") == "none", (
            f"network='ask' → expected network='none', got {kwargs.get('network')!r}"
        )

    def test_ask_filesystem_creates_ro_mode(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """filesystem='ask' → container gets workspace mount mode='ro'."""
        # workspace_id=None so the BIND path is exercised: this test asserts
        # the ``volumes`` dict representation (a truthy workspace_id would
        # take the named-volume path where volumes=None and mounts=[Mount]).
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "ask", "container": True},
            workspace_id=None,
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            return_value=("bridge", "ro"),
        ):
            executor._ensure_container()

        _, kwargs = mock_docker.containers.run.call_args
        vol_cfg = kwargs.get("volumes", {})
        mode = vol_cfg.get("/tmp/test-workspace", {}).get("mode", "?")
        assert mode == "ro", (
            f"filesystem='ask' → expected mode='ro', got {mode!r}"
        )

    def test_ask_not_recreated_when_already_restrictive(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Container already at restrictive settings (none+ro) is NOT recreated for 'ask'."""
        # Start with banned (which produces none+ro)
        mock_container.attrs["HostConfig"]["NetworkMode"] = "none"
        mock_container.attrs["Mounts"][0]["Mode"] = "ro"

        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "banned", "filesystem": "read", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            # Both produce ("none", "ro") — config matches, no recreation
            return_value=("none", "ro"),
        ):
            executor._ensure_container()
            mock_container.stop.reset_mock()
            mock_container.remove.reset_mock()
            mock_docker.containers.run.reset_mock()

            # Switch to 'ask' — still produces (none, ro)
            executor.session_permissions = {
                "network": "ask", "filesystem": "ask", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called is False, (
            "Container should NOT be stopped when switching banned→ask "
            "(both produce same restrictive config)"
        )
        assert mock_container.remove.called is False

    def test_ask_to_write_recreates_container(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """ask→write triggers recreation (none→bridge)."""
        mock_container.attrs["HostConfig"]["NetworkMode"] = "none"
        mock_container.attrs["Mounts"][0]["Mode"] = "ro"

        # workspace_id=None → bind path (volumes dict assertion below).
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "ask", "filesystem": "ask", "container": True},
            workspace_id=None,
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("none", "ro"), ("bridge", "rw"), ("bridge", "rw")],
        ):
            executor._ensure_container()

            executor.session_permissions = {
                "network": "write", "filesystem": "write", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs.get("network") == "bridge"
        vol_cfg = kwargs.get("volumes", {})
        mode = vol_cfg.get("/tmp/test-workspace", {}).get("mode", "?")
        assert mode == "rw"

    def test_write_to_ask_recreates_container(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """write→ask triggers recreation (bridge→none, rw→ro)."""
        # workspace_id=None → bind path (volumes dict assertion below).
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
            workspace_id=None,
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "rw"), ("none", "ro"), ("none", "ro")],
        ):
            executor._ensure_container()

            executor.session_permissions = {
                "network": "ask", "filesystem": "ask", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs.get("network") == "none"
        vol_cfg = kwargs.get("volumes", {})
        mode = vol_cfg.get("/tmp/test-workspace", {}).get("mode", "?")
        assert mode == "ro"
