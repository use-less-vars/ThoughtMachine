"""Default per-session git worktree for workers.

A worker inherits the parent session's ``worktree_root`` when the session has
one; otherwise it used to run straight in the registered workspace root.  This
module gives it a ``worktree_root`` of its own -- ``<workspace>/.worktrees/
<session-id>`` -- so worker file operations cannot collide with the main
agent's checkout.

Everything here is deliberately FAIL-OPEN and side-effect-scoped:

* the only filesystem writes are the worktree directory and the git
  administrative files ``git worktree add`` itself creates;
* every precondition that cannot be proven (no session id, no workspace root,
  no ``.git``, git missing, git failing) logs a warning and returns ``None``,
  which leaves the caller on the workspace root -- exactly the pre-existing
  behaviour.

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

        result = host_run(
            ["git", *argv],
            workspace_id=_resolve_workspace_id(workspace_root) or "",
            session_id=session_id,
            grain="git",
            cwd=cwd,
            timeout=60,
            effective_permissions=effective_permissions,
        )
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
    try:
        status = runner(["worktree", "add", "--detach", worktree], root)
    except Exception as exc:  # a runner must never break the spawn boundary
        logger.warning(
            "Worker worktree default skipped: git worktree add raised (%s)", exc
        )
        return None

    if status != 0:
        logger.warning(
            "Worker worktree default skipped: 'git worktree add %s' exited %s",
            worktree, status,
        )
        return None

    if not os.path.isdir(worktree):
        logger.warning(
            "Worker worktree default skipped: git reported success but %r "
            "does not exist", worktree,
        )
        return None

    return worktree
