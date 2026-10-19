"""Tests for the workspace-configured commit identity (``git_identity``).

Policy under test (``GitWriteTool._git_commit``):

1. the workspace config key ``git_identity: {name, email}`` is optional;
2. when present it is pinned on the COMMIT invocation as
   ``git -c user.name=<name> -c user.email=<email> commit ...`` -- the pins
   precede the subcommand (the only position git accepts ``-c``), the value
   comes from the workspace config only (never agent input) and nothing is
   ever written to ``.git/config``;
3. when absent, git's own repo-local/global resolution stands untouched -- the
   tool neither pins a fallback identity nor probes one (no extra subprocess),
   so identity sources a ``git config --get`` probe cannot see (auto-detected
   ``user@host``, ``EMAIL``, ``GIT_AUTHOR_EMAIL``) keep working;
4. when git ITSELF rejects the commit for want of an identity, the caller gets
   ``_NO_GIT_IDENTITY_ERROR`` (remedies named) in place of git's advice block;
   every other failure -- and every success -- passes through unchanged;
5. a malformed ``git_identity`` is refused, never silently ignored (falling
   through would attribute the commit to the wrong author while reporting
   success).

The backends are covered too: the pins land in the FINAL argv of both
execution modes, after all ``args[0]``-based hook/permission logic has run, and
the hardening pins still win on a key collision.
"""

from pathlib import Path

import pytest

from tools.git_write_tool import (  # noqa: PLC2701
    _NO_GIT_IDENTITY_ERROR,
    GitWriteTool,
)

WS_A = "ws-identity-a"
WS_B = "ws-identity-b"
IDENTITY_A = {"name": "Agent A", "email": "agent-a@example.test"}
IDENTITY_B = {"name": "Agent B", "email": "agent-b@example.test"}


def _tool(**overrides):
    params = {
        "operation": "commit",
        "message": "agent commit",
        "file_path": "a.txt",
        "effective_permissions": {"git": "write"},
        "workspace_id": WS_A,
    }
    params.update(overrides)
    return GitWriteTool(**params)


class _Recorder:
    """Fake ``_run_git``: records argv, answers the branch and commit calls.

    ``commit_output`` overrides the commit result so a test can reproduce git's
    own failure wording verbatim (``_run_git`` returns error-shaped strings on
    failure -- ``Git command failed (exit code N):\n<stderr>`` -- never raises).
    """

    def __init__(self, branch="feat/x", commit_output="COMMIT_OK\n"):
        self.calls = []
        self.branch = branch
        self.commit_output = commit_output

    def __call__(self, repo_root, args, timeout=30):
        self.calls.append(list(args))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return self.branch + "\n"
        if args[:1] == ["commit"]:
            return self.commit_output
        return "ADD_OK\n"

    @property
    def committed(self):
        return any(argv[:1] == ["commit"] for argv in self.calls)

    @property
    def config_writes(self):
        """Any ``git config`` invocation that is not a read-only ``--get``."""
        return [
            argv
            for argv in self.calls
            if argv[:1] == ["config"] and argv[1:2] != ["--get"]
        ]


def _workspace_config(monkeypatch, configs):
    """Patch the workspace-config reader used by the tool.

    ``configs`` maps workspace id -> config dict; an unknown id raises so a
    cross-workspace read is a test failure, not a silent fallback.
    """
    monkeypatch.setattr(
        "tools.git_write_tool.load_workspace_config",
        lambda workspace_id: dict(configs[workspace_id]),
    )


def _stage_spy(tool, recorder):
    """Record the staged ``_commit_identity_config`` at every ``_run_git``."""
    staged = []

    def spy(repo_root, args, timeout=30):
        staged.append(tool._commit_identity_config)  # noqa: SLF001
        return recorder(repo_root, args, timeout=timeout)

    tool._run_git = spy  # noqa: SLF001
    return staged


# --- workspace identity is pinned on the commit ----------------------------


def test_workspace_identity_pinned_on_commit(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {"git_identity": IDENTITY_A}})
    tool = _tool()
    recorder = _Recorder()
    staged = _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert "COMMIT_OK" in result
    # Pinned for the commit call only, never for the branch probe / add.
    assert staged[0] is None
    assert staged[-1] == ["user.name=Agent A", "user.email=agent-a@example.test"]
    # Nothing extra is issued: branch probe + stage + commit, and no ``git
    # config`` at all (the identity is pinned, never read or written).
    assert recorder.calls == [
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["add", "--", "a.txt"],
        ["commit", "-m", "agent commit", "--", "a.txt"],
    ]
    assert recorder.config_writes == []


def test_missing_workspace_identity_issues_no_probe(tmp_path, monkeypatch):
    """No ``git_identity`` -> git's own resolution stands, unprobed and unpinned."""
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {}})
    tool = _tool()
    recorder = _Recorder()
    staged = _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert "COMMIT_OK" in result
    assert staged[-1] is None  # nothing pinned
    assert not any(value for value in staged)
    assert recorder.committed
    # Precisely the pre-existing flow: no identity probe subprocess.
    assert recorder.calls == [
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["add", "--", "a.txt"],
        ["commit", "-m", "agent commit", "--", "a.txt"],
    ]
    assert recorder.config_writes == []


# git's own wording when it cannot resolve an author/committer identity, captured
# from real failures (128 = "fatal"). All three are reworded to the refusal.
GIT_NO_IDENTITY_OUTPUTS = {
    "nothing-configured": (
        "Git command failed (exit code 128):\n"
        "Author identity unknown\n\n"
        "*** Please tell me who you are.\n\n"
        "Run\n\n"
        '  git config --global user.email "you@example.com"\n'
        '  git config --global user.name "Your Name"\n'
    ),
    "email-unresolvable": (
        "Git command failed (exit code 128):\n"
        "fatal: unable to auto-detect email address (got 'agent@build.(none)')\n"
    ),
    "empty-name": (
        "Git command failed (exit code 128):\n"
        "fatal: empty ident name (for <agent@build>) not allowed\n"
    ),
}


@pytest.mark.parametrize(
    "commit_output",
    GIT_NO_IDENTITY_OUTPUTS.values(),
    ids=GIT_NO_IDENTITY_OUTPUTS.keys(),
)
def test_git_missing_identity_refusal_is_reworded(
    tmp_path, monkeypatch, commit_output
):
    """git's own "who are you" failure becomes the actionable refusal."""
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {}})
    tool = _tool()
    recorder = _Recorder(commit_output=commit_output)
    _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    # The documented refusal first, then the usual execution-mode trailer that
    # every operation output carries.
    assert result.startswith(_NO_GIT_IDENTITY_ERROR)
    assert "execution_mode:" in result
    # Detection is post-hoc: branch probe + stage + the one commit attempt.
    assert [argv[:1] for argv in recorder.calls] == [["rev-parse"], ["add"], ["commit"]]
    assert recorder.calls[-1] == ["commit", "-m", "agent commit", "--", "a.txt"]


@pytest.mark.parametrize(
    "commit_output",
    [
        (
            "Git command failed (exit code 1):\n"
            "fatal: pathspec 'a.txt' did not match any file(s) known to git\n"
        ),
        "Git command failed (exit code 128):\nfatal: unable to write new index file\n",
        "Git command not found: git is not installed\n",
        "Error running git command: [Errno 13] Permission denied\n",
    ],
    ids=["pathspec", "index-locked", "git-missing", "exec-error"],
)
def test_other_commit_failures_pass_through_unchanged(
    tmp_path, monkeypatch, commit_output
):
    """Only identity failures are reworded: everything else stays verbatim."""
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {}})
    tool = _tool()
    recorder = _Recorder(commit_output=commit_output)
    _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert _NO_GIT_IDENTITY_ERROR not in result
    assert commit_output.strip() in result


def test_successful_commit_output_is_not_reworded(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {}})
    tool = _tool()
    recorder = _Recorder(commit_output="[feat/x abc1234] agent commit\n")
    _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert "[feat/x abc1234] agent commit" in result
    assert _NO_GIT_IDENTITY_ERROR not in result


def test_missing_identity_detection_is_case_insensitive():
    tool = _tool()
    assert tool._is_missing_identity_error("GIT: AUTHOR IDENTITY UNKNOWN\n")
    assert tool._is_missing_identity_error("*** Please Tell Me Who You Are.")
    assert not tool._is_missing_identity_error("[main 0000000] fine\n")
    assert not tool._is_missing_identity_error("")


def test_no_workspace_bound_keeps_native_behaviour(tmp_path, monkeypatch):
    """Direct/legacy callers (no workspace bound) have no config to consult."""
    (tmp_path / "a.txt").write_text("x\n")

    def _boom(workspace_id):
        raise AssertionError("workspace config must not be read without a binding")

    monkeypatch.setattr("tools.git_write_tool.load_workspace_config", _boom)
    tool = _tool(workspace_id=None)
    recorder = _Recorder()
    staged = _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert "COMMIT_OK" in result
    assert staged[-1] is None
    assert recorder.calls == [
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["add", "--", "a.txt"],
        ["commit", "-m", "agent commit", "--", "a.txt"],
    ]


# --- two workspaces, two identities ---------------------------------------


@pytest.mark.parametrize(
    "workspace_id,configs,expected_name",
    [
        (WS_A, {WS_A: {"git_identity": IDENTITY_A}}, "Agent A"),
        (WS_B, {WS_B: {"git_identity": IDENTITY_B}}, "Agent B"),
        (
            WS_B,
            {WS_A: {"git_identity": IDENTITY_A}, WS_B: {"git_identity": IDENTITY_B}},
            "Agent B",
        ),
    ],
    ids=["ws-a", "ws-b", "ws-b-with-ws-a-configured"],
)
def test_workspace_identities_do_not_cross_contaminate(
    tmp_path, monkeypatch, workspace_id, configs, expected_name
):
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, configs)
    tool = _tool(workspace_id=workspace_id)
    recorder = _Recorder()
    staged = _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert "COMMIT_OK" in result
    assert staged[-1][0] == f"user.name={expected_name}"


def test_pending_identity_cleared_after_commit(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {"git_identity": IDENTITY_A}})
    tool = _tool()
    recorder = _Recorder()
    _stage_spy(tool, recorder)

    tool._git_commit(tmp_path)

    assert tool._commit_identity_config is None  # noqa: SLF001


def test_pending_identity_cleared_when_git_raises(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {"git_identity": IDENTITY_A}})
    tool = _tool()

    def boom(repo_root, args, timeout=30):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "feat/x\n"
        raise RuntimeError("container unavailable")

    tool._run_git = boom  # noqa: SLF001

    with pytest.raises(RuntimeError):
        tool._git_commit(tmp_path)

    assert tool._commit_identity_config is None  # noqa: SLF001


# --- malformed workspace identity is refused, never ignored ----------------


@pytest.mark.parametrize(
    "raw,expected_fragment",
    [
        ("Agent A <agent-a@example.test>", "git_identity` is malformed"),
        ({"name": "Agent A"}, "git_identity.email` must be a non-empty string"),
        ({"name": "Agent A", "email": "   "}, "git_identity.email` must be a non-empty string"),
        ({"name": None, "email": "a@example.test"}, "git_identity.name` must be a non-empty string"),
        ({"name": "Agent <A>", "email": "a@example.test"}, "characters that are not allowed"),
        ({"name": "Agent\nA", "email": "a@example.test"}, "characters that are not allowed"),
        ({"name": "Agent A", "email": "a@example.test>"}, "characters that are not allowed"),
    ],
    ids=[
        "not-an-object",
        "email-absent",
        "email-blank",
        "name-null",
        "name-angle-bracket",
        "name-newline",
        "email-angle-bracket",
    ],
)
def test_malformed_workspace_identity_refused(
    tmp_path, monkeypatch, raw, expected_fragment
):
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {"git_identity": raw}})
    tool = _tool()
    recorder = _Recorder()
    _stage_spy(tool, recorder)

    result = tool._git_commit(tmp_path)

    assert "Cannot commit:" in result
    assert expected_fragment in result
    # Refused before staging, and never silently ignored: no add, no commit.
    assert not recorder.committed
    assert not any(argv[:1] == ["add"] for argv in recorder.calls)
    assert not any(argv[:2] == ["config", "--get"] for argv in recorder.calls)


# --- backend argv: pins precede the subcommand, hardening still wins -------


class _FakeExecResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandboxExecution:
    """Captures the argv handed to the host sandbox."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        _FakeSandboxExecution.instances.append(self)

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return _FakeExecResult(returncode=0, stdout="HOSTOUT", stderr="")


class _FakeManager:
    def __init__(self):
        self.calls = []

    def exec(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}


PINS = ["user.name=Agent A", "user.email=agent-a@example.test"]


def test_host_backend_pins_precede_subcommand(tmp_path, monkeypatch):
    tool = _tool()
    tool.session_permissions = {"git": "write"}
    tool.effective_permissions = {"git": "write"}
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_commit_identity_config", list(PINS))
    _FakeSandboxExecution.instances.clear()
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _FakeSandboxExecution
    )

    result = tool._exec_host_raw(tmp_path, ["commit", "-m", "m", "--", "a.txt"])

    assert result[0] == 0
    command = _FakeSandboxExecution.instances[0].calls[0][0]
    assert command[:5] == ["git", "-c", PINS[0], "-c", PINS[1]]
    # The subcommand (and its --no-verify) still come last: the hook /
    # permission logic that keys off args[0] is untouched by the pins.
    assert command[5:7] == ["-c", "core.hooksPath=/dev/null"]
    assert command[-6:] == ["commit", "--no-verify", "-m", "m", "--", "a.txt"]
    assert command.index("commit") > max(
        i for i, arg in enumerate(command) if arg == "-c"
    )
    # Hardening pins are emitted AFTER the identity pins, so they win on a
    # key collision.
    assert command.index("core.hooksPath=/dev/null") > command.index(PINS[0])


def test_host_backend_identity_absent_leaves_argv_unchanged(tmp_path, monkeypatch):
    tool = _tool()
    tool.session_permissions = {"git": "write"}
    tool.effective_permissions = {"git": "write"}
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_commit_identity_config", None)
    _FakeSandboxExecution.instances.clear()
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _FakeSandboxExecution
    )

    tool._exec_host_raw(tmp_path, ["commit", "-m", "m"])

    command = _FakeSandboxExecution.instances[0].calls[0][0]
    assert command[:2] == ["git", "-c"]
    assert "user.name" not in " ".join(command)


def test_container_backend_pins_precede_subcommand(tmp_path):
    manager = _FakeManager()
    tool = _tool()
    tool.session_permissions = {"git": "write"}
    tool.effective_permissions = {"git": "write"}
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS_A)
    object.__setattr__(tool, "_commit_identity_config", list(PINS))

    result = tool._exec_container_raw(tmp_path, ["commit", "-m", "x"], manager=manager)

    assert result == (0, "ok\n", "")
    command = manager.calls[0][0]
    assert command[:5] == ["git", "-c", PINS[0], "-c", PINS[1]]
    # Container mode's own hooksPath pin sits between the identity pins and the
    # subcommand; the subcommand is still last.
    assert command[-5:] == [
        "-c",
        "core.hooksPath=/workspace/.githooks",
        "commit",
        "-m",
        "x",
    ]
    assert command.index("commit") > max(
        i for i, arg in enumerate(command) if arg == "-c"
    )
    # EOL pins win on collision (emitted after the identity pins).
    assert command.index("core.autocrlf=input") > command.index(PINS[0])


def test_container_backend_identity_absent_argv_unchanged(tmp_path):
    manager = _FakeManager()
    tool = _tool()
    tool.session_permissions = {"git": "write"}
    tool.effective_permissions = {"git": "write"}
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS_A)
    object.__setattr__(tool, "_commit_identity_config", None)

    tool._exec_container_raw(tmp_path, ["commit", "-m", "x"], manager=manager)

    assert manager.calls[0][0] == [
        "git",
        "-c",
        "core.autocrlf=input",
        "-c",
        "core.eol=lf",
        "-c",
        "core.hooksPath=/workspace/.githooks",
        "commit",
        "-m",
        "x",
    ]


def test_identity_pins_never_leak_into_non_commit_argv(tmp_path, monkeypatch):
    """A staged pin from a previous commit must not reach ``git add``/probes."""
    tool = _tool()
    tool.session_permissions = {"git": "write"}
    tool.effective_permissions = {"git": "write"}
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_commit_identity_config", None)
    _FakeSandboxExecution.instances.clear()
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _FakeSandboxExecution
    )

    tool._exec_host_raw(tmp_path, ["add", "--", "a.txt"])

    command = _FakeSandboxExecution.instances[0].calls[0][0]
    assert "user.name" not in " ".join(command)


def test_commit_never_writes_git_config(tmp_path, monkeypatch):
    """The identity is applied as ``-c`` pins only -- ``git config`` is read-only."""
    (tmp_path / "a.txt").write_text("x\n")
    _workspace_config(monkeypatch, {WS_A: {"git_identity": IDENTITY_A}})
    tool = _tool()
    recorder = _Recorder()
    _stage_spy(tool, recorder)

    tool._git_commit(tmp_path)

    assert recorder.config_writes == []
    assert not any(argv[:2] == ["config", "--local"] for argv in recorder.calls)
    assert not any(argv[:2] == ["config", "--global"] for argv in recorder.calls)


def test_refusal_message_is_the_documented_text():
    """The refusal text is part of the contract (surfaced verbatim to callers)."""
    assert _NO_GIT_IDENTITY_ERROR == (
        "Cannot commit: no git identity. Set `git_identity` in the workspace "
        "config, or run `git config --local user.name/email`."
    )
