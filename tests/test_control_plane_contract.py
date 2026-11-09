"""Contract pins for the four-piece control plane (Phase 1: tests only).

The control plane is four pieces, and this module pins each of them at the
seam a Phase-2 implementation must satisfy:

1. **persistent state** -- ``AgentState`` carries a RUN state
   (``ExecutionState``: running / pausing / paused / stopped) that is a
   DIFFERENT axis from ``SessionState`` (new / continuing / resetting, the
   lifecycle axis) and from a worker's free-string ``status``.
2. **interrupt signal** -- ``AgentController.pause_event`` is the canonical
   pause-REQUESTED event with normal cancel polarity: **cleared = running,
   set = paused**.  ``tools.cancellation.bind_pause_event()`` publishes it to
   the thread that is executing a tool, so a blocked tool wakes on pause.
3. **tool return contract** -- an interrupted tool returns an envelope
   (``status == "interrupted"``) that the loop recognises as "stop working,
   conclude with Respond", never as a normal exit code.
4. **loop gate** -- a new worker query is an implicit UNPAUSE, but only when
   the restriction is owned by ``'pause'``: a ``'token'``/``'timeout'``/
   ``'turn'`` restriction is never auto-cleared.

Every test is tagged in its ``def`` line:

* ``# PIN`` -- passes today; it pins behaviour a Phase-2 change must NOT break.
* ``# RED`` -- MUST fail today; it is the specification Phase 2 has to make
  green.  No assertion here is written to be "close enough": each RED test
  fails on the behaviour it names (a missing enum member, a wrong value, a
  missing symbol), never on an incidental import error of the module itself.

Run with::

    python -m pytest tests/test_control_plane_contract.py -v
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.config.models import AgentConfig
from agent.controller import AgentController
from agent.core.agent import Agent
from agent.core.state import AgentState, ExecutionState, SessionState
from tools.docker_code_runner import DockerCodeRunner
from tools.sleep_tool import Sleep
from tools.workspace.worker_thread import WorkerThread


# ────────────────────────────────────────────────────────────────────────────
# Scaffolding
# ────────────────────────────────────────────────────────────────────────────

def _make_agent(session_id: str = "test-control-plane-contract") -> Agent:
    return Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session_id=session_id,
    )


def _make_controller(agent: Agent) -> AgentController:
    """A real ``AgentController`` bound to *agent*, never ``start()``ed.

    The controller's thread is not needed: pause()/resume() are plain method
    calls on the caller's thread, which is exactly how the bridge uses them.
    """
    controller = AgentController()
    if agent.conversation is None:
        agent.conversation = []
    controller.agent = agent
    return controller


def _unstarted_thread(tmp_path, name: str = "test-worker",
                      session_id: str = "sess-control-plane-contract") -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed.

    ``send_query`` is exercised through its workspace-lifecycle fast path (the
    ``_process_query_via_wlm`` monkeypatch), so no run() loop is needed and no
    reply queue is ever waited on.
    """
    return WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id=session_id,
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


# ────────────────────────────────────────────────────────────────────────────
# 1. interrupt signal: pause_event polarity
# ────────────────────────────────────────────────────────────────────────────

class TestInterruptSignal:
    def test_pause_event_polarity_is_pause_requested(self):  # PIN
        """``pause_event`` means "a pause is REQUESTED", not "allowed to run".

        Polarity is normal cancel polarity: **cleared = running, set =
        paused**.  This is the contract every Phase-2 consumer depends on:

        * ``tools.cancellation.bind_pause_event(controller.pause_event)`` can
          hand the SAME event to a blocked tool, and ``Event.wait()``'s
          existing return value then means "interrupted" with no adapter.
        * ``Agent.process_query``'s sleep sites (``agent/core/agent.py``) become
          ``pause_event.wait(...)`` and wake on pause, not on resume.

        Phase 2b flipped the polarity: ``__init__`` leaves the event cleared,
        ``pause()`` sets it and ``resume()`` clears it, which is what the three
        assertions below pin.  (Before the flip the event was INVERTED --
        ``__init__`` set it and ``pause()`` cleared it -- and this test failed on
        the first assertion.)
        """
        agent = _make_agent()
        controller = _make_controller(agent)

        assert controller.pause_event.is_set() is False

        controller.pause()
        assert controller.pause_event.is_set() is True

        controller.resume()
        assert controller.pause_event.is_set() is False


# ────────────────────────────────────────────────────────────────────────────
# 2/3. persistent state: the RUN axis vs the LIFECYCLE axis
# ────────────────────────────────────────────────────────────────────────────

class TestRunStateAxis:
    def test_execution_state_defines_paused_and_stopped(self):  # RED
        """The RUN axis must be able to say "paused" and "stopped".

        PAUSED is a settled state (the agent has wound down and parked);
        PAUSING is the transient 'a pause was requested' state.  Conflating
        them is what makes the UI show a permanent "pausing" spinner, so the
        two must be distinct members.
        """
        values = {member.value for member in ExecutionState}
        assert values >= {"running", "pausing", "ready", "paused", "stopped"}
        assert ExecutionState.PAUSED is not ExecutionState.PAUSING

    def test_session_state_keeps_its_three_lifecycle_values(self):  # PIN
        """``SessionState`` is the LIFECYCLE axis and nothing else.

        new / continuing / resetting describe whether this is a fresh session,
        a continued one, or one being reset.  They say nothing about whether
        the agent is running, paused or stopped.  Run-state must never leak
        into this enum (and ``SessionState`` must not grow a 'paused' member):
        the two axes are read by different consumers and mixed for display.
        """
        assert {member.value for member in SessionState} == {
            "new", "continuing", "resetting",
        }


# ────────────────────────────────────────────────────────────────────────────
# 4/5/6. persistent state: transitions, notifications, controller landing
# ────────────────────────────────────────────────────────────────────────────

class TestExecutionStateTransitions:
    def test_set_execution_state_walks_running_pausing_paused_stopped(self):  # RED
        """One transition = one ``execution_state_change`` event, old + new.

        The walk is the whole RUN axis in order: ready -> running -> pausing ->
        paused -> stopped.  Each call must record the PAIR (not just the new
        value): consumers rebuild their view from ``old_state``/``new_state``,
        which ``convert_to_legacy_format`` puts at the TOP LEVEL of the event
        dict (there is no ``data`` sub-dict here).
        """
        state = AgentState(config=MagicMock())
        walk = [
            (ExecutionState.RUNNING, "ready", "running"),
            (ExecutionState.PAUSING, "running", "pausing"),
            (ExecutionState.PAUSED, "pausing", "paused"),
            (ExecutionState.STOPPED, "paused", "stopped"),
        ]

        for new_state, expected_old, expected_new in walk:
            events = state.set_execution_state(new_state)
            assert state.execution_state is new_state
            assert len(events) == 1
            assert events[0]["type"] == "execution_state_change"
            assert events[0]["old_state"] == expected_old
            assert events[0]["new_state"] == expected_new

    def test_set_execution_state_notifies_logger_with_paused(self):  # RED
        """The logger is told about the transition, with STRING state values.

        ``AgentLogger.log_execution_state_change`` takes the ``.value``
        strings; a Phase-2 caller must keep going through
        ``set_execution_state()`` rather than assigning
        ``state.execution_state`` directly, or the transition disappears from
        the log.
        """
        logger = MagicMock()
        state = AgentState(config=MagicMock(), logger=logger)
        state.execution_state = ExecutionState.RUNNING

        state.set_execution_state(ExecutionState.PAUSED)

        assert logger.log_execution_state_change.call_count == 1
        logger.log_execution_state_change.assert_called_once_with("running", "paused")

    def test_controller_pause_lands_on_paused_not_pausing(self):  # RED
        """``controller.pause()`` must land the agent on PAUSED by value.

        Today ``pause()`` writes ``ExecutionState.PAUSING`` straight onto
        ``agent.state`` (bypassing ``set_execution_state``, so no logger
        notification either).  The settled state after a pause is PAUSED:
        PAUSING exists only for the window in which the wind-down is still in
        flight.
        """
        agent = _make_agent()
        controller = _make_controller(agent)

        controller.pause()

        assert agent.state.execution_state is ExecutionState.PAUSED


# ────────────────────────────────────────────────────────────────────────────
# 7/8. interrupt signal reaching a BLOCKED tool
# ────────────────────────────────────────────────────────────────────────────

class TestPauseReachesBlockedTool:
    def test_sleep_envelope_reports_paused_reason(self):  # RED
        """A pause-interrupted ``sleep`` reports ``reason == 'paused'``.

        ``tools.cancellation.bind_pause_event(event)`` is the specified (and
        currently missing) symbol: it publishes the shared pause-REQUESTED
        event to the executing thread, so ``Sleep`` waits on the pause signal
        itself instead of on the turn-cancel token.  Today the only binding is
        the turn token and the reason falls back to ``'turn_cancelled'``,
        which the window cannot distinguish from a Stop.
        """
        from tools.cancellation import bind_pause_event, unbind

        state = AgentState(config=MagicMock())
        state.restrict("pause")
        assert state.restriction_reason == "pause"

        pause_event = threading.Event()      # cleared = running, set = paused
        bind_pause_event(pause_event)
        try:
            pause_event.set()                # the pause is signalled
            payload = json.loads(Sleep(seconds=30).execute())
        finally:
            unbind()

        assert payload["interrupted"] is True
        assert payload["reason"] == "paused"

    def test_sleep_wakes_promptly_on_pause(self):  # RED
        """A 30 s sleep returns in well under a second when paused.

        The pause signal is raised from ANOTHER thread (the bridge thread, in
        production), which is why the binding must be a SHARED ``Event``:
        ``cancellation.interrupt()`` is thread-local and can never reach the
        sleeper.  The wait granularity is one 0.1 s slice, so 0.5 s is a
        generous upper bound for one wake-up.
        """
        from tools.cancellation import bind_pause_event, unbind

        pause_event = threading.Event()
        bind_pause_event(pause_event)
        timer = threading.Timer(0.05, pause_event.set)
        try:
            timer.start()
            started = time.monotonic()
            payload = json.loads(Sleep(seconds=30).execute())
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
            unbind()

        assert payload["interrupted"] is True
        assert elapsed < 0.5, f"pause took {elapsed:.3f}s to interrupt a sleep"


# ────────────────────────────────────────────────────────────────────────────
# 9. tool return contract
# ────────────────────────────────────────────────────────────────────────────

class TestToolReturnContract:
    def test_interrupted_tool_result_is_recognised_as_interrupt(self):  # RED
        """An interrupted tool result is a control signal, not a tool failure.

        Specified symbol: ``agent.core.tool_executor:is_interrupted_tool_entry``
        -- a pure predicate over one entry of the ``execute_tool_calls`` result
        (keys ``name``/``arguments``/``result``/``success``/``tool_call_id``)
        that says "this batch stopped because the turn was interrupted; the
        loop must conclude with Respond instead of feeding the result back as
        an ordinary observation".  It does not exist yet, so the local import
        below fails -- that IS the red state, and it keeps module-level
        collection clean for the rest of the file.
        """
        from agent.core.tool_executor import is_interrupted_tool_entry

        def _entry(result: str) -> dict:
            return {
                "name": "Sleep",
                "arguments": "{}",
                "result": result,
                "success": False,
                "tool_call_id": "call_1",
            }

        interrupted = _entry(json.dumps({"status": "interrupted", "reason": "paused"}))
        assert is_interrupted_tool_entry(interrupted) is True

        # A genuine (failed but complete) tool result is NOT an interrupt.
        ordinary = _entry(json.dumps({"success": False, "exit_code": 1, "stderr": "boom"}))
        assert is_interrupted_tool_entry(ordinary) is False


# ────────────────────────────────────────────────────────────────────────────
# 10/11. loop gate: implicit unpause, owner-scoped
# ────────────────────────────────────────────────────────────────────────────

class TestImplicitUnpauseOnNewQuery:
    def test_send_query_does_not_clear_a_token_restriction(self, tmp_path, monkeypatch):  # PIN
        """A new query is an implicit UNPAUSE -- it must not lift anything else.

        Only a ``'pause'``-owned restriction is released by a new query.  A
        ``'token'`` restriction belongs to the token monitor and survives:
        clearing it here would let an operator escape a context-window
        restriction just by asking another question.
        """
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        agent.state.restrict("token")

        monkeypatch.setattr(
            thread, "_process_query_via_wlm",
            lambda query, timeout=120.0: (True, "ok"),
        )
        assert thread.send_query("hello") == "ok"

        assert agent.state.restriction_reason == "token"
        assert agent.state.get_allowed_tools() == ["Respond", "SummarizeTool"]

    def test_send_query_unpauses_a_pause_restricted_worker(self, tmp_path, monkeypatch):  # RED
        """A new query returns a paused worker to RUNNING.

        Pause is a wind-down, not a latch: the operator's next question is the
        resume.  Three observable facts must hold afterwards, all on the
        worker/agent itself (there is no controller in a worker's object
        graph): the 'pause' restriction is gone, the tool allow-list is
        unrestricted, and the park flags are in the running position
        (``_park_event`` clear, ``_resume_event`` set).
        """
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        agent.state.restrict("pause")
        assert agent.state.restriction_reason == "pause"

        monkeypatch.setattr(
            thread, "_process_query_via_wlm",
            lambda query, timeout=120.0: (True, "ok"),
        )
        assert thread.send_query("carry on") == "ok"

        assert agent.state.restriction_reason is None
        assert agent.state.get_allowed_tools() == []
        assert thread._park_event.is_set() is False
        assert thread._resume_event.is_set() is True


# ────────────────────────────────────────────────────────────────────────────
# 12. tool return contract: DockerCodeRunner passthrough
# ────────────────────────────────────────────────────────────────────────────

class TestDockerCodeRunnerInterrupt:
    def test_docker_code_runner_returns_interrupted_envelope_on_pause(
        self, tmp_path, monkeypatch,
    ):  # RED
        """DCR must pass the container layer's interrupted payload through.

        The container layer (``infra/container_manager.py``) already returns
        the canonical envelope on interrupt: ``status='interrupted'``,
        ``interrupted=True``, ``exit_code=None`` -- there IS no exit code,
        because the wait was abandoned and the process was never reaped.

        ``DockerCodeRunner`` does not pass it through: it rewrites the envelope
        with ``exit_code=-3`` and ``success=False``, which reads as a failed
        command instead of an interruption.  ``None`` is canonical; DCR must
        align in Phase 2.
        """
        import tools.docker_code_runner as dcr_mod

        class _FakeManager:
            """Stands in for ``ContainerManager``: blocked, then interrupted."""

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def build_image(self, tag=None):
                return True

            def start(self, **kwargs):
                return {"id": "fake-container"}

            def exec(self, container_id, command=None, timeout=None,
                     workdir=None, environment=None):
                return {
                    "status": "interrupted",
                    "interrupted": True,
                    "reason": "paused",
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "note": "wait abandoned",
                }

            def stop(self, container_id):
                return True

        monkeypatch.setattr(dcr_mod, "ContainerManager", _FakeManager)
        monkeypatch.setattr(
            DockerCodeRunner, "_resolve_registry_workspace",
            lambda self: str(tmp_path),
        )

        payload = json.loads(DockerCodeRunner(command="echo hi").execute())

        assert payload["status"] == "interrupted"
        assert payload["interrupted"] is True
        assert payload["exit_code"] is None
        # The prose already says the right thing; it must keep saying it.
        assert "not killed" in json.dumps(payload).lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
