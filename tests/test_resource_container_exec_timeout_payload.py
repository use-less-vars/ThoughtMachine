"""C13 steps 1+2: the exec() timeout contract -- payload + in-place recovery.

Pins what a timed-out ``ResourceContainerManager.exec()`` must do:

* it still raises ``TimeoutError`` (the exception TYPE is unchanged -- callers
  and ``tests/test_resource_container_exec_timeout_survival.py`` pin it), but
  the exception now carries a ``container_payload`` attribute;
* the payload shape is exactly ``timed_out``, ``exit_code`` (-2),
  ``timeout_seconds``, ``container_id``, ``container_killed``,
  ``container_removed`` (always False) and ``container_restarted``.  There is
  deliberately NO ``retryable`` key: retryability is verb-dependent and belongs
  to the tool layer;
* the container is recovered IN PLACE -- ``restart()`` when the handle exposes
  it, otherwise ``kill()`` + ``start()`` -- and is NEVER removed.

Self-contained fakes (no docker, no network, no sleeps), mirroring the house
shape in ``tests/test_resource_container_exec_timeout_survival.py``.
"""

from __future__ import annotations

import copy
import threading

import pytest

import infra.container_manager as container_manager
import infra.resource_container_manager as rcm


# ---------------------------------------------------------------------------
# Minimal fake shapes
# ---------------------------------------------------------------------------


class _FakeImageRef:
    def __init__(self, image_id):
        self.id = image_id

    def __str__(self):
        return self.id


class _HungExecContainer:
    """A hung fake container WITHOUT ``restart`` (forces the kill+start fallback)."""

    def __init__(self, container_id, name, labels, release):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef("sha256:" + container_id)
        self.status = "running"
        self.labels = dict(labels)
        self.attrs = {"State": {"Status": "running"}}
        self.removed = []
        self.kill_calls = []
        self.start_calls = []
        self.exec_runs = 0
        self._release = release

    def start(self):
        self.start_calls.append(True)
        self.status = "running"

    def kill(self):
        self.kill_calls.append(True)

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


class _RestartableHungExecContainer(_HungExecContainer):
    """The same hung fake, but exposing ``restart()`` (the preferred recovery)."""

    def __init__(self, container_id, name, labels, release, restart_raises=False):
        super().__init__(container_id, name, labels, release)
        self.restart_calls = []
        self.restart_raises = restart_raises

    def restart(self):
        self.restart_calls.append(True)
        if self.restart_raises:
            raise RuntimeError("restart() exploded")
        self.status = "running"


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


def _make_mgr(
    monkeypatch, container_cls=_RestartableHungExecContainer, **container_kwargs
):
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
    seed = container_cls(
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
        **container_kwargs,
    )
    fake_docker._client.containers.containers.append(seed)
    return mgr, seed, release


# ---------------------------------------------------------------------------
# (1) restart path: the container is recovered in place, never killed/removed
# ---------------------------------------------------------------------------


def test_timeout_payload_reports_in_place_restart(monkeypatch):
    """A ``restart()``-capable container is restarted, not killed, and described."""
    mgr, ctr, release = _make_mgr(monkeypatch)
    try:
        with pytest.raises(TimeoutError) as excinfo:
            mgr.exec(["sleep", "inf"], timeout=0.02)

        payload = excinfo.value.container_payload
        assert payload["timed_out"] is True
        assert payload["exit_code"] == -2
        assert payload["timeout_seconds"] == 0.02
        assert payload["container_id"] == "c-res"
        assert payload["container_removed"] is False
        assert payload["container_restarted"] is True
        assert payload["container_killed"] is False
        # Retryability is verb-dependent: it belongs to the tool layer.
        assert "retryable" not in payload

        # The restart path was actually taken, and nothing else was touched.
        assert ctr.restart_calls == [True]
        assert ctr.kill_calls == []
        assert ctr.start_calls == []
        assert ctr.removed == []
    finally:
        # Release the leaked daemon thread so it unwinds before teardown.
        release.set()


# ---------------------------------------------------------------------------
# (2) fallback path: no restart() on the handle -> kill() then start()
# ---------------------------------------------------------------------------


def test_timeout_payload_reports_kill_start_fallback(monkeypatch):
    """Without ``restart`` the ladder falls back to kill()+start(), still no remove."""
    mgr, ctr, release = _make_mgr(monkeypatch, container_cls=_HungExecContainer)
    try:
        with pytest.raises(TimeoutError) as excinfo:
            mgr.exec(["sleep", "inf"], timeout=0.02)

        payload = excinfo.value.container_payload
        assert payload["timed_out"] is True
        assert payload["exit_code"] == -2
        assert payload["container_killed"] is True
        assert payload["container_restarted"] is True
        assert payload["container_removed"] is False

        assert ctr.kill_calls == [True]
        assert ctr.start_calls == [True]
        assert ctr.removed == []
    finally:
        release.set()


# ---------------------------------------------------------------------------
# (3) a raising restart() must not mask the TimeoutError; fallback still runs
# ---------------------------------------------------------------------------


def test_restart_failure_falls_back_and_timeout_error_still_escapes(monkeypatch):
    """A raising ``restart()`` is swallowed; kill()+start() runs, error escapes."""
    mgr, ctr, release = _make_mgr(monkeypatch, restart_raises=True)
    try:
        with pytest.raises(
            TimeoutError, match="Git resource command timed out after 0.02 seconds"
        ) as excinfo:
            mgr.exec(["sleep", "inf"], timeout=0.02)

        payload = excinfo.value.container_payload
        assert payload["timed_out"] is True
        assert payload["exit_code"] == -2
        # The fallback ran after the failed restart, and nothing was removed.
        assert payload["container_killed"] is True
        assert payload["container_restarted"] is True
        assert payload["container_removed"] is False

        assert ctr.restart_calls == [True]
        assert ctr.kill_calls == [True]
        assert ctr.start_calls == [True]
        assert ctr.removed == []
    finally:
        release.set()
