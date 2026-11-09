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
