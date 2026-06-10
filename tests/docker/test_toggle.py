"""
test_toggle.py — Stateful toggle tests for DockerExecutor.

Verifies that when ``session_permissions`` changes on a live ``DockerExecutor``
instance, the old container is stopped/removed and a new one is created with
the updated config.

Scenarios tested:
  - network: write → banned   (bridge → none)
  - network: banned → write   (none → bridge)
  - filesystem: write → read  (rw → ro)
  - filesystem: read → write  (ro → rw)
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures: mock container
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
#  Tests
# ══════════════════════════════════════════════════════════════════════════


class TestNetworkToggle:
    """Stateful toggle of network permission."""

    def _assert_container_network(self, container: MagicMock, expected: str):
        actual = container.attrs["HostConfig"]["NetworkMode"]
        assert actual == expected, f"NetworkMode: expected {expected!r}, got {actual!r}"

    def test_write_to_banned(self, mock_docker: MagicMock, mock_container: MagicMock):
        """write (bridge) → banned (none): old container removed, new one created."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )
        # Mock _compute_container_config to avoid gate dependency in unit test
        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "rw"), ("none", "ro"), ("none", "ro")],
        ):
            # First call → creates container with bridge
            executor._ensure_container()
            self._assert_container_network(mock_container, "bridge")
    
            # Change permissions
            executor.session_permissions = {
                "network": "banned", "filesystem": "read", "container": True,
            }
    
            # Second call → should replace container with none
            executor._ensure_container()
        # Old container should have been stopped and removed
        assert mock_container.stop.called, "Old container was not stopped"
        assert mock_container.remove.called, "Old container was not removed"
        # New container should have been created with network=none
        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs.get("network") == "none", (
            f"New container network: expected 'none', got {kwargs.get('network')!r}"
        )

    def test_banned_to_write(self, mock_docker: MagicMock, mock_container: MagicMock):
        """banned (none) → write (bridge)."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "banned", "filesystem": "read", "container": True},
        )
        # First call, container is on "none" — set mock attrs to match
        mock_container.attrs["HostConfig"]["NetworkMode"] = "none"

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("none", "ro"), ("bridge", "rw"), ("bridge", "rw")],
        ):
            executor._ensure_container()
            self._assert_container_network(mock_container, "none")

            executor.session_permissions = {
                "network": "write", "filesystem": "write", "container": True,
            }

            executor._ensure_container()

        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs.get("network") == "bridge"


class TestFilesystemToggle:
    """Stateful toggle of filesystem permission."""

    def _assert_container_mode(self, container: MagicMock, expected: str):
        mounts = container.attrs.get("Mounts", [])
        actual = "ro"
        for m in mounts:
            if m.get("Destination") == "/workspace":
                actual = m.get("Mode", "ro")
                break
        assert actual == expected, f"workspace mount mode: expected {expected!r}, got {actual!r}"

    def test_write_to_read(self, mock_docker: MagicMock, mock_container: MagicMock):
        """filesystem write (rw) → read (ro)."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "rw"), ("bridge", "ro"), ("bridge", "ro")],
        ):
            executor._ensure_container()
            self._assert_container_mode(mock_container, "rw")
    
            executor.session_permissions = {
                "network": "write", "filesystem": "read", "container": True,
            }
            executor._ensure_container()
        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        vol_cfg = kwargs.get("volumes", {})
        mode = vol_cfg.get("/tmp/test-workspace", {}).get("mode", "?")
        assert mode == "ro", f"New container volume mode: expected 'ro', got {mode!r}"

    def test_read_to_write(self, mock_docker: MagicMock, mock_container: MagicMock):
        """filesystem read (ro) → write (rw)."""
        mock_container.attrs["Mounts"][0]["Mode"] = "ro"

        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "read", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "ro"), ("bridge", "rw"), ("bridge", "rw")],
        ):
            executor._ensure_container()
    
            executor.session_permissions = {                "network": "write", "filesystem": "write", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        vol_cfg = kwargs.get("volumes", {})
        mode = vol_cfg.get("/tmp/test-workspace", {}).get("mode", "?")
        assert mode == "rw", f"New container volume mode: expected 'rw', got {mode!r}"
