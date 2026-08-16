"""Unit tests for executor image build-drift detection.

No Docker daemon required: ``docker_executor.docker`` is replaced with fakes
mirroring the SDK surface the code touches (``from_env``,
``client.images.get``). The drift gate labels every built executor image with
``thoughtmachine.build_hash`` (a content hash of repo requirements.txt +
resources/default_dockerfile.txt) and reuses an existing image only while
that label matches the current sources.
"""

import os
from unittest.mock import MagicMock

import pytest

import docker_executor as dex
import infra.resource_container_manager as rcm

try:
    from docker.errors import ImageNotFound
except Exception:  # pragma: no cover - docker SDK absent
    ImageNotFound = Exception


# ------------------------------------------------------------------ fakes


class _FakeImage:
    """Image fake with ``.id`` and ``.labels``."""

    def __init__(self, image_id="sha256:img-current", labels=None):
        self.id = image_id
        self.labels = labels or {}


class _FakeImages:
    """``client.images`` fake with ``get(tag)``."""

    def __init__(self, present=True, labels=None, get_error=None):
        self.present = present
        self.labels = labels
        self.get_error = get_error

    def get(self, tag):
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage("sha256:img-current", self.labels)


class _FakeClient:
    def __init__(self, images):
        self.images = images


class _FakeErrors:
    """``docker.errors`` surface used by docker_executor."""

    ImageNotFound = ImageNotFound
    BuildError = type("BuildError", (Exception,), {})
    APIError = type("APIError", (Exception,), {})
    NotFound = ImageNotFound


class _FakeDockerModule:
    """Stand-in for the ``docker`` module: ``from_env`` + ``errors``."""

    def __init__(self, images):
        self._images = images
        self.errors = _FakeErrors
        self.types = None

    def from_env(self):
        return _FakeClient(self._images)


def _make_executor(images, monkeypatch):
    """Instantiate DockerExecutor against the fake docker module."""
    monkeypatch.setattr(dex, "docker", _FakeDockerModule(images))
    return dex.DockerExecutor(
        workspace_path="/tmp/test-workspace",
        image="agent-executor-test",
        workspace_id="ws-drift",
    )


# ------------------------------------------------------------------ (a) hash


def test_hash_changes_when_sources_change(tmp_path):
    req = tmp_path / "requirements.txt"
    df = tmp_path / "Dockerfile"
    req.write_text("pkg==1.0\n")
    df.write_text("FROM python:3.12-slim\n")
    h1 = dex.compute_executor_build_hash(str(req), str(df))

    df.write_text("FROM python:3.12-slim\nRUN echo hi\n")
    h2 = dex.compute_executor_build_hash(str(req), str(df))
    assert h1 != h2

    req.write_text("pkg==2.0\n")
    h3 = dex.compute_executor_build_hash(str(req), str(df))
    assert h2 != h3

    # Deterministic: same bytes -> same hash
    req.write_text("pkg==1.0\n")
    df.write_text("FROM python:3.12-slim\n")
    assert dex.compute_executor_build_hash(str(req), str(df)) == h1


def test_hash_defaults_to_module_paths(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    df = tmp_path / "Dockerfile"
    req.write_text("pkg==1.0\n")
    df.write_text("FROM python:3.12-slim\n")
    monkeypatch.setattr(dex, "EXECUTOR_REQUIREMENTS", str(req))
    monkeypatch.setattr(dex, "EXECUTOR_RUNTIME_DOCKERFILE", str(df))
    assert dex.compute_executor_build_hash() == dex.compute_executor_build_hash(str(req), str(df))


def test_hash_missing_source_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        dex.compute_executor_build_hash(str(tmp_path / "nope.txt"), str(tmp_path / "Dockerfile"))


# ------------------------------------------------------------------ (b) reuse


def test_ensure_image_reuses_when_label_matches(monkeypatch):
    build_hash = dex.compute_executor_build_hash()
    images = _FakeImages(present=True, labels={dex.EXECUTOR_BUILD_HASH_LABEL: build_hash})
    executor = _make_executor(images, monkeypatch)
    executor._build_image = MagicMock(return_value=("sha256:new", []))

    image = executor._ensure_image()

    assert image.id == "sha256:img-current"
    executor._build_image.assert_not_called()


def test_ensure_image_reuses_when_sources_unreadable(monkeypatch):
    """OSError computing the hash must NOT fail the reuse path."""

    def _boom(*args, **kwargs):
        raise OSError("missing build sources")

    monkeypatch.setattr(dex, "compute_executor_build_hash", _boom)
    images = _FakeImages(present=True, labels={})
    executor = _make_executor(images, monkeypatch)
    executor._build_image = MagicMock(return_value=("sha256:new", []))

    image = executor._ensure_image()

    assert image.id == "sha256:img-current"
    executor._build_image.assert_not_called()


# ------------------------------------------------------------------ (c) rebuild


@pytest.mark.parametrize(
    "images_kwargs",
    [
        {"present": False},  # image missing entirely
        {"present": True, "labels": {}},  # label missing -> unlabeled legacy image
        {"present": True, "labels": {dex.EXECUTOR_BUILD_HASH_LABEL: "stale-hash"}},  # drifted
    ],
)
def test_ensure_image_builds_when_missing_or_drifted(monkeypatch, images_kwargs):
    images = _FakeImages(**images_kwargs)
    executor = _make_executor(images, monkeypatch)
    executor._build_image = MagicMock(return_value=("sha256:new", []))

    image = executor._ensure_image()

    executor._build_image.assert_called_once()
    assert image == "sha256:new"


# ------------------------------------------------------------------ (d) labels


def test_built_image_receives_build_hash_label(monkeypatch, tmp_path):
    # Vault Dockerfile under the (mocked) HOME vault root
    monkeypatch.setenv("HOME", str(tmp_path))
    vault_dir = tmp_path / ".thoughtmachine" / "workspaces" / "ws-drift"
    vault_dir.mkdir(parents=True)
    (vault_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")

    # Committed requirements.txt via git show
    def _fake_git(*args, **kwargs):
        return MagicMock(returncode=0, stdout="pkg==1.0\n")

    monkeypatch.setattr("subprocess.run", _fake_git)

    monkeypatch.setattr(dex, "compute_executor_build_hash", lambda *a, **k: "fixed-hash")

    seen = {}

    def _spy_run_image_build(client, build_path, dockerfile, tag, **kwargs):
        seen["client"] = client
        seen["build_path"] = build_path
        seen["dockerfile"] = dockerfile
        seen["tag"] = tag
        seen["labels"] = kwargs.get("labels")
        seen["staged_files"] = set(os.listdir(build_path))
        return "sha256:img-built", ["step1"]

    monkeypatch.setattr(dex, "_run_image_build", _spy_run_image_build)

    images = _FakeImages(present=False)
    executor = _make_executor(images, monkeypatch)

    image_id, log_lines = executor._build_image()

    assert image_id == "sha256:img-built"
    assert seen["labels"] == {dex.EXECUTOR_BUILD_HASH_LABEL: "fixed-hash"}
    assert seen["dockerfile"] == "Dockerfile"
    assert seen["staged_files"] == {"Dockerfile", "requirements.txt"}


def test_run_image_build_labels_kwarg_is_additive():
    """labels is an optional kwarg: absent -> no labels key in api.build."""
    calls = []

    def _fake_build(**kwargs):
        calls.append(kwargs)
        return [{"aux": {"ID": "sha256:img-x"}}]

    client = MagicMock()
    client.api.build.side_effect = _fake_build

    image_id, _ = dex._run_image_build(client, "/ctx", "Dockerfile", "tag:1", labels={"a": "b"})
    assert image_id == "sha256:img-x"
    assert calls[0].get("labels") == {"a": "b"}

    dex._run_image_build(client, "/ctx", "Dockerfile", "tag:1")
    assert "labels" not in calls[1]


# ------------------------------------------------------------------ (e) parity


def test_executor_hash_matches_resource_hash(tmp_path):
    req = tmp_path / "requirements.txt"
    df = tmp_path / "Dockerfile"
    req.write_bytes(b"pkg==1.0\n")
    df.write_bytes(b"FROM python:3.12-slim\n")

    assert dex.compute_executor_build_hash(str(req), str(df)) == rcm.compute_resource_build_hash(
        str(req), str(df)
    )
    with open(req, "rb") as f:
        req_b = f.read()
    with open(df, "rb") as f:
        df_b = f.read()
    assert dex._hash_executor_build_bytes(req_b, df_b) == rcm._hash_resource_bytes(req_b, df_b)
