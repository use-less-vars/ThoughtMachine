"""A cancelled turn must not block ``ResourceContainerManager.exec()``.

Invariant under test (C11-prime): ``exec()`` use to wait on ONE blocking
``thread.join(timeout)``, so a turn cancelled mid-command could not be
honoured until the whole timeout expired (300 s for a git command). It now
waits in 0.5 s slices and polls the CURRENT TURN's cancellation token between
them, mirroring ``ContainerManager.exec`` (infra/container_manager.py:2554-2638).

The fix deliberately stops at interrupting the WAIT:

* this container is a long-lived, workspace-scoped PERSISTENT git sandbox, so
  the timeout path's ``container.kill()`` must NOT run on the interrupt path --
  it would destroy the workspace's git sandbox;
* ``exec_run`` hands us no killable handle, so the command may still be
  running inside the untouched container and the payload says exactly that.

Self-contained fakes copied from
``tests/test_resource_container_exec_timeout_recovery.py`` / ``..._survival.py``
(a hung ``exec_run`` released by a ``threading.Event`` in teardown, with
recorded kill/remove), so this file stands alone and imports nothing from them.

Cancellation-token hygiene: every test binds a REAL per-thread turn token, and
the autouse fixture calls ``cancellation.unbind()`` AND ``cancellation.reset()``
before and after each test. Before the C12 fix the per-thread FALLBACK event
armed by ``interrupt()`` stayed set after ``unbind()``, which is exactly the
kind of state leak that would make these tests pass or fail depending on
ordering; ``unbind()`` now clears it, and the fixture does not rely on that
alone.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

import infra.container_manager as container_manager
import infra.resource_container_manager as rcm
from tools import cancellation


class _FakeImageRef:
    def __init__(self, image_id):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeExecContainer:
    """A fake container whose ``exec_run`` blocks until *release* is set.

    ``release=None`` makes ``exec_run`` return immediately (the clean-exec
    case). Kill/remove are RECORDED, never performed: the point of every test
    here is which of them the manager chose to call.
    """

    def __init__(self, container_id, name, labels, release=None):
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
        self.exec_runs = 0
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
        self.exec_runs += 1
        if self._release is not None:
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


@pytest.fixture(autouse=True)
def _clean_cancel_token():
    """Start and end every test from a pristine per-thread cancel state."""
    cancellation.unbind()
    cancellation.reset()
    try:
        yield
    finally:
        cancellation.unbind()
        cancellation.reset()


def _make_mgr(monkeypatch, hang=True):
    """Build a manager over a fake docker whose exec hangs (or returns)."""
    workspace_id = "ws-r"
    release = threading.Event() if hang else None
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
    seed = _FakeExecContainer(
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


def _cancel_after(delay, token, reason="stopped_by_operator"):
    """Arm a timer that cancels the bound turn token after *delay* seconds."""
    def _fire():
        token.reason = reason
        token.set()

    timer = threading.Timer(delay, _fire)
    timer.start()
    return timer


# ---------------------------------------------------------------------------
# 1. The core fix: a cancelled turn returns promptly, container untouched
# ---------------------------------------------------------------------------
def test_interrupted_wait_returns_promptly_and_leaves_the_container_alone(monkeypatch):
    """A turn cancel must END the wait, without killing or removing anything."""
    mgr, ctr, release = _make_mgr(monkeypatch)
    token = threading.Event()
    cancellation.bind_turn_cancel(token)
    timer = _cancel_after(0.2, token)
    try:
        wall_started = time.monotonic()
        result = mgr.exec(["git", "gc", "--aggressive"], timeout=30)
        wall_elapsed = time.monotonic() - wall_started
    finally:
        timer.cancel()
        release.set()
        cancellation.unbind()

    # The payload the cancelled turn gets back.
    assert result["status"] == "interrupted"
    assert result["interrupted"] is True
    assert result["reason"] == "stopped_by_operator"
    assert result["exit_code"] is None
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["container_killed"] is False
    assert result["container_removed"] is False
    assert result["exec"] == "container_exec"
    assert result["container_id"] == "c-res"
    assert result["command"] == "git gc --aggressive"
    assert "not killed" in result["note"].lower()
    assert "not removed" in result["note"].lower()

    # Promptness: the wait is sliced at 0.5 s, so a 0.2 s cancel must land in
    # roughly one slice. The bound used here is a 10x margin (5.0 s against the
    # 0.5 s production slice) -- deliberately NOT equal to the slice constant,
    # so the assertion pins "prompt" without re-encoding the implementation.
    assert result["elapsed"] < 5.0
    assert wall_elapsed < 5.0

    # And the container survived: this is a persistent git sandbox.
    assert ctr.killed == []
    assert ctr.removed == []
    assert ctr.stopped == []


# ---------------------------------------------------------------------------
# 2. The interrupted payload keeps the keys the git tool hard-indexes
# ---------------------------------------------------------------------------
def test_interrupted_payload_keeps_the_three_keys_the_git_tool_indexes(monkeypatch):
    """``GitInfoTool._exec_container_raw`` hard-indexes three payload keys.

    That method (tools/git_info_tool.py) ends with
    ``return (result["exit_code"], result["stdout"], result["stderr"])``, so an
    interrupted payload missing any of the three would raise ``KeyError``
    instead of surfacing a cancelled command. Pin all three, and pin that the
    tuple-unpack shape the caller uses actually works on it.
    """
    mgr, ctr, release = _make_mgr(monkeypatch)
    token = threading.Event()
    cancellation.bind_turn_cancel(token)
    timer = _cancel_after(0.2, token, reason="stopped_by_operator")
    try:
        result = mgr.exec(["git", "status"], timeout=30)
    finally:
        timer.cancel()
        release.set()
        cancellation.unbind()

    assert result["status"] == "interrupted"
    for key in ("exit_code", "stdout", "stderr"):
        assert key in result, f"interrupted payload is missing {key!r}"

    # Exactly what GitInfoTool._exec_container_raw does with the payload.
    unpacked = (result["exit_code"], result["stdout"], result["stderr"])
    assert unpacked == (None, "", "")
    assert ctr.killed == [] and ctr.removed == []


# ---------------------------------------------------------------------------
# 3. The happy path is untouched by the cancel polling
# ---------------------------------------------------------------------------
def test_clean_exec_is_unaffected_by_the_cancel_polling(monkeypatch):
    """A bound-but-never-set token must not change the success payload."""
    mgr, ctr, _release = _make_mgr(monkeypatch, hang=False)
    token = threading.Event()
    cancellation.bind_turn_cancel(token)
    try:
        result = mgr.exec(["git", "rev-parse", "HEAD"], timeout=30)
    finally:
        cancellation.unbind()

    # Byte-identical success contract: exactly the three keys, nothing added.
    assert result == {"exit_code": 0, "stdout": "", "stderr": ""}
    assert ctr.exec_runs == 1
    assert ctr.killed == []
    assert ctr.removed == []


# ---------------------------------------------------------------------------
# 4. The timeout contract is unchanged by this fix
# ---------------------------------------------------------------------------
def test_timeout_still_raises_and_still_does_not_remove(monkeypatch):
    """No cancel: still kill best-effort, still ``TimeoutError``, still no remove."""
    mgr, ctr, release = _make_mgr(monkeypatch)
    try:
        with pytest.raises(
            TimeoutError, match="Git resource command timed out after 0.02 seconds"
        ):
            mgr.exec(["sleep", "inf"], timeout=0.02)
    finally:
        release.set()

    assert ctr.killed != []          # timeout path still kills the container
    assert ctr.removed == []         # ...and still never removes it
