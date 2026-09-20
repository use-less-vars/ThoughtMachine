"""RED: a timed-out ResourceContainerManager.exec() must NOT self-destruct.

Invariant under test (COMMIT 2 of fix/git-host-fallback-closure): when a git
resource command exceeds its ``timeout``, ``exec()`` raises ``TimeoutError`` but
the resource container must SURVIVE (still present in ``docker ps -a``) so the
next call can reuse/restart it. The pre-fix code path killed the container and
called ``self.remove()``, tearing it down entirely; this test guards against
that regression.

Self-contained fakes (mirror the minimal house shape in
``tests/test_container_env_injection.py``) with a hung ``exec_run`` that blocks
on a ``threading.Event`` released in teardown, plus recorded
kill/stop/remove so the survival assertion is explicit.
"""

import copy
import threading

import pytest

import infra.container_manager as container_manager
import infra.resource_container_manager as rcm


# ---------------------------------------------------------------------------
# Minimal fake shapes (self-contained; mirrors the house builder shape)
# ---------------------------------------------------------------------------


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
        self.started = []
        self.removed = []
        self.killed = []
        self.stopped = []
        self.exec_calls = []
        self._release = release

    def start(self):
        self.started.append(True)
        self.status = "running"

    def kill(self):
        self.killed.append(True)

    def stop(self, **kwargs):
        self.stopped.append(kwargs)

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        # Simulate a hung command: block until the test releases us.
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


# ---------------------------------------------------------------------------
# Fixtures / builder
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolate the default vault + module-scoped name-index/notes memos."""
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
    workspace_path = "/tmp/tm-env-ws"
    release = threading.Event()
    fake_docker = _FakeDockerModule()
    monkeypatch.setattr(rcm, "docker", fake_docker)
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    mgr = rcm.ResourceContainerManager(
        workspace_id=workspace_id,
        workspace_path=workspace_path,
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


# ---------------------------------------------------------------------------
# RED test
# ---------------------------------------------------------------------------


def test_exec_timeout_does_not_remove_resource_container(monkeypatch):
    """On exec() timeout the resource container must survive (not removed)."""
    mgr, seed, release = _make_mgr(monkeypatch)
    try:
        with pytest.raises(TimeoutError):
            mgr.exec(["sleep", "inf"], timeout=0.02)

        # Invariant: the container still exists (docker ps -a). The timeout
        # guard must NOT run the self-destruct ``self.remove()``.
        assert seed.removed == []
    finally:
        # Release the leaked daemon thread so it unwinds before teardown.
        release.set()
