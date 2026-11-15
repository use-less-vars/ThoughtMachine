"""Worktree defaults: shared traversal exclusions + the per-session worktree.

Three things are proven here:

1. ``TestSharedExclusions`` -- the traversal tools share ONE exclusion list and
   ``.worktrees`` is in it, so a worktree checkout cannot leak into results.
2. ``TestWorktreeIsNotTraversed`` -- end-to-end: a real file inside
   ``.worktrees/<x>/`` is absent from GlobTool, FileSearchTool and
   DirectoryTreeTool results, while its sibling outside is present.
3. ``TestResolveWorkerWorktreeRoot`` -- the fail-open decision table, driven by
   a fake ``git_runner`` (no git binary is needed), plus the real no-git path.
"""

from __future__ import annotations

import os

import pytest

from tools.directory_tree_tool import DirectoryTreeTool
from tools.file_search_tool import FileSearchTool
from tools.glob_tool import GlobTool
from tools.utils import DEFAULT_EXCLUDE_DIRS
from tools.workspace.worktree_defaults import (
    resolve_worker_worktree_root,
    session_worktree_name,
)


# ---------------------------------------------------------------------------
# 1. One shared list
# ---------------------------------------------------------------------------


class TestSharedExclusions:
    def test_worktrees_is_excluded(self):
        assert ".worktrees" in DEFAULT_EXCLUDE_DIRS

    @pytest.mark.parametrize(
        "tool_class", [GlobTool, FileSearchTool, DirectoryTreeTool]
    )
    def test_every_traversal_tool_defaults_to_the_shared_list(self, tool_class):
        defaults = tool_class.model_fields["exclude_dirs"].default_factory()

        assert ".worktrees" in defaults
        # The shared entries are all present; a tool may only ADD its own.
        assert set(DEFAULT_EXCLUDE_DIRS) <= set(defaults)

    def test_the_lists_are_independent_copies(self):
        first = GlobTool.model_fields["exclude_dirs"].default_factory()
        first.append("mutated")

        assert "mutated" not in GlobTool.model_fields[
            "exclude_dirs"
        ].default_factory()
        assert "mutated" not in DEFAULT_EXCLUDE_DIRS


# ---------------------------------------------------------------------------
# 2. End to end: a real file in the worktree is invisible
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path):
    """A workspace with ``visible.txt`` and ``.worktrees/feat/hidden.txt``."""
    root = tmp_path / "workspace"
    worktree = root / ".worktrees" / "feat"
    worktree.mkdir(parents=True)
    (root / "visible.txt").write_text("the needle is here\n")
    (worktree / "hidden.txt").write_text("the needle is here too\n")
    return root


class TestWorktreeIsNotTraversed:
    def test_glob_does_not_return_the_worktree_file(self, workspace):
        tool = GlobTool(
            directory=str(workspace),
            pattern="**/*.txt",
            recursive=True,
            workspace_path=str(workspace),
        )

        found = [os.path.basename(p) for p in tool._get_files()]

        assert found == ["visible.txt"]

    def test_file_search_does_not_read_the_worktree_file(self, workspace):
        tool = FileSearchTool(
            pattern="needle",
            directory=str(workspace),
            file_pattern="*.txt",
            workspace_path=str(workspace),
            effective_permissions={"filesystem": "read"},
        )

        out = tool.execute()

        assert "visible.txt" in out
        assert ".worktrees" not in out and "hidden.txt" not in out

    def test_directory_tree_does_not_descend_into_the_worktree(self, workspace):
        tool = DirectoryTreeTool(
            directory=str(workspace),
            format="list",
            include_sizes=False,
            workspace_path=str(workspace),
        )

        out = tool.execute()

        assert "visible.txt" in out
        assert "hidden.txt" not in out


# ---------------------------------------------------------------------------
# 3. resolve_worker_worktree_root: fail-open decision table
# ---------------------------------------------------------------------------


class _Git:
    """Records calls; returns a scripted status."""

    def __init__(self, status=0, create=True):
        self.status = status
        self.create = create
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        if self.status == 0 and self.create:
            # Mimic ``git worktree add``: create the directory with a file.
            target = argv[-1]
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, ".git"), "w") as handle:
                handle.write("gitdir: /nowhere\n")
        return self.status


def _repo(tmp_path):
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    return root


class TestResolveWorkerWorktreeRoot:
    def test_creates_a_worktree_per_session(self, tmp_path):
        root = _repo(tmp_path)
        git = _Git()

        path = resolve_worker_worktree_root(str(root), "sess-1", git_runner=git)

        assert path == os.path.join(os.path.realpath(root), ".worktrees", "sess-1")
        assert os.path.isdir(path)
        assert git.calls == [
            (["worktree", "add", "--detach", path], os.path.realpath(root))
        ]

    def test_an_existing_worktree_is_reused_without_calling_git(self, tmp_path):
        root = _repo(tmp_path)
        existing = root / ".worktrees" / "sess-1"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("x")
        git = _Git()

        path = resolve_worker_worktree_root(str(root), "sess-1", git_runner=git)

        assert path == str(existing)
        assert git.calls == []

    def test_an_empty_leftover_directory_is_not_adopted(self, tmp_path):
        root = _repo(tmp_path)
        (root / ".worktrees" / "sess-1").mkdir(parents=True)
        git = _Git(status=1, create=False)

        assert resolve_worker_worktree_root(
            str(root), "sess-1", git_runner=git
        ) is None
        assert len(git.calls) == 1

    @pytest.mark.parametrize("session_id", [None, "", "   ", "...", "..", "/"])
    def test_an_unusable_session_id_falls_back_without_calling_git(
        self, tmp_path, session_id
    ):
        root = _repo(tmp_path)
        git = _Git()

        assert (
            resolve_worker_worktree_root(str(root), session_id, git_runner=git)
            is None
        )
        assert git.calls == []

    @pytest.mark.parametrize("root", [None, "", "/does/not/exist/at/all"])
    def test_a_missing_workspace_root_falls_back(self, tmp_path, root):
        git = _Git()

        assert resolve_worker_worktree_root(root, "sess-1", git_runner=git) is None
        assert git.calls == []

    def test_a_non_git_workspace_falls_back(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        git = _Git()

        assert resolve_worker_worktree_root(str(root), "sess-1", git_runner=git) is None
        assert git.calls == []

    def test_a_failing_git_falls_back(self, tmp_path):
        root = _repo(tmp_path)
        git = _Git(status=128, create=False)

        assert resolve_worker_worktree_root(str(root), "sess-1", git_runner=git) is None

    def test_a_runner_that_raises_falls_back(self, tmp_path):
        root = _repo(tmp_path)

        def boom(argv, cwd):
            raise OSError("no git here")

        assert resolve_worker_worktree_root(str(root), "sess-1", git_runner=boom) is None

    def test_git_reporting_success_without_a_directory_falls_back(self, tmp_path):
        root = _repo(tmp_path)
        git = _Git(status=0, create=False)

        assert resolve_worker_worktree_root(str(root), "sess-1", git_runner=git) is None

    def test_the_default_runner_without_a_usable_grant_falls_back(self, tmp_path):
        """The real runner spends the ``git`` grain via host_run; an
        unresolvable workspace id / missing grant is refused, not raised."""
        root = _repo(tmp_path)

        assert resolve_worker_worktree_root(str(root), "sess-1") is None

    def test_the_session_name_is_filesystem_safe(self):
        assert session_worktree_name("2024/01/sess 1") == "2024-01-sess-1"
        # A traversal attempt stays ONE harmless name: no separator survives,
        # so the result can only ever address a child of ``.worktrees``.
        traversal = session_worktree_name("../../etc/passwd")
        assert traversal is not None
        assert "/" not in traversal and os.sep not in traversal
        assert traversal not in (".", "..")
        assert session_worktree_name("..") is None
        assert session_worktree_name(None) is None
