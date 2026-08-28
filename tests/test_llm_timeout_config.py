"""Tests for LLM timeout / max_retries configuration plumbing.

Covers:
- LLMClient resolving timeout/max_retries: env var (LLM_TIMEOUT /
  LLM_MAX_RETRIES) > provider_config > default (120 / 3), with negative or
  unparsable values falling back to the default.
- ProviderFactory passing timeout/max_retries through into ProviderConfig.
- ProviderTimeoutError classification when openai / anthropic raise their
  SDK APITimeoutError (provider-level, no network involved).
- LLMClient mapping ProviderTimeoutError -> LLMError(error_type='timeout').
"""

from types import SimpleNamespace

import pytest

from agent.core.llm_client import LLMClient, LLMError
from llm_providers.base import ProviderConfig
from llm_providers.exceptions import ProviderTimeoutError
from llm_providers.factory import ProviderFactory


def _make_config(provider_config=None):
    """Minimal AgentConfig stub carrying only the attrs LLMClient touches."""
    return SimpleNamespace(
        provider_type='openai_compatible',
        api_key='test-key',
        base_url=None,
        model='test-model',
        temperature=0.2,
        provider_config=provider_config or {},
    )


def _capture_create_provider(monkeypatch):
    """Replace ProviderFactory.create_provider with a kwargs recorder.

    Returns the recorder dict; each call stores {'provider_type': ...,
    'api_key': ..., **extra_kwargs} under the key 'call'.
    """
    captured = {}

    def fake_create(provider_type, api_key=None, **kwargs):
        captured['call'] = dict(provider_type=provider_type, api_key=api_key, **kwargs)
        return object()

    monkeypatch.setattr(ProviderFactory, 'create_provider', staticmethod(fake_create))
    return captured


# ---------------------------------------------------------------------------
# LLMClient plumbing: env var > provider_config > default
# ---------------------------------------------------------------------------

def test_defaults_without_config_or_env(monkeypatch):
    monkeypatch.delenv('LLM_TIMEOUT', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({}))
    assert captured['call']['timeout'] == 120
    assert captured['call']['max_retries'] == 3


def test_provider_config_overrides_defaults(monkeypatch):
    monkeypatch.delenv('LLM_TIMEOUT', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': 45, 'max_retries': 7}))
    assert captured['call']['timeout'] == 45
    assert captured['call']['max_retries'] == 7


def test_env_overrides_provider_config(monkeypatch):
    monkeypatch.setenv('LLM_TIMEOUT', '99')
    monkeypatch.setenv('LLM_MAX_RETRIES', '5')
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': 45, 'max_retries': 7}))
    assert captured['call']['timeout'] == 99
    assert captured['call']['max_retries'] == 5


def test_negative_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.delenv('LLM_TIMEOUT', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': -5, 'max_retries': -1}))
    assert captured['call']['timeout'] == 120
    assert captured['call']['max_retries'] == 3


def test_unparsable_config_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.delenv('LLM_TIMEOUT', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': 'abc', 'max_retries': 'xyz'}))
    assert captured['call']['timeout'] == 120
    assert captured['call']['max_retries'] == 3


def test_unparsable_env_falls_back_to_default(monkeypatch):
    # An unparsable env var still WINS over provider_config (env takes
    # precedence as a whole); the unparsable value collapses to the default.
    monkeypatch.setenv('LLM_TIMEOUT', 'abc')
    monkeypatch.setenv('LLM_MAX_RETRIES', 'nope')
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': 30, 'max_retries': 2}))
    assert captured['call']['timeout'] == 120
    assert captured['call']['max_retries'] == 3


def test_empty_env_treated_as_unset(monkeypatch):
    monkeypatch.setenv('LLM_TIMEOUT', '')
    monkeypatch.setenv('LLM_MAX_RETRIES', '   ')
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': 30, 'max_retries': 2}))
    assert captured['call']['timeout'] == 30
    assert captured['call']['max_retries'] == 2


def test_zero_is_accepted_as_valid(monkeypatch):
    monkeypatch.delenv('LLM_TIMEOUT', raising=False)
    monkeypatch.delenv('LLM_MAX_RETRIES', raising=False)
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_make_config({'timeout': 0, 'max_retries': 0}))
    assert captured['call']['timeout'] == 0
    assert captured['call']['max_retries'] == 0


# ---------------------------------------------------------------------------
# ProviderFactory: timeout/max_retries land in ProviderConfig
# ---------------------------------------------------------------------------

def test_factory_passes_timeout_and_max_retries_into_provider_config(monkeypatch):
    class DummyProvider:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(ProviderFactory, '_providers', {'dummy': DummyProvider})

    provider = ProviderFactory.create_provider('dummy', api_key='k', timeout=30, max_retries=5)

    assert provider.config.timeout == 30
    assert provider.config.max_retries == 5


# ---------------------------------------------------------------------------
# Provider classification: SDK APITimeoutError -> ProviderTimeoutError
# ---------------------------------------------------------------------------

def test_openai_apitimeout_error_maps_to_provider_timeout():
    openai = pytest.importorskip('openai')
    from llm_providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(ProviderConfig(api_key='k', timeout=1))

    class FakeCompletions:
        def create(self, **kwargs):
            raise openai.APITimeoutError(request=SimpleNamespace())

    class FakeClient:
        base_url = 'https://api.openai.com/v1'

        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    provider.client = FakeClient()

    with pytest.raises(ProviderTimeoutError):
        provider.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])


def test_anthropic_apitimeout_error_maps_to_provider_timeout():
    anthropic = pytest.importorskip('anthropic')
    from llm_providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(ProviderConfig(api_key='k', timeout=1))

    class FakeMessages:
        def create(self, **kwargs):
            raise anthropic.APITimeoutError(request=SimpleNamespace())

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    provider.client = FakeClient()

    with pytest.raises(ProviderTimeoutError):
        provider.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])


# ---------------------------------------------------------------------------
# LLMClient mapping: ProviderTimeoutError -> LLMError('timeout')
# ---------------------------------------------------------------------------

def test_llm_client_maps_provider_timeout_to_timeout_error(monkeypatch):
    class FakeProvider:
        def chat_completion(self, messages, tools=None, **kwargs):
            raise ProviderTimeoutError('Request timed out: fake')

    monkeypatch.setattr(
        ProviderFactory,
        'create_provider',
        staticmethod(lambda **kwargs: FakeProvider()),
    )

    client = LLMClient(_make_config({}))
    with pytest.raises(LLMError) as excinfo:
        client.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])

    assert excinfo.value.error_type == 'timeout'
