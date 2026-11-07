"""
presets.py — Central tool presets for agent/engineer modes.

Defines which tools are available per session mode.
These presets are enforced when sessions are created (both PyQt and WebUI paths).

Single source of truth: ``session/tool_presets.py`` — this module re-exports
from there to avoid duplication.

Usage:
    from agent.config.presets import get_tools_for_mode
    tools = get_tools_for_mode("agent")       # → AGENT_TOOLS
    tools = get_tools_for_mode("engineer")    # → ENGINEER_TOOLS
    tools = get_tools_for_mode("custom")      # → CUSTOM_TOOLS (all tools)
"""

from __future__ import annotations

from typing import Dict, List
from session.tool_presets import ENGINEER_TOOLS, AGENT_TOOLS, _ALL_TOOLS


# ── Preset definitions ──────────────────────────────────────────────────────

# Agent mode and Engineer mode are imported from session/tool_presets.py
# (see import at top of file).

# Custom mode — ALL tools available (unrestricted)
CUSTOM_TOOLS = list(_ALL_TOOLS)


# ── Lookup ──────────────────────────────────────────────────────────────────

PRESETS: Dict[str, List[str]] = {
    "agent": AGENT_TOOLS,
    "engineer": ENGINEER_TOOLS,
    "custom": CUSTOM_TOOLS,
}


def get_tools_for_mode(mode: str) -> List[str]:
    """Return the tool list for a given session mode.

    Falls back to AGENT_TOOLS if the mode is not recognised.
    """
    return PRESETS.get(mode, AGENT_TOOLS)
