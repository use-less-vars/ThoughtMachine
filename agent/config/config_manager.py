"""Vault-aware defaults resolution and save-as-default."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.config.deep_merge import deep_merge


logger = logging.getLogger(__name__)


# ── Path helpers ────────────────────────────────────────────────────────────


def _vault_root() -> Path:
    return Path.home() / ".thoughtmachine"


def _factory_defaults_path() -> Path:
    return _vault_root() / "system" / "factory_defaults.json"


def _user_defaults_path() -> Path:
    return _vault_root() / "user" / "defaults.json"


def _workspace_defaults_path(workspace_id: str) -> Path:
    return _vault_root() / "workspaces" / workspace_id / "defaults.json"


# ── Public API ──────────────────────────────────────────────────────────────


def resolve_config_defaults(workspace_id: str) -> dict:
    """Resolve layered config defaults for a workspace.

    Load order (later layers override earlier ones):
    1. ``system/factory_defaults.json`` — immutable base
    2. ``user/defaults.json`` — global user defaults (optional)
    3. ``workspaces/<workspace_id>/defaults.json`` — per-workspace defaults (optional)

    Returns:
        A single merged dict of configuration values.
        Returns an empty dict if nothing is found.
    """
    # Layer 1: factory defaults
    config: dict = {}
    factory_path = _factory_defaults_path()
    try:
        if factory_path.exists():
            raw = json.loads(factory_path.read_text(encoding="utf-8"))
            # factory_defaults.json wraps config under a "config" key
            inner = raw.get("config", raw) if isinstance(raw, dict) else {}
            if isinstance(inner, dict):
                config = deep_merge(config, inner)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read factory defaults from %s: %s", factory_path, exc)

    # Layer 2: user defaults (global)
    user_path = _user_defaults_path()
    try:
        if user_path.exists():
            user_data = json.loads(user_path.read_text(encoding="utf-8"))
            if isinstance(user_data, dict):
                config = deep_merge(config, user_data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read user defaults from %s: %s", user_path, exc)

    # Layer 3: workspace-specific defaults
    ws_path = _workspace_defaults_path(workspace_id)
    try:
        if ws_path.exists():
            ws_data = json.loads(ws_path.read_text(encoding="utf-8"))
            if isinstance(ws_data, dict):
                config = deep_merge(config, ws_data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read workspace defaults from %s: %s", ws_path, exc)

    return config


def save_config_defaults(
    config_dict: dict,
    workspace_id: str,
    *,
    global_scope: bool = False,
) -> Path:
    """Save a config dict as defaults, overwriting any existing file.

    Args:
        config_dict: Configuration values to persist.
        workspace_id: Workspace identifier (used when ``global_scope=False``).
        global_scope: If True, write to ``user/defaults.json``.
                       If False (default), write to ``workspaces/<ws>/defaults.json``.

    Returns:
        The path to the written file.

    Raises:
        OSError: If the file cannot be written.
    """
    if global_scope:
        dst = _user_defaults_path()
    else:
        dst = _workspace_defaults_path(workspace_id)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(config_dict, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    logger.info("Saved config defaults to %s", dst)
    return dst
