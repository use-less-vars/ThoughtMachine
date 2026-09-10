"""
session_routes.py — REST API for session management.

Provides:
- POST   /api/session/create          — create a new session
- GET    /api/session/list            — list all sessions
- GET    /api/session/{session_id}    — get session details
- DELETE /api/session/{session_id}    — delete a session
- POST   /api/session/{session_id}/rename — rename a session
- GET    /api/session/{session_id}/permissions  — stored raw + computed effective session permissions
- PUT    /api/session/{session_id}/permissions  — atomically replace the stored raw session permissions
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ValidationError

from session.store import FileSystemSessionStore
from session.session_registry import SessionRegistry
from thoughtmachine.permission_store import (
    PermissionStoreError,
    read_session_permissions,
    session_grants_path,
    workspace_ceiling,
    write_session_permissions,
)
from thoughtmachine.security import (
    PERMISSION_SCHEMA,
    SessionPermissions,
    coerce_session_permissions,
)
from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_registry import WorkspaceRegistry
from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    ensure_workspace_dirs,
    load_workspace_capabilities,
)

from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager

# ── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/session")


# ── Pydantic models ─────────────────────────────────────────────────────────


class CreateSessionBody(BaseModel):
    name: Optional[str] = None
    workspace_id: Optional[str] = None
    mode: Optional[str] = None
    workspace_path: Optional[str] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    name: str
    created_at: str
    updated_at: str
    workspace_id: str = ""
    mode: str = "agent"


class RenameSessionBody(BaseModel):
    name: str


class SessionListItem(BaseModel):
    session_id: str
    name: str
    mode: str = "agent"
    workspace_id: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    preview: str = ""
    session_size_bytes: Optional[int] = None


class SessionDetailResponse(BaseModel):
    session_id: str
    name: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    message_count: int = 0
    workspace_id: str = ""
    mode: str = "agent"
    session_size_bytes: Optional[int] = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_store() -> FileSystemSessionStore:
    """Get the shared FileSystemSessionStore singleton."""
    return FileSystemSessionStore.get_instance()


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/create", response_model=CreateSessionResponse)
async def create_session(body: CreateSessionBody) -> Dict[str, Any]:
    """Create a new session and persist it immediately.

    Optionally accepts a ``name`` and ``workspace_id``.
    Returns the created session's metadata.
    """
    try:
        store = _get_store()
        session_manager = SessionManager(store, ConfigManager())
        mode = body.mode or "custom"
        session_id, _frontend_config = session_manager.create_session(
            mode=mode,
            workspace_path=body.workspace_path,
            audit_source='api',
        )

        # Reload the persisted session so we can layer workspace/name metadata
        # on top of the SessionManager-built session (create_session does not
        # handle workspace_id itself).
        session = store.load_session(session_id, workspace_id=None)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found after creation: {session_id}",
            )

        if body.name:
            session.metadata['name'] = body.name
        if body.workspace_path:
            # Register the path via WorkspaceRegistry, which returns
            # the existing entry (if already registered) or creates a new one.
            registry = WorkspaceRegistry.get_default()
            entry = registry.register_by_root(body.workspace_path)
            session.workspace_id = entry.id
            ensure_workspace_dirs(entry.id)
        elif body.workspace_id:
            session.workspace_id = body.workspace_id
            # Look up root_path from workspace registry and store in metadata
            # so that when the session is loaded via WebSocket, the bridge can
            # pick it up from agent_config in session metadata.
            try:
                registry = WorkspaceRegistry.get_default()
                entry = registry.get_workspace(body.workspace_id)
                if entry and entry.root_path:
                    if 'agent_config' not in session.metadata:
                        session.metadata['agent_config'] = {}
                    session.metadata['agent_config']['workspace_path'] = entry.root_path
            except Exception:
                pass
        session.ensure_name()

        # Re-save so workspace_id + name + agent_config land on disk.
        # save_session moves the file to the workspace-scoped location.
        store.save_session(session, workspace_id=session.workspace_id)

        # Register in global session registry
        registry = SessionRegistry.get_default()
        registry.register(
            session_id=session.session_id,
            workspace_id=session.workspace_id or "",
            name=session.metadata.get('name', 'Untitled'),
            mode=session.mode,
        )
        registry.set_open(session.session_id, is_open=True)

        return {
            "session_id": session.session_id,
            "name": session.metadata.get('name', 'Untitled Session'),
            "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at),
            "updated_at": session.updated_at.isoformat() if hasattr(session.updated_at, 'isoformat') else str(session.updated_at),
            "workspace_id": session.workspace_id or "",
            "mode": session.mode,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {exc}",
        )


@router.get("/list", response_model=List[SessionListItem])
async def list_sessions(
    workspace_id: Optional[str] = Query(None, description="Filter by workspace ID"),
) -> List[Dict[str, Any]]:
    """List all saved sessions with basic metadata.

    Uses the global session registry as the primary source, falling back
    to a disk scan if the registry is empty. Optionally filtered by
    ``workspace_id``.
    """
    try:
        registry = SessionRegistry.get_default()
        all_sessions = registry.get_all()
        sessions = list(all_sessions.values())

        # Fall back to disk scan if registry is empty
        if not sessions:
            registry.rebuild_from_disk()
            all_sessions = registry.get_all()
            sessions = list(all_sessions.values())

        # Filter by workspace if specified
        if workspace_id:
            sessions = [s for s in sessions if s.get('workspace_id') == workspace_id]

        # Try to get previews from session store for all sessions
        store = _get_store()
        session_ids = [s.get("session_id", "") for s in sessions if s.get("session_id")]
        metadata_batch = store.load_sessions_metadata_batch(session_ids, workspace_id=workspace_id)

        # Map registry fields to the expected SessionListItem format
        result = []
        for s in sessions:
            sid = s.get("session_id", "")
            meta = metadata_batch.get(sid) if metadata_batch else None
            result.append({
                "session_id": sid,
                "name": s.get("name", "Untitled"),
                "mode": s.get("mode", "agent"),
                "workspace_id": s.get("workspace_id", ""),
                "created_at": s.get("created_at"),
                "updated_at": meta.get("updated_at") if meta else s.get("updated_at"),
                "preview": meta.get("preview", "") if meta else "",
                "session_size_bytes": meta.get("session_size_bytes") if meta else None,
            })

        return result
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {exc}",
        )


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific session.

    Returns session metadata including message count.
    """
    try:
        store = _get_store()
        session = store.load_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )

        message_count = len(session.user_history) if session.user_history else 0

        return {
            "session_id": session.session_id,
            "name": session.metadata.get('name', 'Untitled Session'),
            "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at),
            "updated_at": session.updated_at.isoformat() if hasattr(session.updated_at, 'isoformat') else str(session.updated_at),
            "message_count": message_count,
            "workspace_id": session.workspace_id or "",
            "mode": session.mode,
            "session_size_bytes": store.get_session_size_bytes(session_id),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session: {exc}",
        )


@router.post("/{session_id}/rename", response_model=CreateSessionResponse)
async def rename_session(
    session_id: str,
    body: RenameSessionBody,
) -> Dict[str, Any]:
    """Rename a session.

    Accepts a new name. The session is loaded, renamed, and persisted.
    Returns the updated session metadata.
    """
    try:
        store = _get_store()
        session = store.load_session(session_id, workspace_id=None)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )

        session.metadata['name'] = body.name
        store.save_session(session, workspace_id=session.workspace_id)

        # Update name in global session registry
        registry = SessionRegistry.get_default()
        registry.register(
            session_id=session.session_id,
            workspace_id=session.workspace_id or "",
            name=body.name,
            mode=session.mode,
        )

        return {
            "session_id": session.session_id,
            "name": session.metadata.get('name', 'Untitled Session'),
            "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at),
            "updated_at": session.updated_at.isoformat() if hasattr(session.updated_at, 'isoformat') else str(session.updated_at),
            "workspace_id": session.workspace_id or "",
            "mode": session.mode,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rename session: {exc}",
        )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    workspace_id: Optional[str] = Query(None, description="Workspace ID for scoped deletion"),
) -> Dict[str, Any]:
    """Delete a session by ID.

    Optionally accepts ``workspace_id`` for scoped deletion.
    Returns ``{\"success\": true}`` if found and deleted.
    """
    try:
        store = _get_store()
        found = store.delete_session(session_id, workspace_id=workspace_id)
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
        store.remove_open_session(session_id)
        # Remove from global session registry
        registry = SessionRegistry.get_default()
        registry.remove(session_id)
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {exc}",
        )


# ── GET/PUT /api/session/{session_id}/permissions ─────────────────────
# Step-5 disk-pure session permission endpoints: raw = stored uncapped grants
# (vault permission-store sidecar first, legacy session-record
# metadata.session_config.session_permissions fallback); effective = raw
# coerced to the full session profile, capped by the workspace ceiling
# (config.json['permissions']) and merged with workspace capabilities in the
# security gate (same computation as GET /api/workspace/{ws_id}/effective_permissions);
# resolved_at = UTC ISO-8601 timestamp of this resolution.


def _session_record_exists(vault_root_path, workspace_id: str, session_id: str) -> bool:
    """True when a legacy session-record JSON under
    <vault>/workspaces/<ws>/sessions carries this session_id.

    Content scan mirroring thoughtmachine.permission_store._session_record_path
    (files named _meta_* are skipped; unparseable / non-matching files are
    skipped silently).  Duplicated here so the 404-vs-500 distinction for a
    missing permission source does not depend on the store's private helpers.
    """
    sessions_dir = (
        Path(vault_root_path) / "workspaces" / workspace_id / "sessions"
    )
    if not sessions_dir.is_dir():
        return False
    for file_path in sorted(sessions_dir.glob("*.json")):
        if file_path.name.startswith("_meta_"):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("session_id") == session_id:
            return True
    return False


def _load_permission_session(session_id: str):
    """Load a session for the permission endpoints; 404 when unknown
    (mirrors the GET /{session_id} detail handler)."""
    store = _get_store()
    session = store.load_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )
    return store, session


def _compute_effective_session_permissions(
    vault, workspace_id: str, session_id: str, raw_perms: Dict[str, Any]
) -> Dict[str, Any]:
    """Compute effective session permissions enforced at runtime.

    Pipeline (mirrors GET /api/workspace/{ws_id}/effective_permissions):
    coerce raw grants to the full 10-key session profile (safe defaults when
    empty) -> build SessionPermissions -> apply the workspace ceiling from
    config.json['permissions'] (via the permission store) -> merge with
    workspace capabilities in the security gate.  A corrupt/missing workspace
    config is treated as "no ceiling" ({}) so a broken ceiling never nukes the
    session permission read.  The gate import is lazy (import precedent:
    workspace_routes.py effective_permissions handler).
    """
    from security.security_gate import get_effective_permissions as _gate_effective

    caps = load_workspace_capabilities(workspace_id)
    if caps is None:
        caps = WorkspaceCapabilities.default()

    raw = coerce_session_permissions(raw_perms) if raw_perms else {}
    session_obj = SessionPermissions(**raw) if raw else SessionPermissions()

    try:
        ceiling = workspace_ceiling(vault, workspace_id)
    except PermissionStoreError:
        # Unreadable/missing workspace ceiling must not fail the session read;
        # no ceiling == raw session grants pass through capability merge.
        ceiling = {}
    return _gate_effective(session_obj, caps, ceiling)


def _ceiling_provenance(effective: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the workspace-ceiling provenance for a REST response (additive).

    Returns ``{"workspace_ceiling": {resource: level}, "contradictions": [...]}``
    where ``workspace_ceiling`` is the ceiling level the gate actually enforced
    per restricted resource, and ``contradictions`` flags every resource whose
    SESSION grant sits strictly above its workspace ceiling.  The gate's ceiling
    annotations are only READ here -- never altered.  On ImportError both keys
    degrade to empty/neutral values (no silent fallback of the permissions
    themselves).
    """
    try:
        from security.security_gate import ceiling_contradictions, ceiling_levels

        return {
            "workspace_ceiling": ceiling_levels(effective),
            "contradictions": ceiling_contradictions(effective),
        }
    except ImportError:
        return {"workspace_ceiling": {}, "contradictions": []}


@router.get("/{session_id}/permissions")
async def get_session_permissions(session_id: str) -> Dict[str, Any]:
    """Return the session's stored (raw) and computed (effective) permissions:
    {"raw": <dict>, "effective": <dict>, "resolved_at": <ISO-8601 UTC>}.

    raw is the uncapped grant map read from the vault permission-store sidecar
    <vault>/workspaces/<ws>/sessions/<sid>/permissions.json with legacy
    fallback to metadata.session_config.session_permissions in the session
    record; {} is a valid raw value (record exists but carries no grants).
    404 when the session has no permission source at all (no sidecar and no
    matching legacy record); 500 when a present source is corrupt/unreadable
    (unparseable non-matching legacy records are skipped by the content scan,
    mirroring the permission store, and therefore count as "no source").
    """
    try:
        _store, session = _load_permission_session(session_id)
        ws_id = session.workspace_id or ""
        vault = vault_root()

        sidecar = session_grants_path(vault, ws_id, session_id)
        if not sidecar.exists() and not _session_record_exists(vault, ws_id, session_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no session permissions stored for session {session_id}",
            )
        try:
            raw = read_session_permissions(vault, ws_id, session_id)
        except PermissionStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"permission store read failed: {exc}",
            )
        effective = _compute_effective_session_permissions(
            vault, ws_id, session_id, raw
        )
        return {
            "raw": raw,
            "effective": effective,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            **_ceiling_provenance(effective),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session permissions: {exc}",
        )


@router.put("/{session_id}/permissions")
async def put_session_permissions(
    session_id: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Atomically replace the session's stored raw permission grants.

    The request body IS the raw permissions JSON object (full-replace
    semantics).  Order: (1) object body (FastAPI-enforced by the Dict
    annotation); (2) unknown keys (not in PERMISSION_SCHEMA) -> 422
    {"detail": {"errors": ["unknown session permission key: <k>"]}};
    (3) strict value validation via SessionPermissions(**body) ->
    pydantic.ValidationError -> 422 {"detail": {"errors": ["<loc>: <msg>"]}};
    (4) normalized = coerce_session_permissions(body) (invalid values that
    the wider model accepts but the store schema does not, e.g.
    filesystem:"full" or a legacy network bool, fall back to safe defaults);
    (5) atomic write via permission_store.write_session_permissions (same-dir
    temp file + fsync + os.replace); (6) recompute effective exactly like GET
    and return {"raw": normalized, "effective": ..., "resolved_at": ...}.
    An empty object {} clears explicit grants (all-defaults profile).
    """
    try:
        _store, session = _load_permission_session(session_id)
        ws_id = session.workspace_id or ""
        vault = vault_root()

        unknown = sorted(set(body.keys()) - set(PERMISSION_SCHEMA.keys()))
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "errors": [
                        f"unknown session permission key: {k}" for k in unknown
                    ]
                },
            )
        try:
            SessionPermissions(**body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "errors": [
                        f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', '')}"
                        for err in exc.errors()
                    ]
                },
            ) from None
        normalized = coerce_session_permissions(body)
        write_session_permissions(vault, ws_id, session_id, normalized)
        effective = _compute_effective_session_permissions(
            vault, ws_id, session_id, normalized
        )
        return {
            "raw": normalized,
            "effective": effective,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            **_ceiling_provenance(effective),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save session permissions: {exc}",
        )
