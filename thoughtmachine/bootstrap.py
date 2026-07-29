"""
bootstrap.py — First-run initialisation for ThoughtMachine.

Ensures that the user's ``~/.thoughtmachine/`` directory contains all
required defaults on first run (or after a clean setup).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

USER_DIR = Path.home() / ".thoughtmachine"

# ── Manifest ────────────────────────────────────────────────────────────────


def _load_manifest() -> dict:
    """Load the resource deployment manifest from ``resources/MANIFEST.json``.

    Returns the parsed manifest dict.
    """
    manifest_path = _resources_dir() / "MANIFEST.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Cannot load resource manifest at {manifest_path}: {exc}"
        ) from exc


def get_manifest() -> dict:
    """Public accessor for the resource deployment manifest.

    Returns the full manifest dict (files, directories, notes).
    Useful for introspection by the agent or UI.
    """
    return _load_manifest()


def _resolve_source(source_name: str) -> Path:
    """Resolve a manifest source name to an absolute path."""
    return _resources_dir() / source_name


def _resolve_dest(dest_name: str) -> Path:
    """Resolve a manifest dest name under the user directory."""
    return USER_DIR / dest_name

# ── Helpers ───────────────────────────────────────────────────────────────────


def _resources_dir() -> Path:
    """Return the absolute path to the project-level ``resources/`` directory.

    Works both during development (importlib.resources.files on a package
    that is a *namespace package* sibling of ``thoughtmachine/``) and after
    installation (package data shipped alongside ``thoughtmachine``).
    """
    # Use importlib.resources.files to locate our own package …
    import importlib.resources as pkg_resources

    # … then step up to the project root where resources/ lives.
    # If thoughtmachine is a top-level package, its parent is the project root.
    pkg_path = pkg_resources.files("thoughtmachine")
    # pkg_path might be a Traversable; resolve to a real Path.
    return Path(str(pkg_path)).resolve().parent / "resources"


def _read_default(resource_name: str) -> str:
    """Read raw text of a bundled default resource file."""
    src = _resources_dir() / resource_name
    return src.read_text(encoding="utf-8")


def _read_default_json(resource_name: str) -> dict | list:
    """Read and parse a bundled JSON default resource."""
    return json.loads(_read_default(resource_name))


# ── Public API ────────────────────────────────────────────────────────────────


def get_version() -> str:
    """Return the current ThoughtMachine version string from ``.version``."""
    return _read_default(".version").strip()


def ensure_user_defaults(overwrite_existing: bool = False) -> list[str]:
    """Create/reset the ``~/.thoughtmachine/`` user directory with defaults.

    Steps:
    1. Create the new vault compartment structure.
    2. For each bundled default resource, deploy it to the appropriate location.
    3. Copy factory defaults into system/factory_defaults.json.

    Args:
        overwrite_existing: If ``True``, overwrite user files even when they
            already exist (useful for factory-reset scenarios).

    Returns:
        A list of absolute paths to files that were **created** or
        **overwritten**.
    """
    # Step 1: Create the new vault structure (idempotent)
    from thoughtmachine.vault import ensure_vault_structure, vault_root
    created = ensure_vault_structure()

    # Step 2: Create legacy directories that still need to exist
    #          (worker_templates at vault root for backward compatibility)
    (vault_root() / "sessions").mkdir(parents=True, exist_ok=True)
    (vault_root() / "state").mkdir(parents=True, exist_ok=True)
    (vault_root() / "knowledge").mkdir(parents=True, exist_ok=True)
    (vault_root() / "worker_templates").mkdir(parents=True, exist_ok=True)

    # Step 3: Deploy individual files from manifest
    manifest = _load_manifest()
    for entry in manifest.get("files", []):
        if entry.get("internal"):
            continue  # skip internal files (e.g., .version)
        dst = _resolve_dest(entry["dest"])
        if dst.exists() and not overwrite_existing:
            continue
        src = _resolve_source(entry["source"])
        shutil.copy2(str(src), str(dst))
        created.append(str(dst))

    # Step 4: Deploy factory defaults to system/factory_defaults.json
    factory_src = _resources_dir() / "factory_defaults.json"
    factory_dst = vault_root() / "system" / "factory_defaults.json"
    if factory_src.exists() and (overwrite_existing or not factory_dst.exists()):
        shutil.copy2(str(factory_src), str(factory_dst))
        created.append(str(factory_dst))

    # Step 5: Deploy directories from manifest
    for entry in manifest.get("directories", []):
        src_dir = _resolve_source(entry["source"])
        dst_dir = _resolve_dest(entry["dest"])
        condition = entry.get("condition", "")

        if condition == "dest_empty" and any(dst_dir.iterdir()):
            continue

        if not src_dir.is_dir():
            continue

        dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in src_dir.iterdir():
            if src_file.is_file():
                shutil.copy2(str(src_file), str(dst_dir / src_file.name))
                created.append(str(dst_dir / src_file.name))

    # Step 6: Ensure the global knowledge base
    from agent.knowledge.global_kb import ensure_global_kb
    created.extend(ensure_global_kb())

    return created


def load_user_config() -> dict:
    """Load the user's ``agent_config.json``, falling back to defaults.

    Returns:
        A ``dict`` with all keys that the project-level defaults provide.
    """
    config_path = USER_DIR / "agent_config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _read_default_json("default_config.json")


