"""
config_routes.py — REST API for configuration management.

Provides:
- POST /api/config/reset — reset configuration to factory defaults
- POST /api/config/mode  — switch between engineer and full system prompt modes
- GET  /api/config/mode  — query the current system prompt mode
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from agent.presenter.state_bridge import StateBridge
import thoughtmachine.vault as _vault


# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/config")

_CONFIG_PATH = "~/.thoughtmachine/agent_config.json"


# ── Pydantic models ──────────────────────────────────────────────────────────


class ResetConfigBody(BaseModel):
    session_id: Optional[str] = None
    # Optional workspace scope for the cached-bridge update below: a bridge
    # cached for a different workspace is not this caller's to reconfigure.
    workspace_id: Optional[str] = None


class ModeSwitchBody(BaseModel):
    mode: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_custom_prompt_path() -> Path:
    """Return the path to ``custom_system_prompt.txt``."""
    return _vault.vault_root() / "custom_system_prompt.txt"


def _get_engineer_prompt_path() -> Path:
    """Return the path to ``engineer_system_prompt.txt``."""
    return _vault.vault_root() / "engineer_system_prompt.txt"


def _is_engineer_mode() -> bool:
    """Check whether engineer mode is active.

    Engineer mode is active when ``custom_system_prompt.txt`` exists
    **and** has non-whitespace content.
    """
    prompt_path = _get_custom_prompt_path()
    if not prompt_path.exists():
        return False
    try:
        text = prompt_path.read_text(encoding="utf-8").strip()
        return bool(text)
    except (FileNotFoundError, IOError):
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/reset")
async def reset_config(request: Request, body: ResetConfigBody) -> Dict[str, Any]:
    """Reset active configuration to factory defaults.

    Writes an empty overlay to ``agent_config.json`` and removes the
    ``custom_system_prompt.txt`` file if present, so that all values
    revert to factory defaults on the next load.

    If *session_id* is provided and a bridge for that session is cached
    in ``server._session_bridges`` **for the workspace named by
    *workspace_id***, the bridge's in-memory config is also updated.
    This is optional and best-effort — the endpoint always resets the
    config file regardless, also when the cached-bridge lookup misses
    (unknown session, a bridge holding no session, or a bridge belonging
    to another workspace).

    Returns:
        The factory-default configuration dict (with ``api_key`` excluded).
    """
    try:
        import os
        config_path = os.path.expanduser(_CONFIG_PATH)
        st = StateBridge(config_path=config_path)
        factory_config = st.reset_config_to_factory()

        # ── Broadcast to active session (if requested) ─────────────────
        if body.session_id:
            # Import lazily to avoid circular import at module level
            import web_ui.backend.server as server_mod
            # Workspace-scoped lookup (PR #157 discipline): a bridge cached for
            # ANOTHER workspace — or one that holds no session at all — is a
            # miss.  Mutating it here would reconfigure a live session that
            # belongs to a different workspace.  A miss is not an error: the
            # config file above is already reset and this update is optional.
            cached = server_mod._cached_bridge_for(
                body.session_id, expected_workspace_id=body.workspace_id
            )
            if cached is not None:
                # Rebuild the bridge's in-memory AgentConfig from factory
                from agent.config import AgentConfig
                cached._config = AgentConfig(**factory_config)

        return factory_config

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset config: {exc}",
        )


@router.post("/mode")
async def set_mode(request: Request, body: ModeSwitchBody) -> Dict[str, str]:
    """Switch the system prompt mode.

    Accepts ``{"mode": "agent"}``, ``{"mode": "engineer"}``, or
    ``{"mode": "custom"}`` (legacy ``"full"`` is accepted as an alias for
    ``"agent"``).

    This works at the **file level** by writing or removing
    ``custom_system_prompt.txt``, which the ``AgentConfig``
    ``load_default_system_prompt`` validator reads.  The session-level
    ``mode`` field (set via ``POST /api/session/create``) takes precedence
    at runtime.

    * **agent** — removes ``custom_system_prompt.txt`` so the default
      (agent) system prompt is used.
    * **engineer** — copies ``engineer_system_prompt.txt`` to
      ``custom_system_prompt.txt``.
    * **custom** — leaves ``custom_system_prompt.txt`` as-is (user must
      have saved their own prompt).

    Returns ``{"status": "ok", "mode": "..."}``.
    """
    mode = body.mode.strip().lower()

    # Map legacy "full" to "agent"
    if mode == "full":
        mode = "agent"

    if mode not in ("agent", "engineer", "custom"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid mode: '{body.mode}'. Must be 'agent', 'engineer', or 'custom'.",
        )

    try:
        tm_dir = _vault.vault_root()
        tm_dir.mkdir(parents=True, exist_ok=True)

        custom_path = _get_custom_prompt_path()

        if mode == "engineer":
            engineer_path = _get_engineer_prompt_path()
            if not engineer_path.exists():
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Engineer system prompt file not found at "
                           f"{engineer_path}. Ensure the file exists.",
                )
            content = engineer_path.read_text(encoding="utf-8")
            custom_path.write_text(content, encoding="utf-8")
        elif mode == "agent":
            if custom_path.exists():
                custom_path.unlink()
        # mode == "custom": leave custom_system_prompt.txt as-is

        return {"status": "ok", "mode": mode}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to switch mode: {exc}",
        )


@router.get("/mode")
async def get_mode() -> Dict[str, str]:
    """Return the current system prompt mode.

    Returns ``{"mode": "engineer"}`` or ``{"mode": "agent"}`` based on
    whether ``custom_system_prompt.txt`` exists and is non-empty.
    Returns ``{"mode": "custom"}`` if ``custom_system_prompt.txt`` exists
    but is empty or matches none of the factory prompts.
    """
    try:
        active = _is_engineer_mode()
        return {"mode": "engineer" if active else "agent"}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to determine mode: {exc}",
        )
