# ThoughtMachine Configuration Reference

## Configuration File

The main configuration lives at `~/.thoughtmachine/config.json`. It is a JSON file
that controls all aspects of the agent's behavior. If the file does not exist,
defaults from `resources/default_config.json` are used.

### Core Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | `"deepseek-v4-flash"` | LLM model name to use |
| `provider_type` | string | `"openai_compatible"` | Provider type: `openai_compatible`, `anthropic` |
| `provider_id` | string | `"v4_flash"` | Provider profile identifier |
| `base_url` | string | `"https://api.deepseek.com/v1/"` | API endpoint URL |
| `provider_config` | object | `{}` | Additional provider-specific settings |
| `temperature` | number | `1.0` | Model temperature (0.0–2.0) |
| `max_turns` | integer | `200` | Maximum turns per session |
| `detail` | string | `"normal"` | Response detail level: `normal` or `reduced` |
| `system_prompt` | string | `""` | Custom system prompt override |

### Token Monitoring

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `token_monitor_warning_threshold` | integer | `60003` | Token count to trigger warning |
| `token_monitor_critical_threshold` | integer | `75003` | Token count to trigger critical limit |
| `tool_output_token_limit` | integer | `10000` | Max tokens for tool output before truncation |

### Turn Monitoring

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `turn_monitor_enabled` | boolean | `true` | Enable turn limit enforcement |

### Logging

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enable_logging` | boolean | `true` | Master logging toggle |
| `log_dir` | string | `"./logs"` | Directory for log files |
| `log_level` | string | `"INFO"` | Console log level |
| `enable_file_logging` | boolean | `true` | Enable JSONL file logging |
| `jsonl_format` | boolean | `true` | Log in JSONL format |
| `log_categories` | array | `["SESSION", "LLM", "TOOLS"]` | Categories to log |

### File Operations

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_file_size_mb` | integer | `10` | Max file size for read operations |
| `max_backup_files` | integer | `5` | Max backup files for rotated logs |

### Workspace

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace_path` | string | `(project dir)` | The working directory for the agent |

### RAG (Codebase Indexing)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rag_enabled` | boolean | `false` | Enable RAG-based code search |
| `rag_embedding_model` | string | `"BAAI/bge-small-en-v1.5"` | Embedding model for vectors |
| `rag_chunk_size` | integer | `1500` | Chunk size for code splitting |
| `rag_chunk_overlap` | integer | `200` | Overlap between chunks |
| `rag_batch_size` | integer | `16` | Batch size for embedding |
| `rag_truncate_dim` | integer | `256` | Truncated embedding dimension |

### Knowledge Base

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kb_enabled` | boolean | `true` | Enable knowledge base feature |

### Enabled Tools

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled_tools` | array | (all tools) | List of tool names to make available |

---

## Environment Variables

Logging can be controlled via environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `TM_LOG_LEVEL` | Console minimum log level | `INFO` |
| `TM_LOG_TAGS` | Comma-separated tags to show on console | (empty = WARNING+) |
| `TM_LOG_FILE_LEVEL` | JSONL file minimum log level | `DEBUG` |
| `TM_LOG_DIR_MAX_MB` | Maximum total size of log directory | `50` |
| `TM_WORKSPACE` | Override workspace path | (not set) |
| `TM_NO_GUI` | Skip GUI startup | (not set) |
| `TM_UI_PORT` | Web UI port | `3000` |
| `TM_AGENT_PORT` | Agent WebSocket bridge port | `8765` |
| `TM_DISABLE_SUMMARY_SAVE` | Disable auto-summary save on shutdown | (not set) |

The environment variables `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are used for
LLM provider authentication. The agent also checks `~/.thoughtmachine/config.json`
for provider_config.api_key.

---

## Security Policy

Located at `~/.thoughtmachine/security_policy.json`.

```json
{
  "version": 1,
  "session_policy": {
    "read_only": false,
    "allowed_networks": [],
    "tool_overrides": {},
    "default_policy": "allow",
    "capability_requirements": {}
  },
  "agent_overrides": {}
}
```

| Field | Purpose |
|-------|---------|
| `session_policy.read_only` | If true, all write operations are blocked |
| `session_policy.allowed_networks` | CIDR ranges for network access |
| `session_policy.tool_overrides` | Per-tool override policies |
| `session_policy.default_policy` | `"allow"` or `"deny"` — default access rule |
| `session_policy.capability_requirements` | Required capabilities for tool execution |

Capability-aware security is partially implemented. In v1.0, `default_policy: "allow"`
means all tools are available without restriction. Future versions will enforce
explicit capability requirements.

---

## Provider Profiles

Provider profiles are defined in `agent/config/provider_profiles.py`. Each profile
specifies the provider type, model, context window, and endpoint. When you set
`provider_id` in config.json, the corresponding profile is loaded automatically.

### Built-in Profiles

| Profile ID | Provider | Model | Context |
|-----------|----------|-------|---------|
| `v4_flash` | OpenAI-compatible | deepseek-v4-flash | ~128K |
| `v4` | OpenAI-compatible | deepseek-v4 | ~128K |
| `deepseek_v3` | OpenAI-compatible | deepseek-chat | ~128K |
| `claude_35` | Anthropic | claude-3.5-sonnet | ~200K |
| `claude_35_haiku` | Anthropic | claude-3.5-haiku | ~200K |

### Custom Profiles

You can add custom provider profiles by editing the `PROVIDER_PROFILES`
dictionary in `agent/config/provider_profiles.py`. Each profile needs:

```python
"my_custom": ProviderProfile(
    provider_type="openai_compatible",
    model="my-model",
    max_input_tokens=128000,
    base_url="https://api.example.com/v1/",
)
```

---

## MCP Configuration

MCP (Model Context Protocol) servers are configured in the agent's config or
via environment variables. Each MCP server definition includes:
- `name`: Unique server identifier
- `transport`: `"stdio"`, `"http"`, or `"sse"`
- `command` (stdio): The command to start the server
- `args` (stdio): Command-line arguments
- `url` (http/sse): The server URL
- `api_key`: Optional API key for authentication

MCP servers are started lazily when the agent first uses them. They are
registered via `register_mcp_tools()` during agent initialization.
