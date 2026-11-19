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

CONSTRUCTION PATHS (why the guard had to be re-armed)
----------------------------------------------------
The guard used to read ``self._resolved_workspace_path`` and nothing else, but
``GitReadTool.execute()`` RESETS that attribute on entry and only re-populates
it from a session-registry lookup. Every caller that binds the deprecated
``workspace_path=`` (which is exactly what ``ToolExecutor`` injects from
``config.workspace_path``) therefore reached the guard with the attribute
``None``: the guard was consulted but DISARMED, and the commit went out with
``core.hooksPath=/dev/null`` + ``--no-verify``.
``GitReadTool._host_commit_hook_dirs`` fixes that by resolving the workspace at
commit time with one fixed precedence -- an explicitly bound ``workspace_path``
FIRST (the caller's declaration of which workspace this commit belongs to),
falling back to ``_resolve_workspace_root`` (worktree root -> registered
workspace -> deprecated ``workspace_path``) -- and by honouring ``repo_root``
and the directories between it and the workspace root. The declared root
deliberately beats the narrower ``worktree_root``: security code must never
re-target itself at a different repository than the one named, and the wider
root is strictly more fail-closed because the ancestor walk still reaches a
``.githooks`` inside a narrower worktree.
``test_refusal_armed_on_every_construction_path*`` below pins that fix on EVERY
construction path production code uses, not just the one that happened to arm
the attribute -- otherwise a test could pass while a sibling path stays
fail-open.
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
    # Repo-LOCAL identity, written straight into ``.git/config`` (no git
    # subprocess just to configure a commit): the tool ignores ambient/global
    # config (GIT_CONFIG_GLOBAL=/dev/null), so the commit identity has to be
    # repo-local or the tool's commit fails for a reason that has nothing to do
    # with the hook guard.
    git_config = repo / ".git" / "config"
    git_config.write_text(
        git_config.read_text(encoding="utf-8")
        + "[user]\n\tname = TM Test\n\temail = tm@example.invalid\n",
        encoding="utf-8",
    )
    (repo / "hello.txt").write_text("hi\n")
    _git_ok(repo, "add", "hello.txt")
    _git_ok(repo, "commit", "-m", "base commit")
    # Make the working tree DIRTY after the base commit. Without this the tool's
    # commit has nothing to record, git exits 1 with EMPTY stderr ("nothing to
    # commit" goes to stdout) and the control test would fail -- or, worse, pass
    # its "no failure" assertion while proving nothing about committing.
    (repo / "hello.txt").write_text("hi\nsecond line\n")

    githooks = workspace / ".githooks"
    githooks.mkdir(parents=True, exist_ok=True)
    hook = githooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho PWNED > marker.txt\n")
    hook.chmod(0o111)
    return workspace, repo, repo / "marker.txt"


def _commit_kwargs(workspace, repo, message="host switch commit", file_path="hello.txt"):
    """Arguments of a commit tool bound to the deprecated ``workspace_path=``.

    ``workspace_path`` is an INFRASTRUCTURE field: ``ToolExecutor`` always sets
    it from ``config.workspace_path`` (agent/core/tool_executor.py:530), and no
    session-registry entry is involved on that path -- which is exactly why
    ``execute()`` leaves ``_resolved_workspace_path`` unset and why the guard
    used to be armed only by accident.
    """
    return {
        "operation": "commit",
        "message": message,
        "file_path": file_path,
        "working_dir": str(repo),
        "workspace_path": str(workspace),
        "workspace_id": HOST_TEST_WS,
        "session_permissions": {"git": "write", "filesystem": "write"},
        "effective_permissions": {"git": "write", "filesystem": "write"},
    }


def _commit_tool(workspace, repo, message="host switch commit", file_path="hello.txt"):
    """A commit tool bound to the direct ``workspace_path=`` (host) path."""
    return GitWriteTool(**_commit_kwargs(workspace, repo, message, file_path))


def _executor_injected_kwargs(workspace, repo):
    """The kwargs ``ToolExecutor._execute_single_tool`` actually builds.

    Mirrors agent/core/tool_executor.py: ``workspace_path`` comes from
    ``config.workspace_path`` (L530), ``session_permissions`` from the resolved
    profile (L737-741), ``workspace_id`` from the gate (L746), and
    ``session_id`` is injected only because the tool declares the field
    (L799-800) -- empty here, i.e. a sessionless caller, which is the case that
    leaves the registry lookup with nothing to return.
    """
    kwargs = _commit_kwargs(workspace, repo)
    kwargs["tool"] = "GitWriteTool"
    kwargs["session_id"] = ""
    return kwargs


def _construction_paths(workspace, repo):
    """Every distinct way production code builds a git commit tool.

    Factories, so one path's per-call state can never leak into the next.
    """
    kwargs = _commit_kwargs(workspace, repo)
    return {
        "direct_constructor": lambda: GitWriteTool(**kwargs),
        "pydantic_model_validate": lambda: GitWriteTool.model_validate(dict(kwargs)),
        "tool_executor_injection": lambda: GitWriteTool(
            **_executor_injected_kwargs(workspace, repo)
        ),
    }


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
# (a2) workspace PRECEDENCE: the declared workspace outranks ambient discovery
# ---------------------------------------------------------------------------


def test_declared_workspace_beats_a_narrower_worktree_root(tmp_path, monkeypatch):
    """An explicitly bound ``workspace_path`` must not be narrowed or replaced.

    ``_resolve_workspace_root()`` prefers ``worktree_root`` over the declared
    ``workspace_path``. That is right for path-anchored file tools but wrong
    here: resolving through it would judge a commit into the DECLARED workspace
    against a different directory, so a hook configured for the workspace could
    be missed -- and, on a git-equipped runner, a worktree-bound session could
    manufacture a refusal out of another checkout's hook. The declared root is
    also the WIDER one, so honouring it is strictly more fail-closed; phase 2
    pins that a hook in the narrower root is still found by the ancestor walk.
    """
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    (workspace / ".githooks").mkdir()
    (workspace / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    # A narrower root INSIDE the declared workspace, with no hooks of its own.
    narrow = workspace / ".worktrees" / "feat"
    narrow.mkdir(parents=True)

    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _ForbiddenExecutor)
    _ForbiddenExecutor.constructed = 0

    kwargs = dict(_commit_kwargs(workspace, repo))
    kwargs["worktree_root"] = str(narrow)
    tool = GitWriteTool(**kwargs)

    assert tool._resolved_workspace_path is None, (
        "this test is only meaningful while the guard arms itself first"
    )
    rc, out, err = tool._exec_host_raw(repo, ["commit", "-m", "x"])

    assert (rc, out) == (1, ""), (
        f"the guard did not refuse for the DECLARED workspace: {(rc, out, err)!r}"
    )
    assert err == REFUSAL, f"refusal is no longer byte-identical: {err!r}"
    assert Path(tool._resolved_workspace_path).resolve() == workspace.resolve(), (
        "the guard resolved to "
        f"{tool._resolved_workspace_path!r} instead of the declared workspace "
        f"{str(workspace)!r}: it re-targeted itself at a narrower root"
    )
    assert _ForbiddenExecutor.constructed == 0, "an executor was constructed"

    # Phase 2: a hook inside the NARROWER root must still be caught -- the fix
    # must not trade one blind spot for another.
    clean_workspace = tmp_path / "workspace2"
    inner = clean_workspace / ".worktrees" / "feat"
    inner_repo = inner / "repo"
    inner_repo.mkdir(parents=True)
    (inner / ".githooks").mkdir()
    (inner / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")

    kwargs = dict(_commit_kwargs(clean_workspace, inner_repo))
    kwargs["worktree_root"] = str(inner)
    nested = GitWriteTool(**kwargs)
    rc, out, err = nested._exec_host_raw(inner_repo, ["commit", "-m", "x"])

    assert (rc, out) == (1, ""), (
        "a .githooks between repo_root and the declared workspace root was "
        f"MISSED: {(rc, out, err)!r}"
    )
    assert err == REFUSAL


def test_ambient_checkout_does_not_re_target_the_guard(tmp_path, monkeypatch):
    """Another repository on the machine must not decide this commit's verdict.

    The guard resolves its workspace late, and ``_resolve_workspace_root``
    prefers ``worktree_root``/the session registry over the declared
    ``workspace_path``. If that precedence leaked into the guard, a repository
    the process merely HAPPENS to be sitting in (``decoy`` below: a checkout
    with a configured hook, like any real project checkout) could decide the
    verdict for a commit that belongs to a hook-free declared workspace. It
    must not: the declared workspace decides, the decoy is never probed, and
    the only git process started is the commit itself.

    WHY NO PATH SHIM: the most literal "git is installed" world is a fake
    ``git`` early on PATH, but pytest's hermetic HOME/vault lives under a
    ``noexec`` /tmp (``tests/conftest.py`` redirects HOME there at import
    time), so nothing written there can be executed and ``shutil.which`` would
    not even see it. The property is therefore proven at the ONE seam through
    which this module can reach git -- ``SandboxedExecution``, constructed in
    exactly one place in git_info_tool -- where a recorder sees every
    invocation, including a ``rev-parse --show-toplevel`` discovery call. That
    is the same evidence a shim log would give, and it holds on every machine,
    git installed or not.
    """
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)

    decoy = tmp_path / "real-checkout"
    (decoy / ".githooks").mkdir(parents=True)
    (decoy / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")

    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _RecordingExecutor)

    for name, build in _construction_paths(workspace, repo).items():
        _RecordingExecutor.instances = []
        tool = build()

        rc, out, err = tool._exec_host_raw(repo, ["commit", "-m", "x"])

        assert "refusing commit" not in (err or ""), (
            f"{name}: the DECOY checkout's hook manufactured a refusal: "
            f"{(rc, out, err)!r}"
        )
        assert rc == 0 and out == "ok", (
            f"{name}: the guard did not fall through: {(rc, out, err)!r}"
        )
        assert decoy.resolve() not in tool._host_commit_hook_dirs(repo), (
            f"{name}: the guard probed the decoy checkout -- it re-targeted itself"
        )
        assert Path(tool._resolved_workspace_path).resolve() == workspace.resolve(), (
            f"{name}: resolved to {tool._resolved_workspace_path!r} instead of the "
            f"declared workspace {str(workspace)!r}"
        )
        assert len(_RecordingExecutor.instances) == 1, (
            f"{name}: the guard started {len(_RecordingExecutor.instances)} git "
            "processes; only the commit itself may be started"
        )
        argv = _RecordingExecutor.instances[0].argv
        assert "rev-parse" not in argv and "--show-toplevel" not in argv, (
            f"{name}: the guard asked git to discover the repository: {argv!r} -- "
            "that answer can name a different checkout than the declared workspace"
        )


def test_refusal_happens_before_any_git_process(tmp_path, monkeypatch):
    """Hooks in the DECLARED workspace: refused, with no git process started.

    Instrumented rather than stubbed: ``_RecordingExecutor`` would happily run
    the commit, so an EMPTY instance list proves the refusal came BEFORE git
    machinery, not that git happened to fail. A hook-free decoy checkout is
    present to show the refusal is attributable to the declared workspace
    alone -- the decoy cannot cause it and cannot mask it.
    """
    workspace = tmp_path / "workspace"
    (workspace / ".githooks").mkdir(parents=True)
    (workspace / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    repo = workspace / "repo"
    repo.mkdir()

    clean_decoy = tmp_path / "real-checkout"
    clean_decoy.mkdir()

    _RecordingExecutor.instances = []
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _RecordingExecutor)

    for name, build in _construction_paths(workspace, repo).items():
        tool = build()
        assert getattr(tool, "_resolved_workspace_path", None) is None, (
            f"{name}: expected the guard to arm itself"
        )

        rc, out, err = tool._exec_host_raw(repo, ["commit", "-m", "x"])

        assert (rc, out) == (1, ""), (
            f"{name}: expected a fail-closed tuple, got {(rc, out, err)!r}"
        )
        assert err == REFUSAL, f"{name}: refusal is no longer byte-identical: {err!r}"
        assert Path(tool._resolved_workspace_path).resolve() == workspace.resolve(), (
            f"{name}: armed {tool._resolved_workspace_path!r}, not the declared "
            f"workspace {str(workspace)!r}"
        )

    assert _RecordingExecutor.instances == [], (
        "git was started although hooks were configured: the refusal must come "
        "first, not after a git process has run"
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


# ---------------------------------------------------------------------------
# (d) the guard is ARMED on every construction path (the fail-open regression)
# ---------------------------------------------------------------------------


def test_refusal_armed_on_every_construction_path(tmp_path, monkeypatch):
    """Hooks configured -> EVERY construction path refuses, with no git at all.

    This is the regression test for the fail-open: the guard used to read only
    ``self._resolved_workspace_path``, which ``execute()`` resets, so callers
    that bind the deprecated ``workspace_path=`` (what ``ToolExecutor`` injects
    from ``config.workspace_path``) reached the guard DISARMED and the commit
    went out with ``core.hooksPath=/dev/null`` + ``--no-verify``. The assertion
    that the attribute is still ``None`` on entry is the point: the guard must
    arm itself, not wait to be armed.

    Needs no git binary -- and that is deliberate: a machine without git must
    still be shown to fail closed.
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

    paths = _construction_paths(workspace, repo)
    assert set(paths) == {
        "direct_constructor",
        "pydantic_model_validate",
        "tool_executor_injection",
    }

    for name, build in paths.items():
        tool = build()
        assert getattr(tool, "_resolved_workspace_path", None) is None, (
            f"{name}: this construction path is supposed to reach the guard "
            "with NO resolved workspace -- otherwise it does not exercise the "
            "self-arming fix at all"
        )

        rc, out, err = tool._exec_host_raw(repo, ["commit", "-m", "x"])

        assert (rc, out) == (1, ""), (
            f"{name}: expected a fail-closed tuple, got {(rc, out, err)!r} -- "
            "the guard was DISARMED on this construction path"
        )
        assert err == REFUSAL, f"{name}: refusal is no longer byte-identical: {err!r}"
        assert Path(tool._resolved_workspace_path).resolve() == workspace.resolve(), (
            f"{name}: the guard did not arm the workspace it actually probed"
        )

    assert _ForbiddenExecutor.constructed == 0, (
        "an executor was constructed: the refusal must happen before any git "
        "machinery, on every construction path"
    )


@requires_git
def test_refusal_armed_on_every_construction_path_end_to_end(tmp_path):
    """Same guarantee, end to end: the REFUSAL surfaces, no generic git failure.

    A generic ``Git command failed`` would also be "not a commit", so the
    documented refusal is asserted verbatim, HEAD is asserted unmoved, and the
    rogue hook is asserted never to have run -- for every construction path.
    """
    workspace, repo, marker = _workspace_with_rogue_hook(tmp_path)
    head_before = _git_ok(repo, "rev-parse", "HEAD").strip()

    for name, build in _construction_paths(workspace, repo).items():
        result = build().execute()

        assert REFUSAL in result, (
            f"{name}: expected the documented refusal, got {result!r}"
        )
        assert "Git command failed (exit code 1)" in result, (
            f"{name}: the refusal did not come back through the fail-closed "
            f"tuple path: {result!r}"
        )
        assert "execution_mode: host" in result, (
            f"{name}: the refusal was not attributed to host execution: {result!r}"
        )

    assert "host switch commit" not in _git_ok(repo, "log", "--format=%s")
    assert _git_ok(repo, "rev-parse", "HEAD").strip() == head_before, (
        "HEAD moved although every commit was refused"
    )
    assert not marker.exists(), (
        "the configured .githooks/pre-commit EXECUTED on the host path: the "
        "kill switch advertises hooks it then neutralizes"
    )

