"""Integration tests for credential placeholder resolution in Agent LLM client creation.

Tests that ``{{credential:key}}`` placeholders in ``config.api_key`` are
resolved via ``CredentialInjector`` **before** the ``LLMClient`` is created
(at all three construction sites: __init__, restart success, restart rollback).
"""

import pytest
from unittest.mock import patch, MagicMock

from agent.credentials import CredentialError
from agent.config import AgentConfig
from session.models import Session


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_mock_llm():
    """Return a mock LLMClient instance that supports Agent.__init__ flow."""
    instance = MagicMock()
    # ensure_system_prompt must act as identity so conversation stays a list
    instance.ensure_system_prompt.side_effect = lambda conv: conv
    instance.provider = "mock"
    instance.close.return_value = None
    return instance


def _make_agent(config, session=None, session_id=None, **patches):
    """Construct an Agent with LLMClient and CredentialInjector patched.

    *patches* are additional ``patch`` context managers to apply.
    Returns the Agent instance.
    """
    with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
        with patch("agent.credentials.CredentialInjector") as mock_injector_cls:
            # Apply any extra patches (context managers)
            ctx_managers = list(patches.values())
            if ctx_managers:
                with ctx_managers[0] as first_ctx:
                    # Only one extra context manager supported for simplicity
                    from agent.core.agent import Agent
                    agent = Agent(config=config, session=session, session_id=session_id)
                    return agent, mock_llm, mock_injector_cls, first_ctx
            from agent.core.agent import Agent
            agent = Agent(config=config, session=session, session_id=session_id)
            return agent, mock_llm, mock_injector_cls


def _restart_agent(agent, new_config):
    """Call agent.restart with LLMClient patched."""
    with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
        with patch("agent.credentials.CredentialInjector") as mock_injector_cls:
            ok = agent.restart(new_config)
            return ok, mock_llm, mock_injector_cls


# ── Tests ────────────────────────────────────────────────────────────────────

class TestCredentialProviderInjection:
    """Verify that {{credential:key}} is resolved before LLMClient creation."""

    def test_placeholder_resolved_before_llm_created(self, hermetic_vault):
        """When api_key has a placeholder, the resolved Secret is passed to LLMClient."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="{{credential:provider_key}}")

        mock_injector_instance = MagicMock()
        mock_injector_instance.resolve.return_value = "resolved-secret-123"

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                # The config passed to LLMClient should have the resolved key
                call_config = mock_llm.call_args[0][0]
                assert call_config.api_key == "resolved-secret-123"

                # The injector was called with the correct key name
                mock_injector_instance.resolve.assert_called_once_with("provider_key")

    def test_missing_workspace_raises_error(self, hermetic_vault):
        """Placeholder with no workspace_id raises CredentialError."""
        session = Session(session_id="test-session", workspace_id=None)
        config = AgentConfig(api_key="{{credential:provider_key}}")

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()):
            from agent.core.agent import Agent
            with pytest.raises(CredentialError, match="no workspace associated with session"):
                Agent(config=config, session=session)

    def test_no_session_raises_error(self, hermetic_vault):
        """Placeholder with no session at all raises CredentialError."""
        config = AgentConfig(api_key="{{credential:provider_key}}")

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()):
            from agent.core.agent import Agent
            with pytest.raises(CredentialError, match="no workspace associated with session"):
                Agent(config=config, session=None, session_id="orphan-session")

    def test_missing_credential_file_raises_error(self, hermetic_vault):
        """Placeholder pointing to a nonexistent credential raises CredentialError."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="{{credential:nonexistent_key}}")

        mock_injector_instance = MagicMock()
        mock_injector_instance.resolve.side_effect = CredentialError(
            "Credential 'nonexistent_key' not found"
        )

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()):
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                with pytest.raises(CredentialError, match="nonexistent_key"):
                    Agent(config=config, session=session)

    def test_direct_api_key_passes_through_unchanged(self, hermetic_vault):
        """api_key without a placeholder is passed through as-is."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="sk-real-key-abc")

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector"):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                call_config = mock_llm.call_args[0][0]
                assert call_config.api_key == "sk-real-key-abc"

    def test_empty_api_key_passes_through(self, hermetic_vault):
        """Empty (default) api_key is not treated as a placeholder."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig()  # api_key defaults to ""

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector"):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                call_config = mock_llm.call_args[0][0]
                assert call_config.api_key == ""

    def test_malformed_placeholder_raises_error(self, hermetic_vault):
        """A {{credential:...}} pattern that doesn't parse raises CredentialError."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="{{credential:}}")  # empty key name

        mock_injector_instance = MagicMock()

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()):
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                with pytest.raises(CredentialError, match="credential"):
                    Agent(config=config, session=session)

    def test_restart_resolves_new_placeholder(self, hermetic_vault):
        """restart() resolves the placeholder from the *new* config before LLMClient."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="sk-initial-key")

        resolve_calls = []

        def counting_resolve(key):
            resolve_calls.append(key)
            return f"resolved-{len(resolve_calls)}"

        mock_injector_instance = MagicMock()
        mock_injector_instance.resolve.side_effect = counting_resolve

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                # __init__ shouldn't have called resolve (no placeholder)
                assert len(resolve_calls) == 0
                assert mock_llm.call_args[0][0].api_key == "sk-initial-key"

                # Now restart with a placeholder in the new config
                new_config = AgentConfig(api_key="{{credential:restart_key}}")
                ok = agent.restart(new_config)

                assert ok is True
                # Should have called resolve once for restart_key
                assert resolve_calls == ["restart_key"]
                # LLMClient should have received the resolved value
                assert mock_llm.call_args[0][0].api_key == "resolved-1"

    def test_restart_rollback_restores_old_resolved_value(self, hermetic_vault):
        """If restart fails, the rollback path re-uses the already-resolved old config."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="sk-original")

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector"):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                # Verify the original LLMClient received the correct key
                assert mock_llm.call_args[0][0].api_key == "sk-original"

                new_config = AgentConfig(api_key="new-unused-key")

                # Force restart to fail by making create_context_builder() raise
                # after LLMClient is created but before restart completes.
                # This triggers the rollback path which restores old_config.
                with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_restart_llm:
                    mock_restart_llm.return_value.create_context_builder.side_effect =                         RuntimeError("simulated restart failure")

                    ok = agent.restart(new_config)
                    assert ok is False

                    # Verify the agent's config was restored to old_config
                    assert agent.config.api_key == "sk-original"

    def test_restart_rollback_no_placeholder_re_resolve(self, hermetic_vault):
        """Rollback with non-placeholder old_config should not call resolve again."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="sk-original")

        resolve_count = 0

        def tracking_resolve(key):
            nonlocal resolve_count
            resolve_count += 1
            return f"resolved-{resolve_count}"

        mock_injector_instance = MagicMock()
        mock_injector_instance.resolve.side_effect = tracking_resolve

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()):
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                assert resolve_count == 0  # no placeholder in original

                # Force restart to fail by making new_config's resolution fail
                new_config = AgentConfig(api_key="{{credential:will_fail_key}}")

                mock_injector_instance_fail = MagicMock()
                mock_injector_instance_fail.resolve.side_effect = CredentialError("File missing")

                with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance_fail):
                    with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()):
                        ok = agent.restart(new_config)
                        assert ok is False

                # The rollback path calls _resolve_credential_placeholder(old_config)
                # Since old_config.api_key is "sk-original" (no placeholder), it should
                # return immediately without calling resolve
                assert resolve_count == 0

    def test_placeholder_with_whitespace(self, hermetic_vault):
        """Placeholder key with whitespace is stripped before resolution."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="{{credential:  spaced_key  }}")

        mock_injector_instance = MagicMock()
        mock_injector_instance.resolve.return_value = "stripped-value"

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                call_config = mock_llm.call_args[0][0]
                assert call_config.api_key == "stripped-value"
                mock_injector_instance.resolve.assert_called_once_with("spaced_key")

    def test_secret_redaction_preserved(self, hermetic_vault):
        """The resolved Secret should remain redacted in repr/str."""
        session = Session(session_id="test-session", workspace_id="test-ws")
        config = AgentConfig(api_key="{{credential:sensitive_key}}")

        mock_injector_instance = MagicMock()
        from agent.credentials import Secret
        mock_injector_instance.resolve.return_value = Secret("super-secret-value")

        with patch("agent.core.agent.LLMClient", return_value=_make_mock_llm()) as mock_llm:
            with patch("agent.credentials.CredentialInjector", return_value=mock_injector_instance):
                from agent.core.agent import Agent
                agent = Agent(config=config, session=session)

                resolved_key = mock_llm.call_args[0][0].api_key
                assert resolved_key == "super-secret-value"  # equality works
                assert repr(resolved_key) == "***"  # redacted
                assert str(resolved_key) == "***"  # redacted
