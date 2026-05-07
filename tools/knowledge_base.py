"""
Knowledge Base Tool.

Provides a project notebook with persistent, domain-organized Markdown files
for architecture notes, development guides, roadmaps, bug tracking, lessons
learned, and task management.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal, ClassVar, List, Dict, Optional

from pydantic import Field

from .base import ToolBase

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
    and is organized into domains:
      - **system_architecture**: Architectural decisions and component relationships
      - **development_guides**: Coding conventions and workflows
      - **roadmap**: Milestones and future plans
      - **bugs_and_fixes**: Bug logs and fixes applied
      - **lessons_learned**: Insights and recurring patterns
      - **task_tracker**: Current tasks and open items

    Modes:
      - **list**: List all domains with last-modified dates
      - **read**: Return full content of a domain file
      - **append**: Append a timestamped entry to a domain file
      - **update**: Replace a section's content in a domain file
      - **status**: Show current status from task_tracker + recent entries across all files
      - **search**: Search all KB files for a query (substring, case-insensitive)
      - **create_domain**: Create a new domain file
    """

    tool: Literal["KnowledgeBase"] = "KnowledgeBase"

    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = ["read_files", "write_files"]

    mode: Literal["list", "read", "append", "update", "status", "search", "create_domain"] = Field(
        ..., description="Operation mode: list, read, append, update, status, search, or create_domain"
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
        description="Section header to update (required for update mode, e.g., 'Current Status').",
    )
    new_content: Optional[str] = Field(
        None,
        description="New content to replace the section with (required for update mode).",
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

    def execute(self) -> str:
        """
        Execute the knowledge base operation based on self.mode.
        """
        self._log_debug(f"KnowledgeBaseTool.execute called with mode='{self.mode}', domain='{self.domain}'")

        # Resolve the knowledge base root directory
        if self.workspace_path:
            kb_root = Path(self.workspace_path) / ".thoughtmachine" / "knowledge"
        else:
            kb_root = Path.cwd() / ".thoughtmachine" / "knowledge"

        # Initialize the knowledge base (create dirs and missing files)
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
            else:
                return f"Unknown mode: {self.mode}. Supported modes: list, read, append, update, status, search, create_domain."
        except Exception as e:
            self._log_tool_error(f"Unexpected error in {self.mode} mode: {e}")
            return f"An unexpected error occurred: {e}. Please try again or check the knowledge base files."

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

        Lists the six built-in domains plus any custom domain files found
        by scanning the project/ and personal/ directories.
        """
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

        # Built-in domains
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
        import re as _re
        if not _re.match(r"^[a-zA-Z0-9_]+$", domain_name):
            return (
                f"Error: Invalid domain name '{self.domain}'. "
                f"Domain names must contain only letters, numbers, and underscores "
                f"(no spaces, slashes, or special characters)."
            )

        # Determine category
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

    def _mode_read(self, kb_root: Path) -> str:
        """Return full content of the specified domain file."""
        if not self.domain:
            return "Error: `domain` parameter is required for read mode."

        domain_name = self.domain.lower().replace(" ", "_")
        rel_path = DOMAINS.get(domain_name)
        if not rel_path:
            return (
                f"Error: Unknown domain '{self.domain}'. "
                f"Available domains: {', '.join(sorted(DOMAINS.keys()))}. "
                f"Use `mode=list` to see all domains."
            )

        file_path = kb_root / rel_path
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
        return f"## Domain: {domain_name}\n\n{content}"

    def _mode_append(self, kb_root: Path) -> str:
        """Append a timestamped entry to the specified domain file."""
        if not self.domain:
            return "Error: `domain` parameter is required for append mode."
        if not self.entry:
            return "Error: `entry` parameter is required for append mode."

        domain_name = self.domain.lower().replace(" ", "_")
        rel_path = DOMAINS.get(domain_name)
        if not rel_path:
            return (
                f"Error: Unknown domain '{self.domain}'. "
                f"Available domains: {', '.join(sorted(DOMAINS.keys()))}."
            )

        file_path = kb_root / rel_path

        # Generate the entry block
        today = datetime.now().strftime("%Y-%m-%d")
        entry_summary = self.summary if self.summary else (self.entry[:60] + "..." if len(self.entry) > 60 else self.entry)
        entry_block = f"\n## {today} — {entry_summary}\n\n{self.entry}\n"

        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(entry_block)
        except PermissionError:
            self._log_tool_warning(f"Permission denied appending to {file_path}")
            return f"Error: Permission denied writing to '{rel_path}'. Check file permissions."
        except OSError as e:
            self._log_tool_error(f"Error appending to {file_path}: {e}")
            return f"Error appending to '{rel_path}': {e}"

        self._log_debug(f"Appended entry to {rel_path}: {entry_summary}")
        return f"✅ Entry appended to **{domain_name}** (`{rel_path}`).\n\n**Summary:** {entry_summary}"

    def _mode_update(self, kb_root: Path) -> str:
        """Replace a section's content in a domain file."""
        if not self.domain:
            return "Error: `domain` parameter is required for update mode."
        if not self.section:
            return "Error: `section` parameter is required for update mode."
        if self.new_content is None:
            return "Error: `new_content` parameter is required for update mode."

        domain_name = self.domain.lower().replace(" ", "_")
        rel_path = DOMAINS.get(domain_name)
        if not rel_path:
            return (
                f"Error: Unknown domain '{self.domain}'. "
                f"Available domains: {', '.join(sorted(DOMAINS.keys()))}."
            )

        file_path = kb_root / rel_path
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

        # Locate the section header: "## <section>"
        section_header = f"## {clean_section}"
        section_start = content.find(section_header)

        if section_start == -1:
            # Section not found — append it at the end
            updated_content = content.rstrip() + f"\n\n{section_header}\n{self.new_content}\n"
            self._log_debug(f"Section '{clean_section}' not found, appending to end of {rel_path}")
            found = False
        else:
            # Section found — delete from header to next "## " or EOF, then insert new header + content
            # Find the next "## " header after the current one
            next_section = content.find("\n## ", section_start + len(section_header))
            if next_section == -1:
                # No next section, delete until EOF
                updated_content = content[:section_start] + f"{section_header}\n{self.new_content}\n"
            else:
                # Delete until next section
                updated_content = content[:section_start] + f"{section_header}\n{self.new_content}\n" + content[next_section:]
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

        # Read the "Current Status" from task_tracker
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
        for domain_name, rel_path in DOMAINS.items():
            file_path = kb_root / rel_path
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                continue

            # Find all ## YYYY-MM-DD entries
            import re
            for match in re.finditer(r"^## (\d{4}-\d{2}-\d{2})\s*[—\-–]\s*(.+)$", content, re.MULTILINE):
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

        return "\n".join(lines)
