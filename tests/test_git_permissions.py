"""Git permission grains and branch protection.

Canonical 6-key contract:

- The session ``git`` level passes straight through to the effective
  ``git`` grain (SessionPermissions holds a single git key, not split
  read/write grains); the workspace capability (``git_available``) caps it.
- ``GitWriteTool`` commits are gated solely by the ``git`` write grain; the
  legacy operator-managed-worktree exemption is removed.
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
    """A commit without the ``git`` write grain is refused (fail closed)."""

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
            'effective_permissions': {'git': 'write'},
        }
        defaults.update(params)
        return GitWriteTool(**defaults)

    def test_commit_gate_fails_closed_without_git_write_permission(self, tmp_path):
        repo = self._make_operator_managed_repo(tmp_path)
        tool = self._tool(effective_permissions={})
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
    """Contract wrapper: the commit gate fails closed without the ``git`` write grain."""
    tc = TestGitWriteBranchProtection()
    gate_case = tmp_path / 'case_commit_gate'
    gate_case.mkdir()
    tc.test_commit_gate_fails_closed_without_git_write_permission(gate_case)

