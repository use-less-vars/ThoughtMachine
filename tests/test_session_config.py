"""SessionConfig is the session-level owner of git grains and worker fields.

The session owns the ``git_read`` / ``git_write`` permission grains and the
``worker_timeout_seconds`` / ``worker_max_retries`` worker fields. They fold
into the AgentConfig produced by ``to_agent_config()`` only when set (None is
the fail-closed default), and they are hot-swappable at runtime.
"""

from agent.config.models import AgentConfig, HOT_SWAPPABLE
from agent.config.session_config import SessionConfig
from security.security_gate import get_effective_permissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities


class TestSessionGitGrainFolding:
    def test_grains_fold_into_session_permissions(self):
        ac = SessionConfig(git_read='read', git_write='write').to_agent_config()
        assert ac.session_permissions.git_read == 'read'
        assert ac.session_permissions.git_write == 'write'

    def test_grains_merge_with_existing_permissions_dict(self):
        ac = SessionConfig(
            session_permissions={'filesystem': 'write'}, git_write='write'
        ).to_agent_config()
        assert ac.session_permissions.filesystem == 'write'
        assert ac.session_permissions.git_write == 'write'

    def test_none_grains_not_present(self):
        ac = SessionConfig().to_agent_config()
        assert ac.session_permissions.git_read is None
        assert ac.session_permissions.git_write is None

    def test_effective_permissions_use_session_grain(self):
        ac = SessionConfig(git_read='read', git_write='write').to_agent_config()
        eff = get_effective_permissions(ac.session_permissions, WorkspaceCapabilities())
        assert eff['git_write'] == 'write'


class TestWorkerFieldsSessionOwned:
    def test_worker_fields_fold_only_when_set(self):
        ac = SessionConfig(
            worker_timeout_seconds=123, worker_max_retries=5
        ).to_agent_config()
        assert ac.worker_timeout_seconds == 123
        assert ac.worker_max_retries == 5

    def test_worker_fields_absent_stay_none(self):
        ac = SessionConfig().to_agent_config()
        assert ac.worker_timeout_seconds is None
        assert ac.worker_max_retries is None

    def test_agent_config_defaults_are_none(self):
        ac = AgentConfig()
        assert ac.worker_timeout_seconds is None
        assert ac.worker_max_retries is None

    def test_worker_fields_are_hot_swappable_categories(self):
        assert AgentConfig.FIELD_CATEGORIES['worker_timeout_seconds'] == HOT_SWAPPABLE
        assert AgentConfig.FIELD_CATEGORIES['worker_max_retries'] == HOT_SWAPPABLE
        assert AgentConfig.FIELD_CATEGORIES['session_permissions'] == HOT_SWAPPABLE
