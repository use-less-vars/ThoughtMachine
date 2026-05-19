# Bugs and Fixes

## Thread Dying Silently on Agent Creation Failure

**Date**: 2025-04-16

**Symptom**: When `AgentController.start()` is called, the background thread dies silently if the `Agent` constructor raises an exception. This causes `_running=False` and `thread alive=False` before `continue_session` is called, leaving the controller in a dead state with no way for the GUI to recover gracefully.

**Root Cause**: In `agent/controller/__init__.py`, the `_run()` method wrapped both agent creation (lines 298-313) AND the main processing loop (lines 315-363) inside a single `try/except/finally` block. If the `Agent()` constructor threw (e.g., due to invalid config, LLM client initialization failure, etc.), the except block would set `_running=False`, the finally block would also set it, and the thread would die silently.

**Fix**:
1. Separated agent creation into its own `try/except` block before the main processing loop
2. If agent creation fails, `self.agent` is set to `None`, an error event (`AGENT_CREATION_ERROR`) is emitted to the GUI, and the thread stays alive
3. Added an "agentless idle loop" inside the main loop that handles the `self.agent is None` case - it waits for `[RESET]` or `[STOP]` commands to break out cleanly
4. Added a check in `continue_session()` that raises a clear error if `self.agent is None`
5. Added explicit logging (`'_run: Agent created successfully'`) to help diagnose future agent creation issues

**Files Modified**: `agent/controller/__init__.py`

**Related**: Also discovered that `FileEditor(operation='read', filename='...')` returns only partial content for large files. Workaround: use `FilePreviewTool` or `FileSearchTool` with `filenames` parameter as a list.
