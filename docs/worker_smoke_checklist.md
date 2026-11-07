# Worker Panel Smoke Test Checklist

## Prerequisites
- Backend running (e.g., `uvicorn web_ui.backend.server:app`)
- Frontend running (e.g., `npm run dev` in `web_ui/frontend/`)
- Browser open to the app with a workspace selected

## Step-by-Step

### 1. Main Agent Baseline
- [ ] Open a session
- [ ] Send a message, confirm main agent panel renders:
  - User message as chat bubble
  - Tool calls as collapsible blocks
  - Tool results paired with calls
  - Assistant response as markdown
  - Status dot indicating "thinking" / "done"

### 2. Spawn a Worker
- [ ] Use a tool that spawns a worker (e.g., `Worker` tool via the agent)
- [ ] Observe the worker panel appears in the sidebar/worker area
- [ ] Verify status shows "spawning" or "running"

### 3. Worker Lifecycle Events (via WebSocket)
- [ ] Worker transitions to "running" status
- [ ] Tool call block appears (collapsible, with tool name and arguments)
- [ ] Tool result appears underneath the tool call
- [ ] Assistant message appears as markdown-rendered text
- [ ] Any token warnings show as YELLOW/ORANGE banner (distinct from other notifications)

### 4. Duplicate Prevention
- [ ] No duplicate events in the panel
- [ ] Each event appears exactly once
- [ ] Event order is correct (tool_call before tool_result, etc.)

### 5. History Persistence
- [ ] Let the worker complete
- [ ] Verify all events remain visible in the panel
- [ ] Verify no messages were lost

### 6. Multiple Workers
- [ ] Spawn a second worker
- [ ] Confirm both worker panels are independent
- [ ] Events from worker 1 don't appear in worker 2's panel

## Browser DevTools Verification

### 7. Pause / Resume
- [ ] While a worker is running, trigger pause (via API or internal mechanism)
- [ ] Verify worker status transitions to "paused"
- [ ] Verify worker stops processing and waits
- [ ] While paused, trigger resume
- [ ] Verify worker status transitions back to "ready"
- [ ] Verify worker can process new queries after resume
- [ ] Verify paused worker can be stopped (stop overrides pause)
- [ ] Verify pause/resume works across WebSocket (status updates reflected in UI)


Open browser DevTools > Network > WS (WebSocket) and look for messages to `ws://localhost:8000/ws/{session_id}`.

Expected WebSocket message types for workers:

| WS Message Type | When Sent | Payload Shape |
|---|---|---|
| `worker:worker_spawned` | Worker thread starts | `{type, worker_name, session_id}` |
| `worker:worker_status` | Status changes (busy/ready) | `{type, worker_name, status, current_task?}` |
| `worker:tool_call` | Tool execution starts | `{type, worker_name, tool_name, arguments, tool_call_id}` |
| `worker:tool_result` | Tool returns | `{type, worker_name, tool_name, result, tool_call_id, success}` |
| `worker:assistant_message` | LLM responds | `{type, worker_name, content, reasoning_content?}` |
| `worker:worker_message` | Intermediate message | `{type, worker_name, content}` |
| `worker:system_notification` | Token/turn warning | `{type, worker_name, message, token_count?}` |
| `worker:worker_completed` | Worker finishes | `{type, worker_name, status}` |
| `worker:worker_error` | Worker crashes | `{type, worker_name, error}` |

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| Worker panel never shows | `worker:worker_spawned` not received | Check bridge.py subscriptions on global_event_bus |
| Tool calls not appearing | Per-worker bus subscription missing | Check `_subscribe_to_worker_bus` in bridge.py |
| Duplicate events | Events flowing through both global AND per-worker buses | Check duplicate prevention in bridge.py |
| Token warnings as plain text | Missing `is_system_notification` flag | Check adaptWorkerEvent.js token_warning handler |
| Status stuck on "running" | `WORKER_COMPLETED` not published | Check worker.py has global_event_bus.publish(WORKER_COMPLETED) |
