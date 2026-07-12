"""health_routes.py — System Health Dashboard endpoint.

Reads worker status from the filesystem (status.json files written by
worker threads) rather than importing in-memory state.  This makes the
endpoint work even when the backend server runs in a separate process
from the agents.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system")

# ── Filesystem helpers ──────────────────────────────────────────────────────

_WORKSPACES_DIR = Path.home() / ".thoughtmachine" / "workspaces"


def _collect_workers_from_fs() -> list[dict]:
    """Scan *workspaces*/*/workers/ for status.json entries.

    Supports both directory layouts:
      - workers/<name>/status.json                    (legacy, no session)
      - workers/<session_id>/<name>/status.json        (session-scoped)

    Returns a list of worker-info dicts (one per status.json found).
    """
    workers: list[dict] = []

    if not _WORKSPACES_DIR.is_dir():
        return workers

    for ws_dir in _WORKSPACES_DIR.iterdir():
        if not ws_dir.is_dir():
            continue

        # Resolve the human-facing workspace path from config.json
        workspace_path = str(ws_dir)
        config_path = ws_dir / "config.json"
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                if "root" in cfg:
                    workspace_path = cfg["root"]
            except (json.JSONDecodeError, OSError):
                pass

        workers_dir = ws_dir / "workers"
        if not workers_dir.is_dir():
            continue

        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue

            # Check if this is a session-scoped or legacy directory
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/
                session_id = subdir.name
                for worker_subdir in subdir.iterdir():
                    if not worker_subdir.is_dir():
                        continue
                    status_path = worker_subdir / "status.json"
                    if status_path.exists():
                        try:
                            data = json.loads(status_path.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError):
                            continue
                        workers.append({
                            "worker_name": worker_subdir.name,
                            "session_id": data.get("session_id") or session_id,
                            "workspace_path": workspace_path,
                            "status": data.get("runtime_status", "unknown"),
                            "current_task": data.get("current_task"),
                            "last_heartbeat": data.get("last_heartbeat"),
                            "error": data.get("error"),
                            "current_context_tokens": data.get("current_context_tokens"),
                            "max_context_tokens": data.get("max_context_tokens"),
                        })
            else:
                # Legacy: workers/<name>/
                status_path = subdir / "status.json"
                if status_path.exists():
                    try:
                        data = json.loads(status_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        continue
                    workers.append({
                        "worker_name": subdir.name,
                        "session_id": data.get("session_id"),
                        "workspace_path": workspace_path,
                        "status": data.get("runtime_status", "unknown"),
                        "current_task": data.get("current_task"),
                        "last_heartbeat": data.get("last_heartbeat"),
                        "error": data.get("error"),
                        "current_context_tokens": data.get("current_context_tokens"),
                        "max_context_tokens": data.get("max_context_tokens"),
                    })

    return workers


def _collect_sessions_from_store() -> list[dict]:
    """Load open sessions via FileSystemSessionStore.

    Returns a list of session-info dicts with workspace path resolved
    via the workspace config.
    """
    sessions: list[dict] = []

    try:
        from session.store import FileSystemSessionStore
        from thoughtmachine.workspace_capabilities import _workspace_dir

        store = FileSystemSessionStore()
        open_ids = store.get_open_sessions()

        for sid in open_ids:
            try:
                sess = store.load_session(sid)
                if sess is None:
                    continue

                # Resolve workspace path from workspace_id
                ws_path = ""
                ws_id = getattr(sess, "workspace_id", None)
                if ws_id:
                    ws_dir = _workspace_dir(ws_id)
                    config_file = ws_dir / "config.json"
                    if config_file.exists():
                        try:
                            cfg_data = json.loads(config_file.read_text(encoding="utf-8"))
                            ws_path = cfg_data.get("root", str(ws_dir))
                        except (json.JSONDecodeError, OSError):
                            ws_path = str(ws_dir)
                    else:
                        ws_path = str(ws_dir)
                if not ws_path:
                    ws_path = str(getattr(sess, "workspace", ""))

                # Determine mode
                mode = getattr(sess, "mode", None)
                if mode is None:
                    metadata = getattr(sess, "metadata", None) or {}
                    mode = metadata.get("mode", "unknown")

                created_at = getattr(sess, "created_at", None)
                if created_at is not None:
                    created_at = str(created_at)
                updated_at = getattr(sess, "updated_at", None)
                if updated_at is not None:
                    updated_at = str(updated_at)

                sessions.append({
                    "session_id": sid,
                    "workspace": ws_path,
                    "created_at": created_at,
                    "last_activity": updated_at,
                    "mode": mode,
                })
            except Exception:
                continue
    except ImportError:
        logger.warning("FileSystemSessionStore not available")
    except Exception as exc:
        logger.warning("Failed to load sessions: %s", exc)

    return sessions


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/health")
async def system_health(workspace: Optional[str] = Query(None)):
    """Aggregate system state: workers, sessions, and recent event log.

    Parameters
    ----------
    workspace : str, optional
        If provided, only return workers/sessions whose workspace path
        contains this string (simple substring match).
    """
    # 1. running_workers — from filesystem status.json files
    running_workers = _collect_workers_from_fs()
    if workspace:
        running_workers = [
            w for w in running_workers
            if w.get("workspace_path") and workspace in w["workspace_path"]
        ]

    # 2. active_sessions — from FileSystemSessionStore
    active_sessions = _collect_sessions_from_store()
    if workspace:
        active_sessions = [
            s for s in active_sessions
            if workspace in s.get("workspace", "")
        ]

    # 3. recent_event_log_tail — from EventLogger (filesystem)
    event_log_entries: list[dict] = []
    event_log_note: Optional[str] = None
    try:
        from agent.logging.event_logger import EventLogger

        entries = EventLogger.instance().get_tail(20)
        if entries:
            event_log_entries = entries
        else:
            event_log_note = (
                f"Event log is empty or not found at "
                f"{EventLogger.instance().file_path}"
            )
    except Exception as exc:
        event_log_note = f"EventLogger unavailable: {exc}"

    return {
        "running_workers": running_workers,
        "active_sessions": active_sessions,
        "recent_event_log_tail": {
            "entries": event_log_entries,
            "note": event_log_note,
        },
    }
