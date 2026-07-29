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

import json
import shutil
from pathlib import Path


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
