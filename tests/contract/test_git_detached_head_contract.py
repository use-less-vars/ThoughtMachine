"""Contract test (REAL ``git``) -- B1: a detached HEAD fails closed on write.

Complement to ``tests/test_git_detached_head_fail_closed.py``.  That file FAKES
the git execution seam because this CI sandbox image ships NO ``git`` binary;
THIS file drives the *real* ``git`` executable against a throwaway repository so
the fail-closed contract is verified end to end against genuine git output.

What is pinned here (the gate probe is ``git rev-parse --abbrev-ref HEAD``,
which prints the literal string ``"HEAD"`` when HEAD is detached):

* ``commit`` must refuse LOUDLY (an error naming the detached HEAD) instead of
  producing a DANGLING commit (HEAD advancing with no branch ref updated);
* ``branch_create`` must refuse instead of resolving ``HEAD^{commit}`` and
  silently pinning a new branch to the detached commit; and
* ``worktree_add`` (default base ``HEAD``) must refuse instead of running
  ``git worktree add <path> HEAD`` and creating a linked worktree whose own HEAD
  is left detached at the same commit.

HOW THE REAL BACKEND IS REACHED (no monkeypatch)
------------------------------------------------
The tool is driven through its *own* execution path -- there is no
monkeypatching of ``_run_git_raw`` or any other seam.  Reaching genuine git
requires two things that the plain ``session_id`` + ``workspace_path``
construction cannot supply:

1. a bound ``workspace_id`` (a real pydantic field), because
   ``_host_execution_denied_reason`` DENIES host execution when no workspace id
   is bound ("...no workspace id is bound to this session...") -- with only
   ``session_id`` + ``workspace_path`` every ``_run_git_raw`` call raises
   ``RuntimeError`` and the detached gate never fires; and
2. ``THOUGHTMACHINE_VAULT_ROOT`` pointing at a throwaway vault whose
   ``workspaces/<id>/config.json`` carries ``{"allow_host_resources": true}``,
   which is the only non-monkeypatch way to make
   ``tools.host_resource_policy.workspace_allows_host_resources(<id>)`` return
   True and clear the host-resource kill switch.

With both in place ``_use_container_mode()`` is still False (no registry-derived
workspace id), so ``_run_git_raw`` dispatches to the hardened HOST backend
(``_exec_host_raw`` -> ``SandboxedExecution``).  The env var is set directly via
``os.environ`` -- the ``monkeypatch`` fixture is deliberately NOT used.

Because the host backend runs git under a fixed PATH and captures no argv, the
"no mutating argv was issued" claim is asserted by EFFECT: the repository state
(HEAD sha, branch set, absence of the would-be worktree directory) is unchanged
after the refused call.

The whole module is SKIPPED (collected, not errored) when no ``git`` binary is
present; see ``tests/integration/test_server_health.py`` for the same skip
pattern.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.git_write_tool import GitWriteTool

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in CI sandbox",
)

# A full-write session (NOT write_on_feature_branch): the PLAIN commit path.
_WRITE_GRANT = {"session_permissions": {"git": "write"}}
# Throwaway vault workspace id the tool is bound to.
_WORKSPACE_ID = "wstest"


def _git(repo: Path, *args: str) -> str:
    """Run the REAL git binary directly (test harness, not the tool)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


@pytest.fixture
def host_git_backend(tmp_path):
    """Bind a throwaway vault that PERMITS host-resource git execution.

    Points ``THOUGHTMACHINE_VAULT_ROOT`` at a temp vault containing
    ``workspaces/<_WORKSPACE_ID>/config.json == {"allow_host_resources": true}``
    so ``_host_execution_denied_reason()`` returns ``None`` and the real host
    git backend is reachable.  The env var is set through ``os.environ`` (NOT
    the ``monkeypatch`` fixture) and the prior value is restored on teardown.
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


def _tool(workspace_id: str, repo: Path, **overrides) -> GitWriteTool:
    """Build a real-backed tool bound to the vault workspace + throwaway repo."""
    params = {
        "workspace_id": workspace_id,
        "session_id": "sess-contract",
        "workspace_path": str(repo),
        "session_permissions": {"git": "write"},
        "agent_config": _WRITE_GRANT,
    }
    params.update(overrides)
    return GitWriteTool(**params)


@pytest.fixture
def detached_repo(tmp_path: Path) -> Path:
    """A real repo with one commit and HEAD detached at that commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "note.txt").write_text("x\n")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-q", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "--detach", head)
    # Sanity: real git reports the literal "HEAD" on the probe.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    return repo


@pytest.fixture
def attached_repo(tmp_path: Path) -> Path:
    """A real repo with one commit on a normal (non-protected) branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feat/base")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "note.txt").write_text("x\n")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _snapshot(repo: Path) -> tuple:
    """Repository state used to prove no mutating git command took effect."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    branches = sorted(_git(repo, "branch", "--format=%(refname:short)").split())
    return head, tuple(branches)


def test_probe_reports_real_detached_head(host_git_backend, detached_repo):
    """The gate probe parses REAL git output: rev-parse -> literal ``HEAD``."""
    tool = _tool(
        host_git_backend, detached_repo,
        operation="commit", message="m", file_path=["note.txt"],
    )
    assert tool._is_detached_head(detached_repo) is True


def test_probe_reports_real_attached_branch(host_git_backend, attached_repo):
    """Control: a real branch name is not mistaken for a detached HEAD."""
    tool = _tool(
        host_git_backend, attached_repo,
        operation="commit", message="m", file_path=["note.txt"],
    )
    assert tool._is_detached_head(attached_repo) is False


@pytest.mark.parametrize("operation", ["commit", "branch_create", "worktree_add"])
def test_detached_head_refuses_with_real_git(
    host_git_backend, detached_repo, operation
):
    """A detached HEAD must refuse all three mutating entry points LOUDLY.

    The tool's REAL backend runs the probe; the refusal is proven by the
    returned message AND by the unchanged repository state (no mutating argv
    ever took effect).
    """
    before = _snapshot(detached_repo)

    if operation == "commit":
        tool = _tool(
            host_git_backend, detached_repo,
            operation="commit", message="agent commit", file_path=["note.txt"],
        )
    elif operation == "branch_create":
        tool = _tool(
            host_git_backend, detached_repo,
            operation="branch_create", branch="feat/new",
        )
    else:
        # worktree_add defaults base -> "HEAD": a HEAD-resolved target.
        tool = _tool(
            host_git_backend, detached_repo,
            operation="worktree_add", path="wt-new",
        )

    # Drive the tool's OWN (real, non-monkeypatched) execution path.
    result = tool.execute()

    # LOUD + NAMED: a human-readable refusal that names the detached HEAD.
    assert "detached" in result.lower(), (
        f"'{operation}' must refuse a detached HEAD with a loud named error; "
        f"got: {result!r}"
    )

    # EFFECT: no branch-mutating command took effect while HEAD was detached.
    assert _snapshot(detached_repo) == before, (
        f"'{operation}' changed repository state despite the detached HEAD"
    )
    assert _git(detached_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    if operation == "branch_create":
        assert _git(detached_repo, "branch", "--list", "feat/new").strip() == ""
    elif operation == "worktree_add":
        assert not (detached_repo / "wt-new").exists()


def test_out_of_workspace_path_refused_before_any_git(
    host_git_backend, attached_repo, tmp_path
):
    """An OUT-OF-WORKSPACE ``working_dir`` is refused by PATH VALIDATION.

    The bound repository is ``attached_repo`` (HEAD is on a real branch, NOT
    detached); ``working_dir`` is pointed at a sibling directory OUTSIDE the
    workspace.  ``_validate_path`` must reject it up front -- BEFORE the
    detached-HEAD probe and BEFORE any mutating git argv -- for all three
    mutating entry points.  This is the counterpart of the detached-HEAD
    contract: the refusal is a *path* error, NOT the detached-HEAD gate and
    NOT a successful write.

    As with the rest of this module the tool is driven through its OWN real
    execution path (no monkeypatch); the "no mutating argv was issued" claim
    is asserted by EFFECT via the unchanged repository state.
    """
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    # Sanity: the selected working_dir is genuinely OUTSIDE the bound repo.
    assert outside != attached_repo
    assert not str(outside).startswith(str(attached_repo) + os.sep)

    operations = {
        "commit": {
            "operation": "commit",
            "message": "agent commit",
            "file_path": ["note.txt"],
        },
        "branch_create": {
            "operation": "branch_create",
            "branch": "feat/new",
            "base": None,
        },
        "worktree_add": {"operation": "worktree_add", "path": "wt-new"},
    }

    for name, overrides in operations.items():
        before = _snapshot(attached_repo)
        tool = _tool(
            host_git_backend, attached_repo,
            working_dir=str(outside), **overrides,
        )

        result = tool.execute()

        # PATH VALIDATION fired (the byte-exact security message).
        assert "is outside workspace" in result, (
            f"'{name}' must be refused by path validation; got: {result!r}"
        )
        # A refusal, not a success.
        assert result.startswith("Error:"), (
            f"'{name}' must return an error, not a success; got: {result!r}"
        )
        # NOT the detached-HEAD gate (HEAD is attached here).
        assert "detached" not in result.lower(), (
            f"'{name}' must fail on PATH validation, not the detached-HEAD "
            f"gate; got: {result!r}"
        )

        # EFFECT: no commit/branch/worktree argv ever took effect.
        assert _snapshot(attached_repo) == before, (
            f"'{name}' changed repository state despite the path refusal"
        )

    # No branch was created and no linked worktree directory appeared.
    assert _git(attached_repo, "branch", "--list", "feat/new").strip() == ""
    assert not (attached_repo / "wt-new").exists()

