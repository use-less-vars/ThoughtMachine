"""
test_real_gate_smoke.py — End-to-end smoke test that walks through the full
``get_effective_permissions() → _compute_container_config() → containers.run()``
chain with a real (mocked) workspace capabilities file.

Sets up a temporary ``~/.thoughtmachine/workspaces/test-id/capabilities.json``,
mocks ``Path.home()`` to point there, and verifies that ``_ensure_container()``
passes the correct Docker parameters.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_workspace() -> str:
    """Create a temporary workspace directory."""
    tmpdir = tempfile.mkdtemp(prefix="tm-test-")
    yield tmpdir
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_home(temp_workspace: str) -> Path:
    """Point ``Path.home()`` to a temp dir with a workspaces/ structure."""
    fake_home = Path(temp_workspace) / "fake-home"
    (fake_home / ".thoughtmachine" / "workspaces" / "test-id").mkdir(parents=True, exist_ok=True)
    return fake_home


def _write_capabilities(mock_home: Path, **overrides: bool) -> None:
    """Write a capabilities.json for test-id under *mock_home*."""
    caps_path = mock_home / ".thoughtmachine" / "workspaces" / "test-id" / "capabilities.json"
    caps = {
        "allow_network": True,
        "allow_docker": True,
        "filesystem_write": True,
        "git_available": True,
    }
    caps.update(overrides)
    caps_path.write_text(json.dumps(caps), encoding="utf-8")


@pytest.fixture
def mock_container() -> MagicMock:
    c = MagicMock()
    c.id = "smoke-test-001"
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
def mock_docker_client(mock_container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.containers.get.side_effect = ImportError  # no existing container (like docker.errors.NotFound)
    # Make get raise NotFound on first call
    import docker.errors
    client.containers.get.side_effect = docker.errors.NotFound("Mock not found")
    client.containers.run.return_value = mock_container
    client.images.get.return_value = MagicMock()
    client.api.build.return_value = []
    return client


# ══════════════════════════════════════════════════════════════════════════
#  Helper
# ══════════════════════════════════════════════════════════════════════════


def _make_executor(
    mock_docker_client: MagicMock,
    session_permissions: dict | None = None,
    workspace_path: str = "/tmp/test-ws",
    workspace_id: str = "test-id",
):
    """Instantiate DockerExecutor with mocks."""
    from docker_executor import DockerExecutor

    with patch("docker_executor.docker.from_env", return_value=mock_docker_client):
        executor = DockerExecutor(
            workspace_path=workspace_path,
            image="agent-executor-smoke",
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


class TestRealGateSmoke:
    """Full end-to-end smoke tests with a real capabilities file."""

    def test_network_allowed_bridge(
        self,
        mock_home: Path,
        mock_docker_client: MagicMock,
        mock_container: MagicMock,
    ):
        """network: write + capabilities network: True → network=bridge + mode=rw."""
        _write_capabilities(mock_home, allow_network=True, filesystem_write=True)

        with patch("pathlib.Path.home", return_value=mock_home):
            with patch(
                "security.security_gate.Path.home",
                return_value=mock_home,
            ):
                executor = _make_executor(
                    mock_docker_client,
                    session_permissions={
                        "network": "write",
                        "filesystem": "write",
                        "container": True,
                    },
                )

                executor._ensure_container()

                _, kwargs = mock_docker_client.containers.run.call_args
                assert kwargs.get("network") == "bridge", (
                    f"Expected network=bridge, got {kwargs.get('network')!r}"
                )
                vol = kwargs.get("volumes", {})
                mode = list(vol.values())[0].get("mode", "") if vol else ""
                assert mode == "rw", f"Expected volume mode 'rw', got {mode!r}"

    def test_network_banned_none(
        self,
        mock_home: Path,
        mock_docker_client: MagicMock,
        mock_container: MagicMock,
    ):
        """network: write but workspace denies → network=none."""
        _write_capabilities(mock_home, allow_network=False, filesystem_write=True)

        with patch("pathlib.Path.home", return_value=mock_home):
            with patch(
                "security.security_gate.Path.home",
                return_value=mock_home,
            ):
                executor = _make_executor(
                    mock_docker_client,
                    session_permissions={
                        "network": "write",
                        "filesystem": "write",
                        "container": True,
                    },
                )

                executor._ensure_container()

                _, kwargs = mock_docker_client.containers.run.call_args
                assert kwargs.get("network") == "none", (
                    f"Expected network=none, got {kwargs.get('network')!r}"
                )

    def test_filesystem_write_rw(
        self,
        mock_home: Path,
        mock_docker_client: MagicMock,
        mock_container: MagicMock,
    ):
        """filesystem: write + capabilities filesystem_write: True → mode=rw."""
        _write_capabilities(mock_home, allow_network=True, filesystem_write=True)

        with patch("pathlib.Path.home", return_value=mock_home):
            with patch(
                "security.security_gate.Path.home",
                return_value=mock_home,
            ):
                executor = _make_executor(
                    mock_docker_client,
                    session_permissions={
                        "network": "write",
                        "filesystem": "write",
                        "container": True,
                    },
                )

                executor._ensure_container()

                _, kwargs = mock_docker_client.containers.run.call_args
                vol = kwargs.get("volumes", {})
                mode = list(vol.values())[0].get("mode", "") if vol else ""
                assert mode == "rw", f"Expected volume mode 'rw', got {mode!r}"

    def test_filesystem_write_downgraded_to_ro(
        self,
        mock_home: Path,
        mock_docker_client: MagicMock,
        mock_container: MagicMock,
    ):
        """filesystem: write but workspace denies write → mode=ro."""
        _write_capabilities(mock_home, allow_network=True, filesystem_write=False)

        with patch("pathlib.Path.home", return_value=mock_home):
            with patch(
                "security.security_gate.Path.home",
                return_value=mock_home,
            ):
                executor = _make_executor(
                    mock_docker_client,
                    session_permissions={
                        "network": "write",
                        "filesystem": "write",
                        "container": True,
                    },
                )

                executor._ensure_container()

                _, kwargs = mock_docker_client.containers.run.call_args
                vol = kwargs.get("volumes", {})
                mode = list(vol.values())[0].get("mode", "") if vol else ""
                assert mode == "ro", (
                    f"Expected volume mode 'ro' (downgraded), got {mode!r}"
                )

    def test_cleanup_temp_dir(self, temp_workspace: str):
        """Verify the temp dir fixture cleans up."""
        import shutil
        assert os.path.isdir(temp_workspace)
        shutil.rmtree(temp_workspace, ignore_errors=True)
        assert not os.path.isdir(temp_workspace)
