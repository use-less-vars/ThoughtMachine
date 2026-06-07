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
from typing import Optional

from agent.knowledge.global_kb import ensure_global_kb

# ── Paths ─────────────────────────────────────────────────────────────────────

USER_DIR = Path.home() / ".thoughtmachine"

RESOURCE_MAP: dict[str, str] = {
    # resource_name → user_filename
    "default_config.json": "agent_config.json",
    "default_system_prompt.txt": "system_prompt.txt",
    "default_providers.json": "providers.json",
    "default_security_policy.json": "security_policy.json",
}

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
    1. Create ``~/.thoughtmachine/`` if it does not exist.
    2. For each bundled default resource, copy it to the user directory
       **only if** the destination is missing (or *overwrite_existing* is
       ``True``).

    Args:
        overwrite_existing: If ``True``, overwrite user files even when they
            already exist (useful for factory-reset scenarios).

    Returns:
        A list of absolute paths to files that were **created** or
        **overwritten**.
    """
    USER_DIR.mkdir(parents=True, exist_ok=True)

    # ── Ensure required subdirectories exist ────────────────────────────
    (USER_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    (USER_DIR / "state").mkdir(parents=True, exist_ok=True)
    (USER_DIR / "knowledge").mkdir(parents=True, exist_ok=True)
    (USER_DIR / "worker_templates").mkdir(parents=True, exist_ok=True)

    resources = _resources_dir()
    touched: list[str] = []

    for resource_name, user_filename in RESOURCE_MAP.items():
        dst = USER_DIR / user_filename
        if dst.exists() and not overwrite_existing:
            continue

        src = resources / resource_name
        shutil.copy2(str(src), str(dst))
        touched.append(str(dst))

    # ── Copy shipped worker_templates if destination is empty ────────────
    worker_templates_src = resources / "worker_templates"
    worker_templates_dst = USER_DIR / "worker_templates"
    if worker_templates_src.is_dir() and not any(worker_templates_dst.iterdir()):
        for src_file in worker_templates_src.iterdir():
            if src_file.is_file():
                shutil.copy2(str(src_file), str(worker_templates_dst / src_file.name))
                touched.append(str(worker_templates_dst / src_file.name))

    # ── Ensure the global knowledge base (system/ + user/ + .version) ─────
    touched.extend(ensure_global_kb())

    return touched


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


def load_user_system_prompt() -> Optional[str]:
    """Load the user's ``system_prompt.txt``, or ``None`` if missing."""
    prompt_path = USER_DIR / "system_prompt.txt"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    try:
        return _read_default("default_system_prompt.txt")
    except FileNotFoundError:
        return None
