# Troubleshooting Guide

## Agent Startup Issues

### "No module found" errors on import
- Ensure you're in the project directory or have `PYTHONPATH` set
- Run `pip install -e .` or set `PYTHONPATH=.`
- Check that Python 3.11+ is installed

### Agent fails to start
- Ensure `~/.thoughtmachine/config.json` exists and is valid JSON
- Verify LLM provider credentials are correct (API key, base URL)
- Check the provider model name matches what the API expects
- Run `python -c "from thoughtmachine.bootstrap import ensure_user_defaults; ensure_user_defaults()"`
- Check logs in `./logs/` for detailed error information

### Agent starts but immediately fails with API error
- Verify your API key is set (env var or config)
- Check that the provider base URL is correct
- Ensure you have credits/quota with the provider
- Try a different model/provider profile

## Configuration Issues

### Config changes not taking effect
- Restart the agent after changing `~/.thoughtmachine/config.json`
- Check JSON syntax — trailing commas are not allowed
- In-memory config doesn't auto-reload; restart required

### Provider not found
- The `provider_id` must match a key in `PROVIDER_PROFILES`
- Check `agent/config/provider_profiles.py` for available profiles
- For custom providers, add a new profile entry

### "Unknown provider type" error
- Supported types: `openai_compatible`, `anthropic`
- Check the `provider_type` field in your config

## Session Issues

### Session not saving
- Ensure `~/.thoughtmachine/sessions/` directory is writable
- Check disk space
- Sessions are saved near the end of each turn

### Cannot resume a session
- Check that the session file exists in `~/.thoughtmachine/sessions/`
- Verify the session ID is correct
- Session files are named `{session_id}.json`

### "Current session not found" on restart
- The `.current_session` marker may be stale
- Start a new session or list available sessions
- Clean the marker file: `rm ~/.thoughtmachine/sessions/.current_session`

## Tool Issues

### ApplyEdits: "Find block not found"
- The `find` text must match the file content exactly
- Check whitespace, indentation, and line endings
- Try using `use_regex=True` for more flexible matching
- Use `FileEditor operation=read` to see exact content

### ApplyEdits: failed across multiple files
- Use `preview=True` first to see what would change
- Check that the pattern exists in all target files
- Individual file failures won't prevent other files from being edited

### DockerCodeRunner: "Docker not available"
- Ensure Docker is installed: `docker --version`
- Check Docker daemon is running: `systemctl status docker` or `dockerd`
- Ensure your user has permissions: `sudo usermod -aG docker $USER`
- Try rebuilding the image: set `build=True`

### DockerCodeRunner: "Permission denied"
- Your user may not be in the `docker` group
- Run `sudo usermod -aG docker $USER` and log out/in
- Alternatively, run the agent with `sudo` (not recommended)

### DockerCodeRunner: no network
- This is the default security policy
- To enable network: edit `~/.thoughtmachine/security_policy.json`
- Set `"allowed_networks": ["0.0.0.0/0"]` for full access

### KnowledgeBaseTool: domain not found
- Use `mode=list` to see available domains
- Create custom domains with `mode=create_domain`

## Token / Memory Issues

### "Token usage warning" message
- This is normal — the agent is approaching the context window limit
- Use SummarizeTool to prune older conversation turns
- Keep the last 5-10 turns and summarize the rest

### "Critical threshold reached" with countdown
- You have 5 turns before only SummarizeTool and Respond remain
- Stop the current task and summarize immediately
- After summary, you can continue normally

### LLM responses becoming incoherent
- May indicate context window overflow
- Summarize the conversation
- Reduce `tool_output_token_limit` in config

### Session file too large
- Use `history_pruner` which runs automatically during summarization
- Manually prune: use SummarizeTool more aggressively
- Check session file size: `ls -lh ~/.thoughtmachine/sessions/`

## Web UI Issues

### Web UI not loading in browser
- Check that the backend server started on the expected port (default: 3000)
- Verify no other process is using the port
- Check console output for server address
- Try accessing `http://localhost:3000` directly

### WebSocket connection failed
- Ensure the WebSocket bridge is running
- Check for firewall or proxy blocking WebSocket connections
- Verify the port (default: 8765) is accessible

## Qt GUI Issues

### Qt GUI not starting
- Ensure PyQt6 is installed:
  `pip install PyQt6`
- Check display server is available (not applicable in headless environments)
- Try running with `--no-gui` flag

## Logging Issues

### No log files created
- Ensure `enable_logging: true` in config
- Check that `log_dir` exists and is writable
- Verify `enable_file_logging: true`

### Too many logs
- Increase `log_level` to `WARNING` or `ERROR`
- Reduce enabled `log_categories`
- Set `log_dir_max_mb` limit (default 50MB)

## RAG Issues

### RAG search returns no results
- Ensure RAG is enabled: `rag_enabled: true`
- Index the codebase first: `python -m agent.knowledge.codebase_indexer index`
- Check that the vector database exists in `~/.thoughtmachine/rag_db/`

### RAG indexer fails
- Ensure tree-sitter language parsers are installed
- Check that `rag_embedding_model` is accessible (may require download)

## General Tips

- **Check logs first**: `tail -f logs/thoughtmachine.log` for detailed diagnostics
- **Reset configuration**: Delete `~/.thoughtmachine/config.json` and restart
- **Rebuild Docker image**: Use `build=True` in DockerCodeRunner for fresh sandbox
- **Verify Python path**: `python -c "import sys; print(sys.path)"`
- **Check file permissions**: Agent needs read/write to project directory
- **Kill stale sessions**: `rm -rf ~/.thoughtmachine/sessions/*.current*`
