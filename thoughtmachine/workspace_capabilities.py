"""
workspace_capabilities.py — Workspace capability model and persistence.

Defines ``WorkspaceCapabilities``, a dataclass that restricts what an agent
session within a given workspace is allowed to do.  Capabilities are loaded
from ``~/.thoughtmachine/workspaces/{workspace_id}/capabilities.json``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Minimal validation for template workers (avoids circular import via agent.__init__)
_REQUIRED_WORKER_FIELDS = {"name", "description", "system_prompt", "tools", "permission_footprint"}


def _validate_worker_dict(data: dict) -> dict | None:
    """Validate a worker dict has all required fields.  Returns the dict or
    ``None`` if validation fails."""
    missing = _REQUIRED_WORKER_FIELDS - set(data.keys())
    if missing:
        return None
    if not isinstance(data.get("tools"), list):
        return None
    if not isinstance(data.get("permission_footprint"), dict):
        return None
    return data

# ── Default user data directory ───────────────────────────────────────────────

def _user_dir() -> Path:
    """Return the user data directory (lazily, so tests can patch ``Path.home``)."""
    return Path.home() / ".thoughtmachine"


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
    filesystem_write:
        Whether the workspace's agent may write to the filesystem.
    git_available:
        Whether git is available in this workspace.
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
    filesystem_write: bool = True
    git_available: bool = True
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
            "filesystem_write": self.filesystem_write,
            "git_available": self.git_available,
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
            filesystem_write=data.get("filesystem_write", True),
            git_available=data.get("git_available", True),
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
    return _user_dir() / "workspaces" / workspace_id


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
    base = _user_dir() / "workspaces"
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


def _resources_dir() -> Path:
    """Return the absolute path to the project-level ``resources/`` directory.

    Uses ``importlib.resources.files`` to locate the ``thoughtmachine`` package,
    then resolves ``../resources/`` relative to it.  This works both during
    development and after ``pip install`` (package data).
    """
    import importlib.resources as pkg_resources

    pkg_path = pkg_resources.files("thoughtmachine")
    return Path(str(pkg_path)).resolve().parent / "resources"


def ensure_workspace_dirs(workspace_id: str) -> List[str]:
    """
    Bootstrap a workspace's default files.

    Creates ``~/.thoughtmachine/workspaces/{workspace_id}/`` if it does not
    exist and idempotently creates the following default files **if they do
    not already exist**:

    * ``capabilities.json`` — fully permissive workspace capabilities
    * ``Dockerfile`` — copied from ``resources/default_dockerfile.txt``
    * ``domain_allowlist.json`` — empty JSON array ``[]``
    * ``workers.json`` — default template worker from worker_templates/ (default)
    * ``mcp_servers.json`` — empty JSON array ``[]``

    No subdirectories (e.g. ``sessions/``, ``state/``, ``knowledge/``) are
    created inside the workspace config directory.

    After bootstrapping, a safeguard logs warnings for any files or folders
    in the workspace directory that are not in the expected set, but does
    **not** delete them.

    Returns a list of created paths (directories and files).
    """
    base = _workspace_dir(workspace_id)
    created: List[str] = []

    # ── Base directory ───────────────────────────────────────────────────
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        created.append(str(base))

    # ── Default capabilities ─────────────────────────────────────────────
    caps_path = base / "capabilities.json"
    if not caps_path.exists():
        caps_path.write_text(
            json.dumps(WorkspaceCapabilities.default().to_dict(), indent=2),
            encoding="utf-8",
        )
        created.append(str(caps_path))

    # ── Dockerfile (copy from resources/default_dockerfile.txt) ───────────
    dockerfile_path = base / "Dockerfile"
    if not dockerfile_path.exists():
        resources_root = _resources_dir()
        src_dockerfile = resources_root / "default_dockerfile.txt"
        if src_dockerfile.exists():
            shutil.copy2(str(src_dockerfile), str(dockerfile_path))
            created.append(str(dockerfile_path))

    # ── domain_allowlist.json (empty array) ───────────────────────────────
    domain_allowlist_path = base / "domain_allowlist.json"
    if not domain_allowlist_path.exists():
        domain_allowlist_path.write_text("[]", encoding="utf-8")
        created.append(str(domain_allowlist_path))

    # ── workers.json (template workers) ────────────────────────────
    workers_path = base / "workers.json"
    if not workers_path.exists():
        workers_data = _build_default_workers()
        _atomic_write_json(workers_path, workers_data)
        created.append(str(workers_path))

    # ── mcp_servers.json (empty array) ────────────────────────────────────
    mcp_servers_path = base / "mcp_servers.json"
    if not mcp_servers_path.exists():
        mcp_servers_path.write_text("[]", encoding="utf-8")
        created.append(str(mcp_servers_path))

    # ── Safeguard: warn about unexpected items ────────────────────────────
    _safeguard_workspace_dir(base)

    return created


# ── Worker template helpers ───────────────────────────────────────────────────


def _load_template_workers() -> list[dict]:
    """
    Load template worker definitions from disk.

    Checks ``~/.thoughtmachine/worker_templates/`` first, then falls back to
    ``resources/worker_templates/``.  Each ``.json`` file is parsed, validated
    with ``WorkerDefinition.model_validate()``, and converted to a dict.  Invalid
    templates are logged as warnings and skipped.

    Returns:
        A list of dicts (valid template workers).
    """
    logger = logging.getLogger(__name__)

    # Determine which directory to read templates from
    user_template_dir = _user_dir() / "worker_templates"
    if user_template_dir.is_dir() and any(user_template_dir.iterdir()):
        template_dir = user_template_dir
    else:
        template_dir = _resources_dir() / "worker_templates"

    if not template_dir.is_dir():
        return []

    workers: list[dict] = []

    for fpath in sorted(template_dir.iterdir()):
        if fpath.suffix != ".json":
            continue
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            validated = _validate_worker_dict(raw)
            if validated is None:
                logger.warning(
                    "Skipping invalid worker template %s (missing required fields)",
                    fpath.name,
                )
                continue
            workers.append(validated)
        except Exception as exc:
            logger.warning(
                "Skipping invalid worker template %s: %s", fpath.name, exc
            )

    return workers


def _build_default_workers() -> list[dict]:
    """
    Build the default workers list for a freshly bootstrapped workspace.

    Loads the default template workers from worker_templates/.
    """
    result: list[dict] = []
    existing_names: set[str] = set()

    for template in _load_template_workers():
        if template["name"] not in existing_names:
            result.append(template)
            existing_names.add(template["name"])

    return result


def _atomic_write_json(path: Path, data: list) -> None:
    """
    Write *data* as pretty-printed JSON to *path*, atomically.

    Writes to a ``.tmp`` file first, then renames it over the target.
    """
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(str(tmp_path), str(path))


def _safeguard_workspace_dir(base: Path) -> None:
    """
    Log a warning for each item in *base* that is not in the allowed set.

    Allowed file names:
        capabilities.json, Dockerfile, domain_allowlist.json,
        workers.json, mcp_servers.json

    This is a read-only check — no items are deleted or moved.
    """
    allowed = {
        "capabilities.json",
        "config.json",
        "Dockerfile",
        "domain_allowlist.json",
        "workers.json",
        "workers",
        "mcp_servers.json",
        "workspace_identity.json",
    }
    if not base.is_dir():
        return
    for item in base.iterdir():
        if item.name not in allowed:
            logging.warning(
                "Unexpected item in workspace config dir (%s): %s",
                base,
                item.name,
            )
