"""Operator cancellation must FAIL CLOSED in the git execution seam.

``ResourceContainerManager.exec`` now stops WAITING when the operator cancels
the current turn and returns a payload carrying ``status="interrupted"``,
``exit_code=None`` and empty stdio (infra/resource_container_manager.py; the
command may still be running inside the untouched container).

``GitReadTool._exec_container_raw`` used to unpack that payload straight into
the ``(exit_code, stdout, stderr)`` tuple the whole tool is built on.  Two
things went wrong with that:

* a cancelled git command became indistinguishable from a merely failed one
  (``_run_git`` renders a tuple into ``"Git command failed (exit code None)"``,
  a STRING), so the cancellation could not reach the caller as an error; and
* ``None`` is not ``0``-but-``not-None``: every write-path probe is written as
  ``if probe_exit == 0: ...refuse...``, so a cancelled probe silently SKIPPED
  its refusal (duplicate worktree, locked worktree, dirty worktree) and the
  next mutating git call ran as if the cancel had been permission to proceed.

The seam therefore raises instead of returning: cancellation is a hard error
that every caller (and the ``ToolExecutor`` above it) sees as an error.

These tests fake the git execution seam (a fake manager handed to
``_exec_container_raw`` via the real container dispatch) rather than driving a
docker daemon: the behaviour under test is the payload handling, not docker.

Import order: ``tools`` must be imported before ``security.security_gate``
(the container seam imports the gate lazily at call time), hence the
``import tools`` line before the tool imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402,F401  (import before security.security_gate)
from tools.git_info_tool import GitReadTool  # noqa: E402
from tools.git_write_tool import GitWriteTool  # noqa: E402


# The exact message fragment callers may rely on to recognise a cancellation.
CANCEL_MARKER = "git execution interrupted by operator cancellation"

# The payload ``ResourceContainerManager.exec`` returns for a cancelled turn.
INTERRUPTED_PAYLOAD = {
    "status": "interrupted",
    "interrupted": True,
    "reason": "stopped_by_operator",
    "exit_code": None,
    "stdout": "",
    "stderr": "",
    "exec": "container_exec",
    "container_id": "c-git",
    "command": "git status --porcelain=v1",
    "container_killed": False,
    "container_removed": False,
}


class _InterruptingManager:
    """Fake resource manager: records ``exec`` calls, always "interrupted"."""

    def __init__(self, payload=None):
        self.payload = dict(payload or INTERRUPTED_PAYLOAD)
        self.calls = []

    def exec(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return dict(self.payload)

    @property
    def argv(self):
        """Every argv token the manager was asked to run, flattened."""
        return [token for call in self.calls for token in call["argv"]]


def _make_tool(tool_cls, manager, tmp_path, **params):
    """Build a tool whose REAL container seam runs against *manager*.

    Only the resource RESOLUTION is faked (no docker daemon in tests); the
    git argv assembly, the permission gates and -- crucially -- the payload
    handling in ``_exec_container_raw`` all run for real.
    """
    tool = tool_cls(**params)
    tool._resolved_workspace_path = str(Path(tmp_path).resolve())
    tool._resolved_workspace_id = "ws-cancel"
    # ``execute()`` resets and re-resolves that state from the registries, so
    # the registry lookup has to answer too (no registry in a unit test).
    tool._resolve_registry_workspace_info = lambda: (
        "ws-cancel",
        str(Path(tmp_path).resolve()),
    )
    tool._use_container_mode = lambda: True
    tool._resolve_resource_execution = lambda: (
        {"mode": "containerized", "detail": "test fake", "failure_reason": None},
        manager,
    )
    return tool


def _read_tool(manager, tmp_path, **params):
    params.setdefault("effective_permissions", {"git": "read"})
    params.setdefault("session_permissions", {"git": "read"})
    return _make_tool(GitReadTool, manager, tmp_path, **params)


def _write_tool(manager, tmp_path, **params):
    params.setdefault("effective_permissions", {"git": "write"})
    params.setdefault("session_permissions", {"git": "write"})
    return _make_tool(GitWriteTool, manager, tmp_path, **params)


# ---------------------------------------------------------------------------
# 1. The seam itself: an interrupted exec RAISES, and returns nothing
# ---------------------------------------------------------------------------
def test_exec_container_raw_raises_on_interrupt(tmp_path):
    """The cancelled payload must never leave ``_exec_container_raw`` as data."""
    manager = _InterruptingManager()
    tool = _read_tool(manager, tmp_path, operation="status")
    repo_root = Path(tmp_path).resolve()

    returned = None
    with pytest.raises(RuntimeError) as excinfo:
        returned = tool._exec_container_raw(  # noqa: SLF001
            repo_root, ["status", "--porcelain=v1"]
        )

    message = str(excinfo.value)
    assert CANCEL_MARKER in message
    # The reason travels with the error so an operator log is actionable.
    assert "stopped_by_operator" in message
    # NOTHING was returned: the tuple the tool is built on does not exist.
    assert returned is None

    # And the call really went out to the manager, with a LIST argv.
    assert len(manager.calls) == 1
    assert isinstance(manager.calls[0]["argv"], list)
    assert manager.calls[0]["argv"][0] == "git"
    assert "status" in manager.calls[0]["argv"]
    assert manager.calls[0]["workdir"] == "/workspace"


def test_interrupted_payload_is_indistinguishable_from_nothing(tmp_path):
    """The raise happens for the exact payload the resource manager produces.

    ``status`` is the discriminator, so a plain success payload must still be
    returned unchanged (no over-broad "if not ok: raise").
    """
    ok_manager = _InterruptingManager(
        payload={"exit_code": 0, "stdout": "ok\n", "stderr": ""}
    )
    tool = _read_tool(ok_manager, tmp_path, operation="status")

    result = tool._exec_container_raw(  # noqa: SLF001
        Path(tmp_path).resolve(), ["status", "--porcelain=v1"]
    )

    assert result == (0, "ok\n", "")


# ---------------------------------------------------------------------------
# 2. The raise survives the read call chain (never converted to a string)
# ---------------------------------------------------------------------------
def test_run_git_propagates_the_cancellation(tmp_path):
    """``_run_git`` renders tuples as strings -- it must NOT render a cancel."""
    manager = _InterruptingManager()
    tool = _read_tool(manager, tmp_path, operation="status")

    with pytest.raises(RuntimeError) as excinfo:
        tool._run_git(  # noqa: SLF001
            Path(tmp_path).resolve(), ["status", "--porcelain=v1"]
        )

    assert CANCEL_MARKER in str(excinfo.value)
    assert not str(excinfo.value).startswith("Git command failed")


def test_execute_propagates_the_cancellation(tmp_path):
    """The public entry point raises: a cancelled read is an ERROR, not output."""
    (Path(tmp_path) / ".git").mkdir()
    manager = _InterruptingManager()
    tool = _read_tool(
        manager,
        tmp_path,
        operation="status",
        working_dir=str(tmp_path),
    )

    with pytest.raises(RuntimeError) as excinfo:
        tool.execute()

    assert CANCEL_MARKER in str(excinfo.value)
    assert manager.calls, "the status read never reached the container"


# ---------------------------------------------------------------------------
# 3. The write-path probes must not proceed past a cancellation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "operation,path",
    [("worktree_add", "wt-new"), ("worktree_remove", "wt-new")],
)
def test_write_probes_do_not_proceed_on_cancel(tmp_path, operation, path):
    """A cancelled probe must abort the write, never skip its refusal.

    The probes call ``_run_git_raw`` DIRECTLY and gate on ``probe_exit == 0``.
    With the old tuple return, ``None`` silently skipped the duplicate/locked/
    dirty refusals and the mutating ``worktree add``/``worktree remove`` argv
    was assembled next.  Now the probe raises and no mutating argv is built.
    """
    (Path(tmp_path) / path).mkdir()
    manager = _InterruptingManager()
    tool = _write_tool(manager, tmp_path, operation=operation, path=path)
    # Path validation is a separate concern (and needs a registry workspace).
    tool._validate_path = lambda candidate: candidate  # noqa: SLF001

    with pytest.raises(RuntimeError) as excinfo:
        getattr(tool, f"_git_{operation}")(Path(tmp_path).resolve())

    assert CANCEL_MARKER in str(excinfo.value)

    # The probe DID run (that is how we found out), but nothing mutating did.
    assert manager.calls, "the probe never reached the container"
    tokens = manager.argv
    assert "list" in tokens, tokens
    assert "add" not in tokens, tokens
    assert "remove" not in tokens, tokens


# ---------------------------------------------------------------------------
# 4. ``_git_repo_root``: no exit code means "not a repository", explicitly
# ---------------------------------------------------------------------------
def test_git_repo_root_treats_a_none_exit_code_as_not_a_repo(tmp_path):
    tool = GitReadTool(operation="status")
    tool._run_git_raw = lambda repo_root, args, timeout=30: (None, "", "")  # noqa: SLF001

    assert tool._git_repo_root(Path(tmp_path).resolve()) is None  # noqa: SLF001


def test_git_repo_root_none_exit_code_is_never_reported_as_git_unavailable(tmp_path):
    """A cancelled exec must never be re-diagnosed as "git is not installed".

    ``_git_run_indicates_missing_git`` looks for the ``not found`` text, so
    without an explicit ``exit_code is None`` branch a cancelled probe whose
    stderr happens to contain that text raised ``GitUnavailableError`` instead
    of reporting "not a git repository".
    """
    tool = GitReadTool(operation="status")
    tool._run_git_raw = (  # noqa: SLF001
        lambda repo_root, args, timeout=30: (None, "", "git: command not found\n")
    )

    assert tool._git_repo_root(Path(tmp_path).resolve()) is None  # noqa: SLF001
