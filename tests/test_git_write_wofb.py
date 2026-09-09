"""test_git_write_wofb.py -- write_on_feature_branch gate + tool enforcement.

Phase-3 contract under test:

* ``write_on_feature_branch`` ranks at the WRITE tier (3) in
  ``security.gate_helpers._value_satisfies``: the outer ``git:write``
  category gate passes for a wofb session, while ``full`` (rank 4) stays
  denied and rank-3 ordering is preserved (wofb + write -> write via
  ``_min_permission``).
* The feature-branch-only restriction lives in ``tools/git_write_tool.py``:
  ``_git_write_allowed`` admits the wofb grain, and ``_git_commit`` refuses
  commits when the current branch is protected (dev/master/main) or cannot
  be resolved (fail closed).  Full ``write``/``full``/``ask`` grants are
  never branch-restricted by the tool.
* A worker whose git level resolves to ``ask`` is auto-denied for
  ``git:write`` (ask requires interactive approval; not available in
  worker context).

All git subprocess execution is mocked: no real git binary is required.
"""

import pytest
from unittest import mock

from security.gate_helpers import _value_satisfies
from security.security_gate import (
    _min_permission,
    check_required_categories,
    get_effective_permissions,
)
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.git_write_tool import GitWriteTool

_FULL_CAPS = WorkspaceCapabilities()


def _commit_tool(**params):
    """A GitWriteTool instance pre-wired for a commit operation with a
    write_on_feature_branch session grant (the established direct-call
    agent_config convention used across the git permission tests)."""
    defaults = {
        "operation": "commit",
        "message": "agent commit",
        "file_path": ["note.txt"],
        "agent_config": {
            "session_permissions": {"git": "write_on_feature_branch"}
        },
    }
    defaults.update(params)
    return GitWriteTool(**defaults)


# ── Gate level: wofb ranks at the write tier (rank 3) ─────────────────────

def test_wofb_gate_ranks_at_write_tier():
    """Phase-3 helper edit: _value_satisfies ranks write_on_feature_branch
    at the write tier -- git:read AND git:write pass, full (rank 4) is
    denied, and rank-3 ordering is preserved (wofb + write -> write)."""
    assert _value_satisfies("read", "write_on_feature_branch") is True
    assert _value_satisfies("write", "write_on_feature_branch") is True
    assert _value_satisfies("banned", "write_on_feature_branch") is True
    assert _value_satisfies("full", "write_on_feature_branch") is False
    assert _min_permission("write", "write_on_feature_branch") == "write"
    assert _min_permission("read", "write_on_feature_branch") == "read"


def test_wofb_session_passes_git_write_category_gate():
    """A write_on_feature_branch session produces effective grains and now
    passes the outer git:write category gate (enforcement moved tool-side)."""
    eff = get_effective_permissions(
        SessionPermissions(git="write_on_feature_branch"), _FULL_CAPS
    )
    assert eff["git"] == "write_on_feature_branch"
    ok, msg = check_required_categories(
        ["git:write"], dict(eff), "GitWriteTool", {}, "write git", event_bus=None
    )
    assert ok is True and msg == ""


# ── Tool gate: wofb commits restricted to non-protected branches ──────────

@pytest.mark.parametrize("branch", ["dev", "master", "main", ""])
def test_wofb_commit_denied_on_protected_or_unresolved_branch(tmp_path, branch):
    """A wofb-granted commit on a protected branch (or with empty branch
    output) is denied with a clear message and the commit subprocess never
    runs (only branch resolution is attempted)."""
    tool = _commit_tool()
    repo = tmp_path / "repo"
    repo.mkdir()
    with mock.patch.object(
        tool, "_run_git", return_value=branch + "\n"
    ) as run:
        result = tool._git_commit(repo)
    assert "Error: git:write denied" in result
    assert "write_on_feature_branch" in result
    assert "protected" in result
    run.assert_called_once_with(repo, ["rev-parse", "--abbrev-ref", "HEAD"])


def test_wofb_commit_fails_closed_when_branch_unresolvable(tmp_path):
    """Hard security errors while resolving the current branch fail closed:
    the commit is denied as unresolved ('unknown')."""
    tool = _commit_tool()
    repo = tmp_path / "repo"
    repo.mkdir()
    with mock.patch.object(tool, "_run_git", side_effect=RuntimeError("denied")):
        result = tool._git_commit(repo)
    assert "Error: git:write denied" in result
    assert "unknown" in result


def test_wofb_commit_allowed_on_feature_branch(tmp_path):
    """A wofb-granted commit on a non-protected branch proceeds: branch
    resolution runs, then the selective add+commit subprocess flow runs."""
    tool = _commit_tool()
    repo = tmp_path / "repo"
    repo.mkdir()
    with mock.patch.object(
        tool, "_run_git", side_effect=["feat/x\n", "committed"]
    ) as run, mock.patch.object(
        tool, "_validated_rel_paths", return_value=["note.txt"]
    ), mock.patch.object(tool, "_git_add", return_value=""):
        result = tool._git_commit(repo)
    assert "git:write denied" not in result
    assert "write_on_feature_branch" not in result
    assert run.call_count == 2
    assert run.call_args_list[0].args[1] == ["rev-parse", "--abbrev-ref", "HEAD"]
    assert run.call_args_list[1].args[1][0] == "commit"


def test_full_write_grant_commit_allowed_on_protected_branch(tmp_path):
    """write/full grants are NOT branch-restricted by this tool: a commit on
    a protected branch proceeds via both the session_permissions direct-call
    path and the effective-permissions (ToolExecutor) path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    tool_sp = _commit_tool(
        agent_config={"session_permissions": {"git": "write"}}
    )
    with mock.patch.object(
        tool_sp, "_run_git", return_value="committed"
    ) as run_sp, mock.patch.object(
        tool_sp, "_validated_rel_paths", return_value=["note.txt"]
    ), mock.patch.object(tool_sp, "_git_add", return_value=""):
        result_sp = tool_sp._git_commit(repo)
    assert "git:write denied" not in result_sp
    assert run_sp.call_count == 1
    assert run_sp.call_args_list[0].args[1][0] == "commit"

    tool_eff = _commit_tool(
        agent_config={},
        effective_permissions={"git": "write"},
    )
    with mock.patch.object(
        tool_eff, "_run_git", return_value="committed"
    ) as run_eff, mock.patch.object(
        tool_eff, "_validated_rel_paths", return_value=["note.txt"]
    ), mock.patch.object(tool_eff, "_git_add", return_value=""):
        result_eff = tool_eff._git_commit(repo)
    assert "git:write denied" not in result_eff
    assert run_eff.call_count == 1
    assert run_eff.call_args_list[0].args[1][0] == "commit"


# ── Worker path: ask git level auto-denied (no interactive user) ──────────

def test_worker_ask_git_write_auto_denied():
    """A worker call whose git level resolves to 'ask' is denied immediately
    for git:write with the established no-interactive-user message."""
    eff = get_effective_permissions(SessionPermissions(git="ask"), _FULL_CAPS)
    denied, deny_msg = check_required_categories(
        ["git:write"], dict(eff), "GitWriteTool", {}, "write git",
        event_bus=None, is_worker_context=True,
    )
    assert denied is False
    assert "ask requires interactive approval; not available in worker context" in deny_msg
