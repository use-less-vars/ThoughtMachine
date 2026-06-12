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
    """Return the workspace's worker configurations as a JSON array."""
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "workers.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return []


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
