"""
Unit regression tests for the linked-worktree fix in
``infra.resource_container_manager``.

A git linked worktree's ``.git`` is a FILE containing a ``gitdir: <path>``
pointer into the MAIN repository (``<main>/.git/worktrees/<name>``). The
hidden git resource container only bind-mounts the workspace at ``/workspace``
(every other host path is invisible), so git inside the container cannot
resolve the host-only pointer and reports "Not a git repository" (see
tools/git_info_tool.py). The fix bind-mounts the MAIN repository at its
original host path so the pointer resolves; ``_resolve_worktree_main_repo()``
decides, with validation, when that extra mount is warranted.

These are PURE unit tests: no Docker daemon and no docker SDK required (the
module imports docker defensively via try/except). The live-docker regression
(container created with the extra mount; ``git rev-parse`` succeeds inside)
belongs in the docker integration suites; this file pins the resolver logic.
"""

import os
import sys

# Make the repository root importable when running `pytest tests/docker/` or
# this file directly (tests/docker has no conftest.py of its own).
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

import pytest

import infra.resource_container_manager as rcm


def _make_git_dir(root, name=".git"):
    """Create a minimal real-looking git dir (HEAD + objects/refs)."""
    git_dir = root / name
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "objects").mkdir(exist_ok=True)
    (git_dir / "refs").mkdir(exist_ok=True)
    return git_dir


def _make_worktree(workspace, main_repo, name="wt"):
    """Create a linked worktree: workspace/.git FILE -> main repo gitdir."""
    gitdir = _make_git_dir(main_repo) / "worktrees" / name
    gitdir.mkdir(parents=True, exist_ok=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return gitdir


class TestResolveWorktreeMainRepo:
    def test_regular_repository_returns_none(self, tmp_path):
        """A normal repo (.git is a directory) needs no extra mount."""
        ws = tmp_path / "repo"
        ws.mkdir()
        _make_git_dir(ws)
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_missing_dot_git_returns_none(self, tmp_path):
        ws = tmp_path / "empty"
        ws.mkdir()
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_linked_worktree_returns_main_repo(self, tmp_path):
        main = tmp_path / "main-repo"
        main.mkdir()
        _make_git_dir(main)
        ws = tmp_path / "wt-checkout"
        _make_worktree(ws, main)
        result = rcm._resolve_worktree_main_repo(str(ws))
        assert result == str(main.resolve())

    def test_accepts_pathlike_workspace(self, tmp_path):
        """The helper takes str or os.PathLike."""
        main = tmp_path / "main-repo"
        main.mkdir()
        _make_git_dir(main)
        ws = tmp_path / "wt-checkout"
        _make_worktree(ws, main)
        assert rcm._resolve_worktree_main_repo(ws) == str(main.resolve())

    def test_relative_gitdir_resolved_against_workspace(self, tmp_path):
        """git allows a relative gitdir pointer (relative to the .git file)."""
        main = tmp_path / "main-repo"
        main.mkdir()
        _make_git_dir(main)
        ws = tmp_path / "wt-checkout"
        ws.mkdir()
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True, exist_ok=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        rel = os.path.relpath(gitdir, ws)
        (ws / ".git").write_text(f"gitdir: {rel}\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) == str(main.resolve())

    def test_missing_gitdir_target_returns_none(self, tmp_path):
        main = tmp_path / "main-repo"
        main.mkdir()
        _make_git_dir(main)
        ws = tmp_path / "wt-checkout"
        ws.mkdir()
        ghost = main / ".git" / "worktrees" / "ghost"
        (ws / ".git").write_text(f"gitdir: {ghost}\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_submodule_style_gitdir_returns_none(self, tmp_path):
        """.git/modules/<name> is a submodule, not a linked worktree."""
        main = tmp_path / "main-repo"
        main.mkdir()
        _make_git_dir(main)
        ws = tmp_path / "sub-checkout"
        ws.mkdir()
        sub_gitdir = main / ".git" / "modules" / "sub"
        sub_gitdir.mkdir(parents=True, exist_ok=True)
        (ws / ".git").write_text(f"gitdir: {sub_gitdir}\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_malformed_gitdir_content_returns_none(self, tmp_path):
        ws = tmp_path / "wt"
        ws.mkdir()
        (ws / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_empty_gitdir_returns_none(self, tmp_path):
        ws = tmp_path / "wt"
        ws.mkdir()
        (ws / ".git").write_text("gitdir:\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_main_repo_under_vault_returns_none(self, tmp_path):
        """The vault (~/.thoughtmachine) is NEVER mounted — even as a main repo."""
        vault = tmp_path / "vault"
        main = vault / "project"
        main.mkdir(parents=True)
        _make_git_dir(main)
        ws = tmp_path / "wt-checkout"
        _make_worktree(ws, main)
        result = rcm._resolve_worktree_main_repo(str(ws), vault_root=str(vault))
        assert result is None

    def test_default_vault_root_is_home_thoughtmachine(self, tmp_path):
        """Without vault_root, ~/.thoughtmachine is refused."""
        main = tmp_path / "thoughtmachine-sibling"
        main.mkdir()
        _make_git_dir(main)
        ws = tmp_path / "wt-checkout"
        _make_worktree(ws, main)
        # Point vault_root at tmp_path so the main repo (a child of tmp_path)
        # falls inside the vault root, as ~/.thoughtmachine would on the host.
        result = rcm._resolve_worktree_main_repo(str(ws), vault_root=str(tmp_path))
        assert result is None

    def test_main_repo_inside_workspace_returns_none(self, tmp_path):
        """A main repo nested in the workspace is covered by /workspace."""
        ws = tmp_path / "wt-checkout"
        main = ws / "nested-main"
        main.mkdir(parents=True)
        _make_git_dir(main)
        gitdir = main / ".git" / "worktrees" / "inner"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (ws / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_main_root_without_real_git_dir_returns_none(self, tmp_path):
        """.git exists but lacks HEAD/objects/refs — not a real repo."""
        main = tmp_path / "main-repo"
        main.mkdir()
        (main / ".git").mkdir(parents=True)  # plain dir, NOT a git dir
        ws = tmp_path / "wt-checkout"
        ws.mkdir()
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (ws / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        assert rcm._resolve_worktree_main_repo(str(ws)) is None

    def test_pointer_to_fs_root_shape_rejected(self, tmp_path):
        """A gitdir directly under /.git/worktrees/... (root) is refused."""
        ws = tmp_path / "wt-checkout"
        ws.mkdir()
        (ws / ".git").write_text(
            "gitdir: /.git/worktrees/wt\n", encoding="utf-8"
        )
        assert rcm._resolve_worktree_main_repo(str(ws)) is None


class TestPathIsWithin:
    def test_nested(self, tmp_path):
        assert rcm._path_is_within(tmp_path / "a" / "b", tmp_path) is True

    def test_equal(self, tmp_path):
        assert rcm._path_is_within(tmp_path, tmp_path) is True

    def test_sibling(self, tmp_path):
        assert rcm._path_is_within(tmp_path / "a", tmp_path / "b") is False

    def test_empty_parent(self, tmp_path):
        assert rcm._path_is_within(tmp_path, "") is False
