"""Tests for interrupting a container/code-execution WAIT (Stop button).

``ContainerManager.exec`` used to wait on its daemon exec thread with a SINGLE
``thread.join(timeout)``, so a Stop pressed mid-command could not be honoured
until the timeout expired.  It now waits in 0.5 s slices and polls the current
turn's cancellation token between them.

What these tests pin:
  * an interrupted wait returns an ``interrupted`` payload quickly instead of
    blocking for the whole timeout;
  * it does NOT kill or drop the container (interrupting the WAIT is not the
    same as killing the process, and the in-container process was never
    launched as a killable handle);
  * the timeout path is unchanged (still kills + drops + raises TimeoutError);
  * the normal path is unchanged;
  * the Stop button (``WebAgentBridge._interrupt_agent_turn``) sets the main
    agent's published turn token, and is a no-op when no turn is running.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from tools import cancellation
from web_ui.backend.bridge import WebAgentBridge


# ---------------------------------------------------------------------------
# Fakes
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
        self.killed = False
        self.exec_calls = []

    def reload(self):
        return None

    def kill(self):
        self.killed = True
        self.release.set()

    def remove(self, force=False):
        return None

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


def _make_manager(container, tmp_path):
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


@pytest.fixture(autouse=True)
def _clean_token():
    # ``unbind()`` drops the turn token AND the pause reference; the explicit
    # pause clear is stated anyway so a leak can never survive a refactor.
    cancellation.unbind()
    cancellation.bind_pause_event(None)
    yield
    cancellation.bind_pause_event(None)
    cancellation.unbind()


# ---------------------------------------------------------------------------
# ContainerManager.exec — chunked, interruptible wait
# ---------------------------------------------------------------------------


def test_interrupted_wait_returns_promptly_without_killing(tmp_path):
    ctr = _BlockingContainer(block_seconds=30.0)
    cm = _make_manager(ctr, tmp_path)
    token = threading.Event()
    cancellation.bind_turn_cancel(token)

    threading.Timer(0.2, lambda: (setattr(token, "reason", "stopped_by_operator"),
                                  token.set())).start()
    started = time.monotonic()
    result = cm.exec("c1", command="sleep 30", timeout=25)
    elapsed = time.monotonic() - started
    ctr.release.set()

    assert result["status"] == "interrupted"
    assert result["interrupted"] is True
    assert result["reason"] == "stopped_by_operator"
    assert result["exec"] == "container_exec"
    assert result["container_id"] == "c1"
    assert result["command"] == "sleep 30"
    assert result["exit_code"] is None
    # Honest note: we stopped waiting, the process was not killed.
    assert "NOT killed" in result["note"]
    # Quick: nowhere near the 25 s timeout, and no kill / drop happened.
    assert elapsed < 5.0
    assert ctr.killed is False
    assert cm.dropped == []


def test_paused_wait_returns_promptly(tmp_path):
    """A worker PAUSE must abandon the wait, with its own reason string.

    Pause is not a cancel: the turn is meant to CONCLUDE (reading mode).  But a
    tool blocked here would sit out the whole timeout first, so the conclusion
    could never start.  Same payload shape as the cancel path, different
    ``reason`` so callers and tests can tell the two apart.
    """
    ctr = _BlockingContainer(block_seconds=30.0)
    cm = _make_manager(ctr, tmp_path)
    pause_event = threading.Event()
    cancellation.bind_pause_event(pause_event)

    threading.Timer(0.2, pause_event.set).start()
    started = time.monotonic()
    result = cm.exec("c1", command="sleep 30", timeout=25)
    elapsed = time.monotonic() - started
    ctr.release.set()

    assert result["status"] == "interrupted"
    assert result["interrupted"] is True
    assert result["reason"] == "paused"
    assert result["exec"] == "container_exec"
    assert result["container_id"] == "c1"
    assert result["command"] == "sleep 30"
    assert result["exit_code"] is None
    # Honest note: we stopped waiting, the process was not killed.
    assert "NOT killed" in result["note"]
    # Quick: nowhere near the 25 s timeout, and no kill / drop happened.
    assert elapsed < 5.0
    assert ctr.killed is False
    assert cm.dropped == []


def test_uninterrupted_wait_returns_normal_result(tmp_path):
    ctr = _BlockingContainer(block_seconds=0.01)
    cm = _make_manager(ctr, tmp_path)

    result = cm.exec("c1", command="echo hi", timeout=5)

    assert result == {"stdout": "out", "stderr": "err", "exit_code": 0}


def test_timeout_path_still_kills_and_drops(tmp_path):
    """The timeout contract is untouched by the chunked wait."""
    ctr = _BlockingContainer(block_seconds=30.0)
    cm = _make_manager(ctr, tmp_path)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        cm.exec("c1", command="sleep 30", timeout=0.6)
    ctr.release.set()

    assert time.monotonic() - started < 5.0
    assert ctr.killed is True
    assert cm.dropped == ["c1"]


# ---------------------------------------------------------------------------
# Stop button — WebAgentBridge._interrupt_agent_turn
# ---------------------------------------------------------------------------


def test_bridge_sets_the_main_agent_turn_token():
    event = threading.Event()
    bridge = WebAgentBridge.__new__(WebAgentBridge)
    bridge._agent = SimpleNamespace(_turn_cancel_event=event)

    assert bridge._interrupt_agent_turn() is True
    assert event.is_set() is True
    assert event.reason == "stopped_by_operator"


def test_bridge_is_noop_when_no_turn_is_running():
    bridge = WebAgentBridge.__new__(WebAgentBridge)
    bridge._agent = SimpleNamespace(_turn_cancel_event=None)
    assert bridge._interrupt_agent_turn() is False

    bridge._agent = None
    assert bridge._interrupt_agent_turn() is False

    bridge_no_agent = WebAgentBridge.__new__(WebAgentBridge)
    assert bridge_no_agent._interrupt_agent_turn() is False


# ---------------------------------------------------------------------------
# DockerCodeRunner surfaces the interrupted payload
# ---------------------------------------------------------------------------


def test_docker_code_runner_surfaces_interrupted_payload(monkeypatch, tmp_path):
    import json

    import tools.docker_code_runner as dcr

    class _Manager:
        def __init__(self, **kwargs):
            pass

        def start(self, **kwargs):
            return {"id": "c9", "status": "created"}

        def exec(self, container_id, **kwargs):
            return {
                "status": "interrupted",
                "interrupted": True,
                "reason": "stopped_by_operator",
                "exec": "container_exec",
                "container_id": container_id,
                "command": kwargs.get("command"),
                "elapsed": 0.5,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "note": "Stopped WAITING ... NOT killed",
            }

        def stop(self, container_id):
            return None

        def remove(self, container_id):
            return None

    monkeypatch.setattr(dcr, "DOCKER_AVAILABLE", True)
    monkeypatch.setattr(dcr, "ContainerManager", lambda **kwargs: _Manager())
    monkeypatch.setattr(dcr, "audit_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(dcr.DockerCodeRunner, "_resolve_registry_workspace",
                        lambda self: str(tmp_path))

    runner = dcr.DockerCodeRunner(command="echo hi")
    payload = runner.execute()

    assert '"status": "interrupted"' in payload
    assert '"interrupted": true' in payload
    assert '"exit_code": null' in payload
    assert "stopped_by_operator" in payload


# ---------------------------------------------------------------------------
# Worker direction -- the tool loop BINDS the worker's own park event
# ---------------------------------------------------------------------------


class _NullStateBridge:
    """Keep the loop's presenter lazy-init out of the test (same stand-in as
    ``tests/test_worker_pause_midturn_cooperative.py``)."""

    context_length = 0


def _unstarted_worker(tmp_path):
    """A REAL ``WorkerThread`` that is never ``start()``ed (house pattern)."""
    from tools.workspace.worker_thread import WorkerThread

    return WorkerThread(
        name="test-worker-pause-binding",
        definition={"name": "test-worker-pause-binding",
                    "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=tmp_path,
        session_id="sess-pause-binding",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def test_worker_tool_loop_binds_the_pause_event(tmp_path):
    """THE PIN: the tool thread must SEE the worker's park event.

    Without this binding ``pause_requested()`` is False inside every tool on the
    worker's thread, so an in-flight container exec can never notice a pause --
    it waits out its full timeout instead.
    """
    thread = _unstarted_worker(tmp_path)
    thread._state_bridge = _NullStateBridge()
    thread._publish_event = lambda etype, data: None
    seen = {}

    class _Agent:
        def process_query(self, query):
            seen["start"] = cancellation.pause_requested()
            thread.pause()
            seen["paused"] = cancellation.pause_requested()
            thread.resume()
            seen["resumed"] = cancellation.pause_requested()
            thread.pause()
            seen["paused_again"] = cancellation.pause_requested()
            yield {"type": "assistant_message", "content": "working on it..."}
            yield {"type": "agent_responded", "content": "done",
                   "status": "success", "confidence": "high"}

        def enter_reading_mode(self, reason="pause"):
            return None

        def leave_reading_mode(self, reason="pause"):
            return None

        def request_pause(self):
            return None

    thread._agent = _Agent()

    thread._run_tool_loop("do the thing")

    assert seen["start"] is False, seen
    assert seen["paused"] is True, seen
    assert seen["resumed"] is False, seen
    assert seen["paused_again"] is True, seen

    # Turn end DROPS the reference -- even though the event itself is still set
    # (it belongs to the worker, not to the turn).
    assert thread._park_event.is_set() is True
    assert cancellation.pause_requested() is False
