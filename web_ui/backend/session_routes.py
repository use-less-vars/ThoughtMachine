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
from session.models import Session
from session.session_registry import SessionRegistry
from thoughtmachine.workspace_registry import WorkspaceRegistry

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
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    preview: str = ""


class SessionDetailResponse(BaseModel):
    session_id: str
    name: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    message_count: int = 0
    workspace_id: str = ""
    mode: str = "agent"


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
        session = Session()
        session.metadata['source'] = 'rest_api'
        if body.name:
            session.metadata['name'] = body.name
        if body.mode:
            session.mode = body.mode
        if body.workspace_path:
            # Register the path via WorkspaceRegistry, which returns
            # the existing entry (if already registered) or creates a new one.
            registry = WorkspaceRegistry.get_default()
            entry = registry.register_by_root(body.workspace_path)
            session.workspace_id = entry.id
        elif body.workspace_id:
            session.workspace_id = body.workspace_id
        session.ensure_name()

        store.save_session(session, workspace_id=session.workspace_id)
        store.add_open_session(session.session_id)

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

        # Map registry fields to the expected SessionListItem format
        result = []
        for s in sessions:
            result.append({
                "session_id": s.get("session_id", ""),
                "name": s.get("name", "Untitled"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "preview": "",
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
