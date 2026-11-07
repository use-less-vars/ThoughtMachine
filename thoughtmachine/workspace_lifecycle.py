"""
workspace_lifecycle.py — Workspace teardown orchestrator.

``delete_workspace()`` removes every trace of a workspace in a fixed,
independently-reported step order:

1. ``user_containers``             — stop + remove every container labelled
                                     ``thoughtmachine.workspace_id=<id>``
                                     (``infra.container_manager.cleanup_workspace``)
2. ``resource_containers_and_image`` — force-remove hidden resource containers
                                     and the resource image when unused
                                     (``infra.resource_container_manager.cleanup_workspace_resources``)
3. ``package_volume``              — remove the per-workspace pip cache volume
                                     ``tm-packages-<workspace_id>``
4. ``workspace_sessions``          — delete the workspace's session files and
                                     drop its entries from ``open_sessions.json``
                                     / the ``.current_session`` marker
                                     (``session.store.FileSystemSessionStore``)
5. ``workspace_vault_dir``         — delete ``<vault_root>/workspaces/<id>``
                                     (also holds worker state, container notes,
                                     capabilities, config)
6. ``registry_unregister``         — remove the entry from the workspace
                                     registry (``WorkspaceRegistry``)

The registry ``root_path`` is captured into the report for reference but is
NEVER deleted — the workspace's on-disk project files belong to the user.

Contract
--------
- ``delete_workspace`` NEVER raises. Every step runs independently; a failed
  step is recorded in ``errors`` and the remaining steps still run.
- ``dry_run=True`` performs no mutations at all (it does run the read-only
  registry lookup) and reports every cleanup step under ``would_remove``.
- All vault paths flow through ``thoughtmachine.vault.vault_root()``.
- Directory deletion never follows symlinks (refused with an error entry).
"""

import logging
import shutil
import sys
from pathlib import Path

try:
    import docker
except ImportError:  # pragma: no cover - docker SDK is optional to import
    docker = None

from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_registry import WorkspaceRegistry

logger = logging.getLogger(__name__)

# ── Step names (execution order; also used in the report) ──────────────────
_STEP_USER_CONTAINERS = "user_containers"
_STEP_RESOURCE_CLEANUP = "resource_containers_and_image"
_STEP_PACKAGE_VOLUME = "package_volume"
_STEP_WORKSPACE_SESSIONS = "workspace_sessions"
_STEP_WORKSPACE_VAULT_DIR = "workspace_vault_dir"
_STEP_REGISTRY_UNREGISTER = "registry_unregister"

_CLEANUP_STEPS = (
    _STEP_USER_CONTAINERS,
    _STEP_RESOURCE_CLEANUP,
    _STEP_PACKAGE_VOLUME,
    _STEP_WORKSPACE_SESSIONS,
    _STEP_WORKSPACE_VAULT_DIR,
    _STEP_REGISTRY_UNREGISTER,
)


# ── Small helpers ────────────────────────────────────────────────────────────


def _docker_client():
    """Return a Docker client, or None when the SDK/daemon is unavailable.

    Never raises — the caller decides how to treat a missing client.
    """
    if docker is None:
        return None
    try:
        return docker.from_env()
    except Exception:
        return None


def _is_not_found(exc: Exception) -> bool:
    """Return True when *exc* means the requested docker object does not exist."""
    if docker is not None:
        try:
            from docker.errors import NotFound
        except Exception:
            NotFound = ()  # pragma: no cover - docker SDK present by here
        else:
            if isinstance(exc, NotFound):
                return True
    # Duck-typed fallback so docker-free environments/tests can raise their
    # own NotFound-shaped exception.
    return type(exc).__name__ == "NotFound"


def _workspace_vault_dir(workspace_id: str) -> Path:
    """The workspace's vault directory: <vault_root>/workspaces/<id>."""
    return vault_root() / "workspaces" / workspace_id


def _sessions_dir(workspace_id: str) -> Path:
    """The workspace's session directory inside the vault."""
    return _workspace_vault_dir(workspace_id) / "sessions"


def _package_volume_name(workspace_id: str) -> str:
    """Per-workspace pip cache volume name (mirrors docker_executor)."""
    return f"tm-packages-{workspace_id}"


def _rmtree_one_shot(path: Path) -> None:
    """Delete a directory tree in a single pass, never following symlinks.

    Per-file failures are collected (rmtree's ``onerror``/``onexc`` keeps the
    sweep from aborting on the first error) and re-raised as one
    ``RuntimeError`` so the step is reported as failed instead of silently
    leaving a partial deletion behind.
    """
    if path.is_symlink():
        raise RuntimeError(f"refusing to delete symlink: {path}")

    failures: list = []

    if sys.version_info >= (3, 12):

        def _onexc(_func, p, exc):
            failures.append(f"{p}: {exc}")

        shutil.rmtree(path, onexc=_onexc)
    else:

        def _onerror(_func, p, exc_info):
            failures.append(f"{p}: {exc_info[1]}")

        shutil.rmtree(path, onerror=_onerror)

    if failures:
        raise RuntimeError("rmtree left artifacts: " + "; ".join(failures))


# ── Per-step implementations (each returns True=removed / False=skipped) ────


def _cleanup_user_containers(workspace_id: str) -> bool:
    """Stop and remove every container labelled with this workspace."""
    # Imported here so tests can monkeypatch the infra module attribute and
    # so this module stays importable without the infra stack.
    from infra.container_manager import cleanup_workspace  # noqa: PLC0415

    client = _docker_client()
    if client is None:
        raise RuntimeError("docker unavailable")
    result = cleanup_workspace(workspace_id, client)
    return bool(result.get("removed", 0) > 0)


def _cleanup_resource_containers_and_image(workspace_id: str) -> bool:
    """Remove hidden resource containers and the resource image if unused."""
    from infra.resource_container_manager import cleanup_workspace_resources  # noqa: PLC0415

    result = cleanup_workspace_resources(workspace_id)
    if result.get("removed_containers") or result.get("removed_image"):
        return True
    detail = result.get("detail") or ""
    if "not installed" in detail or "unavailable" in detail:
        raise RuntimeError(detail)
    return False


def _cleanup_package_volume(workspace_id: str) -> bool:
    """Remove the per-workspace pip cache volume, if it exists."""
    client = _docker_client()
    if client is None:
        raise RuntimeError("docker unavailable")
    try:
        volume = client.volumes.get(_package_volume_name(workspace_id))
    except Exception as exc:
        if _is_not_found(exc):
            return False
        raise
    volume.remove(force=True)
    return True


def _cleanup_workspace_sessions(workspace_id: str) -> bool:
    """Delete the workspace's session files and session bookkeeping."""
    from session.store import FileSystemSessionStore  # noqa: PLC0415

    workspace_vault_dir = _workspace_vault_dir(workspace_id)
    if workspace_vault_dir.is_symlink():
        raise RuntimeError(f"refusing to delete symlink: {workspace_vault_dir}")

    # Store dirs are derived from vault_root() so cleanup is hermetic and
    # consistent with the vault layout (state/open_sessions.json,
    # state/.current_session, sessions/ legacy dir).
    store = FileSystemSessionStore(
        state_dir=str(vault_root() / "state"),
        sessions_dir=str(vault_root() / "sessions"),
    )
    sessions_dir = _sessions_dir(workspace_id)

    # Drop open-session entries for sessions stored in this workspace.
    for meta in store.list_sessions(workspace_id=workspace_id):
        session_id = meta.get("session_id")
        if not session_id:
            continue
        try:
            store.remove_open_session(session_id)
        except Exception:
            logger.warning(
                "could not drop open-session entry %s", session_id, exc_info=True
            )

    # Clear the current-session marker only if it points into this workspace.
    current = store.get_current_session_id()
    if current:
        try:
            path = store.get_session_path(current)
            if Path(path).resolve().parent == sessions_dir.resolve():
                store.set_current_session_id(None)
        except Exception:
            logger.warning("could not inspect current-session marker", exc_info=True)

    if sessions_dir.exists():
        _rmtree_one_shot(sessions_dir)
        return True
    return False


def _cleanup_workspace_vault_dir(workspace_id: str) -> bool:
    """Delete the workspace's vault directory (never follows symlinks)."""
    target = _workspace_vault_dir(workspace_id)
    if not target.exists():
        return False
    _rmtree_one_shot(target)
    return True


def _unregister_workspace(registry, entry, workspace_id: str) -> bool:
    """Remove the registry entry (only when it existed)."""
    if entry is None:
        return False
    return bool(registry.unregister_workspace(workspace_id))


# ── Public orchestrator ─────────────────────────────────────────────────────


def delete_workspace(workspace_id: str, *, dry_run: bool = False) -> dict:
    """Remove every trace of *workspace_id*; return a per-step report.

    Args:
        workspace_id: The workspace to tear down.
        dry_run: If True, perform no mutations; report every cleanup step
            under ``would_remove`` instead of executing it. The read-only
            registry lookup still runs so the report carries the registry
            state.

    Returns:
        A report dict::

            {
                "workspace_id": str,
                "dry_run": bool,
                "removed": [step names actually cleaned],
                "would_remove": [step names, only in dry_run mode],
                "skipped": [step names with nothing to do],
                "errors": [{"step": str, "error": str}, ...],
                "root_path": str | None,   # registry root_path, never deleted
                "registered": bool,        # whether the registry had an entry
            }

    Never raises — failures are collected in ``errors`` and the remaining
    steps still run.
    """
    report = {
        "workspace_id": workspace_id,
        "dry_run": bool(dry_run),
        "removed": [],
        "would_remove": [],
        "skipped": [],
        "errors": [],
        "root_path": None,
        "registered": False,
    }

    # Preliminary, read-only: capture the registry entry for the report and
    # for the unregister step. Runs in dry_run too — it mutates nothing.
    registry = WorkspaceRegistry()
    entry = None
    try:
        entry = registry.get_workspace(workspace_id)
        report["registered"] = entry is not None
        report["root_path"] = entry.root_path if entry is not None else None
    except Exception as exc:
        report["errors"].append({"step": "registry_lookup", "error": str(exc)})

    steps = (
        (_STEP_USER_CONTAINERS, lambda: _cleanup_user_containers(workspace_id)),
        (
            _STEP_RESOURCE_CLEANUP,
            lambda: _cleanup_resource_containers_and_image(workspace_id),
        ),
        (_STEP_PACKAGE_VOLUME, lambda: _cleanup_package_volume(workspace_id)),
        (_STEP_WORKSPACE_SESSIONS, lambda: _cleanup_workspace_sessions(workspace_id)),
        (_STEP_WORKSPACE_VAULT_DIR, lambda: _cleanup_workspace_vault_dir(workspace_id)),
        (
            _STEP_REGISTRY_UNREGISTER,
            lambda: _unregister_workspace(registry, entry, workspace_id),
        ),
    )

    for step_name, run_step in steps:
        if dry_run:
            report["would_remove"].append(step_name)
            continue
        try:
            done = run_step()
        except Exception as exc:
            report["errors"].append({"step": step_name, "error": str(exc)})
            continue
        (report["removed"] if done else report["skipped"]).append(step_name)

    return report
