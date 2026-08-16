"""Pure-unit tests for the module-level resource lifecycle orchestration.

Covers ``resource_status`` (read-only probe), ``cleanup_workspace_resources``
(workspace teardown) and ``sweep_stale_resource_containers`` (startup sweep)
in ``infra.resource_container_manager``. No Docker daemon required — the
``docker`` module attribute is replaced with fakes that mirror the SDK
surface the orchestration actually touches (``from_env``,
``client.containers.list/run``, ``client.images.get/build/remove``).

These tests deliberately use their OWN fakes (no imports from
``test_resource_ensure`` / ``test_resource_image_ensure``) so the suites stay
independent.
"""

import os

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
    """Container fake with start()/remove() recording + error injection.

    ``owner`` is the ``_FakeContainers`` collection; ``remove()`` removes
    the container from it (mirrors docker, where a removed container no
    longer shows up in later ``list`` calls).
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
    ):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef(image_id)
        self.status = status
        self.labels = labels or {}
        self.owner = owner
        self.remove_error = remove_error
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
            owner=self,
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
        rcm.REPO_REQUIREMENTS, rcm.REPO_RUNTIME_DOCKERFILE
    )


def _labels_for(workspace_id, kind="git", name="tm-res-abc-git"):
    return {
        rcm.ResourceContainerManager.WORKSPACE_LABEL: workspace_id,
        rcm.ResourceContainerManager.RESOURCE_LABEL: kind,
        rcm.ResourceContainerManager.CONTAINER_NAME_LABEL: name,
    }


def _overlay_build_hash(runtime_image_id="sha256:img-current"):
    return rcm.compute_git_overlay_build_hash(
        rcm.REPO_REQUIREMENTS,
        rcm.REPO_RUNTIME_DOCKERFILE,
        rcm.GIT_OVERLAY_DOCKERFILE,
        runtime_image_id,
    )


def _fresh_images(image_id="sha256:img-current", get_error=None):
    return _FakeImages(
        present=True,
        image_id=image_id,
        # tm-resource-git (the overlay) is labeled with the overlay build hash…
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash(image_id)},
        # …while tm-workspace-runtime:latest carries the runtime build hash.
        runtime_labels={rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()},
        get_error=get_error,
    )


def _monkeypatch_docker(monkeypatch, docker_mod):
    monkeypatch.setattr(rcm, "docker", docker_mod)
    return docker_mod


def _mount_source(mount):
    """Read the source path from a docker Mount (dict in docker-py 7.x)."""
    if isinstance(mount, dict):
        return mount.get("Source") or mount.get("source")
    return getattr(mount, "source", None)


def _mount_target(mount):
    """Read the target path from a docker Mount (dict in docker-py 7.x)."""
    if isinstance(mount, dict):
        return mount.get("Target") or mount.get("target")
    return getattr(mount, "target", None)


# ------------------------------------------------- resource_status tests


def test_resource_status_unknown_unavailable(monkeypatch):
    _monkeypatch_docker(monkeypatch, _FakeDockerModule(images=_fresh_images()))
    result = rcm.resource_status("nope", workspace_id="ws-1")
    assert result == {
        "mode": "unavailable",
        "container_id": None,
        "status": None,
        "image": None,
        "detail": "unknown resource 'nope'",
    }


def test_resource_status_docker_none_host_fallback(monkeypatch):
    monkeypatch.setattr(rcm, "docker", None)
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "host_fallback"
    assert "docker unavailable" in result["detail"]


def test_resource_status_from_env_raises_host_fallback(monkeypatch):
    docker_mod = _FakeDockerModule(images=_fresh_images())
    _monkeypatch_docker(monkeypatch, docker_mod)
    docker_mod.from_env_error = RuntimeError("no daemon")
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "host_fallback"
    assert "no daemon" in result["detail"]


def test_resource_status_image_missing_no_build(monkeypatch):
    images = _FakeImages(present=False)
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "containerized"
    assert "will auto-build on first use" in result["detail"]
    assert images.build_calls == []  # read-only probe must never build
    assert containers.run_calls == []


def test_resource_status_stale_hash_no_build(monkeypatch):
    images = _FakeImages(
        present=True, labels={rcm.RESOURCE_BUILD_HASH_LABEL: "deadbeef"}
    )
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "containerized"
    assert "will auto-build on first use" in result["detail"]
    assert images.build_calls == []
    assert containers.run_calls == []


def test_resource_status_container_missing(monkeypatch):
    images = _fresh_images()
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "containerized"
    assert result["container_id"] is None
    assert "container missing" in result["detail"]
    assert "will auto-provision on first use" in result["detail"]


def test_resource_status_container_stopped(monkeypatch):
    images = _fresh_images()
    container = _FakeContainer(
        container_id="c-stop",
        name="tm-res-abc-git",
        status="exited",
        labels=_labels_for("ws-1"),
    )
    containers = _FakeContainers([container])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-stop"
    assert result["status"] == "exited"
    assert "container stopped" in result["detail"]
    assert "will restart on first use" in result["detail"]


def test_resource_status_ok_running(monkeypatch):
    images = _fresh_images()
    container = _FakeContainer(
        container_id="c-ok",
        name="tm-res-abc-git",
        status="running",
        labels=_labels_for("ws-1"),
    )
    containers = _FakeContainers([container])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result == {
        "mode": "containerized",
        "container_id": "c-ok",
        "status": "running",
        "image": rcm.RESOURCE_IMAGE_TAG,
        "detail": "",
    }


def test_resource_status_policy_denied(monkeypatch):
    _monkeypatch_docker(monkeypatch, _FakeDockerModule(images=_fresh_images()))
    result = rcm.resource_status(
        "git",
        workspace_id="ws-1",
        session_permissions={"container": False, "network": "banned"},
    )
    assert result["mode"] == "unavailable"
    assert "disabled/denied" in result["detail"]
    assert "container" in result["detail"].lower()


def test_resource_status_no_workspace_id(monkeypatch):
    images = _fresh_images()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=_FakeContainers())
    )
    result = rcm.resource_status("git")
    assert result["mode"] == "containerized"
    assert "no workspace_id" in result["detail"]
    assert result["container_id"] is None


def test_resource_status_images_get_error_host_fallback(monkeypatch):
    images = _fresh_images(get_error=RuntimeError("daemon exploded"))
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=_FakeContainers())
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "host_fallback"
    assert "daemon exploded" in result["detail"]


def test_resource_status_list_error_host_fallback(monkeypatch):
    images = _fresh_images()
    containers = _FakeContainers(list_error=RuntimeError("list boom"))
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.resource_status("git", workspace_id="ws-1")
    assert result["mode"] == "host_fallback"
    assert "list boom" in result["detail"]


# ------------------------------------------- cleanup_workspace_resources tests


def test_cleanup_removes_workspace_containers_and_image(monkeypatch):
    images = _fresh_images()
    c_ws1 = _FakeContainer("c-ws1", name="tm-res-aaa-git", labels=_labels_for("ws-1"))
    containers = _FakeContainers([c_ws1])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.cleanup_workspace_resources("ws-1")
    assert c_ws1.remove_calls == 1
    assert result["removed_containers"] == 1
    assert result["removed_image"] is True
    assert images.remove_calls == [{"tag": rcm.RESOURCE_IMAGE_TAG, "force": True}]
    assert result["detail"] == ""


def test_cleanup_keeps_image_when_other_workspace_uses_it(monkeypatch):
    images = _fresh_images()
    c_ws1 = _FakeContainer("c-ws1", name="tm-res-aaa-git", labels=_labels_for("ws-1"))
    c_ws2 = _FakeContainer("c-ws2", name="tm-res-bbb-git", labels=_labels_for("ws-2"))
    containers = _FakeContainers([c_ws1, c_ws2])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.cleanup_workspace_resources("ws-1")
    assert c_ws1.remove_calls == 1
    assert c_ws2.remove_calls == 0
    assert result["removed_containers"] == 1
    assert result["removed_image"] is False
    assert images.remove_calls == []


def test_cleanup_leaves_non_resource_containers(monkeypatch):
    images = _fresh_images()
    c_res = _FakeContainer("c-res", name="tm-res-aaa-git", labels=_labels_for("ws-1"))
    c_agent = _FakeContainer(
        "c-agent",
        name="agent-exec-x",
        labels={rcm.ResourceContainerManager.WORKSPACE_LABEL: "ws-1"},
    )
    containers = _FakeContainers([c_res, c_agent])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.cleanup_workspace_resources("ws-1")
    assert c_res.remove_calls == 1
    assert c_agent.remove_calls == 0
    assert result["removed_containers"] == 1
    assert result["removed_image"] is True


def test_cleanup_docker_none(monkeypatch):
    monkeypatch.setattr(rcm, "docker", None)
    result = rcm.cleanup_workspace_resources("ws-1")
    assert result == {
        "removed_containers": 0,
        "removed_image": False,
        "detail": "docker SDK not installed",
    }


def test_cleanup_list_error_never_raises(monkeypatch):
    images = _fresh_images()
    containers = _FakeContainers(list_error=RuntimeError("list boom"))
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.cleanup_workspace_resources("ws-1")
    assert result["removed_containers"] == 0
    assert result["removed_image"] is False
    assert "list boom" in result["detail"]


def test_cleanup_remove_error_collected(monkeypatch):
    images = _fresh_images()
    c_ws1 = _FakeContainer(
        "c-ws1",
        name="tm-res-aaa-git",
        labels=_labels_for("ws-1"),
        remove_error=RuntimeError("remove boom"),
    )
    containers = _FakeContainers([c_ws1])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    result = rcm.cleanup_workspace_resources("ws-1")
    assert result["removed_containers"] == 0
    # failed removal keeps the container -> image still referenced -> kept
    assert result["removed_image"] is False
    assert "remove boom" in result["detail"]


def test_cleanup_resets_image_ready_cache(monkeypatch):
    images = _fresh_images()
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    rcm._RESOURCE_IMAGE_READY = True
    result = rcm.cleanup_workspace_resources("ws-1")
    assert result["removed_image"] is True
    assert rcm._RESOURCE_IMAGE_READY is False


# -------------------------------------- sweep_stale_resource_containers tests


def test_sweep_removes_orphan_keeps_registered(monkeypatch):
    c_orphan = _FakeContainer(
        "c-orphan", name="tm-res-x-git", labels=_labels_for("ws-orphan")
    )
    c_keep = _FakeContainer("c-keep", name="tm-res-y-git", labels=_labels_for("ws-1"))
    containers = _FakeContainers([c_orphan, c_keep])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=_fresh_images(), containers=containers)
    )
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert c_orphan.remove_calls == 1
    assert c_keep.remove_calls == 0
    assert result == {"removed": 1, "skipped_in_use": 1, "detail": ""}


def test_sweep_keeps_registered_stopped(monkeypatch):
    c_stopped = _FakeContainer(
        "c-stopped", name="tm-res-z-git", status="exited", labels=_labels_for("ws-1")
    )
    containers = _FakeContainers([c_stopped])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=_fresh_images(), containers=containers)
    )
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert c_stopped.remove_calls == 0
    assert result == {"removed": 0, "skipped_in_use": 1, "detail": ""}


def test_sweep_removes_container_without_workspace_label(monkeypatch):
    c_nolabel = _FakeContainer(
        "c-nolabel",
        name="tm-res-n-git",
        labels={rcm.ResourceContainerManager.RESOURCE_LABEL: "git"},
    )
    containers = _FakeContainers([c_nolabel])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=_fresh_images(), containers=containers)
    )
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert c_nolabel.remove_calls == 1
    assert result == {"removed": 1, "skipped_in_use": 0, "detail": ""}


def test_sweep_ignores_container_without_resource_label(monkeypatch):
    # No thoughtmachine.resource label -> invisible to the sweep's filter.
    c_plain = _FakeContainer(
        "c-plain",
        name="agent-exec-x",
        labels={rcm.ResourceContainerManager.WORKSPACE_LABEL: "ws-orphan"},
    )
    containers = _FakeContainers([c_plain])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=_fresh_images(), containers=containers)
    )
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert c_plain.remove_calls == 0
    assert result == {"removed": 0, "skipped_in_use": 0, "detail": ""}


def test_sweep_docker_none(monkeypatch):
    monkeypatch.setattr(rcm, "docker", None)
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert result == {
        "removed": 0,
        "skipped_in_use": 0,
        "detail": "docker SDK not installed",
    }


def test_sweep_list_error_never_raises(monkeypatch):
    containers = _FakeContainers(list_error=RuntimeError("list boom"))
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=_fresh_images(), containers=containers)
    )
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert result["removed"] == 0
    assert "list boom" in result["detail"]


def test_sweep_remove_error_collected(monkeypatch):
    c_orphan = _FakeContainer(
        "c-orphan",
        name="tm-res-x-git",
        labels=_labels_for("ws-orphan"),
        remove_error=RuntimeError("remove boom"),
    )
    containers = _FakeContainers([c_orphan])
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=_fresh_images(), containers=containers)
    )
    result = rcm.sweep_stale_resource_containers(["ws-1"])
    assert result["removed"] == 0
    assert "remove boom" in result["detail"]


# ------------------------------------- ensure_resource container mounts tests


def test_resource_container_has_no_venv_bind_mount(monkeypatch, tmp_path):
    """The resource container mounts the workspace ONLY (no .venv bind)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    images = _fresh_images()
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    mgr = rcm.ResourceContainerManager(workspace_id="ws-1", workspace_path=str(ws))
    mgr.ensure_resource("git")
    assert len(containers.run_calls) == 1
    kwargs = containers.run_calls[0]
    mounts = kwargs["mounts"]
    assert len(mounts) == 1
    for mount in mounts:
        source = _mount_source(mount)
        target = _mount_target(mount)
        assert source is not None
        assert target is not None
        assert os.path.basename(str(source)) != ".venv"
        assert os.path.basename(str(target)) != ".venv"
        assert not str(source).endswith(".venv")
        assert not str(target).endswith(".venv")
    # the single mount is the workspace bind at /workspace
    assert _mount_target(mounts[0]) == "/workspace"
    assert _mount_source(mounts[0]) == str(ws)


def test_resource_container_run_command_is_tail_dev_null(monkeypatch, tmp_path):
    """The resource container must stay alive via tail -f /dev/null."""
    ws = tmp_path / "ws"
    ws.mkdir()
    images = _fresh_images()
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    mgr = rcm.ResourceContainerManager(workspace_id="ws-1", workspace_path=str(ws))
    mgr.ensure_resource("git")
    assert len(containers.run_calls) == 1
    kwargs = containers.run_calls[0]
    assert kwargs["command"] == ["tail", "-f", "/dev/null"]
