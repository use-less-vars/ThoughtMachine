"""Does the host git kill-switch actually BITE? (fail-open regression tests)

THREAT
------
Host execution mode runs git with hooks neutralized
(``core.hooksPath=/dev/null`` + ``--no-verify``). That is a deliberate,
documented bypass, and it is only acceptable because the tool FAILS CLOSED
whenever the repository it is asked to commit into HAS hooks configured:
``<workspace>/.githooks/pre-commit`` present -> the commit is refused with a
named error and no git subprocess is spawned at all.

The attack this file pins down: an agent (or anything else with workspace
write access) drops a ``.githooks/pre-commit`` into the workspace and then
obtains a commit anyway. If the refusal were missing, silently bypassed, or
bypassed by the hook-path override, that hook would be a policy-owned file
that the platform claims to honour while the platform in fact neutralizes it
-- i.e. the kill-switch would be advertised but not biting. So the contract
proven here is bidirectional and must be proven BOTH ways:

* (a) hooks configured  -> commit REFUSED, HEAD does not move, hook never runs;
* (b) hooks absent      -> the SAME commit SUCCEEDS, HEAD advances.
  (b) is mandatory: without it, (a) would also pass if the tool simply
  refused/aborted every host commit for an unrelated reason, and the test
  would prove nothing about the switch.

PROVENANCE
----------
* docs/machine_history_and_vision.md section 3.3: the host path neutralizes
  hooks and host mode REFUSES when ``<workspace>/.githooks/pre-commit``
  exists -- fail closed only when hooks are configured-but-unenforceable,
  never a blanket ban on host git; "a commit timeout is not a pass".
* docs/MANDATORY_STOP.md: never pass ``--no-verify`` to satisfy a check,
  never disable/edit/move a host hook; "dormant is a claim about one shell,
  not about the machine" (``.githooks/pre-commit`` was deleted in 85035605 --
  which says nothing about what a later workspace contains).
* Implementation under test: ``GitReadTool._exec_host_raw`` in
  tools/git_info_tool.py -- the refusal is inline, before
  ``SandboxedExecution`` is constructed and before any ``git`` process.

HONESTY ABOUT WHAT RAN
----------------------
Tests (a)/(b) need a real git binary to build a repository and to observe
HEAD move. When git is not installed they are SKIPPED with a reason that says
plainly that the guarantee is UNPROVEN on this machine -- a skip is not a
pass, and this file never reports one as the other.

Tests (c) need NO git binary: they exercise the refusal/fall-through guard
itself by pinning ``SandboxedExecution``, so they RUN everywhere -- including
sandboxes without git -- and the refusal branch is genuinely executed there.
That is the point: the switch must bite on the machine that has no git, too.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool
from tools.git_write_tool import GitWriteTool

# The workspace the host-path helpers bind so the workspace ceiling admits
# host git; its ``allow_host_resources`` config is provisioned by the autouse
# fixture below (mirrors tests/security/test_git_hardening.py).
HOST_TEST_WS = "host-test-ws"

GIT = shutil.which("git")

requires_git = pytest.mark.skipif(
    GIT is None,
    reason=(
        "git binary not available: this test needs real git to build a "
        "repository and to observe HEAD move; the host-switch guarantee is "
        "UNPROVEN on this machine and proven only on a runner that has git "
        "-- SKIPPED is not PASSED"
    ),
)

# The full documented refusal (byte-exact copy of the string returned by
# GitReadTool._exec_host_raw). Asserted verbatim so a reworded refusal cannot
# silently keep a loose "startswith" assertion green.
REFUSAL = (
    "Error: refusing commit: repository hooks are configured "
    "(.githooks/pre-commit) but host execution mode neutralizes "
    "them (core.hooksPath=/dev/null, --no-verify); hooks cannot "
    "be enforced on the host. Use container mode, or remove the "
    "configured hooks to accept the bypass."
)


@pytest.fixture(autouse=True)
def _allow_host_resources_gate(tmp_path, monkeypatch):
    """Provision an ``allow_host_resources`` config for ``HOST_TEST_WS``.

    Host-side git is fail-CLOSED on an unbound workspace id, so the tools here
    bind ``HOST_TEST_WS`` and this fixture gives that workspace an
    ``allow_host_resources: true`` config. Without it every call below would
    be denied by the host kill switch *before* reaching the hook guard, and
    the tests would be asserting about the wrong gate.
    """
    vault = tmp_path / "_host_gate_vault"
    cfg_dir = vault / "workspaces" / HOST_TEST_WS
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"allow_host_resources": True}), encoding="utf-8"
    )
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))


def _run_git_clean(cwd: Path, *args) -> subprocess.CompletedProcess:
    """Run git with a sanitized environment (mirrors the tool's hardening)."""
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["HOME"] = "/dev/null"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def _git_ok(cwd: Path, *args) -> str:
    proc = _run_git_clean(cwd, *args)
    assert proc.returncode == 0, (
        f"setup: git {' '.join(args)} failed in {cwd}: "
        f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return proc.stdout


def _workspace_with_rogue_hook(tmp_path):
    """Workspace + repo whose policy-grained hook dir holds a hostile hook.

    Returns ``(workspace, repo, marker)``. ``marker`` lives inside the repo, so
    the hook -- if it ever executed -- would be trivially observable.
    """
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _git_ok(repo, "init", "-q")
    # Repo-LOCAL identity: the tool ignores ambient/global config, so commits
    # must not depend on it.
    _git_ok(repo, "config", "user.name", "Test User")
    _git_ok(repo, "config", "user.email", "test@example.com")
    (repo / "hello.txt").write_text("hi\n")
    _git_ok(repo, "add", "hello.txt")
    _git_ok(repo, "commit", "-m", "base commit")

    githooks = workspace / ".githooks"
    githooks.mkdir(parents=True, exist_ok=True)
    hook = githooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho PWNED > marker.txt\n")
    hook.chmod(0o111)
    return workspace, repo, repo / "marker.txt"


def _commit_tool(workspace, repo, message="host switch commit", file_path="hello.txt"):
    """A commit tool bound to the direct ``workspace_path=`` (host) path."""
    return GitWriteTool(
        operation="commit",
        message=message,
        file_path=file_path,
        working_dir=str(repo),
        workspace_path=str(workspace),
        workspace_id=HOST_TEST_WS,
        session_permissions={"git": "write", "filesystem": "write"},
        effective_permissions={"git": "write", "filesystem": "write"},
    )


# ---------------------------------------------------------------------------
# (a) hooks configured -> the host commit is REFUSED
# ---------------------------------------------------------------------------


@requires_git
def test_host_commit_is_refused_when_githooks_pre_commit_present(tmp_path):
    """A configured ``.githooks/pre-commit`` makes the host commit fail closed.

    Refusal means: the named error is surfaced to the caller, HEAD does NOT
    move, and the hook provably never ran.
    """
    workspace, repo, marker = _workspace_with_rogue_hook(tmp_path)
    head_before = _git_ok(repo, "rev-parse", "HEAD").strip()

    result = _commit_tool(workspace, repo).execute()

    assert REFUSAL in result, f"expected the documented refusal, got: {result!r}"
    assert "host switch commit" not in _git_ok(repo, "log", "--format=%s")
    head_after = _git_ok(repo, "rev-parse", "HEAD").strip()
    assert head_after == head_before, "HEAD moved although the commit was refused"
    assert not marker.exists(), (
        "the configured .githooks/pre-commit EXECUTED on the host path: the "
        "kill switch advertises hooks it then neutralizes"
    )


# ---------------------------------------------------------------------------
# (b) hooks absent -> the SAME commit SUCCEEDS (non-vacuity control)
# ---------------------------------------------------------------------------


@requires_git
def test_control_same_commit_succeeds_without_hooks(tmp_path):
    """Control: identical setup, no ``.githooks/pre-commit`` -> commit lands.

    Without this, test (a) would also pass if the tool refused every host
    commit for some unrelated reason, proving nothing about the switch.
    """
    workspace, repo, marker = _workspace_with_rogue_hook(tmp_path)
    # Drop the configured hook: same workspace, same tool, no hooks configured.
    (workspace / ".githooks" / "pre-commit").unlink()
    head_before = _git_ok(repo, "rev-parse", "HEAD").strip()

    result = _commit_tool(workspace, repo).execute()

    assert "refusing commit" not in result, f"unexpected refusal: {result!r}"
    assert "Git command failed" not in result, f"commit failed: {result!r}"
    head_after = _git_ok(repo, "rev-parse", "HEAD").strip()
    assert head_after != head_before, "HEAD did not advance: nothing was proven"
    assert "host switch commit" in _git_ok(repo, "log", "--format=%s")
    assert not marker.exists(), "no hook was configured, yet a marker appeared"


# ---------------------------------------------------------------------------
# (c) the guard itself, with NO git binary required (runs everywhere)
# ---------------------------------------------------------------------------


class _RecordingExecutor:
    """``SandboxedExecution`` stand-in that records argv instead of running git."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.argv = None
        type(self).instances.append(self)

    def run(self, argv, **kwargs):
        self.argv = list(argv)
        self.run_kwargs = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class _ForbiddenExecutor:
    """``SandboxedExecution`` stand-in that fails if the guard ever reaches it."""

    constructed = 0

    def __init__(self, **kwargs):
        type(self).constructed += 1
        raise AssertionError(
            "executor must not be constructed when hooks are configured: the "
            "host kill switch has to refuse BEFORE any git machinery is built"
        )


def _guard_tool(workspace):
    # ``commit`` is a GitWriteTool operation; the guard itself lives in the
    # shared GitReadTool._exec_host_raw that GitWriteTool inherits.
    tool = GitWriteTool(
        operation="commit",
        message="x",
        session_permissions={"git": "write", "filesystem": "write"},
        effective_permissions={"git": "write", "filesystem": "write"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(workspace))
    return tool


def test_guard_refuses_without_any_git_invocation(tmp_path, monkeypatch):
    """Hooks configured -> (1, "", refusal) with NO executor and NO git.

    Deliberately needs no git binary: if the refusal only existed behind a
    working git, a machine without git could not be shown to fail closed.
    """
    workspace = tmp_path / "workspace"
    (workspace / ".githooks").mkdir(parents=True)
    (workspace / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    repo = workspace / "repo"
    repo.mkdir()

    _ForbiddenExecutor.constructed = 0
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _ForbiddenExecutor
    )

    rc, out, err = _guard_tool(workspace)._exec_host_raw(
        repo, ["commit", "-m", "x"]
    )

    assert (rc, out) == (1, ""), f"expected a fail-closed tuple, got {(rc, out, err)!r}"
    assert err == REFUSAL, f"refusal is no longer byte-identical: {err!r}"
    assert _ForbiddenExecutor.constructed == 0, "an executor was constructed"


def test_guard_permits_when_no_hooks_configured(tmp_path, monkeypatch):
    """No hooks -> the guard falls through to the HARDENED host argv.

    Asserts the second half of the contract: the refusal is scoped to
    configured hooks, and what it lets through is the neutralized invocation
    (``--no-verify`` + ``core.hooksPath=/dev/null``), not an unhardened one.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = workspace / "repo"
    repo.mkdir()

    _RecordingExecutor.instances = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _RecordingExecutor
    )

    rc, out, err = _guard_tool(workspace)._exec_host_raw(
        repo, ["commit", "-m", "x"]
    )

    assert rc == 0 and out == "ok", f"guard did not fall through: {(rc, out, err)!r}"
    assert "refusing commit" not in (err or "")
    assert len(_RecordingExecutor.instances) == 1, "git was never dispatched"
    argv = _RecordingExecutor.instances[0].argv
    assert argv[0] == "git"
    assert "commit" in argv and "--no-verify" in argv
    assert "core.hooksPath=/dev/null" in argv
    # The hardening pins precede the subcommand (the only position git honours).
    assert argv.index("core.hooksPath=/dev/null") < argv.index("commit")


def test_guard_does_not_refuse_non_commit_operations(tmp_path, monkeypatch):
    """Hooks configured must not become a blanket ban on host git.

    Section 3.3 is explicit: fail closed only when hooks are
    configured-but-unenforceable -- reads and other operations keep working.
    """
    workspace = tmp_path / "workspace"
    (workspace / ".githooks").mkdir(parents=True)
    (workspace / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    repo = workspace / "repo"
    repo.mkdir()

    _RecordingExecutor.instances = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _RecordingExecutor
    )

    tool = GitInfoTool(
        operation="status",
        session_permissions={"git": "read", "filesystem": "read"},
        effective_permissions={"git": "read", "filesystem": "read"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(workspace))

    rc, out, err = tool._exec_host_raw(repo, ["status"])

    assert rc == 0 and out == "ok", f"non-commit op was refused: {(rc, out, err)!r}"
    assert len(_RecordingExecutor.instances) == 1
    argv = _RecordingExecutor.instances[0].argv
    assert argv[-1] == "status" and "--no-verify" not in argv
