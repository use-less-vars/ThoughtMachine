"""
Session Store: Persistence layer for Session objects.

Provides an abstract interface and a file-system based implementation
that stores each session as a JSON file in a configured directory.
"""
import json
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
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

    def __init__(self, sessions_dir: Optional[str] = None, state_dir: Optional[str] = None,
                 enable_session_history_pruning: bool = True):
        """
        Initialize.

        Args:
            sessions_dir: Directory to store session files. If None, defaults to
                         ~/.thoughtmachine/sessions
            state_dir: Directory for state files (open_sessions.json, .current_session).
                      If None, defaults to ~/.thoughtmachine/state
            enable_session_history_pruning: If True (default), old summarization cycles
                         are pruned on save to keep the session file compact. Set to
                         False to disable pruning (useful for debugging or rollback).
        """
        logger.debug(f"[SessionStore] Initializing with sessions_dir={sessions_dir}")
        self._enable_session_history_pruning = enable_session_history_pruning
        logger.debug(f"[SessionStore] Session history pruning enabled: {self._enable_session_history_pruning}")
        self._original_sessions_dir = sessions_dir  # Store original parameter

        # In-memory caches to reduce disk I/O
        self._cached_list: Optional[Tuple[float, List[Dict[str, Any]]]] = None  # (timestamp, list)
        self._cached_paths: Dict[str, Optional[Path]] = {}  # session_id -> path (None = not found)
        self._cached_paths_ts: Dict[str, float] = {}  # when each path entry was cached
        self._cache_ttl = 5.0  # seconds

        # Resolve state directory
        if state_dir is None:
            home = os.path.expanduser("~")
            state_dir = os.path.join(home, ".thoughtmachine", "state")
        self.state_dir = Path(state_dir)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(f"[SessionStore] Could not create state directory at {self.state_dir}")

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

    def _get_meta_path(self, session_id: str) -> Path:
        """Get the file path for a session's lightweight metadata file."""
        return self.sessions_dir / f"_meta_{session_id}.json"

    def _find_session_path(self, session_id: str) -> Optional[Path]:
        """Find the actual file path for a session ID by scanning JSON files.
        Uses an in-memory cache (TTL: 5s) that is invalidated on save/delete.
        """
        # Check cache first (with TTL)
        if session_id in self._cached_paths:
            ts = self._cached_paths_ts.get(session_id, 0)
            if time.time() - ts < self._cache_ttl:
                return self._cached_paths[session_id]
        # Scan files
        for file_path in self.sessions_dir.glob("*.json"):
            # Skip metadata files (used for fast listing)
            if file_path.name.startswith("_meta_"):
                continue
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                if data.get('session_id') == session_id:
                    self._cached_paths[session_id] = file_path
                    self._cached_paths_ts[session_id] = time.time()
                    return file_path
            except Exception:
                continue
        self._cached_paths[session_id] = None  # cache as not found
        self._cached_paths_ts[session_id] = time.time()
        return None

    def _get_friendly_path(self, session: Session) -> Path:
        """Get friendly filename path for a session."""
        name = session.metadata.get('name', 'Untitled Session')
        filename = _generate_friendly_filename(session.session_id, name)
        return self.sessions_dir / filename

    def _save_session_metadata(self, session: Session) -> None:
        """Write a lightweight metadata file for fast session listing.

        The metadata file contains only the fields needed for the sidebar,
        avoiding the need to parse the full session JSON (which can be
        hundreds of messages) just to build the session list.
        """
        meta_path = self._get_meta_path(session.session_id)
        user_history = session.user_history or []
        preview = self._extract_preview(user_history)
        meta = {
            'session_id': session.session_id,
            'name': session.metadata.get('name', 'Untitled Session'),
            'created_at': session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else session.created_at,
            'updated_at': session.updated_at.isoformat() if hasattr(session.updated_at, 'isoformat') else session.updated_at,
            'preview': preview,
        }
        temp_path = meta_path.with_suffix('.tmp')
        try:
            os.makedirs(os.path.dirname(temp_path), exist_ok=True)
            with open(temp_path, 'w') as f:
                json.dump(meta, f)
            temp_path.replace(meta_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

    def save_session(self, session: Session) -> None:
        """Save a session to a JSON file."""
        # Invalidate caches
        self._cached_paths.pop(session.session_id, None)
        self._cached_paths_ts.pop(session.session_id, None)
        self._cached_list = None
        logger.debug(f"[SessionStore] Saving session {session.session_id}")
        # Update the updated_at timestamp
        session.updated_at = datetime.now()
        data = session.to_persistable_dict()

        # Prune old summarization cycles to keep the session file compact.
        # The pruner only acts when there are >= 2 summaries (default min_summaries=2)
        # and preserves the two most recent cycles intact.
        if self._enable_session_history_pruning:
            from session.history_pruner import prune_user_history
            pruned = prune_user_history(data['user_history'])
            data['user_history'] = pruned
        
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
        
        # Write the session data atomically via temp file
        temp_path = new_path.with_suffix('.tmp')
        logger.debug(f"[SessionStore] Writing to {temp_path} (atomic)")
        try:
            os.makedirs(os.path.dirname(temp_path), exist_ok=True)
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)  # default=str handles datetime
            temp_path.replace(new_path)
            logger.debug(f"[SessionStore] Session {session.session_id} saved to {new_path}")
            # Invalidate path cache so subsequent _find_session_path re-scans
            self._cached_paths.pop(session.session_id, None)
            self._cached_paths_ts.pop(session.session_id, None)
            # Write/update the lightweight metadata file for fast listing
            self._save_session_metadata(session)
        except Exception:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise

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

    def load_session_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Lightweight metadata load — reads the session JSON file directly and
        extracts only the fields needed for listing, avoiding the expensive
        Session.from_persistable_dict() deserialization (which creates thousands
        of Message objects).

        Uses a directory scan to find the session file in a single pass rather
        than calling _find_session_path (which also scans but for a different
        purpose), avoiding double-reads of large JSON files.

        Returns a dict with keys:
            session_id, name, updated_at, message_count
        or None if the session file is not found.
        """
        try:
            for file_path in self.sessions_dir.glob("*.json"):
                # Skip metadata files
                if file_path.name.startswith("_meta_"):
                    continue
                try:
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        continue
                    if data.get('session_id') == session_id:
                        name = data.get('metadata', {}).get('name', 'Untitled Session')
                        updated_at = data.get('updated_at')
                        user_history = data.get('user_history', [])
                        message_count = len(user_history) if isinstance(user_history, list) else 0
                        return {
                            'session_id': session_id,
                            'name': name,
                            'updated_at': updated_at,
                            'message_count': message_count,
                        }
                except Exception:
                    continue
            return None
        except Exception as e:
            logger.error(f"[SessionStore] Error loading session metadata for {session_id}: {e}")
            return None

    def load_sessions_metadata_batch(self, session_ids: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """
        Batch metadata load — reads ALL session JSON files in a single directory
        scan and returns metadata for all requested session IDs.

        This is far more efficient than calling load_session_metadata() in a loop
        because each file is read only once, regardless of how many session IDs
        are requested.

        Returns a dict mapping session_id -> metadata dict (or None if not found)
        with metadata keys: session_id, name, updated_at, message_count
        """
        wanted = set(session_ids)
        if not wanted:
            return {}
        results: Dict[str, Optional[Dict[str, Any]]] = {sid: None for sid in session_ids}
        try:
            for file_path in self.sessions_dir.glob("*.json"):
                # Skip metadata files
                if file_path.name.startswith("_meta_"):
                    continue
                try:
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        continue
                    sid = data.get('session_id')
                    if sid is not None and sid in wanted:
                        name = data.get('metadata', {}).get('name', 'Untitled Session')
                        updated_at = data.get('updated_at')
                        user_history = data.get('user_history', [])
                        message_count = len(user_history) if isinstance(user_history, list) else 0
                        results[sid] = {
                            'session_id': sid,
                            'name': name,
                            'updated_at': updated_at,
                            'message_count': message_count,
                        }
                except Exception:
                    continue
                # Early exit: all wanted sessions found
                if all(v is not None for v in results.values()):
                    break
            return results
        except Exception as e:
            logger.error(f"[SessionStore] Error in batch metadata load: {e}")
            return results

    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        List all saved sessions with basic metadata.
        Uses an in-memory cache (TTL: 5s) to avoid re-reading all files on every call.
        Skips files that are not valid session objects (e.g. open_sessions.json).
        """
        now = time.time()
        if self._cached_list is not None:
            ts, cached = self._cached_list
            if now - ts < self._cache_ttl:
                return cached
        sessions = []
        seen_ids = set()

        # Fast path: read lightweight metadata files
        for meta_path in sorted(self.sessions_dir.glob("_meta_*.json")):
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                if isinstance(meta, dict) and meta.get('session_id'):
                    sessions.append(meta)
                    seen_ids.add(meta['session_id'])
            except Exception:
                continue

        # Fallback for sessions without metadata files (migration period)
        for file_path in self.sessions_dir.glob("*.json"):
            # Skip metadata files and already-processed sessions
            if file_path.name.startswith("_meta_"):
                continue
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    logger.debug(f"[SessionStore] Skipping {file_path.name}: not a session object")
                    continue
                sid = data.get('session_id')
                if sid is None or sid in seen_ids:
                    continue
                session_info = {
                    'session_id': sid,
                    'name': data.get('metadata', {}).get('name', 'Untitled Session'),
                    'created_at': data.get('created_at'),
                    'updated_at': data.get('updated_at'),
                    'preview': self._extract_preview(data.get('user_history', [])),
                }
                sessions.append(session_info)
            except json.JSONDecodeError as e:
                logger.warning(f"[SessionStore] Corrupt session file {file_path.name}: {e}")
                continue
            except Exception as e:
                logger.error(f"[SessionStore] Error reading {file_path}: {e}")
                continue
        # Sort by updated_at descending (most recent first)
        sessions.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
        self._cached_list = (time.time(), sessions)
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
        # Invalidate caches
        self._cached_paths.pop(session_id, None)
        self._cached_paths_ts.pop(session_id, None)
        self._cached_list = None
        path = self._find_session_path(session_id)
        found = path is not None and path.exists()
        if found:
            path.unlink()
        # Also delete the metadata file if it exists
        meta_path = self._get_meta_path(session_id)
        if meta_path.exists():
            meta_path.unlink()
        return found

    def get_session_path(self, session_id: str) -> Path:
        """Get the file path for a given session ID."""
        path = self._find_session_path(session_id)
        if path is not None:
            return path
        # Session not saved yet, return the default path (for compatibility)
        return self._get_session_path(session_id)

    # ── Open sessions management ────────────────────────────────────────────

    def get_open_sessions_path(self) -> Path:
        """Get the path for the open_sessions.json state file."""
        return self.state_dir / 'open_sessions.json'

    def get_open_sessions(self) -> List[str]:
        """
        Read the list of open session IDs from open_sessions.json.
        Returns an empty list if the file does not exist or is corrupt.
        """
        path = self.get_open_sessions_path()
        if not path.exists():
            return []
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(sid) for sid in data]
            logger.warning(f"[SessionStore] open_sessions.json content is not a list: {type(data)}")
            return []
        except Exception as e:
            logger.error(f"[SessionStore] Error reading open_sessions.json: {e}")
            return []

    def save_open_sessions(self, session_ids: List[str]) -> None:
        """
        Write the list of open session IDs to open_sessions.json.
        """
        path = self.get_open_sessions_path()
        try:
            # Atomic write via temp file
            temp_path = path.with_suffix('.tmp')
            os.makedirs(os.path.dirname(temp_path), exist_ok=True)
            with open(temp_path, 'w') as f:
                json.dump(session_ids, f)
            temp_path.replace(path)
            logger.debug(f"[SessionStore] Saved {len(session_ids)} open sessions to {path}")
        except Exception as e:
            logger.error(f"[SessionStore] Error saving open_sessions.json: {e}")

    def add_open_session(self, session_id: str) -> None:
        """
        Add a session ID to the open sessions list (idempotent).
        """
        ids = self.get_open_sessions()
        if session_id not in ids:
            ids.append(session_id)
            self.save_open_sessions(ids)

    def remove_open_session(self, session_id: str) -> None:
        """
        Remove a session ID from the open sessions list.
        """
        ids = self.get_open_sessions()
        if session_id in ids:
            ids.remove(session_id)
            self.save_open_sessions(ids)

    def get_current_session_id(self) -> Optional[str]:
        """
        Get the ID of the current session from the marker file.
        Returns None if no marker exists.

        Migrates the marker from the old sessions_dir location to the
        new state_dir location on first access if needed.
        """
        marker = self.state_dir / ".current_session"
        logger.debug(f"[SessionStore] get_current_session_id: marker={marker}, exists={marker.exists()}")

        if marker.exists():
            try:
                content = marker.read_text().strip()
                logger.debug(f"[SessionStore] Marker content: '{content}'")
                return content if content else None
            except Exception as e:
                logger.error(f"[SessionStore] Error reading current session marker: {e}")
                return None

        # Migration: check old location in sessions_dir
        old_marker = self.sessions_dir / ".current_session"
        if old_marker.exists():
            try:
                content = old_marker.read_text().strip()
                if content:
                    logger.info(f"[SessionStore] Migrating .current_session from {old_marker} to {marker}")
                    # Atomic write to new location
                    temp_path = marker.with_suffix('.tmp')
                    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                    temp_path.write_text(content)
                    temp_path.replace(marker)
                    # Remove old marker
                    old_marker.unlink()
                    return content
            except Exception as e:
                logger.error(f"[SessionStore] Error migrating .current_session: {e}")

        return None

    def set_current_session_id(self, session_id: Optional[str]) -> None:
        """
        Set the current session ID by writing to the marker file.
        If session_id is None, the marker file is removed.
        """
        marker = self.state_dir / ".current_session"
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
                os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                temp_path.write_text(session_id)
                temp_path.replace(marker)
                logger.info(f"[SessionStore] Wrote marker file with session_id: {session_id}")
            except Exception as e:
                logger.error(f"[SessionStore] Error writing current session marker: {e}")
