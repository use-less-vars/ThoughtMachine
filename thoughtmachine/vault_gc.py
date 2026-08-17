"""
vault_gc.py — Age-based garbage collection (GC) for the ThoughtMachine vault.

``run_gc()`` sweeps five categories of stale/orphaned vault artifacts. Each
category has its own age threshold, overridable at call time via ``TM_GC_*``
environment variables (module-level constants are the defaults):

+-------------------------------+-----------------------------------------------+------------------------+
| category                      | criterion                                     | default threshold      |
+===============================+===============================================+========================+
| ``stale_workspaces``          | registry entries whose last activity          | 90 days                |
|                               | (``last_opened`` → ``updated_at`` →           |                        |
|                               | ``created_at``) is older than the threshold   |                        |
|                               | AND outside the active window; removed via    |                        |
|                               | ``workspace_lifecycle.delete_workspace``      |                        |
+-------------------------------+-----------------------------------------------+------------------------+
| ``orphan_workspace_dirs``     | ``<vault>/workspaces/<id>`` directories NOT   | 7 days (dir mtime)     |
|                               | registered in the registry and older than     |                        |
|                               | the threshold                                 |                        |
+-------------------------------+-----------------------------------------------+------------------------+
| ``orphan_sessions``           | session files (legacy ``<vault>/sessions``    | 90 days                |
|                               | and workspace-scoped ``.../sessions`` dirs)   |                        |
|                               | that are NOT open and older than the          |                        |
|                               | threshold                                     |                        |
+-------------------------------+-----------------------------------------------+------------------------+
| ``orphan_resource_containers``| STOPPED containers older than the            | 24 hours (CreatedAt)   |
|                               | threshold: ``thoughtmachine.resource``        |                        |
|                               | containers, plus user containers              |                        |
|                               | (``thoughtmachine.workspace_id=<id>``) of     |                        |
|                               | workspaces no longer in the registry;         |                        |
|                               | running / paused / restarting containers      |                        |
|                               | are NEVER touched                             |                        |
+-------------------------------+-----------------------------------------------+------------------------+
| ``orphan_volumes``            | ``tm-packages-<id>`` volumes that do NOT      | 7 days (CreatedAt)     |
|                               | belong to a registered workspace and are      |                        |
|                               | older than the threshold                      |                        |
+-------------------------------+-----------------------------------------------+------------------------+

Env overrides (read fresh on every ``run_gc`` call):

* ``TM_GC_STALE_WORKSPACE_DAYS``             (default 90)
* ``TM_GC_ORPHAN_WORKSPACE_DIR_DAYS``        (default 7)
* ``TM_GC_ORPHAN_SESSION_DAYS``              (default 90)
* ``TM_GC_ORPHAN_RESOURCE_CONTAINER_HOURS``  (default 24)
* ``TM_GC_ORPHAN_VOLUME_DAYS``               (default 7)
* ``TM_GC_ACTIVE_WINDOW_DAYS``               (default 7)

Safety rules
------------
- Active window: a registered workspace whose last activity falls inside the
  active window is NEVER auto-deleted, regardless of the stale threshold.
- Pinned workspaces (``metadata.pinned`` truthy on the registry entry) are
  NEVER auto-deleted, regardless of age.
- Workspaces referenced by open sessions (open session files under the
  workspace's session directory) are NEVER auto-deleted.
- Workspaces with in-use containers (running / paused / restarting) are NEVER
  auto-deleted.
- In-use containers (running / paused / restarting) are never removed.
- Workspace directories that are symlinks are never followed (refused).
- ``run_gc`` NEVER raises: per-item failures land in the category ``errors``
  list, category-level failures (registry unreadable, docker unavailable)
  land in the top-level ``errors`` list, and the remaining categories still
  run.
- ``dry_run=True`` performs no mutation at all and reports every eligible
  item under ``would_remove``.

Report shape
------------
::

    {
        "dry_run": bool,
        "now": "<ISO timestamp>",
        "categories": {
            <category name>: {
                "removed": [item ids actually removed],
                "would_remove": [item ids, dry-run only],
                "skipped": [{"id": str, "reason": str}, ...],
                "errors": [{"id": str, "error": str, ...}, ...],
            },
            ...
        },
        "counts": {
            <category name>: {"removed": n, "would_remove": n,
                              "skipped": n, "errors": n},
            ...
        },
        "errors": [{"category": str, "error": str}, ...],
    }

Item ids: workspace id for ``stale_workspaces``/``orphan_sessions``, the
directory path for ``orphan_workspace_dirs``, the container id for
``orphan_resource_containers`` and the volume name for ``orphan_volumes``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import docker
except ImportError:  # pragma: no cover - docker SDK is optional to import
    docker = None

from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_lifecycle import _rmtree_one_shot, delete_workspace
from thoughtmachine.workspace_registry import WorkspaceRegistry

logger = logging.getLogger(__name__)


# ── Threshold defaults (env-overridable at call time via TM_GC_*) ──────────

def _env_int(name: str, default: int) -> int:
    """Parse *name* from the environment; fall back to *default* when unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


STALE_WORKSPACE_DAYS = _env_int("TM_GC_STALE_WORKSPACE_DAYS", 90)
ORPHAN_WORKSPACE_DIR_DAYS = _env_int("TM_GC_ORPHAN_WORKSPACE_DIR_DAYS", 7)
ORPHAN_SESSION_DAYS = _env_int("TM_GC_ORPHAN_SESSION_DAYS", 90)
ORPHAN_RESOURCE_CONTAINER_HOURS = _env_int("TM_GC_ORPHAN_RESOURCE_CONTAINER_HOURS", 24)
ORPHAN_VOLUME_DAYS = _env_int("TM_GC_ORPHAN_VOLUME_DAYS", 7)
ACTIVE_WINDOW_DAYS = _env_int("TM_GC_ACTIVE_WINDOW_DAYS", 7)

# (report key, env var, module constant name)
_THRESHOLD_SPECS = (
    ("stale_workspace_days", "TM_GC_STALE_WORKSPACE_DAYS", "STALE_WORKSPACE_DAYS"),
    (
        "orphan_workspace_dir_days",
        "TM_GC_ORPHAN_WORKSPACE_DIR_DAYS",
        "ORPHAN_WORKSPACE_DIR_DAYS",
    ),
    ("orphan_session_days", "TM_GC_ORPHAN_SESSION_DAYS", "ORPHAN_SESSION_DAYS"),
    (
        "orphan_resource_container_hours",
        "TM_GC_ORPHAN_RESOURCE_CONTAINER_HOURS",
        "ORPHAN_RESOURCE_CONTAINER_HOURS",
    ),
    ("orphan_volume_days", "TM_GC_ORPHAN_VOLUME_DAYS", "ORPHAN_VOLUME_DAYS"),
    ("active_window_days", "TM_GC_ACTIVE_WINDOW_DAYS", "ACTIVE_WINDOW_DAYS"),
)


def _thresholds() -> dict:
    """Return the current GC thresholds (env overrides applied per call).

    Env vars win; otherwise the (possibly monkeypatched) module-level default
    constants are used, so tests can override either way.
    """
    return {
        key: _env_int(env, globals()[default])
        for key, env, default in _THRESHOLD_SPECS
    }


# ── Category names / report helpers ────────────────────────────────────────

CAT_STALE_WORKSPACES = "stale_workspaces"
CAT_ORPHAN_WORKSPACE_DIRS = "orphan_workspace_dirs"
CAT_ORPHAN_SESSIONS = "orphan_sessions"
CAT_ORPHAN_RESOURCE_CONTAINERS = "orphan_resource_containers"
CAT_ORPHAN_VOLUMES = "orphan_volumes"

_CATEGORIES = (
    CAT_STALE_WORKSPACES,
    CAT_ORPHAN_WORKSPACE_DIRS,
    CAT_ORPHAN_SESSIONS,
    CAT_ORPHAN_RESOURCE_CONTAINERS,
    CAT_ORPHAN_VOLUMES,
)

_RESOURCE_LABEL = "thoughtmachine.resource"
_WORKSPACE_LABEL = "thoughtmachine.workspace_id"
_PACKAGE_VOLUME_PREFIX = "tm-packages-"
# Container states that mean "in use" — never GCed.
_IN_USE_STATUSES = {"running", "paused", "restarting", "removing"}


def _new_category() -> dict:
    """A fresh per-category report section."""
    return {"removed": [], "would_remove": [], "skipped": [], "errors": []}


def _empty_report(dry_run: bool, now: datetime) -> dict:
    """An empty (all-zero) report scaffold."""
    categories = {name: _new_category() for name in _CATEGORIES}
    zero = {"removed": 0, "would_remove": 0, "skipped": 0, "errors": 0}
    return {
        "dry_run": bool(dry_run),
        "now": now.isoformat(),
        "categories": categories,
        "counts": {name: dict(zero) for name in _CATEGORIES},
        "errors": [],
    }


def _fill_counts(report: dict) -> None:
    """Recompute ``counts`` from the per-category lists (mutates report)."""
    for name, cat in report["categories"].items():
        report["counts"][name] = {
            "removed": len(cat["removed"]),
            "would_remove": len(cat["would_remove"]),
            "skipped": len(cat["skipped"]),
            "errors": len(cat["errors"]),
        }


# ── Small shared helpers ───────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value) -> datetime:
    """Coerce *value* (datetime or ISO string) to an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = _parse_timestamp(value) or _utcnow()
    else:
        dt = _utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_timestamp(value) -> datetime | None:
    """Parse an ISO-8601 timestamp (registry, docker ``Created``/``CreatedAt``).

    Handles ``Z`` suffixes, fractional seconds and naive timestamps (assumed
    UTC). Returns None when unparseable.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _docker_client():
    """Return a Docker client, or None when the SDK/daemon is unavailable."""
    if docker is None:
        return None
    try:
        return docker.from_env()
    except Exception:
        return None


def _entry_last_activity(entry) -> str:
    """Best last-activity timestamp of a registry entry (ISO string or "")."""
    for key in ("last_opened", "updated_at", "created_at"):
        value = getattr(entry, key, None)
        if value:
            return str(value)
    return ""


# ── Category sweeps (each is dry-run aware and never raises per-item) ──────

def _gc_stale_workspaces(report, entries, now, thresholds, dry_run, docker_client=None) -> None:
    """GC registry workspaces inactive beyond the stale threshold.

    Read-only retention guards are checked before any removal (all dry-run
    safe): pinned metadata, open sessions referencing the workspace, and
    in-use containers for the workspace.
    """
    cat = report["categories"][CAT_STALE_WORKSPACES]
    active_window = timedelta(days=thresholds["active_window_days"])
    stale_after = timedelta(days=thresholds["stale_workspace_days"])
    open_ws_ids = _open_session_workspace_ids()
    in_use_ws_ids = _in_use_workspace_ids(docker_client)

    for entry in entries:
        ws_id = getattr(entry, "id", None)
        if not ws_id:
            cat["skipped"].append({"id": None, "reason": "entry without id"})
            continue
        ts = _parse_timestamp(_entry_last_activity(entry))
        if ts is None:
            cat["skipped"].append(
                {"id": ws_id, "reason": "no parseable last-activity timestamp"}
            )
            continue
        age = now - ts
        if age < timedelta(0):
            cat["skipped"].append({"id": ws_id, "reason": "timestamp in the future"})
            continue
        if age <= active_window:
            cat["skipped"].append(
                {"id": ws_id, "reason": "active (within active window)"}
            )
            continue
        if age <= stale_after:
            cat["skipped"].append({"id": ws_id, "reason": "not old enough"})
            continue

        # Retention guards (read-only, dry-run safe). A workspace is kept
        # when pinned, when an open session references it, or when it has an
        # in-use container.
        metadata = getattr(entry, "metadata", None) or {}
        if metadata.get("pinned"):
            cat["skipped"].append(
                {"id": ws_id, "reason": "pinned (metadata.pinned)"}
            )
            continue
        if ws_id in open_ws_ids:
            cat["skipped"].append({"id": ws_id, "reason": "has open sessions"})
            continue
        if in_use_ws_ids and ws_id in in_use_ws_ids:
            cat["skipped"].append(
                {"id": ws_id, "reason": "has in-use containers"}
            )
            continue

        if dry_run:
            cat["would_remove"].append(ws_id)
            continue
        try:
            result = delete_workspace(ws_id)
        except Exception as exc:  # pragma: no cover - delete_workspace never raises
            cat["errors"].append({"id": ws_id, "error": str(exc)})
            continue
        if result.get("errors"):
            for err in result["errors"]:
                cat["errors"].append(
                    {
                        "id": ws_id,
                        "step": err.get("step"),
                        "error": err.get("error"),
                    }
                )
        else:
            cat["removed"].append(ws_id)


def _gc_orphan_workspace_dirs(report, registered_ids, now, thresholds, dry_run) -> None:
    """GC vault workspace directories that are not registered and old.

    ``registered_ids`` is None when the registry could not be read — the
    category is then skipped entirely (deleting dirs without knowing which
    workspaces are registered would be dangerous).
    """
    cat = report["categories"][CAT_ORPHAN_WORKSPACE_DIRS]
    if registered_ids is None:
        cat["errors"].append({"id": None, "error": "registry unavailable; skipped"})
        return

    root = vault_root() / "workspaces"
    if not root.is_dir():
        return
    cutoff = timedelta(days=thresholds["orphan_workspace_dir_days"])
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        cat["errors"].append({"id": None, "error": str(exc)})
        return

    for child in children:
        if child.is_symlink():
            cat["skipped"].append({"id": str(child), "reason": "symlink (refusing)"})
            continue
        if not child.is_dir():
            cat["skipped"].append({"id": str(child), "reason": "not a directory"})
            continue
        if child.name in registered_ids:
            cat["skipped"].append(
                {"id": str(child), "reason": "registered workspace"}
            )
            continue
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            cat["errors"].append({"id": str(child), "error": str(exc)})
            continue
        age = now - mtime
        if age < timedelta(0):
            cat["skipped"].append({"id": str(child), "reason": "mtime in the future"})
            continue
        if age <= cutoff:
            cat["skipped"].append({"id": str(child), "reason": "not old enough"})
            continue

        if dry_run:
            cat["would_remove"].append(str(child))
            continue
        try:
            _rmtree_one_shot(child)
        except Exception as exc:
            cat["errors"].append({"id": str(child), "error": str(exc)})
            continue
        cat["removed"].append(str(child))


_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"')


def _json_string_field(text: str, key: str) -> str | None:
    """Extract the string value of *key* from a JSON document (regex, cheap)."""
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"', text)
    return match.group(1) if match else None


def _extract_session_meta(path: Path) -> dict | None:
    """Lightweight session meta (session_id/created_at/updated_at) from a file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SESSION_ID_RE.search(text)
    if not match:
        return None
    return {
        "session_id": match.group(1),
        "created_at": _json_string_field(text, "created_at"),
        "updated_at": _json_string_field(text, "updated_at"),
    }


def _open_session_ids() -> set:
    """Session ids currently open (open_sessions.json + .current_session)."""
    ids: set = set()
    state_dir = vault_root() / "state"
    try:
        data = json.loads((state_dir / "open_sessions.json").read_text(encoding="utf-8"))
        if isinstance(data, list):
            ids.update(str(x) for x in data)
    except (OSError, ValueError):
        pass
    try:
        current = (state_dir / ".current_session").read_text(encoding="utf-8").strip()
        if current:
            ids.add(current)
    except OSError:
        pass
    return ids


def _session_dirs() -> list:
    """Vault session directories: legacy ``<vault>/sessions`` plus every
    ``<vault>/workspaces/<id>/sessions`` (symlinked workspace dirs skipped)."""
    dirs: list = []
    base = vault_root()
    legacy = base / "sessions"
    if legacy.is_dir() and not legacy.is_symlink():
        dirs.append(legacy)
    ws_root = base / "workspaces"
    if ws_root.is_dir():
        try:
            children = sorted(ws_root.iterdir(), key=lambda p: p.name)
        except OSError:
            children = []
        for child in children:
            if child.is_symlink():
                continue
            sessions_dir = child / "sessions"
            if sessions_dir.is_dir():
                dirs.append(sessions_dir)
    return dirs


def _open_session_workspace_ids() -> set:
    """Workspace ids referenced by currently-open session files.

    Uses the same open-session source as the orphan-sessions sweep
    (``_open_session_ids``) and maps each open session id to the workspace
    whose session directory holds that session file. Legacy
    ``<vault>/sessions`` files cannot be attributed to a workspace and never
    protect one.
    """
    open_ids = _open_session_ids()
    if not open_ids:
        return set()
    ws_ids: set = set()
    for sessions_dir in _session_dirs():
        if sessions_dir.parent.parent.name != "workspaces":
            continue  # legacy <vault>/sessions - not workspace-scoped
        ws_id = sessions_dir.parent.name
        try:
            files = sorted(sessions_dir.glob("*.json"), key=lambda p: p.name)
        except OSError:
            continue
        for path in files:
            if path.name.startswith("_meta_"):
                continue
            meta = _extract_session_meta(path)
            if meta and meta.get("session_id") in open_ids:
                ws_ids.add(ws_id)
    return ws_ids


def _in_use_workspace_ids(client) -> set | None:
    """Workspace ids with at least one in-use container (read-only).

    Any container labelled ``thoughtmachine.workspace_id=<id>`` (user
    containers and resource containers both carry the label) whose status is
    running/paused/restarting/removing marks the workspace as protected.
    Returns None when the container list cannot be read (e.g. docker
    unavailable) - callers then skip this guard rather than guessing.
    """
    if client is None:
        return None
    try:
        containers = client.containers.list(
            all=True, filters={"label": [_WORKSPACE_LABEL]}
        )
    except Exception:
        return None
    ws_ids: set = set()
    for container in containers:
        labels = getattr(container, "labels", None) or {}
        ws_id = labels.get(_WORKSPACE_LABEL)
        if not ws_id:
            continue
        status = (getattr(container, "status", "") or "").lower()
        if status in _IN_USE_STATUSES:
            ws_ids.add(ws_id)
    return ws_ids


def _gc_orphan_sessions(report, now, thresholds, dry_run) -> None:
    """GC session files that are not open and older than the threshold."""
    cat = report["categories"][CAT_ORPHAN_SESSIONS]
    cutoff = timedelta(days=thresholds["orphan_session_days"])
    open_ids = _open_session_ids()

    for sessions_dir in _session_dirs():
        try:
            files = sorted(sessions_dir.glob("*.json"), key=lambda p: p.name)
        except OSError as exc:
            cat["errors"].append({"id": str(sessions_dir), "error": str(exc)})
            continue
        for path in files:
            if path.name.startswith("_meta_"):
                continue
            meta = _extract_session_meta(path)
            if not meta or not meta.get("session_id"):
                cat["skipped"].append(
                    {"id": str(path), "reason": "unreadable/unparseable session file"}
                )
                continue
            sid = meta["session_id"]
            if sid in open_ids:
                cat["skipped"].append({"id": sid, "reason": "session is open"})
                continue
            ts = _parse_timestamp(meta.get("updated_at") or meta.get("created_at"))
            if ts is None:
                cat["skipped"].append(
                    {"id": sid, "reason": "no parseable timestamp"}
                )
                continue
            age = now - ts
            if age < timedelta(0):
                cat["skipped"].append({"id": sid, "reason": "timestamp in the future"})
                continue
            if age <= cutoff:
                cat["skipped"].append({"id": sid, "reason": "not old enough"})
                continue

            if dry_run:
                cat["would_remove"].append(sid)
                continue
            try:
                path.unlink(missing_ok=True)
                meta_path = path.with_name(f"_meta_{sid}.json")
                if meta_path.exists():
                    meta_path.unlink()
            except OSError as exc:
                cat["errors"].append({"id": sid, "error": str(exc)})
                continue
            cat["removed"].append(sid)


def _maybe_remove_container(cat, container, now, cutoff, dry_run) -> None:
    """Apply the stopped/age/dry-run rules to a single container (never raises)."""
    cid = (
        getattr(container, "id", "")
        or getattr(container, "name", "")
        or str(container)
    )
    status = (getattr(container, "status", "") or "").lower()
    if status in _IN_USE_STATUSES:
        cat["skipped"].append({"id": cid, "reason": f"in use (status={status})"})
        return
    attrs = getattr(container, "attrs", None) or {}
    created = _parse_timestamp(attrs.get("Created") or getattr(container, "created", ""))
    if created is None:
        cat["skipped"].append(
            {"id": cid, "reason": "no parseable Created timestamp"}
        )
        return
    age = now - created
    if age < timedelta(0):
        cat["skipped"].append({"id": cid, "reason": "Created in the future"})
        return
    if age <= cutoff:
        cat["skipped"].append({"id": cid, "reason": "not old enough"})
        return

    if dry_run:
        cat["would_remove"].append(cid)
        return
    try:
        container.remove(force=True)
    except Exception as exc:
        cat["errors"].append({"id": cid, "error": str(exc)})
        return
    cat["removed"].append(cid)


def _gc_orphan_resource_containers(report, client, registered_ids, now, thresholds, dry_run) -> None:
    """GC stopped orphan containers older than threshold.

    Two sweeps share this category:

    * ``thoughtmachine.resource`` containers (sandboxes etc.) — always
      eligible regardless of the registry.
    * ``thoughtmachine.workspace_id=<id>`` *user* containers whose workspace
      is no longer registered (the stale-workspace path already removes user
      containers of registered workspaces via ``delete_workspace``).

    Both are stopped-only and use the same age cutoff.
    """
    cat = report["categories"][CAT_ORPHAN_RESOURCE_CONTAINERS]
    if client is None:
        report["errors"].append(
            {"category": CAT_ORPHAN_RESOURCE_CONTAINERS, "error": "docker unavailable"}
        )
        return
    cutoff = timedelta(hours=thresholds["orphan_resource_container_hours"])

    try:
        resource_containers = client.containers.list(
            all=True, filters={"label": _RESOURCE_LABEL}
        )
    except Exception as exc:
        resource_containers = []
        report["errors"].append(
            {"category": CAT_ORPHAN_RESOURCE_CONTAINERS, "error": str(exc)}
        )
    for container in resource_containers:
        _maybe_remove_container(cat, container, now, cutoff, dry_run)

    # Orphan user containers: labelled ``thoughtmachine.workspace_id=<id>``
    # whose workspace is no longer registered. Resource containers also carry
    # the workspace_id label but are handled by the sweep above.
    if registered_ids is None:
        cat["errors"].append(
            {"id": None, "error": "registry unavailable; user-container sweep skipped"}
        )
        return
    try:
        user_containers = client.containers.list(
            all=True, filters={"label": [_WORKSPACE_LABEL]}
        )
    except Exception as exc:
        user_containers = []
        report["errors"].append(
            {"category": CAT_ORPHAN_RESOURCE_CONTAINERS, "error": str(exc)}
        )
    for container in user_containers:
        labels = getattr(container, "labels", None) or {}
        if labels.get(_RESOURCE_LABEL):
            continue  # resource container — handled above
        ws_id = labels.get(_WORKSPACE_LABEL)
        if not ws_id:
            continue
        if ws_id in registered_ids:
            cid = (
                getattr(container, "id", "")
                or getattr(container, "name", "")
                or str(container)
            )
            cat["skipped"].append(
                {"id": cid, "reason": "belongs to a registered workspace"}
            )
            continue
        _maybe_remove_container(cat, container, now, cutoff, dry_run)


def _gc_orphan_volumes(report, client, registered_ids, now, thresholds, dry_run) -> None:
    """GC ``tm-packages-*`` volumes not owned by a registered workspace."""
    cat = report["categories"][CAT_ORPHAN_VOLUMES]
    if client is None:
        report["errors"].append(
            {"category": CAT_ORPHAN_VOLUMES, "error": "docker unavailable"}
        )
        return
    if registered_ids is None:
        cat["errors"].append({"id": None, "error": "registry unavailable; skipped"})
        return
    cutoff = timedelta(days=thresholds["orphan_volume_days"])
    try:
        volumes = client.volumes.list()
    except Exception as exc:
        report["errors"].append(
            {"category": CAT_ORPHAN_VOLUMES, "error": str(exc)}
        )
        return

    for volume in volumes:
        name = getattr(volume, "name", "") or ""
        if not name.startswith(_PACKAGE_VOLUME_PREFIX):
            continue
        ws_id = name[len(_PACKAGE_VOLUME_PREFIX):]
        if ws_id in registered_ids:
            cat["skipped"].append(
                {"id": name, "reason": "belongs to a registered workspace"}
            )
            continue
        attrs = getattr(volume, "attrs", None) or {}
        created = _parse_timestamp(
            attrs.get("CreatedAt") or getattr(volume, "CreatedAt", "")
        )
        if created is None:
            cat["skipped"].append(
                {"id": name, "reason": "no parseable CreatedAt timestamp"}
            )
            continue
        age = now - created
        if age < timedelta(0):
            cat["skipped"].append({"id": name, "reason": "CreatedAt in the future"})
            continue
        if age <= cutoff:
            cat["skipped"].append({"id": name, "reason": "not old enough"})
            continue

        if dry_run:
            cat["would_remove"].append(name)
            continue
        try:
            volume.remove(force=True)
        except Exception as exc:
            cat["errors"].append({"id": name, "error": str(exc)})
            continue
        cat["removed"].append(name)


# ── Public entry point ─────────────────────────────────────────────────────

def run_gc(
    *,
    dry_run: bool = False,
    now=None,
    registry=None,
    docker_client=None,
) -> dict:
    """Run the age-based vault GC; return a full report. Never raises.

    Args:
        dry_run: If True, perform no mutation; every eligible item is listed
            under ``would_remove`` instead of being removed.
        now: "Current" time as an aware datetime (or ISO string); defaults to
            ``datetime.now(timezone.utc)``. Tests inject a fixed time.
        registry: Registry instance with ``list_workspaces()``. Defaults to a
            fresh ``WorkspaceRegistry``. When listing fails the error is
            reported and the registry-dependent categories are skipped.
        docker_client: Docker client (``containers.list``/``volumes.list``)
            used for the container/volume sweeps. Defaults to
            ``docker.from_env()``; when unavailable the affected categories
            report ``docker unavailable``.

    Returns:
        A report dict — see the module docstring for the shape.
    """
    dry_run = bool(dry_run)
    now = _as_utc(now) if now is not None else _utcnow()
    report = _empty_report(dry_run, now)
    thresholds = _thresholds()

    # ── Registry (shared by stale_workspaces / orphan dirs / orphan volumes)
    if registry is None:
        try:
            registry = WorkspaceRegistry()
        except Exception as exc:
            report["errors"].append({"category": "registry", "error": str(exc)})
            registry = None

    entries = []
    registered_ids = None  # None == "unknown"; categories must then skip
    if registry is not None:
        try:
            entries = registry.list_workspaces()
            registered_ids = {
                getattr(e, "id", "") for e in entries if getattr(e, "id", "")
            }
        except Exception as exc:
            report["errors"].append({"category": "registry", "error": str(exc)})
            entries = []
            registered_ids = None

    # ── Docker client (shared by the stale-workspaces container guard and
    #    the container/volume sweeps)
    if docker_client is None:
        docker_client = _docker_client()

    # ── Category sweeps (each failure is contained; never abort the run)
    try:
        _gc_stale_workspaces(
            report, entries, now, thresholds, dry_run, docker_client
        )
    except Exception as exc:  # pragma: no cover - defensive
        report["errors"].append(
            {"category": CAT_STALE_WORKSPACES, "error": str(exc)}
        )
    try:
        _gc_orphan_workspace_dirs(report, registered_ids, now, thresholds, dry_run)
    except Exception as exc:  # pragma: no cover - defensive
        report["errors"].append(
            {"category": CAT_ORPHAN_WORKSPACE_DIRS, "error": str(exc)}
        )
    try:
        _gc_orphan_sessions(report, now, thresholds, dry_run)
    except Exception as exc:  # pragma: no cover - defensive
        report["errors"].append(
            {"category": CAT_ORPHAN_SESSIONS, "error": str(exc)}
        )

    try:
        _gc_orphan_resource_containers(
            report, docker_client, registered_ids, now, thresholds, dry_run
        )
    except Exception as exc:  # pragma: no cover - defensive
        report["errors"].append(
            {"category": CAT_ORPHAN_RESOURCE_CONTAINERS, "error": str(exc)}
        )
    try:
        _gc_orphan_volumes(report, docker_client, registered_ids, now, thresholds, dry_run)
    except Exception as exc:  # pragma: no cover - defensive
        report["errors"].append(
            {"category": CAT_ORPHAN_VOLUMES, "error": str(exc)}
        )

    _fill_counts(report)
    return report
