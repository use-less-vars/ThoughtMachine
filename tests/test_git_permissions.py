"""Git permission grains and branch protection.

Canonical 6-key contract:

- The session ``git`` level passes straight through to the effective
  ``git`` grain (SessionPermissions holds a single git key, not split
  read/write grains); the workspace capability (``git_available``) caps it.
- ``GitWriteTool`` refuses agent commits in operator-managed worktrees
  unless container execution is active AND the branch is unprotected.
"""

from pathlib import Path
from unittest import mock

from security.security_gate import get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.git_write_tool import GitWriteTool


class TestGitReadWritePermissionGrains:
    """Session 'git' level passes through to the effective 'git' grain;
    the workspace capability caps it (canonical 6-key contract)."""

    def test_git_read_passthrough(self):
        session = SessionPermissions(git='read')
        eff = get_effective_permissions(session, WorkspaceCapabilities())
        assert eff['git'] == 'read'

    def test_git_write_passthrough(self):
        session = SessionPermissions(git='write')
        eff = get_effective_permissions(session, WorkspaceCapabilities())
        assert eff['git'] == 'write'

    def test_workspace_git_unavailable_caps_git_to_false(self):
        caps = WorkspaceCapabilities(git_available=False)
        session = SessionPermissions(git='write')
        eff = get_effective_permissions(session, caps)
        assert eff['git'] is False

    def test_safe_defaults_deny_write(self):
        # Canonical defaults: git='read' -- write is denied unless granted.
        eff = get_effective_permissions(
            SessionPermissions(), WorkspaceCapabilities()
        )
        assert eff['git'] == 'read'


class TestGitWriteBranchProtection:
    """Operator-managed worktrees reject agent commits outside the narrow path."""

    @staticmethod
    def _make_operator_managed_repo(tmp_path: Path) -> Path:
        repo = tmp_path / 'repo'
        repo.mkdir()
        # A .git FILE pointing at a gitdir marks an operator-managed worktree.
        (repo / '.git').write_text(
            'gitdir: /somewhere/else/.git/worktrees/feat-x\n', encoding='utf-8'
        )
        return repo

    @staticmethod
    def _tool(**params):
        defaults = {
            'operation': 'commit',
            'message': 'agent commit',
            'agent_config': {'session_permissions': {'git': 'write'}},
        }
        defaults.update(params)
        return GitWriteTool(**defaults)

    def test_unprotected_branch_requires_container_mode(self):
        tool = self._tool()
        with mock.patch.object(tool, '_git_write_allowed', return_value=True), \
                mock.patch.object(tool, '_use_container_mode', return_value=False):
            assert tool._unprotected_branch_agent_commit_allowed(
                Path('/tmp/nonexistent-repo')
            ) is False

    def test_unprotected_branch_allowed_on_feature_branch_in_container(self):
        tool = self._tool()
        with mock.patch.object(tool, '_git_write_allowed', return_value=True), \
                mock.patch.object(tool, '_use_container_mode', return_value=True), \
                mock.patch.object(tool, '_run_git', return_value='feat/x'):
            assert tool._unprotected_branch_agent_commit_allowed(
                Path('/tmp/r')
            ) is True

    def test_protected_branch_denied_even_in_container(self):
        tool = self._tool()
        with mock.patch.object(tool, '_git_write_allowed', return_value=True), \
                mock.patch.object(tool, '_use_container_mode', return_value=True), \
                mock.patch.object(tool, '_run_git', return_value='main'):
            assert tool._unprotected_branch_agent_commit_allowed(
                Path('/tmp/r')
            ) is False

    def test_commit_in_operator_managed_worktree_denied_without_container(
        self, tmp_path
    ):
        repo = self._make_operator_managed_repo(tmp_path)
        tool = self._tool()
        with mock.patch.object(tool, '_git_write_allowed', return_value=True), \
                mock.patch.object(tool, '_use_container_mode', return_value=False):
            result = tool._git_commit(repo)
        assert 'performed host-side by the operator' in result

    def test_commit_gate_fails_closed_without_git_write_permission(self, tmp_path):
        repo = self._make_operator_managed_repo(tmp_path)
        tool = self._tool(agent_config={'session_permissions': {}})
        with mock.patch.object(tool, '_use_container_mode', return_value=True):
            result = tool._git_commit(repo)
        assert result == (
            'Error: git:write denied: session git_write permission is not "write"'
        )


def test_git_read_write_permission_grains():
    """Contract wrapper: git passthrough, workspace caps, safe defaults."""
    tc = TestGitReadWritePermissionGrains()
    tc.test_git_read_passthrough()
    tc.test_git_write_passthrough()
    tc.test_workspace_git_unavailable_caps_git_to_false()
    tc.test_safe_defaults_deny_write()


def test_git_write_respects_branch_protection(tmp_path):
    """Contract wrapper: branch protection and operator-managed worktrees."""
    tc = TestGitWriteBranchProtection()
    tc.test_unprotected_branch_requires_container_mode()
    tc.test_unprotected_branch_allowed_on_feature_branch_in_container()
    tc.test_protected_branch_denied_even_in_container()
    worktree_case = tmp_path / 'case_operator_managed'
    worktree_case.mkdir()
    tc.test_commit_in_operator_managed_worktree_denied_without_container(worktree_case)
    gate_case = tmp_path / 'case_commit_gate'
    gate_case.mkdir()
    tc.test_commit_gate_fails_closed_without_git_write_permission(gate_case)

