# Troubleshooting Guide

## Common Issues

### Agent fails to start
- Ensure `~/.thoughtmachine/config.json` exists and is valid JSON
- Check that your LLM provider credentials are correctly configured
- Run `python -c "from thoughtmachine.bootstrap import ensure_user_defaults; ensure_user_defaults()"`

### Knowledge Base files missing
- If `.thoughtmachine/knowledge/` doesn't exist, the agent creates it
  automatically on first `KnowledgeBaseTool` call
- Custom domains created via `mode=create_domain` are listed via `mode=list`

### Docker sandbox not working
- Ensure Docker is installed and the daemon is running
- Check that the user has permission to run Docker containers
- Try rebuilding the executor image: set `build=True` in DockerCodeRunner

### Memory / token issues
- Use `SummarizeTool` to prune conversation history
- Set `tool_output_token_limit` in config to limit tool output
- Use `mode=search` instead of reading large KB files
