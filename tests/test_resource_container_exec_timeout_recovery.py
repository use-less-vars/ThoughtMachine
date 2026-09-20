"""COMMIT 4 / gap iii: warm reuse after a git exec timeout.

Follow-on to ``tests/test_resource_container_exec_timeout_survival.py`` (COMMIT
2), which asserts only that a timed-out ``exec()`` does NOT remove the resource
container. It never asserts that a SUBSEQUENT call on the same manager actually
returns a result -- the warm-reuse behavior proven manually on the host.

This test closes that loop: after the hung command is released, the very next
``exec()`` on the same manager must return ``{"exit_code": 0, ...}`` without
raising, on the same (surviving) container.

NOTE: this is a GREEN regression guard, not a RED. The COMMIT 2 fix already
landed the "do not remove on timeout" behavior, so the second call succeeds on
the current tree; the gap was the absence of a test, not a live defect.
"""

from __future__ import annotations

import copy
import threading

import pytest

import infra.container_manager as container_manager
import infra.resource_container_manager as rcm


class _FakeImageRef:
    def __init__(self, image_id):
        self.id = image_id

    def __str__(self):
        return self.id


class _HungExecContainer:
    """A fake container whose ``exec_run`` blocks until *release* is set."""

    def __init__(self, container_id, name, labels, release):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef("sha256:" + container_id)
        self.status = "running"
        self.labels = dict(labels)
        self.attrs = {"State": {"Status": "running"}}
        self.removed = []
        self.killed = []
        self.exec_runs = 0
        self._release = release

    def start(self):
        self.status = "running"

    def kill(self):
        self.killed.append(True)

    def stop(self, **kwargs):
        pass

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        self.exec_runs += 1
        self._release.wait(timeout=5)
        return (0, (b"", b""))


class _FakeContainers:
    def __init__(self, containers=None):
        self.containers = list(containers or [])

    def list(self, all=False, filters=None):
        return copy.copy(self.containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)


class _FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(containers or [])

    def ping(self):
        return True


class _FakeDockerModule:
    def __init__(self, containers=None):
        self._client = _FakeDockerClient(containers)

    def from_env(self, **kwargs):
        return self._client


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

    def _reset():
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()
        container_manager._NAME_MIGRATED.clear()
        container_manager._NOTES_WARNED.clear()
        container_manager._NOTES_MIGRATED.clear()

    _reset()
    yield
    _reset()


def _make_mgr(monkeypatch):
    workspace_id = "ws-r"
    release = threading.Event()
    fake_docker = _FakeDockerModule()
    monkeypatch.setattr(rcm, "docker", fake_docker)
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    mgr = rcm.ResourceContainerManager(
        workspace_id=workspace_id,
        workspace_path="/tmp/tm-env-ws",
        network_mode="none",
        vault_root="/tmp/tm-env-vault",
        session_config=None,
        session_id="s-r",
    )
    seed = _HungExecContainer(
        container_id="c-res",
        name=mgr.container_name,
        labels={
            rcm.ResourceContainerManager.WORKSPACE_LABEL: workspace_id,
            rcm.ResourceContainerManager.RESOURCE_LABEL: (
                rcm.ResourceContainerManager.RESOURCE_KIND
            ),
            rcm.ResourceContainerManager.CONTAINER_NAME_LABEL: mgr.container_name,
        },
        release=release,
    )
    fake_docker._client.containers.containers.append(seed)
    return mgr, seed, release


def test_second_exec_succeeds_after_timeout(monkeypatch):
    """After an exec() timeout the next exec() must return a result (warm reuse)."""
    mgr, seed, release = _make_mgr(monkeypatch)
    try:
        with pytest.raises(TimeoutError):
            mgr.exec(["sleep", "inf"], timeout=0.02)

        # COMMIT 2 invariant: the container survived the timeout.
        assert seed.removed == []

        # Make the container responsive again, then drive a second call.
        release.set()
        result = mgr.exec(["git", "status"], timeout=5)

        assert result["exit_code"] == 0
        assert seed.exec_runs == 2
    finally:
        release.set()
