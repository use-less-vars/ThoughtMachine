"""An OPERATOR pause must survive a later query.

``WorkerThread.pause(manual_only=True)`` marks the pause operator-owned: the
worker stays parked -- and stays in reading mode -- until an explicit
``resume()``.  The historical rule "a new query IS the resume" silently
defeated the operator's pause, so the very next query (or the agent's own
``Worker(action='query')``) unparked a worker the operator had just paused.

Two seams are covered here:

* :meth:`WorkerThread.send_query` -- the implicit-unpause site itself.  It must
  HOLD (log ``[CONTROL_PLANE_PAUSE_HOLD]``) and leave both the park flag and the
  agent's reading-mode restriction in place.
* :meth:`resume` -- releases the latch, so the NEXT query is not held: the
  latch is a pause that survives queries, not a one-way door.
"""

import json
import logging

import pytest

import tools.workspace.worker_thread as worker_module
from tools.workspace.worker_thread import WorkerThread
from tests.test_worker_sync_query_timeout_containment import _spawn_worker, _wait_until

from agent.config.models import AgentConfig
from agent.core.agent import Agent

SESSION_ID = "sess-manual-pause"


@pytest.fixture(autouse=True)
def _own_registry_teardown():
    """Unregister the worker this module registers (run() never does).

    ``_spawn_worker`` starts a real supervisor thread and registers it under
    ``(session_id, name, 1)``; nothing pops that entry, so this module owns the
    teardown rather than relying on another test module's fixture.
    """
    with worker_module._registry_lock:
        preexisting = set(worker_module._worker_registry.keys())
    yield
    with worker_module._registry_lock:
        added = [
            key
            for key in worker_module._worker_registry.keys()
            if key not in preexisting
        ]
        threads = [worker_module._worker_registry.pop(key, None) for key in added]
    for thread in threads:
        if thread is None:
            continue
        try:
            thread.stop()
        except Exception:
            pass
        thread.join(timeout=5)


def _make_agent():
    """A real Agent: the pause latch is read off its own reading-mode state."""
    return Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session_id=SESSION_ID,
    )


def test_operator_pause_holds_against_a_new_query(tmp_path, monkeypatch):
    """send_query must HOLD -- not silently unpause -- an operator pause."""
    name = "w-manual-hold"
    thread = _spawn_worker(tmp_path, monkeypatch, name, SESSION_ID)
    thread._agent = _make_agent()
    agent = thread._agent

    logged = []
    monkeypatch.setattr(
        worker_module, "log", lambda level, logger_name, msg: logged.append(msg)
    )

    thread.pause(manual_only=True)
    assert thread._manual_only_pause is True
    assert agent.state.restriction_reason == "pause"
    assert _wait_until(lambda: thread.status == "paused"), (
        f"worker never parked (status={thread.status!r})"
    )

    # A query arrives while the operator-paused worker sits in its park loop.
    # It is not answered (a parked worker dequeues nothing), and that is the
    # point: the query must NOT release the pause.
    with pytest.raises(TimeoutError):
        thread.send_query("anyone there?", timeout=1.0)

    hold = [m for m in logged if "[CONTROL_PLANE_PAUSE_HOLD]" in m]
    assert len(hold) == 1, logged
    assert "reason=manual_only" in hold[0]
    assert not [m for m in logged if "[CONTROL_PLANE_PAUSE_RELEASED]" in m], logged

    # The latch and its side effects are all still in place.
    assert thread._manual_only_pause is True
    assert thread._park_event.is_set() is True
    assert agent.state.restriction_reason == "pause"
    assert thread.status == "paused"


def test_resume_releases_the_latch_so_the_next_query_runs(tmp_path, monkeypatch):
    """After an explicit resume the next query runs: the latch is not a door."""
    name = "w-manual-resume"
    thread = _spawn_worker(tmp_path, monkeypatch, name, SESSION_ID)
    thread._agent = _make_agent()
    agent = thread._agent

    def _fake_tool_loop(self, query):
        self._last_elapsed_val = 0.0
        return json.dumps({"response": f"echo: {query}", "query": query})

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)

    logged = []
    monkeypatch.setattr(
        worker_module, "log", lambda level, logger_name, msg: logged.append(msg)
    )

    thread.pause(manual_only=True)
    assert thread._manual_only_pause is True
    assert _wait_until(lambda: thread.status == "paused"), (
        f"worker never parked (status={thread.status!r})"
    )

    assert thread.resume() is True
    assert thread._manual_only_pause is False
    assert agent.state.restriction_reason is None
    assert _wait_until(lambda: thread.status == "ready"), (
        f"worker never resumed (status={thread.status!r})"
    )

    reply = thread.send_query("are we running again?", timeout=15.0)

    assert "echo:" in reply and "are we running again?" in reply
    assert thread._park_event.is_set() is False
    assert not [m for m in logged if "[CONTROL_PLANE_PAUSE_HOLD]" in m], logged
    assert logging.getLogger("tools.worker") is not None
