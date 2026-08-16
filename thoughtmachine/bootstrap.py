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


def _resolve_source(source_name: str, base: str = "resources") -> Path:
    """Resolve a manifest source name to an absolute path.

    Args:
        source_name: File or directory name from the manifest.
        base: ``"resources"`` (default) resolves under ``resources/``;
            ``"repo_root"`` resolves under the project root (parent of
            ``resources/``) — used for files like ``requirements.txt``.
    """
    if base == "repo_root":
        return _resources_dir().parent / source_name
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
    1. Create the vault compartment structure via ``ensure_vault_structure()``.
    2. Deploy all spec files via ``ensure_vault_defaults()`` (factory defaults,
       provider config, system prompts, registry stubs, etc).
    2b. Seed the vault-managed resource-image build files — the authoritative
       runtime Dockerfile, git overlay Dockerfile, and pinned requirements
       (single source for both the executor image and the tm-resource-git
       resource image) via ``ensure_resource_build_files()`` (never
       overwritten).
    3. Deploy individual resource files from the manifest.
    4. Deploy directories from the manifest (e.g., worker_templates).
    5. Ensure the global knowledge base.

    Args:
        overwrite_existing: If ``True``, overwrite user files even when they
            already exist (useful for factory-reset scenarios).

    Returns:
        A list of absolute paths to files that were **created** or
        **overwritten**.
    """
    # Step 1: Create the vault structure (idempotent)
    from thoughtmachine.vault import (
        ensure_vault_structure,
        ensure_vault_defaults,
        ensure_resource_build_files,
        vault_root,
    )
    created = ensure_vault_structure()

    # Step 2: Deploy all spec files via vault defaults
    created.extend(ensure_vault_defaults(_resources_dir(), overwrite_existing))

    # Step 2b: Seed the vault-managed resource-image build files — the
    # authoritative runtime Dockerfile, git overlay Dockerfile, and pinned
    # requirements.txt (single source for both the executor image and the
    # tm-resource-git resource image). Hard-coded overwrite_existing=False:
    # existing vault copies are trust anchors and must never be replaced by
    # bootstrap.
    created.extend(
        ensure_resource_build_files(
            _resources_dir(),
            _resources_dir().parent,
            overwrite_existing=False,
        )
    )

    # Step 3: Deploy individual files from manifest
    manifest = _load_manifest()
    for entry in manifest.get("files", []):
        if entry.get("internal"):
            continue  # skip internal files (e.g., .version)
        dst = _resolve_dest(entry["dest"])
        # never_overwrite entries (e.g. the resource image Dockerfile) are
        # trust anchors: even a factory reset must not clobber them.
        may_overwrite = overwrite_existing and not entry.get("never_overwrite")
        if dst.exists() and not may_overwrite:
            continue
        src = _resolve_source(entry["source"], entry.get("base", "resources"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        created.append(str(dst))

    # Step 4: Deploy directories from manifest
    for entry in manifest.get("directories", []):
        src_dir = _resolve_source(entry["source"], entry.get("base", "resources"))
        dst_dir = _resolve_dest(entry["dest"])
        condition = entry.get("condition", "")

        if not src_dir.is_dir():
            continue

        # Create destination if needed, then check condition
        dst_dir.mkdir(parents=True, exist_ok=True)

        if condition == "dest_empty" and any(dst_dir.iterdir()):
            continue

        for src_file in src_dir.iterdir():
            if src_file.is_file():
                shutil.copy2(str(src_file), str(dst_dir / src_file.name))
                created.append(str(dst_dir / src_file.name))

    # Step 5: Ensure the global knowledge base
    from agent.knowledge.global_kb import ensure_global_kb
    created.extend(ensure_global_kb())

    return created


def load_user_config() -> dict:
    """Load the user's config from ``user/defaults.json``, falling back to bundled defaults.

    Returns:
        A ``dict`` with all keys that the project-level defaults provide.
    """
    config_path = USER_DIR / "user" / "defaults.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _read_default_json("default_config.json")


