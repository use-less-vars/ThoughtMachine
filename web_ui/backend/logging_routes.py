"""
logging_routes.py — REST API for runtime logging configuration.

Provides:
- GET  /api/logging/config — return current logging configuration
- PUT  /api/logging/config — update logging configuration at runtime
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agent.logging.unified import (
    get_log_config,
    set_log_level,
    set_log_tags,
    set_truncation_limits,
)

router = APIRouter(prefix="/api/logging")


class LoggingConfigBody(BaseModel):
    level: Optional[str] = None
    tags: Optional[str] = None
    truncation_limits: Optional[Dict[str, int]] = None
    session_id: Optional[str] = None


@router.get("/config")
async def get_logging_config(session_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the current runtime logging configuration.

    Query parameters:
        - session_id (optional): If provided, returns config with session-specific
          overrides merged.

    Returns the full logging config dict (log_level, log_tags, truncation_limits,
    available_tags, etc.).
    """
    try:
        return get_log_config(session_id=session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get logging config: {exc}",
        )


@router.put("/config")
async def update_logging_config(body: LoggingConfigBody) -> Dict[str, Any]:
    """Update runtime logging configuration.

    Accepts JSON body with optional fields:
        - level (str): One of DEBUG, INFO, WARNING, ERROR, CRITICAL
        - tags (str): Comma-separated tag patterns, "*" for all, "" to reset
        - truncation_limits (dict): Partial dict of truncation hint -> int limit
        - session_id (str, optional): Apply changes to a specific session only

    Returns the previous and new config values, and broadcasts the update
    to all active WebSocket bridges.
    """
    result: Dict[str, Any] = {
        "status": "ok",
        "config": {},
    }

    try:
        if body.level is not None:
            previous_level = set_log_level(body.level)
            result["previous_level"] = previous_level

        if body.tags is not None:
            previous_tags = set_log_tags(body.tags)
            result["previous_tags"] = previous_tags

        if body.truncation_limits is not None:
            previous_limits = set_truncation_limits(body.truncation_limits)
            result["previous_truncation_limits"] = previous_limits

        # Get the updated config
        result["config"] = get_log_config(session_id=body.session_id)

        # Broadcast to all active WebSocket bridges
        try:
            from web_ui.backend.bridge import WebAgentBridge

            WebAgentBridge.broadcast_logging_config(result["config"])
        except Exception:
            pass  # No bridges active, that's fine

        return result

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update logging config: {exc}",
        )
