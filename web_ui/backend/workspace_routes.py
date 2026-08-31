"""
workspace_routes.py — REST API for per-workspace file read/write.

Provides GET/PUT endpoints for workspace files (Dockerfile, domain_allowlist,
workers, mcp_servers) as well as GET effective_permissions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from thoughtmachine.workspace_registry import WorkspaceRegistry

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    _workspace_dir,
    ensure_workspace_dirs,
    load_workspace_capabilities,
)

from tools.workspace.worker_registry import WorkerRegistry as _WorkerRegistry
_worker_registry = _WorkerRegistry.get_instance()._worker_registry
_registry_lock = _WorkerRegistry.get_instance()._registry_lock

from agent.models.worker_definition import WorkerDefinition

# Module-level reference for monkeypatchability in tests; the import is
# guarded so a failing worker module can never break router import.
try:
    from tools.workspace.worker_manager import get_manager as _get_worker_manager
except Exception:  # pragma: no cover - defensive
    _get_worker_manager = None

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RESOURCE_CATALOG_PATH = _PROJECT_ROOT / "agent" / "config" / "resource_catalog.json"

# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/workspace")

# ── POST /api/workspace/resolve ─────────────────────────────────────────────────


class ResolvePathBody(BaseModel):
    path: str


# ── Path confinement helpers (mirror of web_ui/backend/server.py) ───────────────────────────────────────────────────────────────────
# Duplicated here rather than imported from server.py to avoid a circular
# import (server.py imports this router at module level).  Keep both copies
# in sync — or extract into a shared helper module.

def _vault_root_path() -> Path:
    """Return the vault root — the trust anchor for this server."""
    return Path.home() / ".thoughtmachine"


def _path_is_within(path: str, prefix: str) -> bool:
    """Return True if *path* equals *prefix* or is nested under it."""
    norm = os.path.normpath(path)
    base = os.path.normpath(prefix)
    return norm == base or norm.startswith(base + os.sep)


def _confine_to_home(path: str) -> str:
    """Resolve *path* and confine it to $HOME minus the vault root.

    TRUST ANCHOR: ``~/.thoughtmachine`` is the trust anchor for this server —
    it holds the workspace registry, credentials, configuration and session
    state.  User-reachable endpoints (browse, create, workspace registration)
    must never resolve into it: a workspace rooted there would hand the agent
    the trust anchor and everything inside it.  Raises ``ValueError`` when the
    resolved path is outside $HOME or inside the vault root.
    """
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    home = os.path.realpath(os.path.expanduser("~"))
    if not _path_is_within(resolved, home):
        raise ValueError(
            f"Path '{path}' resolves outside the allowed home directory '{home}'"
        )
    vault = os.path.realpath(str(_vault_root_path()))
    if _path_is_within(resolved, vault):
        raise ValueError(
            f"Path '{path}' resolves into the protected vault directory '{vault}'"
        )
    return resolved


@router.post("/resolve")
async def resolve_workspace_path(body: ResolvePathBody) -> Dict[str, Any]:
    """Resolve a filesystem path to a workspace ID.

    If the path is already registered, returns the existing workspace ID.
    Otherwise, registers it as a new workspace and returns the new ID.

    TRUST ANCHOR: ``~/.thoughtmachine`` is the trust anchor for this server.
    New registrations are confined to $HOME minus the vault root: a path that
    resolves outside the home directory or into ``~/.thoughtmachine`` is
    rejected with HTTP 403 Forbidden, and a non-existent path with HTTP 400.
    Already-registered paths are returned as-is — their entries were written
    only by trusted code (bootstrap, server startup, or this same confined
    endpoint), so they are trusted.
    """
    try:
        if not body.path or not body.path.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="path is required",
            )

        registry = WorkspaceRegistry.get_default()

        # Already registered → return the existing (trusted) entry untouched.
        existing = registry.resolve_by_root(body.path)
        if existing is not None:
            return {"workspace_id": existing.id, "root": existing.root_path}

        # New registration: confine to $HOME minus the vault root.
        try:
            confined = _confine_to_home(body.path)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            )
        if not os.path.isdir(confined):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path '{confined}' is not an existing directory",
            )

        entry = registry.register_by_root(confined)
        if entry:
            # Best-effort provisioning of the hidden git resource container.
            # NEVER fatal: a missing docker daemon or a failed provision must
            # not break the resolve endpoint.
            try:
                from infra.resource_container_manager import (
                    provision_workspace_resource,
                )

                provision_workspace_resource(entry.id, entry.root_path)
            except Exception as exc:
                logger.warning(
                    "resolve_workspace_path: resource provisioning failed for "
                    "workspace %s: %s",
                    entry.id,
                    exc,
                )
            return {"workspace_id": entry.id, "root": entry.root_path}
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register workspace path",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resolve workspace path: {exc}",
        )


# ── GET /api/workspace/list ─────────────────────────────────────────────────────

@router.get("/list")
async def list_workspaces() -> List[Dict[str, Any]]:
    """List all registered workspaces."""
    try:
        registry = WorkspaceRegistry.get_default()
        entries = registry.list_workspaces()
        return [
            {
                "id": entry.id,
                "root": entry.root_path,
                "label": entry.label or entry.id,
            }
            for entry in entries
        ]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list workspaces: {exc}",
        )


# ── GET /api/workspace/templates ────────────────────────────────────────────────

@router.get("/templates")
async def get_worker_templates() -> List[Dict[str, Any]]:
    """Return validated worker templates as a JSON array.

    Reads from ``~/.thoughtmachine/worker_templates/`` first (the user's
    deployed templates, set up by ``bootstrap.py`` on first run).  Falls
    back to ``resources/worker_templates/`` in the repo if the user directory
    doesn't exist or contains no ``.json`` files — this ensures the endpoint
    works on a fresh install before the first bootstrap completes.

    Invalid or unparseable ``.json`` files are skipped with a warning log
    message; remaining valid templates are still returned.
    """
    # Resolve template directories
    user_dir = Path.home() / ".thoughtmachine" / "worker_templates"
    repo_dir = (
        Path(__file__).resolve().parents[2]
        / "resources"
        / "worker_templates"
    )

    # Prefer deployed user templates; fall back to repo source templates
    if user_dir.is_dir() and list(user_dir.glob("*.json")):
        template_dir = user_dir
    else:
        template_dir = repo_dir

    results: List[Dict[str, Any]] = []

    if template_dir.is_dir():
        for json_file in sorted(template_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                wd = WorkerDefinition.model_validate(data)
                results.append(wd.model_dump())
            except Exception as exc:
                logger.warning(
                    "Skipping invalid template %s: %s",
                    json_file.name,
                    exc,
                )

    return results

# ── Pydantic models ──────────────────────────────────────────────────────────

class DomainAllowlistBody(BaseModel):
    domains: List[str]

# ── Helpers ──────────────────────────────────────────────────────────────────

def _atomic_write_json(data: Any, file_path: Path, retries: int = 3) -> None:
    """Atomically write *data* as JSON to *file_path*, with Windows-safe retries.

    Writes to a temporary file in the same directory, then replaces the
    destination via ``os.replace`` (falling back to ``shutil.move``).
    """
    for attempt in range(1, retries + 2):
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=str(file_path.parent),
            suffix=".tmp",
            prefix="workspace_",
            encoding="utf-8",
        )
        try:
            json.dump(data, tmp, indent=2, default=str)
            tmp.flush()
            tmp_path = tmp.name
        finally:
            tmp.close()

        try:
            os.replace(tmp_path, str(file_path))
            return  # success
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            if attempt > retries:
                # Final fallback: try shutil.move (more resilient on Windows)
                try:
                    shutil.move(tmp_path, str(file_path))
                    return
                except OSError as exc:
                    raise exc

            time.sleep(0.2 * attempt)

def _atomic_write_text(data: str, file_path: Path, retries: int = 3) -> None:
    """Atomically write a plain-text string to *file_path*, with Windows-safe retries.

    Same pattern as ``_atomic_write_json`` but writes raw text instead of JSON.
    """
    for attempt in range(1, retries + 2):
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=str(file_path.parent),
            suffix=".tmp",
            prefix="workspace_",
            encoding="utf-8",
        )
        try:
            tmp.write(data)
            tmp.flush()
            tmp_path = tmp.name
        finally:
            tmp.close()

        try:
            os.replace(tmp_path, str(file_path))
            return  # success
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            if attempt > retries:
                try:
                    shutil.move(tmp_path, str(file_path))
                    return
                except OSError as exc:
                    raise exc

            time.sleep(0.2 * attempt)

def _load_session_permissions(session_id: str) -> Optional[Dict[str, Any]]:
    """Load session permissions from a saved session's metadata.

    Returns ``None`` if the session cannot be found or has no permissions
    embedded.  The frontend is expected to pass ``session_id`` from the
    currently active WebSocket connection.
    """
    try:
        from session.store import FileSystemSessionStore

        store = FileSystemSessionStore()
        session = store.load_session(session_id)
        if session is None:
            return None

        # Check workspace_id against this session's workspace
        agent_config_data = session.metadata.get("agent_config", {})
        if isinstance(agent_config_data, dict):
            return agent_config_data.get("session_permissions")
        return None
    except Exception:
        return None

# ── GET /api/workspace/{ws_id}/dockerfile ────────────────────────────────────

@router.get("/{ws_id}/dockerfile", response_class=PlainTextResponse)
async def get_dockerfile(ws_id: str):
    """Return the workspace's Dockerfile as plain text."""
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "Dockerfile"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dockerfile not found for workspace '{ws_id}'",
        )
    return PlainTextResponse(path.read_text(encoding="utf-8"))

# ── GET /api/workspace/{ws_id}/domain_allowlist ──────────────────────────────

@router.get("/{ws_id}/domain_allowlist")
async def get_domain_allowlist(ws_id: str):
    """Return the workspace's domain allowlist as a JSON array."""
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "domain_allowlist.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return []

# ── PUT /api/workspace/{ws_id}/domain_allowlist ──────────────────────────────

@router.put("/{ws_id}/domain_allowlist")
async def put_domain_allowlist(ws_id: str, body: DomainAllowlistBody):
    """Atomically replace the workspace's domain allowlist."""
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "domain_allowlist.json"
    _atomic_write_json(body.domains, path)
    return {"domains": body.domains}

# ── GET /api/workspace/{ws_id}/workers ───────────────────────────────────────


def _worker_instance_key(label: str):
    """Split a worker directory label into (base_name, instance_id).

    Instance dirs are named with ``instance_label``: the bare worker name for
    instance 1 and ``<name>#<N>`` for N>1.  A label whose final ``#`` suffix is
    an integer belongs to that instance; anything else is treated as its own
    base name at instance 1.
    """
    if "#" in label:
        base, _, suffix = label.rpartition("#")
        if suffix.isdigit():
            return base, int(suffix)
    return label, 1


def _query_default(value):
    """Normalize a FastAPI ``Query`` sentinel to its declared default.

    FastAPI substitutes ``Query(...)`` defaults for their value only while
    binding HTTP requests; direct calls (e.g. tests invoking the endpoint
    function) receive the ``Query`` object itself.  Treating that sentinel as
    ``None`` keeps both paths identical for these optional params.
    """
    from fastapi.params import Query as QueryParam

    return None if isinstance(value, QueryParam) else value


@router.get("/{ws_id}/workers")
async def get_workers(
    ws_id: str,
    name: Optional[str] = Query(None, description="Filter to a single worker by name"),
):
    """Return the worker config templates defined for this workspace.

    This endpoint is intentionally configuration-only: each entry is a raw
    template from ``workers.json`` (name, system_prompt, tool_classes, ...).
    Live runtime state is exposed separately via
    ``GET /api/workspace/{ws_id}/workers/active``, so the frontend can render
    template CRUD and runtime monitoring independently.
    """
    name = _query_default(name)
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)

    # Load worker configurations from workers.json
    config_path = ws_dir / "workers.json"
    if config_path.exists():
        try:
            configs = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            configs = []
    else:
        configs = []

    result: List[Dict[str, Any]] = []
    for cfg in configs:
        if isinstance(cfg, str):
            # Legacy shorthand: a bare name with no config fields.
            result.append({"name": cfg})
        elif isinstance(cfg, dict):
            result.append(cfg)

    if name is not None:
        match = [cfg for cfg in result if cfg.get("name") == name]
        if not match:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker '{name}' not found",
            )
        return match[0]

    return result


# ── GET /api/workspace/{ws_id}/workers/active ─────────────────────────────────


def _resolve_worker_manager():
    """Return the live WorkerManager singleton, or None if unavailable."""
    if _get_worker_manager is None:
        return None
    try:
        return _get_worker_manager()
    except Exception:
        return None


def _worker_elapsed_seconds(started_at: Optional[str]) -> Optional[float]:
    """Seconds since *started_at* (ISO-8601), rounded to 1 decimal place.

    Returns ``None`` when the timestamp is missing or unparseable, and never
    returns a negative value (a clock skew between the worker thread and this
    process is clamped to zero).
    """
    if not started_at:
        return None
    try:
        parsed = started_at.replace("Z", "+00:00")
        started = datetime.fromisoformat(parsed)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - started).total_seconds()
        return round(max(0.0, delta), 1)
    except (ValueError, TypeError):
        return None


def _worker_active_entry(thread) -> Dict[str, Any]:
    """Map a live WorkerThread to its compact runtime handle.

    Carries the instance identity (``name``/``worker_name``, ``instance_id``),
    its owning session, runtime status and heartbeat freshness.  ``elapsed``
    is seconds since start (None when the start timestamp is missing);
    ``container_id`` is the thread's provisioned container when attributable
    (None otherwise).  ``worker_name``/``instance_id``/``status``/``elapsed``
    are kept for backward compatibility with existing consumers.
    """
    return {
        "worker_name": getattr(thread, "worker_name", ""),
        "instance_id": getattr(thread, "instance_id", 1),
        "status": getattr(thread, "status", "unknown"),
        "elapsed": _worker_elapsed_seconds(getattr(thread, "started_at", None)),
        "name": getattr(thread, "worker_name", ""),
        "session_id": getattr(thread, "session_id", "") or "",
        "last_heartbeat": getattr(thread, "last_heartbeat", None),
        "container_id": getattr(thread, "container_id", None),
    }


def _collect_active_workers(ws_id: str) -> List[Dict[str, Any]]:
    """Collect live worker threads belonging to *ws_id*.

    Resolution order:
      1. Sessions registered for this workspace (``SessionRegistry``); worker
         threads are looked up per session via the WorkerManager.
      2. Fallback when no open session maps to the workspace: scan the entire
         worker registry and keep threads whose session id is empty or whose
         thread cannot be attributed to another workspace.
    """
    manager = _resolve_worker_manager()
    if manager is None:
        return []

    entries: List[Dict[str, Any]] = []
    seen = set()

    session_ids: List[str] = []
    try:
        from session.session_registry import SessionRegistry

        sessions = SessionRegistry.get_default().get_all()
        if isinstance(sessions, dict):
            for s in sessions.values():
                if not isinstance(s, dict):
                    continue
                if s.get("workspace_id") == ws_id and s.get("is_open"):
                    sid = str(s.get("session_id") or "")
                    if sid:
                        session_ids.append(sid)
    except Exception:
        session_ids = []

    def _append(thread) -> None:
        key = (
            getattr(thread, "session_id", "") or "",
            getattr(thread, "worker_name", "") or "",
            getattr(thread, "instance_id", 1),
        )
        if key in seen:
            return
        seen.add(key)
        entries.append(_worker_active_entry(thread))

    if session_ids:
        for sid in session_ids:
            try:
                threads = manager.list_workers(sid) or []
            except Exception:
                threads = []
            for thread in threads:
                _append(thread)
    else:
        # No open session maps to this workspace: fall back to a full-registry
        # scan and keep threads that are not attributable to another workspace.
        try:
            registry = getattr(manager, "_registry", None)
            all_workers = getattr(registry, "get_all_workers", lambda: {})() or {}
        except Exception:
            all_workers = {}
        for (sid, _wname, _iid), thread in all_workers.items():
            try:
                from session.session_registry import SessionRegistry

                sessions = SessionRegistry.get_default().get_all()
                owner_ws = None
                if isinstance(sessions, dict):
                    s = sessions.get(str(sid))
                    if isinstance(s, dict):
                        owner_ws = s.get("workspace_id")
                if sid and owner_ws != ws_id:
                    continue
            except Exception:
                pass
            _append(thread)

    entries.sort(key=lambda e: (e["worker_name"], e["instance_id"]))
    return entries


@router.get("/{ws_id}/workers/active")
async def get_active_workers(
    ws_id: str,
    session_id: Optional[str] = Query(None, description="Filter to a single session id"),
) -> List[Dict[str, Any]]:
    """Return live worker threads for the workspace (runtime handles only).

    Each handle carries ``{worker_name, instance_id, status, elapsed, name,
    session_id, last_heartbeat, container_id}`` where ``elapsed`` is the
    seconds since the thread started (None when the start timestamp is
    missing), ``last_heartbeat`` is the thread's last heartbeat ISO timestamp
    (None when the thread has not heartbeated yet) and ``container_id`` is the
    thread's provisioned container (None when not attributable).  The
    optional ``?session_id=`` query param narrows the result to the threads of
    a single session.  Template definitions are served by
    ``GET /api/workspace/{ws_id}/workers`` instead.
    """
    session_id = _query_default(session_id)
    entries = _collect_active_workers(ws_id)
    if session_id is not None:
        entries = [e for e in entries if e.get("session_id") == session_id]
    return entries


# ── POST /api/workspace/{ws_id}/workers ───────────────────────────────────────

@router.post("/{ws_id}/workers", status_code=status.HTTP_201_CREATED)
async def create_worker(ws_id: str, request: Request):
    """Create a new worker definition in the workspace.

    Validates the request body against ``WorkerDefinition``, checks for
    duplicate names, and appends to ``workers.json`` atomically.
    """
    # 1. Parse and validate request body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}")
    # 2. Validate with Pydantic
    try:
        worker = WorkerDefinition.model_validate(body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    # 2. Load existing workers.json
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "workers.json"
    existing = []
    if path.exists():
        existing = json.loads(path.read_text())
    # 3. Duplicate check
    if any(w["name"] == worker.name for w in existing):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Worker '{worker.name}' already exists",
        )
    # 4. Append & write
    existing.append(worker.model_dump())
    _atomic_write_json(existing, path)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=worker.model_dump())

# ── PUT /api/workspace/{ws_id}/workers/{name} ──────────────────────────────

@router.put("/{ws_id}/workers/{name}")
async def update_worker(ws_id: str, name: str, request: Request):
    """Update an existing worker definition by name.

    Validates the request body, finds the matching worker, replaces it
    in ``workers.json`` atomically, and returns the updated definition.
    """
    # 1. Parse and validate request body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}")
    # 2. Validate
    try:
        updated_worker = WorkerDefinition.model_validate(body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    # 2. Load existing
    ws_dir = _workspace_dir(ws_id)
    path = ws_dir / "workers.json"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workers file not found",
        )
    existing = json.loads(path.read_text())
    # 3. Find index
    index = next((i for i, w in enumerate(existing) if w["name"] == name), None)
    if index is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{name}' not found",
        )
    # 4. Replace & write
    existing[index] = updated_worker.model_dump()
    _atomic_write_json(existing, path)
    return updated_worker.model_dump()

# ── DELETE /api/workspace/{ws_id}/workers/{name} ───────────────────────────

@router.delete("/{ws_id}/workers/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_worker(ws_id: str, name: str):
    """Delete a worker definition by name from ``workers.json``.

    Removes the matching worker and writes the remaining list atomically.
    Returns 204 with no content on success.
    """
    ws_dir = _workspace_dir(ws_id)
    path = ws_dir / "workers.json"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workers file not found",
        )
    existing = json.loads(path.read_text())
    filtered = [w for w in existing if w["name"] != name]
    if len(filtered) == len(existing):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{name}' not found",
        )
    _atomic_write_json(filtered, path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

# ── PUT /api/workspace/{ws_id}/dockerfile ──────────────────────────────────

@router.put("/{ws_id}/dockerfile")
async def put_dockerfile(ws_id: str, request: Request):
    """Atomically replace the workspace's ``Dockerfile`` with raw text.

    Reads the request body as UTF-8 text and writes it to the workspace's
    ``Dockerfile`` using the same atomic-write pattern as other endpoints.
    """
    body = await request.body()
    text = body.decode("utf-8")
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "Dockerfile"
    _atomic_write_text(text, path)
    return {"status": "ok", "workspace_id": ws_id}

# ── GET /api/workspace/{ws_id}/mcp_servers ───────────────────────────────────

@router.get("/{ws_id}/mcp_servers")
async def get_mcp_servers(ws_id: str):
    """Return the workspace's MCP server configurations as a JSON array."""
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "mcp_servers.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return []

# ── GET /api/workspace/{ws_id}/effective_permissions ─────────────────────────

@router.get("/{ws_id}/effective_permissions")
async def get_effective_permissions(
    ws_id: str,
    session_id: Optional[str] = Query(None, description="Active session ID for permission context"),
):
    """Return the effective (merged) permissions for this workspace.

    Merges the session-level permissions with workspace-level capabilities.
    If *session_id* is provided, the session is loaded and its embedded
    ``session_permissions`` are used; otherwise a read-only, no-network
    default is assumed.
    """
    ensure_workspace_dirs(ws_id)

    # ── Load workspace capabilities ──────────────────────────────────────
    caps = load_workspace_capabilities(ws_id)
    if caps is None:
        caps = WorkspaceCapabilities.default()

    # ── Build SessionPermissions ─────────────────────────────────────────
    from thoughtmachine.security import SessionPermissions

    session_perms = None
    if session_id:
        raw_perms = _load_session_permissions(session_id)
        if raw_perms is not None and isinstance(raw_perms, dict):
            try:
                session_perms = SessionPermissions(**raw_perms)
            except Exception:
                session_perms = None

    if session_perms is None:
        # Default safety: read-only filesystem, no network, no container
        session_perms = SessionPermissions(
            container=False,
            network="banned",
            filesystem="read",
            system="read",
            git="read",
            execution="banned",
        )

    # ── Merge via security gate (lazy import, with fallback) ──────────────
    try:
        from security.security_gate import get_effective_permissions as _gate_effective

        effective = _gate_effective(session_perms, caps)
    except ImportError:
        # Fallback when security gate dependencies are not available
        # (e.g., in minimal test environments)
        effective = {
            "filesystem": session_perms.filesystem,
            "network": session_perms.network,
            "container": session_perms.container,
            "git": session_perms.git,
            "system": session_perms.system,
            "execution": session_perms.execution,
            "workspace_restrictions": {
                "allow_docker": caps.allow_docker,
                "allow_network": caps.allow_network,
                "filesystem_write": caps.filesystem_write,
            },
        }
    return {"workspace_id": ws_id, "effective_permissions": effective}

# ── GET /api/workspace/{ws_id}/workers/{name}/events ─────────────────────

# ── POST /api/workspace/{ws_id}/workers/stop_all ───────────────────────────


class StopAllWorkersBody(BaseModel):
    """Optional request body for ``POST .../workers/stop_all``.

    ``session_id`` restricts the stop to the workers of one session
    (``workers/<session_id>/...``).  An absent body (or ``session_id: null``)
    stops every worker instance in the workspace.
    """

    session_id: Optional[str] = None


def _stop_worker_instance(
    ws_dir: Path,
    name: str,
    instance_id: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Cooperatively stop a single worker instance.

    Shared by the per-worker ``stop`` route and the ``stop_all`` route so
    both use the exact same semantics:

    * resolve the worker directory from the same on-disk layout
      (``workers/<session_id>/<name>[/<name>#<N>]`` session-scoped or legacy
      ``workers/<name>``/``workers/<name>#<N>``),
    * write ``command.json`` with ``{"action": "stop"}`` so the polling
      worker thread picks the signal up (within its poll interval),
    * optimistically write ``status.json`` with
      ``runtime_status: "completed"`` so the UI sees a terminal state
      immediately (instead of "jumping back" to "busy" before the worker
      processes the stop),
    * fast-path the in-memory registry thread, optionally narrowed by
      ``session_id`` and ``instance_id`` (no ``Thread.kill`` — the worker
      stops cooperatively).

    Returns the per-worker response dict used by the stop route
    (``{"status": "ok", "name": name}``, plus ``instance_id`` /
    ``instance_label`` when ``instance_id`` is not None).
    """
    workers_dir = ws_dir / "workers"

    # Optional instance targeting: with instance_id, resolve the instance
    # directory "<name>" (instance 1) or "<name>#<N>" (N>1).  Without it,
    # keep the legacy by-name lookup (first matching directory).
    target_label = (
        _WorkerRegistry.instance_label(name, instance_id)
        if instance_id is not None else None
    )

    # ── Find the worker directory (session-scoped first, then legacy) ──
    worker_dir: Optional[Path] = None

    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            # Check if this is a session directory (contains sub-worker dirs)
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/ or <name#N>/
                candidate = subdir / (target_label if target_label is not None else name)
                if candidate.is_dir():
                    worker_dir = candidate
                    break
            else:
                # Legacy: workers/<name>/ or workers/<name#N>/
                if subdir.name == (target_label if target_label is not None else name):
                    worker_dir = subdir
                    break

    if worker_dir is None or not worker_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"status": "not_found", "name": name},
        )

    # Write the stop command file — the worker thread polls for this
    _atomic_write_json({"action": "stop"}, worker_dir / "command.json")

    # Immediately write status.json as "completed" so the UI doesn't
    # "jump back" to "busy" on the next poll before the worker
    # thread has a chance to process the stop signal.
    _atomic_write_json({
        "runtime_status": "completed",
        "current_task": None,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }, worker_dir / "status.json")

    # Fast-path: if the thread is in-memory (same process), signal directly.
    # Registry keys are tuples (session_id, worker_name[, instance_id]), so
    # iterate all entries matching the worker name — optionally narrowed to
    # the requested session and/or instance.
    with _registry_lock:
        for key, thread in list(_worker_registry.items()):
            wname = key[1]
            if wname != name:
                continue
            if session_id is not None and (key[0] if len(key) > 0 else None) != session_id:
                continue
            if instance_id is not None and (key[2] if len(key) > 2 else 1) != instance_id:
                continue
            try:
                thread.stop()
            except Exception:
                pass  # File-based stop will still work

    resp = {"status": "ok", "name": name}
    if target_label is not None:
        resp["instance_id"] = instance_id
        resp["instance_label"] = target_label
    return resp


def _enumerate_worker_instances(
    ws_dir: Path,
    session_id: Optional[str] = None,
) -> List[tuple]:
    """Enumerate ``(worker_name, instance_id)`` pairs from the same on-disk
    layout the per-worker routes use.

    Session-scoped directories (``workers/<session_id>/<name>/`` or
    ``<name>#<N>/``) yield one entry per instance; when ``session_id`` is
    given only that session's directory is considered.  Legacy directories
    (``workers/<name>/``, ``workers/<name>#<N>/``) are included only when no
    ``session_id`` filter is active.  Non-directory entries are skipped.
    """
    workers_dir = ws_dir / "workers"
    if not workers_dir.is_dir():
        return []
    targets: List[tuple] = []
    for subdir in workers_dir.iterdir():
        if not subdir.is_dir():
            continue
        first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
        if first_child is not None and first_child.is_dir():
            # Session-scoped: workers/<session_id>/<name>[/<name>#<N>]/
            if session_id is not None and subdir.name != session_id:
                continue
            for worker_subdir in subdir.iterdir():
                if not worker_subdir.is_dir():
                    continue
                targets.append(_worker_instance_key(worker_subdir.name))
        else:
            # Legacy: workers/<name>/ or workers/<name#N>/
            if session_id is not None:
                continue
            targets.append(_worker_instance_key(subdir.name))
    return targets


@router.post("/{ws_id}/workers/stop_all")
async def stop_all_workers(ws_id: str, body: Optional[StopAllWorkersBody] = None):
    """Cooperatively stop every worker instance in the workspace.

    Enumerates instances from the same on-disk layout the per-worker ``stop``
    route uses (``workers/<session_id>/<name>[/<name>#<N>]`` and legacy
    ``workers/<name>``/``workers/<name>#<N>``) and stops each one through the
    shared per-worker helper: a file-based ``command.json`` signal plus an
    optimistic ``status.json`` (``runtime_status: "completed"``) plus the
    in-memory registry fast-path.  No ``Thread.kill`` — workers stop
    cooperatively on their next poll.  No job_registry or container changes.

    An optional JSON body ``{"session_id": "<id>"}`` restricts the stop to
    that session's workers; an absent or empty body stops all instances.
    If one worker fails, the remaining workers are still processed.

    Returns:
        200 with a JSON list of per-worker results::

            [{"worker_name": ..., "instance_id": ...,
              "status": "ok" | "not_found" | "error", "error": ...|None}, ...]
    """
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)
    session_filter = body.session_id if body is not None else None
    targets = _enumerate_worker_instances(ws_dir, session_id=session_filter)
    results = []
    for name, instance_id in targets:
        try:
            resp = _stop_worker_instance(
                ws_dir, name, instance_id, session_id=session_filter
            )
            results.append({
                "worker_name": name,
                "instance_id": resp.get("instance_id", instance_id),
                "status": resp.get("status", "ok"),
                "error": None,
            })
        except HTTPException as exc:
            results.append({
                "worker_name": name,
                "instance_id": instance_id,
                "status": "not_found",
                "error": exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str),
            })
        except Exception as exc:  # pragma: no cover - defensive
            results.append({
                "worker_name": name,
                "instance_id": instance_id,
                "status": "error",
                "error": str(exc),
            })
    return results


# ── POST /api/workspace/{ws_id}/workers/{name}/stop ────────────────────────

@router.post("/{ws_id}/workers/{name}/stop")
async def stop_worker(
    ws_id: str,
    name: str,
    instance_id: Optional[int] = Query(
        None,
        description="Target a specific worker instance (default: legacy by-name behavior)",
    ),
):
    """Stop a running worker via a file-based command signal.

    Supports both directory layouts:
      - ``workers/<session_id>/<name>/`` (session-scoped)
      - ``workers/<name>/`` (legacy, no session)

    When ``instance_id`` is provided the command targets that specific
    instance: the directory is resolved as ``<name>`` (instance 1) or
    ``<name>#<N>`` (N>1) and only the matching in-memory thread is signalled.
    Without it, the legacy by-name behavior is kept (first matching directory,
    all matching threads).

    Writes ``{"action": "stop"}`` to the worker's ``command.json`` so that
    the worker thread (which polls for this file) picks it up within 2 seconds.
    Also attempts an in-memory stop as a fast path if the thread is in the
    registry.  Returns immediately — the worker will transition to ``stopped``
    asynchronously.

    Also immediately writes ``status.json`` with ``runtime_status: "completed"``
    so the web UI's next poll sees a terminal state right away (instead of
    "jumping back" to "busy" when the optimistic update gets overwritten
    before the worker processes the stop).

    Returns:
        200 with ``{"status": "ok", "name": name}`` on success.
        404 if the worker directory does not exist.
    """
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)
    instance_id = _query_default(instance_id)
    return _stop_worker_instance(ws_dir, name, instance_id)

# ── POST /api/workspace/{ws_id}/workers/{name}/pause ───────────────────────

@router.post("/{ws_id}/workers/{name}/pause")
async def pause_worker(
    ws_id: str,
    name: str,
    instance_id: Optional[int] = Query(
        None,
        description="Target a specific worker instance (default: legacy by-name behavior)",
    ),
):
    """Pause a running worker after it completes its current turn.

    Supports both directory layouts:
      - ``workers/<session_id>/<name>/`` (session-scoped)
      - ``workers/<name>/`` (legacy, no session)

    When ``instance_id`` is provided the command targets that specific
    instance: the directory is resolved as ``<name>`` (instance 1) or
    ``<name>#<N>`` (N>1) and only the matching in-memory thread is signalled.
    Without it, the legacy by-name behavior is kept (first matching directory,
    all matching threads).

    Writes ``{"action": "pause"}`` to the worker's ``command.json`` so that
    the worker thread (which polls for this file) picks it up within 2 seconds.
    Also attempts an in-memory pause as a fast path if the thread is in the
    registry.  Returns immediately — the worker will transition to ``paused``
    asynchronously.

    Also immediately writes ``status.json`` with ``runtime_status: "pausing"``
    so the web UI's next poll shows the pending pause right away.  The worker
    transitions to ``"paused"`` only once its current turn actually completes.

    Returns:
        200 with ``{"status": "pausing", "name": name}`` on success.
        404 if the worker directory does not exist.
    """
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)
    workers_dir = ws_dir / "workers"
    instance_id = _query_default(instance_id)

    # Optional instance targeting: with instance_id, resolve the instance
    # directory "<name>" (instance 1) or "<name>#<N>" (N>1).  Without it,
    # keep the legacy by-name lookup (first matching directory).
    target_label = (
        _WorkerRegistry.instance_label(name, instance_id)
        if instance_id is not None else None
    )

    # ── Find the worker directory (session-scoped first, then legacy) ──
    worker_dir: Optional[Path] = None

    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            # Check if this is a session directory (contains sub-worker dirs)
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/ or <name#N>/
                candidate = subdir / (target_label if target_label is not None else name)
                if candidate.is_dir():
                    worker_dir = candidate
                    break
            else:
                # Legacy: workers/<name>/ or workers/<name#N>/
                if subdir.name == (target_label if target_label is not None else name):
                    worker_dir = subdir
                    break

    if worker_dir is None or not worker_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"status": "not_found", "name": name},
        )

    # Write the pause command file — the worker thread polls for this
    _atomic_write_json({"action": "pause"}, worker_dir / "command.json")

    # Immediately write status.json as "pausing" (the pause is in flight).
    # The worker flips to "paused" only when its current turn completes.
    _atomic_write_json({
        "runtime_status": "pausing",
        "current_task": None,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }, worker_dir / "status.json")

    # Fast-path: if the thread is in-memory (same process), signal directly.
    # Registry keys are tuples (session_id, worker_name[, instance_id]), so
    # iterate all entries matching the worker name — optionally narrowed to
    # the requested instance.
    with _registry_lock:
        for key, thread in list(_worker_registry.items()):
            wname = key[1]
            if wname != name:
                continue
            if instance_id is not None and (key[2] if len(key) > 2 else 1) != instance_id:
                continue
            try:
                thread.pause()
            except Exception:
                pass  # File-based pause will still work

    resp = {"status": "pausing", "name": name}
    if target_label is not None:
        resp["instance_id"] = instance_id
        resp["instance_label"] = target_label
    return resp


# ── POST /api/workspace/{ws_id}/workers/{name}/resume ──────────────────────

@router.post("/{ws_id}/workers/{name}/resume")
async def resume_worker(
    ws_id: str,
    name: str,
    instance_id: Optional[int] = Query(
        None,
        description="Target a specific worker instance (default: legacy by-name behavior)",
    ),
):
    """Resume a paused worker.

    Supports both directory layouts:
      - ``workers/<session_id>/<name>/`` (session-scoped)
      - ``workers/<name>/`` (legacy, no session)

    When ``instance_id`` is provided the command targets that specific
    instance: the directory is resolved as ``<name>`` (instance 1) or
    ``<name>#<N>`` (N>1) and only the matching in-memory thread is signalled.
    Without it, the legacy by-name behavior is kept (first matching directory,
    all matching threads).

    Writes ``{"action": "resume"}`` to the worker's ``command.json`` so that
    the worker thread (which polls for this file) picks it up within 2 seconds.
    Also attempts an in-memory resume as a fast path if the thread is in the
    registry.  Returns immediately — the worker will transition back to
    ``ready``/``busy`` asynchronously.

    Returns:
        200 with ``{"status": "resumed", "name": name}`` on success.
        404 if the worker directory does not exist.
    """
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)
    workers_dir = ws_dir / "workers"
    instance_id = _query_default(instance_id)

    # Optional instance targeting: with instance_id, resolve the instance
    # directory "<name>" (instance 1) or "<name>#<N>" (N>1).  Without it,
    # keep the legacy by-name lookup (first matching directory).
    target_label = (
        _WorkerRegistry.instance_label(name, instance_id)
        if instance_id is not None else None
    )

    # ── Find the worker directory (session-scoped first, then legacy) ──
    worker_dir: Optional[Path] = None

    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            # Check if this is a session directory (contains sub-worker dirs)
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/ or <name#N>/
                candidate = subdir / (target_label if target_label is not None else name)
                if candidate.is_dir():
                    worker_dir = candidate
                    break
            else:
                # Legacy: workers/<name>/ or workers/<name#N>/
                if subdir.name == (target_label if target_label is not None else name):
                    worker_dir = subdir
                    break

    if worker_dir is None or not worker_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"status": "not_found", "name": name},
        )

    # Write the resume command file — the worker thread polls for this
    _atomic_write_json({"action": "resume"}, worker_dir / "command.json")

    # Fast-path: if the thread is in-memory (same process), signal directly.
    # Registry keys are tuples (session_id, worker_name[, instance_id]), so
    # iterate all entries matching the worker name — optionally narrowed to
    # the requested instance.
    with _registry_lock:
        for key, thread in list(_worker_registry.items()):
            wname = key[1]
            if wname != name:
                continue
            if instance_id is not None and (key[2] if len(key) > 2 else 1) != instance_id:
                continue
            try:
                thread.resume()
            except Exception:
                pass  # File-based resume will still work

    resp = {"status": "resumed", "name": name}
    if target_label is not None:
        resp["instance_id"] = instance_id
        resp["instance_label"] = target_label
    return resp






# ══════════════════════════════════════════════════════════════════════════════
#  Workspace config backbone: purpose / permissions / risk (Phase 1)
#  New routes: POST /api/workspace (create with purpose preset),
#  GET/PUT /api/workspace/{ws_id}/permissions, GET /api/workspace/{ws_id}.
#  Persisted in vault workspaces/<id>/config.json alongside the existing
#  capabilities / domain_allowlist keys (see agent/config/schema_manifest.json).
#  Registered last so the more specific /list, /templates, /{ws_id}/... routes
#  keep route-matching precedence.
# ══════════════════════════════════════════════════════════════════════════════


class WorkspacePermissionsBody(BaseModel):
    """Body for PUT /api/workspace/{ws_id}/permissions."""

    permissions: Dict[str, str]
    allow_host_resources: Optional[bool] = None


class WorkspaceCreateBody(BaseModel):
    """Body for POST /api/workspace (create workspace with a purpose preset)."""

    path: str
    purpose: Optional[str] = "general"
    settings: Optional[Dict[str, Any]] = {}


def _load_workspace_config(ws_id: str) -> Dict[str, Any]:
    """Load vault ``workspaces/<id>/config.json`` (``{}`` if missing/unparsable)."""
    cfg_path = _workspace_dir(ws_id) / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_workspace_config(ws_id: str, data: Dict[str, Any]) -> None:
    """Atomically write vault ``workspaces/<id>/config.json``."""
    _atomic_write_json(data, _workspace_dir(ws_id) / "config.json")


def _resolve_workspace_permissions(cfg: Dict[str, Any], purpose: str) -> Dict[str, str]:
    """Return the saved permission map, or the purpose preset as fallback."""
    from agent.config.workspace_purpose import apply_purpose_preset

    saved = cfg.get("permissions")
    if isinstance(saved, dict) and saved:
        return {str(k): str(v) for k, v in saved.items()}
    return apply_purpose_preset(purpose)


# ── GET /api/workspace/{ws_id}/permissions ─────────────────────────────────────


@router.get("/{ws_id}/permissions")
async def get_workspace_permissions(ws_id: str) -> Dict[str, Any]:
    """Return the workspace's resource permission map, purpose and host flag.

    Falls back to the purpose preset (or catalog defaults) when the
    workspace has no saved permission map in ``config.json`` yet.
    """
    ensure_workspace_dirs(ws_id)
    cfg = _load_workspace_config(ws_id)
    purpose = cfg.get("purpose", "general")
    allow_host_resources = bool(cfg.get("allow_host_resources", False))
    permissions = _resolve_workspace_permissions(cfg, purpose)
    return {
        "workspace_id": ws_id,
        "purpose": purpose,
        "permissions": permissions,
        "allow_host_resources": allow_host_resources,
    }


# ── PUT /api/workspace/{ws_id}/permissions ─────────────────────────────────────


@router.put("/{ws_id}/permissions")
async def put_workspace_permissions(
    ws_id: str,
    body: WorkspacePermissionsBody,
) -> Dict[str, Any]:
    """Persist a workspace resource permission map (validated against the catalog).

    422 on unknown resource names or invalid permission levels; otherwise
    merges into ``config.json`` (preserving purpose / capabilities /
    domain_allowlist) and returns the updated map with its runtime risk
    assessment. ``allow_host_resources`` is updated only when explicitly
    provided in the body (None leaves the saved value untouched).
    """
    from agent.config.resource_catalog import validate_workspace_permissions
    from agent.config.risk_model import compute_workspace_risk

    ensure_workspace_dirs(ws_id)
    normalized, errors = validate_workspace_permissions(body.permissions)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"errors": errors},
        )

    cfg = _load_workspace_config(ws_id)
    if body.allow_host_resources is not None:
        cfg["allow_host_resources"] = bool(body.allow_host_resources)
    cfg["permissions"] = normalized
    _save_workspace_config(ws_id, cfg)

    purpose = cfg.get("purpose", "general")
    allow_host_resources = bool(cfg.get("allow_host_resources", False))
    risk = compute_workspace_risk(
        permissions=normalized,
        allow_host_resources=allow_host_resources,
        purpose=purpose,
    )
    return {
        "workspace_id": ws_id,
        "purpose": purpose,
        "permissions": normalized,
        "allow_host_resources": allow_host_resources,
        "risk": risk,
    }


# ── POST /api/workspace (create) ───────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(body: WorkspaceCreateBody) -> Dict[str, Any]:
    """Register a new workspace rooted at *path* with a purpose preset.

    Validates the purpose (422), confines the path to $HOME minus the vault
    root (403), registers the workspace, creates the vault workspace dirs
    and writes ``config.json`` with ``{purpose, permissions,
    allow_host_resources}``.  ``settings.permissions`` (dict of
    resource→level overrides) and ``settings.allow_host_resources`` (bool)
    are layered on top of the purpose preset.
    """
    from agent.config.workspace_purpose import WORKSPACE_PURPOSES, apply_purpose_preset
    from agent.config.risk_model import compute_workspace_risk

    if body.purpose not in WORKSPACE_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "errors": [
                    f"unknown purpose '{body.purpose}' "
                    f"(expected one of {WORKSPACE_PURPOSES})"
                ]
            },
        )

    try:
        confined = _confine_to_home(body.path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    if not os.path.isdir(confined):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path '{body.path}' does not exist or is not a directory",
        )

    registry = WorkspaceRegistry.get_default()
    entry = registry.register_by_root(confined)
    ensure_workspace_dirs(entry.id)

    settings = body.settings or {}
    custom_permissions = settings.get("permissions")
    if not isinstance(custom_permissions, dict):
        custom_permissions = None
    permissions = apply_purpose_preset(body.purpose, custom_permissions)
    allow_host_resources = bool(settings.get("allow_host_resources", False))

    cfg = _load_workspace_config(entry.id)
    cfg["purpose"] = body.purpose
    cfg["permissions"] = permissions
    cfg["allow_host_resources"] = allow_host_resources
    _save_workspace_config(entry.id, cfg)

    risk = compute_workspace_risk(
        permissions=permissions,
        allow_host_resources=allow_host_resources,
        purpose=body.purpose,
    )
    return {
        "workspace_id": entry.id,
        "root": entry.root_path,
        "purpose": body.purpose,
        "permissions": permissions,
        "allow_host_resources": allow_host_resources,
        "risk": risk,
    }


# ── GET /api/workspace/{ws_id}/summary ────────────────────────────────────────


def _workspace_root_mountable(entry) -> bool:
    """True when the workspace root exists and is outside the vault.

    The vault root (~/.thoughtmachine) is the trust anchor and must never be
    mounted into a container; the registry should never contain it, but a
    defensive check keeps this invariant cheap to enforce.
    """
    try:
        root = os.path.abspath(os.path.expanduser(entry.root_path))
        vault = str(_vault_root_path())
        if root == vault or root.startswith(vault + os.sep):
            return False
        return bool(root)
    except Exception:
        return False


def _containers_for_workspace(entry) -> Optional[List[Dict[str, Any]]]:
    """List the docker containers provisioned for this workspace.

    Returns ``None`` when the workspace root is not mountable or the container
    manager cannot be reached (e.g. no docker daemon) - callers treat None as
    "unknown" and fall back to an empty list. Mirrors global_routes.py.
    """
    if not _workspace_root_mountable(entry):
        return None
    try:
        from infra.container_manager import ContainerManager

        manager = ContainerManager(
            workspace_path=entry.root_path,
            workspace_id=entry.id,
            session_id=None,
            session_permissions=None,
        )
        raw = manager.list_containers() or []
        handles = []
        for c in raw:
            name = c.get("name") or ""
            handles.append(
                {
                    "id": c.get("container_id") or "",
                    "name": name,
                    "type": "resource" if name.startswith("tm-res-") else "free_use",
                    "workspace_id": c.get("workspace_id") or entry.id,
                    "status": c.get("status") or "unknown",
                }
            )
        handles.sort(key=lambda h: h["name"])
        return handles
    except Exception:
        return None


@router.get("/{ws_id}/summary")
async def get_workspace_overview(ws_id: str) -> Dict[str, Any]:
    """Return a full read-only overview of a workspace for the UI dashboard.

    Combines configuration (permissions ceiling, capabilities, dockerfile,
    worker templates) with live state (active worker threads, open sessions,
    provisioned containers) and global registries (tool list, resource
    catalog). No secrets are included: file contents are limited to the
    workspace Dockerfile.
    """
    registry = WorkspaceRegistry.get_default()
    entry = registry.get_workspace(ws_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace '{ws_id}' not found",
        )

    # Permissions ceiling (saved config wins, else purpose preset, else {}).
    try:
        from web_ui.backend.config_manager import _load_workspace_permission_ceiling

        permissions = _load_workspace_permission_ceiling(ws_id)
    except Exception:
        permissions = {}
    if not isinstance(permissions, dict):
        permissions = {}

    # Capability flags (default = fully permissive when nothing saved).
    try:
        capabilities = WorkspaceCapabilities.default()
        caps = load_workspace_capabilities(ws_id)
        if caps is not None:
            capabilities = caps
    except Exception:
        capabilities = WorkspaceCapabilities.default()

    # Workspace Dockerfile (content is safe - it is user-authored build config).
    dockerfile_path = _workspace_dir(ws_id) / "Dockerfile"
    try:
        dockerfile = {
            "path": str(dockerfile_path),
            "content": dockerfile_path.read_text(encoding="utf-8"),
        }
    except OSError:
        dockerfile = {"path": str(dockerfile_path), "content": None}

    # Worker templates (config only) + live worker threads.
    try:
        worker_templates = await get_workers(ws_id)
    except Exception:
        worker_templates = []
    try:
        active_workers = _collect_active_workers(ws_id)
    except Exception:
        active_workers = []

    # Open sessions for this workspace.
    active_sessions: List[Dict[str, Any]] = []
    try:
        from session.session_registry import SessionRegistry

        sessions = SessionRegistry.get_default().get_all()
        if isinstance(sessions, dict):
            for s in sessions.values():
                if not isinstance(s, dict):
                    continue
                if s.get("workspace_id") != ws_id or not s.get("is_open"):
                    continue
                active_sessions.append(
                    {
                        "session_id": str(s.get("session_id") or ""),
                        "workspace_id": ws_id,
                        "name": s.get("name") or "",
                        "mode": s.get("mode") or "",
                        "started_at": s.get("created_at") or "",
                    }
                )
            active_sessions.sort(key=lambda s: s["session_id"])
    except Exception:
        active_sessions = []

    # Provisioned containers for this workspace.
    try:
        active_containers = _containers_for_workspace(entry) or []
    except Exception:
        active_containers = []

    # Tool registry + resource catalog.
    try:
        from session.tool_presets import _ALL_TOOLS
    except ImportError:
        try:
            from agent.config.presets import _ALL_TOOLS
        except ImportError:
            _ALL_TOOLS = []
    tools = sorted(_ALL_TOOLS) if isinstance(_ALL_TOOLS, (list, tuple)) else []

    resource_catalog: List[Any] = []
    try:
        if _RESOURCE_CATALOG_PATH.is_file():
            data = json.loads(_RESOURCE_CATALOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                resource_catalog = data
    except (OSError, ValueError):
        resource_catalog = []

    return {
        "workspace_id": ws_id,
        "label": entry.label or entry.id,
        "root_path": entry.root_path,
        "allow_host_resources": bool(
            _load_workspace_config(ws_id).get("allow_host_resources", False)
        ),
        "permissions": permissions,
        "capabilities": capabilities.to_dict(),
        "dockerfile": dockerfile,
        "worker_templates": worker_templates,
        "active_workers": active_workers,
        "active_sessions": active_sessions,
        "active_containers": active_containers,
        "tools": tools,
        "resource_catalog": resource_catalog,
    }


# ── GET /api/workspace/{ws_id} (summary) ───────────────────────────────────────


@router.get("/{ws_id}")
async def get_workspace_summary(ws_id: str) -> Dict[str, Any]:
    """Return the workspace summary: root, purpose, permissions, risk.

    Registered after the more specific ``/{ws_id}/...`` routes so they keep
    precedence.  404 when the workspace ID is not in the registry.
    """
    from agent.config.risk_model import compute_workspace_risk

    registry = WorkspaceRegistry.get_default()
    entry = registry.get_workspace(ws_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace '{ws_id}' not found",
        )

    cfg = _load_workspace_config(ws_id)
    purpose = cfg.get("purpose", "general")
    allow_host_resources = bool(cfg.get("allow_host_resources", False))
    permissions = _resolve_workspace_permissions(cfg, purpose)

    risk = compute_workspace_risk(
        permissions=permissions,
        allow_host_resources=allow_host_resources,
        purpose=purpose,
    )
    return {
        "workspace_id": ws_id,
        "root": entry.root_path,
        "purpose": purpose,
        "permissions": permissions,
        "allow_host_resources": allow_host_resources,
        "risk": risk,
    }

