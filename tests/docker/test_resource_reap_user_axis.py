"""Pure-unit tests for the resource-reap USER-drift axis.

``ResourceContainerManager.ensure_resource`` must treat a Docker per-container
user mismatch (``container.attrs["Config"]["User"]`` differing from the host
policy user ``host_user() or "0:0"``) as STALE, so the container is removed and
recreated. This mirrors the restart-policy/network axes already exercised in
``infra.container_manager`` and closes the gap where a resource container built
under a different uid:gid was silently reused.

No Docker daemon required: the ``docker`` module attribute is replaced with
fakes (``from_env``, ``client.containers.list/run``, ``client.images.get``) and
``host_user`` is monkeypatched for determinism. Pure fakes only — no
``@needs_docker``.
"""

import shutil
from pathlib import Path

import pytest

import infra.resource_container_manager as rcm


_REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from docker.errors import ImageNotFound
except Exception:  # pragma: no cover - docker SDK absent
    ImageNotFound = Exception


POLICY_USER = "502:20"
OTHER_USER = "1000:1000"


# ------------------------------------------------------------------ fakes


class _FakeImageRef:
    """``container.image`` fake — only ``.id`` is used."""

    def __init__(self, image_id):
        self.id = image_id


class _FakeImage:
    """Image fake with ``.id`` and ``.labels``."""

    def __init__(self, image_id="sha256:img-current", labels=None):
        self.id = image_id
        self.labels = labels or {}


class _FakeImages:
    """``client.images`` fake with ``get``, ``build`` and ``remove``."""

    def __init__(
        self,
        present=True,
        image_id="sha256:img-current",
        labels=None,
        runtime_labels=None,
        get_error=None,
        remove_error=None,
    ):
        self.present = present
        self.image_id = image_id
        self.labels = labels
        self.runtime_labels = runtime_labels
        self.get_error = get_error
        self.remove_error = remove_error
        self.get_calls = []
        self.build_calls = []
        self.remove_calls = []

    def get(self, tag):
        self.get_calls.append(tag)
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(tag)
        if tag == rcm.RUNTIME_IMAGE_TAG:
            return _FakeImage(self.image_id, self.runtime_labels)
        return _FakeImage(self.image_id, self.labels)

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        self.present = True
        return None, []

    def remove(self, tag, force=False):
        self.remove_calls.append({"tag": tag, "force": force})
        if self.remove_error is not None:
            raise self.remove_error
        self.present = False


class _FakeContainer:
    """Container fake carrying ``attrs["Config"]["User"]`` (the axis under test).

    ``attrs`` defaults to ``None`` (no observable user) so the axis is a no-op
    unless a fake explicitly opts in — mirroring ``_live_user``'s structural
    absence semantics.
    """

    def __init__(
        self,
        container_id,
        name=None,
        image_id="sha256:img-current",
        status="running",
        labels=None,
        owner=None,
        remove_error=None,
        attrs=None,
    ):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef(image_id)
        self.status = status
        self.labels = labels or {}
        self.owner = owner
        self.remove_error = remove_error
        self.attrs = attrs
        self.remove_calls = 0

    def start(self):
        self.status = "running"

    def remove(self, force=False):
        self.remove_calls += 1
        if self.remove_error is not None:
            raise self.remove_error
        if self.owner is not None and self in self.owner.containers:
            self.owner.containers.remove(self)


def _matches_filters(container, filters):
    """Emulate docker-py's label-filter semantics for the fakes."""
    if not filters:
        return True
    label_filters = filters.get("label")
    if label_filters:
        if isinstance(label_filters, str):
            label_filters = [label_filters]
        for lf in label_filters:
            labels = container.labels or {}
            if "=" in lf:
                key, value = lf.split("=", 1)
                if labels.get(key) != value:
                    return False
            elif labels.get(lf) is None:
                return False
    name_filters = filters.get("name")
    if name_filters and (not container.name or name_filters not in container.name):
        return False
    return True


class _FakeContainers:
    """``client.containers`` fake with label-filtered ``list`` and ``run``."""

    def __init__(self, containers=None, run_error=None, list_error=None):
        self.containers = list(containers or [])
        for container in self.containers:
            container.owner = self
        self.run_error = run_error
        self.list_error = list_error
        self.run_calls = []
        self._next_id = 1

    def list(self, all=True, filters=None):
        if self.list_error is not None:
            raise self.list_error
        return [c for c in self.containers if _matches_filters(c, filters)]

    def run(self, *args, **kwargs):
        if args:
            kwargs.setdefault("image", args[0])
        if len(args) > 1:
            kwargs.setdefault("command", args[1])
        self.run_calls.append(kwargs)
        if self.run_error is not None:
            raise self.run_error
        container = _FakeContainer(
            container_id=f"new-{self._next_id}",
            name=kwargs.get("name"),
            image_id=kwargs.get("image", rcm.RESOURCE_IMAGE_TAG),
            status="running",
            labels=kwargs.get("labels") or {},
            owner=self,
        )
        self._next_id += 1
        self.containers.append(container)
        return container


class _FakeClient:
    def __init__(self, images=None, containers=None):
        self.images = images
        self.containers = containers if containers is not None else _FakeContainers()

    def ping(self):
        return True


class _FakeDockerModule:
    """Stands in for the ``docker`` module: ``from_env()`` -> client."""

    def __init__(self, images=None, containers=None):
        self.images = images
        self.containers = containers

    def from_env(self):
        return _FakeClient(self.images, self.containers)


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_image_ready():
    """Isolate the module-level image-readiness cache per test."""
    rcm._RESOURCE_IMAGE_READY = False
    yield
    rcm._RESOURCE_IMAGE_READY = False


@pytest.fixture(autouse=True)
def _vault_resource_files(tmp_path, monkeypatch):
    """Point rcm's VAULT_* build inputs at a tmp vault seeded from the repo
    seeds (resources/ + requirements.txt). Identical bytes -> identical build
    hashes, so ``_ensure_resource_image`` reports the image ready."""
    vault_dir = tmp_path / "docker" / "resource"
    vault_dir.mkdir(parents=True)
    shutil.copy2(
        _REPO_ROOT / "resources" / "default_dockerfile.txt",
        vault_dir / "default_runtime.Dockerfile",
    )
    shutil.copy2(
        _REPO_ROOT / "resources" / "git_resource_overlay_dockerfile.txt",
        vault_dir / "git_overlay.Dockerfile",
    )
    shutil.copy2(_REPO_ROOT / "requirements.txt", vault_dir / "requirements.txt")
    monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
    monkeypatch.setattr(rcm, "VAULT_REQUIREMENTS", str(vault_dir / "requirements.txt"))
    monkeypatch.setattr(
        rcm,
        "VAULT_RUNTIME_DOCKERFILE",
        str(vault_dir / "default_runtime.Dockerfile"),
    )
    monkeypatch.setattr(
        rcm,
        "VAULT_OVERLAY_DOCKERFILE",
        str(vault_dir / "git_overlay.Dockerfile"),
    )
    yield vault_dir


@pytest.fixture(autouse=True)
def _pin_host_user(monkeypatch):
    """Pin the host policy user so the axis is deterministic across platforms."""
    monkeypatch.setattr(rcm, "host_user", lambda: POLICY_USER)


# ----------------------------------------------------------------- helpers


def _repo_build_hash():
    return rcm.compute_resource_build_hash(
        rcm.VAULT_REQUIREMENTS, rcm.VAULT_RUNTIME_DOCKERFILE
    )


def _overlay_build_hash(runtime_image_id="sha256:img-current"):
    return rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        rcm.VAULT_OVERLAY_DOCKERFILE,
        runtime_image_id,
    )


def _fresh_images(image_id="sha256:img-current"):
    """Images fake whose build-hash labels satisfy ``_ensure_resource_image``."""
    return _FakeImages(
        present=True,
        image_id=image_id,
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash(image_id)},
        runtime_labels={rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()},
    )


def _labels_for(mgr, workspace_id):
    """Labels that make ``_find_resource_container`` match with NO label drift."""
    return {
        rcm.ResourceContainerManager.WORKSPACE_LABEL: workspace_id,
        rcm.ResourceContainerManager.RESOURCE_LABEL: "git",
        rcm.ResourceContainerManager.CONTAINER_NAME_LABEL: mgr.container_name,
    }


def _setup(monkeypatch, tmp_path, attrs, workspace_id="ws-1", container_id="c-test"):
    """Wire docker fakes BEFORE constructing the manager (the constructor
    captures ``docker.from_env()`` at line 928), then register ONE labelled
    container.

    The container carries NO label or image-id drift, so only the USER axis
    under test can make it stale.  Returns ``(mgr, container, containers)``.
    """
    images = _fresh_images()
    containers = _FakeContainers()
    monkeypatch.setattr(
        rcm,
        "docker",
        _FakeDockerModule(images=images, containers=containers),
    )
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    mgr = rcm.ResourceContainerManager(
        workspace_id=workspace_id, workspace_path=str(ws)
    )
    container = _FakeContainer(
        container_id=container_id,
        name=mgr.container_name,
        labels=_labels_for(mgr, workspace_id),
        attrs=attrs,
    )
    containers.containers.append(container)
    container.owner = containers
    return mgr, container, containers


# --------------------------------------------------- user-drift axis tests


def test_user_drift_is_stale_and_recreated(monkeypatch, tmp_path):
    """Live ``Config.User`` != policy user -> stale -> remove + recreate."""
    mgr, container, containers = _setup(
        monkeypatch, tmp_path, {"Config": {"User": OTHER_USER}}, container_id="c-drift"
    )

    result = mgr.ensure_resource("git")

    assert container.remove_calls == 1
    assert len(containers.run_calls) == 1
    assert result["mode"] == "containerized"
    assert result["container_id"] == "new-1"
    assert result["container_id"] != "c-drift"


def test_user_match_is_not_stale(monkeypatch, tmp_path):
    """Live ``Config.User`` == policy user -> NOT stale -> reused in place."""
    mgr, container, containers = _setup(
        monkeypatch, tmp_path, {"Config": {"User": POLICY_USER}}, container_id="c-match"
    )

    result = mgr.ensure_resource("git")

    assert container.remove_calls == 0
    assert containers.run_calls == []
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-match"


@pytest.mark.parametrize(
    "attrs",
    [
        {"Config": {}},               # Config present, User structurally absent
        {},                           # no Config key at all
        {"Config": {"User": ""}},     # blank User reads as absent
        None,                         # attrs is None (unreadable)
        "broken",                     # attrs is not a mapping (unreadable)
    ],
)
def test_user_absent_is_not_stale(monkeypatch, tmp_path, attrs):
    """Absent/unreadable live user cannot prove a mismatch -> NOT stale."""
    mgr, container, containers = _setup(
        monkeypatch, tmp_path, attrs, container_id="c-absent"
    )

    result = mgr.ensure_resource("git")

    assert container.remove_calls == 0
    assert containers.run_calls == []
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-absent"
