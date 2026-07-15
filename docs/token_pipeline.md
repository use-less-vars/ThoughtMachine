# Token Pipeline — Debug Trace Standard

## Purpose

The `[TOKEN_PIPELINE]` marker is the **single canonical debug trace** for all token/context/state-sync events flowing from the LLM backend through to the frontend UI components. It replaces the legacy `[STATE_SYNC_TRACE]`, `[TRACE:tokens_updated]`, `[TRACE:context_updated]`, `[TRACE:handleWorkerEvent]`, and `DIAG: Trace` comment markers.

All `[TOKEN_PIPELINE]` log entries use `log('DEBUG', 'pipeline.*', ...)` in Python and `console.log('[TOKEN_PIPELINE]', ...)` in the frontend.

---

## Architecture

```
┌──────────────┐   emit_tokens_updated / emit_context_updated / emit_state_sync
│   Worker     │ ──────────────────────────────────────────────────────────────►
│  (worker.py) │   forward_agent_event (agent_responded, tool_call, etc.)
└──────┬───────┘
       │ Per-worker EventBus
       ▼
┌──────────────┐   Per-worker bus handler    ┌──────────────────┐
│   Bridge     │ ───────────────────────────►│  WebSocket       │
│ (bridge.py)  │   _on_worker_spawned         │  (SessionTab)   │
│              │   _forward_worker_event      └────────┬─────────┘
└──────────────┘                                       │
                                                        │ tokens_updated / context_updated / worker_state_sync
                                                        ▼
                                               ┌──────────────────┐
                                               │   SessionTab     │──► handleWorkerEvent ──► App.jsx
                                               │  (WS handler)    │
                                               └──────────────────┘
                                                        │
                                                        │ Forwarded to worker panel
                                                        ▼
                                               ┌──────────────────┐
                                               │ WorkerOutputPanel │
                                               │  (display logic)  │
                                               └──────────────────┘
```

---

## Marker Locations

### Python Backend

| File | Count | Events Traced |
|------|-------|---------------|
| `tools/workspace/worker.py` (WorkerBusAdapter) | 15 | `emit_tokens_updated`, `emit_context_updated`, `emit_status_message`, `emit_error_occurred`, `emit_config_changed`, `emit_conversation_changed`, `emit_state_sync`, `forward_agent_event` (all subtypes: agent_responded, tool_call, tool_result, token_warning, turn_warning, time_warning, assistant_message, system_notification) |
| `web_ui/backend/bridge.py` | 6 | Global bus handler, `_on_worker_spawned`, `worker_state_sync` send, per-worker bus handler, `_forward_worker_event` |

**Total Python markers: 21** (20 log calls + 1 comment marker)

### Frontend (JSX/JS)

| File | Count | Events Traced |
|------|-------|---------------|
| `web_ui/frontend/src/App.jsx` | 4 | `handleWorkerEvent` called, DROPPED, DEDUPED, stored event |
| `web_ui/frontend/src/components/WorkerOutputPanel.jsx` | 4 | `worker_state_sync` received, `tokens_updated` mapped, `context_updated` mapped, `worker_state_sync` mapped |
| `web_ui/frontend/src/components/SessionTab.jsx` | 5 | `tokens_updated` arrived, forwarding `tokens_updated`, `context_updated` arrived, forwarding `context_updated`, `worker_state_sync` received |

**Total frontend markers: 13**

**Grand total: 34 `[TOKEN_PIPELINE]` markers across 6 files.**

---

## Event Flow Details

### 1. Worker Emission (`worker.py` — WorkerBusAdapter)

The `WorkerBusAdapter` class emits events from the worker process to the per-worker EventBus. Each `emit_*` method and the `forward_agent_event` dispatcher logs a `[TOKEN_PIPELINE]` trace before publishing.

**Key events:**
- **`emit_tokens_updated`**: Published when token counts change. Payload: `{total_input, total_output}`.
- **`emit_context_updated`**: Published when context length changes. Payload: `{context_length}`.
- **`emit_state_sync`**: Published on explicit state sync. Payload: `{context_length, token_state, warning_message, critical_threshold}`.
- **`forward_agent_event`**: Dispatches agent lifecycle events (`agent_responded`, `tool_call`, `tool_result`, `token_warning`, `turn_warning`, `time_warning`, `assistant_message`, `system_notification`).

### 2. Bridge Relay (`bridge.py`)

The bridge subscribes to the per-worker EventBus and relays events to the frontend via WebSocket.

**Key trace points:**
- **Global bus handler**: Logs all events arriving on the global bus, filtered by session.
- **`_on_worker_spawned`**: Logs when a new worker is spawned, including bus availability.
- **`worker_state_sync` send**: Logs the full `worker_state_sync` payload before sending over the WebSocket.
- **Per-worker bus handler**: Logs every event forwarded from the per-worker bus to the frontend.
- **`_forward_worker_event`**: Logs when events are forwarded individually (legacy path).

### 3. Frontend Reception

#### SessionTab.jsx (WebSocket handler)
Receives raw events from the bridge WebSocket and dispatches them:
- **`tokens_updated`**: Updates token counters (`tokensIn`/`tokensOut`) and forwards to worker panel.
- **`context_updated`**: Updates `contextLength` and forwards to worker panel.
- **`worker_state_sync`**: Received via `worker:worker_state_sync` prefixed events (belt-and-suspenders path).

#### App.jsx (Global event store)
Central event deduplication and storage:
- **`handleWorkerEvent` called**: Entry point for all worker events.
- **DROPPED**: When `sessionId` is falsy (event discarded).
- **DEDUPED**: When an event with the same canonical type + timestamp already exists.
- **Stored event**: Logged after successful storage (with count).

#### WorkerOutputPanel.jsx (Display component)
Maps raw events to display-ready objects:
- **`worker_state_sync received`**: Logs the full state sync payload.
- **`tokens_updated` mapped**: Logs when a `tokens_updated` event is mapped for display.
- **`context_updated` mapped**: Logs when a `context_updated` event is mapped for display.
- **`worker_state_sync` mapped**: Logs when a `worker_state_sync` event is mapped for display.

---

## Adding New Trace Points

When adding new trace points for token/context/state-sync events:

### Python
```python
log('DEBUG', 'pipeline.<component>',
    f"**[TOKEN_PIPELINE]** <Component>.<method>: "
    f"key1={value1} key2={value2}")
```

### Frontend
```javascript
console.log('[**TOKEN_PIPELINE**] <Component>: <description>', { key1: value1, key2: value2 })
```

**Rules:**
1. Always use the `[TOKEN_PIPELINE]` prefix (bolded with `**` in Python, or `**` around it in JS).
2. Include enough context to trace the event end-to-end (worker_name, session_id, counts).
3. Keep log level at `DEBUG` in Python; use `console.log` in JS.
4. Do **not** use `[STATE_SYNC_TRACE]`, `[TRACE:*]`, or `DIAG: Trace` — these are legacy and deprecated.

---

## Verification Commands

To verify no legacy traces remain:

```bash
# Check for legacy STATE_SYNC_TRACE (should return 0)
grep -r "STATE_SYNC_TRACE" --include="*.py" .
grep -r "STATE_SYNC_TRACE" --include="*.jsx" --include="*.js" .

# Check for legacy TRACE: markers (should return 0)
grep -r "\[TRACE:" --include="*.py" .
grep -r "\[TRACE:" --include="*.jsx" --include="*.js" .

# Check for DIAG: Trace comments (should return 0)
grep -r "DIAG: Trace" --include="*.py" .

# Count active TOKEN_PIPELINE markers
grep -r "TOKEN_PIPELINE" --include="*.py" . | wc -l
grep -r "TOKEN_PIPELINE" --include="*.jsx" --include="*.js" . | wc -l
```

---

## Migration History

| Date | Change |
|------|--------|
| Phase 1 | Replaced `[STATE_SYNC_TRACE]` → `[TOKEN_PIPELINE]` in bridge.py, WorkerOutputPanel.jsx, SessionTab.jsx |
| Phase 2 | Replaced remaining `[STATE_SYNC_TRACE]`, `[TRACE:tokens_updated]`, `[TRACE:context_updated]`, `[TRACE:handleWorkerEvent]`, and `DIAG: Trace` comments → `[TOKEN_PIPELINE]` across all 6 files (21 Python + 13 frontend = 34 markers) |
