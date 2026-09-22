"""RED test — B1: the PLAIN git-write path must fail closed on a detached HEAD.

``tools/git_write_tool.py`` already refuses a detached HEAD on two NARROW
paths, both keyed off the same probe (``git rev-parse --abbrev-ref HEAD``
returns the literal string ``"HEAD"`` when HEAD is detached) and the same
sentinel string:

* ``GitWriteTool._unprotected_branch_agent_commit_allowed`` — the
  operator-managed-worktree agent-commit gate (sets
  ``_agent_commit_refusal_reason = _DETACHED_HEAD_ERROR``); and
* ``GitWriteTool._git_commit`` — the ``write_on_feature_branch`` grant path
  (returns ``_DETACHED_HEAD_ERROR``).

The PLAIN path is NOT covered.  For an ordinary (non-operator-managed)
checkout with a ``git: write`` / ``full`` / ``ask`` session, and for
``GitWriteTool._git_branch_create`` / ``GitWriteTool._git_worktree_add`` with
``base=None``, no detached-HEAD check runs at all.  All three therefore
proceed SILENTLY while HEAD is detached:

* ``_git_commit`` runs ``git add`` + ``git commit`` — on a real repo this
  produces a DANGLING commit (HEAD advances, no branch ref is updated);
* ``_git_branch_create`` resolves ``HEAD^{commit}`` and pins the new branch
  to the detached commit; and
* ``_git_worktree_add`` builds ``base = self.base or "HEAD"`` and runs
  ``git worktree add <path> HEAD`` — creating a linked worktree whose own
  HEAD is left detached at the same commit.

This file pins the MISSING fail-closed behaviour: while HEAD is detached,
``commit`` (primary), ``branch_create`` and ``worktree_add`` must refuse
LOUDLY with a stable, named error mentioning ``detached`` — never silently
create a dangling commit, pin a branch to the detached commit, or spin up a
detached linked worktree.

CLASSIFICATION (B1.2 — see the appended section of the RED transcript):
``worktree_add`` is IN-CLASS — its target is HEAD-resolved (``base =
self.base or "HEAD"`` feeds ``git worktree add <path> <base>``).
``stash_push`` / ``stash_pop`` are OUT-OF-CLASS — their targets are the
index/worktree changes and the stash reflog entry (``stash@{index}``);
neither resolves a base from HEAD, so they are deliberately NOT expanded here.

The tool's git execution seam (``_run_git`` / ``_run_git_raw``) is faked
rather than driven through a real ``git`` binary: this sandbox image has NO
``git`` executable (``git --version`` -> ``command not found``), so a
subprocess-driven setup would error during fixture construction for a reason
UNRELATED to the defect.  Faking the seam keeps the RED a genuine assertion
failure attributable solely to the missing detached-HEAD gate.  The fake
mirrors the exact probe the existing gates use, so a fix built on
``rev-parse --abbrev-ref HEAD`` -> ``"HEAD"`` turns this GREEN.

EXPECTED TODAY: RED (all three parametrised cases: commit, branch_create,
worktree_add).
"""

import pytest

from tools.git_write_tool import GitWriteTool

# A full-write session (NOT write_on_feature_branch): the plain commit path.
_WRITE_GRANT = {"session_permissions": {"git": "write"}}


def _tool(**overrides):
    params = {"agent_config": _WRITE_GRANT}
    params.update(overrides)
    return GitWriteTool(**params)


def _detached_run_git(recorder):
    """Fake ``_run_git`` reporting a DETACHED HEAD via the canonical probe."""

    def fake_run_git(repo_root, args, timeout=30):
        recorder.append(list(args))
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            # Detached HEAD: rev-parse --abbrev-ref HEAD prints "HEAD".
            return "HEAD\n"
        return "ok\n"

    return fake_run_git


def _detached_run_git_raw(recorder, sha="a" * 40):
    """Fake ``_run_git_raw``: HEAD^{commit} resolves to a fixed SHA."""

    def fake_run_git_raw(repo_root, args, timeout=30):
        recorder.append(list(args))
        if args[:2] == ["rev-parse", "--verify"]:
            return (0, sha + "\n", "")
        return (0, "ok\n", "")

    return fake_run_git_raw


@pytest.mark.parametrize(
    "operation", ["commit", "branch_create", "worktree_add"]
)
def test_detached_head_fails_closed(tmp_path, operation):
    """A detached HEAD must refuse `commit`, `branch_create` and `worktree_add`.

    The repo root is an ORDINARY checkout (``_is_operator_managed_worktree``
    -> False) and the grant is plain ``git: write`` (not
    ``write_on_feature_branch``), so neither of the two existing detached-HEAD
    gates runs: this exercises exactly the uncovered PLAIN path.
    """
    (tmp_path / "note.txt").write_text("x\n")

    calls = []
    if operation == "commit":
        tool = _tool(
            operation="commit",
            message="agent commit",
            file_path=["note.txt"],
        )
    elif operation == "branch_create":
        tool = _tool(operation="branch_create", branch="feat/new")
    else:
        # worktree_add defaults base -> "HEAD": a HEAD-resolved target.
        tool = _tool(operation="worktree_add", path="wt-new")

    tool._is_operator_managed_worktree = lambda root: False  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _detached_run_git(calls)  # noqa: SLF001
    tool._run_git_raw = _detached_run_git_raw(calls)  # noqa: SLF001

    if operation == "commit":
        result = tool._git_commit(tmp_path)
    elif operation == "branch_create":
        result = tool._git_branch_create(tmp_path)
    else:
        result = tool._git_worktree_add(tmp_path)

    # LOUD + NAMED: a human-readable refusal that names the detached HEAD.
    assert "detached" in result.lower(), (
        f"'{operation}' must refuse a detached HEAD with a loud named error; "
        f"got: {result!r}"
    )

    # No silent provenance while detached: never commit, never pin a branch,
    # never spin up a linked worktree off the detached HEAD.
    if operation == "commit":
        assert not any(argv[:1] == ["commit"] for argv in calls), calls
    elif operation == "branch_create":
        assert not any(argv[:1] == ["branch"] for argv in calls), calls
    else:
        assert not any(argv[:2] == ["worktree", "add"] for argv in calls), calls
