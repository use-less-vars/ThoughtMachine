# ThoughtMachine Handbook

Welcome to ThoughtMachine! This guide covers core concepts, tool usage,
configuration, and best practices.

## Getting Started

ThoughtMachine is an AI agent framework that runs tasks using a set of
specialized tools. Start by configuring your LLM provider in `config.json`
(see Configuration section).

## Core Concepts

### Tools
The agent uses tools to interact with your project. Key tools include:
- `FileEditor` — Read, write, and edit files
- `ApplyEdits` — Apply search/replace edits with resilient matching
- `DockerCodeRunner` — Execute code in a secure Docker sandbox
- `KnowledgeBaseTool` — Store and retrieve persistent project notes
- `SearchCodebaseTool` — Semantic code search

### Sessions
Each interaction with the agent is part of a session. Sessions maintain
context history and can be configured with token limits and model settings.

### Knowledge Base
The project-level knowledge base lives in `.thoughtmachine/knowledge/`. Use
`KnowledgeBaseTool` to store architecture decisions, bug fixes, and
development guides.

## Configuration

Configuration is stored in `~/.thoughtmachine/config.json`. Key settings:
- `default_model` — The LLM model to use
- `max_tokens` — Maximum response tokens
- `temperature` — Model temperature (0.0–2.0)
- `tool_output_token_limit` — Max tokens for tool output

## Best Practices

1. Use `KnowledgeBaseTool mode=status` before starting new tasks
2. Log bugs and fixes to `bugs_and_fixes.md`
3. Use `mode=search` for targeted knowledge retrieval
