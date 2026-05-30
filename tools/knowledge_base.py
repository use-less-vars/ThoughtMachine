"""
Knowledge Base Tool.

Provides a project notebook with persistent, domain-organized Markdown files
for architecture notes, development guides, roadmaps, bug tracking, lessons
learned, and task management.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal, ClassVar, List, Dict, Optional, Tuple

import re as _re

from pydantic import Field

from .base import ToolBase

# Lazy import for global KB (avoids circular import at module level)
def _get_ensure_global_kb():
    from agent.knowledge.global_kb import ensure_global_kb
    return ensure_global_kb()


def _get_global_kb_root():
    from agent.knowledge.global_kb import get_global_kb_root
    return get_global_kb_root()

logger = logging.getLogger(__name__)


# Hardcoded domain registry mapping domain names to relative file paths
DOMAINS: Dict[str, str] = {
    "system_architecture": "project/system_architecture.md",
    "development_guides": "project/development_guides.md",
    "roadmap": "project/roadmap.md",
    "bugs_and_fixes": "personal/bugs_and_fixes.md",
    "lessons_learned": "personal/lessons_learned.md",
    "task_tracker": "personal/task_tracker.md",
}

# Template headers for each domain file (used when creating missing files)
DOMAIN_TEMPLATES: Dict[str, str] = {
    "system_architecture": (
        "# System Architecture\n\n"
        "Key architectural decisions, component relationships, and data flow patterns.\n\n"
        "## Current Status\n"
        "- No architecture notes recorded yet.\n\n"
        "## Components\n"
        "(To be populated)\n\n"
        "## Data Flow\n"
        "(To be populated)\n"
    ),
    "development_guides": (
        "# Development Guides\n\n"
        "Coding conventions, setup instructions, and development workflows.\n\n"
        "## Current Status\n"
        "- No guides recorded yet.\n\n"
        "## Setup\n"
        "(To be populated)\n\n"
        "## Conventions\n"
        "(To be populated)\n\n"
        "## Workflows\n"
        "(To be populated)\n"
    ),
    "roadmap": (
        "# Roadmap\n\n"
        "Project milestones, planned features, and long-term goals.\n\n"
        "## Current Status\n"
        "- No roadmap items recorded yet.\n\n"
        "## Upcoming Milestones\n"
        "(To be populated)\n\n"
        "## Future Ideas\n"
        "(To be populated)\n"
    ),
    "bugs_and_fixes": (
        "# Bugs and Fixes\n\n"
        "Record of bugs encountered, root causes, and fixes applied.\n\n"
        "## Current Status\n"
        "- No bugs recorded yet.\n\n"
        "## Open Bugs\n"
        "(To be populated)\n\n"
        "## Fixed\n"
        "(To be populated)\n"
    ),
    "lessons_learned": (
        "# Lessons Learned\n\n"
        "Insights, gotchas, and recurring patterns discovered during development.\n\n"
        "## Current Status\n"
        "- No lessons recorded yet.\n\n"
        "## Lessons\n"
        "(To be populated)\n"
    ),
    "task_tracker": (
        "# Task Tracker\n\n"
        "Current tasks, phased plans, and open items.\n\n"
        "## Current Status\n"
        "- No active tasks. Ready for new work.\n\n"
        "## Active Tasks\n"
        "(To be populated)\n\n"
        "## Completed\n"
        "(To be populated)\n"
    ),
}


class KnowledgeBaseTool(ToolBase):
    """
    PROJECT NOTEBOOK — Persistent, domain-organized knowledge base.

    Use this tool to store and retrieve project information that persists
    across sessions. The knowledge base lives in `.thoughtmachine/knowledge/`
    (workspace scope) or `~/.thoughtmachine/knowledge/` (global scope).
    Use the ``scope`` parameter (``workspace`` or ``global``) to select which KB to use.

    **Workspace scope:** Local to the project, organized into domains:
    **Global scope:** User-wide, shared across all projects, with ``system/`` (read-only built-in reference)
    and ``user/`` (writable personal notes) subdirectories.
      - **system_architecture**: Architectural decisions and component relationships
      - **development_guides**: Coding conventions and workflows
      - **roadmap**: Milestones and future plans
      - **bugs_and_fixes**: Bug logs and fixes applied
      - **lessons_learned**: Insights and recurring patterns
      - **task_tracker**: Current tasks and open items

    Modes:
      - **list**: List all domains with last-modified dates
      - **read**: Return full content of a domain file (supports optional max_tokens and section parameters)
      - **append**: Append a timestamped entry to a domain file (supports ``append_section`` for section-targeted appends)
      - **update**: Replace a section's content in a domain file
      - **status**: Show current status from task_tracker + recent entries across all files
      - **search**: Search all KB files for a query (substring, case-insensitive)
      - **create_domain**: Create a new domain file
      - **summary**: Return a lightweight section index of a domain file
    """

    tool: Literal["KnowledgeBase"] = "KnowledgeBase"

    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = ["read_files", "write_files"]

    mode: Literal["list", "read", "append", "update", "status", "search", "create_domain", "summary"] = Field(
        ..., description="Operation mode: list, read (with optional max_tokens and section), append (with optional append_section for section-targeted appends), update, status, search, create_domain, or summary"
    )
    domain: Optional[str] = Field(
        None,
        description=(
            "Domain name (required for read/append/update). "
            f"Valid values: {', '.join(sorted(DOMAINS.keys()))}"
        ),
    )
    entry: Optional[str] = Field(
        None,
        description="Content to append (required for append mode). Use for recording bugs, lessons, etc.",
    )
    summary: Optional[str] = Field(
        None,
        description="Optional one-line summary for the appended entry. If omitted, derived from first 60 chars of entry.",
    )
    section: Optional[str] = Field(
        None,
        description=(
            "Section header for update or read mode. In update mode (required): the section to replace. "
            "In read mode (optional): extract only this section's content."
        ),
    )
    new_content: Optional[str] = Field(
        None,
        description="New content to replace the section with (required for update mode).",
    )
    max_tokens: Optional[int] = Field(
        None,
        description=(
            "Optional max token limit for read mode. When set, content is truncated to this limit. "
            "Token estimation uses ~4 chars per token (len(content)//4)."
        ),
    )
    query: Optional[str] = Field(
        None,
        description="Search term (required for search mode). Case-insensitive substring match across all KB files.",
    )
    category: Optional[str] = Field(
        None,
        description="Category for create_domain mode: 'project' (shared) or 'personal' (private). Defaults to 'personal'.",
    )
    description: Optional[str] = Field(
        None,
        description="One-line description for the new domain (used in create_domain mode).",
    )
    append_section: Optional[str] = Field(
        None,
        description=(
            "Optional heading for append mode. If provided, the entry will be "
            "inserted under this ## heading (creating it if missing). "
            "Ignored unless mode=append."
        ),
    )
    scope: Literal["workspace", "global"] = Field(
        "workspace",
        description=(
            "Which knowledge base to use: workspace (project-local, "
            ".thoughtmachine/knowledge/) or global (user-wide, "
            "~/.thoughtmachine/knowledge/)."
        ),
    )

    def execute(self) -> str:
        """
        Execute the knowledge base operation based on self.mode.
        """
        self._log_debug(f"KnowledgeBaseTool.execute called with mode='{self.mode}', domain='{self.domain}'")

        # Resolve the knowledge base root directory
        if self.scope == "global":
            kb_root = _get_global_kb_root()
            init_result = None  # global KB is self-initialising
        else:
            if self.workspace_path:
                kb_root = Path(self.workspace_path) / ".thoughtmachine" / "knowledge"
            else:
                kb_root = Path.cwd() / ".thoughtmachine" / "knowledge"
            init_result = self._initialize_kb(kb_root)
            if init_result:
                self._log_debug(f"Knowledge base initialized: {init_result}")

        # Dispatch to mode handler
        try:
            if self.mode == "list":
                return self._mode_list(kb_root)
            elif self.mode == "read":
                return self._mode_read(kb_root)
            elif self.mode == "append":
                return self._mode_append(kb_root)
            elif self.mode == "update":
                return self._mode_update(kb_root)
            elif self.mode == "status":
                return self._mode_status(kb_root)
            elif self.mode == "search":
                return self._mode_search(kb_root)
            elif self.mode == "create_domain":
                return self._mode_create_domain(kb_root)
            elif self.mode == "summary":
                return self._mode_summary(kb_root)
            else:
                return f"Unknown mode: {self.mode}. Supported modes: list, read, append, update, status, search, create_domain, summary."
        except Exception as e:
            self._log_tool_error(f"Unexpected error in {self.mode} mode: {e}")
            return f"An unexpected error occurred: {e}. Please try again or check the knowledge base files."

    def _resolve_domain_path(self, kb_root: Path, domain_name: str, *, for_write: bool = False) -> tuple[Path, str]:
        """
        Resolve a domain name to its (file_path, rel_path).

        **Workspace scope:**
        First checks the built-in DOMAINS dict, then scans project/ and personal/
        directories for a matching ``{domain_name}.md`` file.

        **Global scope:**
        Checks ``system/{domain}.md`` then ``user/{domain}.md``.
        If ``for_write=True`` and the domain exists in ``system/``, raises
        ``ValueError`` because system domains are read-only.

        Raises ValueError if the domain cannot be found.
        """
        if self.scope == "global":
            # ── Global scope ────────────────────────────────────────────────
            system_path = kb_root / "system" / f"{domain_name}.md"
            user_path = kb_root / "user" / f"{domain_name}.md"

            if for_write:
                if system_path.exists():
                    raise ValueError(
                        f"Domain '{domain_name}' is a system domain and is read-only. "
                        f"Use a different domain name for personal notes, or use scope=workspace."
                    )
                # Always write to user/
                return user_path, f"user/{domain_name}.md"

            # Read: check system/ first, then user/
            if system_path.exists():
                return system_path, f"system/{domain_name}.md"
            if user_path.exists():
                return user_path, f"user/{domain_name}.md"

            raise ValueError(
                f"Unknown domain '{domain_name}' in global knowledge base. "
                f"Available domains: use `mode=list scope=global` to list all."
            )

        # ── Workspace scope (existing behaviour) ────────────────────────────
        # Check built-in registry first
        rel_path = DOMAINS.get(domain_name)
        if rel_path:
            file_path = kb_root / rel_path
            if file_path.exists():
                return file_path, rel_path
            return file_path, rel_path

        # Scan filesystem for custom domain files
        for subdir in ["project", "personal"]:
            candidate = kb_root / subdir / f"{domain_name}.md"
            if candidate.exists():
                rel = f"{subdir}/{domain_name}.md"
                return candidate, rel

        raise ValueError(
            f"Unknown domain '{domain_name}'. "
            f"Available domains: {', '.join(sorted(DOMAINS.keys()))}. "
            f"Use `mode=list` to see all domains (including custom)."
        )

    def _initialize_kb(self, kb_root: Path) -> str:
        """
        Ensure kb_root and its subdirectories exist, and that all registered
        domain files exist (create with template header if missing).

        Returns a summary string of what was created, or empty string if nothing changed.
        """
        created = []

        # Create directory structure
        for subdir in ["project", "personal"]:
            dir_path = kb_root / subdir
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                created.append(f"directory {dir_path.relative_to(kb_root.parent)}")

        # Ensure each domain file exists
        for domain_name, rel_path in DOMAINS.items():
            file_path = kb_root / rel_path
            if not file_path.exists():
                template = DOMAIN_TEMPLATES.get(domain_name, f"# {domain_name.replace('_', ' ').title()}\n\n(To be populated)\n")
                try:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(template, encoding="utf-8")
                    created.append(f"file {rel_path}")
                except OSError as e:
                    self._log_tool_warning(f"Could not create {rel_path}: {e}")

        if created:
            return f"Created {', '.join(created)}"
        return ""

    def _mode_list(self, kb_root: Path) -> str:
        """List all domains with last-modified dates.

        For workspace scope: lists built-in domains plus custom files from
        project/ and personal/ directories.
        For global scope: lists domains from system/ (read-only) and user/ (writable) directories.
        """
        if self.scope == "global":
            return self._mode_list_global(kb_root)

        # ── Workspace scope (existing behaviour) ──────────────────────────
        lines = ["## Knowledge Base - Available Domains\n"]
        lines.append(f"| Domain | File | Category | Last Modified |")
        lines.append(f"|--------|------|----------|---------------|")

        # Track which rel_paths are already listed
        listed_paths = set()

        # List built-in domains first
        for domain_name in sorted(DOMAINS.keys()):
            rel_path = DOMAINS[domain_name]
            file_path = kb_root / rel_path
            category = rel_path.split("/")[0]  # "project" or "personal"
            modified = "N/A"
            if file_path.exists():
                try:
                    mtime = file_path.stat().st_mtime
                    modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    modified = "unreadable"
            listed_paths.add(rel_path)
            lines.append(f"| {domain_name} | `{rel_path}` | {category} | {modified} |")

        # Scan filesystem for custom domain files not in DOMAINS dict
        custom_domains = []
        for subdir in ["project", "personal"]:
            dir_path = kb_root / subdir
            if not dir_path.exists():
                continue
            try:
                for fpath in sorted(dir_path.iterdir()):
                    if fpath.suffix != ".md":
                        continue
                    rel = f"{subdir}/{fpath.name}"
                    if rel in listed_paths:
                        continue
                    # Derive domain name from filename (strip .md)
                    custom_name = fpath.stem
                    modified = "N/A"
                    try:
                        mtime = fpath.stat().st_mtime
                        modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    except OSError:
                        modified = "unreadable"
                    custom_domains.append((custom_name, rel, subdir, modified))
            except OSError:
                continue

        if custom_domains:
            lines.append("")
            lines.append("### Custom Domains")
            lines.append("| Domain | File | Category | Last Modified |")
            lines.append("|--------|------|----------|---------------|")
            for custom_name, rel, subdir, modified in custom_domains:
                lines.append(f"| {custom_name} | `{rel}` | {subdir} | {modified} |")

        lines.append("")
        lines.append("**Usage:**")
        lines.append("- `mode=read domain=<name>` — View a domain's full content")
        lines.append("- `mode=append domain=<name> entry=\"...\"` — Add a timestamped entry")
        lines.append("- `mode=update domain=<name> section=\"...\" new_content=\"...\"` — Replace a section")
        lines.append("- `mode=search query=<term>` — Search across all KB files")
        lines.append("- `mode=status` — Show current status and recent activity")
        lines.append("- `mode=create_domain domain=<name> category=<project|personal>` — Add a new domain")
        return "\n".join(lines)

    def _mode_list_global(self, kb_root: Path) -> str:
        """List all domains in the global knowledge base.

        Lists domains from system/ (read-only) and user/ (writable) directories.
        """
        lines = ["## Global Knowledge Base - Available Domains\n"]
        lines.append("> **Scope:** `global` — the knowledge base at `~/.thoughtmachine/knowledge/`")
        lines.append("> Domains in `system/` are read-only built-in reference materials.")
        lines.append("> Domains in `user/` are writable personal notes.\n")

        for section_label, subdir, icon in [
            ("### System Domains (read-only)", "system", "🔒"),
            ("### User Domains (writable)", "user", "✏️"),
        ]:
            dir_path = kb_root / subdir
            if not dir_path.exists():
                continue
            domain_list = []
            try:
                for fpath in sorted(dir_path.iterdir()):
                    if fpath.suffix != ".md":
                        continue
                    domain_name = fpath.stem
                    modified = "N/A"
                    try:
                        mtime = fpath.stat().st_mtime
                        modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    except OSError:
                        modified = "unreadable"
                    domain_list.append((domain_name, f"{subdir}/{fpath.name}", modified))
            except OSError:
                continue

            if domain_list:
                lines.append("")
                lines.append(f"{icon} {section_label}")
                lines.append("| Domain | File | Last Modified |")
                lines.append("|--------|------|---------------|")
                for domain_name, rel, modified in domain_list:
                    lines.append(f"| {domain_name} | `{rel}` | {modified} |")

        lines.append("")
        lines.append("**Usage:**")
        lines.append("- `mode=read domain=<name> scope=global` — View a global domain")
        lines.append("- `mode=append domain=<name> scope=global entry=\"...\"` — Add a timestamped entry to a user domain")
        lines.append("- `mode=update domain=<name> scope=global section=\"...\" new_content=\"...\"` — Update a user domain section")
        lines.append("- `mode=search query=<term> scope=global` — Search across all global KB files")
        lines.append("- `mode=status scope=global` — Show current status and recent activity")
        lines.append("- `mode=create_domain domain=<name> scope=global category=user` — Add a new user domain")
        return "\n".join(lines)

    def _mode_search(self, kb_root: Path) -> str:
        """Search all KB files for a query (case-insensitive substring match)."""
        if not self.query:
            return "Error: `query` parameter is required for search mode."

        query_lower = self.query.lower()
        self._log_debug(f"Searching KB for '{query_lower}'")

        lines = [f"## Search results for: \"{self.query}\"\n"]
        found_any = False

        # Collect all .md files: built-in domains + any custom files
        all_files = []  # list of (domain_name, rel_path)

        if self.scope == "global":
            # Global scope: scan system/ and user/ directories
            for subdir in ["system", "user"]:
                dir_path = kb_root / subdir
                if not dir_path.exists():
                    continue
                try:
                    for fpath in dir_path.iterdir():
                        if fpath.suffix != ".md":
                            continue
                        rel = f"{subdir}/{fpath.name}"
                        custom_name = fpath.stem
                        all_files.append((custom_name, rel))
                except OSError:
                    continue
        else:
            # Workspace scope: built-in domains first
            for domain_name, rel_path in DOMAINS.items():
                all_files.append((domain_name, rel_path))

            # Custom files from filesystem
            for subdir in ["project", "personal"]:
                dir_path = kb_root / subdir
                if not dir_path.exists():
                    continue
                try:
                    for fpath in dir_path.iterdir():
                        if fpath.suffix != ".md":
                            continue
                        rel = f"{subdir}/{fpath.name}"
                        # Skip if already in DOMAINS
                        if rel in DOMAINS.values():
                            continue
                        custom_name = fpath.stem
                        all_files.append((custom_name, rel))
                except OSError:
                    continue

        for domain_name, rel_path in all_files:
            file_path = kb_root / rel_path
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError as e:
                self._log_tool_warning(f"Could not read {rel_path} for search: {e}")
                continue

            if query_lower not in content.lower():
                continue

            found_any = True
            lines.append(f"### {rel_path}\n")

            # Split into lines for context extraction
            content_lines = content.split("\n")

            # Find matching lines and their surrounding context
            # Also track the nearest ## section heading above each match
            current_section = "(top)"
            section_line_map = {}  # line_idx -> section heading
            for i, cl in enumerate(content_lines):
                if cl.startswith("## "):
                    current_section = cl.strip()
                section_line_map[i] = current_section

            # Find all matching line indices
            matching_lines = []
            for i, cl in enumerate(content_lines):
                if query_lower in cl.lower():
                    matching_lines.append(i)

            # Group contiguous matches
            groups = []
            if matching_lines:
                current_group = [matching_lines[0]]
                for i in range(1, len(matching_lines)):
                    if matching_lines[i] - matching_lines[i - 1] <= 4:
                        current_group.append(matching_lines[i])
                    else:
                        groups.append(current_group)
                        current_group = [matching_lines[i]]
                groups.append(current_group)

            for group in groups:
                start = max(0, group[0] - 2)
                end = min(len(content_lines), group[-1] + 3)
                section = section_line_map.get(group[0], "")
                if section and section != "(top)":
                    lines.append(f"  *{section}*")

                for i in range(start, end):
                    prefix = ">" if i in group else " "
                    lines.append(f"  {prefix} {content_lines[i]}")
                lines.append("")

        if not found_any:
            return (
                f"No results found for \"{self.query}\".\n\n"
                f"💡 **Suggestions:**\n"
                f"- Try a different search term\n"
                f"- Use broader keywords\n"
                f"- Check `mode=list` to see available domains\n"
                f"- The knowledge base may not contain that topic yet"
            )

        return "\n".join(lines)

    def _mode_create_domain(self, kb_root: Path) -> str:
        """Create a new domain file."""
        if not self.domain:
            return "Error: `domain` parameter is required for create_domain mode."

        domain_name = self.domain.lower().replace(" ", "_").strip()

        # Validate domain name: alphanumeric + underscores, no path separators
        if not _re.match(r"^[a-zA-Z0-9_]+$", domain_name):
            return (
                f"Error: Invalid domain name '{self.domain}'. "
                f"Domain names must contain only letters, numbers, and underscores "
                f"(no spaces, slashes, or special characters)."
            )

        if self.scope == "global":
            # Global scope: only user/ is writable
            category = "user"
            if self.category is not None and self.category.lower().strip() != "user":
                return (
                    f"Error: Invalid category '{self.category}' for global scope. "
                    f"Only 'user' category is supported (system/ is read-only)."
                )

            # Check if domain already exists in system/ (read-only) or user/
            system_path = kb_root / "system" / f"{domain_name}.md"
            user_path = kb_root / "user" / f"{domain_name}.md"
            if system_path.exists():
                return f"Domain '{domain_name}' already exists in system/ (read-only). Choose a different name."
            if user_path.exists():
                return f"Domain '{domain_name}' already exists at `user/{domain_name}.md`."

            rel_path = f"user/{domain_name}.md"
            file_path = user_path
        else:
            # Workspace scope
            category = self.category
            if category is None:
                category = "personal"
                self._log_debug(f"No category specified, defaulting to 'personal'")
            else:
                category = category.lower().strip()
                if category not in ("project", "personal"):
                    return (
                        f"Error: Invalid category '{self.category}'. "
                        f"Category must be 'project' (shared) or 'personal' (private)."
                    )

            # Check if domain already exists in built-in registry
            if domain_name in DOMAINS:
                existing_path = DOMAINS[domain_name]
                return f"Domain '{domain_name}' already exists (built-in) at `{existing_path}`."

            # Check if file already exists on disk
            rel_path = f"{category}/{domain_name}.md"
            file_path = kb_root / rel_path
            if file_path.exists():
                return f"Domain '{domain_name}' already exists at `{rel_path}`."


        # Create the file with a template header
        title = domain_name.replace("_", " ").title()
        desc = self.description if self.description else "Documentation and notes for this topic."
        template = (
            f"# {title}\n\n"
            f"{desc}\n\n"
            f"## Overview\n"
            f"(To be populated)\n"
        )

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(template, encoding="utf-8")
        except OSError as e:
            self._log_tool_error(f"Could not create domain file {rel_path}: {e}")
            return f"Error creating domain file '{rel_path}': {e}. Check permissions."

        self._log_debug(f"Created new domain '{domain_name}' at {rel_path}")
        return (
            f"✅ New domain **{domain_name}** created!\n\n"
            f"**Path:** `{rel_path}`\n"
            f"**Category:** {category}\n"
            f"**Description:** {desc}\n\n"
            f"You can now use it with `read`, `append`, or `update` modes. "
            f"It will appear automatically in `mode=list`."
        )

    def _build_section_index(self, content: str, rel_path: str) -> List[Tuple[str, int, int, int]]:
        """
        Build a list of (heading, start_line, end_line, token_estimate) for all
        ##-level sections in the given content.

        Line numbers are 1-indexed. Token estimate uses len(text)//4.
        """
        lines = content.split("\n")
        sections: List[Tuple[str, int, int, int]] = []

        # Find all ## (but not ###) heading lines
        heading_indices = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("## ") and not stripped.startswith("### "):
                heading_indices.append(i)

        if not heading_indices:
            # No ## headings — treat entire file as one section
            token_est = len(content) // 4
            sections.append("(entire file)", 1, len(lines), token_est)
            return sections

        for idx, line_idx in enumerate(heading_indices):
            start_line = line_idx + 1  # 1-indexed
            if idx + 1 < len(heading_indices):
                end_line = heading_indices[idx + 1]  # next heading line (exclusive end)
            else:
                end_line = len(lines)  # until EOF

            # Content of this section (heading line inclusive)
            section_content = "\n".join(lines[line_idx:end_line])
            token_est = len(section_content) // 4
            heading_text = lines[line_idx].strip()
            sections.append((heading_text, start_line, end_line, token_est))

        return sections

    def _mode_read(self, kb_root: Path) -> str:
        """Return content of the specified domain file.

        Supports optional parameters:
        - section: extract only content under a matching ## heading
        - max_tokens: truncate output to token limit
        - If neither is given and file >4000 tokens, auto-truncate with smart suggestion.
        """
        if not self.domain:
            return "Error: `domain` parameter is required for read mode."

        domain_name = self.domain.lower().replace(" ", "_")
        try:
            file_path, rel_path = self._resolve_domain_path(kb_root, domain_name, for_write=True)
        except ValueError as e:
            return str(e)

        if not file_path.exists():
            return f"File not found for domain '{domain_name}'. Try `mode=list` to see available domains."

        try:
            content = file_path.read_text(encoding="utf-8")
        except PermissionError:
            self._log_tool_warning(f"Permission denied reading {file_path}")
            return f"Error: Permission denied reading '{rel_path}'. Check file permissions."
        except OSError as e:
            self._log_tool_error(f"Error reading {file_path}: {e}")
            return f"Error reading '{rel_path}': {e}"

        self._log_debug(f"Read {len(content)} characters from {rel_path}")

        # --- Step 1: Handle section extraction (if section is provided) ---
        if self.section:
            clean_section = self.section.lstrip("#").strip()
            # Try exact match first, then case-insensitive match
            # Prefer the match with the most content (handles duplicate section headings)
            match_heading = None
            match_start = None
            match_size = 0
            for candidate_heading, sline, eline, _ in self._build_section_index(content, rel_path):
                candidate_clean = candidate_heading.lstrip("#").strip()
                if candidate_clean == clean_section or candidate_clean.lower() == clean_section.lower():
                    size = eline - sline
                    if size > match_size:
                        match_heading = candidate_heading
                        match_start = sline - 1  # convert to 0-indexed
                        match_end = eline
                        match_size = size

            if match_heading is None or match_start is None:
                # Section not found — build and show available sections
                available = self._build_section_index(content, rel_path)
                avail_lines = [f"Section \"{self.section}\" not found in **{domain_name}**. Available sections:\n"]
                for h, sl, el, tok in available:
                    avail_lines.append(f"- {h} (lines {sl}–{el}, ~{tok} tokens)")
                return "\n".join(avail_lines)

            # Extract section content (heading line inclusive)
            lines = content.split("\n")
            section_content = "\n".join(lines[match_start:match_end])

            # If max_tokens is also set, apply truncation after section extraction
            if self.max_tokens is not None:
                full_tokens = len(section_content) // 4
                if full_tokens > self.max_tokens:
                    trunc_chars = self.max_tokens * 4
                    section_content = section_content[:trunc_chars]
                    section_content += (
                        f"\n\n... [truncated from {full_tokens} tokens to {self.max_tokens} tokens. "
                        f"Use mode=search query=\"...\" for targeted results, "
                        f"or mode=read section=\"{match_heading}\" with a larger max_tokens.]"
                    )
                    self._log_debug(f"Truncated section to {self.max_tokens} tokens")

            return f"## Domain: {domain_name}\n\n{section_content}"

        # --- Step 2: Handle max_tokens-only truncation ---
        if self.max_tokens is not None:
            full_tokens = len(content) // 4
            if full_tokens <= self.max_tokens:
                result = f"## Domain: {domain_name}\n\n{content}"
                self._log_debug(f"Content fits within {self.max_tokens} tokens ({full_tokens} total)")
                return result

            trunc_chars = self.max_tokens * 4
            truncated = content[:trunc_chars]
            truncated += (
                f"\n\n... [truncated from {full_tokens} tokens to {self.max_tokens} tokens. "
                f"Use mode=search query=\"...\" for targeted results, "
                f"mode=summary domain={domain_name} for section index, "
                f"or mode=read section=\"...\" for specific sections.]"
            )
            self._log_debug(f"Truncated content to {self.max_tokens} tokens (was {full_tokens})")
            return f"## Domain: {domain_name}\n\n{truncated}"

        # --- Step 3: Auto-truncation (neither section nor max_tokens given) ---
        full_tokens = len(content) // 4
        if full_tokens <= 4000:
            # File is small enough — return full content
            self._log_debug(f"File is {full_tokens} tokens, <= 4000, returning full content")
            return f"## Domain: {domain_name}\n\n{content}"

        # Auto-truncate to 4000 tokens with smart suggestion
        trunc_chars = 4000 * 4
        truncated = content[:trunc_chars]
        warning = (
            f"⚠️ This file is ~{full_tokens} tokens. Automatically showing first 4000 tokens.\n"
            f"💡 Better alternatives:\n"
            f"   - mode=search query=\"...\" for keyword search\n"
            f"   - mode=summary domain={domain_name} for a section index\n"
            f"   - mode=read section=\"Section Name\" for a specific section\n"
        )
        truncated += (
            f"\n\n... [truncated from {full_tokens} tokens to 4000 tokens. "
            f"Use the alternatives above for better results.]"
        )
        self._log_debug(f"Auto-truncated {domain_name} from {full_tokens} tokens to 4000")
        return f"## Domain: {domain_name}\n\n{warning}\n{truncated}"

    def _mode_summary(self, kb_root: Path) -> str:
        """
        Return a lightweight section index of a domain file:
        all ## headings, line ranges, and estimated tokens per section.
        """
        if not self.domain:
            return "Error: `domain` parameter is required for summary mode."

        domain_name = self.domain.lower().replace(" ", "_")
        try:
            file_path, rel_path = self._resolve_domain_path(kb_root, domain_name)
        except ValueError as e:
            return str(e)

        if not file_path.exists():
            return f"File not found for domain '{domain_name}'. Try `mode=list` to see available domains."

        try:
            content = file_path.read_text(encoding="utf-8")
        except PermissionError:
            self._log_tool_warning(f"Permission denied reading {file_path}")
            return f"Error: Permission denied reading '{rel_path}'. Check file permissions."
        except OSError as e:
            self._log_tool_error(f"Error reading {file_path}: {e}")
            return f"Error reading '{rel_path}': {e}"

        file_size = len(content)
        total_tokens = file_size // 4
        line_count = len(content.split("\n"))

        sections = self._build_section_index(content, rel_path)

        # Build the table
        lines = [
            f"📐 **{domain_name}** ({file_size:,} bytes / ~{total_tokens:,} tokens — {line_count} lines)\n",
            "| Section | Lines | Tokens |",
            "|---------|-------|--------|",
        ]

        # Find largest section(s) for warning
        max_tokens = max((tok for _, _, _, tok in sections), default=0)

        for heading, start_line, end_line, tok in sections:
            line_range = f"{start_line}–{end_line}"
            warning_emoji = " ⚠️ LARGE" if tok == max_tokens and tok > 2000 else ""
            lines.append(f"| {heading} | {line_range} | ~{tok}{warning_emoji} |")

        lines.append("")
        lines.append(
            f"💡 Use `mode=read domain={domain_name} section=\"...\"` "
            f"to read a specific section, or `mode=read domain={domain_name} max_tokens=N` "
            f"for limited content."
        )

        return "\n".join(lines)

    def _mode_append(self, kb_root: Path) -> str:
        """Append a timestamped entry to the specified domain file.

        If ``append_section`` is set, the entry is inserted under that ## heading
        (creating it if missing). Otherwise the entry is appended at the end.

        After writing, a warning is returned if the file exceeds ~25,000 tokens.
        """
        if not self.domain:
            return "Error: `domain` parameter is required for append mode."
        if not self.entry:
            return "Error: `entry` parameter is required for append mode."

        domain_name = self.domain.lower().replace(" ", "_")
        try:
            file_path, rel_path = self._resolve_domain_path(kb_root, domain_name, for_write=True)
        except ValueError as e:
            return str(e)

        # Generate the entry block
        today = datetime.now().strftime("%Y-%m-%d")
        entry_summary = self.summary if self.summary else (self.entry[:60] + "..." if len(self.entry) > 60 else self.entry)
        entry_block = f"\n## {today} — {entry_summary}\n\n{self.entry}\n"

        try:
            if self.append_section:
                # Section-aware append: insert under a named ## heading
                self._log_debug(f"append_section='{self.append_section}', inserting under that heading")
                content = file_path.read_text(encoding="utf-8")
                clean_section = self.append_section.lstrip("#").strip()
                pattern = _re.compile(rf"^## {_re.escape(clean_section)}\s*$", _re.MULTILINE)
                match = pattern.search(content)

                if match:
                    # Heading found — find the next ## heading after it (or EOF)
                    after_heading = match.end()
                    next_section = content.find("\n## ", after_heading)
                    if next_section == -1:
                        # No next section — append at end
                        updated = content + entry_block
                    else:
                        # Insert before the next heading
                        updated = content[:next_section] + entry_block + content[next_section:]
                else:
                    # Section not found — create it at the end
                    section_header = f"## {clean_section}"
                    updated = content.rstrip() + f"\n\n{section_header}\n{entry_block}\n"

                file_path.write_text(updated, encoding="utf-8")
            else:
                # Simple append to end of file (existing behavior)
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(entry_block)
        except PermissionError:
            self._log_tool_warning(f"Permission denied appending to {file_path}")
            return f"Error: Permission denied writing to '{rel_path}'. Check file permissions."
        except OSError as e:
            self._log_tool_error(f"Error appending to {file_path}: {e}")
            return f"Error appending to '{rel_path}': {e}"

        # --- Size warning ---
        APPEND_SIZE_WARNING_TOKENS = 25000
        try:
            current_size = file_path.stat().st_size
            token_est = current_size // 4
        except OSError:
            token_est = 0

        result = f"✅ Entry appended to **{domain_name}** (`{rel_path}`).\n\n**Summary:** {entry_summary}"
        if token_est > APPEND_SIZE_WARNING_TOKENS:
            result += (
                f"\n\n⚠️ This file is now ~{token_est:,} tokens. "
                f"Consider archiving old entries or using mode=read with section= for targeted access."
            )

        self._log_debug(f"Appended entry to {rel_path}: {entry_summary}")
        return result

    def _mode_update(self, kb_root: Path) -> str:
        """Replace a section's content in a domain file."""
        if not self.domain:
            return "Error: `domain` parameter is required for update mode."
        if not self.section:
            return "Error: `section` parameter is required for update mode."
        if self.new_content is None:
            return "Error: `new_content` parameter is required for update mode."

        domain_name = self.domain.lower().replace(" ", "_")
        try:
            file_path, rel_path = self._resolve_domain_path(kb_root, domain_name)
        except ValueError as e:
            return str(e)

        if not file_path.exists():
            return f"File not found for domain '{domain_name}'. Try `mode=list` to see available domains."

        try:
            content = file_path.read_text(encoding="utf-8")
        except PermissionError:
            self._log_tool_warning(f"Permission denied reading {file_path} for update")
            return f"Error: Permission denied reading '{rel_path}'. Check file permissions."
        except OSError as e:
            self._log_tool_error(f"Error reading {file_path} for update: {e}")
            return f"Error reading '{rel_path}': {e}"

        # Normalize section name: strip leading '#' and whitespace
        clean_section = self.section.lstrip('#').strip()

        # Locate the section header using regex anchored to line start
        section_header_re = _re.compile(rf"^## {_re.escape(clean_section)}\s*$", _re.MULTILINE)
        match = section_header_re.search(content)

        if not match:
            # Section not found — append it at the end
            section_header = f"## {clean_section}"
            updated_content = content.rstrip() + f"\n\n{section_header}\n{self.new_content}\n"
            self._log_debug(f"Section '{clean_section}' not found, appending to end of {rel_path}")
            found = False
        else:
            # Section found — delete from header to next "## " or EOF, then insert new header + content
            section_start = match.start()
            # Find the next "## " header after the current one
            next_section = content.find("\n## ", match.end())
            if next_section == -1:
                # No next section, delete until EOF
                updated_content = content[:section_start] + f"## {clean_section}\n{self.new_content}\n"
            else:
                # Delete until next section
                updated_content = content[:section_start] + f"## {clean_section}\n{self.new_content}\n" + content[next_section:]
            found = True

        try:
            file_path.write_text(updated_content, encoding="utf-8")
        except PermissionError:
            self._log_tool_warning(f"Permission denied writing {file_path} for update")
            return f"Error: Permission denied writing to '{rel_path}'. Check file permissions."
        except OSError as e:
            self._log_tool_error(f"Error writing {file_path} for update: {e}")
            return f"Error writing to '{rel_path}': {e}"

        if found:
            self._log_debug(f"Updated section '{self.section}' in {rel_path}")
            return f"✅ Section **{self.section}** updated in **{domain_name}** (`{rel_path}`)."
        else:
            self._log_debug(f"Created new section '{self.section}' in {rel_path}")
            return f"✅ Section **{self.section}** created (appended to end) in **{domain_name}** (`{rel_path}`)."

    def _mode_status(self, kb_root: Path) -> str:
        """Return the 'Current Status' section from task_tracker plus the 5 most recent
        date-headed entries across all files (sorted descending)."""
        lines = ["## Knowledge Base — Current Status\n"]

        if self.scope == "global":
            # Global scope: no task_tracker, just show recent entries
            lines.append("> **Global Knowledge Base** — showing recent activity across all domains.\n")
        else:
            # Workspace scope: read the "Current Status" from task_tracker
            task_file = kb_root / DOMAINS["task_tracker"]
            current_status = "No status available."
            if task_file.exists():
                try:
                    content = task_file.read_text(encoding="utf-8")
                    # Extract "## Current Status" section
                    status_start = content.find("## Current Status")
                    if status_start != -1:
                        after_header = content.index("\n", status_start) + 1
                        next_section = content.find("\n## ", after_header)
                        if next_section == -1:
                            current_status = content[after_header:].strip()
                        else:
                            current_status = content[after_header:next_section].strip()
                except OSError as e:
                    self._log_tool_warning(f"Could not read task_tracker for status: {e}")
                    current_status = f"Error reading task_tracker: {e}"
            else:
                current_status = "task_tracker.md not found. The knowledge base may need initialization."

            lines.append(f"### Task Tracker Status\n\n{current_status}\n")

        # Collect all date-headed entries (## YYYY-MM-DD) across all files
        all_entries = []  # List of (date, domain_name, content_preview)

        # Collect all .md files
        all_domain_files_for_entries = []

        if self.scope == "global":
            # Global scope: scan system/ and user/ directories
            for subdir in ["system", "user"]:
                dir_path = kb_root / subdir
                if not dir_path.exists():
                    continue
                try:
                    for fpath in sorted(dir_path.iterdir()):
                        if fpath.suffix != ".md":
                            continue
                        rel = f"{subdir}/{fpath.name}"
                        custom_name = fpath.stem
                        all_domain_files_for_entries.append((custom_name, rel))
                except OSError:
                    continue
        else:
            # Workspace scope: built-in domains
            for domain_name, rel_path in DOMAINS.items():
                all_domain_files_for_entries.append((domain_name, rel_path))

            # Custom files from filesystem
            for subdir in ["project", "personal"]:
                dir_path = kb_root / subdir
                if not dir_path.exists():
                    continue
                try:
                    for fpath in sorted(dir_path.iterdir()):
                        if fpath.suffix != ".md":
                            continue
                        rel = f"{subdir}/{fpath.name}"
                        if rel in DOMAINS.values():
                            continue
                        custom_name = fpath.stem
                        all_domain_files_for_entries.append((custom_name, rel))
                except OSError:
                    continue

        for domain_name, rel_path in all_domain_files_for_entries:
            file_path = kb_root / rel_path
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                continue

            # Find all ## YYYY-MM-DD entries
            for match in _re.finditer(r"^## (\d{4}-\d{2}-\d{2})\s*[—\-–]\s*(.+)$", content, _re.MULTILINE):
                date_str = match.group(1)
                entry_summary = match.group(2).strip()
                all_entries.append((date_str, domain_name, entry_summary))

        # Sort by date descending
        all_entries.sort(key=lambda x: x[0], reverse=True)

        # Take the 5 most recent entries
        recent_entries = all_entries[:5]

        if recent_entries:
            lines.append("### Recent Activity\n")
            lines.append("| Date | Domain | Summary |")
            lines.append("|------|--------|---------|")
            for date_str, domain_name, entry_summary in recent_entries:
                lines.append(f"| {date_str} | {domain_name} | {entry_summary} |")
            lines.append("")

            if len(all_entries) > 5:
                lines.append(f"*(Showing 5 of {len(all_entries)} total entries)*\n")
        else:
            lines.append("*No dated entries found in the knowledge base yet.*\n")

        # --- KB Storage Statistics ---
        all_domain_files = []
        for subdir in ["project", "personal"]:
            dir_path = kb_root / subdir
            if not dir_path.exists():
                continue
            try:
                for fpath in dir_path.iterdir():
                    if fpath.suffix == ".md":
                        all_domain_files.append(fpath)
            except OSError:
                continue

        total_bytes = 0
        file_stats = []  # (stem, size_bytes, token_est)
        for fpath in all_domain_files:
            try:
                size = fpath.stat().st_size
                total_bytes += size
                tok_est = size // 4
                file_stats.append((fpath.stem, size, tok_est))
            except OSError:
                continue

        # Sort by token count descending, take top 3
        file_stats.sort(key=lambda x: x[2], reverse=True)
        top3 = file_stats[:3]

        total_tokens = total_bytes // 4
        lines.append("")
        lines.append(f"📊 **KB Storage:** total {total_bytes:,} bytes / ~{total_tokens:,} tokens across {len(all_domain_files)} files")
        lines.append("⚠️ **Largest files:**")
        for stem, size_bytes, tok_est in top3:
            lines.append(f"   - {stem}.md: ~{tok_est:,} tokens")
        lines.append("💡 **Tip:** Use `mode=search` for targeted queries, `mode=summary` for section indexes, "
                      "and `mode=read` with `max_tokens` for safe reading.")

        return "\n".join(lines)
