"""Per-session git worktree for workers -- OPT-IN, not the default.

A worker inherits the parent session's ``worktree_root`` when the session has
one; otherwise it runs straight in the registered workspace root.  This module
gives it a ``worktree_root`` of its own -- ``<workspace>/.worktrees/
<session-id>`` -- and is reached only when a caller explicitly decides to
isolate a session by setting ``worktree_root`` to the value this helper
returns.

Why the default moved to OPT-IN: a worker confined to
``<workspace>/.worktrees/<session>`` could neither see nor commit the tree the
agent's git tools and the docker mount use -- the container mounts
``source=self.workspace_path``, i.e. the registered workspace root -- so its
edits became a copy no diff, commit or CI run ever saw.  Reuse also
short-circuited on any non-empty directory with zero git contact, so a
re-summoned session kept whatever base its first spawn left behind.

Everything here is deliberately FAIL-OPEN, and the side effect is WIDER than
the worktree directory:

* the worktree is created ON A BRANCH (``wt/<session>``) instead of detached,
  because a detached worktree leaves every commit the worker makes dangling.
  That writes a branch ref under the repository's SHARED ``.git/refs/heads``:
  the ref is visible from the main checkout and pushable from there, so it is
  NOT private to the worker.  The worktree is checked out on ``wt/<session>``
  precisely so the worker can commit at all;
* the other filesystem writes are the worktree directory and the git
  administrative files ``git worktree add`` itself creates;
* a re-summoned session keeps whatever branch its existing worktree is already
  on: reuse short-circuits before any git call, so the branch is never
  re-pointed;
* every precondition that cannot be proven (no session id, no workspace root,
  no ``.git``, a session id that yields no usable branch name, git missing, git
  failing) logs a warning and returns ``None``, which leaves the caller on the
  workspace root -- exactly the pre-existing behaviour.

The helper is a plain function taking an injectable ``git_runner`` so the
decision logic is testable without a git binary (the execution container has
none).  The DEFAULT runner does not import ``subprocess``: host execution
belongs to the single chokepoint, ``thoughtmachine.host_port.host_run()``, so
``git`` is spent through the ``git`` grain like every other git operation.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable, List, Mapping, Optional

logger = logging.getLogger(__name__)

#: Directory (inside the workspace) that holds the per-session worktrees.
WORKTREES_DIRNAME = ".worktrees"

#: ``git_runner(argv, cwd) -> returncode``.  Only the exit status is
#: interpreted: 0 means the worktree is usable.
GitRunner = Callable[[List[str], str], int]

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

#: Branch (namespace) the worker worktrees are checked out on: ``wt/<session>``.
_BRANCH_PREFIX = "wt"


def _resolve_workspace_id(workspace_root: str) -> Optional[str]:
    """Map a workspace path to its workspace id (``None`` when unknown)."""
    try:
        from thoughtmachine.workspace_capabilities import resolve_workspace_id

        return resolve_workspace_id(workspace_root)
    except Exception as exc:  # never let id resolution break a spawn
        logger.warning(
            "Worker worktree default: cannot resolve workspace id (%s)", exc
        )
        return None


def _host_git_runner(
    workspace_root: str,
    session_id: str,
    effective_permissions: Optional[Mapping[str, object]],
) -> GitRunner:
    """The default runner: ``git`` through the one host-execution chokepoint.

    ``host_run()`` owns the kill switch, the grant check and the audit trail,
    so a refused or unavailable host backend simply yields a non-zero status
    here and the caller falls back to the workspace root.
    """

    def run(argv: List[str], cwd: str) -> int:
        from thoughtmachine.host_port import host_run  # local: keeps import cost off spawn

        try:
            result = host_run(
                ["git", *argv],
                workspace_id=_resolve_workspace_id(workspace_root) or "",
                session_id=session_id,
                grain="git",
                cwd=cwd,
                timeout=60,
                effective_permissions=effective_permissions,
            )
        except PermissionError as exc:
            # ``host_run`` REPORTS a refusal by raising, so without this the
            # docstring's "a refused ... backend simply yields a non-zero
            # status" would be a lie and the refusal would reach the caller as
            # the much vaguer "git worktree add raised (...)".  Log the real
            # reason once, here, and return the non-zero status the contract
            # promises.
            logger.warning("Worker worktree default: host git refused (%s)", exc)
            return 1
        return 0 if result.ok else (result.exit_code or 1)

    return run


def session_worktree_name(session_id: Optional[str]) -> Optional[str]:
    """Return the directory name for *session_id*, or ``None`` if unusable.

    Session ids are not guaranteed to be filesystem-safe (and must never be
    able to address a path outside ``.worktrees``), so every character outside
    ``[A-Za-z0-9._-]`` is folded to ``-``.  Leading dots are stripped so the
    result can never be ``.``/``..``.
    """
    if not isinstance(session_id, str):
        return None
    name = _UNSAFE_CHARS.sub("-", session_id.strip()).strip(".")
    name = name.strip("-")
    if not name or name in (".", ".."):
        return None
    return name


def _is_usable_branch_component(name: str) -> bool:
    """Whether *name* is something ``git`` accepts as a branch component.

    Fail-closed: ``git`` refuses ``..``, a leading ``-`` (it would be parsed as
    an option), a trailing ``.`` and a ``.lock`` suffix, and an over-long ref
    makes ``git`` fail anyway -- all of which must be rejected HERE so the
    caller falls back instead of guessing.
    """
    if not name or len(name) > 200:
        return False
    if ".." in name or name.endswith((".", ".lock")):
        return False
    return not name.startswith("-")


def worktree_branch_name(session_id: Optional[str]) -> Optional[str]:
    """Return the branch the worktree is checked out on, or ``None``.

    ``None`` means the session id yields no name ``git`` would accept as a
    branch: the caller must then NOT create a worktree (a detached one would
    let the worker's commits dangle) and fall back to the workspace root.
    """
    name = session_worktree_name(session_id)
    if name is None or not _is_usable_branch_component(name):
        return None
    return f"{_BRANCH_PREFIX}/{name}"


def resolve_worker_worktree_root(
    workspace_root: Optional[str],
    session_id: Optional[str],
    *,
    effective_permissions: Optional[Mapping[str, object]] = None,
    git_runner: Optional[GitRunner] = None,
) -> Optional[str]:
    """Ensure and return ``<workspace_root>/.worktrees/<session>``.

    Returns the worktree path when it exists (created now or reused from an
    earlier spawn), ``None`` when the default cannot be established -- in
    which case the caller must keep using the workspace root.
    """
    if not isinstance(workspace_root, str) or not workspace_root:
        logger.warning(
            "Worker worktree default skipped: no workspace root available"
        )
        return None

    root = os.path.realpath(workspace_root)
    if not os.path.isdir(root):
        logger.warning(
            "Worker worktree default skipped: workspace root %r is not a directory",
            workspace_root,
        )
        return None

    name = session_worktree_name(session_id)
    if name is None:
        logger.warning(
            "Worker worktree default skipped: unusable session id %r", session_id
        )
        return None

    worktree = os.path.join(root, WORKTREES_DIRNAME, name)

    # Idempotent reuse.  Non-empty only: an empty directory is the residue of a
    # failed ``git worktree add`` and must not be adopted as a working root.
    try:
        if os.path.isdir(worktree) and os.listdir(worktree):
            return worktree
    except OSError as exc:
        logger.warning(
            "Worker worktree default skipped: cannot inspect %r (%s)", worktree, exc
        )
        return None

    if not os.path.exists(os.path.join(root, ".git")):
        logger.warning(
            "Worker worktree default skipped: %r is not a git repository", root
        )
        return None

    runner = git_runner or _host_git_runner(
        root, session_id if isinstance(session_id, str) else "", effective_permissions
    )
    branch = worktree_branch_name(session_id)
    if branch is None:
        logger.warning(
            "Worker worktree default skipped: unusable branch name for session %r",
            session_id,
        )
        return None

    try:
        # On a BRANCH, never ``--detach``: a detached worktree lets the worker
        # commit onto nothing.  ``-b`` fails when the ref already exists (a
        # removed worktree leaves its branch behind), so re-attach to the
        # existing branch instead of giving up.
        status = runner(["worktree", "add", "-b", branch, worktree], root)
        if status != 0 and runner(
            ["rev-parse", "--verify", f"refs/heads/{branch}"], root
        ) == 0:
            status = runner(["worktree", "add", worktree, branch], root)
    except Exception as exc:  # a runner must never break the spawn boundary
        logger.warning(
            "Worker worktree default skipped: git worktree add raised (%s)", exc
        )
        return None

    if status != 0:
        logger.warning(
            "Worker worktree default skipped: 'git worktree add' for branch %s "
            "at %s exited %s",
            branch, worktree, status,
        )
        return None

    if not os.path.isdir(worktree):
        logger.warning(
            "Worker worktree default skipped: git reported success but %r "
            "does not exist", worktree,
        )
        return None

    return worktree
