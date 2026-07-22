"""
tool_presets.py — Tool presets for agent/engineer modes.

Defines which tools are available per session mode.
These presets are enforced when sessions are created or restored.

This is the single source of truth for the canonical tool list (``_ALL_TOOLS``)
and the per-mode subsets.  ``agent/config/presets.py`` re-exports from here.
"""

from __future__ import annotations

from typing import Dict, List


# ═══════════════════════════════════════════════════════════════════════════
# ALL available tool names (canonical set)
# ═══════════════════════════════════════════════════════════════════════════
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

# ── Tool Lists ────────────────────────────────────────────────────────────

# Agent mode — 23 tools focused on code editing, research, and user interaction
AGENT_TOOLS = [
    "ApplyEdits",
    "CheckSystem",
    "CodeModifier",
    "DateTimeTool",
    "DirectoryCreator",
    "DirectoryTreeTool",
    "DockerCodeRunner",
    "FieldViewer",
    "FileEditor",
    "FileMover",
    "FilePreviewTool",
    "FileSearchTool",
    "FileSummaryTool",
    "GitInfoTool",
    "GlobTool",
    "KnowledgeBaseTool",
    "MCPValidator",
    "PaginateTool",
    "ProgressReport",
    "ReadFile",
    "Respond",
    "SummarizeTool",
    "Thought",
]

# Engineer mode — 7 tools focused on worker orchestration and agent introspection
ENGINEER_TOOLS = [
    "Worker",
    "CheckSystem",
    "Respond",
    "SummarizeTool",
    "ProgressReport",
    "KnowledgeBaseTool",
    "GitInfoTool",
]

# Custom mode — all 27 tools (unrestricted)
CUSTOM_TOOLS = list(_ALL_TOOLS)


# ── Lookup ──────────────────────────────────────────────────────────────────

PRESETS: Dict[str, List[str]] = {
    "agent": AGENT_TOOLS,
    "engineer": ENGINEER_TOOLS,
    "custom": CUSTOM_TOOLS,
}


def get_tools_for_mode(mode: str) -> List[str]:
    """Return the tool list for a given session mode."""
    return PRESETS.get(mode, AGENT_TOOLS)


def validate_mode_tools(mode: str, tools: List[str]) -> bool:
    """Check if a tool list matches the preset for a given mode."""
    expected = get_tools_for_mode(mode)
    return set(tools) == set(expected)
