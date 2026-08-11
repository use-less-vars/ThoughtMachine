"""Tests for infra/workspace_lifecycle_manager.py (Phase 1).

Covers the WorkerSupervisor state machine, process_query correlation-id
handling, ExecutionTracker termination paths, the feature-flag gate, the
resource-container guard on request_container, and (when importable) the
integration wiring in tools/workspace/worker.py.
"""

from __future__ import annotations

import queue
import signal

import pytest

from infra.workspace_lifecycle_manager import (
    EXEC_KILL_GRACE,
    RESOURCE_IMAGE_TAG,
    ExecutionTracker,
    StateMachineError,
    WorkerState,
    WorkerSupervisor,
    is_wlm_enabled,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeContainerManager:
    """Duck-typed ContainerManager exposing session_config + start/stop/remove
    and (unlike the real manager) exec_stop — used to verify the
    exec_stop-preferred path of ExecutionTracker."""

    def __init__(self, session_config=None, start_result=None):
        self.session_config = session_config or {}
        self.start_result = start_result
        self.exec_stopped = []
        self.stopped = []
        self.removed = []
        self.started = []

    def exec_stop(self, container_id, exec_id, timeout=None):
        self.exec_stopped.append((container_id, exec_id, timeout))

    def stop(self, container_id):
        self.stopped.append(container_id)
        return {"status": "stopped", "container_id": container_id}

    def remove(self, container_id):
        self.removed.append(container_id)
        return {"status": "removed", "container_id": container_id}

    def start(self, image=None, name=None, note=None):
        self.started.append({"image": image, "name": name, "note": note})
        if self.start_result is not None:
            return self.start_result
        return {"id": "c1", "name": "agent-exec-x", "status": "created", "note": ""}


class FakeResourceContainerManager:
    container_name = "tm-res-0123456789ab-git"


class FakeCMNoExecStop:
    """Real-API-like manager: stop/remove only, no exec_stop."""

    def __init__(self):
        self.stopped = []
        self.removed = []

    def stop(self, container_id):
        self.stopped.append(container_id)
        return {"status": "stopped", "container_id": container_id}

    def remove(self, container_id):
        self.removed.append(container_id)
        return {"status": "removed", "container_id": container_id}


class FakeCMStopRaises:
    """stop() fails so ExecutionTracker must fall back to remove()."""

    def __init__(self):
        self.removed = []

    def stop(self, container_id):
        raise RuntimeError("stop failed")

    def remove(self, container_id):
        self.removed.append(container_id)
        return {"status": "removed", "container_id": container_id}


def make_supervisor(worker_id="w1", cm=None, rcm=None, **kwargs):
    return WorkerSupervisor(
        worker_id,
        cm,
        rcm,
        feature_flag_check=kwargs.pop("feature_flag_check", lambda: True),
        **kwargs,
    )


def set_handler(sup, reply_factory):
    """Install a query_handler that publishes a reply derived from the query."""

    def handler(query, query_id):
        sup._publish_reply(query_id, reply_factory(query))

    sup.query_handler = handler
    return handler


# ---------------------------------------------------------------------------
# 1. Valid transitions
# ---------------------------------------------------------------------------

def test_valid_transitions_and_state_flow():
    sup = make_supervisor()
    assert sup.status_report()["state"] == WorkerState.IDLE

    qid1 = sup.transition_busy()
    assert qid1.startswith("q_")
    assert sup.status_report()["state"] == WorkerState.BUSY
    assert sup.status_report()["query_id"] == qid1

    # BUSY -> IDLE clears tracker
    sup.execution_tracker.add("e1", {"type": "subprocess", "pid": 1})
    sup.transition_idle()
    assert sup.status_report()["state"] == WorkerState.IDLE
    assert sup.execution_tracker.active_count() == 0

    # BUSY -> TIMED_OUT sets timeout_triggered
    sup.transition_busy()
    sup.transition_timeout()
    report = sup.status_report()
    assert report["state"] == WorkerState.TIMED_OUT
    assert report["timeout_triggered"] is True

    # TIMED_OUT -> BUSY resets timeout_triggered
    qid2 = sup.transition_busy()
    report = sup.status_report()
    assert report["state"] == WorkerState.BUSY
    assert report["timeout_triggered"] is False
    assert qid2 != qid1

    # stop() from BUSY -> STOPPED
    sup.stop()
    report = sup.status_report()
    assert report["state"] == WorkerState.STOPPED
    assert report["is_alive"] is False


def test_pause_resume_restores_query_id():
    sup = make_supervisor()
    qid = sup.transition_busy()
    sup.transition_pause(intentional=True)
    report = sup.status_report()
    assert report["state"] == WorkerState.PAUSED
    assert report["paused_intentional"] is True

    sup.transition_resume()
    report = sup.status_report()
    assert report["state"] == WorkerState.BUSY
    assert report["query_id"] == qid

    # pause from IDLE (no pending) resumes to IDLE
    sup.transition_idle()
    sup.transition_pause(intentional=False)
    assert sup.status_report()["state"] == WorkerState.PAUSED
    sup.transition_resume()
    assert sup.status_report()["state"] == WorkerState.IDLE


# ---------------------------------------------------------------------------
# 2. Invalid transitions raise StateMachineError
# ---------------------------------------------------------------------------

def test_invalid_transitions_raise():
    cases = [
        ("IDLE->STOPPING", lambda s: s.transition_stopping()),
        ("IDLE->STOPPED", lambda s: s.transition_stopped()),
        ("IDLE->TIMEOUT", lambda s: s.transition_timeout()),
        ("IDLE->RESUME", lambda s: s.transition_resume()),
    ]
    for name, fn in cases:
        sup = make_supervisor()
        with pytest.raises(StateMachineError):
            fn(sup)
        assert sup.status_report()["state"] == WorkerState.IDLE, name

    sup = make_supervisor()
    sup.transition_busy()
    with pytest.raises(StateMachineError):
        sup.transition_busy()  # BUSY->BUSY
    # BUSY->PAUSED->TIMEOUT is invalid (timeout only from BUSY)
    sup.transition_pause()
    with pytest.raises(StateMachineError):
        sup.transition_timeout()

    sup = make_supervisor()
    sup.transition_busy()
    sup.transition_timeout()
    with pytest.raises(StateMachineError):
        sup.transition_idle()  # TIMED_OUT->IDLE invalid

    sup = make_supervisor()
    sup.transition_busy()
    sup.transition_stopping()
    with pytest.raises(StateMachineError):
        sup.transition_busy()  # STOPPING->BUSY

    sup = make_supervisor()
    sup.transition_busy()
    sup.stop()
    assert sup.status_report()["state"] == WorkerState.STOPPED
    for name, fn in [
        ("STOPPED->BUSY", lambda s: s.transition_busy()),
        ("STOPPED->IDLE", lambda s: s.transition_idle()),
        ("STOPPED->PAUSE", lambda s: s.transition_pause()),
        ("STOPPED->TIMEOUT", lambda s: s.transition_timeout()),
        ("STOPPED->STOPPING", lambda s: s.transition_stopping()),
        ("STOPPED->STOPPED", lambda s: s.transition_stopped()),
        ("STOPPED->RESUME", lambda s: s.transition_resume()),
        ("STOPPED->process_query", lambda s: s.process_query("x", timeout=0.1)),
    ]:
        with pytest.raises(StateMachineError):
            fn(sup)
        assert sup.status_report()["state"] == WorkerState.STOPPED, name


# ---------------------------------------------------------------------------
# 3. process_query happy path
# ---------------------------------------------------------------------------

def test_process_query_happy_path():
    sup = make_supervisor()
    set_handler(sup, lambda query: {"ok": query})
    reply = sup.process_query("hello", timeout=2.0)
    assert reply == {"ok": "hello"}
    report = sup.status_report()
    assert report["state"] == WorkerState.IDLE
    assert report["query_id"] is None


def test_process_query_discards_stale_reply():
    sup = make_supervisor()
    # Pre-put a stale tuple; transition_busy drains it. A wrong-qid reply
    # published by the handler is discarded by the correlation check.
    sup._output_queue.put(("q_stale", "stale"))
    set_handler(sup, lambda query: {"echo": query})
    reply = sup.process_query("hi", timeout=2.0)
    assert reply == {"echo": "hi"}
    assert sup.status_report()["state"] == WorkerState.IDLE


def test_process_query_wrong_query_id_times_out():
    sup = make_supervisor()

    def wrong_handler(query, query_id):
        sup._output_queue.put(("q_wrong", "nope"))

    sup.query_handler = wrong_handler
    with pytest.raises(TimeoutError):
        sup.process_query("x", timeout=0.3)
    report = sup.status_report()
    assert report["state"] == WorkerState.TIMED_OUT
    assert report["timeout_triggered"] is True


def test_process_query_while_busy_raises():
    sup = make_supervisor()
    sup.transition_busy()
    with pytest.raises(StateMachineError):
        sup.process_query("x", timeout=0.2)


def test_process_query_auto_resume_from_paused():
    sup = make_supervisor()
    sup.pause(intentional=True)  # IDLE -> PAUSED (intentional)
    set_handler(sup, lambda query: {"reply": query})
    reply = sup.process_query("auto", timeout=2.0)
    assert reply == {"reply": "auto"}
    report = sup.status_report()
    assert report["state"] == WorkerState.PAUSED
    assert report["paused_intentional"] is True


def test_process_query_no_handler_times_out():
    sup = make_supervisor()
    with pytest.raises(TimeoutError):
        sup.process_query("x", timeout=0.2)
    assert sup.status_report()["state"] == WorkerState.TIMED_OUT


# ---------------------------------------------------------------------------
# 8. ExecutionTracker.terminate_all
# ---------------------------------------------------------------------------

def test_terminate_all_docker_exec_with_exec_stop():
    cm = FakeContainerManager()
    tracker = ExecutionTracker()
    tracker.add("e1", {"type": "docker_exec", "container_id": "c1", "exec_id": "x1"})
    tracker.terminate_all("w1", cm, None)
    assert cm.exec_stopped == [("c1", "x1", EXEC_KILL_GRACE)]
    assert tracker.active_count() == 0
    # Idempotent: empty tracker is a safe no-op
    tracker.terminate_all("w1", cm, None)
    assert cm.exec_stopped == [("c1", "x1", EXEC_KILL_GRACE)]


def test_terminate_all_docker_exec_falls_back_to_stop():
    cm = FakeCMNoExecStop()
    tracker = ExecutionTracker()
    tracker.add("e1", {"type": "docker_exec", "container_id": "c1", "exec_id": "x1"})
    tracker.terminate_all("w1", cm, None)
    assert cm.stopped == ["c1"]
    assert cm.removed == []


def test_terminate_all_docker_exec_stop_failure_removes():
    cm = FakeCMStopRaises()
    tracker = ExecutionTracker()
    tracker.add("e1", {"type": "docker_exec", "container_id": "c1", "exec_id": "x1"})
    tracker.terminate_all("w1", cm, None)
    assert cm.removed == ["c1"]
    assert tracker.active_count() == 0


def test_terminate_all_scoped_container_stops():
    cm = FakeContainerManager()
    tracker = ExecutionTracker()
    tracker.add("e1", {"type": "scoped_container", "container_id": "c9"})
    tracker.terminate_all("w1", cm, None)
    assert cm.stopped == ["c9"]
    assert cm.exec_stopped == []


def test_terminate_all_subprocess_killpg_fallback_kill(monkeypatch):
    calls = {"killpg": [], "kill": []}

    def fake_killpg(pid, sig):
        calls["killpg"].append((pid, sig))
        raise ProcessLookupError("no such process group")

    def fake_kill(pid, sig):
        calls["kill"].append((pid, sig))

    monkeypatch.setattr("os.killpg", fake_killpg)
    monkeypatch.setattr("os.kill", fake_kill)
    tracker = ExecutionTracker()
    tracker.add("e1", {"type": "subprocess", "pid": 4242})
    tracker.terminate_all("w1", None, None)
    assert calls["killpg"] == [(4242, signal.SIGTERM)]
    assert calls["kill"] == [(4242, signal.SIGTERM)]


def test_terminate_all_unknown_type_skipped():
    cm = FakeContainerManager()
    tracker = ExecutionTracker()
    tracker.add("e1", {"type": "mystery"})
    tracker.terminate_all("w1", cm, None)
    assert cm.stopped == []
    assert tracker.active_count() == 0


# ---------------------------------------------------------------------------
# 9. Feature-flag gate
# ---------------------------------------------------------------------------

def test_is_wlm_enabled():
    assert is_wlm_enabled(None) is False
    assert is_wlm_enabled(FakeContainerManager()) is False
    assert is_wlm_enabled(FakeContainerManager(session_config={"other": 1})) is False
    assert is_wlm_enabled(
        FakeContainerManager(session_config={"use_workspace_lifecycle_manager": True})
    ) is True


# ---------------------------------------------------------------------------
# 11. Container requests (resource-container guard)
# ---------------------------------------------------------------------------

def test_request_container_rejects_resource_requests():
    sup = make_supervisor(cm=FakeContainerManager())
    with pytest.raises(PermissionError):
        sup.request_container({"image": RESOURCE_IMAGE_TAG})
    with pytest.raises(PermissionError):
        sup.request_container({"name": "tm-res-abc123def456-git"})
    with pytest.raises(PermissionError):
        sup.request_container({"image": "python:3.12", "name": "tm-res-abc-git"})
    with pytest.raises(PermissionError):
        sup.request_container(RESOURCE_IMAGE_TAG)


def test_request_container_non_resource_passthrough():
    cm = FakeContainerManager(start_result={"id": "c42"})
    sup = make_supervisor(cm=cm)
    reply = sup.request_container({"image": "python:3.12", "name": "my-box", "note": "n"})
    assert reply == {"id": "c42"}
    assert cm.started == [{"image": "python:3.12", "name": "my-box", "note": "n"}]

    reply = sup.request_container("python:3.12")
    assert reply == {"id": "c42"}
    assert cm.started[1] == {"image": "python:3.12", "name": None, "note": None}


def test_request_release_container_no_manager_raises():
    sup = make_supervisor()
    with pytest.raises(RuntimeError):
        sup.request_container({"image": "python:3.12"})
    with pytest.raises(RuntimeError):
        sup.release_container("c1")


def test_release_container_stops():
    cm = FakeContainerManager()
    sup = make_supervisor(cm=cm)
    sup.release_container("c7")
    assert cm.stopped == ["c7"]


# ---------------------------------------------------------------------------
# 10. Integration wiring into tools/workspace/worker.py
# ---------------------------------------------------------------------------

class FakeWorkerThread:
    """Duck-typed WorkerThread exposing only what _get_wlm touches."""

    def __init__(self, agent_config=None, definition=None):
        self.worker_name = "fake-worker"
        self._agent_config_dict = agent_config or {}
        self.definition = definition or {}
        self._input_queue = queue.Queue()
        self._output_queue = queue.Queue()


def test_worker_wlm_wiring_flag_on():
    tools_worker = pytest.importorskip("tools.workspace.worker")
    WorkerThread = tools_worker.WorkerThread
    # Bind the real worker methods onto the duck-typed fake so the actual
    # integration code (flag gate, lazy supervisor, handler closure) runs.
    FakeWorkerThread._get_wlm = WorkerThread._get_wlm
    FakeWorkerThread._wlm_flag_enabled = WorkerThread._wlm_flag_enabled
    FakeWorkerThread._process_query_via_wlm = WorkerThread._process_query_via_wlm

    fake = FakeWorkerThread(agent_config={"session_config": {"use_workspace_lifecycle_manager": True}})
    sup = fake._get_wlm()
    assert sup is not None
    assert sup.query_handler is not None
    assert sup.worker_id == "fake-worker"

    # Pre-answer the handler's _output_queue.get(); process_query bridges
    # through the supervisor correlation queue.
    fake._output_queue.put("hi from worker")
    used, reply = fake._process_query_via_wlm("q", timeout=5.0)
    assert used is True
    assert "hi from worker" in str(reply)
    # Second call reuses the cached supervisor
    assert fake._get_wlm() is sup


def test_worker_wlm_wiring_flag_off():
    tools_worker = pytest.importorskip("tools.workspace.worker")
    WorkerThread = tools_worker.WorkerThread
    FakeWorkerThread._get_wlm = WorkerThread._get_wlm
    FakeWorkerThread._wlm_flag_enabled = WorkerThread._wlm_flag_enabled
    FakeWorkerThread._process_query_via_wlm = WorkerThread._process_query_via_wlm

    fake = FakeWorkerThread()  # no flag anywhere
    assert fake._get_wlm() is None
    used, reply = fake._process_query_via_wlm("q", timeout=5.0)
    assert used is False
    assert reply is None

    # definition-key variant enables the flag too
    fake2 = FakeWorkerThread(definition={"use_workspace_lifecycle_manager": True})
    assert fake2._get_wlm() is not None
