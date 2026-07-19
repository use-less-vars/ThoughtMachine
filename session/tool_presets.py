"""
tool_presets.py — Tool presets for agent/engineer modes.

Defines which tools are available per session mode.
These presets are enforced when sessions are created or restored.
"""

from __future__ import annotations

from typing import Dict, List


# ── Tool Lists ───────────────────────────────────────────────────────

# Tools available to ALL modes (core communication + utility)
CORE_TOOLS = [
    "Respond",
    "Thought",
    "CheckSystem",
]

# Agent mode tools — focused on research, knowledge, and user interaction
AGENT_TOOLS = CORE_TOOLS + [
    "KnowledgeBaseTool",
    "FileSearchTool",
    "SearchCodebaseTool",
    "GlobTool",
    "DirectoryTreeTool",
    "DateTimeTool",
    "SummarizeTool",
    "ProgressReport",
]

# Engineer mode tools — focused on code editing, git, and execution
ENGINEER_TOOLS = CORE_TOOLS + [
    "FileEditor",
    "ReadFile",
    "ApplyEdits",
    "CodeModifier",
    "RefactorTool",
    "FileMover",
    "FilePreviewTool",
    "FileSummaryTool",
    "DirectoryCreator",
    "GlobTool",
    "DirectoryTreeTool",
    "SearchCodebaseTool",
    "FileSearchTool",
    "GitInfoTool",
    "DockerCodeRunner",
    "DateTimeTool",
    "SummarizeTool",
    "KnowledgeBaseTool",
    "ProgressReport",
]

# Custom mode tools — full access (can be customized per session)
CUSTOM_TOOLS = CORE_TOOLS + [
    "FileEditor",
    "ReadFile",
    "ApplyEdits",
    "CodeModifier",
    "RefactorTool",
    "FileMover",
    "FilePreviewTool",
    "FileSummaryTool",
    "DirectoryCreator",
    "GlobTool",
    "DirectoryTreeTool",
    "SearchCodebaseTool",
    "FileSearchTool",
    "GitInfoTool",
    "DockerCodeRunner",
    "DateTimeTool",
    "SummarizeTool",
    "KnowledgeBaseTool",
    "ProgressReport",
    "Worker",
    "EditDockerfile",
    "FieldViewer",
]


# ── Lookup ───────────────────────────────────────────────────────────

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
