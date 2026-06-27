"""
config_routes.py — REST API for configuration management.

Provides:
- POST /api/config/reset — reset configuration to factory defaults
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agent.presenter.state_bridge import StateBridge

# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/config")

_CONFIG_PATH = "~/.thoughtmachine/agent_config.json"


# ── Pydantic models ──────────────────────────────────────────────────────────


class ResetConfigBody(BaseModel):
    session_id: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/reset")
async def reset_config(body: ResetConfigBody) -> Dict[str, Any]:
    """Reset active configuration to factory defaults.

    Writes an empty overlay to ``agent_config.json`` and removes the
    ``custom_system_prompt.txt`` file if present, so that all values
    revert to factory defaults on the next load.

    If *session_id* is provided and a bridge for that session is cached
    in ``server._session_bridges``, the bridge's in-memory config is
    also updated.  This is optional — the endpoint always resets the
    config file regardless.

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
            cached = server_mod._session_bridges.get(body.session_id)
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
