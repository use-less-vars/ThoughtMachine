"""Provider profiles are the global-only owner of provider timeout/retries.

The profile's ``timeout`` / ``max_retries`` may ONLY reach the LLM client
via ``provider_config``. Neither AgentConfig nor SessionConfig exposes
top-level provider timeout/retry fields, and ``resolve_from_profile``
copies only provider identity fields.
"""

from pathlib import Path

from agent.config.models import AgentConfig
from agent.config.provider_profile import ProviderManager, ProviderProfile
from agent.config.session_config import SessionConfig


def _manager_with_profile(timeout=45, max_retries=2) -> ProviderManager:
    mgr = ProviderManager(file_path=Path('/nonexistent/providers.json'))
    mgr.add_profile(
        ProviderProfile(
            id='p1',
            label='Profile 1',
            provider_type='openai_compatible',
            base_url='https://llm.example.com',
            api_key='secret-key',
            default_model='model-x',
            timeout=timeout,
            max_retries=max_retries,
        )
    )
    return mgr


class TestProviderTimeoutNotCopiedToSession:
    def test_resolve_config_keeps_timeout_in_provider_config_only(self):
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        result = mgr.resolve_config({'provider_id': 'p1', 'provider_config': {}})
        assert result['provider_config']['timeout'] == 45
        # Never a top-level session/agent field.
        assert 'timeout' not in result

    def test_agent_and_session_configs_have_no_provider_timeout_field(self):
        assert 'timeout' not in AgentConfig.model_fields
        assert 'timeout' not in SessionConfig.model_fields

    def test_agent_config_soft_budget_is_distinct(self):
        # timeout_seconds is the agent soft budget, NOT the provider timeout.
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        cfg = AgentConfig(provider_id='p1', timeout_seconds=300)
        resolved = cfg.resolve_from_profile(mgr)
        assert resolved.provider_config == {}  # profile timeout NOT folded in
        assert resolved.timeout_seconds == 300

    def test_session_config_carries_provider_config_dict_only(self):
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        result = mgr.resolve_config({'provider_id': 'p1', 'provider_config': {}})
        sc = SessionConfig(provider_config=dict(result.get('provider_config', {})))
        assert sc.provider_config['timeout'] == 45
        assert not hasattr(sc, 'timeout')  # provider timeout is not a session field

    def test_full_config_dict_builds_agent_config_with_provider_config_only(self):
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        result = mgr.resolve_config({'provider_id': 'p1', 'provider_config': {}})
        ac = AgentConfig(**result)
        assert ac.provider_config == {'timeout': 45, 'max_retries': 2}
        assert ac.timeout_seconds == 300  # soft budget untouched


class TestProviderMaxRetriesNotCopiedToSession:
    def test_resolve_config_keeps_max_retries_in_provider_config_only(self):
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        result = mgr.resolve_config({'provider_id': 'p1', 'provider_config': {}})
        assert result['provider_config']['max_retries'] == 2
        # Never a top-level session/agent field.
        assert 'max_retries' not in result

    def test_agent_and_session_configs_have_no_provider_max_retries_field(self):
        assert 'max_retries' not in AgentConfig.model_fields
        assert 'max_retries' not in SessionConfig.model_fields

    def test_agent_config_worker_retries_are_distinct(self):
        # worker_max_retries is a session/worker concern, not the LLM retry.
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        cfg = AgentConfig(provider_id='p1', worker_max_retries=5)
        resolved = cfg.resolve_from_profile(mgr)
        assert resolved.provider_config == {}
        assert resolved.worker_max_retries == 5

    def test_resolve_from_profile_copies_only_identity_fields(self):
        mgr = _manager_with_profile(timeout=45, max_retries=2)
        cfg = AgentConfig(provider_id='p1')
        resolved = cfg.resolve_from_profile(mgr)
        assert resolved.provider_type == 'openai_compatible'
        assert resolved.base_url == 'https://llm.example.com'
        assert resolved.api_key == 'secret-key'
        assert resolved.model == 'model-x'
        assert resolved.provider_config == {}  # timeout/max_retries NOT copied


def test_provider_timeout_not_copied_to_session():
    """Contract wrapper: provider timeout stays in provider_config only."""
    tc = TestProviderTimeoutNotCopiedToSession()
    tc.test_resolve_config_keeps_timeout_in_provider_config_only()
    tc.test_agent_and_session_configs_have_no_provider_timeout_field()
    tc.test_agent_config_soft_budget_is_distinct()
    tc.test_session_config_carries_provider_config_dict_only()
    tc.test_full_config_dict_builds_agent_config_with_provider_config_only()


def test_provider_max_retries_not_copied_to_session():
    """Contract wrapper: provider max_retries stays in provider_config only."""
    tc = TestProviderMaxRetriesNotCopiedToSession()
    tc.test_resolve_config_keeps_max_retries_in_provider_config_only()
    tc.test_agent_and_session_configs_have_no_provider_max_retries_field()
    tc.test_agent_config_worker_retries_are_distinct()
    tc.test_resolve_from_profile_copies_only_identity_fields()

