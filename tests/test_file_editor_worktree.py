"""File-editing tools must resolve against the SESSION's root, not the CWD.

A session bound to a git worktree (``AgentConfig.worktree_root``, injected into
every tool instance by ``ToolExecutor``) must edit the worktree; a session with
no worktree must edit the registered workspace root. The one root that anchors
a *relative* path is ``ToolBase._resolve_workspace_root()``
(``tools/base.py:356``), which returns the validated ``worktree_root`` when one
is configured and the registry root otherwise.

``FileEditor`` already routes every path through ``ToolBase._validate_path``,
so it obeys that root. The glob-anchored editors did not: ``ApplyEdits``
globbed ``Path('.')`` -- the process CWD, which is not a root at all -- and
``RefactorTool`` globbed ``_resolve_registry_workspace()``, the WIDER root, so a
session bound to a worktree matched files it then refused, and silently
processed nothing.
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
# FileEditor: one root, chosen by worktree_root, failing closed
# ---------------------------------------------------------------------------


class TestFileEditorRoot:
    def test_worktree_write_leaves_the_main_checkout_untouched(self, tree):
        root, worktree = tree

        out = FileEditor(
            operation="write",
            filename="proof.txt",
            content="bound to the worktree\n",
            workspace_path=str(root),
            worktree_root=str(worktree),
            effective_permissions={"filesystem": "full"},
        ).execute()

        assert "Error" not in out, out
        assert (worktree / "proof.txt").read_text() == "bound to the worktree\n"
        # A silent fallback to the wider root is the failure this prevents.
        assert not (root / "proof.txt").exists()

    def test_traversal_out_of_the_worktree_is_refused(self, tree):
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
        assert str(worktree) in out, out
        assert not (root / "escaped.txt").exists()
        assert not (root.parent / "escaped.txt").exists()

    def test_absolute_path_outside_the_root_is_refused(self, tree):
        root, worktree = tree
        outside = root / "outside.txt"
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
        assert str(worktree) in out, out
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
    def test_apply_edits_pattern_globs_the_worktree_not_the_cwd(
        self, tree, monkeypatch
    ):
        root, worktree = tree
        main_only = root / "main_only.py"
        main_only.write_text("alpha\n")
        in_worktree = worktree / "worktree_only.py"
        in_worktree.write_text("alpha\n")
        # The live situation: the process happens to sit in the wider checkout
        # while the session is bound to the worktree.
        monkeypatch.chdir(root)

        out = ApplyEdits(
            edits=[{"find": "alpha", "replace": "beta"}],
            file_pattern="*.py",
            workspace_path=str(root),
            worktree_root=str(worktree),
        ).execute()

        assert in_worktree.read_text() == "beta\n", out
        assert main_only.read_text() == "alpha\n", out

    def test_refactor_pattern_globs_the_worktree_not_the_wider_root(self, tree):
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
            str(worktree / "pkg" / "mod.py")
        ]
