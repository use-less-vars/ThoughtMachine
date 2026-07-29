"""Skeleton test: Agent + PuppetLLM + event assertions."""

from __future__ import annotations

import pytest

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from tests.mocks.puppet_agent import PuppetLLM
from tests.assertions.event_assertions import (
    assert_event_sequence,
    assert_respond,
    assert_tool_call,
)


class TestPuppetAgentBasic:
    """Agent with a puppet LLM responds without real tool execution."""

    def test_assistant_replies_directly(self):
        """When the LLM replies as an assistant, Agent yields a response."""
        config = AgentConfig(api_key="test-key", enable_logging=False)
        agent = Agent(config=config, session_id="test-session")

        puppet = PuppetLLM(scenario=[
            {"type": "assistant", "content": "Hello from the puppet!"},
        ])
        agent.llm_client.chat_completion = puppet.chat_completion

        events = list(agent.process_query("Say hello"))
        assert_event_sequence(events, ["turn", "agent_responded"])
        assert_respond(events, expected_content="Hello from the puppet!")

    def test_tool_call_then_respond(self):
        """LLM calls a tool, then responds with final answer."""
        config = AgentConfig(api_key="test-key", enable_logging=False)
        agent = Agent(config=config, session_id="test-session")

        puppet = PuppetLLM(scenario=[
            {
                "type": "respond",
                "content": "Task complete.",
                "status": "final",
                "confidence": "high",
            },
        ])
        agent.llm_client.chat_completion = puppet.chat_completion

        events = list(agent.process_query("Do the thing"))
        assert_event_sequence(events, ["turn", "tool_call", "agent_responded"])
        assert_tool_call(events, "Respond")
        assert_respond(events, expected_content="Task complete.", expected_status="final")
