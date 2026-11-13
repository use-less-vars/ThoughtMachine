"""Control-plane instrumentation: mutation site vs dispatch gate state identity.

Diagnostic pin for the Gen 61 dispatch 13 propagation question: is the
``AgentState`` instance mutated by ``AgentState.restrict()`` the same object
``ToolExecutor.execute_tool_calls`` consults at its dispatch gate?

Both sides stamp ``state_id=id(...)`` on their own diagnostic log line
(``[CONTROL_PLANE_STATE]`` emitted through ``agent.logging.log`` at the
mutation site, ``[CONTROL_PLANE_GATE]`` emitted through the stdlib logger at
the gate).  This test drives BOTH real code paths with the SAME state object
and compares the two identities; it does not assert a fix.

Run with::

    pytest tests/test_control_plane_instrumentation.py -v
"""

from __future__ import annotations

import logging
import re

import pytest

from agent.config.models import AgentConfig
from agent.core.state import AgentState
from agent.core.tool_executor import ToolExecutor
from agent.logging import set_log_level, set_log_tags


_STATE_ID_RE = re.compile(r"\[CONTROL_PLANE_STATE\].*?\bstate_id=(\d+)")
_GATE_ID_RE = re.compile(r"\[CONTROL_PLANE_GATE\].*?\bstate_id=(\d+)")


@pytest.fixture()
def console_for_state_tag():
    """agent.logging.log() prints INFO to console only when the tag is enabled
    (default console threshold is WARNING).  Enable exactly our own tag so the
    mutation-site line reaches stderr, then restore the previous configuration.
    """
    previous_tags = set_log_tags(["[CONTROL_PLANE_STATE]"])
    previous_level = set_log_level("INFO")
    try:
        yield
    finally:
        set_log_tags(previous_tags)
        set_log_level(previous_level)


def test_mutation_site_and_gate_agree_on_state_identity(
    capsys, caplog, console_for_state_tag
):
    config = AgentConfig(api_key="test-key", enable_logging=False)
    state = AgentState(config=config)
    executor = ToolExecutor(
        tool_classes=[],
        config=config,
        state=state,
        logger=None,
        security_available=False,
        agent=None,
    )

    # (i) MUTATION SITE: the real pause path mutates the real state object.
    changed = state.restrict("pause")
    assert changed is True
    state_stderr = capsys.readouterr().err
    state_lines = [
        line for line in state_stderr.splitlines() if "[CONTROL_PLANE_STATE]" in line
    ]
    assert state_lines, (
        "no [CONTROL_PLANE_STATE] line was emitted by AgentState.restrict(); "
        f"stderr={state_stderr!r}"
    )
    mutation_match = _STATE_ID_RE.search(state_lines[-1])
    assert mutation_match, state_lines[-1]
    mutation_id = mutation_match.group(1)

    # (ii) DISPATCH GATE: the real public entry point, same state object,
    # a tool name the active 'pause' restriction forbids.
    conversation: list = []
    with caplog.at_level(logging.INFO):
        executed, *_rest = executor.execute_tool_calls(
            [{"id": "tc-1", "function": {"name": "Thought", "arguments": "{}"}}],
            add_to_conversation_func=conversation.append,
            session_id="test-control-plane",
        )
    gate_lines = [
        record.getMessage()
        for record in caplog.records
        if "[CONTROL_PLANE_GATE]" in record.getMessage()
    ]
    assert gate_lines, (
        "no [CONTROL_PLANE_GATE] record was emitted by the dispatch gate; "
        f"records={[r.getMessage() for r in caplog.records]!r}"
    )
    gate_line = gate_lines[-1]
    gate_match = _GATE_ID_RE.search(gate_line)
    assert gate_match, gate_line
    gate_id = gate_match.group(1)

    # agent.logging.log() writes to stderr and is gated at WARNING+, so INFO diagnostics are invisible under pytest; this print is the only way the ids show in CI output.
    print(f"[CONTROL_PLANE_TEST] mutation_state_id={mutation_id} gate_state_id={gate_id} same={mutation_id == gate_id}")

    # (iii) THE PROPAGATION QUESTION: same object seen at both ends?
    assert gate_id == mutation_id, (
        "STATE IDENTITY NOT PROPAGATED: AgentState.restrict('pause') mutated the "
        f"state object with id={mutation_id}, but the ToolExecutor dispatch gate "
        f"consulted the state object with id={gate_id}. The gate therefore reads a "
        "DIFFERENT (stale or unshared) AgentState instance than the pause mutation "
        "site, so the 'pause' restriction cannot refuse tool calls. "
        f"gate line: {gate_line!r}"
    )

    # (iv) the gate really took the refusal branch, and it saw the pause flag.
    assert executed, executed
    assert executed[0].get("success") is False, executed
    assert "not allowed" in executed[0]["result"], executed[0]["result"]
    assert "restrictions_active=True" in gate_line, gate_line
    assert "restriction_reason=pause" in gate_line, gate_line


# ══════════════════════════════════════════════════════════════════════════════
# C22-class: WORKER pause — the state seen by the worker's agent loop vs the
# state held by that worker's ToolExecutor.
# ══════════════════════════════════════════════════════════════════════════════

def test_worker_pause_executor_observes_the_same_state_object(
    tmp_path, capsys, caplog, console_for_state_tag
):
    """A REAL WorkerThread (never ``start()``ed) + a REAL per-worker Agent.

    Construction follows ``tests/test_worker_pause_reading_mode.py``
    (``_unstarted_thread`` + a real ``Agent`` assigned to ``thread._agent``);
    the pause is triggered through ``WorkerThread.pause()``, the production
    entry point, which calls ``agent.enter_reading_mode('pause')``.

    Question: does the ``AgentState`` the worker's loop holds identity-equal the
    one its ``ToolExecutor`` consults at the dispatch gate?
    """
    from pathlib import Path

    from agent.core.agent import Agent
    from tools.workspace.worker_thread import WorkerThread

    # (1) the worker, built exactly like the pause precedent builds it.
    worker = WorkerThread(
        name="test-worker-cp",
        definition={"name": "test-worker-cp", "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id="sess-worker-cp",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    # The per-worker Agent is normally built lazily on the first query inside
    # run() (worker_thread.py `if self._agent is None: ... self._agent = Agent(...)`).
    # An unstarted thread never gets there, so the precedent assigns the agent
    # directly -- the same object the loop would have been driving.
    assert worker._agent is None
    worker._agent = Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session_id="sess-worker-cp",
    )
    assert worker._agent is not None
    assert worker._agent.state is not None

    # (2) identity of the state object the worker's loop would run against.
    loop_state_id = id(worker._agent.state)
    assert worker._agent.state.restrictions_active is False, "precondition: unpaused"

    # (3) trigger the pause through the production worker entry point.
    worker.pause()

    # (4) the restriction landed on THAT object, with the expected token.
    assert worker._agent.state.restrictions_active is True
    assert worker._agent.state.restriction_reason == "pause"

    executor = worker._agent.tool_executor

    # (5a) IDENTITY: the executor is wired to the very same AgentState.
    # C22-class check: if this ever becomes `None`, the executor is a fail-open
    # surface (see briefing C22); do not weaken the assertion below to pass.
    assert executor.state is not None, (
        "# C22-class: executor built with state=None; this is the fail-open surface"
    )
    assert executor.state is worker._agent.state, (
        "EXECUTOR STATE IDENTITY SPLIT on the worker path: "
        f"worker._agent.state id={id(worker._agent.state)} but "
        f"worker._agent.tool_executor.state id={id(executor.state)}"
    )

    # (5b) OBSERVABLE: one real tool call through the executor is REFUSED.
    conversation: list = []
    with caplog.at_level(logging.INFO):
        executed, *_rest = executor.execute_tool_calls(
            [{"id": "tc-cp-1", "function": {"name": "Thought", "arguments": "{}"}}],
            add_to_conversation_func=conversation.append,
            session_id="sess-worker-cp",
        )
        gate_stderr = capsys.readouterr().err
    gate_lines = [
        record.getMessage()
        for record in caplog.records
        if "[CONTROL_PLANE_GATE]" in record.getMessage()
    ]
    assert gate_lines, (
        "no [CONTROL_PLANE_GATE] record emitted for the worker's executor; "
        f"records={[r.getMessage() for r in caplog.records]!r} stderr={gate_stderr!r}"
    )
    gate_line = gate_lines[-1]
    gate_match = _GATE_ID_RE.search(gate_line)
    assert gate_match, gate_line
    gate_state_id = int(gate_match.group(1))

    assert executed, executed
    assert executed[0].get("success") is False, executed
    assert "not allowed" in executed[0]["result"], executed[0]["result"]
    assert "restrictions_active=True" in gate_line, gate_line
    assert "restriction_reason=pause" in gate_line, gate_line

    # agent.logging INFO is invisible under pytest; this print is the only
    # place the three ids become visible in CI output.
    print(
        f"[CONTROL_PLANE_WORKER_TEST] loop_state_id={loop_state_id} "
        f"exec_state_id={id(executor.state)} gate_state_id={gate_state_id} "
        f"same={loop_state_id == gate_state_id == id(executor.state)}"
    )

    # (5c) THE CORE ASSERTION: gate == loop == executor, all one object.
    assert gate_state_id == loop_state_id, (
        "WORKER STATE PROPAGATION BROKEN: the worker loop holds AgentState "
        f"id={loop_state_id}, but its ToolExecutor gate consulted id={gate_state_id}. "
        f"gate line: {gate_line!r}"
    )
    assert gate_state_id == id(executor.state), (gate_state_id, id(executor.state))

    # The identity of the state actually ASKED (not just logged) must be the loop's.
    assert worker._agent.state.is_tool_allowed("Thought") is False
    assert worker._agent.state.get_allowed_tools() == ["Respond"]
