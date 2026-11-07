"""Regression tests -- worker teardown must NOT reap non-ephemeral
worker-labelled containers (bugfix ``worker-teardown-reaps-persistent-containers``).

Two orthogonal contracts:

  T1 WRITE-SIDE (``infra/container_manager.py::ContainerManager._fresh_start``).
     The worker-ownership label (``thoughtmachine.worker``) is a TEARDOWN
     marker only, so it is stamped ONLY on ephemeral creates. A non-ephemeral
     (persistent) worker create must carry NO worker label. Ephemeral creates
     are byte-for-byte unchanged (label value still == owner identity).

  T2 READ-SIDE (``tools/workspace/worker_container.py`` +
     ``tools/workspace/worker_thread.py``). Worker teardown -- both the
     module-level ``cleanup_worker_containers`` and
     ``WorkerThread._cleanup_worker_containers`` -- SKIPS a worker-labelled
     container whose canonical ``ContainerRecord`` lifecycle class is not
     ephemeral. FAIL-CLOSED on an unresolvable class (recordless container):
     the class resolves to ``LIFECYCLE_PERSISTENT``, so such a container is
     EXCLUDED and never reaped.

Hermetic: all searching for these seams used ``FileSearchTool`` (regex,
per-extension) -- no shell ``grep``/``find``. Mocks only: no real Docker, no
real container records written (``find_by_docker_label`` is patched on the
read side).

Run:  python3 -m pytest tests/test_worker_teardown_lifecycle_exclusion.py -v
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import infra.container_manager as container_manager  # noqa: E402
from infra.container_manager import ContainerManager  # noqa: E402
from thoughtmachine.container_record import (  # noqa: E402
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    RECORD_LABEL_KEY,
    load_record,
)
import tools.workspace.worker_container as worker_container  # noqa: E402
from tools.workspace.worker_container import (  # noqa: E402
    _container_lifecycle_class,
    cleanup_worker_containers,
    is_worker_owned_container,
    is_worker_teardown_excluded_container,
)
from tools.workspace.worker import WorkerThread  # noqa: E402

WORKER_LABEL = "thoughtmachine.worker"
OWNER = "s1:w-x"  # "<session_id>:<worker_name>"


# ===========================================================================
# Fakes (write side) -- mirror tests/test_container_record_hook.py so the real
# ContainerManager._fresh_start path (admission + record_creation + run) runs
# against a fake docker client.
# ===========================================================================


class _FakeCtr:
    """Minimal stand-in for a docker container returned by containers.run."""

    def __init__(self, container_id="docker-1", name="ctr", labels=None):
        self.id = container_id
        self.name = name
        self.status = "created"
        self.labels = dict(labels or {})
        self.attrs = {"State": {"Status": "created"}}

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        pass

    def remove(self, **kwargs):
        pass

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        return (0, (b"", b""))


class _FakeContainers:
    def __init__(self, containers=None):
        self.containers = list(containers or [])
        self.run_calls = []
        self.list_calls = []

    def list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        return copy.copy(self.containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        name = kwargs.get("name") or "run-ctr"
        ctr = _FakeCtr("c-run-1", name=name, labels=kwargs.get("labels") or {})
        self.containers.append(ctr)
        return ctr


class _FakeDockerClient:
    def __init__(self):
        self.containers = _FakeContainers()

    def ping(self):
        return True


def _make_container_manager(client=None, *, workspace_id="w1", session_id="s1"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = {"use_container_registry": False}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client if client is not None else _FakeDockerClient()
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


# ===========================================================================
# T1 WRITE-SIDE
# ===========================================================================


def test_fresh_start_persistent_omits_worker_label(monkeypatch):
    """A persistent worker create carries NO ``thoughtmachine.worker`` label.

    RED before the class-gate: ``if worker_name:`` stamped the label on every
    worker create regardless of lifecycle class.
    """
    monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))
    client = _FakeDockerClient()
    cm = _make_container_manager(client, workspace_id="ws-lc-p")

    result = cm.start(
        image="agent-executor",
        name="agent-exec-persistent",
        worker_name=OWNER,
        lifecycle_class=LIFECYCLE_PERSISTENT,
    )

    assert result["id"] == "c-run-1"
    labels = client.containers.run_calls[-1]["kwargs"]["labels"]
    assert WORKER_LABEL not in labels, (
        "persistent worker create must not carry the teardown-ownership label; "
        f"labels={labels}"
    )
    # The create is still recorded as persistent (record path unchanged).
    assert RECORD_LABEL_KEY in labels
    record = load_record("ws-lc-p", labels[RECORD_LABEL_KEY])
    assert record is not None
    assert record.lifecycle_class == LIFECYCLE_PERSISTENT


def test_fresh_start_ephemeral_stamps_worker_label(monkeypatch):
    """An ephemeral worker create still carries the worker label (verbatim)."""
    monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))
    client = _FakeDockerClient()
    cm = _make_container_manager(client, workspace_id="ws-lc-e")

    result = cm.start(
        image="agent-executor",
        name="agent-exec-ephemeral",
        worker_name=OWNER,
        lifecycle_class=LIFECYCLE_EPHEMERAL,
    )

    assert result["id"] == "c-run-1"
    labels = client.containers.run_calls[-1]["kwargs"]["labels"]
    assert labels[WORKER_LABEL] == OWNER
    record = load_record("ws-lc-e", labels[RECORD_LABEL_KEY])
    assert record.lifecycle_class == LIFECYCLE_EPHEMERAL


def test_fresh_start_non_worker_ephemeral_has_no_worker_label(monkeypatch):
    """No worker_name -> no worker label, even on an ephemeral create."""
    monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))
    client = _FakeDockerClient()
    cm = _make_container_manager(client, workspace_id="ws-lc-nw")

    cm.start(
        image="agent-executor",
        name="agent-exec-noowner",
        lifecycle_class=LIFECYCLE_EPHEMERAL,
    )

    labels = client.containers.run_calls[-1]["kwargs"]["labels"]
    assert WORKER_LABEL not in labels


# ===========================================================================
# T2 READ-SIDE
# ===========================================================================


class _FakeRecord:
    def __init__(self, lifecycle_class):
        self.lifecycle_class = lifecycle_class


def _patch_find(monkeypatch, mapping):
    """Patch the canonical record lookup seam used by _container_lifecycle_class."""

    def _find(record_id, vault_root=None):
        return mapping.get(record_id)

    monkeypatch.setattr(worker_container, "find_by_docker_label", _find)


def _worker_labelled(container_id, name, labels):
    return SimpleNamespace(id=container_id, name=name, labels=labels)


def _called_with(mock_obj, container):
    """True if any recorded call passed the container object, its id or name."""
    for call in mock_obj.call_args_list:
        values = list(call.args or ()) + list((call.kwargs or {}).values())
        for v in values:
            if v == container or v == container.id or v == container.name:
                return True
    return False


# ---- predicate unit contract --------------------------------------------


def test_predicate_fails_closed_when_class_unresolvable(monkeypatch):
    """Recordless container -> LIFECYCLE_PERSISTENT -> EXCLUDED (fail-closed)."""
    _patch_find(monkeypatch, {})
    recordless = _worker_labelled("c-nol", "tm-worker-nol", {WORKER_LABEL: OWNER})
    assert _container_lifecycle_class(recordless) == LIFECYCLE_PERSISTENT
    assert is_worker_teardown_excluded_container(recordless) is True


def test_predicate_excludes_non_ephemeral(monkeypatch):
    _patch_find(
        monkeypatch,
        {
            "rec-p": _FakeRecord(LIFECYCLE_PERSISTENT),
            "rec-r": _FakeRecord(LIFECYCLE_RESOURCE),
            "rec-e": _FakeRecord(LIFECYCLE_EPHEMERAL),
        },
    )
    persistent = _worker_labelled(
        "c-p", "tm-worker-p", {RECORD_LABEL_KEY: "rec-p", WORKER_LABEL: OWNER}
    )
    resource = _worker_labelled(
        "c-r", "tm-worker-r", {RECORD_LABEL_KEY: "rec-r", WORKER_LABEL: OWNER}
    )
    ephemeral = _worker_labelled(
        "c-e", "tm-worker-e", {RECORD_LABEL_KEY: "rec-e", WORKER_LABEL: OWNER}
    )
    assert is_worker_teardown_excluded_container(persistent) is True
    assert is_worker_teardown_excluded_container(resource) is True
    assert is_worker_teardown_excluded_container(ephemeral) is False


def test_worker_owned_predicate_folds_in_class_exclusion(monkeypatch):
    """MUTATION GUARD: the OWNERSHIP predicate itself excludes non-ephemeral
    worker-labelled containers (class exclusion folded into the single seam).
    Nulling the class exclusion inside ``is_worker_owned_container`` turns this
    RED (persistent would then report True)."""
    _patch_find(
        monkeypatch,
        {
            "rec-p": _FakeRecord(LIFECYCLE_PERSISTENT),
            "rec-e": _FakeRecord(LIFECYCLE_EPHEMERAL),
        },
    )
    persistent = _worker_labelled(
        "c-p", "tm-worker-p", {RECORD_LABEL_KEY: "rec-p", WORKER_LABEL: OWNER}
    )
    ephemeral = _worker_labelled(
        "c-e", "tm-worker-e", {RECORD_LABEL_KEY: "rec-e", WORKER_LABEL: OWNER}
    )
    recordless = _worker_labelled("c-nol", "tm-worker-nol", {WORKER_LABEL: OWNER})
    foreign = _worker_labelled(
        "c-f", "tm-worker-f", {RECORD_LABEL_KEY: "rec-e", WORKER_LABEL: "s1:other"}
    )
    assert is_worker_owned_container(persistent, OWNER) is False
    assert is_worker_owned_container(ephemeral, OWNER) is True
    assert is_worker_owned_container(recordless, OWNER) is False  # fail-closed
    assert is_worker_owned_container(foreign, OWNER) is False  # exact-value


def test_predicate_absent_record_returns_persistent(monkeypatch):
    """A record label that resolves to NO record -> LIFECYCLE_PERSISTENT -> EXCLUDED."""
    _patch_find(monkeypatch, {})
    c = _worker_labelled(
        "c-miss", "tm-worker-miss", {RECORD_LABEL_KEY: "rec-gone", WORKER_LABEL: OWNER}
    )
    assert _container_lifecycle_class(c) == LIFECYCLE_PERSISTENT
    assert is_worker_teardown_excluded_container(c) is True


def test_predicate_record_store_error_fails_closed(monkeypatch):
    """A raising record lookup -> LIFECYCLE_PERSISTENT -> EXCLUDED (fail-closed)."""

    def _boom(record_id, vault_root=None):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(worker_container, "find_by_docker_label", _boom)
    c = _worker_labelled(
        "c-err", "tm-worker-err", {RECORD_LABEL_KEY: "rec-err", WORKER_LABEL: OWNER}
    )
    assert _container_lifecycle_class(c) == LIFECYCLE_PERSISTENT
    assert is_worker_teardown_excluded_container(c) is True


# ---- module-level cleanup_worker_containers ------------------------------


def test_module_cleanup_skips_persistent_worker_container(monkeypatch):
    persistent = _worker_labelled(
        "c-pers", "tm-worker-pers", {RECORD_LABEL_KEY: "rec-pers", WORKER_LABEL: OWNER}
    )
    ephemeral = _worker_labelled(
        "c-eph", "tm-worker-eph", {RECORD_LABEL_KEY: "rec-eph", WORKER_LABEL: OWNER}
    )
    recordless = _worker_labelled("c-nol", "tm-worker-nol", {WORKER_LABEL: OWNER})
    resource = SimpleNamespace(
        id="c-res", name="tm-resource-git", labels={"thoughtmachine.resource": "git"}
    )
    _patch_find(
        monkeypatch,
        {
            "rec-pers": _FakeRecord(LIFECYCLE_PERSISTENT),
            "rec-eph": _FakeRecord(LIFECYCLE_EPHEMERAL),
        },
    )

    cm = mock.Mock()
    cm.list_containers.return_value = [persistent, ephemeral, recordless, resource]

    cleanup_worker_containers(cm, OWNER)

    # Persistent worker-labelled container survives teardown.
    assert not _called_with(cm.stop, persistent), cm.stop.call_args_list
    assert not _called_with(cm.remove, persistent), cm.remove.call_args_list
    # Ephemeral worker-labelled container is reclaimed.
    assert _called_with(cm.stop, ephemeral), cm.stop.call_args_list
    assert _called_with(cm.remove, ephemeral), cm.remove.call_args_list
    # Recordless worker-labelled container is NOT reclaimed (fail-closed).
    assert not _called_with(cm.stop, recordless), cm.stop.call_args_list
    assert not _called_with(cm.remove, recordless), cm.remove.call_args_list
    # Resource containers remain excluded.
    assert not _called_with(cm.stop, resource)
    assert not _called_with(cm.remove, resource)


# ---- WorkerThread._cleanup_worker_containers -----------------------------


class _FakeEventBus:
    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


@pytest.fixture
def worker_plumbing():
    patchers = [
        mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
        mock.patch(
            "tools.workspace.worker.register_worker_event_bus",
            new=lambda *a, **k: None,
        ),
        mock.patch(
            "tools.workspace.worker.unregister_worker_event_bus",
            new=lambda *a, **k: None,
        ),
        mock.patch("tools.workspace.worker.global_event_bus", new=None),
    ]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()


def _make_thread(workspace_dir, name="w-x", session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": "You are a helpful worker assistant."},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=60,
    )


def test_worker_thread_cleanup_skips_persistent_worker_container(
    monkeypatch, tmp_path, worker_plumbing
):
    _patch_find(
        monkeypatch,
        {
            "rec-pers": _FakeRecord(LIFECYCLE_PERSISTENT),
            "rec-eph": _FakeRecord(LIFECYCLE_EPHEMERAL),
        },
    )
    persistent = _worker_labelled(
        "c-pers", "tm-worker-pers", {RECORD_LABEL_KEY: "rec-pers", WORKER_LABEL: OWNER}
    )
    ephemeral = _worker_labelled(
        "c-eph", "tm-worker-eph", {RECORD_LABEL_KEY: "rec-eph", WORKER_LABEL: OWNER}
    )
    recordless = _worker_labelled("c-nol", "tm-worker-nol", {WORKER_LABEL: OWNER})
    resource = SimpleNamespace(
        id="c-res", name="tm-resource-git", labels={"thoughtmachine.resource": "git"}
    )

    cm = mock.Mock()
    cm.list_containers.return_value = [persistent, ephemeral, recordless, resource]

    thread = _make_thread(str(tmp_path), name="w-x", session_id="s1")
    assert thread.owner_identity == OWNER
    thread._container_manager = cm
    thread._cleanup_worker_containers()

    assert not _called_with(cm.stop, persistent), cm.stop.call_args_list
    assert not _called_with(cm.remove, persistent), cm.remove.call_args_list
    assert _called_with(cm.stop, ephemeral), cm.stop.call_args_list
    assert _called_with(cm.remove, ephemeral), cm.remove.call_args_list
    assert not _called_with(cm.stop, recordless), cm.stop.call_args_list
    assert not _called_with(cm.remove, recordless), cm.remove.call_args_list
    assert not _called_with(cm.stop, resource)


def test_worker_thread_owned_predicate_folds_in_class_exclusion(
    monkeypatch, tmp_path, worker_plumbing
):
    """MUTATION GUARD (thread predicate): _is_worker_owned_container itself
    excludes non-ephemeral worker-labelled containers."""
    _patch_find(
        monkeypatch,
        {
            "rec-p": _FakeRecord(LIFECYCLE_PERSISTENT),
            "rec-e": _FakeRecord(LIFECYCLE_EPHEMERAL),
        },
    )
    persistent = _worker_labelled(
        "c-p", "tm-worker-p", {RECORD_LABEL_KEY: "rec-p", WORKER_LABEL: OWNER}
    )
    ephemeral = _worker_labelled(
        "c-e", "tm-worker-e", {RECORD_LABEL_KEY: "rec-e", WORKER_LABEL: OWNER}
    )
    recordless = _worker_labelled("c-nol", "tm-worker-nol", {WORKER_LABEL: OWNER})
    thread = _make_thread(str(tmp_path), name="w-x", session_id="s1")
    assert thread.owner_identity == OWNER
    assert thread._is_worker_owned_container(persistent) is False
    assert thread._is_worker_owned_container(ephemeral) is True
    assert thread._is_worker_owned_container(recordless) is False  # fail-closed


# ---- ExecutionTracker._terminate_container_exec call-site exclusion ------


class _FakeExecCM:
    """Minimal ExecutionTracker container-manager fake (records exec_run/stop)."""

    def __init__(self, containers=None):
        self.containers = containers or []
        self.exec_run_calls = []
        self.stopped = []

    def exec_run(self, container_id, cmd):
        self.exec_run_calls.append((container_id, cmd))

    def stop(self, container_id):
        self.stopped.append(container_id)

    def list_containers(self):
        return list(self.containers)


def _make_tracker():
    from tools.workspace.worker_execution import ExecutionTracker

    return ExecutionTracker()


def test_terminate_container_exec_skips_persistent_worker_container(monkeypatch):
    """ITEM 4: the class-exclusion guard at the ``_terminate_container_exec``
    call sites must SKIP a persistent worker-labelled container in BOTH the
    pid branch (no in-container kill) and the no-pid branch (no stop), while an
    ephemeral worker-labelled container is still reaped."""
    _patch_find(
        monkeypatch,
        {
            "rec-p": _FakeRecord(LIFECYCLE_PERSISTENT),
            "rec-e": _FakeRecord(LIFECYCLE_EPHEMERAL),
        },
    )
    persistent = {
        "container_id": "c-p", "name": "agent-exec-p",
        "labels": {RECORD_LABEL_KEY: "rec-p", WORKER_LABEL: "w1"},
    }
    ephemeral = {
        "container_id": "c-e", "name": "agent-exec-e",
        "labels": {RECORD_LABEL_KEY: "rec-e", WORKER_LABEL: "w1"},
    }

    # pid branch: persistent worker container -> NO docker exec kill.
    cm = _FakeExecCM(containers=[persistent, ephemeral])
    tracker = _make_tracker()
    tracker.add("e1", {"type": "container_exec", "container_id": "c-p", "pid": 4242})
    tracker.terminate_all("w1", cm, None, session_id="s1")
    assert cm.exec_run_calls == []
    assert cm.stopped == []
    assert tracker.active_count() == 0

    # no-pid branch: persistent worker container -> NO stop.
    tracker = _make_tracker()
    tracker.add("e1", {"type": "container_exec", "container_id": "c-p"})
    tracker.terminate_all("w1", cm, None, session_id="s1")
    assert cm.stopped == []
    assert tracker.active_count() == 0

    # ephemeral control (pid): in-container kill still happens.
    tracker = _make_tracker()
    tracker.add("e1", {"type": "container_exec", "container_id": "c-e", "pid": 1})
    tracker.terminate_all("w1", cm, None, session_id="s1")
    assert cm.exec_run_calls == [("c-e", ["kill", "1"])]

    # ephemeral control (no pid): container still stopped.
    tracker = _make_tracker()
    tracker.add("e1", {"type": "container_exec", "container_id": "c-e"})
    tracker.terminate_all("w1", cm, None, session_id="s1")
    assert cm.stopped == ["c-e"]


def test_terminate_container_exec_fails_closed_on_recordless(monkeypatch):
    """Fail-closed: a worker-labelled container with NO resolvable record is
    NOT reaped (its lifecycle class resolves to persistent -> excluded)."""
    _patch_find(monkeypatch, {})
    recordless = {
        "container_id": "c-nol", "name": "agent-exec-nol",
        "labels": {WORKER_LABEL: "w1"},
    }
    cm = _FakeExecCM(containers=[recordless])
    tracker = _make_tracker()
    tracker.add("e1", {"type": "container_exec", "container_id": "c-nol"})
    tracker.terminate_all("w1", cm, None, session_id="s1")
    assert cm.stopped == []


# ---- structural ----------------------------------------------------------


def test_seam_symbols_present():
    assert callable(cleanup_worker_containers)
    assert callable(is_worker_teardown_excluded_container)
    assert callable(_container_lifecycle_class)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
