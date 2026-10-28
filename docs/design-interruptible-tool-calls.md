# Design: Interruptible Tool Calls

## Problem
Currently, a blocking tool call (e.g., `host_bash` running a 10-minute build) cannot be interrupted. The agent's `process_query` generator yields to `execute_tool_calls`, which blocks the thread. Pause and Stop signals are ignored until the tool finishes.

## Proposed Architecture (The "Thin Layer")
We need a layer between the agent loop and the tool executor that allows the main thread to monitor the `halt_check` while the tool runs.

1. **Tool Wrapper:** `execute_tool_calls` should run the actual tool in a separate thread (or sub-agent).
2. **Shared State:** The main agent loop continues yielding heartbeats and polling `halt_check`.
3. **Cancellation:** If `halt_check` becomes `True`, the main agent emits a cancellation signal to the tool thread. The tool thread attempts a graceful shutdown (e.g., sending `SIGTERM` to the Docker container).
4. **Return:** The partial output is captured and returned as the tool result.

This design is heavily coupled to Gen 52's `docs/design-streaming-canonical-ir.md` §5 (Cancellation).
