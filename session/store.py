"""
Session Store: Persistence layer for Session objects.

Provides an abstract interface and a file-system based implementation
that stores each session as a JSON file in a configured directory.
"""
import json
import os
import re
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import logging
logger = logging.getLogger(__name__)

from .models import Session
from .lock import FileLock


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
    def save_session(self, session: Session, workspace_id: Optional[str] = None) -> None:
        """Save a session to storage."""
        pass

    @abstractmethod
    def load_session(self, session_id: str, workspace_id: Optional[str] = None) -> Optional[Session]:
        """Load a session by ID. Returns None if not found."""
        pass

    @abstractmethod
    def list_sessions(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all saved sessions with basic metadata.
        Returns a list of dicts with at least: session_id, name, created_at, updated_at.
        """
        pass

    @abstractmethod
    def delete_session(self, session_id: str, workspace_id: Optional[str] = None) -> bool:
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
        self._cache_ttl = 60.0  # seconds — session list changes infrequently

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

    @property
    def _base_dir(self) -> Path:
        """Root directory for workspace-scoped session storage."""
        return Path(os.path.expanduser("~")) / ".thoughtmachine"

    def _resolve_session_path(self, session_id: str, workspace_id: Optional[str] = None) -> Path:
        """Resolve the filesystem path for a session file.

        If workspace_id is given, saves to:
            ~/.thoughtmachine/workspaces/<ws_id>/sessions/<session_id>.json
        Otherwise uses the legacy sessions_dir:
            <sessions_dir>/<session_id>.json
        """
        if workspace_id:
            return self._base_dir / "workspaces" / workspace_id / "sessions" / f"{session_id}.json"
        return self.sessions_dir / f"{session_id}.json"

    def _get_session_path(self, session_id: str) -> Path:
        """Get the file path for a session ID (legacy)."""
        return self.sessions_dir / f"{session_id}.json"

    def _get_meta_path(self, session_id: str) -> Path:
        """Get the file path for a session's lightweight metadata file."""
        return self.sessions_dir / f"_meta_{session_id}.json"

    def _find_session_path(self, session_id: str) -> Optional[Path]:
        """Find the actual file path for a session ID by scanning JSON files.
        Uses an in-memory cache (TTL: 5s) that is invalidated on save/delete.

        Scans both the legacy sessions_dir and any workspace-scoped directories
        under ~/.thoughtmachine/workspaces/<ws_id>/sessions/.
        """
        # Check cache first (with TTL)
        if session_id in self._cached_paths:
            ts = self._cached_paths_ts.get(session_id, 0)
            if time.time() - ts < self._cache_ttl:
                return self._cached_paths[session_id]

        # Collect all candidate directories
        candidates = [self.sessions_dir]
        workspaces_root = self._base_dir / "workspaces"
        if workspaces_root.exists():
            for ws_dir in workspaces_root.iterdir():
                ws_sessions = ws_dir / "sessions"
                if ws_sessions.is_dir():
                    candidates.append(ws_sessions)

        # Scan files across all candidate directories
        for sess_dir in candidates:
            for file_path in sess_dir.glob("*.json"):
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

    def _save_session_metadata(self, session: Session, workspace_id: Optional[str] = None) -> None:
        """Write a lightweight metadata file for fast session listing.

        The metadata file contains only the fields needed for the sidebar,
        avoiding the need to parse the full session JSON (which can be
        hundreds of messages) just to build the session list.

        When workspace_id is given, the meta file is stored under:
            ~/.thoughtmachine/workspaces/<ws_id>/sessions/_meta_<session_id>.json
        Otherwise it uses the legacy sessions_dir.
        """
        if workspace_id:
            meta_dir = self._base_dir / "workspaces" / workspace_id / "sessions"
            meta_path = meta_dir / f"_meta_{session.session_id}.json"
        else:
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

    def save_session(self, session: Session, workspace_id: Optional[str] = None) -> None:
        """Save a session to a JSON file.

        If workspace_id is provided, saves under the workspace-scoped path:
            ~/.thoughtmachine/workspaces/<ws_id>/sessions/<name>_<short_id>.json
        Otherwise uses the legacy sessions_dir.

        Acquires an exclusive file lock to prevent concurrent writes.
        """
        # Resolve workspace_id from session if not explicitly passed
        if workspace_id is None and session.workspace_id:
            workspace_id = session.workspace_id

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

        # Determine the target directory and friendly filename
        if workspace_id:
            target_dir = self._base_dir / "workspaces" / workspace_id / "sessions"
            name = session.metadata.get('name', 'Untitled Session')
            filename = _generate_friendly_filename(session.session_id, name)
            new_path = target_dir / filename
            old_path_raw = target_dir / f"{session.session_id}.json"
        else:
            target_dir = self.sessions_dir
            new_path = self._get_friendly_path(session)
            old_path_raw = None

        # Find existing file (if any, for rename logic)
        old_path = self._find_session_path(session.session_id)

        # If there's an existing file and it's different from new_path, rename it
        if old_path is not None and old_path != new_path:
            logger.debug(f"[SessionStore] Renaming session file from {old_path} to {new_path}")
            if new_path.exists():
                logger.warning(f"[SessionStore] Target file {new_path} already exists, overwriting")
            # Ensure the destination directory exists before the move
            os.makedirs(os.path.dirname(str(new_path)), exist_ok=True)
            shutil.move(str(old_path), str(new_path))

        # Write the session data atomically via temp file
        temp_path = new_path.with_suffix('.tmp')
        logger.debug(f"[SessionStore] Writing to {temp_path} (atomic)")
        # Ensure the target directory exists BEFORE acquiring the file lock,
        # because FileLock also creates a .lock file in the same directory.
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        try:
            with FileLock(str(new_path)):
                with open(temp_path, 'w') as f:
                    json.dump(data, f, indent=2, default=str)
                temp_path.replace(new_path)
            logger.debug(f"[SessionStore] Session {session.session_id} saved to {new_path}")
            # Invalidate path cache so subsequent _find_session_path re-scans
            self._cached_paths.pop(session.session_id, None)
            self._cached_paths_ts.pop(session.session_id, None)
            # Write/update the lightweight metadata file for fast listing
            self._save_session_metadata(session, workspace_id=workspace_id)
        except Exception:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise

    def load_session(self, session_id: str, workspace_id: Optional[str] = None) -> Optional[Session]:
        """Load a session from a JSON file.

        If workspace_id is provided, the workspace-scoped path is checked first,
        falling back to the legacy sessions_dir.

        Acquires a file lock to prevent reading a partially-written file.
        """
        if workspace_id:
            # Try workspace-scoped path first
            ws_path = self._resolve_session_path(session_id, workspace_id)
            if ws_path.exists():
                path = ws_path
            else:
                # Fall back to finding by scanning (handles friendly filenames)
                path = self._find_session_path(session_id)
        else:
            path = self._find_session_path(session_id)
        if path is None or not path.exists():
            return None
        try:
            with FileLock(str(path)):
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

    @staticmethod
    def _extract_preview(user_history: List[Any], max_chars: int = 80) -> str:
        """Extract a short preview from the last user message."""
        if not isinstance(user_history, list) or not user_history:
            return ""
        for msg in reversed(user_history):
            if isinstance(msg, dict) and msg.get('role') == 'user':
                content = msg.get('content', '')
                if content:
                    # Take first line, truncate
                    first_line = content.split('\n')[0]
                    if len(first_line) > max_chars:
                        return first_line[:max_chars] + '…'
                    return first_line
        return ""

    def load_session_metadata(self, session_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Lightweight metadata load — reads the session JSON file directly and
        extracts only the fields needed for listing, avoiding the expensive
        Session.from_persistable_dict() deserialization (which creates thousands
        of Message objects).

        Uses a directory scan to find the session file in a single pass rather
        than calling _find_session_path (which also scans but for a different
        purpose), avoiding double-reads of large JSON files.

        If workspace_id is provided, scans only the workspace-scoped directory.

        Returns a dict with keys:
            session_id, name, updated_at, message_count, preview
        or None if the session file is not found.
        """
        target_dir = self._base_dir / "workspaces" / workspace_id / "sessions" if workspace_id else self.sessions_dir
        try:
            for file_path in target_dir.glob("*.json"):
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
                            'preview': self._extract_preview(user_history),
                        }
                except Exception:
                    continue
            return None
        except Exception as e:
            logger.error(f"[SessionStore] Error loading session metadata for {session_id}: {e}")
            return None

    def load_sessions_metadata_batch(self, session_ids: List[str], workspace_id: Optional[str] = None) -> Dict[str, Optional[Dict[str, Any]]]:
        """
        Batch metadata load — reads session JSON files across relevant directories
        and returns metadata for all requested session IDs.

        This is far more efficient than calling load_session_metadata() in a loop
        because each file is read only once, regardless of how many session IDs
        are requested.

        If workspace_id is provided, scans only the workspace-scoped directory.
        If workspace_id is None, scans the legacy sessions_dir AND all
        workspace-scoped directories (mirroring _find_session_path logic).

        Returns a dict mapping session_id -> metadata dict (or None if not found)
        with metadata keys: session_id, name, updated_at, message_count, preview
        """
        wanted = set(session_ids)
        if not wanted:
            return {}
        results: Dict[str, Optional[Dict[str, Any]]] = {sid: None for sid in session_ids}

        # Build list of candidate directories to scan
        if workspace_id:
            candidates = [self._base_dir / "workspaces" / workspace_id / "sessions"]
        else:
            candidates = [self.sessions_dir]
            workspaces_root = self._base_dir / "workspaces"
            if workspaces_root.exists():
                for ws_dir in workspaces_root.iterdir():
                    ws_sessions = ws_dir / "sessions"
                    if ws_sessions.is_dir():
                        candidates.append(ws_sessions)

        for target_dir in candidates:
            try:
                if not target_dir.exists():
                    continue
                for file_path in target_dir.glob("*.json"):
                    # Skip metadata files
                    if file_path.name.startswith("_meta_"):
                        continue
                    try:
                        with open(file_path, 'r') as f:
                            data = json.load(f)
                        if not isinstance(data, dict):
                            continue
                        sid = data.get('session_id')
                        if sid is not None and sid in wanted and results.get(sid) is None:
                            name = data.get('metadata', {}).get('name', 'Untitled Session')
                            updated_at = data.get('updated_at')
                            user_history = data.get('user_history', [])
                            message_count = len(user_history) if isinstance(user_history, list) else 0
                            results[sid] = {
                                'session_id': sid,
                                'name': name,
                                'updated_at': updated_at,
                                'message_count': message_count,
                                'preview': self._extract_preview(user_history),
                            }
                    except Exception:
                        continue
                    # Early exit: all wanted sessions found
                    if all(v is not None for v in results.values()):
                        return results
            except Exception as e:
                logger.warning(f"[SessionStore] Error scanning {target_dir} in batch metadata load: {e}")
                continue
        return results

    def list_sessions(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all saved sessions with basic metadata.

        If workspace_id is provided, only sessions in that workspace's directory
        (~/.thoughtmachine/workspaces/<ws_id>/sessions/) are returned.
        Otherwise, sessions from the legacy sessions_dir are returned.

        Uses an in-memory cache (TTL: 60s) to avoid re-reading all files on every call.
        Skips files that are not valid session objects (e.g. open_sessions.json).
        """
        now = time.time()
        # Only use cache for the default (no workspace_id) case
        if workspace_id is None and self._cached_list is not None:
            ts, cached = self._cached_list
            if now - ts < self._cache_ttl:
                return cached

        if workspace_id:
            target_dir = self._base_dir / "workspaces" / workspace_id / "sessions"
        else:
            target_dir = self.sessions_dir

        sessions = []
        seen_ids = set()

        # Fast path: read lightweight metadata files
        for meta_path in sorted(target_dir.glob("_meta_*.json")):
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                if isinstance(meta, dict) and meta.get('session_id'):
                    sessions.append(meta)
                    seen_ids.add(meta['session_id'])
            except Exception:
                continue

        # Fallback for sessions without metadata files (migration period)
        for file_path in target_dir.glob("*.json"):
            # Skip metadata files and already-processed sessions
            if file_path.name.startswith("_meta_"):
                continue
            # Fast path: try to extract metadata from ~8 KB head (avoids full JSON parse)
            session_info = self._fast_extract_metadata(file_path)
            if session_info and session_info.get('session_id') not in seen_ids:
                sid = session_info['session_id']
                seen_ids.add(sid)
                sessions.append(session_info)
                self._write_meta_file(session_info)
                continue
            # Fallback: full file read (should be rare after migration)
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
                    'preview': '',  # Intentionally empty — extracting preview adds no value vs. 52s delay
                }
                seen_ids.add(sid)
                sessions.append(session_info)
                self._write_meta_file(session_info)
            except json.JSONDecodeError as e:
                logger.warning(f"[SessionStore] Corrupt session file {file_path.name}: {e}")
                continue
            except Exception as e:
                logger.error(f"[SessionStore] Error reading {file_path}: {e}")
                continue
        # Sort by updated_at descending (most recent first)
        sessions.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
        if workspace_id is None:
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

    def _fast_extract_metadata(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """
        Extract session metadata without parsing the full JSON tree.

        Session JSON files contain a ``user_history`` array that can hold
        hundreds of messages.  Full ``json.load()`` of such files is the
        primary cause of 50+ second block.  This method reads the entire
        file as a string (fast — just bytes) but only calls
        ``json.JSONDecoder.raw_decode()`` on individual field values,
        never on the full ``user_history`` array.

        Returns a metadata dict with keys: session_id, name, created_at,
        updated_at, preview.  Returns None if extraction fails (caller
        falls back to the full-read path).
        """
        try:
            # Read full file as string (fast I/O, no object creation)
            with open(file_path, 'r') as f:
                content = f.read()

            decoder = json.JSONDecoder()

            # ── session_id ────────────────────────────────────────────────
            sid = self._json_decode_at_key(content, 'session_id', decoder)
            if not sid or not isinstance(sid, str):
                return None

            # ── Timestamps ────────────────────────────────────────────────
            created_at = self._json_decode_at_key(content, 'created_at', decoder)
            updated_at = self._json_decode_at_key(content, 'updated_at', decoder)
            if not isinstance(created_at, str):
                created_at = None
            if not isinstance(updated_at, str):
                updated_at = None

            # ── metadata.name ─────────────────────────────────────────────
            name = 'Untitled Session'
            meta_idx = content.find('"metadata"')
            if meta_idx >= 0:
                # Find the '{' that starts the metadata dict
                val_start = content.find('{', meta_idx)
                if val_start >= 0:
                    try:
                        meta_obj, _ = decoder.raw_decode(content, val_start)
                        if isinstance(meta_obj, dict):
                            raw_name = meta_obj.get('name')
                            if raw_name and isinstance(raw_name, str):
                                name = raw_name
                    except json.JSONDecodeError:
                        pass

            return {
                'session_id': sid,
                'name': name,
                'created_at': created_at,
                'updated_at': updated_at,
                'preview': '',
            }
        except Exception:
            return None

    @staticmethod
    def _json_decode_at_key(content: str, key: str, decoder: json.JSONDecoder) -> Any:
        """Find *key* in *content* and decode the JSON value after it."""
        idx = content.find(f'"{key}"')
        if idx < 0:
            return None
        colon = content.find(':', idx)
        if colon < 0:
            return None
        try:
            val, _ = decoder.raw_decode(content, colon + 1)
            return val
        except json.JSONDecodeError:
            return None

    def _regex_extract_metadata(self, text: str) -> Optional[Dict[str, Any]]:
        """Fallback: extract key fields via regex when ``_fast_extract_metadata`` tail-decodes fail."""
        sid_match = re.search(r'"session_id"\s*:\s*"([^"]+)"', text)
        if not sid_match:
            return None
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', text)
        name = name_match.group(1) if name_match else 'Untitled Session'
        ca_match = re.search(r'"created_at"\s*:\s*"([^"]+)"', text)
        ua_match = re.search(r'"updated_at"\s*:\s*"([^"]+)"', text)
        return {
            'session_id': sid_match.group(1),
            'name': name,
            'created_at': ca_match.group(1) if ca_match else None,
            'updated_at': ua_match.group(1) if ua_match else None,
            'preview': '',
        }

    def _write_meta_file(self, session_info: Dict[str, Any]) -> None:
        """Write a lightweight _meta_ file so the fast path can use it next time."""
        sid = session_info.get('session_id')
        if not sid:
            return
        meta_path = self._get_meta_path(sid)
        try:
            temp_path = meta_path.with_suffix('.tmp')
            os.makedirs(os.path.dirname(temp_path), exist_ok=True)
            with open(temp_path, 'w') as f:
                json.dump(session_info, f)
            temp_path.replace(meta_path)
        except Exception:
            # Non-critical — will retry on next list_sessions() call
            if temp_path.exists():
                temp_path.unlink()

    # ── Singleton access ─────────────────────────────────────────────────────

    _instance: Optional['FileSystemSessionStore'] = None

    @classmethod
    def get_instance(cls) -> 'FileSystemSessionStore':
        """Return the singleton FileSystemSessionStore instance.

        Creates one on first call using default settings.  All WebSocket
        connections and bridges share one store so that the in-memory
        ``list_sessions()`` cache is coherent across concurrent connections.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def delete_session(self, session_id: str, workspace_id: Optional[str] = None) -> bool:
        """Delete a session file.

        If workspace_id is provided, also deletes the workspace-scoped meta file.
        """
        # Invalidate caches
        self._cached_paths.pop(session_id, None)
        self._cached_paths_ts.pop(session_id, None)
        self._cached_list = None
        path = self._find_session_path(session_id)
        found = path is not None and path.exists()
        if found:
            path.unlink()
        # Delete metadata file(s) — try both legacy and workspace-scoped
        meta_path = self._get_meta_path(session_id)
        if meta_path.exists():
            meta_path.unlink()
        if workspace_id:
            ws_meta_path = self._base_dir / "workspaces" / workspace_id / "sessions" / f"_meta_{session_id}.json"
            if ws_meta_path.exists():
                ws_meta_path.unlink()
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
