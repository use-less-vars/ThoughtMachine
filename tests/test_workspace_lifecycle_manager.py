"""Tests for infra/workspace_lifecycle_manager.py (Phase 1).

Covers the WorkerSupervisor state machine, process_query correlation-id
handling, ExecutionTracker termination paths, the feature-flag gate, the
resource-container guard on request_container, and (when importable) the
integration wiring in tools/workspace/worker.py.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import subprocess
import threading
import time

import pytest

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, List, Optional
from unittest import mock

from tools.base import ToolBase

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
    # killpg(SIGTERM) fails -> kill(SIGTERM) fallback; the escalation path
    # then probes liveness with killpg(pid, 0) and, seeing the group is
    # already dead (ProcessLookupError), skips the SIGKILL.
    assert calls["killpg"] == [(4242, signal.SIGTERM), (4242, 0)]
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
        self.session_id = None
        self._session_permissions = {}
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


# ---------------------------------------------------------------------------
# 11. WLM Phase 4: session/permissions plumbing, soft-timeout warning,
#     query outcome log, ExecutionTracker details, worker integration.
# ---------------------------------------------------------------------------


def _docker_available():
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def test_supervisor_ctor_accepts_session_and_permissions_provider():
    sup = make_supervisor(session_id="sess-1", permissions_provider=lambda: {"x": 1})
    assert sup.session_id == "sess-1"
    assert sup.permissions_provider() == {"x": 1}


def test_soft_timeout_warning_emitted_once():
    sup = make_supervisor()
    calls = []
    release = threading.Event()
    sup.soft_timeout_warning_callback = lambda qid: calls.append(qid)

    def slow_handler(query, query_id):
        release.wait(5.0)
        sup._publish_reply(query_id, {"ok": query})

    sup.query_handler = slow_handler
    with pytest.raises(TimeoutError):
        sup.process_query("x", timeout=0.6)
    assert sup.soft_timeout_warning_emitted is True
    assert len(calls) == 1
    assert calls[0].startswith("q_")
    assert sup.status_report()["state"] == WorkerState.TIMED_OUT
    release.set()


def test_drain_logs_wlm_drain_prefix(caplog, monkeypatch):
    monkeypatch.setattr("infra.workspace_lifecycle_manager._agent_log", None)
    caplog.set_level("INFO")
    sup = make_supervisor()
    sup._output_queue.put(("q_old", "stale"))
    sup.transition_busy()
    assert "[WLM-DRAIN]" in caplog.text
    assert "q_old" in caplog.text


def test_query_log_marks_abandoned_on_timeout():
    sup = make_supervisor()
    with pytest.raises(TimeoutError):
        sup.process_query("x", timeout=0.2)
    ab = sup.abandoned_query_ids()
    assert len(ab) == 1 and ab[0].startswith("q_")
    assert sup.completed_query_ids() == []


def test_query_log_ttl_bounded_to_2():
    sup = make_supervisor()
    qids = []

    def handler(query, query_id):
        qids.append(query_id)
        sup._publish_reply(query_id, {"ok": query})

    sup.query_handler = handler
    for i in range(3):
        sup.process_query(f"q{i}", timeout=2.0)
    assert sup.completed_query_ids() == list(reversed(qids[-2:]))
    assert sup.abandoned_query_ids() == []


def test_execution_tracker_register_and_terminate_all_clears():
    tracker = ExecutionTracker()
    eid = tracker.register(
        "w1", query_id="q1", tool_call_id="tc1", container_id="c1",
        exec_id="x1", pid=None, tool_name="bash",
    )
    assert eid == "w1:tc1"
    assert tracker.active_count() == 1
    d = tracker._executions[eid]
    assert d["query_id"] == "q1"
    assert d["tool_call_id"] == "tc1"
    assert d["container_id"] == "c1"
    assert d["exec_id"] == "x1"
    assert d["type"] == "subprocess"
    assert d["tool_name"] == "bash"
    assert d["pid"] is None
    eid2 = tracker.register("w1", query_id="q1")
    assert eid2.startswith("w1:q1:") and eid2 != eid
    assert tracker.active_count() == 2
    tracker.terminate_all("w1", None, None)
    assert tracker.active_count() == 0


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available")
def test_wlm_integration_real_docker(tmp_path, caplog, monkeypatch):
    """End-to-end: send_query delegates to the WLM supervisor; a worker
    subprocess tool that overruns the query budget is killed on timeout, the
    stale reply is drained, and the supervisor returns to IDLE."""
    class _FakeEventBus:
        def __init__(self, *a, **k):
            pass

        def publish(self, *a, **k):
            return None

    monkeypatch.setattr("tools.workspace.worker.EventBus", _FakeEventBus)
    monkeypatch.setattr("tools.workspace.worker.register_worker_event_bus", lambda *a, **k: None)
    monkeypatch.setattr("tools.workspace.worker.unregister_worker_event_bus", lambda *a, **k: None)
    monkeypatch.setattr("tools.workspace.worker.global_event_bus", None)
    monkeypatch.setattr("infra.workspace_lifecycle_manager._agent_log", None)
    caplog.set_level("INFO")

    from tools.workspace.worker import WorkerThread

    thread = WorkerThread(
        name="w-wlm-int",
        definition={"system_prompt": "sys"},
        agent_config={"model": "gpt-4o", "session_config": {"use_workspace_lifecycle_manager": True}},
        workspace_dir=Path(tmp_path) / "ws",
        session_id="s1",
        timeout_seconds=60,
    )
    thread._agent = SimpleNamespace(state=SimpleNamespace(
        current_turn=0, turn_state=None, last_turn_warning_state=None,
        restrictions_active=False, restrictions_pending=False,
        restriction_reason=None, time_start=0.0, last_time_warning_state=None,
        time_state=SimpleNamespace(value="LOW"),
    ))
    release = threading.Event()
    proc_holder = {}

    def fake_run_tool_loop(self, query):
        if query == "run":
            proc = subprocess.Popen(["sleep", "60"])
            proc_holder["proc"] = proc
            wlm = getattr(self, "_wlm", None)
            if wlm is not None:
                wlm.execution_tracker.register(
                    worker_id=self.worker_name,
                    query_id=wlm.current_query_id,
                    tool_call_id="tc-sleep",
                    tool_name="bash",
                    pid=proc.pid,
                )
            release.wait(30.0)
        self._last_elapsed_val = 1.0
        self._final_token_usage = self.get_current_context_tokens()
        return "done"

    with mock.patch.object(thread, "_run_tool_loop", new=fake_run_tool_loop.__get__(thread, WorkerThread)):
        worker_t = threading.Thread(target=thread.run, daemon=True)
        worker_t.start()
        try:
            with pytest.raises(TimeoutError):
                thread.send_query("run", timeout=2.0)
        finally:
            release.set()
            thread._stop_event.set()
            thread._input_queue.put(None)
            worker_t.join(timeout=5.0)

    report = thread._wlm.status_report()
    assert report["state"] == WorkerState.TIMED_OUT
    assert report["timeout_triggered"] is True

    proc = proc_holder["proc"]
    proc.wait(timeout=5.0)
    assert proc.poll() is not None
    assert thread._wlm.execution_tracker.active_count() == 0

    deadline = time.monotonic() + 5.0
    while not thread._output_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert thread._output_queue.empty()

    # A fresh query drains the stale reply left over from the timed-out one.
    thread._wlm._output_queue.put(("q_stale", "stale"))
    thread._wlm.query_handler = lambda q, qid: thread._wlm._publish_reply(qid, {"ok": q})
    reply = thread._wlm.process_query("final", timeout=2.0)
    assert reply == {"ok": "final"}
    assert thread._wlm.status_report()["state"] == WorkerState.IDLE
    assert "[WLM-DRAIN]" in caplog.text
    assert "q_stale" in caplog.text

    data = json.loads((thread._worker_dir / "context.json").read_text(encoding="utf-8"))
    conv = data["conversation"]
    assert sum(1 for m in conv if m.get("role") == "system") == 1
    assert len(conv) <= 3


# ---------------------------------------------------------------------------
# 12. Production plumbing: SessionConfig -> AgentConfig -> ToolExecutor ->
#     Worker tool _build_agent_config -> WorkerThread._wlm_flag_enabled
# ---------------------------------------------------------------------------


class ProbeTool(ToolBase):
    """Minimal ToolBase capturing the injected agent_config dict."""

    required_categories: ClassVar[List[str]] = []
    captured: ClassVar[List[Optional[dict]]] = []

    workspace_path: Optional[str] = None
    token_limit: Optional[int] = None
    session_permissions: Optional[dict] = None
    effective_permissions: Optional[dict] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_config: Optional[dict] = None

    @classmethod
    def get_required_categories(cls, params=None):
        return cls.required_categories

    def execute(self) -> str:
        ProbeTool.captured.append(self.agent_config)
        return "ok"


def _make_probe_executor(config):
    from agent.core.tool_executor import ToolExecutor

    return ToolExecutor(
        tool_classes=[ProbeTool],
        config=config,
        state=SimpleNamespace(
            is_tool_allowed=lambda name: True,
            get_allowed_tools=lambda: ["ProbeTool"],
        ),
    )


def _run_probe(executor) -> dict:
    ProbeTool.captured.clear()
    result = executor._execute_single_tool(
        ProbeTool, {}, "ProbeTool", 1,
        lambda: False, lambda: None, lambda: 0,
    )
    assert result["result"] == "ok"
    assert ProbeTool.captured, "probe tool never executed"
    return ProbeTool.captured[-1]


def _build_via_worker_tool(injected: dict) -> dict:
    from tools.workspace.worker import Worker

    worker_tool = SimpleNamespace(
        agent_config=injected,
        workspace_path=None,
        _resolve_registry_workspace=lambda: None,
    )
    worker_tool._build_agent_config = Worker._build_agent_config.__get__(worker_tool)
    return worker_tool._build_agent_config()


def test_plumbing_wlm_flag_on_end_to_end(tmp_path):
    """SessionConfig(flag=True) reaches WorkerThread._wlm_flag_enabled()."""
    from agent.config.session_config import SessionConfig
    from agent.config.models import AgentConfig
    from tools.workspace.worker import WorkerThread

    session = SessionConfig(mode="custom", use_workspace_lifecycle_manager=True)
    acfg = session.to_agent_config(workspace_path=str(tmp_path))
    assert isinstance(acfg, AgentConfig)
    assert acfg.use_workspace_lifecycle_manager is True

    injected = _run_probe(_make_probe_executor(acfg))
    assert injected["use_workspace_lifecycle_manager"] is True

    built = _build_via_worker_tool(injected)
    assert built.get("use_workspace_lifecycle_manager") is True

    thread = WorkerThread(
        name="w-plumb-on",
        definition={},
        agent_config=built,
        workspace_dir=Path(tmp_path) / "ws",
        session_id="s1",
        timeout_seconds=60,
    )
    assert thread._wlm_flag_enabled() is True


def test_plumbing_wlm_flag_off_end_to_end(tmp_path):
    """Default-off session yields False all the way down the chain."""
    from agent.config.session_config import SessionConfig
    from tools.workspace.worker import WorkerThread

    session = SessionConfig(mode="custom")
    acfg = session.to_agent_config(workspace_path=str(tmp_path))
    assert acfg.use_workspace_lifecycle_manager is False

    injected = _run_probe(_make_probe_executor(acfg))
    assert injected["use_workspace_lifecycle_manager"] is False

    built = _build_via_worker_tool(injected)
    assert built.get("use_workspace_lifecycle_manager") is False

    thread = WorkerThread(
        name="w-plumb-off",
        definition={},
        agent_config=built,
        workspace_dir=Path(tmp_path) / "ws2",
        session_id="s1",
        timeout_seconds=60,
    )
    assert thread._wlm_flag_enabled() is False


def test_plumbing_container_registry_flag_forwarding(tmp_path):
    """use_container_registry forwards through the same chain (default False)."""
    from agent.config.session_config import SessionConfig
    from agent.config.models import AgentConfig

    session = SessionConfig(mode="custom", use_container_registry=True)
    acfg = session.to_agent_config(workspace_path=str(tmp_path))
    assert isinstance(acfg, AgentConfig)
    assert acfg.use_container_registry is True

    injected = _run_probe(_make_probe_executor(acfg))
    assert injected["use_container_registry"] is True

    built = _build_via_worker_tool(injected)
    assert built.get("use_container_registry") is True

    off = SessionConfig(mode="custom").to_agent_config()
    assert off.use_container_registry is False
    off_injected = _run_probe(_make_probe_executor(off))
    assert off_injected["use_container_registry"] is False
    off_built = _build_via_worker_tool(off_injected)
    assert off_built.get("use_container_registry") is False

