"""
workspace_routes.py — REST API for per-workspace file read/write.

Provides GET/PUT endpoints for workspace files (Dockerfile, domain_allowlist,
workers, mcp_servers) as well as GET effective_permissions.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    _workspace_dir,
    ensure_workspace_dirs,
    load_workspace_capabilities,
)

# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/workspace")


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


@router.get("/{ws_id}/workers")
async def get_workers(ws_id: str):
    """Return worker configs merged with runtime status and persisted context.

    Each worker entry includes:
      - Config fields from workers.json (name, system_prompt, tool_classes, etc.)
      - runtime_status:  "running" | "completed" | "failed" | "idle" | None
      - current_task:    current activity description (if running)
      - last_heartbeat:  ISO-8601 timestamp of last activity
      - error:           error message (if failed)
      - has_persisted_context: whether the worker has saved conversation on disk
    """
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)

    # 1. Load worker configurations from workers.json
    config_path = ws_dir / "workers.json"
    if config_path.exists():
        try:
            configs = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            configs = []
    else:
        configs = []

    # 2. Get runtime status from the module-level worker registry
    runtime_statuses = {}
    try:
        from tools.workspace.worker import _worker_registry, _registry_lock
        with _registry_lock:
            for wname, thread in list(_worker_registry.items()):
                runtime_statuses[wname] = {
                    "runtime_status": thread.status,
                    "current_task": thread.current_task,
                    "last_heartbeat": thread.last_heartbeat,
                    "error": thread.error,
                }
    except ImportError:
        pass

    # 3. Check for persisted contexts on disk
    persisted_names = set()
    workers_dir = ws_dir / "workers"
    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if subdir.is_dir() and (subdir / "context.json").exists():
                persisted_names.add(subdir.name)

    # 4. Merge everything
    result = []
    for cfg in configs:
        name = cfg.get("name", "")
        entry = dict(cfg)
        if name in runtime_statuses:
            entry["runtime_status"] = runtime_statuses[name]["runtime_status"]
            entry["current_task"] = runtime_statuses[name]["current_task"]
            entry["last_heartbeat"] = runtime_statuses[name]["last_heartbeat"]
            entry["error"] = runtime_statuses[name]["error"]
        else:
            entry["runtime_status"] = None
            entry["current_task"] = None
            entry["last_heartbeat"] = None
            entry["error"] = None
        entry["has_persisted_context"] = name in persisted_names
        result.append(entry)

    return result


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


@router.get("/{ws_id}/workers/{name}/events")
async def get_worker_events(
    ws_id: str,
    name: str,
    limit: Optional[int] = Query(None, description="Max number of events to return (most recent)"),
    since: Optional[str] = Query(None, description="ISO-8601 timestamp \u2014 return only events after this time"),
):
    """Return the event log for a specific worker as a JSON array.

    Events are read from ``workers/events.jsonl`` and filtered by the
    worker's ``name`` field (present on every event line).

    Query parameters:
      - ``limit``: max number of events to return (most recent)
      - ``since``: ISO-8601 timestamp \u2014 return only events after this time
    """
    ensure_workspace_dirs(ws_id)
    events_path = _workspace_dir(ws_id) / "workers" / "events.jsonl"
    if not events_path.exists():
        return []

    events = []
    try:
        with open(events_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("worker_name") != name:
                        continue
                    # Apply ?since= filter
                    if since is not None:
                        ts = event.get("timestamp", "")
                        if ts < since:
                            continue
                    events.append(event)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    # Apply ?limit= (most recent N)
    if limit is not None and limit > 0 and len(events) > limit:
        events = events[-limit:]

    return events

