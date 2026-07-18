"""
workspace_registry.py — Persistent workspace registry.

Manages a JSON file at ``~/.thoughtmachine/workspace_registry.json`` that
tracks all known workspaces.  This replaces ad-hoc directory scanning with
an explicit registry.

Public API
----------
- ``WorkspaceRegistryEntry`` — dataclass for a single workspace entry.
- ``WorkspaceRegistry`` — manages the JSON registry file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

def generate_human_id() -> str:
    """Generate a short, human-readable workspace ID using random words + digits.

    Format: ``{adjective}-{noun}-{digits}`` where adjective and noun are drawn
    from small built-in word lists.  The result is ~8--12 chars.
    Returns a string like ``'blue-fox-42'`` or ``'swift-eagle-91'``.
    """
    import random

    adjectives = [
        "bright", "calm", "dark", "eager", "fierce", "gentle", "happy", "keen",
        "lively", "mellow", "noble", "proud", "quick", "sharp", "swift", "warm",
        "brave", "clear", "crisp", "divine", "eagle", "fancy", "golden", "holy",
        "jolly", "kindly", "lithe", "merry", "neat", "quiet", "royal", "shiny",
    ]
    nouns = [
        "bear", "bird", "cat", "deer", "eagle", "frog", "goat", "hare", "ibis",
        "jay", "koala", "lion", "moth", "newt", "owl", "panda", "quail", "rook",
        "seal", "toad", "urus", "vole", "wolf", "yak", "zebra", "crane", "dove",
        "finch", "gecko", "heron", "impala", "jaguar", "kraken", "lemur", "moose",
    ]
    adj = random.choice(adjectives)
    noun = random.choice(nouns)
    num = random.randint(10, 99)
    return f"{adj}-{noun}-{num}"




# ── Helpers ─────────────────────────────────────────────────────────────


def _user_dir() -> Path:
    """Return the user data directory (lazily, so tests can patch ``Path.home``)."""
    return Path.home() / ".thoughtmachine"


def _registry_path() -> Path:
    """Return the path to the workspace registry JSON file."""
    return _user_dir() / "workspace_registry.json"


def _now_iso() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ── Entry dataclass ─────────────────────────────────────────────────────


@dataclass
class WorkspaceRegistryEntry:
    """A single workspace entry in the registry.

    Fields
    ------
    id:
        Unique workspace identifier.
    root_path:
        Absolute filesystem path to the workspace root.
    label:
        Human-readable label (optional).
    created_at:
        ISO 8601 timestamp of when the workspace was registered.
    updated_at:
        ISO 8601 timestamp of the last update.
    last_opened:
        ISO 8601 timestamp of the last time the workspace was opened/used.
    metadata:
        Arbitrary extra key-value data.
    """

    id: str
    root_path: str
    label: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_opened: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "id": self.id,
            "root_path": self.root_path,
            "label": self.label,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_opened": self.last_opened,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceRegistryEntry":
        """Reconstruct from a dictionary (missing keys get defaults)."""
        return cls(
            id=data.get("id", ""),
            root_path=data.get("root_path", ""),
            label=data.get("label", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_opened=data.get("last_opened", ""),
            metadata=data.get("metadata", {}),
        )

    def __repr__(self) -> str:
        return (
            f"WorkspaceRegistryEntry(id={self.id!r}, root_path={self.root_path!r}, "
            f"label={self.label!r})"
        )


# ── Registry ────────────────────────────────────────────────────────────

_VALID_UPDATE_FIELDS = {"root_path", "label", "last_opened", "metadata"}


class WorkspaceRegistry:
    """Persistent workspace registry backed by a JSON file.

    Thread-safe: all public methods acquire an internal lock.
    """

    _instances: Dict[str, "WorkspaceRegistry"] = {}

    def __init__(self, path: Optional[Path] = None) -> None:
        """Create or open a registry at *path*.

        If *path* is ``None``, the default ``~/.thoughtmachine/workspace_registry.json``
        is used.
        """
        self._path = path or _registry_path()
        self._lock = threading.Lock()

    # ── Singleton / default instance ────────────────────────────────

    @classmethod
    def get_default(cls) -> "WorkspaceRegistry":
        """Return a cached default instance for the standard registry path."""
        path = str(_registry_path())
        if path not in cls._instances:
            cls._instances[path] = cls(Path(path))
        return cls._instances[path]

    # ── Internal I/O ────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        """Load the raw registry data from disk.

        Returns an empty dict if the file does not exist or is corrupt
        (a warning is logged for corrupt files).
        """
        if not self._path.exists():
            return {}

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning(
                    "Registry file %s is not a JSON object; resetting.",
                    self._path,
                )
                return {}
            return raw
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load registry %s: %s; resetting.",
                self._path,
                exc,
            )
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        """Atomically write *data* to the registry JSON file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(self._path))

    # ── Public API ──────────────────────────────────────────────────

    def list_workspaces(self) -> List[WorkspaceRegistryEntry]:
        """Return all registered workspaces, sorted by label then id."""
        with self._lock:
            raw = self._load()
            entries = [
                WorkspaceRegistryEntry.from_dict(v)
                for v in raw.values()
                if isinstance(v, dict)
            ]
            entries.sort(key=lambda e: (e.label or "", e.id or ""))
            return entries

    def get_workspace(self, workspace_id: str) -> Optional[WorkspaceRegistryEntry]:
        """Return the entry for *workspace_id*, or ``None``."""
        with self._lock:
            raw = self._load()
            raw_entry = raw.get(workspace_id)
            if raw_entry is None or not isinstance(raw_entry, dict):
                return None
            return WorkspaceRegistryEntry.from_dict(raw_entry)

    def register_workspace(
        self,
        workspace_id: str,
        root_path: str,
        label: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WorkspaceRegistryEntry:
        """Register a new workspace.

        Raises ``ValueError`` if *workspace_id* already exists.
        """
        with self._lock:
            raw = self._load()
            if workspace_id in raw:
                raise ValueError(
                    f"Workspace {workspace_id!r} is already registered."
                )

            now = _now_iso()
            entry = WorkspaceRegistryEntry(
                id=workspace_id,
                root_path=os.path.abspath(root_path),
                label=label,
                created_at=now,
                updated_at=now,
                metadata=metadata or {},
            )
            raw[workspace_id] = entry.to_dict()
            self._save(raw)
            return entry

    def register_by_root(
        self,
        root_path: str,
        label: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WorkspaceRegistryEntry:
        """Register a workspace by its root path, auto-generating a human-readable ID.

        If a workspace already exists for this root path, returns the existing entry
        (no-op).  Otherwise, generates a new ID via :func:`generate_human_id` and
        registers it.

        Args:
            root_path: Absolute or relative filesystem path to the workspace root.
            label: Optional human-readable label.
            metadata: Optional dict of arbitrary key-value data.

        Returns:
            The existing or newly created :class:`WorkspaceRegistryEntry`.
        """
        existing = self.resolve_by_root(root_path)
        if existing is not None:
            return existing
        ws_id = generate_human_id()
        # Ensure uniqueness in the unlikely event of a collision
        while self.get_workspace(ws_id) is not None:
            ws_id = generate_human_id()
        return self.register_workspace(ws_id, root_path, label=label, metadata=metadata)

    def unregister_workspace(self, workspace_id: str) -> bool:
        """Remove a workspace from the registry.

        Returns ``True`` if the entry was removed, ``False`` if it did not exist.
        """
        with self._lock:
            raw = self._load()
            if workspace_id not in raw:
                return False
            del raw[workspace_id]
            self._save(raw)
            return True

    def update_workspace(
        self,
        workspace_id: str,
        **updates: Any,
    ) -> Optional[WorkspaceRegistryEntry]:
        """Update fields on an existing workspace entry.

        Accepts only valid field names: ``root_path``, ``label``, ``last_opened``,
        ``metadata``.

        Returns the updated entry, or ``None`` if the workspace does not exist.
        """
        invalid = set(updates) - _VALID_UPDATE_FIELDS
        if invalid:
            raise ValueError(
                f"Invalid update fields: {', '.join(sorted(invalid))}. "
                f"Allowed: {', '.join(sorted(_VALID_UPDATE_FIELDS))}."
            )

        with self._lock:
            raw = self._load()
            raw_entry = raw.get(workspace_id)
            if raw_entry is None or not isinstance(raw_entry, dict):
                return None

            entry = WorkspaceRegistryEntry.from_dict(raw_entry)

            for key, value in updates.items():
                if key == "root_path":
                    entry.root_path = os.path.abspath(str(value))
                elif key == "label":
                    entry.label = str(value)
                elif key == "last_opened":
                    entry.last_opened = str(value)
                elif key == "metadata":
                    if not isinstance(value, dict):
                        raise TypeError("metadata must be a dict")
                    entry.metadata = value

            entry.updated_at = _now_iso()
            raw[workspace_id] = entry.to_dict()
            self._save(raw)
            return entry

    def resolve_by_root(self, root_path: str) -> Optional[WorkspaceRegistryEntry]:
        """Resolve a filesystem path to a workspace entry.

        The *root_path* is normalised via ``os.path.abspath`` before matching.
        Returns the first matching entry, or ``None``.
        """
        normalised = os.path.abspath(root_path).replace("\\", "/").rstrip("/")
        with self._lock:
            raw = self._load()
            for raw_entry in raw.values():
                if not isinstance(raw_entry, dict):
                    continue
                entry_root = os.path.abspath(
                    raw_entry.get("root_path", "")
                ).replace("\\", "/").rstrip("/")
                if entry_root == normalised:
                    return WorkspaceRegistryEntry.from_dict(raw_entry)
            return None
