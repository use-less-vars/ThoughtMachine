"""File-editing tools must resolve against ONE root, not the CWD.

Every file tool is anchored at ``ToolBase._resolve_workspace_root()``
(``tools/base.py``), which is the registered workspace root. A configured
``worktree_root`` (``AgentConfig.worktree_root``, injected into every tool
instance by ``ToolExecutor``) is validated fail-closed and then IGNORED: it no
longer narrows anything, because narrowing only the write side is what let
``ReadFile`` read the main checkout while ``FileEditor`` wrote a
``.worktrees/<uuid>`` tree that no diff, commit, CI run or container mount
could see.

``FileEditor`` routes every path through ``ToolBase._validate_path``, so it
obeys that root. The glob-anchored editors did not: ``ApplyEdits`` globbed
``Path('.')`` -- the process CWD, which is not a root at all -- and
``RefactorTool`` globbed ``_resolve_registry_workspace()`` directly, bypassing
``_resolve_workspace_root()`` entirely. Both now glob the same one root.
"""

from __future__ import annotations

import pytest

from tools.apply_edits import ApplyEdits
from tools.file_editor import FileEditor
from tools.refactor_tool import RefactorTool


@pytest.fixture()
def tree(tmp_path):
    """``(root, worktree)`` with the worktree nested inside the root."""
    root = tmp_path / "workspace"
    worktree = root / ".worktrees" / "feat"
    worktree.mkdir(parents=True)
    return root, worktree


# ---------------------------------------------------------------------------
# FileEditor: one root, worktree_root validated then ignored, failing closed
# ---------------------------------------------------------------------------


class TestFileEditorRoot:
    def test_worktree_root_is_ignored_and_the_write_lands_in_the_one_root(self, tree):
        root, worktree = tree

        out = FileEditor(
            operation="write",
            filename="proof.txt",
            content="bound to the one root\n",
            workspace_path=str(root),
            worktree_root=str(worktree),
            effective_permissions={"filesystem": "full"},
        ).execute()

        assert "Error" not in out, out
        assert (root / "proof.txt").read_text() == "bound to the one root\n"
        # The worktree copy stays untouched: an edit there is one no diff,
        # commit, CI run or container mount would ever see.
        assert not (worktree / "proof.txt").exists()

    def test_traversal_out_of_the_root_is_refused(self, tree):
        root, worktree = tree

        out = FileEditor(
            operation="write",
            filename="../escaped.txt",
            content="must not be written\n",
            workspace_path=str(root),
            worktree_root=str(worktree),
            effective_permissions={"filesystem": "full"},
        ).execute()

        assert "outside workspace" in out, out
        # Named root + offending path, so the caller can see what was refused.
        assert str(root) in out, out
        assert not (root / "escaped.txt").exists()
        assert not (root.parent / "escaped.txt").exists()

    def test_absolute_path_outside_the_root_is_refused(self, tree):
        root, worktree = tree
        # Outside the ONE root -- and note ``root / "outside.txt"`` is now an
        # ordinary in-root path, so it can no longer serve as the escapee.
        outside = root.parent / "outside.txt"
        outside.write_text("untouched\n")

        out = FileEditor(
            operation="write",
            filename=str(outside),
            content="must not be written\n",
            workspace_path=str(root),
            worktree_root=str(worktree),
            effective_permissions={"filesystem": "full"},
        ).execute()

        assert "outside workspace" in out, out
        assert str(root) in out, out
        assert outside.read_text() == "untouched\n"

    def test_without_a_worktree_the_write_lands_in_the_workspace_root(self, tree):
        root, worktree = tree

        out = FileEditor(
            operation="write",
            filename="proof.txt",
            content="bound to the workspace\n",
            workspace_path=str(root),
            effective_permissions={"filesystem": "full"},
        ).execute()

        assert "Error" not in out, out
        assert (root / "proof.txt").read_text() == "bound to the workspace\n"
        assert not (worktree / "proof.txt").exists()


# ---------------------------------------------------------------------------
# The glob-anchored editors: the pattern is resolved against the same root
# ---------------------------------------------------------------------------


class TestGlobAnchoredEditors:
    def test_apply_edits_pattern_globs_the_one_root_not_the_cwd(
        self, tree, monkeypatch
    ):
        root, worktree = tree
        main_only = root / "main_only.py"
        main_only.write_text("alpha\n")
        # A decoy inside the ignored worktree: it must never be matched, even
        # though the session was bound there.
        in_worktree = worktree / "worktree_only.py"
        in_worktree.write_text("alpha\n")
        # The live situation: the process happens to sit in the registered root
        # while the (stale) worktree_root points somewhere else.
        monkeypatch.chdir(root)

        out = ApplyEdits(
            edits=[{"find": "alpha", "replace": "beta"}],
            file_pattern="*.py",
            workspace_path=str(root),
            worktree_root=str(worktree),
        ).execute()

        assert main_only.read_text() == "beta\n", out
        assert in_worktree.read_text() == "alpha\n", out

    def test_refactor_pattern_globs_the_one_root(self, tree):
        root, worktree = tree
        (root / "pkg").mkdir()
        (root / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
        (worktree / "pkg").mkdir()
        (worktree / "pkg" / "mod.py").write_text("def f():\n    return 1\n")

        tool = RefactorTool(
            operation="replace_function_body",
            target="f",
            file_pattern="pkg/mod.py",
            params={"new_body": "return 2"},
            workspace_path=str(root),
            worktree_root=str(worktree),
        )

        assert [str(p) for p in tool._get_file_paths()] == [
            str(root / "pkg" / "mod.py")
        ]
