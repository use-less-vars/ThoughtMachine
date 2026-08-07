"""
Persistence integration test for the Phase-2 mount model.

Tests that:
  1. The host workspace directory is bind-mounted at /workspace: files
     written inside a container land in the host dir and survive container
     stop + remove (recreate with the same workspace dir).
  2. User-site packages (``pip install --user`` with PYTHONUSERBASE pointing
     at /home/agent/.local) are backed by a per-workspace named volume
     ``tm-packages-<workspace_id>``: they survive container stop + remove
     and are visible to a new container created with the same workspace_id.

Requires a real Docker daemon. Skipped if Docker is unavailable.
"""

from __future__ import annotations

import os
import sys
import time

import docker
import pytest

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)


# ── Docker availability check ───────────────────────────────────────────────

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


# ── Test ────────────────────────────────────────────────────────────────────


@needs_docker
class TestVolumePersistence:
    """Verify workspace + package persistence across container lifecycles."""

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
        # Phase-2: per-workspace package volume (mirrors docker_executor's
        # tm-packages-<workspace_id> volume mounted at /home/agent/.local).
        self.pkg_volume_name = f"tm-packages-{self.workspace_id}"

        # Build image
        image_tag = "tm-test-persistence:latest"
        self.client.images.build(
            path=str(self.workspace_dir),
            tag=image_tag,
            rm=True,
        )
        self.image_tag = image_tag

        yield

        # Teardown: remove containers, package volume, image
        try:
            self.client.containers.get(
                f"tm-test-persist-{self.workspace_id}"
            ).remove(force=True)
        except (docker.errors.NotFound, docker.errors.APIError):
            pass
        try:
            self.client.volumes.get(self.pkg_volume_name).remove(force=True)
        except (docker.errors.NotFound, docker.errors.APIError):
            pass
        try:
            self.client.images.get(image_tag).remove(force=True)
        except (docker.errors.NotFound, docker.errors.APIError):
            pass

    def _create_container(self):
        """Create a container with the Phase-2 mount layout.

        - host workspace dir bind-mounted at /workspace (rw)
        - named package volume ``tm-packages-<workspace_id>`` at
          /home/agent/.local (auto-created by Docker on first mount; the
          same name across restarts means the same persistent volume)
        - PYTHONUSERBASE=/home/agent/.local so ``pip install --user`` lands
          inside the package volume
        """
        container_name = f"tm-test-persist-{self.workspace_id}"
        try:
            existing = self.client.containers.get(container_name)
            existing.remove(force=True)
        except docker.errors.NotFound:
            pass

        container = self.client.containers.run(
            image=self.image_tag,
            name=container_name,
            mounts=[
                docker.types.Mount(
                    target="/workspace",
                    source=str(self.workspace_dir),
                    type="bind",
                    read_only=False,
                ),
                docker.types.Mount(
                    target="/home/agent/.local",
                    source=self.pkg_volume_name,
                    type="volume",
                ),
            ],
            detach=True,
            tty=True,
            stdin_open=True,
            command=["tail", "-f", "/dev/null"],
            user="1000:1000",
            environment=["PYTHONUSERBASE=/home/agent/.local"],
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

    def test_workspace_files_survive_container_restart(self):
        """A file written to /workspace survives stop + remove (host dir persists)."""
        # ── First container ────────────────────────────────────────────────
        container1 = self._create_container()
        time.sleep(1)  # let container fully start

        stdout, _, rc = self._exec(container1, "touch /workspace/testfile")
        assert rc == 0, f"touch failed: {stdout}"

        stdout, _, rc = self._exec(container1, "cat /workspace/testfile")
        assert rc == 0, f"File not found immediately after creation: {stdout}"

        # ── Destroy first container ────────────────────────────────────────
        container1.stop()
        container1.remove()

        # ── Second container: same host workspace dir (bind) ───────────────
        container2 = self._create_container()
        time.sleep(1)

        stdout, _, rc = self._exec(container2, "cat /workspace/testfile")
        assert rc == 0, (
            f"/workspace/testfile missing after container restart: stdout={stdout!r}"
        )

        container2.stop()
        container2.remove()

    def test_user_packages_persist_via_package_volume(self):
        """pip install --user lands in tm-packages-<id> and survives restart."""
        # ── First container ────────────────────────────────────────────────
        container1 = self._create_container()
        time.sleep(1)

        stdout, _, rc = self._exec(container1, "pip install --user six")
        assert rc == 0, f"pip install --user six failed: {stdout}"

        # ── Destroy first container ────────────────────────────────────────
        container1.stop()
        container1.remove()

        # ── Second container: same workspace_id -> same package volume ─────
        container2 = self._create_container()
        time.sleep(1)

        stdout, _, rc = self._exec(container2, "python -c 'import six; print(\"OK\")'")
        assert rc == 0, f"Package import failed: {stdout}"
        assert "OK" in stdout, f"Package import output missing 'OK': {stdout}"

        container2.stop()
        container2.remove()
