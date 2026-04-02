"""
Session Store: Persistence layer for Session objects.

Provides an abstract interface and a file-system based implementation
that stores each session as a JSON file in a configured directory.
"""
import json
import os
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
import logging
logger = logging.getLogger(__name__)

from .models import Session


def _sanitize_filename(name: str, max_length: int = 100) -> str:
    """Sanitize a string to be safe for use as a filename.
    
    Removes or replaces characters that are problematic on common filesystems.
    """
    # Replace path separators and other problematic characters
    name = re.sub(r'[\\/*?:"<>|]', '_', name)
    # Replace any non-ASCII characters?
    # Keep spaces, dots, hyphens, underscores, alphanumeric
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Strip leading/trailing spaces and underscores
    name = name.strip(' _')
    # Ensure not empty
    if not name:
        name = 'Untitled'
    # Truncate to max length
    if len(name) > max_length:
        # Try to cut at word boundary
        truncated = name[:max_length].rsplit(' ', 1)[0]
        if len(truncated) < max_length // 2:
            truncated = name[:max_length]
        name = truncated.strip(' _')
    return name


def _generate_friendly_filename(session_id: str, session_name: str) -> str:
    """Generate a friendly filename for a session.
    
    Format: {sanitized_name}_{short_id}.json
    """
    sanitized = _sanitize_filename(session_name)
    short_id = session_id[:6]  # First 6 chars of UUID
    return f"{sanitized}_{short_id}.json"


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
    Saves each session as a JSON file in the sessions_dir with friendly filenames: {sanitized_name}_{short_id}.json
    """

    def __init__(self, sessions_dir: Optional[str] = None):
        """
        Initialize.

        Args:
            sessions_dir: Directory to store session files. If None, defaults to
                         ~/.thoughtmachine/sessions
        """
        logger.debug(f"[SessionStore] Initializing with sessions_dir={sessions_dir}")
        self._original_sessions_dir = sessions_dir  # Store original parameter
        if sessions_dir is None:
            home = os.path.expanduser("~")
            sessions_dir = os.path.join(home, ".thoughtmachine", "sessions")
            logger.debug(f"[SessionStore] Using default directory: {sessions_dir}")
        self.sessions_dir = Path(sessions_dir)
        logger.debug(f"[SessionStore] Final sessions_dir: {self.sessions_dir}")
        # Try to create directory, with fallbacks if needed
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"[SessionStore] Directory created/exists: {self.sessions_dir}")
        except OSError as e:
            # Only attempt fallbacks if using default directory (not user-provided)
            if self._original_sessions_dir is None:
                logger.warning(f"[SessionStore] Warning: Could not create default sessions directory at {self.sessions_dir}: {e}")
                # Try fallback in current working directory
                try:
                    import sys
                    fallback = Path.cwd() / ".thoughtmachine" / "sessions"
                    fallback.mkdir(parents=True, exist_ok=True)
                    self.sessions_dir = fallback
                    logger.info(f"[SessionStore] Using fallback directory: {self.sessions_dir}")
                except OSError as e2:
                    logger.warning(f"[SessionStore] Warning: Could not create fallback directory at {fallback}: {e2}")
                    # Try system temp directory as last resort
                    import tempfile
                    temp_fallback = Path(tempfile.gettempdir()) / "thoughtmachine_sessions"
                    temp_fallback.mkdir(parents=True, exist_ok=True)
                    self.sessions_dir = temp_fallback
                    logger.info(f"[SessionStore] Using temp directory: {self.sessions_dir}")
            else:
                # User-provided directory, re-raise the error
                raise

    def _get_session_path(self, session_id: str) -> Path:
        """Get the file path for a session ID."""
        return self.sessions_dir / f"{session_id}.json"

    def _find_session_path(self, session_id: str) -> Optional[Path]:
        """Find the actual file path for a session ID by scanning JSON files."""
        for file_path in self.sessions_dir.glob("*.json"):
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                if data.get('session_id') == session_id:
                    return file_path
            except Exception:
                continue
        return None

    def _get_friendly_path(self, session: Session) -> Path:
        """Get friendly filename path for a session."""
        name = session.metadata.get('name', 'Untitled Session')
        filename = _generate_friendly_filename(session.session_id, name)
        return self.sessions_dir / filename

    def save_session(self, session: Session) -> None:
        """Save a session to a JSON file."""
        logger.debug(f"[SessionStore] Saving session {session.session_id}")
        # Update the updated_at timestamp
        session.updated_at = datetime.now()
        data = session.to_persistable_dict()
        
        # Remove external_file_path from metadata if present (legacy concept)
        if 'metadata' in data and 'external_file_path' in data['metadata']:
            del data['metadata']['external_file_path']
        
        # Determine the friendly filename
        new_path = self._get_friendly_path(session)
        
        # Find existing file (if any)
        old_path = self._find_session_path(session.session_id)
        
        # If there's an existing file and it's different from new_path, rename it
        if old_path is not None and old_path != new_path:
            logger.debug(f"[SessionStore] Renaming session file from {old_path} to {new_path}")
            # Ensure we don't overwrite another session's file (should not happen due to unique short ID)
            if new_path.exists():
                logger.warning(f"[SessionStore] Target file {new_path} already exists, overwriting")
            old_path.rename(new_path)
        
        # Write the session data
        logger.debug(f"[SessionStore] Writing to {new_path}")
        with open(new_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)  # default=str handles datetime
        
        logger.debug(f"[SessionStore] Session {session.session_id} saved to {new_path}")

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load a session from a JSON file."""
        path = self._find_session_path(session_id)
        if path is None or not path.exists():
            return None
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            # Remove external_file_path from metadata if present (legacy concept)
            if 'metadata' in data and 'external_file_path' in data['metadata']:
                del data['metadata']['external_file_path']
            return Session.from_persistable_dict(data)
        except Exception as e:
            # Log error? For now return None
            logger.error(f"[SessionStore] Error loading session {session_id}: {e}")
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
                logger.error(f"[SessionStore] Error reading {file_path}: {e}")
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
        path = self._find_session_path(session_id)
        if path is not None and path.exists():
            path.unlink()
            return True
        return False

    def get_session_path(self, session_id: str) -> Path:
        """Get the file path for a given session ID."""
        path = self._find_session_path(session_id)
        if path is not None:
            return path
        # Session not saved yet, return the default path (for compatibility)
        return self._get_session_path(session_id)

    def get_current_session_id(self) -> Optional[str]:
        """
        Get the ID of the current session from the marker file.
        Returns None if no marker exists.
        """
        marker = self.sessions_dir / ".current_session"
        logger.debug(f"[SessionStore] get_current_session_id: marker={marker}, exists={marker.exists()}")
        if marker.exists():
            try:
                content = marker.read_text().strip()
                logger.debug(f"[SessionStore] Marker content: '{content}'")
                return content if content else None
            except Exception as e:
                logger.error(f"[SessionStore] Error reading current session marker: {e}")
                return None
        return None

    def set_current_session_id(self, session_id: Optional[str]) -> None:
        """
        Set the current session ID by writing to the marker file.
        If session_id is None, the marker file is removed.
        """
        marker = self.sessions_dir / ".current_session"
        logger.debug(f"[SessionStore] set_current_session_id: marker={marker}, session_id={session_id}")
        # Ensure session_id is a string if not None
        if session_id is not None and not isinstance(session_id, str):
            session_id = str(session_id)
        
        if session_id is None:
            if marker.exists():
                marker.unlink()
                logger.info(f"[SessionStore] Removed marker file")
        else:
            # Atomic write via temp file
            temp_path = marker.with_suffix('.tmp')
            try:
                temp_path.write_text(session_id)
                temp_path.replace(marker)
                logger.info(f"[SessionStore] Wrote marker file with session_id: {session_id}")
            except Exception as e:
                logger.error(f"[SessionStore] Error writing current session marker: {e}")
