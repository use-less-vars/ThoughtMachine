"""
Unit tests for the auto image-build logic in ``infra.resource_container_manager``
(``compute_resource_build_hash``, ``_ensure_resource_image`` /
``is_resource_image_available`` and the ``ensure_container`` guard).

The resource images are auto-built in TWO stages from THIS repo's pinned
sources: the workspace runtime base (``tm-workspace-runtime:latest``) from
``requirements.txt`` + ``resources/default_dockerfile.txt``, then the git
resource overlay (``tm-resource-git``) from
``resources/git_resource_overlay_dockerfile.txt`` built on top of it (via
``--build-arg BASE_IMAGE=...``). Every auto-built image carries a
``thoughtmachine.build_hash`` label (sha256 of the exact bytes built), and
``_ensure_resource_image`` rebuilds a stage when its label is missing or
stale (drift detection).

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
    """Stand-in for a docker image object; carries ``.id`` and ``.labels``."""

    def __init__(self, image_id="sha256:img-current", labels=None):
        self.id = image_id
        self.labels = labels or {}


class _FakeImages:
    """Tag-aware ``client.images`` fake for the TWO-STAGE resource image build.

    ``tm-workspace-runtime:latest`` (the runtime base) is modelled by the
    ``runtime_*`` attributes; ``tm-resource-git`` (the git resource overlay)
    by the plain ``*`` attributes. ``get()`` raises ``ImageNotFound`` for a
    missing tag, mirroring production's ``_check_resource_image``.
    """

    def __init__(
        self,
        present=True,
        labels=None,
        runtime_present=True,
        runtime_image_id="sha256:img-runtime",
        runtime_labels=None,
        image_id="sha256:img-resource",
        build_error=None,
        get_error=None,
    ):
        self.present = present
        self.labels = labels or {}
        self.runtime_present = runtime_present
        self.runtime_image_id = runtime_image_id
        self.runtime_labels = runtime_labels or {}
        self.image_id = image_id
        self.build_error = build_error
        self.get_error = get_error
        self.build_calls = []
        self.build_context_listing = None
        self.build_context_listings = []

    def get(self, tag):
        if self.get_error is not None:
            raise self.get_error
        if tag == rcm.RUNTIME_IMAGE_TAG:
            if not self.runtime_present:
                raise ImageNotFound(tag)
            return _FakeImage(self.runtime_image_id, self.runtime_labels)
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage(self.image_id, self.labels)

    def build(self, **kwargs):
        # record the attempt even when the build fails (simulates a real
        # docker build that is attempted and errors).
        self.build_calls.append(kwargs)
        # snapshot the staged build context while it still exists (the
        # caller removes the temp dir right after build returns).
        if "path" in kwargs:
            listing = sorted(os.listdir(kwargs["path"]))
            self.build_context_listing = listing
            self.build_context_listings.append(listing)
        if self.build_error is not None:
            raise self.build_error
        # simulate docker: the built image exists after a successful build,
        # labelled with the labels that were passed to build(). The fake's
        # image id stays stable so production can read it via
        # ``client.images.get(tag).id`` right after the build.
        labels = kwargs.get("labels") or {}
        if kwargs.get("tag") == rcm.RUNTIME_IMAGE_TAG:
            self.runtime_present = True
            self.runtime_labels = labels
            built = _FakeImage(self.runtime_image_id, labels)
        else:
            self.present = True
            self.labels = labels
            built = _FakeImage(self.image_id, labels)
        return built, iter([])


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
        rcm.REPO_REQUIREMENTS, rcm.REPO_RUNTIME_DOCKERFILE
    )


def _overlay_build_hash(runtime_image_id="sha256:img-runtime"):
    """The overlay hash for the real repo sources built on ``runtime_image_id``.

    Mirrors production: the git resource overlay's build hash covers
    requirements + runtime dockerfile + overlay dockerfile + the runtime
    image id the overlay is built on.
    """
    return rcm.compute_git_overlay_build_hash(
        rcm.REPO_REQUIREMENTS,
        rcm.REPO_RUNTIME_DOCKERFILE,
        rcm.GIT_OVERLAY_DOCKERFILE,
        runtime_image_id,
    )


def _ready_images(runtime_image_id="sha256:img-runtime"):
    """Both resource images present with matching build-hash labels."""
    return _FakeImages(
        present=True,
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash(runtime_image_id)},
        runtime_present=True,
        runtime_image_id=runtime_image_id,
        runtime_labels={rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()},
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
    def test_images_present_with_matching_hashes_no_build(self, monkeypatch):
        # Both stages (runtime base AND git overlay) present with matching
        # build-hash labels -> the fast existence+drift check passes, no build.
        images = _ready_images()
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert images.build_calls == []
        assert rcm._RESOURCE_IMAGE_READY is True

    @pytest.mark.parametrize(
        "runtime_missing,expected_tag",
        [
            (True, rcm.RUNTIME_IMAGE_TAG),
            (False, rcm.RESOURCE_IMAGE_TAG),
        ],
        ids=["runtime-missing-label", "overlay-missing-label"],
    )
    def test_image_present_without_hash_label_rebuilds(
        self, monkeypatch, runtime_missing, expected_tag
    ):
        # Exactly the image missing its build-hash label is rebuilt; the
        # other stage stays untouched.
        images = _FakeImages(
            present=True,
            labels=(
                {}
                if not runtime_missing
                else {rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash()}
            ),
            runtime_present=True,
            runtime_labels=(
                {}
                if runtime_missing
                else {rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()}
            ),
        )
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 1
        assert images.build_calls[0]["tag"] == expected_tag
        assert images.build_calls[0]["labels"] == {
            rcm.RESOURCE_BUILD_HASH_LABEL: (
                _repo_build_hash() if runtime_missing else _overlay_build_hash()
            )
        }
        if not runtime_missing:
            assert images.build_calls[0]["buildargs"] == {
                "BASE_IMAGE": rcm.RUNTIME_IMAGE_TAG
            }

    @pytest.mark.parametrize(
        "runtime_stale,expected_tag",
        [
            (True, rcm.RUNTIME_IMAGE_TAG),
            (False, rcm.RESOURCE_IMAGE_TAG),
        ],
        ids=["stale-runtime-label", "stale-overlay-label"],
    )
    def test_image_present_with_stale_hash_rebuilds(
        self, monkeypatch, runtime_stale, expected_tag
    ):
        # Exactly the image carrying a stale (drifted) build-hash label is
        # rebuilt; the other stage stays untouched.
        images = _FakeImages(
            present=True,
            labels=(
                {rcm.RESOURCE_BUILD_HASH_LABEL: "0" * 64}
                if not runtime_stale
                else {rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash()}
            ),
            runtime_present=True,
            runtime_labels=(
                {rcm.RESOURCE_BUILD_HASH_LABEL: "0" * 64}
                if runtime_stale
                else {rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()}
            ),
        )
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 1
        assert images.build_calls[0]["tag"] == expected_tag

    def test_images_missing_build_both_stages_from_repo_sources(self, monkeypatch):
        images = _FakeImages(present=False, runtime_present=False)
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        assert rcm._ensure_resource_image() is True
        assert len(images.build_calls) == 2
        # Stage 1 - the workspace runtime base image (tm-workspace-runtime:latest).
        runtime_kwargs = images.build_calls[0]
        # build context is a fresh temp dir staging the repo sources, NOT the
        # vault-managed directory (removed after the build, so only the
        # prefix is checkable).
        assert os.path.basename(runtime_kwargs["path"]).startswith(
            "tm-resource-build-"
        )
        # the context stages exactly the two repo sources and is never the
        # repo root itself nor the vault-managed directory.
        assert runtime_kwargs["path"] != rcm._REPO_ROOT
        assert runtime_kwargs["path"] != os.path.join(
            os.path.expanduser("~"), ".thoughtmachine", "docker", "resource"
        )
        # staged context contains exactly the two repo sources (snapshot
        # taken by the fake at build time, before the temp dir is removed)
        assert images.build_context_listings[0] == [
            "Dockerfile",
            "requirements.txt",
        ]
        assert runtime_kwargs["dockerfile"] == "Dockerfile"
        assert runtime_kwargs["tag"] == rcm.RUNTIME_IMAGE_TAG
        assert runtime_kwargs["rm"] is True
        assert runtime_kwargs["labels"] == {
            rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()
        }
        # Stage 2 - the git resource overlay on top of the runtime image.
        overlay_kwargs = images.build_calls[1]
        assert overlay_kwargs["tag"] == rcm.RESOURCE_IMAGE_TAG
        # the overlay context stages ONLY the Dockerfile; the runtime base is
        # passed via --build-arg.
        assert images.build_context_listings[1] == ["Dockerfile"]
        assert overlay_kwargs["dockerfile"] == "Dockerfile"
        assert overlay_kwargs["rm"] is True
        assert overlay_kwargs["buildargs"] == {"BASE_IMAGE": rcm.RUNTIME_IMAGE_TAG}
        assert overlay_kwargs["labels"] == {
            rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash()
        }
        assert rcm._RESOURCE_IMAGE_READY is True

    def test_build_failure_returns_false_no_raise(self, monkeypatch):
        images = _FakeImages(
            present=False,
            runtime_present=False,
            build_error=RuntimeError("build failed"),
        )
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
        images = _FakeImages(present=False, runtime_present=False)
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
        # Single-flight: exactly one runtime build + one overlay build total.
        assert len(images.build_calls) == 2
        assert [call["tag"] for call in images.build_calls] == [
            rcm.RUNTIME_IMAGE_TAG,
            rcm.RESOURCE_IMAGE_TAG,
        ]


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
        images = _FakeImages(
            present=False,
            runtime_present=False,
            build_error=RuntimeError("build failed"),
        )
        monkeypatch.setattr(rcm, "docker", _FakeDockerModule(images))
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mgr = rcm.ResourceContainerManager(
            workspace_id="ws-1", workspace_path=str(workspace)
        )
        with pytest.raises(RuntimeError, match="docker build -t tm-resource-git"):
            mgr.ensure_container()
