"""RED tests for A2 OOM observability (``ContainerManager``).

Deliverable D1 — at container CREATE the ``started`` lifecycle event's ``data``
must carry the declared memory budget (``mem_limit``), the OOM score
adjustment (``oom_score_adj``) and the container's ``lifecycle_class``.

Deliverable D2 — a command that exits 137 (128 + SIGKILL: the OOM signature)
must emit a DISTINCT record event (``container.command_oom_killed``, never the
``drift.container_absent`` type) carrying the exit code, the container's
lifecycle class, the DECLARED ``mem_limit`` budget, and ``OOMKilled`` when the
daemon reports it.

Harnesses mirror tests/test_record_state_advance_on_create.py (D1, the
``_fresh_start`` create path) and tests/test_container_exec_drift.py (D2, the
real ``exec`` path). The manager is built via ``ContainerManager.__new__`` so
the real Docker client is never touched.

RED mechanism — before the fix the create-time ``data`` dict has no
``mem_limit``/``oom_score_adj``/``lifecycle_class`` keys, and ``exec`` emits no
OOM event at all.
"""

from __future__ import annotations

import contextlib

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import LIFECYCLE_PERSISTENT, RECORD_LABEL_KEY


# ---------------------------------------------------------------------------
# Fakes / helpers — create path (D1)
# ---------------------------------------------------------------------------


class _FakeCtr:
    """Minimal docker container stand-in for the create path."""

    def __init__(self, id="ctr-1", name="c1", labels=None, attrs=None,
                 status="running"):
        self.id = id
        self.name = name
        self.labels = labels or {}
        self.attrs = attrs or {}
        self.status = status

    def reload(self):
        return None

    def stop(self, timeout=None):
        return None

    def remove(self, force=False):
        return None

    def kill(self):
        return None

    def exec_run(self, *args, **kwargs):
        return (0, (b"", b""))


class _FakeContainers:
    def __init__(self, get_result=None):
        self._get_result = get_result

    def list(self, all=False, filters=None):
        return []

    def get(self, name):
        return self._get_result

    def run(self, *args, **kwargs):
        return self._get_result


class _FakeVolumes:
    def get_or_create(self, name):
        return None

    def create(self, name):
        return None


class _FakeClient:
    def __init__(self, get_result=None):
        self.containers = _FakeContainers(get_result=get_result)
        self.volumes = _FakeVolumes()

    def ping(self):
        return True


class _SpyRecord:
    def __init__(self):
        self.id = "rec-spy"

    def attach(self, container, **kwargs):
        return None


def _spy_record_creation(spy):
    """Context-manager factory yielding *spy* (replaces module ``record_creation``)."""

    @contextlib.contextmanager
    def _cm(*args, **kwargs):
        yield spy

    return _cm


def _make_container_manager(client, tmp_path):
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
    cm.client = client
    cm._compute_config = lambda: ("none", "ro")
    cm.vault_root = str(tmp_path)
    cm.container_notes = {}
    cm.max_containers = 10
    cm.workspace_config = {}
    cm.dockerfile_path = None
    return cm


# ---------------------------------------------------------------------------
# Fakes / helpers — exec path (D2)
# ---------------------------------------------------------------------------


class _ExecFakeContainer:
    def __init__(self, container_id="c1", labels=None, attrs=None,
                 exec_result=None):
        self.id = container_id
        self.name = container_id
        self.status = "running"
        self.labels = dict(labels or {})
        self.attrs = attrs if attrs is not None else {"State": {"Status": "running"}}
        self.exec_calls = []
        self._exec_result = exec_result if exec_result is not None else (0, (b"out", b"err"))

    def reload(self):
        return None

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        return self._exec_result


class _ExecFakeContainers:
    def __init__(self, containers):
        self.containers = list(containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)


class _ExecFakeDockerClient:
    def __init__(self, containers):
        self.containers = _ExecFakeContainers(containers)


def _exec_attrs(network="none", workspace_rw=False, oom_killed=None):
    attrs = {
        "State": {"Status": "running"},
        "HostConfig": {"NetworkMode": network},
        "Mounts": [{"Destination": "/workspace", "RW": workspace_rw}],
    }
    if oom_killed is not None:
        attrs["State"]["OOMKilled"] = oom_killed
    return attrs


def _make_exec_cm(client, want=("none", "ro"), workspace_id="w1"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = "s1"
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm.client = client
    cm.mem_limit = "512m"
    cm.workspace_config = {"disk_quota_mb": 0}
    cm._compute_config = lambda *a, **k: want
    return cm


@pytest.fixture(autouse=True)
def _clear_memo():
    container_manager._EXEC_DRIFT_SEEN.clear()
    yield
    container_manager._EXEC_DRIFT_SEEN.clear()


@pytest.fixture
def events(monkeypatch):
    """Capture record events via a patched ``thoughtmachine.container_record``."""
    import thoughtmachine.container_record as cr

    recorded = []

    def _fake_append(workspace_id, record_id, event_type, actor,
                     vault_root=None, **payload):
        recorded.append({
            "workspace_id": workspace_id,
            "record_id": record_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        })

    monkeypatch.setattr(cr, "append_event", _fake_append)
    return recorded


# ---------------------------------------------------------------------------
# D1 — create-time budget is observable
# ---------------------------------------------------------------------------


def test_fresh_start_logs_mem_budget_and_lifecycle(monkeypatch, tmp_path):
    captured = []

    def _spy_log(event_type, *, container_id="", session_id="",
                 workspace_id="", data=None):
        captured.append({
            "event_type": event_type,
            "container_id": container_id,
            "data": dict(data or {}),
        })

    monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))
    monkeypatch.setattr(container_manager, "admit", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_load_capabilities", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "log_container_event", _spy_log)
    monkeypatch.setattr(
        container_manager, "record_creation", _spy_record_creation(_SpyRecord())
    )

    cm = _make_container_manager(_FakeClient(get_result=_FakeCtr()), tmp_path)
    monkeypatch.setattr(cm, "_run_container", lambda **kw: _FakeCtr())
    monkeypatch.setattr(cm, "_cache_container", lambda name, c: c)

    cm._fresh_start(
        image="img", name="c1", note=None, worker_name=None,
        lifecycle_class=LIFECYCLE_PERSISTENT,
        network_mode="none", workspace_mode="ro",
    )

    started = [e for e in captured if e["event_type"] == "started"]
    assert started, "no 'started' lifecycle event was emitted on create"
    data = started[-1]["data"]
    assert data.get("name") == "c1"
    assert data.get("mem_limit") == "1g", (
        f"create-time event must carry the DECLARED mem_limit; got data={data!r}"
    )
    assert data.get("oom_score_adj") == 1000, (
        f"create-time event must carry oom_score_adj; got data={data!r}"
    )
    assert data.get("lifecycle_class") == LIFECYCLE_PERSISTENT, (
        f"create-time event must carry the lifecycle_class; got data={data!r}"
    )


# ---------------------------------------------------------------------------
# D2 — exit-137 OOM event on the real exec path
# ---------------------------------------------------------------------------


def test_exec_exit_137_emits_distinct_oom_event(events):
    ctr = _ExecFakeContainer(
        labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_exec_attrs(),
        exec_result=(137, (b"", b"killed")),
    )
    cm = _make_exec_cm(_ExecFakeDockerClient([ctr]))

    result = cm.exec("c1", "kill -9 $$")

    assert result["exit_code"] == 137
    assert len(events) == 1, f"expected exactly one OOM event; got {events!r}"
    ev = events[0]
    assert ev["event_type"] == "container.command_oom_killed"
    assert ev["event_type"] != "drift.container_absent"
    assert ev["event_type"] != container_manager._EXEC_DRIFT_EVENT
    assert ev["record_id"] == "rec-1"
    assert ev["payload"]["exit_code"] == 137
    assert ev["payload"]["mem_limit"] == "512m"
    assert "lifecycle_class" in ev["payload"]


def test_exec_exit_137_carries_oom_killed_when_reported(events):
    ctr = _ExecFakeContainer(
        labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_exec_attrs(oom_killed=True),
        exec_result=(137, (b"", b"killed")),
    )
    cm = _make_exec_cm(_ExecFakeDockerClient([ctr]))

    cm.exec("c1", "kill -9 $$")

    assert len(events) == 1
    assert events[0]["payload"].get("oom_killed") is True


def test_exec_zero_exit_emits_no_oom_event(events):
    ctr = _ExecFakeContainer(
        labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_exec_attrs(),
        exec_result=(0, (b"ok", b"")),
    )
    cm = _make_exec_cm(_ExecFakeDockerClient([ctr]))

    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 0
    assert events == []


def test_exec_exit_137_without_record_emits_no_event(events):
    ctr = _ExecFakeContainer(
        labels={},  # no record label -> nothing to attach the event to
        attrs=_exec_attrs(),
        exec_result=(137, (b"", b"killed")),
    )
    cm = _make_exec_cm(_ExecFakeDockerClient([ctr]))

    result = cm.exec("c1", "kill -9 $$")

    assert result["exit_code"] == 137
    assert events == []
