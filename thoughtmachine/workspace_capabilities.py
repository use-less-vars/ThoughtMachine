"""
workspace_capabilities.py — Workspace capability model and persistence.

Defines ``WorkspaceCapabilities``, a dataclass that restricts what an agent
session within a given workspace is allowed to do.  Capabilities are loaded
from ``~/.thoughtmachine/workspaces/{workspace_id}/capabilities.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Default user data directory ───────────────────────────────────────────────

USER_DIR = Path.home() / ".thoughtmachine"


# ══════════════════════════════════════════════════════════════════════════════
#  WorkspaceCapabilities
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class WorkspaceCapabilities:
    """
    Declares what a workspace is allowed to do.

    Fields
    ------
    allowed_tools:
        List of fully-qualified tool names that are enabled in this workspace.
        An empty list means **all** tools are allowed.
    blocked_tools:
        List of tool names that are explicitly forbidden (takes precedence
        over *allowed_tools*).
    allowed_providers:
        List of provider IDs (e.g. ``"openai"``, ``"anthropic"``) that may
        be used.  Empty means all configured providers are allowed.
    max_context_length:
        Maximum total context length (input + output) in tokens.
        0 means no restriction.
    max_conversation_turns:
        Maximum number of user-assistant turn pairs before the agent must
        summarise or stop.  0 means no restriction.
    allowed_file_extensions:
        Only these file extensions may be read or written by file tools.
        Empty means all extensions are allowed.
    max_file_size_bytes:
        Maximum size (in bytes) for file read/write operations.
        0 means no restriction.
    allow_network:
        Whether the workspace's agent may make outbound HTTP requests.
    allow_docker:
        Whether the workspace's agent may spawn Docker containers.
    allowed_workspace_dirs:
        List of directory paths (relative to the workspace root) that tools
        may access.  Empty / ['.'] means the whole workspace.
    extra:
        Arbitrary extra capability flags for future extensibility.
    """

    allowed_tools: List[str] = field(default_factory=list)
    blocked_tools: List[str] = field(default_factory=list)
    allowed_providers: List[str] = field(default_factory=list)
    max_context_length: int = 0
    max_conversation_turns: int = 0
    allowed_file_extensions: List[str] = field(default_factory=list)
    max_file_size_bytes: int = 0
    allow_network: bool = True
    allow_docker: bool = True
    allowed_workspace_dirs: List[str] = field(default_factory=lambda: ["."])
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "allowed_tools": list(self.allowed_tools),
            "blocked_tools": list(self.blocked_tools),
            "allowed_providers": list(self.allowed_providers),
            "max_context_length": self.max_context_length,
            "max_conversation_turns": self.max_conversation_turns,
            "allowed_file_extensions": list(self.allowed_file_extensions),
            "max_file_size_bytes": self.max_file_size_bytes,
            "allow_network": self.allow_network,
            "allow_docker": self.allow_docker,
            "allowed_workspace_dirs": list(self.allowed_workspace_dirs),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceCapabilities":
        """Reconstruct from a dictionary (missing keys get defaults)."""
        return cls(
            allowed_tools=data.get("allowed_tools", []),
            blocked_tools=data.get("blocked_tools", []),
            allowed_providers=data.get("allowed_providers", []),
            max_context_length=data.get("max_context_length", 0),
            max_conversation_turns=data.get("max_conversation_turns", 0),
            allowed_file_extensions=data.get("allowed_file_extensions", []),
            max_file_size_bytes=data.get("max_file_size_bytes", 0),
            allow_network=data.get("allow_network", True),
            allow_docker=data.get("allow_docker", True),
            allowed_workspace_dirs=data.get("allowed_workspace_dirs", ["."]),
            extra=data.get("extra", {}),
        )

    @classmethod
    def default(cls) -> "WorkspaceCapabilities":
        """Return a fully-permissive default capability set."""
        return cls()


# ── Disk I/O ──────────────────────────────────────────────────────────────────


def _workspace_dir(workspace_id: str) -> Path:
    """Return the filesystem path for a given workspace ID."""
    return USER_DIR / "workspaces" / workspace_id


def _capabilities_path(workspace_id: str) -> Path:
    """Return the path to the capabilities JSON file for a workspace."""
    return _workspace_dir(workspace_id) / "capabilities.json"


def load_workspace_capabilities(
    workspace_id: str,
) -> Optional[WorkspaceCapabilities]:
    """
    Load workspace capabilities from disk.

    Returns ``None`` if the workspace directory or capabilities file does not
    exist (callers should fall back to the unrestricted default).
    """
    path = _capabilities_path(workspace_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return WorkspaceCapabilities.from_dict(raw)
    except (json.JSONDecodeError, OSError) as exc:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to load capabilities for workspace %s: %s", workspace_id, exc
        )
        return None


def save_workspace_capabilities(
    workspace_id: str,
    capabilities: WorkspaceCapabilities,
) -> None:
    """Write workspace capabilities to disk (creates workspace dir if needed)."""
    path = _capabilities_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(capabilities.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )


def resolve_workspace_id(workspace_path: str) -> Optional[str]:
    """
    Resolve a workspace filesystem path to its workspace ID.

    Iterates over ``~/.thoughtmachine/workspaces/<id>/config.json`` files,
    compares the ``root`` field (normalised) to *workspace_path*, and returns
    the matching ID.  Returns ``None`` if no match is found.
    """
    base = USER_DIR / "workspaces"
    if not base.is_dir():
        return None

    normalised_input = os.path.abspath(workspace_path).replace("\\", "/").rstrip("/")

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        config_file = entry / "config.json"
        if not config_file.is_file():
            continue
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            root = data.get("root", "")
            if not root:
                continue
            normalised_root = os.path.abspath(root).replace("\\", "/").rstrip("/")
            if normalised_root == normalised_input:
                return entry.name
        except (json.JSONDecodeError, OSError):
            continue

    return None


def ensure_workspace_dirs(workspace_id: str) -> List[str]:
    """
    Bootstrap a workspace's subdirectory structure.

    Creates ``~/.thoughtmachine/workspaces/{workspace_id}/`` with standard
    subdirectories (``sessions/``, ``state/``, ``knowledge/``) and writes a
    default capabilities file if one does not already exist.

    Returns a list of created paths.
    """
    base = _workspace_dir(workspace_id)
    created: List[str] = []

    for subdir in ("", "sessions", "state", "knowledge"):
        target = base / subdir
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(str(target))

    # Write default capabilities if not present
    caps_path = base / "capabilities.json"
    if not caps_path.exists():
        caps_path.write_text(
            json.dumps(WorkspaceCapabilities.default().to_dict(), indent=2),
            encoding="utf-8",
        )
        created.append(str(caps_path))

    return created
