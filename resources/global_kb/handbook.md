# ThoughtMachine Handbook — Usage Guide & Best Practices

## Getting Started

### First Run
1. Create `~/.thoughtmachine/config.json` with your LLM provider settings
2. Set the `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` environment variable
3. Run the agent from your project directory
4. The agent will create `.thoughtmachine/knowledge/` on first use

### Basic Interaction
- Give natural language instructions
- The agent will use appropriate tools to help
- You can ask questions, request code changes, run analyses
- Sessions are saved automatically — you can resume later

---

## Core Concepts

### Sessions
Each conversation is a **session**. Sessions are:
- Saved to `~/.thoughtmachine/sessions/` as JSON files
- Given a unique ID (UUID v4) at creation
- Named with a human-readable slug
- Trackable via metadata files for fast listing
- Resumable — reload a previous session and continue

### Turns
Each exchange (user message → agent response) is a **turn**. Turn limits
prevent runaway conversations. Turn count includes tool calls within a
response cycle.

### Token Monitoring
The agent monitors token usage against the model's context window:
- **Warning threshold** (~60K tokens): advise to summarize
- **Critical threshold** (~75K tokens): countdown begins (default 5 turns)
- After countdown expires: only SummarizeTool and Respond available

### Knowledge Base (KB)
- **Workspace KB**: `.thoughtmachine/knowledge/` — project-specific notes
- **Global KB**: `~/.thoughtmachine/knowledge/` — user-wide notes
- **Self-KB**: `resources/global_kb/` — TM's knowledge about itself
- Domains: system_architecture, development_guides, bugs_and_fixes, etc.
- Use `KnowledgeBaseTool mode=status` before starting tasks

---

## Workflow Patterns

### Starting a Task
```
1. KnowledgeBaseTool mode=status           # Check current state
2. DirectoryTreeTool directory="."          # Understand project layout
3. FilePreviewTool filename="relevant.py"   # Quick file overview
4. FileSearchTool pattern="thing"           # Find relevant code
5. Begin work with ApplyEdits or CodeModifier
```

### Investigating a Bug
```
1. GitInfoTool operation=log max_count=5   # Recent changes
2. FileSearchTool pattern="error message"   # Find related code
3. SearchCodebaseTool "feature description" # Semantic search
4. DockerCodeRunner command="python test"   # Reproduce
5. Fix with ApplyEdits
6. KnowledgeBaseTool mode=append domain=bugs_and_fixes
```

### Making Changes
```
1. Understand the code (FileSearchTool / SearchCodebaseTool)
2. Plan the change (Thought tool)
3. Apply changes (ApplyEdits — prefer batch/file_pattern)
4. Preview if uncertain (ApplyEdits preview=True)
5. Test (DockerCodeRunner)
6. Log learning (KnowledgeBaseTool append)
```

### Researching Architecture
```
1. DirectoryTreeTool max_depth=1            # Top-level structure
2. FileSummaryTool for key files            # Structural analysis
3. SearchCodebaseTool "class X"             # Find implementations
4. FileSearchTool pattern="import.*X"       # Find usage
5. Document in KnowledgeBase
```

---

## Best Practices

### Tool Selection
- **ApplyEdits** first for code changes (supports regex, batch, preview)
- **CodeModifier** for Python structural changes (AST-based)
- **RefactorTool** for cross-file Python changes
- **DockerCodeRunner** for running code (not raw shell if Docker available)
- **SearchCodebaseTool** for semantic understanding (not just text search)

### Efficiency
- Batch operations when possible (ApplyEdits file_pattern)
- Read specific lines, not entire files (FileEditor line_numbers)
- Preview before writing (ApplyEdits preview=True)
- Use ProgressReport during long batch jobs
- Summarize when approaching token limits

### Knowledge Discipline
- Check `mode=status` before starting every task
- Log bugs to `bugs_and_fixes.md` immediately after resolving
- Update `system_architecture.md` when design decisions are made
- Add to `lessons_learned.md` when discovering reliable patterns
- Use `append_section` parameter for targeted updates

### Conversation Management
- Use SummarizeTool proactively at the warning threshold
- Store intermediate results in KnowledgeBase before summarizing
- Keep the most recent 5-10 turns when summarizing
- Use questions for tasks needing user input

### Security Awareness
- Docker sandbox has no network by default
- `pip install --user` requires writable home in security policy
- Never store API keys in session files
- Read-only sessions can be set via security policy

---

## Common Patterns

### "Read this file and tell me what it does"
Use: `FilePreviewTool` + `FileSummaryTool` + `SearchCodebaseTool`

### "Find where this function is defined"
Use: `FileSearchTool pattern="def function_name"` + context_lines

### "Add error handling to all these files"
Use: `ApplyEdits file_pattern="*.py"` with regex for the batch

### "Run my tests"
Use: `DockerCodeRunner command="python -m pytest"` or similar

### "Save this information for later"
Use: `KnowledgeBaseTool mode=append domain=development_guides`

### "How do I configure the model?"
Check: `resources/global_kb/configuration.md`

---

## LLM Provider Setup

### OpenAI-Compatible (DeepSeek, StepFun, etc.)
```json
{
  "base_url": "https://api.deepseek.com/v1/",
  "model": "deepseek-v4-flash",
  "provider_type": "openai_compatible",
  "provider_id": "v4_flash",
  "provider_config": {
    "api_key": "sk-..."  // or set OPENAI_API_KEY env var
  }
}
```

### Anthropic (Claude)
```json
{
  "provider_type": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "provider_id": "claude_sonnet_4"
}
```
Set `ANTHROPIC_API_KEY` environment variable.
