"""
Unit tests for the auto image-build logic in ``infra.resource_container_manager``
(``_ensure_resource_image`` / ``is_resource_image_available`` and the
``ensure_container`` guard).

The resource image Dockerfile lives in the VAULT
(``~/.thoughtmachine/docker/resource/Dockerfile``, seeded from
``resources/resource_dockerfile.txt``) so the image definition is NOT
agent-writable; ``_ensure_resource_image`` builds the ``tm-resource-git``
image from that vault directory on demand (single-flight, success-cached,
never raising).

These are PURE unit tests: no Docker daemon and no docker SDK required (the
module imports docker defensively via try/except, and every test replaces
``rcm.docker`` with a fake or ``None``).
"""

import os
import sys
import threading

# Make the repository root importable when running `pytest tests/docker/` or
# this file directly (tests/docker has no conftest.py of its own).
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

import pytest

try:
    from docker.errors import ImageNotFound
except ImportError:  # pragma: no cover - environment without docker SDK
    class ImageNotFound(Exception):
        pass

import infra.resource_container_manager as rcm


class _FakeImage:
    """Stand-in for a docker image object; intentionally has no attributes."""


class _FakeImages:
    def __init__(self, present, build_error=None, get_error=None):
        self.present = present
        self.build_error = build_error
        self.get_error = get_error
        self.build_calls = []

    def get(self, tag):
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage()

    def build(self, **kwargs):
        if self.build_error is not None:
            raise self.build_error
        self.build_calls.append(kwargs)
        self.present = True  # simulate docker: the image exists after a build
        return _FakeImage(), iter([])


class _FakeClient:
    def __init__(self, images):
        self.images = images


class _FakeDockerModule:
    def __init__(self, images, from_env_error=None, from_env_returns_none=False):
        self.images = images
        self.from_env_error = from_env_error
        self.from_env_returns_none = from_env_returns_none
        self.from_env_calls = 0

    def from_env(self):
        self.from_env_calls += 1
        if self.from_env_error is not None:
            raise self.from_env_error
        if self.from_env_returns_none:
            return None
        return _FakeClient(self.images)


@pytest.fixture(autouse=True)
def _reset_image_ready():
    """Each test starts and ends with a cold image-readiness cache."""
    rcm._RESOURCE_IMAGE_READY = False
    yield
    rcm._RESOURCE_IMAGE_READY = False


class TestEnsureResourceImage:
    def test_image_present_no_build(self, monkeypatch):
        images = _FakeImages(present=True)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert images.build_calls == []
        assert rcm._RESOURCE_IMAGE_READY is True

    def test_image_missing_builds_from_vault_dockerfile(self, monkeypatch):
        images = _FakeImages(present=False)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 1
        kwargs = images.build_calls[0]
        assert kwargs["path"] == str(rcm.RESOURCE_IMAGE_DOCKERFILE_DIR)
        assert kwargs["dockerfile"] == "Dockerfile"
        assert kwargs["tag"] == rcm.RESOURCE_IMAGE_TAG
        assert kwargs["rm"] is True
        assert rcm._RESOURCE_IMAGE_READY is True

    def test_from_env_raises_returns_false_no_build(self, monkeypatch):
        images = _FakeImages(present=False)
        monkeypatch.setattr(
            rcm, "docker",
            _FakeDockerModule(images, from_env_error=RuntimeError("no daemon")),
        )
        assert rcm._ensure_resource_image() is False
        assert images.build_calls == []
        assert rcm._RESOURCE_IMAGE_READY is False

    def test_docker_unavailable_returns_false(self, monkeypatch):
        monkeypatch.setattr(rcm, "docker", None)
        assert rcm._ensure_resource_image() is False
        assert rcm._RESOURCE_IMAGE_READY is False

    def test_concurrent_callers_single_build(self, monkeypatch):
        images = _FakeImages(present=False)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(rcm._ensure_resource_image())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [True] * 8
        assert len(images.build_calls) == 1


class TestIsResourceImageAvailable:
    def test_present(self, monkeypatch):
        images = _FakeImages(present=True)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm.is_resource_image_available() is True

    def test_absent(self, monkeypatch):
        images = _FakeImages(present=False)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm.is_resource_image_available() is False

    def test_docker_none(self, monkeypatch):
        monkeypatch.setattr(rcm, "docker", None)
        assert rcm.is_resource_image_available() is False


class TestEnsureContainerGuard:
    def test_unavailable_image_raises_with_manual_build_cmd(self, monkeypatch, tmp_path):
        images = _FakeImages(present=False, build_error=RuntimeError("build failed"))
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = rcm.ResourceContainerManager(
            workspace_id="ws-1", workspace_path=str(workspace)
        )
        with pytest.raises(RuntimeError, match="docker build -t tm-resource-git"):
            mgr.ensure_container()
