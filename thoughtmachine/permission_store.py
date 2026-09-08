"""
Disk-pure session permission store (sidecar-first, additive).

This module is the storage half of the permission-simplification effort
(see ``.thoughtmachine/working_docs/impl_plan_permission_simplification.md``).
It makes the **disk** the source of truth for raw, uncapped per-session
permission grants.  Nothing else reads or writes it yet -- the tool gate is
refactored in a later phase; this module only *adds* the store plus its
hermetic unit tests (constraint: no modifications to security_gate.py,
tool_executor.py, session/store.py, container launch code, routes, or the
frontend in this round).

Canonical on-disk layout (pinned by the Phase-1 RED test
``tests/test_permission_disk_staleness.py`` and blueprint section 3.1)::

    <vault_root>/workspaces/<workspace_id>/sessions/<session_id>/permissions.json   # sidecar: canonical, raw uncapped grants
    <vault_root>/workspaces/<workspace_id>/sessions/<friendly>_<short6>.json        # session record (legacy metadata fallback)
    <vault_root>/workspaces/<workspace_id>/config.json                              # workspace config (ceiling source)

Read order for session grants: **sidecar first**, then legacy
``metadata.session_config.session_permissions`` inside the session record.
Workspace-scoped session records land under
``workspaces/<ws_id>/sessions/`` as *friendly-named* files
(``{sanitized_name}_{short_id}.json`` -- see ``session/store.py``
``_generate_friendly_filename`` / ``save_session``), so the legacy fallback
locates a record by content scan (``session_id`` field), mirroring
``FileSystemSessionStore._find_session_path`` (session/store.py L175).

Fail-closed contract
--------------------
A read that cannot positively establish grants must never silently grant:

* corrupt JSON (sidecar, session record, or workspace config) -> raises
  :class:`PermissionStoreError`;
* unreadable file (``OSError``) -> raises :class:`PermissionStoreError`;
* no sidecar **and** no matching session record -> raises
  :class:`PermissionStoreError` (unknown session: deny, do not default);
* a session record that exists but records no ``session_permissions`` ->
  returns ``{}`` (no grants recorded; the gate applies defaults afterwards
  -- an empty grant set is neutral, it grants nothing).

Canonical session-grant shape
-----------------------------
Grants are stored and returned in the canonical resource-catalog shape
(``security/resource_catalog.py``): only the session-grant catalog keys
(``git``, ``filesystem``, ``container``, ``network``, ``mcp`` and
``host_bash``) survive at the session level.  Every write AND read
normalises the payload (:func:`_normalize_session_permissions`):

* legacy explicit grains ``git_read`` / ``git_write`` collapse onto ``git``
  when no ``git`` key is present (``git_read`` -> ``git: 'read'``;
  ``git_write`` maps ``write`` / ``ask`` / ``read`` / ``banned`` to the same
  ``git`` level and anything else fails closed to ``banned``; ``git_write``
  overwrites the ``git_read`` result), then the grains are always dropped --
  a present ``git`` key is left untouched;
* ``host_bash`` is a regular catalog session grain (``banned`` /
  ``ask`` / ``allow``) and is preserved; the security gate additionally
  caps the session value by the workspace ceiling.  Non-catalog
  ``git_allow_worktree_commits`` is dropped;
* remaining keys are coerced via ``coerce_resource_permissions``: keys
  outside the catalog (``system``, ``execution``, ...) and invalid
  values are dropped with a warning each.

The workspace ceiling (``workspace_ceiling``) is deliberately NOT coerced:
its canonical key for sandboxed execution is ``container`` (boolean); the
legacy alias ``docker`` is accepted and normalised onto ``container`` by the
security gate at enforcement time (write-level allows the container grant,
banned/read/ask denies it).  The ceiling keeps the wider workspace
vocabulary (``container``, ``host_bash``, ``git_read``, ``git_write``, ...)
and is applied by the security gate.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from security.resource_catalog import coerce_resource_permissions
from thoughtmachine.security import SessionPermissions

__all__ = [
    "PermissionStoreError",
    "session_grants_path",
    "read_session_permissions",
    "write_session_permissions",
    "migrate_session_permissions",
    "workspace_ceiling",
]

logger = logging.getLogger(__name__)


class PermissionStoreError(Exception):
    """Raised when session grants or the workspace ceiling cannot be read or
    written safely.  The gate treats this as a deny signal (fail closed)."""


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def session_grants_path(
    vault_root, workspace_id: str, session_id: str
) -> Path:
    """Absolute path of the permissions sidecar for a session.

    ``<vault_root>/workspaces/<workspace_id>/sessions/<session_id>/permissions.json``
    """
    return (
        Path(vault_root)
        / "workspaces"
        / workspace_id
        / "sessions"
        / session_id
        / "permissions.json"
    )


def _workspace_sessions_dir(vault_root, workspace_id: str) -> Path:
    return (
        Path(vault_root) / "workspaces" / workspace_id / "sessions"
    )


def _session_record_path(
    vault_root, workspace_id: str, session_id: str
) -> Optional[Path]:
    """Locate the session JSON record for ``session_id`` inside the
    workspace-scoped sessions directory by content scan.

    Mirrors ``FileSystemSessionStore._find_session_path`` (session/store.py
    L175): files named ``_meta_*`` are skipped, and unparseable / non-matching
    files are skipped silently (a corrupt record surfaces later as a
    fail-closed read error only when it is the one that matches, or via the
    sidecar read if present).
    """
    sessions_dir = _workspace_sessions_dir(vault_root, workspace_id)
    if not sessions_dir.is_dir():
        return None
    for file_path in sorted(sessions_dir.glob("*.json")):
        if file_path.name.startswith("_meta_"):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("session_id") == session_id:
            return file_path
    return None


def _read_json_strict(path: Path, what: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise PermissionStoreError(f"{what} not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise PermissionStoreError(
            f"{what} is corrupt (invalid JSON): {path} -- {exc}"
        ) from exc
    except OSError as exc:
        raise PermissionStoreError(
            f"{what} unreadable: {path} -- {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PermissionStoreError(
            f"{what} has an unexpected shape (expected JSON object): {path}"
        )
    return data


# ---------------------------------------------------------------------------
# Canonical normalisation (legacy migration + catalog coercion)
# ---------------------------------------------------------------------------


def _normalize_session_permissions(permissions: Dict[str, Any]) -> Dict[str, Any]:
    """Map legacy session grants onto the canonical resource catalog.

    Returns a NEW dict; ``permissions`` is not mutated.  Applied on every
    session-grant write and read so the disk never carries -- and callers
    never see -- pre-catalog junk.  Migration order (permission-simplification
    blueprint, Phase 1):

    1. ``git_read`` present and ``git`` absent -> ``git: 'read'``;
    2. ``git_write`` present and ``git`` absent -> ``git`` set from the
       grain's value (``'write'`` -> ``'write'``, ``'ask'`` -> ``'ask'``,
       ``'read'`` -> ``'read'``, ``'banned'`` -> ``'banned'``; any other
       value fails closed to ``'banned'``).  When both grains are present
       and ``git`` is absent, ``git_write`` overwrites the ``git_read``
       result;
    3. ``git_read`` / ``git_write`` are always dropped afterwards (a present
       ``git`` key is never overwritten by the grains);
    4. ``host_bash`` is kept when its value is a valid catalog level
       (``banned`` / ``ask`` / ``allow``); ``git_allow_worktree_commits``
       is dropped;
    5. the result is coerced via ``security.resource_catalog.
       coerce_resource_permissions``: keys outside the catalog
       (``system``, ``execution``, ...) and invalid values are dropped
       with a warning each.
    """
    result = dict(permissions)
    if "git" not in result:
        if "git_read" in result:
            result["git"] = "read"
        if "git_write" in result:
            grain = result["git_write"]
            if grain in ("write", "ask", "read", "banned"):
                result["git"] = grain
            else:
                result["git"] = "banned"  # fail closed
    result.pop("git_read", None)
    result.pop("git_write", None)
    result.pop("git_allow_worktree_commits", None)
    return coerce_resource_permissions(result)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


def read_session_permissions(
    vault_root, workspace_id: str, session_id: str
) -> Dict[str, Any]:
    """Return the session grants in canonical resource-catalog shape.

    Sidecar first; legacy ``metadata.session_config.session_permissions``
    fallback when the sidecar is absent.  Both sources are normalised on the
    way out (see :func:`_normalize_session_permissions`): legacy git grains
    are migrated and unknown/invalid entries are dropped, so callers never
    see pre-catalog junk.  Fail closed -- see module docstring.
    """
    sidecar = session_grants_path(vault_root, workspace_id, session_id)
    if sidecar.exists():
        return _normalize_session_permissions(
            _read_json_strict(sidecar, "permissions sidecar")
        )

    record = _session_record_path(vault_root, workspace_id, session_id)
    if record is None:
        raise PermissionStoreError(
            f"no permission source for session {session_id!r} in workspace "
            f"{workspace_id!r}: no sidecar at {sidecar} and no matching "
            "session record"
        )
    data = _read_json_strict(record, "session record")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    session_config = metadata.get("session_config")
    if not isinstance(session_config, dict):
        return {}
    legacy = session_config.get("session_permissions")
    if legacy is None:
        return {}
    if not isinstance(legacy, dict):
        raise PermissionStoreError(
            f"session record {record} has non-object "
            "metadata.session_config.session_permissions"
        )
    return _normalize_session_permissions(legacy)


def workspace_ceiling(vault_root, workspace_id: str) -> Dict[str, Any]:
    """Return the workspace ceiling map from ``config.json['permissions']``.

    The ceiling is exposed separately from session grants so the gate can
    apply it at enforcement time (``security_gate.apply_workspace_ceiling``
    later).  Fail closed: a missing or corrupt ``config.json`` raises
    :class:`PermissionStoreError` (an unreadable ceiling must never be
    treated as "no ceiling").  A config that exists but carries no
    ``permissions`` key yields ``{}`` -- the caller is expected to apply the
    purpose-preset/default fallback (blueprint section 8).
    """
    config_path = (
        Path(vault_root) / "workspaces" / workspace_id / "config.json"
    )
    data = _read_json_strict(config_path, "workspace config")
    permissions = data.get("permissions")
    if permissions is None:
        return {}
    if not isinstance(permissions, dict):
        raise PermissionStoreError(
            f"workspace config {config_path} has non-object 'permissions'"
        )
    return dict(permissions)


# ---------------------------------------------------------------------------
# Write path (atomic)
# ---------------------------------------------------------------------------


def _coerce_permissions_dict(permissions) -> Dict[str, Any]:
    if isinstance(permissions, SessionPermissions):
        return permissions.model_dump()
    if isinstance(permissions, dict):
        return dict(permissions)
    raise PermissionStoreError(
        "permissions must be a raw dict or a SessionPermissions instance, "
        f"got {type(permissions).__name__}"
    )


def write_session_permissions(
    vault_root,
    workspace_id: str,
    session_id: str,
    permissions,
    *,
    fsync: bool = True,
) -> Path:
    """Atomically write session grants to the sidecar in canonical shape.

    Accepts a raw ``dict`` (e.g. ``{'filesystem': 'read', 'git': 'write'}``)
    or a :class:`SessionPermissions` instance.  The payload is normalised
    before writing (see :func:`_normalize_session_permissions`): legacy
    ``git_read`` / ``git_write`` grains collapse onto ``git``,
    ``git_allow_worktree_commits`` is dropped, and only canonical
    resource-catalog keys with valid values survive (``host_bash`` is a
    catalog key and is preserved, to be ceiling-capped by the gate at
    enforcement time) -- the sidecar never stores ``system`` / ``execution``
    or keys outside the catalog.  Writes via a temp file in the same directory +
    ``os.replace`` (with optional fsync of file and directory), so a crash or
    failure never leaves a partial ``permissions.json``.  Returns the sidecar
    path.
    """
    perms = _normalize_session_permissions(_coerce_permissions_dict(permissions))
    target = session_grants_path(vault_root, workspace_id, session_id)
    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    tmp = target_dir / (
        f".permissions.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(perms, f, indent=2, sort_keys=True)
            f.write("\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if fsync:
            try:
                dir_fd = os.open(target_dir, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # Directory fsync is best-effort (some platforms disallow it).
                pass
        os.replace(tmp, target)
    except PermissionStoreError:
        raise
    except OSError as exc:
        raise PermissionStoreError(
            f"failed to write permissions sidecar {target}: {exc}"
        ) from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


# ---------------------------------------------------------------------------
# Migration / dual-write helper
# ---------------------------------------------------------------------------


def migrate_session_permissions(
    vault_root, workspace_id: str, session_id: str
) -> bool:
    """Write the sidecar from legacy embedded metadata when it is absent.

    Idempotent and purely additive:

    * sidecar already present -> ``False`` (nothing to do; the sidecar is
      authoritative and is never overwritten from legacy metadata);
    * no sidecar, session record carries ``metadata.session_config.
      session_permissions`` -> sidecar written, ``True``;
    * no sidecar, session record present but with no legacy grants ->
      ``False`` (nothing to migrate; reads will fall back to ``{}``);
    * no sidecar and no session record -> :class:`PermissionStoreError`
      (nothing to migrate from; do not fabricate grants).

    Existing sessions therefore gain a sidecar without data loss; legacy
    fallback reads keep working for sessions that predate migration.
    """
    sidecar = session_grants_path(vault_root, workspace_id, session_id)
    if sidecar.exists():
        return False

    record = _session_record_path(vault_root, workspace_id, session_id)
    if record is None:
        raise PermissionStoreError(
            f"cannot migrate session {session_id!r} in workspace "
            f"{workspace_id!r}: no sidecar and no matching session record"
        )
    data = _read_json_strict(record, "session record")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return False
    session_config = metadata.get("session_config")
    if not isinstance(session_config, dict):
        return False
    legacy = session_config.get("session_permissions")
    if legacy is None:
        return False
    if not isinstance(legacy, dict):
        raise PermissionStoreError(
            f"session record {record} has non-object "
            "metadata.session_config.session_permissions"
        )
    write_session_permissions(
        vault_root, workspace_id, session_id, dict(legacy)
    )
    return True
