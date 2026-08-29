"""Mid-session provider_config changes must take effect for the running agent.

A ``provider_config`` change (e.g. timeout 1 -> 120, or max_retries) only takes
effect when the LLM provider is re-initialised: LLMClient reads
``config.provider_config`` once in ``__init__`` and passes timeout/max_retries
to ``ProviderFactory.create_provider()``.  ``AgentConfig.FIELD_CATEGORIES``
already marks ``provider_config`` as RESTART_REQUIRED, but
``Agent._can_hot_swap`` did not check it — so the change was silently
hot-swapped (LLM provider untouched) and the running agent kept the OLD
timeout/max_retries.

This suite verifies:
- ``_can_hot_swap`` returns False when provider_config differs (timeout 1 -> 120).
- ``_can_hot_swap`` returns True when provider_config is unchanged.
- ``_apply_pending_config`` takes the restart path for a provider_config change
  and the rebuilt LLM client carries the new provider_config, while the
  conversation history is preserved.
- A pure hot-swap (temperature) still works and does NOT trigger a restart.
"""

from agent.config.models import AgentConfig
from agent.core.agent import Agent


def _make_config(**overrides):
    """Minimal AgentConfig for hot-swap tests (no logging, test API key)."""
    base = dict(
        api_key='test-key',
        enable_logging=False,
        provider_config={},
    )
    base.update(overrides)
    return AgentConfig(**base)


class TestCanHotSwapProviderConfig:
    """_can_hot_swap: provider_config changes must be classified as restart."""

    def test_false_when_provider_config_differs(self):
        agent = Agent(config=_make_config(), session_id='test-session')
        new = _make_config(provider_config={'timeout': 120})
        assert agent._can_hot_swap(new) is False

    def test_false_when_provider_config_timeout_changes(self):
        """Regression: the operator scenario (timeout 1 -> 120)."""
        agent = Agent(
            config=_make_config(provider_config={'timeout': 1}),
            session_id='test-session',
        )
        new = _make_config(provider_config={'timeout': 120})
        assert agent._can_hot_swap(new) is False

    def test_true_when_provider_config_equal_empty(self):
        agent = Agent(config=_make_config(), session_id='test-session')
        new = _make_config(provider_config={})
        assert agent._can_hot_swap(new) is True

    def test_true_when_provider_config_equal_non_empty(self):
        cfg = {'timeout': 120, 'max_retries': 5}
        agent = Agent(
            config=_make_config(provider_config=dict(cfg)),
            session_id='test-session',
        )
        new = _make_config(provider_config=dict(cfg))
        assert agent._can_hot_swap(new) is True


class TestApplyPendingConfigProviderConfig:
    """_apply_pending_config: provider_config change must use the restart path."""

    def test_provider_config_change_triggers_restart(self, monkeypatch):
        """A provider_config-only change must go through restart(), not hot-swap."""
        agent = Agent(
            config=_make_config(provider_config={'timeout': 1}),
            session_id='test-session',
        )

        restarts = []
        original_restart = agent.restart

        def spy_restart(new_config):
            restarts.append(new_config)
            return original_restart(new_config)

        monkeypatch.setattr(agent, 'restart', spy_restart)

        new = _make_config(provider_config={'timeout': 120})
        agent.request_config_update(new)
        result = agent._apply_pending_config()

        assert result is True
        assert len(restarts) == 1
        assert restarts[0] is new
        assert agent._pending_config is None
        # The rebuilt LLM client must carry the new provider_config.
        assert agent.llm_client.config.provider_config == {'timeout': 120}

    def test_restart_preserves_conversation_and_applies_new_provider_config(self):
        """End-to-end: conversation survives restart; LLM client re-initialised."""
        agent = Agent(
            config=_make_config(provider_config={'timeout': 1}),
            session_id='test-session',
        )
        old_llm_client = agent.llm_client
        agent.conversation.append({'role': 'user', 'content': 'hello'})
        assert len(agent.conversation) == 2  # system prompt + user message

        new = _make_config(provider_config={'timeout': 120})
        agent.request_config_update(new)
        result = agent._apply_pending_config()

        assert result is True
        # LLM client was re-initialised with the new config.
        assert agent.llm_client is not old_llm_client
        assert agent.llm_client.config.provider_config == {'timeout': 120}
        assert agent.config.provider_config == {'timeout': 120}
        # Conversation history preserved.
        user_contents = [
            m.get('content')
            for m in agent.conversation
            if m.get('role') == 'user'
        ]
        assert 'hello' in user_contents

    def test_hot_swap_still_works_when_only_temperature_changes(self, monkeypatch):
        """provider_config equal: pure runtime change stays on the hot-swap path."""
        agent = Agent(config=_make_config(temperature=0.2), session_id='test-session')

        restarts = []
        monkeypatch.setattr(
            agent,
            'restart',
            lambda new_config: restarts.append(new_config) or True,
        )

        new = _make_config(temperature=0.9)
        agent.request_config_update(new)
        result = agent._apply_pending_config()

        assert result is True
        assert restarts == []  # no restart for a hot-swappable change
        assert agent.runtime_params.temperature == 0.9
        assert agent.config.provider_config == {}
