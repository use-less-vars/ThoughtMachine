"""
Session Store: Persistence layer for Session objects.

Provides an abstract interface and a file-system based implementation
that stores each session as a JSON file in a configured directory.
"""
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from .models import Session


class SessionStore(ABC):
    """Abstract base class for session storage."""

    @abstractmethod
    def save_session(self, session: Session) -> None:
        """Save a session to storage."""
        pass

    @abstractmethod
    def load_session(self, session_id: str) -> Optional[Session]:
        """Load a session by ID. Returns None if not found."""
        pass

    @abstractmethod
    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        List all saved sessions with basic metadata.
        Returns a list of dicts with at least: session_id, name, created_at, updated_at.
        """
        pass

    @abstractmethod
    def delete_session(self, session_id: str) -> bool:
        """Delete a session. Returns True if deleted, False if not found."""
        pass


class FileSystemSessionStore(SessionStore):
    """
    File-system based session store.
    Saves each session as a JSON file in the sessions_dir: {session_id}.json
    """

    def __init__(self, sessions_dir: Optional[str] = None):
        """
        Initialize.

        Args:
            sessions_dir: Directory to store session files. If None, defaults to
                         ~/.thoughtmachine/sessions
        """
        if sessions_dir is None:
            home = os.path.expanduser("~")
            sessions_dir = os.path.join(home, ".thoughtmachine", "sessions")
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_id: str) -> Path:
        """Get the file path for a session ID."""
        return self.sessions_dir / f"{session_id}.json"

    def save_session(self, session: Session) -> None:
        """Save a session to a JSON file."""
        # Update the updated_at timestamp
        session.updated_at = datetime.now()
        data = session.to_persistable_dict()
        path = self._get_session_path(session.session_id)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, default=str)  # default=str handles datetime

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load a session from a JSON file."""
        path = self._get_session_path(session_id)
        if not path.exists():
            return None
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            return Session.from_persistable_dict(data)
        except Exception as e:
            # Log error? For now return None
            print(f"[SessionStore] Error loading session {session_id}: {e}")
            return None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        List all saved sessions with basic metadata.
        Reads each JSON file and extracts a few fields.
        """
        sessions = []
        for file_path in self.sessions_dir.glob("*.json"):
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                # Extract minimal metadata for listing
                session_info = {
                    'session_id': data.get('session_id'),
                    'name': data.get('metadata', {}).get('name', 'Untitled Session'),
                    'created_at': data.get('created_at'),
                    'updated_at': data.get('updated_at'),
                    'preview': self._extract_preview(data.get('user_history', [])),
                }
                sessions.append(session_info)
            except Exception as e:
                print(f"[SessionStore] Error reading {file_path}: {e}")
                continue
        # Sort by updated_at descending (most recent first)
        sessions.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
        return sessions

    def _extract_preview(self, user_history: List[Dict[str, Any]], max_length: int = 100) -> str:
        """Extract a short preview from the user_history (first user message)."""
        for msg in user_history:
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, str):
                    return content[:max_length] + ('...' if len(content) > max_length else '')
        return "(empty)"

    def delete_session(self, session_id: str) -> bool:
        """Delete a session file."""
        path = self._get_session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
