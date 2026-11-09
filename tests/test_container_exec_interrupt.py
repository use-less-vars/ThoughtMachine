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
    cancellation.unbind()
    yield
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
