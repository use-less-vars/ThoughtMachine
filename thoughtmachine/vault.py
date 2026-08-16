"""
vault.py — User vault management for ThoughtMachine.

Provides the directory layout for ``~/.thoughtmachine/`` and factory-defaults
bootstrap logic.

The vault is the user-side counterpart of the project-level ``resources/``
directory.  On first run (or factory reset) the bootstrap module calls
:func:`ensure_vault_structure` to create the compartment hierarchy, then
:func:`ensure_vault_defaults` to populate it from bundled resources.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)


# ── Paths ──────────────────────────────────────────────────────────────────


def vault_root() -> Path:
    """Return the absolute path to the user's ThoughtMachine vault directory.

    Typically ``~/.thoughtmachine/`` on Linux/macOS or the equivalent
    on Windows.
    """
    return Path.home() / ".thoughtmachine"


# ── Vault structure ─────────────────────────────────────────────────────────

# Sub-directories that form the vault compartment hierarchy.
VAULT_SUBDIRS: tuple[str, ...] = (
    "system",
    "user",
    "credentials",
    "workspaces",
    "global",
    "state",
    "logs",
)


def ensure_vault_structure() -> list[str]:
    """Create the vault compartment structure under ``~/.thoughtmachine/``.

    Each subdirectory is created only if it does not already exist
    (idempotent).  Newly created directories get permissions ``0o700``
    (owner-only access).

    Returns:
        A list of absolute paths to directories that were **created**
        during this call.
    """
    root = vault_root()
    created: list[str] = []

    for subdir in VAULT_SUBDIRS:
        target = root / subdir
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o700)
            created.append(str(target))

    return created


# ── Factory defaults ────────────────────────────────────────────────────────

_FACTORY_DEFAULTS_RELPATH = "system/factory_defaults.json"
_CHECKSYSTEM_ALLOWLIST_RELPATH = "system/checksystem_allowlist.json"


def _copy_resource(
    src: Path,
    dst: Path,
    overwrite_existing: bool,
    created: list[str],
) -> None:
    """Copy *src* to *dst* if conditions are met and set 0o644 permissions."""
    if src.exists() and (overwrite_existing or not dst.exists()):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        dst.chmod(0o644)
        created.append(str(dst))


def _write_file(
    content: str,
    dst: Path,
    overwrite_existing: bool,
    created: list[str],
) -> None:
    """Write *content* to *dst* if conditions are met and set 0o644 permissions."""
    if overwrite_existing or not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        dst.chmod(0o644)
        created.append(str(dst))


def ensure_vault_defaults(
    resources_dir: Path,
    overwrite_existing: bool = False,
) -> list[str]:
    """Deploy factory-default resource files into the vault.

    Copies / creates the following files:

    * ``system/factory_defaults.json``          — from ``resources/factory_defaults.json``
    * ``system/checksystem_allowlist.json``     — from ``resources/checksystem_allowlist.json``
    * ``system/providers.json``                 — from ``resources/default_providers.json``
    * ``system/default_system_prompt.txt``      — from ``resources/default_system_prompt.txt``
    * ``system/engineer_system_prompt.txt``     — from ``resources/engineer_system_prompt.txt``
    * ``system/.vault_version``                 — from ``get_version()``
    * ``state/session_registry.json``           — written as ``[]``
    * ``state/workspace_registry.json``         — written as ``[]``

    Args:
        resources_dir: Absolute path to the project-level ``resources/``
            directory.
        overwrite_existing: If ``True``, overwrite any existing file.
            Otherwise only deploy when the destination does not exist.

    Returns:
        A list of absolute paths to files that were written.
    """
    from thoughtmachine.bootstrap import get_version

    created: list[str] = []
    root = vault_root()

    # 1–2. Copy factory_defaults.json and checksystem_allowlist.json
    _copy_resource(
        resources_dir / "factory_defaults.json",
        root / _FACTORY_DEFAULTS_RELPATH,
        overwrite_existing, created,
    )
    _copy_resource(
        resources_dir / "checksystem_allowlist.json",
        root / _CHECKSYSTEM_ALLOWLIST_RELPATH,
        overwrite_existing, created,
    )

    # 3. Copy providers.json (from default_providers.json)
    _copy_resource(
        resources_dir / "default_providers.json",
        root / "system" / "providers.json",
        overwrite_existing, created,
    )

    # 4–5. Copy system prompt files
    _copy_resource(
        resources_dir / "default_system_prompt.txt",
        root / "system" / "default_system_prompt.txt",
        overwrite_existing, created,
    )
    _copy_resource(
        resources_dir / "engineer_system_prompt.txt",
        root / "system" / "engineer_system_prompt.txt",
        overwrite_existing, created,
    )

    # 6. Write .vault_version marker
    _write_file(
        get_version(),
        root / "system" / ".vault_version",
        overwrite_existing, created,
    )

    # 7–8. Write empty state registry files
    _write_file(
        "[]\n",
        root / "state" / "session_registry.json",
        overwrite_existing, created,
    )
    _write_file(
        "[]\n",
        root / "state" / "workspace_registry.json",
        overwrite_existing, created,
    )

    return created


_RESOURCE_DOCKERFILE_RELPATH = "docker/resource/Dockerfile"


def ensure_resource_dockerfile(
    resources_dir: Path,
    overwrite_existing: bool = False,
) -> list[str]:
    """Seed the vault-managed runtime Dockerfile.

    Copies ``resources/default_dockerfile.txt`` — the repo's UNIFIED runtime
    Dockerfile, the single source for BOTH the executor image and the
    ``tm-resource-git`` resource image — to
    ``~/.thoughtmachine/docker/resource/Dockerfile``. The vault copy is the
    manual-build fallback only: auto-builds read the repo file directly, and
    the manual command is ``docker build -t tm-resource-git -f
    resources/default_dockerfile.txt .``. Seeding a copy into the vault —
    which is agent-write-blocked — keeps a fallback image definition out of
    agent reach.

    Callers MUST pass ``overwrite_existing=False`` (the default): an existing
    vault copy is a trust anchor and must never be replaced by bootstrap.

    Args:
        resources_dir: Absolute path to the project-level ``resources/``
            directory.
        overwrite_existing: If ``True``, overwrite an existing vault copy.
            Keep ``False`` (the default) — see docstring above.

    Returns:
        A list of absolute paths to files that were written.
    """
    created: list[str] = []
    _copy_resource(
        resources_dir / "default_dockerfile.txt",
        vault_root() / _RESOURCE_DOCKERFILE_RELPATH,
        overwrite_existing, created,
    )
    return created


def load_factory_defaults() -> dict:
    """Load and return the factory defaults from the vault.

    Returns an empty dict if the file does not exist or is unreadable.
    """
    path = vault_root() / _FACTORY_DEFAULTS_RELPATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# ── CheckSystem allowlist ──────────────────────────────────────────────────


def verify_allowlist_integrity(allowlist_path: Path) -> tuple[bool, str]:
    """Verify the SHA-256 integrity of an allowlist file.

    Reads the file, extracts the 'allowlist' array and the stored 'sha256'
    hash, computes SHA-256 of the sorted, joined allowlist entries,
    and compares.

    Args:
        allowlist_path: Path to the allowlist JSON file.

    Returns:
        A tuple of (is_valid: bool, message: str).
        If valid: (True, "Integrity check passed").
        If invalid: (False, description of the issue).
    """
    try:
        data = json.loads(allowlist_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        return False, f"Cannot read allowlist: {exc}"

    if not isinstance(data, dict):
        return False, "Allowlist must be a JSON object"

    allowlist = data.get("allowlist")
    if not isinstance(allowlist, list):
        return False, "'allowlist' must be a JSON array"

    stored_hash = data.get("sha256", "")
    if not isinstance(stored_hash, str) or len(stored_hash) != 64:
        return False, "'sha256' must be a 64-character hex string"

    # Compute hash over sorted, joined entries (no wildcards allowed)
    sorted_entries = sorted(str(e) for e in allowlist)
    canonical = "\n".join(sorted_entries)
    computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    if computed_hash != stored_hash:
        return (
            False,
            f"Integrity mismatch: computed {computed_hash}, stored {stored_hash}",
        )

    return True, "Integrity check passed"


_checksystem_allowlist_cache: tuple[list[str], float] | None = None
"""Module-level cache: (allowlist entries, mtime of file at load time)."""


def get_checksystem_allowlist() -> list[str]:
    """Load and return the CheckSystem allowlist from the vault.

    Reads ``~/.thoughtmachine/system/checksystem_allowlist.json``,
    verifies integrity via SHA-256, and returns the list of allowed
    filenames.  Results are cached based on file mtime.

    Returns:
        A list of explicit filenames (no wildcards).
        Returns an empty list if the file is missing, unreadable,
        or fails integrity check.
    """
    import time

    path = vault_root() / "system" / "checksystem_allowlist.json"
    global _checksystem_allowlist_cache

    # Check cache freshness
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        # File doesn't exist or can't be stat'd
        _checksystem_allowlist_cache = None
        return []

    if _checksystem_allowlist_cache is not None:
        cached_entries, cached_mtime = _checksystem_allowlist_cache
        if cached_mtime == current_mtime:
            return cached_entries

    # Load and verify
    is_valid, msg = verify_allowlist_integrity(path)
    if not is_valid:
        logger.warning("CheckSystem allowlist integrity check failed: %s", msg)
        _checksystem_allowlist_cache = ([], current_mtime)
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries: list[str] = list(str(e) for e in data.get("allowlist", []))
        # Validate: no wildcards (Amendment 3)
        for entry in entries:
            if "*" in entry or "?" in entry or "[" in entry:
                logger.warning(
                    "Allowlist entry '%s' contains wildcard — rejecting entire allowlist",
                    entry,
                )
                _checksystem_allowlist_cache = ([], current_mtime)
                return []
        _checksystem_allowlist_cache = (entries, current_mtime)
        return entries
    except Exception as exc:
        logger.error("Failed to load allowlist: %s", exc)
        _checksystem_allowlist_cache = ([], current_mtime)
        return []


def is_path_allowed(filename: str) -> bool:
    """Check if a filename is in the CheckSystem allowlist.

    Args:
        filename: A bare filename (not a full path) to check.

    Returns:
        True if the filename is in the allowlist.
    """
    allowlist = get_checksystem_allowlist()
    return filename in allowlist
