"""The resume notice: a pause that ends must tell the model it may act again.

Pause is one-way from the model's point of view: `_apply_pause_reading_mode`
buffers a `[System Notice: ...]` nudge saying "tool calls are refused, the
session stays paused until it is resumed".  Nothing used to be injected on the
way OUT, so a resumed agent stayed latched in the paused reading of its own
history.

`leave_reading_mode()` now arms `_resume_notice_pending` when it actually
releases a 'pause'-owned restriction, and `_apply_resume_notice()` -- on the
turn thread, before the next LLM call -- writes the symmetric notice to
history exactly once.

These tests pin the contract at the Agent level and need no network: the one
test that runs a real turn reuses the scripted-stream harness from
tests/test_agent_stop_midstream.py (the same one tests/test_agent_reading_mode.py
uses), so the LLM is faked end to end.
"""

from __future__ import annotations

from agent.core.message import SYSTEM_NOTICE_PREFIX
from agent.core.stream_events import Done, TextDelta
from tests.test_agent_reading_mode import _make_agent
from tests.test_agent_stop_midstream import _ScriptedLLM

RESUME_NOTICE_TEXT = (
    "[System Notice: You are resumed. The pause is lifted. "
    "Tool calls are allowed.]"
)


def _resume_notices(agent):
    """Committed resume notices (Message is a dict subclass -> use .get())."""
    return [
        message
        for message in agent.conversation
        if (message.get("metadata") or {}).get("kind") == "resume_notice"
    ]


class _RecordingLLM:
    """Async scripted stream that snapshots the messages handed to the provider.

    Mirrors _ScriptedLLM's async-generator shape (the turn loop calls
    ``aclose()`` on it), but records each ``messages`` list so a test can
    assert what the MODEL saw, not merely what landed in history.
    """

    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0
        self.seen = []

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        self.seen.append([message.get("content", "") for message in messages])
        for event in self._turns[index]:
            yield event


def _talking_agent(session_id: str):
    """An agent whose one scripted turn answers directly (no tool calls)."""
    agent = _make_agent()
    agent.session_id = session_id
    agent.llm_client.stream_completion = _ScriptedLLM(
        [[TextDelta(text="Hello."), Done()]]
    ).stream_completion
    return agent


class TestResumeNoticeIsInjected:
    """Pause -> resume writes exactly one notice, with the specified wording."""

    def test_resume_after_pause_injects_the_notice_once(self):
        agent = _make_agent()

        agent.enter_reading_mode()
        assert agent.leave_reading_mode() is True

        # Arming is a flag only: the cross-thread contract forbids building a
        # Message inside leave_reading_mode().
        assert agent._resume_notice_pending is True
        assert _resume_notices(agent) == []

        agent._apply_resume_notice()

        notices = _resume_notices(agent)
        assert len(notices) == 1, agent.conversation
        assert notices[0]["content"] == RESUME_NOTICE_TEXT

    def test_notice_matches_the_pause_notice_formatting(self):
        agent = _make_agent()
        agent.enter_reading_mode()
        agent.leave_reading_mode()
        agent._apply_resume_notice()

        notice = _resume_notices(agent)[0]
        # Same family/shape as the pause nudge, so the model parses it alike.
        assert notice["role"] == "user"
        assert notice["is_system_notification"] is True
        assert notice["content"].startswith(SYSTEM_NOTICE_PREFIX)
        assert notice["content"].endswith("]")

    def test_the_flag_is_consumed_so_later_turns_do_not_re_inject(self):
        agent = _make_agent()
        agent.enter_reading_mode()
        agent.leave_reading_mode()

        agent._apply_resume_notice()
        assert len(_resume_notices(agent)) == 1

        # Every later checkpoint/turn start calls the same method; nothing is
        # armed any more, so it must stay at one notice.
        agent._apply_resume_notice()
        agent._apply_resume_notice()
        assert len(_resume_notices(agent)) == 1, agent.conversation
        assert agent._resume_notice_pending is False


class TestNoNoticeWhenNeverPaused:
    """The notice is guarded on the actual prior paused state."""

    def test_idle_agent_never_arms_the_flag(self):
        agent = _make_agent()

        # Not paused: leave_reading_mode() releases nothing.
        assert agent.leave_reading_mode() is False
        assert agent._resume_notice_pending is False

        agent._apply_resume_notice()
        assert _resume_notices(agent) == []

    def test_a_normal_turn_gets_no_resume_notice(self):
        agent = _talking_agent("test-resume-notice-normal-turn")

        events = list(agent.process_query("hello"))

        assert "agent_responded" in [event["type"] for event in events], events
        assert _resume_notices(agent) == [], agent.conversation

    def test_resume_then_a_normal_turn_injects_it_once(self):
        agent = _talking_agent("test-resume-notice-turn")
        agent.enter_reading_mode()
        agent.leave_reading_mode()

        list(agent.process_query("carry on"))

        assert len(_resume_notices(agent)) == 1, agent.conversation
        assert _resume_notices(agent)[0]["content"] == RESUME_NOTICE_TEXT

    def test_a_non_pause_restriction_never_claims_a_pause_was_lifted(self):
        agent = _make_agent()
        # A monitor owns the restriction, not the Pause button.
        agent.state.restrict("token")

        # Owner-agnostic clear: it releases something, but not a pause.
        assert agent.leave_reading_mode(None) is True
        assert agent._resume_notice_pending is False

        agent._apply_resume_notice()
        assert _resume_notices(agent) == []


class TestNoticeReachesTheModel:
    """The notice must be in the messages of the FIRST post-resume LLM call.

    A notice written to history after the model has already been called fixes
    nothing, so this is the assertion that actually pins the C26 contract.
    """

    def test_first_llm_call_after_resume_carries_the_notice(self):
        agent = _make_agent()
        llm = _RecordingLLM([[TextDelta(text="Hello."), Done()]])
        agent.llm_client.stream_completion = llm.stream_completion

        agent.enter_reading_mode()
        agent.leave_reading_mode()
        list(agent.process_query("carry on"))

        assert len(llm.seen) == 1, llm.seen
        first_call = llm.seen[0]
        assert RESUME_NOTICE_TEXT in first_call, first_call
        # Freshest possible signal: it is the last thing the model reads.
        assert first_call[-1] == RESUME_NOTICE_TEXT

    def test_first_llm_call_without_a_pause_carries_no_notice(self):
        agent = _make_agent()
        llm = _RecordingLLM([[TextDelta(text="Hello."), Done()]])
        agent.llm_client.stream_completion = llm.stream_completion

        list(agent.process_query("carry on"))

        assert len(llm.seen) == 1, llm.seen
        assert RESUME_NOTICE_TEXT not in llm.seen[0]


class TestResumeTwice:
    """Idempotent: a double resume must not double the notice."""

    def test_second_resume_releases_nothing_and_injects_nothing(self):
        agent = _make_agent()
        agent.enter_reading_mode()

        assert agent.leave_reading_mode() is True
        assert agent.leave_reading_mode() is False  # nothing left to release

        agent._apply_resume_notice()
        agent._apply_resume_notice()

        assert len(_resume_notices(agent)) == 1, agent.conversation

    def test_pause_resume_pause_resume_yields_two_notices(self):
        """Once per resume -- but each resume does get its own notice."""
        agent = _make_agent()

        for _ in range(2):
            agent.enter_reading_mode()
            agent.leave_reading_mode()
            agent._apply_resume_notice()

        assert len(_resume_notices(agent)) == 2, agent.conversation
