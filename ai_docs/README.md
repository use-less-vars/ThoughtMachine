# AI Documentation Index

This directory contains meta-information and guidelines for AI agents interacting with this codebase.

## Quick Links

- **Tool Creation Guide**: [tool_creation_guide.md](tool_creation_guide.md) – How to create new tools for the agent
- **File Operations Guide**: [integration_guide.md](integration_guide.md) – How to use file operation tools including FileEditor
- **Tool Directory**: [../tools/](../tools/) – Existing tool implementations
- **Agent Core**: [../agent_core.py](../agent_core.py) – Main agent logic and system prompt
- **Documentation**: [../documentation/](../documentation/) – General project documentation

## Purpose

This folder serves as a reference point for AI agents to understand:
1. How tools are structured and how to create new ones
2. Best practices for file operations (prefer line-specific operations over full file rewrites)
3. Project architecture and conventions

## When to Consult This Directory

- When you need to create a new tool
- When you're unsure about the correct way to modify files
- When you need to understand the agent's tool ecosystem
- When looking for project-specific guidelines

## Important Guidelines

1. **File Operations**: Use the FileEditor tool for all file operations. When modifying existing files, prefer line-specific operations (insert, replace, delete) over rewriting entire files. This reduces the risk of errors with large files.
2. **Tool Creation**: Follow the schema defined in the tool creation guide.
3. **System Prompt**: The agent's behavior is guided by the system prompt in `agent_core.py` which references this directory.