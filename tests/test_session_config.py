"""SessionConfig is the session-level owner of git permissions and worker fields.

The session's canonical git permission lives in ``session_permissions['git']``
(one of the six canonical keys). The removed legacy top-level ``git_read`` /
``git_write`` keys are migrated on load by ``SessionConfig``'s before-validator:
``git_read`` is dropped, and ``git_write == 'write'`` (or the removed
``git_allow_worktree_commits == True`` flag) folds into
``session_permissions['git'] = 'write'``; other ``git_write`` values are
dropped. The ``worker_timeout_seconds`` / ``worker_max_retries`` worker fields
fold into the AgentConfig produced by ``to_agent_config()`` only when set
(None is the fail-closed default), and they are hot-swappable at runtime.
"""

from agent.config.models import AgentConfig, HOT_SWAPPABLE
from agent.config.session_config import SessionConfig
from security.security_gate import get_effective_permissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities


class TestSessionGitGrainFolding:
    def test_git_write_grain_folds_into_session_permissions(self):
        # Legacy git_read is dropped; git_write == 'write' folds into git.
        ac = SessionConfig(git_read='read', git_write='write').to_agent_config()
        assert ac.session_permissions.git == 'write'

    def test_fold_merges_with_existing_session_permissions_dict(self):
        ac = SessionConfig(
            session_permissions={'filesystem': 'write'}, git_write='write'
        ).to_agent_config()
        assert ac.session_permissions.filesystem == 'write'
        assert ac.session_permissions.git == 'write'

    def test_no_legacy_grains_keeps_canonical_git_default(self):
        ac = SessionConfig().to_agent_config()
        assert ac.session_permissions.git == 'read'  # canonical default

    def test_non_write_legacy_git_write_is_dropped(self):
        ac = SessionConfig(git_write='write_on_feature_branch').to_agent_config()
        assert ac.session_permissions.git == 'read'  # default retained

    def test_git_allow_worktree_commits_true_folds_equally(self):
        ac = SessionConfig(git_allow_worktree_commits=True).to_agent_config()
        assert ac.session_permissions.git == 'write'

    def test_effective_permissions_use_canonical_git_permission(self):
        ac = SessionConfig(git_read='read', git_write='write').to_agent_config()
        eff = get_effective_permissions(ac.session_permissions, WorkspaceCapabilities())
        assert eff['git'] == 'write'


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


class TestLegacyAllowHostResourcesDropped:
    """The legacy SessionConfig allow_host_resources key is dropped on load."""

    def test_model_validate_accepts_and_drops_legacy_key(self):
        cfg = SessionConfig.model_validate({"allow_host_resources": True})
        assert "allow_host_resources" not in cfg.model_dump()

    def test_dump_roundtrip_omits_legacy_key(self):
        cfg = SessionConfig.model_validate(
            {"allow_host_resources": False, "git_write": "write"}
        )
        dumped = cfg.model_dump()
        assert "allow_host_resources" not in dumped
        assert "git_write" not in dumped
        assert dumped["session_permissions"]["git"] == "write"

    def test_attribute_absent(self):
        cfg = SessionConfig.model_validate({"allow_host_resources": True})
        assert not hasattr(cfg, "allow_host_resources")

    def test_agent_config_from_legacy_session_omits_key(self):
        cfg = SessionConfig.model_validate({"allow_host_resources": True})
        ac = cfg.to_agent_config()
        assert "allow_host_resources" not in ac.model_dump()

