"""Container-exec INTERFACE CONTRACT: both managers honour turn cancellation.

The C11-prime bug happened because cancel polling was implemented in ONE
manager (``infra.container_manager.ContainerManager.exec``) and forgotten in the
other (``infra.resource_container_manager.ResourceContainerManager.exec``): the
resource manager waited on a single blocking ``thread.join(timeout)``, so a
cancelled turn could not be honoured until the whole timeout expired (300 s for
a git command).

Two managers, two copies of the same wait/timeout/interrupt logic, is exactly
the arrangement where one copy silently rots.  This test therefore runs the
SAME scenario against BOTH entry points and demands the SAME answer:

* the call RETURNS (it does not block for the timeout) once the turn is
  cancelled, within a bounded margin;
* it reports ``status="interrupted"`` / ``interrupted=True`` / the operator's
  ``reason`` / ``exit_code=None`` with empty stdio;
* it does NOT kill and does NOT remove its container -- interrupting the WAIT
  is not the same as killing the process, and for the git resource container
  (a long-lived, workspace-scoped persistent sandbox) a kill would destroy the
  workspace's git sandbox.

Adding a third manager, or fixing one copy and not the other, goes RED here.

The two managers have different call shapes (``exec(container_id, command=...)``
vs ``exec(argv, ...)``), so each is driven through a tiny adapter; everything
below the adapter is the real manager.  Fakes are self-contained copies (a hung
``exec_run`` released on teardown, with kill/remove recorded) so this file
imports nothing from ``tests/test_container_exec_interrupt.py`` or
``tests/test_resource_container_cancel.py``.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

import infra.container_manager as container_manager
import infra.resource_container_manager as rcm
from infra.container_manager import ContainerManager
from tools import cancellation

# The production cancel-poll slice both managers use.
POLL_SLICE = 0.5
# The turn is cancelled well inside the first slice.
CANCEL_AFTER = 0.2

# Bound on the RETURN (not on the payload's own ``elapsed``).  Expected is
# CANCEL_AFTER plus at most one POLL_SLICE, i.e. ~0.7 s.  The margin is
# deliberately NOT 0.5 (= POLL_SLICE) and not a multiple that encodes the
# implementation: it only has to be "prompt" (nowhere near the 25/30 s timeout
# the fakes would otherwise block for) and it must not fail on a loaded CI box.
RETURN_MARGIN = 2.0

_REASON = "stopped_by_operator"


# ---------------------------------------------------------------------------
# Fakes: ContainerManager (docker SDK) side
# ---------------------------------------------------------------------------
class _BlockingContainer:
    """A container whose ``exec_run`` blocks until *release* is set."""

    def __init__(self, container_id="c1", block_seconds=30.0):
        self.id = container_id
        self.name = container_id
        self.status = "running"
        self.labels = {}
        self.attrs = {"State": {"Status": "running"}}
        self.release = threading.Event()
        self.block_seconds = block_seconds
        self.killed = []
        self.removed = []
        self.exec_calls = []

    def reload(self):
        return None

    def kill(self):
        self.killed.append(True)
        self.release.set()

    def stop(self, **kwargs):
        return None

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        self.release.wait(self.block_seconds)
        return (0, (b"out", b"err"))


class _Containers:
    def __init__(self, container):
        self._container = container

    def get(self, container_id):
        return self._container

    def list(self, all=False, filters=None):
        return []


class _Client:
    def __init__(self, container):
        self.containers = _Containers(container)

    def ping(self):
        return True


def _make_container_manager(container, tmp_path):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = str(tmp_path)
    cm.image = "agent-executor"
    cm.network = "none"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm.session_id = "s1"
    cm.workspace_id = "ws-1"
    cm.session_permissions = {}
    cm._session_config = {}
    cm._containers = {}
    cm.client = _Client(container)
    cm.vault_root = str(tmp_path)
    cm.container_notes = {}
    cm.max_containers = 10
    cm.workspace_config = {}
    cm.dockerfile_path = None
    # Neutralise the guards that are not under test here.
    cm._agent_access_denial = lambda lifecycle_class: None
    cm.class_of = lambda ctr: "ephemeral"
    cm._check_exec_drift = lambda ctr, cid: ("allow", None)
    cm._exceeds_disk_quota = lambda ctr, mb: False
    cm._append_usage_log = lambda *a, **k: None
    cm.dropped = []
    cm._drop_container = lambda cid: cm.dropped.append(cid)
    return cm


# ---------------------------------------------------------------------------
# Fakes: ResourceContainerManager (persistent git sandbox) side
# ---------------------------------------------------------------------------
class _FakeImageRef:
    def __init__(self, image_id):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeExecContainer:
    """``exec_run`` blocks until *release*; kill/remove are only RECORDED."""

    def __init__(self, container_id, name, labels, release=None):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef("sha256:" + container_id)
        self.status = "running"
        self.labels = dict(labels)
        self.attrs = {"State": {"Status": "running"}}
        self.killed = []
        self.removed = []
        self.stopped = []
        self.exec_runs = 0
        self._release = release

    def start(self):
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


def _make_resource_manager(monkeypatch):
    """Build a real ``ResourceContainerManager`` over a fake, hung docker."""
    workspace_id = "ws-r"
    release = threading.Event()
    fake_docker = _FakeDockerModule()
    monkeypatch.setattr(rcm, "docker", fake_docker)
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    mgr = rcm.ResourceContainerManager(
        workspace_id=workspace_id,
        workspace_path="/tmp/tm-contract-ws",
        network_mode="none",
        vault_root="/tmp/tm-contract-vault",
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


# ---------------------------------------------------------------------------
# The adapter both managers are driven through
# ---------------------------------------------------------------------------
class _Harness:
    def __init__(self, kind, run, container, release):
        self.kind = kind
        self.run = run          # () -> payload dict
        self.container = container
        self.release = release


def _build_harness(kind, monkeypatch, tmp_path):
    if kind == "container_manager":
        ctr = _BlockingContainer()
        cm = _make_container_manager(ctr, tmp_path)
        return _Harness(
            kind,
            lambda: cm.exec("c1", command="git gc --aggressive", timeout=25),
            ctr,
            ctr.release,
        )
    assert kind == "resource_container_manager"
    mgr, seed, release = _make_resource_manager(monkeypatch)
    return _Harness(
        kind,
        lambda: mgr.exec(["git", "gc", "--aggressive"], timeout=30),
        seed,
        release,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolate the default vault + the module-scoped name-index/notes memos."""
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
    """Start and end every case from a pristine per-thread cancel state."""
    cancellation.unbind()
    cancellation.reset()
    try:
        yield
    finally:
        cancellation.unbind()
        cancellation.reset()


def _cancel_after(delay, token, reason=_REASON):
    def _fire():
        token.reason = reason
        token.set()

    timer = threading.Timer(delay, _fire)
    timer.start()
    return timer


# ---------------------------------------------------------------------------
# THE CONTRACT: both entry points answer a cancelled turn identically
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind", ["container_manager", "resource_container_manager"]
)
def test_cancelled_exec_returns_promptly_and_leaves_its_container_alone(
    kind, monkeypatch, tmp_path
):
    harness = _build_harness(kind, monkeypatch, tmp_path)
    token = threading.Event()
    cancellation.bind_turn_cancel(token)
    timer = _cancel_after(CANCEL_AFTER, token)
    try:
        started = time.monotonic()
        result = harness.run()
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        harness.release.set()
        cancellation.unbind()

    # (1) It RETURNED, promptly, instead of blocking for the whole timeout.
    assert elapsed < RETURN_MARGIN, (
        f"{kind}: a cancelled exec took {elapsed:.2f}s "
        f"(expected < {RETURN_MARGIN}s; poll slice is {POLL_SLICE}s)"
    )

    # (2) Same cancellation report from both managers.
    assert result["status"] == "interrupted", (kind, result)
    assert result["interrupted"] is True, (kind, result)
    assert result["reason"] == _REASON, (kind, result)
    assert result["exit_code"] is None, (kind, result)
    assert result["stdout"] == "", (kind, result)
    assert result["stderr"] == "", (kind, result)

    # (3) Neither manager kills or drops its container: interrupting the WAIT
    #     is not the same as killing the process.
    assert harness.container.killed == [], (
        f"{kind}: the cancel path killed its container"
    )
    assert harness.container.removed == [], (
        f"{kind}: the cancel path removed its container"
    )
