"""Reading mode: pause asks the agent to conclude, it never truncates the turn.

Pause is implemented as a state restriction owned by the reason 'pause'
(AgentState.restrict('pause')), which narrows the allow-list to ['Respond'].
The Pause button calls enter_reading_mode(); the turn thread's checkpoint
(_apply_pause_reading_mode) buffers at most ONE [System Notice: ...] nudge
asking the model to wrap up with a Respond, and the turn concludes normally.

These tests pin that contract at the Agent level (no truncation, one nudge per
pause, owner-scoped release) and at the Controller level (pause = reading mode,
resume = owner-scoped release). Checkpoint [3] -- a pause that arrived while
tools were executing -- is covered structurally only, see
TestCheckpointThreeIsStructuralOnly.
"""

from __future__ import annotations

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.message import SYSTEM_NOTICE_PREFIX, is_system_notice_text
from agent.core.stream_events import Done, TextDelta
from tests.test_agent_stop_midstream import _ScriptedLLM


def _make_agent() -> Agent:
    config = AgentConfig(api_key="test-key", enable_logging=False)
    return Agent(config=config, session_id="test-reading-mode")


def _fake_tool_calls():
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "Thought", "arguments": "{}"},
        }
    ]


def _nudges(agent: Agent):
    """Buffered pause nudges (Message is a dict subclass -> use .get())."""
    out = []
    for message in agent._pending_warnings:
        metadata = message.get("metadata") if hasattr(message, "get") else None
        if metadata and metadata.get("pause_nudge"):
            out.append(message)
    return out


def _halt_agent(session_id: str, turns=None) -> Agent:
    """An agent wired to halt mid-stream after the first delta.

    Reuses the scripted-stream harness from tests/test_agent_stop_midstream.py
    (the halt contract's own tests) rather than building a second one.
    """
    if turns is None:
        turns = [[TextDelta(text="Par"), TextDelta(text="tial"), Done()]]
    agent = Agent(config=AgentConfig(api_key="test-key", enable_logging=False), session_id=session_id)
    llm = _ScriptedLLM(turns)
    agent.llm_client.stream_completion = llm.stream_completion
    polls = {"n": 0}

    def halt_check():
        # False on the first poll (delta #1 is allowed through), True after.
        polls["n"] += 1
        return polls["n"] > 1

    agent.halt_check = halt_check
    return agent


def _stop_notices(agent: Agent):
    """Committed stop notices (Message is a dict subclass -> use .get())."""
    return [
        message
        for message in agent.conversation
        if (message.get("metadata") or {}).get("kind") == "stop_notice"
    ]


class TestEnterLeaveReadingMode:
    """enter_reading_mode()/leave_reading_mode() are the Pause/Resume verbs."""

    def test_enter_reading_mode_restricts_to_respond(self):
        agent = _make_agent()
        assert agent.state.is_tool_allowed("ContainerExecTool") is True

        changed = agent.enter_reading_mode()

        assert changed is True
        assert agent.state.restriction_reason == "pause"
        assert agent.state.restrictions_active is True
        assert agent.state.is_tool_allowed("Respond") is True
        assert agent.state.is_tool_allowed("ContainerExecTool") is False
        assert agent.state.get_allowed_tools() == ["Respond"]
        # Cross-thread contract: the flag is set, no message is built here.
        assert agent._pause_requested is True
        assert agent._pending_warnings == []

    def test_enter_reading_mode_twice_is_a_noop(self):
        agent = _make_agent()
        assert agent.enter_reading_mode() is True
        assert agent.enter_reading_mode() is False
        assert agent.state.restriction_reason == "pause"

    def test_leave_reading_mode_releases_pause(self):
        agent = _make_agent()
        agent.enter_reading_mode()
        assert agent.leave_reading_mode() is True
        assert agent.state.restriction_reason is None
        assert agent.state.is_tool_allowed("ContainerExecTool") is True
        assert agent._pause_requested is False

    def test_leave_reading_mode_when_idle_returns_false(self):
        agent = _make_agent()
        assert agent.leave_reading_mode() is False
        assert agent.state.restrictions_active is False

    def test_leave_reading_mode_cannot_steal_a_token_restriction(self):
        agent = _make_agent()
        agent.state.restrict("token")

        assert agent.leave_reading_mode() is False
        # The monitor's restriction survives untouched.
        assert agent.state.restriction_reason == "token"
        assert agent.state.restrictions_active is True
        assert agent.state.is_tool_allowed("SummarizeTool") is True


class TestPauseCheckpoint:
    """_apply_pause_reading_mode() nudges once and never halts the turn."""

    def test_nudge_is_buffered_exactly_once_per_pause(self):
        agent = _make_agent()
        agent.enter_reading_mode()

        agent._apply_pause_reading_mode(_fake_tool_calls())
        assert len(_nudges(agent)) == 1

        agent._apply_pause_reading_mode(_fake_tool_calls())
        assert len(_nudges(agent)) == 1
        assert len(agent._pending_warnings) == 1

        nudge = _nudges(agent)[0]
        assert nudge["role"] == "user"
        assert nudge["is_system_notification"] is True
        assert nudge["content"].startswith(SYSTEM_NOTICE_PREFIX)
        assert "paused" in nudge["content"]
        assert "Respond" in nudge["content"]

    def test_checkpoint_consumes_request_without_halting(self):
        agent = _make_agent()
        agent.enter_reading_mode()
        execution_state_before = agent.state.execution_state
        allowed_before = agent.state.get_allowed_tools()

        result = agent._apply_pause_reading_mode(_fake_tool_calls())

        # No truncation signal: the checkpoint returns None, the pause request
        # is consumed, and the turn keeps its state (it concludes on its own).
        assert result is None
        assert agent._pause_requested is False
        assert agent.state.execution_state == execution_state_before
        assert agent.state.get_allowed_tools() == allowed_before == ["Respond"]
        assert agent.state.restriction_reason == "pause"

    def test_checkpoint_is_noop_for_a_token_restriction(self):
        agent = _make_agent()
        agent.state.restrict("token")
        agent._pause_requested = True

        agent._apply_pause_reading_mode(_fake_tool_calls())

        # The monitors own non-pause reasons and inject their own warnings.
        assert _nudges(agent) == []
        assert agent._pending_warnings == []
        assert agent.state.restriction_reason == "token"
        # A pause request under another owner's restriction is left alone.
        assert agent._pause_requested is True

    def test_checkpoint_without_tool_calls_buffers_nothing(self):
        agent = _make_agent()
        agent.enter_reading_mode()

        agent._apply_pause_reading_mode(None)

        # Nothing will execute, so there is nothing to nudge before; the turn
        # is already concluding.
        assert _nudges(agent) == []
        assert agent._pause_requested is False

    def test_resume_then_pause_again_yields_a_fresh_nudge(self):
        agent = _make_agent()
        agent.enter_reading_mode()
        agent._apply_pause_reading_mode(_fake_tool_calls())
        assert len(_nudges(agent)) == 1

        assert agent.leave_reading_mode() is True
        assert agent.enter_reading_mode() is True

        agent._apply_pause_reading_mode(_fake_tool_calls())
        assert len(_nudges(agent)) == 2


def _make_controller(agent: Agent):
    """A real AgentController bound to `agent`, without starting its thread."""
    from agent.controller import AgentController

    controller = AgentController()
    if agent.conversation is None:
        agent.conversation = []
    controller.agent = agent
    return controller


class TestControllerPauseResume:
    """Pause/Resume routed through the controller: reading mode, then release."""

    def test_pause_through_controller_narrows_tools_and_resume_restores_them(self):
        agent = _make_agent()
        controller = _make_controller(agent)

        controller.pause()

        assert agent.state.restriction_reason == "pause"
        assert agent.state.is_tool_allowed("ContainerExecTool") is False
        assert agent.state.is_tool_allowed("Respond") is True

        controller.resume()

        assert agent.state.restriction_reason is None
        assert agent.state.is_tool_allowed("ContainerExecTool") is True
        assert agent._pause_requested is False

    def test_resume_leaves_a_restriction_it_does_not_own(self):
        agent = _make_agent()
        controller = _make_controller(agent)

        controller.pause()
        # A monitor re-attributes the restriction to 'token' while paused.
        assert agent.state.restrict("token") is True

        controller.resume()

        # Controller.resume() releases only a 'pause'-owned restriction, so the
        # token restriction survives the resume untouched.
        assert agent.state.restriction_reason == "token"
        assert agent.state.is_tool_allowed("ContainerExecTool") is False
        assert agent.state.is_tool_allowed("SummarizeTool") is True


class TestCheckpointThreeIsStructuralOnly:
    """Checkpoint [3] (pause arriving while tools execute) no longer truncates.

    Covered STRUCTURALLY only: the checkpoint lives deep inside the turn
    generator, and what the change removed is the halt itself, so the body
    following the checkpoint is asserted to contain the reading-mode call and
    none of the old PAUSING / 'paused' event / 'return' halt.
    """

    def _checkpoint3_window(self):
        """The statements that follow the checkpoint [3] marker (900 chars)."""
        import inspect

        source = inspect.getsource(Agent)
        start = source.index("PAUSE CHECKPOINT [3]")
        return source[start:start + 900]

    def test_checkpoint_three_applies_reading_mode(self):
        window = self._checkpoint3_window()

        assert "_apply_pause_reading_mode(tool_calls)" in window

    def test_checkpoint_three_no_longer_halts_the_turn(self):
        window = self._checkpoint3_window()
        source = __import__("inspect").getsource(Agent)
        code_lines = [
            line.strip()
            for line in window.splitlines()
            if not line.strip().startswith("#")
        ]

        # The old halt: PAUSING transition + turn-truncating 'paused' event + return.
        assert "ExecutionState.PAUSING" not in window
        assert "stop_reason': 'paused'" not in window
        assert [line for line in code_lines if line == "return"] == []
        # ...and the removed branch is gone from the whole class, not just here.
        assert "Pause detected after turn processing" not in source


class TestStopNotice:
    """A hard stop leaves a notice in history, AFTER the partial answer it explains."""

    def test_halt_appends_a_stop_notice_after_the_partial_answer(self):
        agent = _halt_agent("test-stop-notice")

        events = list(agent.process_query("tell me a long story"))
        assert "interrupted" in [event["type"] for event in events], events

        notices = _stop_notices(agent)
        assert len(notices) == 1, agent.conversation
        notice = notices[0]
        assert notice["role"] == "user"
        assert notice["is_system_notification"] is True
        assert notice["content"] == (
            "[System Notice: You were stopped by the user. "
            "Review your current progress and resume.]"
        )

        # Ordering: the truncated answer stays ahead of the notice about it.
        notice_at = next(i for i, message in enumerate(agent.conversation) if message is notice)
        last_assistant = max(
            i for i, message in enumerate(agent.conversation) if message.get("role") == "assistant"
        )
        assert agent.conversation[last_assistant]["content"] == "Par"
        assert notice_at == last_assistant + 1, agent.conversation

    def test_stop_notice_and_pause_nudge_share_the_notice_predicate(self):
        agent = _halt_agent("test-stop-notice-predicate", [[TextDelta(text="Par"), Done()]])
        list(agent.process_query("tell me a long story"))
        stop_notice = _stop_notices(agent)[0]

        paused = _make_agent()
        paused.enter_reading_mode()
        paused._apply_pause_reading_mode(_fake_tool_calls())
        pause_nudge = _nudges(paused)[0]

        for message in (stop_notice, pause_nudge):
            assert message["role"] == "user"
            assert message["is_system_notification"] is True
            assert is_system_notice_text(message["content"]) is True
            assert message["content"].startswith(SYSTEM_NOTICE_PREFIX)
            assert message["content"].endswith("]")
            # ...and neither is a monitor notification any more.
            assert "[SYSTEM NOTIFICATION]" not in message["content"]

    def test_each_halted_turn_gets_its_own_notice(self):
        agent = _halt_agent(
            "test-stop-notice-twice",
            [
                [TextDelta(text="Par"), TextDelta(text="tial"), Done()],
                [TextDelta(text="Second"), Done()],
            ],
        )

        list(agent.process_query("first query"))
        assert agent._stop_notice_sent is True

        # The per-turn reset lets the next stopped turn explain itself too.
        list(agent.process_query("second query"))
        assert len(_stop_notices(agent)) == 2, agent.conversation
