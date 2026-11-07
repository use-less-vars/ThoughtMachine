"""Tests for SessionConfig <-> AgentConfig provider_config plumbing.

Covers the web-path timeout/max_retries plumbing:

- ``SessionConfig.provider_config`` field round-trips through the
  constructor / attribute / ``model_dump`` (persistence-safe).
- ``to_agent_config()`` forwards ``provider_config`` into ``AgentConfig``
  (previously dropped, which is why web-path timeouts never applied).
- Default ``SessionConfig`` yields an empty ``provider_config`` on
  ``AgentConfig`` (no behaviour change for sessions without overrides).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.config.session_config import SessionConfig
from agent.config.models import AgentConfig


class TestSessionConfigProviderConfigField:
    """The new field exists, defaults to {}, and survives serialization."""

    def test_roundtrip_via_constructor(self):
        cfg = SessionConfig(
            provider_id='p1',
            model='m1',
            provider_config={'timeout': 5, 'max_retries': 2},
        )
        assert cfg.provider_config == {'timeout': 5, 'max_retries': 2}

    def test_default_is_empty_dict(self):
        cfg = SessionConfig()
        assert cfg.provider_config == {}

    def test_roundtrip_via_model_dump(self):
        """Field serializes and reconstructs (persistence round-trip)."""
        cfg = SessionConfig(
            mode='custom',
            provider_config={'timeout': 30, 'max_retries': 1},
        )
        data = cfg.model_dump(exclude={'api_key'})
        restored = SessionConfig(**data)
        assert restored.provider_config == {'timeout': 30, 'max_retries': 1}

    def test_attribute_assignment(self):
        """Bridge/config_manager merge path assigns the attribute directly."""
        cfg = SessionConfig()
        cfg.provider_config = {'timeout': 5, 'max_retries': 2}
        assert cfg.provider_config == {'timeout': 5, 'max_retries': 2}
        assert cfg.to_agent_config().provider_config == {'timeout': 5, 'max_retries': 2}


class TestToAgentConfigProviderConfig:
    """to_agent_config() forwards provider_config into AgentConfig."""

    def test_provider_config_forwarded(self):
        cfg = SessionConfig(
            provider_id='p1',
            provider_config={'timeout': 5, 'max_retries': 2},
        )
        acfg = cfg.to_agent_config()
        assert isinstance(acfg, AgentConfig)
        assert acfg.provider_config == {'timeout': 5, 'max_retries': 2}

    def test_default_yields_empty_provider_config(self):
        cfg = SessionConfig()
        acfg = cfg.to_agent_config()
        assert isinstance(acfg, AgentConfig)
        assert acfg.provider_config == {}

    def test_copy_not_reference(self):
        """The dict is copied, so later mutation of the source is not visible."""
        cfg = SessionConfig(provider_config={'timeout': 5})
        acfg = cfg.to_agent_config()
        cfg.provider_config['timeout'] = 999
        assert acfg.provider_config == {'timeout': 5}
