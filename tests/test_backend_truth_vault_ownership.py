"""Backend truth & vault ownership: config-migration and documentation guards.

Covers the Phase 1+2 contract:

- ``git_allow_worktree_commits`` (removed operator flag) migrates to the
  ``git_write`` session permission and never re-appears as a model field.
- ``docs/param_ownership_map.md`` exists and pins the canonical parameter
  owners (session config, provider profile, worker definition).
"""

from pathlib import Path

from agent.config.models import AgentConfig
from agent.config.session_config import SessionConfig
from thoughtmachine.security import SessionPermissions

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestGitAllowWorktreeCommitsMigration:
    """The removed operator flag folds into session_permissions.git_write."""

    def test_agent_config_true_folds_to_git_write(self):
        cfg = AgentConfig(git_allow_worktree_commits=True)
        assert cfg.session_permissions.git_write == 'write'

    def test_agent_config_false_stays_fail_closed(self):
        cfg = AgentConfig(git_allow_worktree_commits=False)
        assert cfg.session_permissions.git_write is None
        assert cfg.session_permissions.git == 'read'

    def test_agent_config_absent_stays_fail_closed(self):
        cfg = AgentConfig()
        assert cfg.session_permissions.git_write is None

    def test_legacy_flag_is_not_a_field_anymore(self):
        assert 'git_allow_worktree_commits' not in AgentConfig.model_fields
        assert 'git_allow_worktree_commits' not in SessionConfig.model_fields

    def test_agent_config_session_permissions_instance_path(self):
        # The legacy flag must also fold when session_permissions is already
        # a SessionPermissions instance (round-trips through to_dict()).
        cfg = AgentConfig(
            git_allow_worktree_commits=True,
            session_permissions=SessionPermissions(git_write='read'),
        )
        assert cfg.session_permissions.git_write == 'write'

    def test_session_config_true_folds_to_git_write(self):
        sc = SessionConfig(git_allow_worktree_commits=True)
        assert sc.git_write == 'write'

    def test_session_config_false_stays_fail_closed(self):
        sc = SessionConfig(git_allow_worktree_commits=False)
        assert sc.git_write is None

    def test_session_config_absent_stays_fail_closed(self):
        sc = SessionConfig()
        assert sc.git_write is None

    def test_session_config_migration_reaches_agent_config(self):
        sc = SessionConfig(git_allow_worktree_commits=True)
        ac = sc.to_agent_config()
        assert ac.session_permissions.git_write == 'write'


class TestParamOwnershipMapDocumentation:
    """docs/param_ownership_map.md exists and pins the canonical owners."""

    def _doc_text(self) -> str:
        doc = _REPO_ROOT / 'docs' / 'param_ownership_map.md'
        assert doc.exists(), 'docs/param_ownership_map.md must exist'
        return doc.read_text(encoding='utf-8')

    def test_documentation_exists(self):
        assert (_REPO_ROOT / 'docs' / 'param_ownership_map.md').exists()

    def test_documentation_covers_key_owners(self):
        text = self._doc_text()
        for needle in (
            'session_config',
            'provider_profile',
            'global_defaults',
            'WORKER_TIMEOUT_SECONDS',
            'git_write',
            'worker',
        ):
            assert needle in text, (
                f'docs/param_ownership_map.md must mention {needle!r}'
            )


def test_git_allow_worktree_commits_migration():
    """Contract wrapper: removed flag folds to git_write in both configs."""
    tc = TestGitAllowWorktreeCommitsMigration()
    tc.test_agent_config_true_folds_to_git_write()
    tc.test_session_config_true_folds_to_git_write()


def test_param_ownership_map_documentation_exists():
    """Contract wrapper: docs/param_ownership_map.md exists."""
    TestParamOwnershipMapDocumentation().test_documentation_exists()

