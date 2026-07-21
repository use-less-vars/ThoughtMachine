"""
presets.py — Central tool presets for agent/engineer modes.

Defines which tools are available per session mode.
These presets are enforced when sessions are created (both PyQt and WebUI paths).

Usage:
    from agent.config.presets import get_tools_for_mode
    tools = get_tools_for_mode("agent")       # → AGENT_TOOLS
    tools = get_tools_for_mode("engineer")    # → ENGINEER_TOOLS
    tools = get_tools_for_mode("custom")      # → CUSTOM_TOOLS (all tools)
"""

from __future__ import annotations

from typing import Dict, List

# ── ALL available tool names (canonical set) ─────────────────────────────
# These are the names used by SIMPLIFIED_TOOL_CLASSES in tools/__init__.py.

_ALL_TOOLS = [
    # Core communication
    "Respond",
    "Thought",
    "CheckSystem",
    # File operations
    "FileEditor",
    "ReadFile",
    "ApplyEdits",
    "CodeModifier",
    "RefactorTool",
    "FileMover",
    "FilePreviewTool",
    "FileSummaryTool",
    "DirectoryCreator",
    # Search & navigation
    "GlobTool",
    "DirectoryTreeTool",
    "FileSearchTool",
    "SearchCodebaseTool",
    # Git
    "GitInfoTool",
    # Execution
    "DockerCodeRunner",
    # Date/time
    "DateTimeTool",
    # Session & agent utilities
    "SummarizeTool",
    "KnowledgeBaseTool",
    "ProgressReport",
    # Worker & advanced
    "Worker",
    "EditDockerfile",
    "FieldViewer",
    "MCPValidator",
    "PaginateTool",
]


# ── Preset definitions ───────────────────────────────────────────────────

# Agent mode — all tools EXCEPT Worker, EditDockerfile, SearchCodebaseTool
AGENT_TOOLS = [
    t for t in _ALL_TOOLS
    if t not in ("Worker", "EditDockerfile", "SearchCodebaseTool")
]

# Engineer mode — minimal set focused on worker orchestration and agent introspection
ENGINEER_TOOLS = [
    "Worker",
    "SummarizeTool",
    "Respond",
    "ProgressReport",
    "CheckSystem",
    "KnowledgeBaseTool",
    "GitInfoTool",
]

# Custom mode — ALL tools available (unrestricted)
CUSTOM_TOOLS = list(_ALL_TOOLS)

# ── Lookup ───────────────────────────────────────────────────────────────

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
