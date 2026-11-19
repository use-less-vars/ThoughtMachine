"""Contract test (REAL ``git``) -- a worker worktree is on a BRANCH, not detached.

The unit-level pin lives in ``tests/test_worktree_defaults.py`` (it FAKES the
injectable ``git_runner`` seam because this CI sandbox image ships NO ``git``
binary).  THIS file drives the *real* ``git`` executable against a throwaway
repository, so the two claims that only genuine git can settle are settled here:

* ``resolve_worker_worktree_root`` produces ``git worktree add -b wt/<session>``
  and real git ACCEPTS it -- inside the new worktree
  ``git rev-parse --abbrev-ref HEAD`` prints ``wt/sess-1``, not the literal
  ``HEAD`` that means detached; and
* a ``GitWriteTool`` commit executed INSIDE that worktree is not refused as a
  detached-HEAD write (the refusal text never appears, and the commit actually
  lands on ``wt/sess-1``).

The widened side effect of the branch (the ref is written into the SHARED
repository, so it is visible from the main checkout) is pinned too.

WHICH RUNNER
------------
The resolver's DEFAULT runner (``_host_git_runner``) reaches git through
``host_run()``, which needs a vault-registered workspace id (the repo here is a
``tmp_path`` throwaway) plus a ``allow_host_resources`` grant.  That path and its
refusal fallback are covered by the unit tests; here the resolver is driven
through its documented injectable seam with a runner that EXECUTES REAL GIT, so
what is under test is the argv the resolver builds and git's verdict on it.

The whole module is SKIPPED (collected, not errored) when no ``git`` binary is
present; see ``tests/integration/test_server_health.py`` for the same pattern.
Any WRONG behaviour fails -- a skip never stands in for a pass.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.git_write_tool import GitWriteTool
from tools.workspace.worktree_defaults import (
    resolve_worker_worktree_root,
    worktree_branch_name,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in CI sandbox",
)

# A full-write grant (NOT write_on_feature_branch): the PLAIN commit path.
# Seeded into BOTH ``session_permissions`` and ``effective_permissions``
# because the ToolExecutor injects BOTH on every real call.
_WRITE_GRANT = {"git": "write", "filesystem": "write"}
# Throwaway vault workspace id the commit tool is bound to.
_WORKSPACE_ID = "wstest"

_SESSION = "sess-1"
_BRANCH = "wt/sess-1"


def _git(cwd, *args: str) -> str:
    """Run the REAL git binary directly (test harness, not the tool)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _real_runner(argv, cwd) -> int:
    """A ``git_runner`` that executes the REAL git binary; returns its status."""
    proc = subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=False
    )
    return proc.returncode


@pytest.fixture
def host_git_backend(tmp_path):
    """Bind a throwaway vault that PERMITS host-resource git execution.

    Points ``THOUGHTMACHINE_VAULT_ROOT`` at a temp vault containing
    ``workspaces/<_WORKSPACE_ID>/config.json == {"allow_host_resources": true}``
    so ``_host_execution_denied_reason()`` returns ``None`` and the real host
    git backend of ``GitWriteTool`` is reachable.
    """
    vault = tmp_path / "vault"
    ws_dir = vault / "workspaces" / _WORKSPACE_ID
    ws_dir.mkdir(parents=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"allow_host_resources": True}), encoding="utf-8"
    )
    prior = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    os.environ["THOUGHTMACHINE_VAULT_ROOT"] = str(vault)
    try:
        yield _WORKSPACE_ID
    finally:
        if prior is None:
            os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)
        else:
            os.environ["THOUGHTMACHINE_VAULT_ROOT"] = prior


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repo with one commit on a normal (non-protected) branch."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "feat/base")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "note.txt").write_text("x\n")
    _git(root, "add", "note.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _default_worktree(repo: Path) -> str:
    worktree = resolve_worker_worktree_root(
        str(repo), _SESSION, git_runner=_real_runner
    )
    assert worktree is not None, (
        "the real git binary must accept 'worktree add -b wt/sess-1 <path>'; "
        "the resolver fell back to the workspace root instead"
    )
    return worktree


def test_the_default_worktree_is_checked_out_on_its_branch(repo):
    """Real git puts the worktree on ``wt/sess-1``, NOT on a detached HEAD."""
    assert worktree_branch_name(_SESSION) == _BRANCH

    worktree = _default_worktree(repo)

    head = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert head != "HEAD", (
        "the default worktree is DETACHED, so every worker commit would dangle"
    )
    assert head == _BRANCH, f"the worktree is not on its branch; got {head!r}"

    # The branch ref lives in the SHARED repository: visible from the main
    # checkout, and therefore pushable from there.
    in_main = _git(
        repo, "branch", "--list", "--format=%(refname:short)", _BRANCH
    ).strip()
    assert in_main == _BRANCH, (
        f"the branch {_BRANCH!r} the worktree is on is not visible from the "
        f"main checkout; got {in_main!r}"
    )


def test_a_commit_in_the_default_worktree_is_not_refused_as_detached(
    host_git_backend, repo
):
    """The point of the branch: the worker can actually COMMIT in its worktree."""
    worktree = _default_worktree(repo)
    (Path(worktree) / "worker.txt").write_text("worker\n")

    tool = GitWriteTool(
        workspace_id=host_git_backend,
        session_id=_SESSION,
        workspace_path=worktree,
        # BOTH permission attributes: the ToolExecutor injects the raw session
        # profile AND the session x ceiling view as distinct objects.
        session_permissions=_WRITE_GRANT,
        effective_permissions=_WRITE_GRANT,
        operation="commit",
        message="worker commit",
        file_path=["worker.txt"],
    )

    result = tool.execute()

    assert "detached" not in result.lower(), (
        "a commit in the default worktree was refused as a detached-HEAD write: "
        f"{result!r}"
    )
    # EFFECT: the commit really landed on the worktree's branch.
    assert _git(worktree, "log", "-1", "--pretty=%s").strip() == "worker commit"
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == _BRANCH
