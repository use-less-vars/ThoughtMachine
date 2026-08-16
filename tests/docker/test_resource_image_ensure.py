"""
Unit tests for the auto image-build logic in ``infra.resource_container_manager``
(``compute_resource_build_hash``, ``_ensure_resource_image`` /
``is_resource_image_available`` and the ``ensure_container`` guard).

The resource image is auto-built from THIS repo's pinned sources
(``requirements.txt`` + ``resources/resource_dockerfile.txt``) staged into a
temp build context; every auto-built image carries a
``thoughtmachine.build_hash`` label (sha256 of the exact bytes built), and
``_ensure_resource_image`` rebuilds when the label is missing or stale
(drift detection).

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
    """Stand-in for a docker image object; carries only its labels."""

    def __init__(self, labels=None):
        self.labels = labels or {}


class _FakeImages:
    def __init__(self, present, labels=None, build_error=None, get_error=None):
        self.present = present
        self.labels = labels or {}
        self.build_error = build_error
        self.get_error = get_error
        self.build_calls = []

    def get(self, tag):
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage(self.labels)

    def build(self, **kwargs):
        # record the attempt even when the build fails (simulates a real
        # docker build that is attempted and errors).
        self.build_calls.append(kwargs)
        if self.build_error is not None:
            raise self.build_error
        # simulate docker: the image exists after a successful build,
        # labelled with the labels that were passed to build().
        self.present = True
        self.labels = kwargs.get("labels") or {}
        return _FakeImage(self.labels), iter([])


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


def _repo_build_hash():
    """The hash ``_ensure_resource_image`` expects for the real repo sources."""
    return rcm.compute_resource_build_hash(
        rcm.REPO_REQUIREMENTS, rcm.REPO_RESOURCE_DOCKERFILE
    )


class TestComputeResourceBuildHash:
    def test_deterministic_same_content_same_hash(self, tmp_path):
        req = tmp_path / "requirements.txt"
        df = tmp_path / "Dockerfile"
        req.write_text("fastapi\npytest\n")
        df.write_text("FROM python:3.12-slim\n")
        first = rcm.compute_resource_build_hash(str(req), str(df))
        second = rcm.compute_resource_build_hash(str(req), str(df))
        assert first == second

    def test_different_requirements_different_hash(self, tmp_path):
        req = tmp_path / "requirements.txt"
        df = tmp_path / "Dockerfile"
        df.write_text("FROM python:3.12-slim\n")
        req.write_text("fastapi\n")
        h1 = rcm.compute_resource_build_hash(str(req), str(df))
        req.write_text("fastapi==0.100.0\n")
        h2 = rcm.compute_resource_build_hash(str(req), str(df))
        assert h1 != h2

    def test_different_dockerfile_different_hash(self, tmp_path):
        req = tmp_path / "requirements.txt"
        df = tmp_path / "Dockerfile"
        req.write_text("fastapi\n")
        df.write_text("FROM python:3.12-slim\n")
        h1 = rcm.compute_resource_build_hash(str(req), str(df))
        df.write_text("FROM python:3.13-slim\n")
        h2 = rcm.compute_resource_build_hash(str(req), str(df))
        assert h1 != h2

    def test_returns_hex_digest(self, tmp_path):
        req = tmp_path / "requirements.txt"
        df = tmp_path / "Dockerfile"
        req.write_text("fastapi\n")
        df.write_text("FROM python:3.12-slim\n")
        digest = rcm.compute_resource_build_hash(str(req), str(df))
        assert len(digest) == 64
        int(digest, 16)  # is valid hex


class TestEnsureResourceImage:
    def test_image_present_with_matching_hash_no_build(self, monkeypatch):
        images = _FakeImages(
            present=True,
            labels={rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()},
        )
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert images.build_calls == []
        assert rcm._RESOURCE_IMAGE_READY is True

    def test_image_present_without_hash_label_rebuilds(self, monkeypatch):
        images = _FakeImages(present=True, labels={})
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 1
        assert images.build_calls[0]["labels"] == {
            rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()
        }

    def test_image_present_with_stale_hash_rebuilds(self, monkeypatch):
        images = _FakeImages(
            present=True,
            labels={rcm.RESOURCE_BUILD_HASH_LABEL: "0" * 64},
        )
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 1
        assert images.build_calls[0]["labels"] == {
            rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()
        }

    def test_image_missing_builds_from_repo_sources(self, monkeypatch):
        images = _FakeImages(present=False)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 1
        kwargs = images.build_calls[0]
        # build context is a fresh temp dir staging the repo sources, NOT the
        # vault-managed directory (removed after the build, so only the
        # prefix is checkable).
        assert os.path.basename(kwargs["path"]).startswith("tm-resource-build-")
        assert kwargs["path"] != str(rcm.RESOURCE_IMAGE_DOCKERFILE_DIR)
        assert kwargs["dockerfile"] == "Dockerfile"
        assert kwargs["tag"] == rcm.RESOURCE_IMAGE_TAG
        assert kwargs["rm"] is True
        assert kwargs["labels"] == {
            rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()
        }
        assert rcm._RESOURCE_IMAGE_READY is True

    def test_build_failure_returns_false_no_raise(self, monkeypatch):
        images = _FakeImages(present=False, build_error=RuntimeError("build failed"))
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is False
        assert len(images.build_calls) == 1
        assert rcm._RESOURCE_IMAGE_READY is False

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
