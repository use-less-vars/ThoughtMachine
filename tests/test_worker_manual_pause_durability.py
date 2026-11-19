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

Log-capture note: the ``log`` name ``worker_thread`` resolves is
``agent.logging.log``, which prints to stderr and forwards to the optional
AgentLogger; it never touches stdlib ``logging``, so ``caplog`` records none of
these lines (measured: 0 records).  These tests therefore capture the module's
own ``log`` seam and assert level + tag + text, which is stricter than a caplog
substring match.
"""

import json
import time

import pytest

import tools.workspace.worker_thread as worker_module
from tools.workspace.worker import Worker, WorkerThread
from tests.test_worker_sync_query_timeout_containment import _spawn_worker, _wait_until

from agent.config.models import AgentConfig
from agent.core.agent import Agent, _discard_manual_only_worker_pause

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
    # Module-level bookkeeping, not a registry key: the session-scoped record of
    # the operator's manual-only pauses must not leak into the next test (it is
    # keyed by a session id this module reuses).
    with worker_module._MANUAL_ONLY_PAUSED_LOCK:
        worker_module._MANUAL_ONLY_PAUSED.pop(SESSION_ID, None)


def _make_agent():
    """A real Agent: the pause latch is read off its own reading-mode state."""
    return Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session_id=SESSION_ID,
    )


def _capture_log(monkeypatch):
    """Record every ``log(level, tag, message)`` the worker module emits."""
    logged = []
    monkeypatch.setattr(
        worker_module,
        "log",
        lambda level, tag, msg: logged.append((level, tag, msg)),
    )
    return logged


def test_operator_pause_survives_a_query(tmp_path, monkeypatch):
    """Neither the Worker tool nor send_query may release an operator pause."""
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    thread._agent = _make_agent()
    agent = thread._agent
    logged = _capture_log(monkeypatch)

    thread.pause(manual_only=True)
    assert thread._manual_only_pause is True
    assert agent.state.restriction_reason == "pause"
    assert _wait_until(lambda: thread.status == "paused"), (
        f"worker never parked (status={thread.status!r})"
    )

    # Seam 1 -- the query surface the agent itself uses.  It must refuse
    # non-destructively and leave the pause (and its side effects) untouched.
    tool = Worker(
        action="query",
        worker_name="w1",
        worker_query="hello",
        session_id=SESSION_ID,
    )
    envelope = tool._action_query([])
    assert envelope["status"] == "paused", envelope
    assert "manually paused" in envelope["message"], envelope
    assert thread._manual_only_pause is True
    assert agent.state.restriction_reason == "pause"

    # Seam 2 -- send_query, where the implicit unpause lives.  A parked worker
    # dequeues nothing, so the query times out; that is the point: it must NOT
    # be answered by silently releasing the operator's pause.
    with pytest.raises(TimeoutError):
        thread.send_query("anyone there?", timeout=1.0)

    hold = [
        (level, tag, msg)
        for level, tag, msg in logged
        if "[CONTROL_PLANE_PAUSE_HOLD]" in msg
    ]
    assert len(hold) == 1, logged
    level, tag, msg = hold[0]
    assert level == "INFO" and tag == "tools.worker", (level, tag)
    assert "reason=manual_only" in msg
    assert not [m for _, _, m in logged if "[CONTROL_PLANE_PAUSE_RELEASED]" in m], logged

    # Every side effect of the pause is still in place.
    assert thread._manual_only_pause is True
    assert thread._park_event.is_set() is True
    assert agent.state.restriction_reason == "pause"
    assert thread.status == "paused"
    assert thread.is_alive()


def test_resume_releases_the_operator_pause(tmp_path, monkeypatch):
    """After an explicit resume the next query runs: the latch is not a door."""
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    thread._agent = _make_agent()
    agent = thread._agent

    def _fake_tool_loop(self, query):
        self._last_elapsed_val = 0.0
        return json.dumps({"response": f"echo: {query}", "query": query})

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)
    logged = _capture_log(monkeypatch)

    thread.pause(manual_only=True)
    assert thread._manual_only_pause is True
    assert _wait_until(lambda: thread.status == "paused"), (
        f"worker never parked (status={thread.status!r})"
    )

    assert thread.resume() is True
    assert thread._manual_only_pause is False
    assert agent.state.restriction_reason is None
    assert _wait_until(lambda: thread.status != "paused"), (
        f"worker never left the paused status (status={thread.status!r})"
    )

    reply = thread.send_query("are we running again?", timeout=15.0)

    assert "echo:" in reply and "are we running again?" in reply
    assert thread._park_event.is_set() is False
    assert not [m for _, _, m in logged if "[CONTROL_PLANE_PAUSE_HOLD]" in m], logged


def test_pause_tool_latches_and_records_the_operator_pause(tmp_path, monkeypatch):
    """The ``pause`` ACTION must leave a latch a later turn can actually find.

    ``Worker(action="pause", worker_query="manual_only")`` is the only way the
    agent can reach an operator pause, and it stamps ``_manual_only_pause``
    itself before calling ``pause()``.  That latch lives and dies with the
    thread object, so the pause must ALSO be recorded session-scoped --
    otherwise the next operator turn has nothing to release.
    """
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    thread._agent = _make_agent()

    tool = Worker(
        action="pause",
        worker_name="w1",
        worker_query="manual_only",
        session_id=SESSION_ID,
    )
    envelope = tool._action_pause([])

    assert envelope["manual_only"] is True
    assert envelope["workers_paused"] == [{"worker_name": "w1", "instance_id": 1}]
    assert thread._manual_only_pause is True
    with worker_module._MANUAL_ONLY_PAUSED_LOCK:
        recorded = set(worker_module._MANUAL_ONLY_PAUSED.get(SESSION_ID, set()))
    assert recorded == {("w1", 1)}, recorded


def test_query_is_refused_before_the_loop_observes_the_park(tmp_path, monkeypatch):
    """THE RACE: the latch is set at once, ``status`` flips later.

    ``pause()`` sets the park event and the marker immediately, but the worker's
    loop only writes ``status = "paused"`` when it next reaches its safe
    boundary.  A query arriving inside that window used to be DELIVERED (the
    ``status == "paused"`` branch never ran), which silently unpaused the
    worker: the operator's pause "did nothing".  The latch is now tested above
    every status branch.
    """
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    thread._agent = _make_agent()

    def _unreachable_tool_loop(self, query):
        raise AssertionError("a manually paused worker must not be sent a query")

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _unreachable_tool_loop)

    # The measured window: the operator's latch is set, the loop has not reached
    # its boundary yet, so the status still says "ready".
    thread._manual_only_pause = True
    assert thread.status == "ready"

    tool = Worker(
        action="query",
        worker_name="w1",
        worker_query="hello",
        session_id=SESSION_ID,
    )
    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"the latch did not short-circuit the query ({elapsed:.1f}s)"
    assert envelope == {
        "status": "paused",
        "message": "Worker is manually paused",
        "worker_name": "w1",
        "session_id": SESSION_ID,
    }
    assert thread._manual_only_pause is True
    assert thread.status == "ready" and thread.is_alive()


def test_pause_tool_latches_intent_even_when_the_loop_is_not_alive(tmp_path, monkeypatch):
    """The latch is set BEFORE the liveness test, and the skip is logged.

    A worker whose loop is not running at this instant must still receive the
    operator's latch (the pause may not "do nothing" merely because the thread
    was between turns), and the not-paused-now outcome must be visible instead
    of silent.
    """
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    logged = _capture_log(monkeypatch)
    monkeypatch.setattr(thread, "is_alive", lambda: False)

    tool = Worker(
        action="pause",
        worker_name="w1",
        worker_query="manual_only",
        session_id=SESSION_ID,
    )
    envelope = tool._action_pause([])

    assert thread._manual_only_pause is True
    assert envelope["workers_paused"] == []
    warnings = [(lvl, tag, msg) for lvl, tag, msg in logged if lvl == "WARNING"]
    assert warnings and all(tag == "tools.worker" for _, tag, _ in warnings), warnings
    assert any(
        "w1" in msg and "loop not running" in msg for _, _, msg in warnings
    ), warnings


def test_user_turn_release_resumes_the_recorded_workers(tmp_path, monkeypatch):
    """``_resume_manual_only_paused`` is the latch's only release path."""
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    thread._agent = _make_agent()

    thread.pause(manual_only=True)
    assert _wait_until(lambda: thread.status == "paused"), (
        f"worker never parked (status={thread.status!r})"
    )

    assert worker_module._resume_manual_only_paused(SESSION_ID) == ["w1"]
    assert thread._manual_only_pause is False
    assert _wait_until(lambda: thread.status != "paused"), (
        f"worker never left the paused status (status={thread.status!r})"
    )
    assert thread.is_alive()
    with worker_module._MANUAL_ONLY_PAUSED_LOCK:
        assert SESSION_ID not in worker_module._MANUAL_ONLY_PAUSED
    # Popped, never swept: a second operator turn has nothing left to do.
    assert worker_module._resume_manual_only_paused(SESSION_ID) == []


def test_only_an_operator_turn_releases_it(tmp_path, monkeypatch):
    """Worker-mode turns share the session id and must NOT release the latch.

    It must also not hide behind ``_discard_session_main_pause``: the operator
    can pause workers without pausing the main agent, so there may be no
    session main-pause marker to discard.
    """
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", SESSION_ID)
    thread._agent = _make_agent()
    thread.pause(manual_only=True)
    assert _wait_until(lambda: thread.status == "paused"), (
        f"worker never parked (status={thread.status!r})"
    )
    # The operator paused WORKERS, not the main agent.
    worker_module._SESSION_MAIN_PAUSED.discard(SESSION_ID)
    assert SESSION_ID not in worker_module._SESSION_MAIN_PAUSED

    assert _discard_manual_only_worker_pause(SESSION_ID, worker_mode=True) == []
    assert thread._manual_only_pause is True and thread.status == "paused"

    assert _discard_manual_only_worker_pause(SESSION_ID, worker_mode=False) == ["w1"]
    assert thread._manual_only_pause is False
    assert _wait_until(lambda: thread.status != "paused"), (
        f"worker never left the paused status (status={thread.status!r})"
    )
