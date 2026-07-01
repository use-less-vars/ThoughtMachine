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
from datetime import datetime, timezone
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

from tools.workspace.worker import _worker_registry, _registry_lock

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
      - runtime_status:  "ready" | "busy" | "completed" | "error" | None
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

    # 2. Read runtime status from status.json written by the worker thread.
    #    This bridges the agent process and the backend API server process.
    workers_dir = ws_dir / "workers"
    runtime_statuses = {}
    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if subdir.is_dir():
                status_path = subdir / "status.json"
                if status_path.exists():
                    try:
                        data = json.loads(status_path.read_text(encoding="utf-8"))
                        runtime_statuses[subdir.name] = {
                            "runtime_status": data.get("runtime_status"),
                            "current_task": data.get("current_task"),
                            "last_heartbeat": data.get("last_heartbeat"),
                            "error": data.get("error"),
                            "session_id": data.get("session_id"),
                            "current_context_tokens": data.get("current_context_tokens"),
                            "max_context_tokens": data.get("max_context_tokens"),
                        }
                    except (json.JSONDecodeError, OSError):
                        pass

    # 3. Check for persisted contexts on disk
    persisted_names = set()
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
            entry["session_id"] = runtime_statuses[name]["session_id"]
            entry["current_context_tokens"] = runtime_statuses[name]["current_context_tokens"]
            entry["max_context_tokens"] = runtime_statuses[name]["max_context_tokens"]
        else:
            entry["runtime_status"] = None
            entry["current_task"] = None
            entry["last_heartbeat"] = None
            entry["error"] = None
            entry["session_id"] = None
            entry["current_context_tokens"] = None
            entry["max_context_tokens"] = None
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


# ── POST /api/workspace/{ws_id}/workers/{name}/stop ────────────────────────


@router.post("/{ws_id}/workers/{name}/stop")
async def stop_worker(ws_id: str, name: str):
    """Stop a running worker via a file-based command signal.

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
    worker_dir = ws_dir / "workers" / name

    if not worker_dir.is_dir():
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

    # Fast-path: if the thread is in-memory (same process), signal directly
    with _registry_lock:
        thread = _worker_registry.get(name)
    if thread is not None:
        try:
            thread.stop()
        except Exception:
            pass  # File-based stop will still work

    return {"status": "ok", "name": name}

