"""
global_kb.py — Global Knowledge Base management.

Handles synchronisation of the global (user-wide) knowledge base at
``~/.thoughtmachine/global/`` with the packaged resource files shipped
with ThoughtMachine.

File structure:
    ~/.thoughtmachine/global/
        .version              — currently deployed version string
        system/               — read-only system files synced from package data
            (all .md files from resources/global_kb/)
        user/                 — writable user area
            my_notes.md
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

GLOBAL_KB_DIR = Path.home() / ".thoughtmachine" / "global"
SYSTEM_DIR = GLOBAL_KB_DIR / "system"
USER_DIR = GLOBAL_KB_DIR / "user"

# ── Resource helpers ──────────────────────────────────────────────────────────

_GLOBAL_KB_RESOURCES: Optional[Path] = None


def _global_kb_resources_dir() -> Path:
    """Return the path to the bundled ``resources/global_kb/`` directory.

    Works in both development (source tree) and installed (package data) modes.
    """
    global _GLOBAL_KB_RESOURCES
    if _GLOBAL_KB_RESOURCES is not None:
        return _GLOBAL_KB_RESOURCES

    import importlib.resources as pkg_resources

    try:
        # Resolve the thoughtmachine package location, then step up to
        # the project root where resources/ lives.
        pkg_path = pkg_resources.files("thoughtmachine")
        candidate = Path(str(pkg_path)).resolve().parent / "resources" / "global_kb"
        if candidate.is_dir():
            _GLOBAL_KB_RESOURCES = candidate
            return candidate
    except (ModuleNotFoundError, TypeError, OSError):
        pass

    # Fallback: try relative to this file (development layout)
    fallback = Path(__file__).resolve().parent.parent.parent / "resources" / "global_kb"
    if fallback.is_dir():
        _GLOBAL_KB_RESOURCES = fallback
        return fallback

    raise FileNotFoundError(
        "Could not locate resources/global_kb/ directory. "
        "Ensure ThoughtMachine is properly installed."
    )


def _read_resource_version() -> str:
    """Read the version string from the bundled ``.version`` file."""
    src_dir = _global_kb_resources_dir()
    return (src_dir / ".version").read_text(encoding="utf-8").strip()


# ── Public API ────────────────────────────────────────────────────────────────


def ensure_global_kb(version_file: Optional[Path] = None) -> list[str]:
    """Ensure the global knowledge base is populated and up-to-date.

    Reads the version from ``resources/global_kb/.version``, compares it with
    the stored version in ``~/.thoughtmachine/global/.version``, and copies
    system resource files (all .md files from ``resources/global_kb/``) if the
    version has changed or the system directory is missing.

    Also ensures the ``user/`` subdirectory exists and creates a placeholder
    ``my_notes.md`` file if one does not already exist.

    Args:
        version_file: Override path for the version marker (used in tests).
                      Defaults to ``GLOBAL_KB_DIR / ".version"``.

    Returns:
        A list of absolute paths to files that were created or overwritten.
    """
    touched: list[str] = []

    # ── Determine current & stored versions ──────────────────────────────
    current_version = _read_resource_version()
    version_marker = version_file or (GLOBAL_KB_DIR / ".version")
    stored_version: Optional[str] = None
    if version_marker.exists():
        try:
            stored_version = version_marker.read_text(encoding="utf-8").strip()
        except OSError:
            stored_version = None

    needs_sync = stored_version != current_version

    # ── Sync system/ subdirectory ────────────────────────────────────────
    if needs_sync or not SYSTEM_DIR.is_dir():
        SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
        src_dir = _global_kb_resources_dir()
        for resource_name in sorted(src_dir.glob("*.md")):
            dst = SYSTEM_DIR / resource_name.name
            try:
                shutil.copy2(str(resource_name), str(dst))
                touched.append(str(dst))
                logger.debug("Copied %s -> %s", resource_name.name, dst)
            except OSError as exc:
                logger.warning("Failed to copy %s: %s", resource_name.name, exc)

        # Remove stale files that no longer exist in the shipped resources
        existing_system_files = {f.name for f in SYSTEM_DIR.glob("*.md")}
        shipped_files = {f.name for f in src_dir.glob("*.md")}
        for stale_name in existing_system_files - shipped_files:
            stale_path = SYSTEM_DIR / stale_name
            try:
                stale_path.unlink()
                touched.append(str(stale_path))
                logger.debug("Removed stale %s", stale_path)
            except OSError as exc:
                logger.warning("Failed to remove stale %s: %s", stale_path, exc)

        # Write the current version marker
        try:
            version_marker.parent.mkdir(parents=True, exist_ok=True)
            version_marker.write_text(current_version, encoding="utf-8")
            touched.append(str(version_marker))
        except OSError as exc:
            logger.warning("Failed to write version marker %s: %s", version_marker, exc)

    # ── Ensure user/ subdirectory ────────────────────────────────────────
    USER_DIR.mkdir(parents=True, exist_ok=True)

    my_notes = USER_DIR / "my_notes.md"
    if not my_notes.exists():
        template = (
            "# My Notes\n\n"
            "Personal notes, preferences, and reminders.\n"
            "This file is in the user area of the global knowledge base.\n\n"
            "## Notes\n"
            "(Add your notes here)\n"
        )
        try:
            my_notes.write_text(template, encoding="utf-8")
            touched.append(str(my_notes))
        except OSError as exc:
            logger.warning("Failed to create %s: %s", my_notes, exc)

    return touched


def get_global_kb_root() -> Path:
    """Return the global KB root directory, ensuring it is initialised."""
    ensure_global_kb()
    return GLOBAL_KB_DIR
