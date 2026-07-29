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
    "credentials",
    "knowledge",
    "sessions",
    "state",
    "system",
    "worker_templates",
)


def ensure_vault_structure() -> list[str]:
    """Create the vault compartment structure under ``~/.thoughtmachine/``.

    Each subdirectory is created only if it does not already exist
    (idempotent).  Directories that already exist are left untouched.

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
            created.append(str(target))

    return created


# ── Factory defaults ────────────────────────────────────────────────────────

_FACTORY_DEFAULTS_RELPATH = "system/factory_defaults.json"
_CHECKSYSTEM_ALLOWLIST_RELPATH = "system/checksystem_allowlist.json"


def ensure_vault_defaults(
    resources_dir: Path,
    overwrite_existing: bool = False,
) -> list[str]:
    """Deploy factory-default resource files into the vault.

    Currently this copies ``resources/factory_defaults.json`` into
    ``~/.thoughtmachine/system/factory_defaults.json``.

    Args:
        resources_dir: Absolute path to the project-level ``resources/``
            directory.
        overwrite_existing: If ``True``, overwrite any existing file.
            Otherwise only deploy when the destination does not exist.

    Returns:
        A list of absolute paths to files that were written.
    """
    created: list[str] = []
    root = vault_root()

    # Ensure the system subdirectory exists
    (root / "system").mkdir(parents=True, exist_ok=True)

    # Copy factory_defaults.json
    src = resources_dir / "factory_defaults.json"
    dst = root / _FACTORY_DEFAULTS_RELPATH
    if src.exists() and (overwrite_existing or not dst.exists()):
        shutil.copy2(str(src), str(dst))
        created.append(str(dst))

    # Copy checksystem_allowlist.json
    src = resources_dir / "checksystem_allowlist.json"
    dst = root / _CHECKSYSTEM_ALLOWLIST_RELPATH
    if src.exists() and (overwrite_existing or not dst.exists()):
        shutil.copy2(str(src), str(dst))
        created.append(str(dst))

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
