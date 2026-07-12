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

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    _workspace_dir,
    ensure_workspace_dirs,
    load_workspace_capabilities,
)

from tools.workspace.worker import _worker_registry, _registry_lock

from agent.models.worker_definition import WorkerDefinition

# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/workspace")

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

@router.get("/{ws_id}/workers")
async def get_workers(
    ws_id: str,
    name: Optional[str] = Query(None, description="Filter to a single worker by name"),
):
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
    #    Supports both structures:
    #      - workers/<name>/status.json (no session)
    #      - workers/<session_id>/<name>/status.json (session-scoped)
    workers_dir = ws_dir / "workers"
    runtime_statuses = {}
    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            # Check if this is a session directory (contains sub-worker dirs)
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/
                session_id = subdir.name
                for worker_subdir in subdir.iterdir():
                    if worker_subdir.is_dir():
                        status_path = worker_subdir / "status.json"
                        if status_path.exists():
                            try:
                                data = json.loads(status_path.read_text(encoding="utf-8"))
                                runtime_statuses[worker_subdir.name] = {
                                    "runtime_status": data.get("runtime_status"),
                                    "current_task": data.get("current_task"),
                                    "last_heartbeat": data.get("last_heartbeat"),
                                    "error": data.get("error"),
                                    "session_id": data.get("session_id") or session_id,
                                    "current_context_tokens": data.get("current_context_tokens"),
                                    "max_context_tokens": data.get("max_context_tokens"),
                                }
                            except (json.JSONDecodeError, OSError):
                                pass
            else:
                # Legacy: workers/<name>/
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
    #    Supports both legacy and session-scoped structures.
    persisted_names = set()
    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/context.json
                for worker_subdir in subdir.iterdir():
                    if worker_subdir.is_dir() and (worker_subdir / "context.json").exists():
                        persisted_names.add(worker_subdir.name)
            else:
                # Legacy: workers/<name>/context.json
                if (subdir / "context.json").exists():
                    persisted_names.add(subdir.name)

    # 4. Merge everything
    result = []
    for cfg in configs:
        if isinstance(cfg, str):
            # Entry is just a string name — promote to a minimal dict
            worker_name = cfg
            cfg = {"name": cfg}
        else:
            worker_name = cfg.get("name", "")
        entry = dict(cfg)
        if worker_name in runtime_statuses:
            entry["runtime_status"] = runtime_statuses[worker_name]["runtime_status"]
            entry["current_task"] = runtime_statuses[worker_name]["current_task"]
            entry["last_heartbeat"] = runtime_statuses[worker_name]["last_heartbeat"]
            entry["error"] = runtime_statuses[worker_name]["error"]
            entry["session_id"] = runtime_statuses[worker_name]["session_id"]
            entry["current_context_tokens"] = runtime_statuses[worker_name]["current_context_tokens"]
            entry["max_context_tokens"] = runtime_statuses[worker_name]["max_context_tokens"]
        else:
            entry["runtime_status"] = None
            entry["current_task"] = None
            entry["last_heartbeat"] = None
            entry["error"] = None
            entry["session_id"] = None
            entry["current_context_tokens"] = None
            entry["max_context_tokens"] = None
        entry["has_persisted_context"] = worker_name in persisted_names
        result.append(entry)

    # 5. Optional ?name= filter — return single worker entry or 404
    if name is not None:
        for entry in result:
            if entry.get("name") == name:
                return entry
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{name}' not found",
        )

    return result

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

# ── POST /api/workspace/{ws_id}/workers/{name}/stop ────────────────────────

@router.post("/{ws_id}/workers/{name}/stop")
async def stop_worker(ws_id: str, name: str):
    """Stop a running worker via a file-based command signal.

    Supports both directory layouts:
      - ``workers/<session_id>/<name>/`` (session-scoped)
      - ``workers/<name>/`` (legacy, no session)

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
    workers_dir = ws_dir / "workers"

    # ── Find the worker directory (session-scoped first, then legacy) ──
    worker_dir: Optional[Path] = None

    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            # Check if this is a session directory (contains sub-worker dirs)
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/
                candidate = subdir / name
                if candidate.is_dir():
                    worker_dir = candidate
                    break
            else:
                # Legacy: workers/<name>/
                if subdir.name == name:
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
    # Registry keys are tuples (session_id, worker_name), so iterate all
    # entries matching the worker name across all sessions.
    with _registry_lock:
        for (sid, wname), thread in list(_worker_registry.items()):
            if wname == name:
                try:
                    thread.stop()
                except Exception:
                    pass  # File-based stop will still work

    return {"status": "ok", "name": name}

