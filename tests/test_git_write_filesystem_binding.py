"""test_git_write_filesystem_binding.py -- git:write binds the filesystem grain.

Ruling under test: a git write mutates the working tree, so ``git:write``
requires a write-capable ``filesystem`` grain as well. This is an ADDITIONAL
gate applied ON TOP of the pre-existing ``git:write`` check (no existing check
is weakened).

Coverage:

* (a) a session with ``filesystem:read`` + ``git:write`` is REFUSED, both at
  the outer category gate and at the tool's ``execute()`` entry point.
* (b) a session with ``filesystem:write`` + ``git:write`` is ALLOWED.
* (c) the refusal message names the missing grain (``filesystem:write``) and
  the session's actual grain.
* (d) a ``git:read`` operation is still ALLOWED under ``filesystem:read``
  (reads carry no filesystem:write requirement).
* ``get_required_categories`` now advertises ``filesystem:write`` for every
  write op (and keeps the legacy ``["git:write"]`` default when params are
  falsy).

All git subprocess execution is mocked/absent: no real git binary is required.
"""

from pathlib import Path

import pytest
from unittest import mock

from security.security_gate import check_required_categories
from tools.git_write_tool import GitWriteTool


def _tool(**params):
    """A GitWriteTool pre-wired for a stage op (no git subprocess needed to
    reach the filesystem grain gate)."""
    defaults = {"operation": "stage", "file_path": ["a.txt"]}
    defaults.update(params)
    return GitWriteTool(**defaults)


# --- get_required_categories advertises filesystem:write ------------------

@pytest.mark.parametrize(
    "op",
    [
        "commit", "init", "branch_create", "checkout", "stage", "unstage",
        "worktree_add", "worktree_remove", "stash_push", "stash_pop",
    ],
)
def test_get_required_categories_requires_filesystem_write(op):
    cats = GitWriteTool.get_required_categories({"operation": op})
    assert "git:write" in cats
    assert "filesystem:write" in cats


def test_get_required_categories_clone_requires_filesystem_and_network():
    assert GitWriteTool.get_required_categories({"operation": "clone"}) == [
        "git:write", "filesystem:write", "network:outbound",
    ]


def test_get_required_categories_legacy_default_when_params_falsy():
    # Existing behaviour preserved when params are falsy.
    assert GitWriteTool.get_required_categories() == ["git:write"]
    assert GitWriteTool.get_required_categories({}) == ["git:write"]


# --- (a)/(b)/(c) tool-level filesystem grain gate ------------------------

def test_tool_refuses_git_write_under_filesystem_read():
    """(a): filesystem:read + git:write -> refused before any git call."""
    tool = _tool(effective_permissions={"git": "write", "filesystem": "read"})
    with mock.patch.object(tool, "_run_git", side_effect=AssertionError("git ran")):
        result = tool.execute()
    assert result.startswith("Error: git:write requires filesystem:write")
    # (c): the message names the missing grain AND the session's actual grain.
    assert "filesystem:write" in result
    assert "this session has filesystem:read" in result


def test_tool_refuses_git_write_when_filesystem_grain_absent():
    """A session with no filesystem key at all fails closed (label 'none')."""
    tool = _tool(effective_permissions={"git": "write"})
    result = tool.execute()
    assert result.startswith("Error: git:write requires filesystem:write")
    assert "this session has filesystem:none" in result


def test_tool_allows_git_write_under_filesystem_write():
    """(b): filesystem:write + git:write -> the filesystem grain gate passes
    and execute() proceeds past it to the operation dispatch."""
    tool = _tool(effective_permissions={"git": "write", "filesystem": "write"})
    assert tool._filesystem_write_allowed() is True
    # execute() must NOT short-circuit with the filesystem grain error; stub
    # the repo/op layer so no real git subprocess is needed.
    with mock.patch.object(tool, "_git_repo_root", return_value=Path("/workspace")), \
            mock.patch.object(tool, "_git_stage", return_value="ok") as stage:
        result = tool.execute()
    assert result == "ok"
    stage.assert_called_once()


def test_tool_allows_git_write_under_filesystem_full():
    tool = _tool(effective_permissions={"git": "write", "filesystem": "full"})
    assert tool._filesystem_write_allowed() is True


def test_tool_allows_git_write_under_filesystem_ask():
    # 'ask' defers to the outer ToolExecutor gate (already prompted).
    tool = _tool(effective_permissions={"git": "write", "filesystem": "ask"})
    assert tool._filesystem_write_allowed() is True


# --- (a)/(b)/(d) outer category gate -------------------------------------

def test_gate_refuses_git_write_under_filesystem_read():
    """(a)+(c): the outer gate refuses and names the missing grain."""
    ok, msg = check_required_categories(
        ["git:write", "filesystem:write"],
        {"git": "write", "filesystem": "read"},
        "GitWriteTool", {}, "write git", event_bus=None,
    )
    assert ok is False
    assert "filesystem:write" in msg


def test_gate_allows_git_write_under_filesystem_write():
    """(b): the outer gate allows when both grains are write-capable."""
    ok, msg = check_required_categories(
        ["git:write", "filesystem:write"],
        {"git": "write", "filesystem": "write"},
        "GitWriteTool", {}, "write git", event_bus=None,
    )
    assert ok is True and msg == ""


def test_gate_git_read_allowed_under_filesystem_read():
    """(d): a git READ needs no filesystem:write and passes under read-only."""
    ok, msg = check_required_categories(
        ["git:read"],
        {"git": "read", "filesystem": "read"},
        "GitInfoTool", {}, "read git", event_bus=None,
    )
    assert ok is True and msg == ""
