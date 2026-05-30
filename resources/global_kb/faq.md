# Frequently Asked Questions

## General

**Q: What is ThoughtMachine?**
A: ThoughtMachine is an AI software engineering agent — it works in your project
directory to help write code, understand codebases, run commands, and manage
development workflows. Think of it as an AI pair programmer.

**Q: What makes it different from other AI coding tools?**
A: Key differentiators:
- **Full session persistence** — save and resume conversations
- **Secure sandbox** — Docker-based code execution with no network by default
- **Rich tool ecosystem** — 20+ specialized tools for file ops, analysis, git, etc.
- **Dual knowledge base** — project-local and user-global persistent storage
- **Multi-LLM support** — works with OpenAI, Anthropic, and any OpenAI-compatible API
- **RAG code search** — semantic understanding of your codebase
- **Web UI and Qt GUI** — multiple interface options

**Q: How do I change the LLM model?**
A: Edit `~/.thoughtmachine/config.json` and set `model` to your desired model name,
`provider_id` to a matching profile, and `provider_type` to `openai_compatible` or
`anthropic`.

**Q: How do I reset my configuration?**
A: Delete `~/.thoughtmachine/config.json` and restart the agent. Defaults from
`resources/default_config.json` will be used.

**Q: How do I update ThoughtMachine?**
A: Pull the latest code from the repository and restart the agent. Configuration
and sessions are preserved in `~/.thoughtmachine/`.

**Q: Can I use multiple LLM providers at once?**
A: Not simultaneously. The agent uses one provider per session. You can switch
by changing config and restarting.

## Configuration

**Q: Where is the config file?**
A: `~/.thoughtmachine/config.json`

**Q: Where are my sessions stored?**
A: `~/.thoughtmachine/sessions/` — each as a JSON file with unique ID.

**Q: How do I set up a custom LLM provider?**
A: Use `provider_type: "openai_compatible"` with your API base URL and key.
See `resources/global_kb/configuration.md` for details.

**Q: Can I use environment variables for config?**
A: Yes! API keys use standard env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).
Logging is controlled via `TM_LOG_*` variables. See configuration.md.

## Knowledge Base

**Q: What is the difference between `scope=workspace` and `scope=global`?**
A: `scope=workspace` accesses project-local knowledge (`.thoughtmachine/knowledge/`
in your project). `scope=global` accesses the user-wide global knowledge base at
`~/.thoughtmachine/knowledge/`.

**Q: What is `resources/global_kb/`?**
A: That's ThoughtMachine's knowledge about itself — used when it's working outside
its home project to answer questions about its own capabilities, architecture,
configuration, etc.

**Q: Can I create custom KB domains?**
A: Yes! Use `KnowledgeBaseTool mode=create_domain domain=my_topic` to create a new
domain file. The category is optional.

**Q: How do I search the knowledge base?**
A: Use `KnowledgeBaseTool mode=search query="your search term" scope=workspace`
(or `scope=global`).

**Q: What domains exist by default?**
A: system_architecture, development_guides, roadmap, bugs_and_fixes,
lessons_learned, task_tracker.

## Tools

**Q: Why did my edit fail?**
A: The ApplyEdits tool uses exact text matching by default. Ensure your `find`
text matches the source exactly, including whitespace and indentation. Use
`use_regex=True` for flexible matching.

**Q: How do I run Python code?**
A: Use `DockerCodeRunner` with `command='python3 -c "..."'` for one-liners or
`script` parameter for multi-line scripts. The code runs in a secure Docker sandbox.

**Q: Can the agent install pip packages?**
A: Yes! Use DockerCodeRunner with `command='pip install package_name'` or include
it in a multi-step script. This requires the Docker sandbox with writable home
(or the default policy allows it).

**Q: How do I search across many files?**
A: Use `FileSearchTool pattern="your_text" file_pattern="*.py"` for text search,
or `SearchCodebaseTool "what you're looking for"` for semantic search.

**Q: Can the agent make git commits?**
A: No. GitInfoTool is read-only — status, diff, log, blame, branch. No commits,
pushes, or merges.

**Q: How do I preview changes before applying them?**
A: Use `ApplyEdits` with `preview=True` to see the diff without writing. Also
works with `file_pattern` for batch previews.

## Sessions

**Q: Are sessions saved automatically?**
A: Yes — sessions are persisted to disk after each turn. You can resume any
previous session.

**Q: How do I resume a session?**
A: Start the agent with the session ID, or use the Web UI to select from the
session list.

**Q: Can I delete old sessions?**
A: Yes — delete the JSON file from `~/.thoughtmachine/sessions/`. Metadata files
(`_meta_*.json`) can also be cleaned up.

**Q: How long are sessions stored?**
A: Indefinitely — they're just JSON files on disk. You can archive or delete them
manually.

## Security

**Q: Is my code safe?**
A: Yes. DockerCodeRunner runs in an isolated container with:
- No network access by default
- Read-only root filesystem
- Dropped Linux capabilities
- Non-root user execution

**Q: Can the agent access my API keys?**
A: The agent reads them from config/env vars in its own process. They are not
written to session files or sent anywhere except the LLM provider API.

**Q: How do I make the agent read-only?**
A: Set `"read_only": true` in `~/.thoughtmachine/security_policy.json`.

## Performance

**Q: Why is the agent slow?**
A: Possible reasons:
- Large context window (summarize to reduce tokens)
- Complex tool operations (file search across many files)
- LLM provider latency
- Docker container startup time
- Try reducing `detail` to "reduced" in config

**Q: How can I speed things up?**
A: Use batch operations, read specific lines instead of whole files, use
FileSearchTool with targeted patterns, and summarize proactively.

**Q: What's the maximum file size the agent can read?**
A: Default is 10MB (`max_file_size_mb` in config). Larger files are truncated.

## Docker

**Q: Do I need Docker?**
A: No — Docker is optional. Without it, the agent can still edit files, search
code, use git, etc. Docker only enables code execution.

**Q: What Docker image does the agent use?**
A: It builds `agent-executor` from `docker/executor.Dockerfile` — a minimal
Python image with common packages.

**Q: How do I add packages to the Docker image?**
A: Edit `docker/requirements-docker.txt` and set `build=True` in DockerCodeRunner
to rebuild the image.

**Q: Can I enable network in Docker?**
A: Yes — edit `~/.thoughtmachine/security_policy.json` and set
`"allowed_networks": ["0.0.0.0/0"]` for full network access.

## Web UI

**Q: How do I start the Web UI?**
A: Run the agent with the web UI flag, or start the backend server directly.
The frontend is served as static files or runs via `npm run dev`.

**Q: What port does the Web UI use?**
A: Default is 3000 for the HTTP server, 8765 for the WebSocket bridge.
Configurable via `TM_UI_PORT` and `TM_AGENT_PORT`.

**Q: Can I use the Web UI remotely?**
A: Currently designed for local use. Remote access would require additional
security measures (TLS, authentication).

## Debugging

**Q: How do I see what the agent is thinking?**
A: The agent uses the `Thought` tool to record reasoning. You can see these in
the conversation. Enable `detail: "normal"` for full reasoning.

**Q: How do I enable more logging?**
A: Set `TM_LOG_LEVEL=DEBUG` environment variable, or set `log_level: "DEBUG"`
in config.

**Q: How do I report a bug?**
A: The agent itself can log bugs to the KB (`bugs_and_fixes` domain). For code
bugs, provide the error message, stack trace, and steps to reproduce.
