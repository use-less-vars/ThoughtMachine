"""
session_registry.py — Global session registry.

Maintains a JSON file at ~/.thoughtmachine/session_registry.json that tracks
every session across all workspaces. Used for fast listing, cross-workspace
lookup, and rebuilding the session list on startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _registry_path() -> Path:
    return Path.home() / ".thoughtmachine" / "session_registry.json"


class SessionRegistry:
    """Thread-safe global session registry backed by a JSON file."""

    _instance: Optional["SessionRegistry"] = None
    _lock = threading.Lock()

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _registry_path()
        self._file_lock = threading.Lock()

    @classmethod
    def get_default(cls) -> "SessionRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("Registry is not a dict; resetting.")
                return {}
            return raw
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load registry: %s; resetting.", exc)
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(str(tmp), str(self._path))

    def get_all(self) -> Dict[str, Any]:
        """Return the full registry dict: {session_id: {workspace_id, name, mode, ...}}"""
        with self._file_lock:
            return dict(self._load())

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._file_lock:
            return self._load().get(session_id)

    def register(self, session_id: str, workspace_id: str, name: str, mode: str) -> None:
        """Register or update a session in the registry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._file_lock:
            data = self._load()
            existing = data.get(session_id, {})
            data[session_id] = {
                "session_id": session_id,
                "workspace_id": workspace_id,
                "name": name,
                "mode": mode,
                "last_active": now,
                "is_open": existing.get("is_open", False),
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }
            self._save(data)

    def set_open(self, session_id: str, is_open: bool = True) -> None:
        """Mark a session as open or closed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._file_lock:
            data = self._load()
            if session_id in data:
                data[session_id]["is_open"] = is_open
                data[session_id]["last_active"] = now
                data[session_id]["updated_at"] = now
                self._save(data)

    def remove(self, session_id: str) -> bool:
        """Remove a session from the registry. Returns True if existed."""
        with self._file_lock:
            data = self._load()
            if session_id in data:
                del data[session_id]
                self._save(data)
                return True
            return False

    def rebuild_from_disk(self) -> int:
        """Scan all workspace session directories and rebuild the registry.
        Returns the number of sessions found."""
        base = Path.home() / ".thoughtmachine"
        sessions_dir = base / "sessions"
        workspaces_dir = base / "workspaces"

        registry = {}
        
        # Legacy sessions
        if sessions_dir.exists():
            for f in sessions_dir.glob("*.json"):
                if f.name.startswith("_meta_") or f.name == "open_sessions.json" or not f.is_file():
                    continue
                self._read_session_file(f, None, registry)

        # Workspace-scoped sessions
        if workspaces_dir.exists():
            for ws_dir in workspaces_dir.iterdir():
                if not ws_dir.is_dir():
                    continue
                ws_sessions_dir = ws_dir / "sessions"
                if ws_sessions_dir.exists():
                    for f in ws_sessions_dir.glob("*.json"):
                        if f.name.startswith("_meta_") or f.name == "open_sessions.json" or not f.is_file():
                            continue
                        self._read_session_file(f, ws_dir.name, registry)

        with self._file_lock:
            self._save(registry)
        return len(registry)

    def _read_session_file(self, path: Path, workspace_id: Optional[str], registry: Dict) -> None:
        """Read a session JSON file and add its entry to the registry dict."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data.get("session_id", "")
            if not sid:
                return
            # Only add if not already present (first found wins)
            if sid not in registry:
                registry[sid] = {
                    "session_id": sid,
                    "workspace_id": workspace_id or data.get("workspace_id", ""),
                    "name": data.get("metadata", {}).get("name", "Untitled"),
                    "mode": data.get("mode", "agent"),
                    "last_active": data.get("last_active", ""),
                    "is_open": False,
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                }
        except (json.JSONDecodeError, OSError):
            pass
