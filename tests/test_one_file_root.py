"""ONE resolved root for every file tool -- a stale ``worktree_root`` is inert.

The bug this pins: ``AgentConfig.worktree_root`` is persisted in the agent
config JSON, so a long-lived session can keep pointing at a
``.worktrees/<uuid>`` tree long after that tree stopped being interesting. File
tools that *narrowed* themselves to it edited a tree no diff, commit, CI run or
container mount ever saw, while ``ReadFile`` -- which resolved the registered
root -- read the main checkout: two roots, one workspace.

The contract pinned here:

* a relative write lands under the REGISTERED workspace root, and the worktree
  copy of that relative path is left untouched;
* a read resolves the same root the write used, so what is read is what is
  edited;
* the ignored value is reported exactly ONCE per (worktree, registered root)
  pair at INFO level -- one receipt per spawn, not one per tool call;
* a ``worktree_root`` that *is* the registered root is not being ignored and
  therefore says nothing at all.
"""

from __future__ import annotations

import json
import logging

import pytest

from tools import base as base_module
from tools.file_editor import FileEditor
from tools.read_file_tool import ReadFile

IGNORED_MARKER = "ignored: file tools use the registered workspace root"


@pytest.fixture(autouse=True)
def _fresh_receipts():
    """The receipt dedupe set is module scope (one INFO per spawn), so reset it."""
    base_module._IGNORED_WORKTREE_ROOTS.clear()
    yield
    base_module._IGNORED_WORKTREE_ROOTS.clear()


@pytest.fixture()
def trees(tmp_path):
    """``(root, worktree)`` with a DIFFERENT ``notes.txt`` in each."""
    root = tmp_path / "repo"
    worktree = root / ".worktrees" / "sess-xyz"
    worktree.mkdir(parents=True)
    (root / "notes.txt").write_text("registered root copy\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("worktree copy\n", encoding="utf-8")
    return root, worktree


def _editor(root, worktree, filename="proof.txt", content="written\n"):
    return FileEditor(
        operation="write",
        filename=filename,
        content=content,
        workspace_path=str(root),
        worktree_root=str(worktree) if worktree is not None else None,
        effective_permissions={"filesystem": "full"},
    )


def _read(root, worktree, file_path="notes.txt"):
    return ReadFile(
        file_path=file_path,
        workspace_path=str(root),
        worktree_root=str(worktree) if worktree is not None else None,
        effective_permissions={"filesystem": "read"},
    )


# ---------------------------------------------------------------------------
# The write side and the read side agree on one root
# ---------------------------------------------------------------------------


def test_relative_write_lands_in_the_registered_root(trees):
    root, worktree = trees

    out = _editor(root, worktree, content="written from a stale worktree_root\n").execute()

    assert "Error" not in out, out
    assert (root / "proof.txt").read_text(encoding="utf-8") == (
        "written from a stale worktree_root\n"
    )
    # The narrower tree must not receive the write: an edit there is one no
    # diff, commit, CI run or container mount would ever see.
    assert not (worktree / "proof.txt").exists()


def test_read_resolves_the_same_root_the_write_used(trees):
    root, worktree = trees

    _editor(root, worktree, filename="notes.txt", content="edited in the one root\n").execute()

    payload = json.loads(_read(root, worktree).execute())

    assert payload.get("error") is None, payload
    assert payload["content"] == "edited in the one root\n"
    # ... and the worktree's copy of the same relative path is untouched.
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "worktree copy\n"


def test_without_a_worktree_root_the_same_root_is_used(trees):
    root, _worktree = trees

    _editor(root, None, content="no worktree configured\n").execute()
    payload = json.loads(_read(root, None).execute())

    assert payload["content"] == "registered root copy\n"


# ---------------------------------------------------------------------------
# The receipt: one INFO per (worktree, root) pair
# ---------------------------------------------------------------------------


def test_the_ignored_value_is_reported_once_across_tool_calls(trees, caplog):
    root, worktree = trees

    with caplog.at_level(logging.INFO):
        _editor(root, worktree).execute()
        _read(root, worktree).execute()

    receipts = [rec for rec in caplog.records if IGNORED_MARKER in rec.getMessage()]
    assert len(receipts) == 1, caplog.text
    message = receipts[0].getMessage()
    # It names BOTH roots, so an operator can see what was ignored and where
    # the tools actually went.
    assert str(worktree) in message, message
    assert str(root) in message, message


def test_a_worktree_root_equal_to_the_root_is_not_reported(trees, caplog):
    root, _worktree = trees

    with caplog.at_level(logging.INFO):
        _editor(root, root, content="explicitly the same root\n").execute()
        _read(root, root).execute()

    assert IGNORED_MARKER not in caplog.text
    assert (root / "proof.txt").read_text(encoding="utf-8") == "explicitly the same root\n"
