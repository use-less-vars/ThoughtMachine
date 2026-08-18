"""
session_routes.py — REST API for session management.

Provides:
- POST   /api/session/create          — create a new session
- GET    /api/session/list            — list all sessions
- GET    /api/session/{session_id}    — get session details
- DELETE /api/session/{session_id}    — delete a session
- POST   /api/session/{session_id}/rename — rename a session
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from session.store import FileSystemSessionStore
from session.session_registry import SessionRegistry
from thoughtmachine.workspace_registry import WorkspaceRegistry
from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

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
