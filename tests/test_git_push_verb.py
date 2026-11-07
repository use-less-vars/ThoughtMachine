"""tests/test_git_push_verb.py -- the ``push`` verb on the git_write surface.

Ruling under test (credential design Decision 1): ``push`` IS exposed on the
git WRITE surface, but narrowed to ``git push origin <branch>``:

* the remote is hard-coded to ``origin`` -- no URL, no refspec, no flags, so
  the agent cannot invent a new egress target or rewrite remote refs;
* ``force=True`` is refused outright, a ``src:dst`` refspec is refused, and a
  protected branch (``dev`` / ``master`` / ``main``) is never pushed;
* the credential reaches git ONLY through the environment (``GIT_ASKPASS`` +
  ``GIT_CONFIG_COUNT``, W3), so argv stays credential-free. A session with no
  credential in the ``git_environment`` seam is refused HERE, before git runs,
  instead of falling back to a terminal prompt nobody can answer;
* the verb needs ``git:write`` + ``filesystem:write`` + ``network:outbound``,
  and the network category is re-checked atomically before any git subprocess;
* it is a WRITE-level operation (``_get_operation_level``) and never executes
  through the host execution path, so no host-execution audit record is written.

The gate tests drive ``GitWriteTool._git_push`` directly (bypassing
``execute()``) with a fake ``_run_git``. The end-to-end tests use a real git
binary against a temp bare repo and are skipped when git is unavailable.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from thoughtmachine.git_auth_env import ASKPASS_SECRET_ENV
from tools.git_write_tool import GitWriteTool

FLAG_ERROR = 'Error: git:write denied: session git_write permission is not "write"'

WRITE_FS = {"git": "write", "filesystem": "write"}
CREDENTIAL_SEAM = {"git_environment": {ASKPASS_SECRET_ENV: "ghp_fake_credential"}}

NO_GIT = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary is unavailable"
)


def _tool(**overrides):
    params = {"operation": "push", "branch": "feat/x"}
    params.update(overrides)
    return GitWriteTool(**params)


def _credentialed(**overrides):
    """A push tool that passes both grain gates and carries a credential."""
    overrides.setdefault("effective_permissions", dict(WRITE_FS))
    overrides.setdefault("agent_config", {"git_environment": dict(CREDENTIAL_SEAM["git_environment"])})
    return _tool(**overrides)


def _fake_run_git(calls, output="ok\n"):
    """Fake ``_run_git`` that records each argv and returns ``output``."""

    def fake_run_git(repo_root, args, timeout=30):
        calls.append(list(args))
        return output

    return fake_run_git


# --- environment-only credential -----------------------------------------


def test_push_without_credential_refuses():
    """No credential in the git_environment seam -> refused before git runs."""
    tool = _tool(effective_permissions=dict(WRITE_FS))
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert "no git credential is configured" in result
    assert ASKPASS_SECRET_ENV in result
    # argv never runs, and nothing credential-shaped is suggested either.
    assert calls == []


# --- the narrowed argv ----------------------------------------------------


def test_push_to_feature_branch_succeeds():
    """A feature branch pushes as exactly ``git push origin <branch>``."""
    tool = _credentialed()
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert calls == [["push", "origin", "feat/x"]]
    assert "ok" in result


def test_push_refuses_force():
    """``force=True`` is refused: a force-push rewrites shared history."""
    tool = _credentialed(force=True)
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert "push refuses force=True" in result
    assert calls == []
    assert "--force" not in result


def test_push_refuses_refspec():
    """A ``src:dst`` refspec (or a fully-qualified ref) is refused."""
    tool = _credentialed(branch="feat/x:refs/heads/main")
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert "not a refspec" in result
    assert calls == []


def test_push_refuses_protected_branch():
    """A protected branch is never pushed."""
    tool = _credentialed(branch="main")
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert "refusing to push protected branch" in result
    assert "main" in result
    assert calls == []


def test_push_requires_branch_name():
    """Push takes an explicit branch name; there is no implicit HEAD push."""
    tool = _credentialed(branch=None)
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert result == "Error: branch is required for push operation"
    assert calls == []


# --- permission surface ---------------------------------------------------


def test_push_requires_git_write_category():
    """``push`` advertises git:write + filesystem:write + network:outbound."""
    assert GitWriteTool.get_required_categories({"operation": "push"}) == [
        "git:write",
        "filesystem:write",
        "network:outbound",
    ]


def test_push_requires_network_outbound_grain():
    """execute() re-checks ``network:outbound`` atomically before any git run."""
    tool = _tool(
        effective_permissions=dict(WRITE_FS),
        agent_config={"git_environment": dict(CREDENTIAL_SEAM["git_environment"])},
    )
    with mock.patch.object(
        tool, "_run_git", side_effect=AssertionError("git ran")
    ):  # noqa: SLF001
        result = tool.execute()

    payload = json.loads(result)
    assert "network:outbound required for push" in payload["error"]


def test_push_refuses_when_session_holds_only_git_read():
    """A git:read session can never push (fail closed on the write gate)."""
    tool = _tool(
        effective_permissions={"git": "read", "filesystem": "write"},
        agent_config={"git_environment": dict(CREDENTIAL_SEAM["git_environment"])},
    )
    calls = []
    tool._run_git = _fake_run_git(calls)  # noqa: SLF001

    result = tool._git_push(Path("/tmp/repo"))  # noqa: SLF001

    assert result == FLAG_ERROR
    assert calls == []


def test_push_operation_level_is_write():
    """``push`` is a WRITE-level operation (git:write, never git:read)."""
    assert _tool()._get_operation_level([]) == "write"  # noqa: SLF001


# --- no host-execution record --------------------------------------------


def test_push_does_not_write_host_execution_jsonl(tmp_path, monkeypatch):
    """A container-mode push never touches the host execution backend.

    The raw container backend is stubbed (no docker needed) and the host
    backend is booby-trapped: if push ever executed on the host, the test
    fails AND a host-execution audit record would appear under the vault root.
    """
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
    tool = _credentialed()
    calls = []

    def raw_container(repo_root, args, timeout=30, manager=None):
        calls.append(list(args))
        return (0, "ok\n", "")

    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git_raw = raw_container  # noqa: SLF001
    tool._exec_host_raw = lambda *a, **k: (_ for _ in ()).throw(  # noqa: SLF001
        AssertionError("push executed on the host backend")
    )

    result = tool._git_push(tmp_path / "repo")  # noqa: SLF001

    assert calls == [["push", "origin", "feat/x"]]
    assert "ok" in result
    assert list((tmp_path / "vault").rglob("*.jsonl")) == []


# --- end-to-end against a real local remote -------------------------------

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo, *args):
    """Run a real git command (test-side setup only) and assert it succeeded."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=dict(_GIT_ENV),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


def _real_raw(repo_env):
    """A ``_run_git_raw`` that runs the real git binary without a sandbox."""

    def raw(repo_root, args, timeout=30):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            env=dict(repo_env),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (proc.returncode, proc.stdout, proc.stderr)

    return raw


def _push_repo(tmp_path, *, with_origin=True):
    """A temp repo on branch ``feat/w4-push`` (plus an optional bare origin)."""
    bare = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    if with_origin:
        bare.mkdir()
        _git(bare, "init", "--bare", "-q")
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "hello.txt").write_text("hi\n")
    _git(repo, "add", "hello.txt")
    _git(repo, "commit", "-q", "-m", "initial commit")
    _git(repo, "checkout", "-q", "-b", "feat/w4-push")
    if with_origin:
        _git(repo, "remote", "add", "origin", str(bare))
    return repo, bare


@NO_GIT
def test_push_succeeds_against_local_bare_repo(tmp_path):
    """The narrowed argv really pushes: the branch lands in the bare remote."""
    repo, bare = _push_repo(tmp_path)
    tool = _credentialed(branch="feat/w4-push")
    tool._run_git_raw = _real_raw(_GIT_ENV)  # noqa: SLF001

    result = tool._git_push(repo)  # noqa: SLF001

    assert "Git command failed" not in result
    landing = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/feat/w4-push"],
        cwd=str(bare),
        env=dict(_GIT_ENV),
        capture_output=True,
        text=True,
    )
    assert landing.returncode == 0, landing.stderr
    local_sha = subprocess.run(
        ["git", "rev-parse", "feat/w4-push"],
        cwd=str(repo),
        env=dict(_GIT_ENV),
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert landing.stdout.strip() == local_sha


@NO_GIT
def test_push_refuses_when_no_origin_configured(tmp_path):
    """No ``origin`` remote -> git fails and the failure is reported, not hidden."""
    repo, _ = _push_repo(tmp_path, with_origin=False)
    tool = _credentialed(branch="feat/w4-push")
    tool._run_git_raw = _real_raw(_GIT_ENV)  # noqa: SLF001

    result = tool._git_push(repo)  # noqa: SLF001

    assert "Git command failed" in result
    assert "Traceback" not in result
