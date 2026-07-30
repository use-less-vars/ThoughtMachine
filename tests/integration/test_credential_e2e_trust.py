"""End-to-end credential trust tests for Pillar 2.3.

Exercises the full credential flow: vault -> session config -> agent -> provider,
with the security gate still enforcing permissions.

Uses hermetic_vault, real credential files (not mocked), PuppetLLM for the
LLM loop, and mock LLMClient to capture the resolved api_key.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from session.models import Session
from agent.config import AgentConfig
from llm_providers.base import LLMResponse
from tests.mocks.puppet_agent import PuppetLLM
from tests.assertions.event_assertions import (
    assert_event_sequence,
    assert_tool_call,
    assert_respond,
)
from thoughtmachine.security import SessionPermissions
from agent.credentials import Secret


class TestCredentialE2ETrust:
    """End-to-end credential trust with real vault files and security gate."""

    def _write_credential(self, vault_path, workspace_id, key, value):
        """Write a real credential file to the hermetic vault."""
        cred_dir = vault_path / "credentials" / workspace_id
        cred_dir.mkdir(parents=True, exist_ok=True)
        (cred_dir / key).write_text(value)

    def test_full_credential_flow_with_gate_enforcement(self, hermetic_vault):
        """End-to-end: credential flows from vault to provider. Secret never reaches
        LLM prompt. Security gate still enforced."""
        # === Setup ===
        vault_path = hermetic_vault  # tmp_path / .thoughtmachine
        workspace_id = "test-ws-001"
        credential_key = "provider_api_key"
        real_secret = "sk-test-secret-for-e2e-123456"

        # Write credential file to vault — real file, not mocked
        self._write_credential(vault_path, workspace_id, credential_key, real_secret)

        # Create a real Session with workspace_id
        session = Session(session_id="e2e-session-001", workspace_id=workspace_id)

        # AgentConfig with credential placeholder
        config = AgentConfig(
            api_key="{{credential:provider_api_key}}",
            provider_type="openai_compatible",
            model="test-model",
            enable_logging=False,
        )

        # === Agent with real credential resolution ===
        with patch("agent.core.agent.LLMClient") as mock_llm_cls:
            mock_llm_instance = MagicMock()
            mock_llm_instance.ensure_system_prompt.side_effect = lambda x: x
            mock_llm_instance.close.return_value = None
            mock_llm_instance.format_tools.return_value = []
            mock_llm_cls.return_value = mock_llm_instance

            # Create agent — triggers real CredentialInjector.resolve()
            from agent.core.agent import Agent
            agent = Agent(config=config, session=session)

            # Capture what was passed to LLMClient constructor
            captured_config = mock_llm_cls.call_args[0][0]
            captured_api_key = captured_config.api_key

            # === Puppet-driven conversation with tool calls ===
            puppet = PuppetLLM(scenario=[
                {"type": "tool_call", "tool_name": "ReadFile", "arguments": {"file_path": "/tmp/test.txt"}},
                {"type": "respond", "content": "All done", "status": "final"},
            ])
            agent.llm_client.chat_completion = puppet.chat_completion
            agent.llm_client.format_tools = lambda x: []

            # Run the agent loop
            events = list(agent.process_query("Read /tmp/test.txt"))

        # === Assertions ===
        # 1. Provider received the real secret (resolved from vault, not placeholder)
        assert captured_api_key == real_secret, (
            f"Provider received wrong API key: {captured_api_key!r}"
        )
        assert isinstance(captured_api_key, Secret), (
            "API key should be Secret for redaction"
        )

        # 2. Secret never appears in any event string representation
        events_str = json.dumps(events, default=str)
        assert real_secret not in events_str, (
            f"SECRET LEAKED INTO EVENTS! Found in: {events_str[:500]}"
        )

        # 3. Events contain expected structure (turn, tool_call, agent_responded)
        assert_event_sequence(events, ["turn", "tool_call", "tool_result", "agent_responded"])
        assert_tool_call(events, "ReadFile")
        assert_respond(events, expected_status="final")

    def test_security_gate_denies_write_with_readonly_permissions(self, hermetic_vault):
        """Session with filesystem:read only. Tool requiring filesystem:write
        must be denied by the security gate."""
        # === Setup ===
        vault_path = hermetic_vault
        workspace_id = "test-ws-002"
        credential_key = "api_key"
        real_secret = "sk-test-secret-002"

        self._write_credential(vault_path, workspace_id, credential_key, real_secret)

        session = Session(session_id="e2e-session-002", workspace_id=workspace_id)

        # Config with session_permissions = filesystem:read only
        config = AgentConfig(
            api_key="{{credential:api_key}}",
            provider_type="openai_compatible",
            model="test-model",
            enable_logging=False,
            session_permissions=SessionPermissions(filesystem="read"),
        )

        # === Agent with real credential + restricted permissions ===
        with patch("agent.core.agent.LLMClient") as mock_llm_cls:
            mock_llm_instance = MagicMock()
            mock_llm_instance.ensure_system_prompt.side_effect = lambda x: x
            mock_llm_instance.close.return_value = None
            mock_llm_instance.format_tools.return_value = []
            mock_llm_cls.return_value = mock_llm_instance

            from agent.core.agent import Agent
            agent = Agent(config=config, session=session)

            captured_config = mock_llm_cls.call_args[0][0]
            captured_api_key = captured_config.api_key

            # Puppet scenario: ApplyEdits requires filesystem:write -> should be denied
            puppet = PuppetLLM(scenario=[
                {
                    "type": "tool_call",
                    "tool_name": "ApplyEdits",
                    "arguments": {"file_path": "/tmp/test.py", "edits": [{"find": "a", "replace": "b"}]},
                },
                {"type": "respond", "content": "Write attempt finished", "status": "final"},
            ])
            agent.llm_client.chat_completion = puppet.chat_completion
            agent.llm_client.format_tools = lambda x: []

            events = list(agent.process_query("Write to a file"))

        # === Assertions ===
        # 1. Credential was resolved correctly
        assert captured_api_key == real_secret

        # 2. Gate denied the write tool
        events_str = json.dumps(events, default=str).lower()
        denied_indicators = ["denied", "permission", "not allowed", "rejected"]
        found_denial = any(indicator in events_str for indicator in denied_indicators)
        assert found_denial, (
            f"Security gate should have denied the write tool. Events: {events_str[:800]}"
        )

        # 3. Secret never leaks
        assert real_secret not in events_str, "SECRET LEAKED!"

    def test_secret_never_in_llm_prompt(self, hermetic_vault):
        """The prompt sent to the LLM must never contain the resolved secret."""
        # === Setup ===
        vault_path = hermetic_vault
        workspace_id = "test-ws-003"
        credential_key = "openai_key"
        real_secret = "sk-test-secret-003"

        self._write_credential(vault_path, workspace_id, credential_key, real_secret)

        session = Session(session_id="e2e-session-003", workspace_id=workspace_id)

        config = AgentConfig(
            api_key="{{credential:openai_key}}",
            provider_type="openai_compatible",
            model="test-model",
            enable_logging=False,
        )

        captured_prompts = []

        def capturing_chat_completion(messages, tools=None, **kwargs):
            """Capture messages sent to the LLM for leak detection."""
            captured_prompts.append(list(messages))
            return LLMResponse(content="Test response", usage={"prompt_tokens": 10, "completion_tokens": 5})

        # === Agent with real credential ===
        with patch("agent.core.agent.LLMClient") as mock_llm_cls:
            mock_llm_instance = MagicMock()
            mock_llm_instance.ensure_system_prompt.side_effect = lambda x: x
            mock_llm_instance.close.return_value = None
            mock_llm_instance.format_tools.return_value = []
            mock_llm_instance.chat_completion = capturing_chat_completion
            mock_llm_cls.return_value = mock_llm_instance

            from agent.core.agent import Agent
            agent = Agent(config=config, session=session)

            # The mock already has chat_completion = capturing_chat_completion
            events = list(agent.process_query("Hello"))

        # === Assertions ===
        # No prompt should contain the real secret
        for i, prompt in enumerate(captured_prompts):
            prompt_str = json.dumps(prompt, default=str)
            assert real_secret not in prompt_str, (
                f"SECRET LEAKED INTO PROMPT #{i}: {prompt_str[:300]}"
            )

        # The agent should have completed without errors
        assert len(captured_prompts) > 0, "No prompts were sent to the LLM"
