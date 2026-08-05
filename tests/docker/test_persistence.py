"""
Persistence integration test for named Docker volumes.

Tests that:
  1. A named volume survives container stop + remove
  2. pip-installed packages (installed into the workspace volume via
     --target) persist across container restarts
  3. Files written to /workspace persist across container restarts

Requires a real Docker daemon. Skipped if Docker is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import docker
import pytest

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from docker_executor import ensure_workspace_volume_populated  # noqa: E402


# ── Docker availability check ──────────────────────────────────────────

_DOCKER_AVAILABLE: bool = False
"""Cached result of the Docker daemon check."""


def docker_available() -> bool:
    """Return True if a real Docker daemon is reachable."""
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE:
        return True
    try:
        client = docker.from_env()
        client.ping()
        _DOCKER_AVAILABLE = True
        return True
    except Exception:
        return False


needs_docker = pytest.mark.skipif(
    not docker_available(),
    reason="Docker daemon is not available",
)


# ── Test ───────────────────────────────────────────────────────────────


@needs_docker
class TestVolumePersistence:
    """Verify that data written to a named volume survives container lifecycle."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        """Create a temporary workspace dir and build a minimal image."""
        self.workspace_dir = tmp_path / "ws"
        self.workspace_dir.mkdir()

        # Write a minimal Dockerfile
        dockerfile = self.workspace_dir / "Dockerfile"
        dockerfile.write_text(
            "FROM python:3.11-slim\n"
            "RUN adduser --uid 1000 --disabled-password --gecos '' agent\n"
            "USER agent\n"
            'CMD ["tail", "-f", "/dev/null"]\n'
        )

        self.client = docker.from_env()
        self.workspace_id = "test-persistence-001"
        self.volume_name = f"tm-workspace-{self.workspace_id}"

        # Build image
        image_tag = "tm-test-persistence:latest"
        image, build_log = self.client.images.build(
            path=str(self.workspace_dir),
            tag=image_tag,
            rm=True,
        )
        self.image_tag = image_tag

        yield

        # Teardown: remove containers, volume, image
        try:
            self.client.containers.get(
                f"tm-test-persist-{self.workspace_id}"
            ).remove(force=True)
        except (docker.errors.NotFound, docker.errors.APIError):
            pass
        try:
            self.client.volumes.get(self.volume_name).remove(force=True)
        except (docker.errors.NotFound, docker.errors.APIError):
            pass
        try:
            self.client.images.get(image_tag).remove(force=True)
        except (docker.errors.NotFound, docker.errors.APIError):
            pass

    def _ensure_volume(self):
        """Create the named volume if it doesn't exist."""
        try:
            vol = self.client.volumes.get(self.volume_name)
        except docker.errors.NotFound:
            vol = self.client.volumes.create(self.volume_name)
        return vol

    def _create_container(self):
        """Create a new container using the named volume."""
        container_name = f"tm-test-persist-{self.workspace_id}"
        try:
            existing = self.client.containers.get(container_name)
            existing.remove(force=True)
        except docker.errors.NotFound:
            pass

        vol = self._ensure_volume()
        # Raw-SDK mount: a freshly created named volume is root-owned (0755),
        # so the container (uid 1000:1000) cannot write to it. Populate/chown
        # it via the product helper before mounting — this mirrors what the
        # real executor does in its create path.
        ensure_workspace_volume_populated(
            self.client,
            self.image_tag,
            str(self.workspace_dir),
            self.volume_name,
            network_mode="none",
        )
        container = self.client.containers.run(
            image=self.image_tag,
            name=container_name,
            mounts=[
                docker.types.Mount(
                    target="/workspace",
                    source=self.volume_name,
                    type="volume",
                    read_only=False,
                ),
            ],
            detach=True,
            tty=True,
            stdin_open=True,
            command=["tail", "-f", "/dev/null"],
            user="1000:1000",
        )
        return container

    def _exec(self, container, cmd: str, env=None) -> tuple[str, str, int]:
        """Run a command inside the container and return (stdout, stderr, exit_code).

        NOTE: ``cmd`` is handed to docker-py's ``exec_run``, which
        shlex-splits strings into argv — no shell is involved, so shell
        syntax (``&&``, ``;``, pipes, ``VAR=x`` prefixes) must not be used.
        Pass environment variables via ``env`` instead.
        """
        exit_code, output = container.exec_run(cmd, environment=env)
        stdout = output.decode() if isinstance(output, bytes) else str(output)
        return stdout, "", exit_code

    def test_pip_install_survives_container_restart(self):
        """Install a package into the volume, destroy the container, recreate, verify it's still there."""
        # ── First container ────────────────────────────────────────────
        container1 = self._create_container()
        time.sleep(1)  # let container fully start

        # Install into the workspace volume (--target): a plain `pip install`
        # would write to the container's writable layer, which is destroyed
        # when container1 is removed below.
        stdout, _, rc = self._exec(
            container1, "pip install --target /workspace/pylibs requests"
        )
        assert rc == 0, f"pip install failed: {stdout}"

        # Write a file to the workspace
        stdout, _, rc = self._exec(container1, "touch /workspace/testfile")
        assert rc == 0, f"touch failed: {stdout}"

        # Verify file exists
        stdout, _, rc = self._exec(container1, "cat /workspace/testfile")
        assert rc == 0, f"File not found immediately after creation: {stdout}"

        # ── Destroy first container ────────────────────────────────────
        container1.stop()
        container1.remove()

        # ── Second container ───────────────────────────────────────────
        container2 = self._create_container()
        time.sleep(1)

        # Verify the package is still installed
        stdout, _, rc = self._exec(
            container2,
            "python -c 'import requests; print(\"OK\")'",
            env={"PYTHONPATH": "/workspace/pylibs"},
        )
        assert rc == 0, f"Package import failed: {stdout}"
        assert "OK" in stdout, f"Package import output missing 'OK': {stdout}"

        # Verify the test file still exists
        stdout, _, rc = self._exec(container2, "cat /workspace/testfile")
        assert rc == 0, (
            f"/workspace/testfile missing after container restart: stdout={stdout!r}"
        )

        # Clean up second container
        container2.stop()
        container2.remove()
