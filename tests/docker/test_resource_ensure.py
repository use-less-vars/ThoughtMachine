"""Pure-unit tests for ``ResourceContainerManager.ensure_resource``.

No Docker daemon required: the ``docker`` module attribute is replaced with
fakes that mirror the SDK surface the manager actually touches (``from_env``,
``client.containers.list/run``, ``client.images.get/build``).

These tests deliberately use their OWN fakes (no imports from
``test_resource_image_ensure``) so the suites stay independent.
"""

import pytest

import infra.resource_container_manager as rcm

try:
    from docker.errors import ImageNotFound
except Exception:  # pragma: no cover - docker SDK absent
    ImageNotFound = Exception


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
    """``client.images`` fake with ``get(tag)`` and ``build(**kwargs)``."""

    def __init__(
        self,
        present=True,
        image_id="sha256:img-current",
        labels=None,
        build_error=None,
        get_error=None,
    ):
        self.present = present
        self.image_id = image_id
        self.labels = labels
        self.build_error = build_error
        self.get_error = get_error
        self.build_calls = []

    def get(self, tag):
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage(self.image_id, self.labels)

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        if self.build_error is not None:
            raise self.build_error
        self.present = True
        return None, []


class _FakeContainer:
    """Container fake with start()/remove() recording + error injection."""

    def __init__(
        self,
        container_id,
        name=None,
        image_id="sha256:img-current",
        status="running",
        labels=None,
        attrs=None,
        start_error=None,
        remove_error=None,
    ):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef(image_id)
        self.status = status
        self.labels = labels or {}
        self.attrs = attrs or {}
        self.start_error = start_error
        self.remove_error = remove_error
        self.start_calls = 0
        self.remove_calls = 0

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.status = "running"

    def remove(self, force=False):
        self.remove_calls += 1
        if self.remove_error is not None:
            raise self.remove_error


class _FakeContainers:
    """``client.containers`` fake with ``list`` and ``run``."""

    def __init__(self, containers=None, run_error=None):
        self.containers = list(containers or [])
        self.run_error = run_error
        self.run_calls = []
        self._next_id = 1

    def list(self, all=True, filters=None):
        return list(self.containers)

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        if self.run_error is not None:
            raise self.run_error
        container = _FakeContainer(
            container_id=f"new-{self._next_id}",
            name=kwargs.get("name"),
            image_id=kwargs.get("image", rcm.RESOURCE_IMAGE_TAG),
            status="running",
            labels=kwargs.get("labels") or {},
        )
        self._next_id += 1
        self.containers.append(container)
        return container


class _FakeClient:
    def __init__(self, images=None, containers=None):
        self.images = images
        self.containers = containers


class _FakeDockerModule:
    """Stands in for the ``docker`` module: ``from_env()`` -> client."""

    def __init__(
        self,
        images=None,
        containers=None,
        from_env_error=None,
        from_env_returns_none=False,
    ):
        self.images = images
        self.containers = containers
        self.from_env_error = from_env_error
        self.from_env_returns_none = from_env_returns_none
        self.from_env_calls = 0

    def from_env(self):
        self.from_env_calls += 1
        if self.from_env_error is not None:
            raise self.from_env_error
        if self.from_env_returns_none:
            return None
        return _FakeClient(self.images, self.containers)


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_image_ready():
    """Isolate the module-level image-readiness cache per test."""
    rcm._RESOURCE_IMAGE_READY = False
    yield
    rcm._RESOURCE_IMAGE_READY = False


def _repo_build_hash():
    return rcm.compute_resource_build_hash(
        rcm.REPO_REQUIREMENTS, rcm.REPO_RESOURCE_DOCKERFILE
    )


def _labels_for(mgr, kind="git"):
    return {
        mgr.WORKSPACE_LABEL: mgr.workspace_id,
        mgr.RESOURCE_LABEL: kind,
        mgr.CONTAINER_NAME_LABEL: mgr.container_name,
    }


def _make_manager(monkeypatch, images=None, containers=None, docker_mod=None,
                  session_permissions=None):
    if docker_mod is None:
        docker_mod = _FakeDockerModule(images=images, containers=containers)
    monkeypatch.setattr(rcm, "docker", docker_mod)
    return rcm.ResourceContainerManager(
        workspace_id="ws-1",
        workspace_path="/tmp/tm-ensure-ws",
        session_permissions=session_permissions,
    )


def _images_present(image_id="sha256:img-current", get_error=None):
    return _FakeImages(
        present=True,
        image_id=image_id,
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()},
        get_error=get_error,
    )


# ------------------------------------------------------------------ tests


def test_unknown_resource_unavailable(monkeypatch):
    mgr = _make_manager(monkeypatch, images=_images_present())
    result = mgr.ensure_resource("nope")
    assert result == {
        "mode": "unavailable",
        "container_id": None,
        "status": None,
        "image": None,
        "detail": "unknown resource 'nope'",
    }


def test_docker_none_host_fallback(monkeypatch):
    mgr = _make_manager(monkeypatch, images=_images_present())
    monkeypatch.setattr(rcm, "docker", None)
    result = mgr.ensure_resource("git")
    assert result["mode"] == "host_fallback"
    assert "docker build -t tm-resource-git" in result["detail"]


def test_from_env_raises_host_fallback(monkeypatch):
    docker_mod = _FakeDockerModule(images=_images_present())
    mgr = _make_manager(monkeypatch, docker_mod=docker_mod)
    docker_mod.from_env_error = RuntimeError("no daemon")
    result = mgr.ensure_resource("git")
    assert result["mode"] == "host_fallback"
    assert "docker build -t tm-resource-git" in result["detail"]
    assert docker_mod.from_env_calls >= 1


def test_image_missing_builds_and_creates(monkeypatch):
    images = _FakeImages(present=False)
    containers = _FakeContainers()
    mgr = _make_manager(monkeypatch, images=images, containers=containers)
    result = mgr.ensure_resource("git")
    assert len(images.build_calls) == 1
    assert len(containers.run_calls) == 1
    assert result == {
        "mode": "containerized",
        "container_id": "new-1",
        "status": "running",
        "image": rcm.RESOURCE_IMAGE_TAG,
        "detail": "",
    }


def test_image_build_fails_host_fallback(monkeypatch):
    images = _FakeImages(present=False, build_error=RuntimeError("build boom"))
    containers = _FakeContainers()
    mgr = _make_manager(monkeypatch, images=images, containers=containers)
    result = mgr.ensure_resource("git")
    assert result["mode"] == "host_fallback"
    assert "docker build -t tm-resource-git" in result["detail"]
    assert len(images.build_calls) == 1
    assert len(containers.run_calls) == 0


def test_container_missing_creates(monkeypatch):
    images = _images_present()
    containers = _FakeContainers()
    mgr = _make_manager(monkeypatch, images=images, containers=containers)
    result = mgr.ensure_resource("git")
    assert result["mode"] == "containerized"
    assert result["container_id"] == "new-1"
    assert len(containers.run_calls) == 1


def test_container_stopped_is_started(monkeypatch):
    images = _images_present()
    mgr = _make_manager(monkeypatch, images=images)
    container = _FakeContainer(
        container_id="c-stop",
        name=mgr.container_name,
        image_id="sha256:img-current",
        status="exited",
        labels=_labels_for(mgr),
    )
    containers = _FakeContainers([container])
    monkeypatch.setattr(mgr.client, "containers", containers)
    result = mgr.ensure_resource("git")
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-stop"
    assert container.start_calls == 1
    assert len(containers.run_calls) == 0


def test_stale_image_recreated(monkeypatch):
    images = _images_present(image_id="sha256:img-current")
    mgr = _make_manager(monkeypatch, images=images)
    container = _FakeContainer(
        container_id="c-old",
        name=mgr.container_name,
        image_id="sha256:img-old",
        status="running",
        labels=_labels_for(mgr),
    )
    containers = _FakeContainers([container])
    monkeypatch.setattr(mgr.client, "containers", containers)
    result = mgr.ensure_resource("git")
    assert container.remove_calls == 1
    assert len(containers.run_calls) == 1
    assert result["mode"] == "containerized"
    assert result["container_id"] == "new-1"


def test_wrong_kind_container_recreated(monkeypatch):
    images = _images_present()
    mgr = _make_manager(monkeypatch, images=images)
    container = _FakeContainer(
        container_id="c-py",
        name=mgr.container_name,
        image_id="sha256:img-current",
        status="running",
        labels=_labels_for(mgr, kind="python"),
    )
    containers = _FakeContainers([container])
    monkeypatch.setattr(mgr.client, "containers", containers)
    result = mgr.ensure_resource("git")
    assert container.remove_calls == 1
    assert len(containers.run_calls) == 1
    assert result["container_id"] == "new-1"


def test_container_create_fails_host_fallback(monkeypatch):
    images = _images_present()
    containers = _FakeContainers(run_error=RuntimeError("run boom"))
    mgr = _make_manager(monkeypatch, images=images, containers=containers)
    result = mgr.ensure_resource("git")
    assert result["mode"] == "host_fallback"
    assert "failed to create resource container" in result["detail"]
    assert "run boom" in result["detail"]


def test_correct_container_reused(monkeypatch):
    images = _images_present()
    mgr = _make_manager(monkeypatch, images=images)
    container = _FakeContainer(
        container_id="c-ok",
        name=mgr.container_name,
        image_id="sha256:img-current",
        status="running",
        labels=_labels_for(mgr),
    )
    containers = _FakeContainers([container])
    monkeypatch.setattr(mgr.client, "containers", containers)
    result = mgr.ensure_resource("git")
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-ok"
    assert container.start_calls == 0
    assert len(containers.run_calls) == 0


def test_policy_denied_unavailable(monkeypatch):
    images = _images_present()
    mgr = _make_manager(
        monkeypatch,
        images=images,
        session_permissions={"container": False, "network": "banned"},
    )
    result = mgr.ensure_resource("git")
    assert result["mode"] == "unavailable"
    assert "disabled/denied" in result["detail"]
    assert "container" in result["detail"].lower()


def test_policy_allows_container(monkeypatch):
    images = _images_present()
    containers = _FakeContainers()
    mgr = _make_manager(
        monkeypatch,
        images=images,
        containers=containers,
        session_permissions={"container": True, "network": "none"},
    )
    result = mgr.ensure_resource("git")
    assert result["mode"] == "containerized"
    assert len(containers.run_calls) == 1


def test_images_get_error_guarded(monkeypatch):
    images = _images_present(get_error=RuntimeError("images.get boom"))
    mgr = _make_manager(monkeypatch, images=images)
    container = _FakeContainer(
        container_id="c-ok",
        name=mgr.container_name,
        image_id="sha256:img-current",
        status="running",
        labels=_labels_for(mgr),
    )
    containers = _FakeContainers([container])
    monkeypatch.setattr(mgr.client, "containers", containers)
    # Warm the image cache so ensure_resource skips _ensure_resource_image's
    # images.get; the failure then surfaces only in the stale-image check,
    # which must be guarded (never raises, never blocks reuse).
    rcm._RESOURCE_IMAGE_READY = True
    result = mgr.ensure_resource("git")
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-ok"
    assert len(containers.run_calls) == 0
