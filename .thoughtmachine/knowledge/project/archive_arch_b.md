# Archive — Architecture & Guides B (system_architecture 2026-07-16 → 2026-07-31; development_guides stale; roadmap completed)
Archived 2026-08-16 from .thoughtmachine/knowledge/project/{system_architecture,development_guides,roadmap}.md during KB hygiene restructure.
## SOURCE: system_architecture.md — dated sections 2026-07-16 → 2026-07-31
## 2026-07-17 — 2026-07-17 — Worker-Level Pause/Resume Implementation

Th...

## 2026-07-17 — Worker-Level Pause/Resume Implementation

The worker-level pause/resume system wraps the agent-level `request_pause()` in a full lifecycle managed by `WorkerThread` (`tools/workspace/worker.py`):

**Two-layer signalling:**
1. **In-memory fast path:** `threading.Event` objects (`_pause_event`, `_resume_event`) for instant signalling within the same process
2. **Cross-process file path:** `command.json` with `{"action":"pause"}` or `{"action":"resume"}` for web UI → worker communication

**Checkpoints in the worker run loop (`run()`):**
- After `_run_tool_loop()` returns, checks `_pause_event.is_set()`
- If paused: saves context, sends response, blocks in `_resume_event.wait(1.0)` loop
- If stopped during pause: breaks outer loop (stop wins over pause)
- If resumed: sets status to "ready", clears resume event, continues loop

**API endpoints** in `workspace_routes.py`:
- `POST .../pause` — writes command.json + status.json, calls `thread.pause()`
- `POST .../resume` — writes command.json + status.json, calls `thread.resume()`

The full lifecycle is documented in the "Cooperative Pause/Resume" section of this KB.


## Q8: Error Handling

**File: agent/core/agent.py (process_query lines ~1020-1081)**
- Catches: `ProviderError`, `RateLimitExceeded`, `LLMError`, generic `Exception`
- Each yields an 'error' event dict with `error_type`, `message`, `traceback`, and `stop_reason='error'`
- Rate limiting: Exponential backoff (`rate_limit_backoff_factor=1.2`, max 60s). After all retries exhausted, yields 'stop_reason' with `stop_reason='rate_limit'`.
- Emergency retries: On token limit exceeded, the turn loop can retry (up to `_emergency_retries` limit).

## Q9: Conversation History Management

**File: agent/core/turn_transaction.py — TurnTransaction**
- Atomic commits: Messages are collected in a transaction during a turn.
- `commit_assistant_only()`: Commits assistant message to user_history before yielding events.
- `commit()`: Commits all messages (assistant + tool results) atomically.
- This ensures data is never lost even if consumer pauses between yielded events.

**File: agent/core/agent.py**
- `_add_to_conversation(msg)`: Adds message to conversation via `conversation_manager.add_message_to_session()`.
- Session's `user_history` (or WorkerContext's `user_history`) is the source of truth.
- ContextBuilder is used to build the LLM context window from conversation history.

## Q10: Summarization and Compaction

**File: tools/summarize_tool.py — SummarizeTool**
- Triggered when token state becomes CRITICAL (or explicitly by LLM).
- SummarizeTool generates a summary message with `'summary': True` and a context-cleared notification.
- After summarization, `WorkerContext.compact_after_summary()` prunes old messages.

**File: agent/core/worker_context.py (compact_after_summary)**
- Finds the last summary message in user_history.
- Preserves leading system prompts (role='system' before first non-system message).
- Removes all messages between the system prompts and the summary.
- Keeps the summary + all messages after it.
- Updates conversation_hash and conversation_version.

## Q11: Test Coverage

**File: tests/test_worker_agent_transplant.py (6 test classes)**
1. `TestSmokeMultiTurnTask` — Agent runs multiple turns with tool calls + final response
2. `TestResumeWorkerContinuesConversation` — Sequential process_query() calls preserve history in WorkerContext
3. `TestTimeoutEnforcesRestrictions` — Short timeout triggers CRITICAL time_state, soft restriction
4. `TestTokenCriticalTriggersSummarisation` — Low token threshold triggers CRITICAL token state
5. `TestStopFlagGracefulExit` — stop_check stops agent mid-execution
6. `TestGateDenialInstant` — Security gate denies via NullEventBus instantly
7. `TestCompactAfterSummary` (7 sub-tests) — WorkerContext.compact_after_summary() behavior

**Mock Strategy:** Uses `ScriptedProvider` (a custom `LLMProvider` subclass registered in `ProviderFactory`) that returns pre-configured `LLMResponse` objects. Monkey-patches `ScriptedProvider.__init__` to inject responses.

## Q12: Findings and Recommendations

### Strengths
1. **Clean separation**: Generator pattern (process_query yielding dicts) cleanly separates agent execution from event consumption.
2. **Well-tested**: 6 comprehensive test classes with ScriptedProvider mock covering all key scenarios.
3. **Dual event paths**: Generator yield for lifecycle + EventBus for security prompts is well-designed.
4. **Atomic commits**: TurnTransaction prevents data loss on pause.
5. **State machine design**: Independent token/time/turn states with restriction logic is robust.
6. **NullEventBus.ask()**: Returns "deny" instantly — correct for worker context.
7. **Compaction logic**: Well-tested with edge cases (multiple summaries, empty history, hash/version updates).

### Potential Issues
1. **EventBus default**: Agent.__init__ defaults `event_bus=None`, then uses `global_event_bus` (a real EventBus). Worker callers must explicitly pass `NullEventBus()`. If forgotten, security prompts will hang waiting for GUI response.
2. **Worker code location**: There is NO `worker.py` or dedicated WorkerThread file in the codebase. WorkerThread must be defined elsewhere or not yet migrated. The tests demonstrate the expected interface but the actual worker implementation may be in the `thoughtmachine` package (external to this project).
3. **_pending_events field**: AgentState has `_pending_events: List[Dict[str, Any]] = field(default_factory=list)` but this list is never populated or consumed in the code examined — dead code.
4. **Token estimation**: Process_query uses `self._estimate_tokens()` for system notifications/warnings added to conversation, which is rough but acceptable for tracking.
5. **Session vs WorkerContext property differences**: WorkerContext uses plain `user_history: list`, while Session wraps it in `ObservableList`. The conversation setter in Agent may call `session.user_history[:] = ...` which works for both types but WorkerContext lacks the change notification that ObservableList provides.

### Architectural Summary

```
WorkerThread (external)
    │
    │  creates
    ▼
WorkerContext(session_id) ──► Agent(config, session=ctx, event_bus=NullEventBus())
    │                              │
    │  process_query(query)        │  __init__ creates:
    │  yields event dicts ─────────┼──► ToolExecutor(tool_classes, config, state, event_bus=NullEventBus)
    │                              │       │
    │                              │       └──► Security Gate (via EventBus.ask() → "deny")
    │                              │
    │                              ├──► LLMClient → LLMProvider
    │                              ├──► ConversationManager
    │                              ├──► TokenCounter
    │                              └──► AgentState (token/time/turn state machines)
    │
    ▼
WorkerThread iterates events, handles responses
```

## NullEventBus

## 2026-07-16 — Event Pipeline Complete Trace — 5 Event Types

### Key So...

## Event Pipeline Complete Trace — 5 Event Types

### Key Source Files Analyzed:
- `agent/events.py` — All event types, EventBus, create_event(), event_class_map
- `agent/core/state.py` — AgentState, update_token_state() with WARNING/CRITICAL/LOW transitions, update_turn_state()
- `agent/core/agent.py` — process_query(), _update_tokens_after_tool(), _handle_state_event(), _create_token_update_event(), _apply_summary_pruning()
- `agent/presenter/event_processor.py` — process_event() with all sub-processors, emit_* methods
- `agent/presenter/state_bridge.py` — StateBridge with context_length/token tracking
- `tools/workspace/worker.py` — WorkerThread._run_tool_loop(), WorkerBusAdapter (all emit_* and forward_agent_event), WorkerSessionLifecycle
- `web_ui/backend/bridge.py` — _make_bus_handler(), _subscribe_to_worker_bus(), WebSocket forwarding
- `web_ui/frontend/src/components/chat/adaptWorkerEvent.js` — Frontend event dispatch switch

### Architecture Summary:
Agent.process_query() is a generator that yields event dicts. These are consumed by either:
- **Main agent path**: WebAgentBridge._agent_task() in bridge.py iterates process_query() and dispatches each event to WebSocket callbacks
- **Worker path**: WorkerThread._run_tool_loop() in worker.py iterates process_query() and forwards selected events via WorkerBusAdapter → per-worker EventBus → bridge._make_bus_handler() → WebSocket

### Insights:
- Warnings are **buffered** in _pending_warning_events and flushed after turn_transaction.commit() so they appear chronologically after tool results and before the next assistant message
- Worker events flow through **two parallel bus mechanisms**: the global EventBus (lifecycle events like WORKER_SPAWNED, WORKER_STATUS) and per-worker EventBus (detailed events like tool_call, token_warning)
- The bridge deduplicates worker:context_updated events by comparing formatted display strings (e.g., "12.3K")
- Worker-sourced token warnings are only forwarded via per-worker bus (Path A); global bus handler (Path B) skips source="worker" to avoid duplicates

## 2026-07-17 — Cooperative Pause/Resume — Full Codebase Analysis

### Ke...

## Cooperative Pause/Resume — Full Codebase Analysis

### Key Files
- **`tools/workspace/worker.py`** (2140 lines): WorkerThread class (lines 545-1420)
  - `__init__()` (560-624): Has `self._stop_event = threading.Event()` at line 624
  - `stop()` (716-726): Sets `_stop_event`, writes `command.json` with `{"action": "stop"}`, unblocks `_input_queue`
  - `_poll_command()` (728-750): Checks `command.json` for `"action": "stop"`, sets `_stop_event`, unblocks queue
  - `_run_tool_loop()` (843-996): Polls `_poll_command()` on each event, checks `_stop_event.is_set()`, calls `self._agent.request_pause()`, breaks
  - `run()` (1000-1277): Main loop - polls command before blocking on input queue, creates Agent lazily, processes queries. On exception sets `status="error"`. On else/normal exit sets `status="completed"`. No pause handling yet.
  - `status` field: `"ready" | "busy" | "completed" | "error"` (line 599)
  
- **`web_ui/backend/workspace_routes.py`** (659 lines): REST API
  - `stop_worker()` (580-658): Finds worker dir, writes `command.json` with `{"action": "stop"}`, immediately writes `status.json` with `runtime_status: "completed"`, fast-path signals in-memory thread via `thread.stop()`

- **`web_ui/backend/PAUSE_PROPAGATION_DESIGN.md`**: Existing design doc (file-based approach)

### Design Decision: threading.Event-based Approach
Instead of file-based `command.json` approach (which relies on 2-second polling latency), use a dedicated `threading.Event` for pause signalling:
- Add `self._pause_event = threading.Event()` to `__init__()` - clean, instant, race-condition-free
- `_poll_command()`: handle `"action": "pause"` → set `_pause_event` instead of `_stop_event`
- New `pause()` method: set `_pause_event`, write `command.json` for cross-process, unblock `_input_queue`
- New `resume()` method: clear `_pause_event`, write status as `"ready"`
- `_run_tool_loop()`: check both `_pause_event` and `_stop_event`
- `run()`: preserve `"paused"` status in except/else blocks
- API endpoint: `POST /{ws_id}/workers/{name}/pause` and `POST /{ws_id}/workers/{name}/resume`


## 2026-07-17 — 2026-07-17 — Implementation Details (Post-Review)

### Wo...

## 2026-07-17 — Implementation Details (Post-Review)

### WorkerThread Changes (`tools/workspace/worker.py`)

**New fields** (all `threading.Event`):
- `self._pause_event = threading.Event()` — set when pause is requested, cleared on resume
- `self._resume_event = threading.Event()` — set on resume, cleared on pause

**New methods:**
- `pause()` — sets `_pause_event`, clears `_resume_event`, writes `{"action":"pause"}` to `command.json`, unblocks `_input_queue` so the run loop can detect the event
- `resume()` — clears `_pause_event`, sets `_resume_event`, sets `self.status = "ready"`, writes status file

**Modified `_poll_command()`** — now handles `"action": "pause"` (same pattern as stop):
  ```python
  elif action == "pause":
      cmd_path.unlink(missing_ok=True)
      self._pause_event.set()
      self._input_queue.put(None)
  ```

**Modified `_run_tool_loop()` (line ~947):** — after the tool loop body, checks `_pause_event.is_set()`:
  1. Calls `self._agent.request_pause()` to signal the agent to yield
  2. Sets `self.status = "paused"`
  3. Writes status file (optimistic UI)
  4. Publishes `worker_paused` event via `_publish_event()`
  5. Returns a pause response JSON to the caller

**Modified `run()` (line ~1222):** — after `_run_tool_loop` returns, if paused:
  1. Preserves `"paused"` status (does NOT overwrite with `"ready"`)
  2. Writes status file
  3. Publishes `worker_paused` event to per-worker AND global event bus
  4. Saves conversation context via `_save_context()`
  5. Sends pause response to `_output_queue`
  6. **Blocks** in a wait loop: `while self._pause_event.is_set() and not self._stop_event.is_set(): self._resume_event.wait(1.0)`
  7. If stopped during pause → loop breaks
  8. If resumed → sets `status = "ready"`, clears `_resume_event`, publishes `worker_resumed` event, continues outer loop

**When NOT paused:** existing flow unchanged (status → `"ready"`, saves context, sends reply, continues loop)

### API Endpoints (`web_ui/backend/workspace_routes.py`)

**`POST /api/workspace/{ws_id}/workers/{name}/pause`** (line 663):
  - Resolves worker directory in both `session/{session_id}/workers/{name}/` and `workers/{name}/` layouts
  - Atomic-writes `{"action":"pause"}` to `command.json`
  - Immediately writes status.json with `runtime_status: "paused"` (optimistic UI)
  - Fast path: if thread found in registry, calls `thread.pause()` directly
  
**`POST /api/workspace/{ws_id}/workers/{name}/resume`** (line 741):
  - Same directory resolution
  - Atomic-writes `{"action":"resume"}` to `command.json` (for consistency; resume uses in-memory path)
  - Immediately writes status.json with `runtime_status: "ready"`
  - Fast path: calls `thread.resume()` directly

### Status Values
The worker status enum now supports: `"ready" | "busy" | "paused" | "completed" | "error"`

### Lifecycle Flow
```
                     pause()
ready ──── query ────► busy ────► paused ──── resume() ────► ready
                        │                                      ▲
                        └── stop() ────► completed              │
                        └── error ────► error                   │
                                                                └── spawn again
```
- Pause is *cooperative*: the agent finishes its current tool call/turn before yielding
- While paused, the worker blocks in the `run()` loop's wait loop, checking `_resume_event` every 1s
- Stop takes priority: if stop is requested while paused, the break condition exits the wait loop
- Context is saved before entering the pause wait, so paused state survives process restart
- Cross-process signalling via `command.json` enables pause/resume from the web UI


## 2026-07-17 — Chunk 1 — Forensics Complete: Pause & Stop Button Deep Di...

## Chunk 1 — Forensics Complete: Pause & Stop Button Deep Dive

### Q1: Main Panel Pause Button — End-to-End Trace

**START → QueryBar.jsx (lines 23–65)**
- `handleToggle()` called when user clicks "⏸ Pause" button (rendered at line 102 when `status === 'RUNNING'`)
- Line 55: `sendCommand('pause_session', {})` — empty object payload

**WebSocket transport:**
- QueryBar's `sendCommand` prop is passed down from SessionTab
- SessionTab owns a session-scoped WebSocket to `ws://host:8000/ws`
- sendCommand serializes to JSON `{"command": "pause_session"}` and sends via the WS

**server.py handler (lines 548–552):**
```python
elif command == "pause_session":
    if bridge is not None:
        bridge.pause()
        await ws.send_json({"type": "status_message", "text": "⏸ Paused."})
```

**bridge.py — WebAgentBridge.pause() (lines 1056–1073):**
Three actions:
1. **V2 Controller**: `self._controller.pause()` if controller exists
2. **Legacy agent**: `self._agent.request_pause()` + clears `_pause_event`
3. **ALL session workers**: Iterates `_worker_registry` for matching `session_id`, calls `thread.pause()` on each

**Key insight**: Session-wide pause — main agent AND all sub-workers pause cooperatively.

---

### Q2: Worker Stop Button — End-to-End Trace (two locations)

**Location A — WorkerManagementPanel.jsx (lines 586–617):**
- `handleStop(name)` via REST: `POST /api/workspace/{ws_id}/workers/{name}/stop`
- Optimistic state: sets `runtime_status` to `'stopped'` immediately
- List rows have stop buttons enabled when status is `busy`, `ready`, or falsy

**Location B — WorkerOutputPanel.jsx (lines 610–633 + 813–817):**
- Same REST POST to `/api/workspace/{ws_id}/workers/{name}/stop`
- Has cross-session guard: blocks stop if `workerInfo.session_id !== sessionId`
- `canStop = runtimeStatus === 'busy' || runtimeStatus === 'ready'`
- Stop button rendered in bottom bar: `<button className="worker-output-stop-btn">⏹ Stop</button>`

**workspace_routes.py — stop_worker() (line 580+):**
1. Writes `{"action": "stop"}` to worker's `command.json` (file-based signal, polled by worker thread)
2. Writes `status.json` with `runtime_status: "completed"` for immediate UI feedback
3. Falls back to in-memory stop via thread registry if available

**bridge.py — WebAgentBridge.stop() (lines 1096–1104):**
- Unregisters bridge, unsubscribes security & worker events
- Sets `_stop_event` and `_pause_event` (unblocks if paused)

---

### Q3: Purpose Comparison — Pause vs Stop

| Aspect | Main Pause (`pause_session`) | Worker Stop (REST API) |
|---|---|---|
| Transport | WebSocket (bidirectional) | HTTP POST (request-response) |
| Scope | Session-wide (agent + ALL workers) | Single named worker |
| Semantics | COOPERATIVE — requests pause after current turn | TERMINAL — file signal + thread kill |
| Resumable? | YES — via `continue_session` / `resume_session` | NO — terminal stop |
| UI location | QueryBar "⏸ Pause" button | WorkerManagementPanel rows + WorkerOutputPanel bottom bar |
| Worker registry | Iterates all workers for this session_id | Only the named worker by name |

**Conclusion:** They serve completely different purposes. `pause_session` is a cooperative, resumable suspension of the entire session (agent + workers). Worker stop is a terminal shutdown of a single worker, used when a specific sub-worker is misbehaving or no longer needed.

## 2026-07-17 — Chunk 2 — Backend Signal Routing (Complete)

### Q4: Main...

## Chunk 2 — Backend Signal Routing (Complete)

### Q4: Main pause signal routing

**bridge.py pause() — lines 1061-1076 (full method):**
```python
def pause(self) -> None:
    if self._controller is not None:
        self._controller.pause()       # V2 path
    else:
        if not self.is_running:
            return
        self._pause_event.clear()       # Legacy: block the main loop
        if self._agent is not None:
            self._agent.request_pause() # Legacy: tell agent to pause
    # ALWAYS runs (after either branch) — pauses ALL workers for this session:
    if WORKER_BUS_AVAILABLE and _worker_registry is not None:
        with _registry_lock:
            for (sid, wname), thread in list(_worker_registry.items()):
                if sid == self._session_id:
                    thread.pause()
```

**Three actions AFTER the if/else:**
1. Worker iteration via `_worker_registry` (same `(session_id, worker_name)` tuple keys)
2. Guarded by `WORKER_BUS_AVAILABLE and _worker_registry is not None`
3. Matches by `sid == self._session_id` → calls `thread.pause()` on all matching

**Import is FIXED — bridge.py lines 86-99:**
```python
try:
    from tools.workspace.worker import (
        shutdown_workers, get_worker_event_bus, register_worker_event_bus,
        unregister_worker_event_bus, _worker_registry, _registry_lock
    )
    WORKER_BUS_AVAILABLE = True
except ImportError:
    ...
    _worker_registry = None
    _registry_lock = None
    WORKER_BUS_AVAILABLE = False
```
`_worker_registry` is successfully imported from `tools.workspace.worker`, where it's defined at line 467-468 as `_worker_registry: dict = {}` and `_registry_lock = threading.Lock()`. Keys are `(session_id, worker_name)` tuples — consistent with the iteration in `pause()`.

**self._controller is AgentController** from `/workspace/agent/controller/__init__.py`:
- Line 14: `class AgentController`
- Line 475-502: `pause()` and `resume()` methods
- `controller.pause()` (line 475-496): Clears `pause_event`, sets `_pause_requested = True`, emits `execution_state_change` event with `PAUSING`, calls `self.agent.request_pause()`, cleans orphaned tool messages. **DOES NOT touch worker registry** — worker pausing is only in bridge.pause().
- `controller.resume()` (line 502-514): Sets `pause_event`, clears `_pause_requested`, clears `agent._pause_requested`. **No worker logic.**

**Legacy `self._agent.request_pause()`** — called in the else branch of bridge.pause(). Also the V2 path `controller.pause()` calls `self.agent.request_pause()` internally. Both signal the agent to pause after current turn.

### Q5: Worker stop signal routing

**workspace_routes.py stop_worker() — lines 580-658:**
1. **Finds worker directory** — session-scoped (`workers/<session_id>/<name>/`) or legacy (`workers/<name>/`)
2. **Writes `command.json`** with `{"action": "stop"}` — file signal polled by worker thread
3. **Writes `status.json`** with `{"runtime_status": "completed", "current_task": null, ...}` — immediate UI visual update
4. **Calls `thread.stop()`** on matching registry entries (matched by `wname == name`)

```python
with _registry_lock:
    for (sid, wname), thread in list(_worker_registry.items()):
        if wname == name:
            thread.stop()
```

Note: Matches on `wname` only (not session_id), so it can stop workers across sessions.

**WorkerThread.stop() — worker.py lines 719-728:**
```python
def stop(self) -> None:
    self._stop_event.set()
    try:
        cmd_path = self._worker_dir / "command.json"
        cmd_path.write_text(json.dumps({"action": "stop"}), encoding="utf-8")
    except OSError:
        pass
    self._input_queue.put(None)
```
- Sets `_stop_event`
- Writes `command.json {"action": "stop"}` (belt-and-suspenders with file-based signaling)
- Unblocks input queue by putting None
- **Does NOT touch `_pause_event`** — it's a terminal stop, not a resume-from-pause

**WorkerThread.pause() — worker.py lines 731-742:**
```python
def pause(self) -> None:
    self._pause_event.set()
    self._resume_event.clear()
    try:
        cmd_path = self._worker_dir / "command.json"
        cmd_path.write_text(json.dumps({"action": "pause"}), encoding="utf-8")
    except OSError:
        pass
    self._input_queue.put(None)
```

**WorkerThread.resume() — worker.py lines 744-749:**
```python
def resume(self) -> None:
    self._pause_event.clear()
    self._resume_event.set()
    self.status = "ready"
    self._write_status_file()
```

**Polling loop — `_poll_command()` worker.py lines 756-778:**
Reads `command.json`, handles `"stop"` and `"pause"` actions (deletes file, sets events, unblocks queue).

### Q6: Why aren't they unified?

**1. bridge.pause() DOES now call thread.pause() on workers.** The import is fixed. The code:
```python
if WORKER_BUS_AVAILABLE and _worker_registry is not None:
    with _registry_lock:
        for (sid, wname), thread in list(_worker_registry.items()):
            if sid == self._session_id:
                thread.pause()
```
This correctly pauses all workers in the session. ✅

**2. Worker stop is a TERMINAL (HARD) stop.** It sets `_stop_event` and `command.json {"action": "stop"}`. The worker thread loop checks `_stop_event` on every iteration and breaks out. It does NOT set `_pause_event` — no resumption path. It's a one-way door: once stopped, the worker thread exits permanently.

**3. Is there a gap?** The main pause button (`bridge.pause()`) now correctly pauses workers via `thread.pause()` — this is the RESUMPTIVE path. Worker stop (`thread.stop()`) is the TERMINAL path. They are architecturally distinct by design:
- **Pause** = cooperative suspension (set `_pause_event`, clear `_resume_event`, write `command.json {"action": "pause"}`)
- **Stop** = terminal exit (set `_stop_event`, write `command.json {"action": "stop"}`)
- They are NOT redundant and should NOT be unified — they serve different session lifecycle phases.

## 2026-07-17 — Chunk 3 — Complete Forensic Investigation

### Q7: proces...

## Chunk 3 — Complete Forensic Investigation

### Q7: process_query() pause checkpoints (3 locations)

**Checkpoint [1] — turn_start (agent.py ~line 883-897):**
```python
# At top of each turn loop iteration
if self.stop_check and self.stop_check():
    events = self.state.set_execution_state(ExecutionState.PAUSING)
    for event in events:
        for yielded_event in self._handle_state_event(event):
            yield yielded_event
    # yields 'stopped' event, returns
```
- Uses `self.stop_check` callable (config.stop_check — set externally by controller)
- Conversation untouched: no turn has started, nothing committed
- Yields: `execution_state_change` (from _handle_state_event) + `stopped`

**Checkpoint [2] — after_llm (agent.py ~line 1138-1179):**
```python
if self._pause_requested:
    if tool_calls:
        # DEFER: _pause_requested stays True, checkpoint [3] catches it later
    else:
        # GRACE TURN: commit assistant message BEFORE pausing
        assistant_msg = {'role': 'assistant', 'content': content, ...}  # no tool_calls
        grace_tx = TurnTransaction(session, context_builder, conversation)
        grace_tx.add_assistant_message(assistant_msg)
        grace_tx.commit()  # → extends session.user_history immediately
        events = self.state.set_execution_state(ExecutionState.PAUSING)
        # yields execution_state_change + paused events, returns
```
- **No tool_calls**: Grace turn committed (assistant message saved to user_history)
- **Has tool_calls**: Deferred — _pause_requested stays True, will be caught at [3]

**Checkpoint [3] — after_turn (agent.py ~line 1277-1300):**
```python
if self._pause_requested:
    events = self.state.set_execution_state(ExecutionState.PAUSING)
    # yields execution_state_change + paused events, returns
```
- At this point `turn_transaction.commit()` already called (tool results + assistant in conversation)
- Full turn data already saved

### Q8: Complete event stream (typical turn with tool calls)

```
user_query (from process_query start)
token_update (after user msg estimate)
[if time warning] time_warning + token_update
[if turn warning] turn_warning + token_update
token_update (after turn state checks)
[if token warning] token_warning + token_update
turn (carries content + tool_calls metadata)
  ── LLM call happens here ──
  [checkpoint 2: pause check]
token_update (after commit_assistant_only)
  ── tool_executor.execute_tool_calls() runs ──
  turn_transaction.commit() (all tool results committed to history)
  for each tool: tool_call event
  for each tool: tool_result event
  [flush pending warnings]
  [if final_detected] agent_responded
  [if summary_text] context_summarized
  [checkpoint 3: pause check]
  [if no tool_calls] agent_responded
```

### Q9: Conversation state when pause is yielded

**Pause at [1] (turn_start via stop_check):**
- Conversation is EXACTLY as it was at end of last turn
- NO new messages added — this check is at the very top of the loop
- Event yields: `execution_state_change` (PAUSING) → `stopped`

**Pause at [2] (after_LLM, no tool_calls):**
- Conversation has: last turn's messages + user query for this turn + assistant response (saved via grace_tx.commit())
- Grace turn: assistant_msg = {'role': 'assistant', 'content': content} — NO tool_calls
- Event yields: state events → `execution_state_change` (PAUSING) → `paused`

**Pause at [3] (after_turn, had tool_calls):**
- Conversation has: all messages from full turn (assistant with tool_calls + tool results)
- `turn_transaction.commit()` already called before checkpoint check
- Event yields: state events → `execution_state_change` (PAUSING) → `paused`

**Pause at [2] deferred (had tool_calls):**
- Same as [3] — full turn committed before yielding pause

### TurnTransaction atomic buffer (turn_transaction.py)

- **Buffered**: assistant_message + tool_calls_buffer (list of tool call/result msgs)
- **Two-phase commit** for tool turns:
  1. `commit_assistant_only()` — commits assistant message immediately after LLM call (before any events)
  2. `commit()` — commits tool results (or everything if assistant not pre-committed)
- **Rollback**: clears buffer; cannot rollback committed transaction
- On pause at [2] (no tools): `commit()` called on grace_tx (assistant only)
- On pause at [3]: normal `commit()` already called before checkpoint

### _add_to_conversation (agent.py ~line 645)
```
def _add_to_conversation(self, message):
    updated = self.conversation_manager.add_message(message, self.conversation)
    self.conversation = updated
    # validates is_system_notification flag consistency
    # invalidates context_builder._cached_context
```
- Delegates to conversation_manager.add_message()
- Updates conversation property (which for session assigns back to session.user_history)
- Validates system notification flags
- Invalidates context_builder cache

### conversation property (agent.py ~line 536-560)
- Getter: returns session.user_history when session exists, else _conversation
- Setter: replaces session.user_history contents in-place and calls _on_conversation_changed()
- Ensures HistoryProvider cache is invalidated on mutation

## 2026-07-18 — Workspace UUID Generation — Investigation Results (Partia...

## Workspace UUID Generation — Investigation Results (Partial)

### UUID Algorithm (not UUID4, but SHA-256 hash)
- Workspace "ID" is NOT a UUID4 — it's the first 16 hex characters of `hashlib.sha256(project_path.encode()).hexdigest()[:16]`
- Found in `setup_workspace.py` line 24: `ws_id = hashlib.sha256(PROJECT_ROOT.encode()).hexdigest()[:16]`
- The comment says "same algorithm as thoughtmachine.workspace_capabilities" — but actually `workspace_capabilities.py` does NOT generate IDs, it only has `resolve_workspace_id()` which scans config.json files
- The auto-registration code in `server.py` `apply_config` confirms: `import uuid; ws_id = hashlib.sha256(...).hexdigest()[:16]`

### Workspace config.json Structure
- Located at `~/.thoughtmachine/workspaces/{workspace_id}/config.json`
- Contains: `{"root": PROJECT_ROOT, "capabilities": {}}` (from setup_workspace.py line 32)
- In tests: `json.dumps({"root": f"/projects/{label}"})`

### bridge.py Cache Mechanism
- `_workspace_id_cache: Dict[str, str]` — module-level dict mapping normalized workspace path → workspace_id
- `_build_workspace_id_cache()` — scans `~/.thoughtmachine/workspaces/<id>/config.json`, normalizes root paths (abspath + replace backslash + rstrip /), builds cache
- `_resolve_workspace_id()` — looks up in cache, builds cache on first call
- Protected by `_workspace_cache_lock` (threading.Lock())
- Cache is built once, never invalidated — persistent for bridge instance lifetime

### Dual Resolution Path
1. **bridge.py cache** (`web_ui/backend/bridge.py`) — Web UI path, caches workspace_id by scanning config.json
2. **workspace_capabilities.py** (`thoughtmachine/workspace_capabilities.py`) — same algorithm but no caching, scans every call

### Auto-Registration (server.py apply_config)
When workspace_path changes and no ID is found:
1. Uses `hashlib.sha256(project_path.encode()).hexdigest()[:16]` to generate deterministic ID
2. Calls `ensure_workspace_dirs(ws_id)` to bootstrap workspace files
3. Writes `config.json` with `{"root": workspace_path}`
4. Calls `_build_workspace_id_cache()` to refresh cache

### No Human-Readable Names
- Workspace IDs are purely deterministic hashes with no human-readable alias/naming system
- Frontend receives `workspace_id` as opaque string in `session_loaded` event
- Sessions have human-readable names (via `rename_session`/`metadata['name']`) but workspaces do not


## 2026-07-18 — Chunk 1: Core Agent Loop (process_query) - Complete Analy...

## Chunk 1: Core Agent Loop (process_query) - Complete Analysis

### Overall Architecture
The Agent class (`agent/core/agent.py`, 1519 lines, 39 methods) is a **facade coordinator** that delegates to modular components. It was refactored from a monolithic 1972-line class.

### Key Components
1. **TokenCounter** (`agent/core/token_counter.py`, 111 lines) - Token estimation using tiktoken
2. **LLMClient** (`agent/core/llm_client.py`, 172 lines) - Wraps ProviderFactory, handles system prompts, chat completion
3. **ConversationManager** (`agent/core/conversation_manager.py`, 117 lines) - Message addition with cache invalidation
4. **ToolExecutor** (`agent/core/tool_executor.py`, 349 lines) - Executes tool calls, handles SummarizeTool specially
5. **TurnTransaction** (`agent/core/turn_transaction.py`) - Atomic commit/rollback for turn buffering
6. **AgentState** (`agent/core/state.py`) - State machine with TokenState, TurnState, TimeState, ExecutionState, SessionState
7. **DebugContext** (`agent/core/debug_context.py`) - Debug logging helper
8. **message_utils** (`agent/core/message_utils.py`) - Turn grouping utilities

### process_query() Flow (generator, yields dict events)
1. Add user query to conversation first (never lost on config failure)
2. Yield token_update event
3. Apply pending config (mailbox pattern)
4. If config failed: yield error event, return
5. Yield user_query event
6. Set execution state to RUNNING
7. **Turn loop** (for turn in range(max_turns)):
   a. Check stop signal (stop_check callback)
   b. Time monitoring (update_time_state)
   c. Turn state monitoring (update_turn_state)
   d. Token state monitoring (update_token_state) - fires warnings
   e. Build context via context_builder.build()
   f. Call LLM via llm_client.chat_completion()
   g. On LLM response:
      - Use prompt_tokens as ground truth (drift detection)
      - Handle token_limit_exceeded with emergency retry
      - Handle RateLimitExceeded with backoff
   h. Check pause requests at 3 checkpoints
   i. Create TurnTransaction, add assistant message
   j. Commit assistant message immediately (prevents data loss on pause)
   k. Yield token_update, turn events
   l. If tool_calls: execute via ToolExecutor
   m. Commit all tool results via turn_transaction.commit()
   n. Yield tool_call, tool_result events
   o. Flush buffered token warnings
   p. If final_detected (Respond tool): yield agent_responded, return
   q. If SummarizeTool: apply summary pruning, continue loop
   r. If no tool_calls: commit turn, yield agent_responded, return
8. **Max turns exhausted**: yield stop_reason event

### Token Management Strategy
- Truth-based tracking: LLM-reported prompt_tokens = ground truth
- Pre-call estimates tracked for drift detection (>5% warning)
- Buffered warnings (flushed after turn commit for correct chronology)
- Emergency mode: when token_limit_exceeded fires, activation with retries
- Token state machine: LOW -> WARNING -> CRITICAL thresholds

### Context Summarization Flow
- SummarizeTool generates summary text and keep_recent_turns
- _apply_summary_pruning() inserts summary message with metadata
- _find_summary_insertion_index() uses turn-grouping to find position
- Fallback path when no session available
- Post-summary: token estimate reevaluated, restrictions cleared
- Emergency mode reset after successful summary

## 2026-07-18 — Chunk 2: Event System — Complete Analysis

### Event Type...

## Chunk 2: Event System — Complete Analysis

### Event Types (agent/events.py)
~55 EventType enum values organized into categories:
- **Agent lifecycle**: AGENT_START, AGENT_END
- **LLM interaction**: LLM_REQUEST, LLM_RESPONSE, RAW_RESPONSE
- **Tool execution**: TOOL_CALL, TOOL_RESULT
- **Conversation**: CONVERSATION_UPDATE, CONVERSATION_PRUNE
- **State monitoring**: EXECUTION_STATE_CHANGE, SESSION_STATE_CHANGE
- **Token/Turn/Time warnings**: TOKEN_WARNING, TURN_WARNING, TIME_WARNING
- **Control flow**: FINAL_DETECTED, FINAL, STOP_SIGNAL, MAX_TURNS, PAUSED, STOPPED
- **Security**: SECURITY_PROMPT, SECURITY_RESPONSE, FILE_ACCESS, SECURITY_VIOLATION
- **Worker lifecycle**: WORKER_SPAWNED, WORKER_STATUS, WORKER_COMPLETED, WORKER_ERROR, WORKER_MESSAGE
- **WorkerBusAdapter**: TOKENS_UPDATED, CONTEXT_UPDATED, STATUS_MESSAGE, ERROR_OCCURRED, WORKER_STATE_SYNC, etc.

### Event Hierarchy (Pydantic v2)
- **BaseEvent**: type + metadata (EventMetadata with event_id, timestamp, source, session_id, turn) + data dict
- Typed subclasses: AgentStartEvent, AgentEndEvent, ToolCallEvent, ToolResultEvent, TokenWarningEvent, TurnWarningEvent, ErrorEvent, TurnEvent, SecurityPromptEvent, SecurityResponseEvent, WorkerSpawnedEvent, WorkerStatusEvent, WorkerCompletedEvent, WorkerErrorEvent, WorkerMessageEvent, AssistantMessageEvent
- Each typed subclass has @validator ensuring required data fields
- ToolCallEvent/ToolResultEvent normalize both 'name' and 'tool_name' keys for backward compatibility

### EventBus (agent/events.py)
- Thread-safe pub/sub with threading.Lock
- Per-type subscriber lists + wildcard subscribers (event_type=None)
- publish() calls subscribers in order, wrapping each in try/except
- publish_dict() for legacy dict format
- NullEventBus: No-op stub for testing/worker contexts (no human), ask() returns "deny" instantly
- global_event_bus = EventBus() — singleton used throughout

### Event Wiring
- **Security**: global_event_bus.subscribe(SECURITY_PROMPT) → WebAgentBridge forwards to frontend WebSocket
- **Worker lifecycle**: global_event_bus.subscribe(WORKER_SPAWNED/WORKER_STATUS/WORKER_COMPLETED/WORKER_ERROR/WORKER_MESSAGE/TOKEN_WARNING) → WebAgentBridge _make_handler() forwards as worker:* events
- **Per-worker buses**: WorkerBusAdapter creates per-worker EventBus; WebAgentBridge subscribes per-worker for tool_call, tool_result, tokens_updated, context_updated, context_summarized, etc.
- **Event dedup**: context_updated uses display string dedup per worker_name to avoid flooding frontend
- **Late-arriving bridge guard**: _discover_existing_workers() on session load finds already-running workers

### Backward Compatibility
- create_event() factory function maps EventType → typed event class
- _map_legacy_event_type() maps old string types to enum values
- convert_to_legacy_format() / convert_from_legacy_format() for interop with legacy dict-based code
- Token/Turn warnings handle 'message'/'warning'/'warning_message' key normalization
- ErrorEvent handles various error_type detection from message prefix

## Chunk 3: Worker Architecture — Complete Analysis

### WorkerThread (tools/workspace/worker.py, ~1000 lines)
- **Runs in daemon thread** with input/output queues (threading.Queue)
- **Lifecycle**: ready → busy → ready
- **Control signals**: threading.Event for stop, pause, resume
- **command.json pattern**: External control via JSON file in worker's state dir
- **Status publishing**: status_changed callback
- **Structured output**: output_handler callback
- **Agent config building**: Builds config from system prompt, tools, permissions
- **EventBus per worker**: WorkerBusAdapter bridges worker events to global bus + per-worker bus
- **Cleanup**: shutdown_workers() for graceful teardown

### Worker (tool entry point, ~770 lines)
- Permission checking (ask policy integration)
- Agent config building from tool params
- Context sanitization
- Delegates to WorkerThread for actual execution

### Key Patterns
- Workers are spawned as tool calls from the main agent
- Each worker gets its own EventBus (per-worker bus) for detailed events
- WorkerBusAdapter publishes to both per-worker bus AND global_event_bus
- NullEventBus used in worker contexts (no human to answer security prompts)
- Worker state is persisted in workspace for session resume

## Chunk 4: Session Model — Complete Analysis

### Session (session/models.py)
- Core dataclass: session_id (UUID), created_at, updated_at, runtime_params (temperature), user_history (ObservableList), total_input/output_tokens, context_length, agent_context, containers, preset_name, version, next_seq, summary, agent_instance, workspace_id, metadata, security_config
- **ObservableList**: Wraps user_history with mutation callbacks (_on_conversation_changed)
- **Conversation change tracking**: _conversation_version (int) + conversation_hash (md5 hexdigest)
- **connect_conversation_changed/disconnect**: External listeners (used by AutosaveMonitor in server.py)
- **Serialization**: to_persistable_dict() / from_persistable_dict() with backward compat for old 'config' key → metadata['agent_config']
- **Security config**: merge_security_config + coerce_session_permissions on load
- **Message normalization**: All messages converted to Message objects on load

### HistoryProvider (session/history_provider.py)
- Wraps Session.user_history, provides get_context_for_llm()
- **Cached context**: _cached_context invalidated on add_message()
- **Delegates context building to SummaryBuilder** (session/context_builder.py)
- **create_summary()**: Adds summary system message with metadata (pruning_keep_recent_turns, pruning_insertion_idx, timestamp)
- **_find_latest_summary()**: Searches backward for summary messages
- **_group_messages_into_turns()**: Groups messages for keep_recent_turns logic
- **Debug support**: DEBUG_HISTORY_PROVIDER, DEBUG_CONTEXT env vars

### ContextBuilder (session/context_builder.py)
- SummaryBuilder: Builds LLM context with summary + recent turns
- _cleanup_orphaned_tool_messages(): Removes tool messages without matching assistant
- Token estimation methods

### SessionStore (session/store.py)
- **FileSystemSessionStore**: JSON files in ~/.thoughtmachine/sessions/
- **Friendly filenames**: {sanitized_name}_{short_id}.json
- **Atomic writes**: Temp file + rename pattern
- **File locking**: FileLock for concurrent access safety
- **Metadata files**: _meta_{session_id}.json for fast sidebar listing
- **In-memory caches**: _cached_list (60s TTL), _cached_paths (5s TTL) for fast listing
- **Fast metadata extraction**: _fast_extract_metadata() reads ~8KB head to avoid full JSON parse
- **Open sessions management**: open_sessions.json, .current_session marker
- **History pruning**: prune_user_history() removes old summarization cycles on save
- **Fallback paths**: CWD → system temp if ~/.thoughtmachine unavailable

## Chunk 5: Bridge & Presenter Layer — Complete Analysis

### AgentController (agent/controller/__init__.py, 719 lines)
- **Thread-based agent runner**: Runs Agent in daemon thread, collects events via callback
- **State machine**: ExecutionState tracking (IDLE, RUNNING, PAUSED, STOPPING, etc.)
- **Synchronization**: threading.Event for stop/pause, queue.Queue for query dispatch
- **Lifecycle**: process_query() → start thread → iterate events → callback → cleanup
- **Config management**: set_session(), update_config(), get_config()
- **Global event bus publishing**: Publishes control events (PAUSED, STOPPED, etc.)

### WebAgentBridge (web_ui/backend/bridge.py, 2140 lines)
- **Thread-safe bridge**: One bridge per tab/session
- **Agent wrapped in daemon thread**: start(query, config) → thread → process_query()
- **Event mapping**: Agent events → frontend events (state_changed, tokens_updated, conversation_changed)
- **Security prompt forwarding**: global_event_bus → WebSocket callback
- **Worker event forwarding**: Per-worker bus subscription for real-time events
- **Session persistence**: load/save sessions via FileSystemSessionStore
- **Multi-tab support**: _active_tab_bridges set, _broadcast_rename()
- **Cleanup**: cleanly_closed flag prevents data loss on disconnect

### Presenter Layer (agent/presenter/)
- **RefactoredAgentPresenter**: High-level orchestrator (config, session, agent lifecycle)
- **EventProcessor**: Processes agent events, delegates to StateBridge/SessionLifecycle
- **SessionLifecycle**: Session CRUD, start/stop/pause, autosave
- **StateBridge**: Config management, tool registration, session binding
- **StateBridge state**: SessionState enum (IDLE, LOADING, ACTIVE, ERROR)

### Server Layer (web_ui/backend/server.py, ~2300 lines)
- **FastAPI + WebSocket**: Single WebSocket endpoint for all real-time communication
- **Protocol**: JSON messages with 'type' field routing
- **Message types**: process_query, pause, resume, stop, update_config, load_session, save_session, rename_session, delete_session, security_response, list_sessions, list_worker_definitions, spawn_worker, worker_command, load_workspace, etc.
- **Session persistence**: AutosaveMonitor with 2s debounce + manual save on close
- **Workspace management**: Load/save workspace state, worker contexts
- **Logging**: Logging routes for real-time log streaming
- **Health**: Health check endpoint for container orchestration

## 2026-07-19 — Workspace & Worker Pipeline Analysis (Complete)

### Conf...

## Workspace & Worker Pipeline Analysis (Complete)

### Config Loading Chain
1. `resources/default_config.json` has `"workspace_path": ""` (empty string)
2. `agent/config/loader.py::load_factory_config()` loads this into a dict
3. `AgentConfig` Pydantic model has `workspace_path: Optional[str] = Field(default=None)`
4. When loaded from JSON dict, `""` is assigned (not `None`)
5. `tool_executor.py` line 225: `if self.config.workspace_path:` → `""` is falsy → workspace_path NOT injected into tool args

### Tool Injection Chain
6. `ToolExecutor._execute_single_tool()` (line 224-226): validates args first, THEN injects workspace_path only if truthy
7. Worker tool class receives no `workspace_path` → `self.workspace_path` is None/Pydantic default
8. In `Worker.execute()` (line 1656-1659): `if resolve_workspace_id and self.workspace_path:` → self.workspace_path is falsy → ws_id stays None
9. `_load_workers(ws_id)` is called with ws_id=None
10. In the spawn handler (line ~1990-1998): ws_id=None → `ws_dir = None` → returns "Cannot create worker: no workspace directory resolved"

### Web UI Auto-Registration
11. `server.py` line 136: `_project_root = dirname(dirname(dirname(abspath(__file__))))` → 3 levels up from web_ui/backend/server.py = project root
12. Startup auto-registration (lines 253-263): `WorkspaceRegistry.get_default().register_by_root(str(_project_root))` + `ensure_workspace_dirs(entry.id)`
13. `_default_frontend_config()` (line 2210): includes `"workspace_path": _project_root` at line 2109
14. When user selects a folder in the GUI, server.py handles `set_project`/`apply_config` with path → registers workspace → sets `bridge._config.workspace_path = _project_path`

### Worker Template Loading
15. `workspace_capabilities.py::_load_template_workers()` checks `~/.thoughtmachine/worker_templates/` first, then `resources/worker_templates/`
16. `_build_default_workers()` loads templates and falls back to hardcoded "default" worker
17. Workspace `workers.json` is created by `ensure_workspace_dirs()` on first bootstrap

### Key Design Gap
- The factory `default_config.json` sets workspace_path="" but AgentConfig model default is None
- Neither "" nor None triggers workspace_path injection into tool args
- Worker tool needs workspace_path to resolve ws_id → workspace dir → workers.json
- Without Web UI (CLI mode), workspace_path is never set → Worker tool always fails

## 2026-07-20 — Workspace Path Resolution v2 (Registry-First)

**Date**: ...

## Workspace Path Resolution v2 (Registry-First)

**Date**: 2025-07-18
**Status**: Applied to Worker tool and CheckSystem tool; remaining tools still use AgentConfig.workspace_path via ToolBase injection

### Problem
`AgentConfig.workspace_path` was populated at bridge-initialization time, meaning tools that ran before the session bridge had fully initialized would get `None`. This was a timing-dependent bug that caused silent failures.

### Solution
A registry-first resolution pattern:

1. Tools query **SessionRegistry** by `self.session_id` to get the workspace ID
2. They then query **WorkspaceRegistry.get_workspace(ws_id)** to get the canonical workspace path
3. Falls back to `AgentConfig.workspace_path` with a deprecation warning

### Files Modified
- `tools/workspace/worker.py` — lines 1656-1678: registry-first resolution with CAPABILITIES_AVAILABLE guard
- `tools/workspace/check_system.py` — execute() resolves at top; passes to 3 methods
- `agent/config/models.py` — [DEPRECATED] tags on workspace_path field

### Remaining Work
15+ files still consume `self.workspace_path` from ToolBase. Each should be refactored following the same pattern.

## 2026-07-20 — Workspace Path Resolution v2 — Full Tool Suite Refactor

...

## Workspace Path Resolution v2 — Full Tool Suite Refactor

**Date**: 2025-07-18
**Scope**: All tools in `tools/` directory

### Added to ToolBase (`tools/base.py`)
- **`_resolve_registry_workspace()`** — new helper method on ToolBase that all tools call instead of reading `self.workspace_path` directly. Queries SessionRegistry → WorkspaceRegistry first, falls back to deprecated AgentConfig.workspace_path with a warning.
- **`_validate_path()`** — updated to use `_resolve_registry_workspace()` internally, so all path validation goes through registries automatically.
- **`workspace_path` field** — marked `[DEPRECATED]` in its description.

### Files Refactored (12 files total)

| File | Pattern Used |
|------|-------------|
| `tools/base.py` | Added helper, updated _validate_path, deprecated field |
| `tools/respond.py` | Inline registry resolution in execute() |
| `tools/git_info_tool.py` | Inline registry resolution in execute() |
| `tools/knowledge_base.py` | Inline registry resolution in execute() |
| `tools/read_file_tool.py` | Inline registry resolution in execute() |
| `tools/progress_report.py` | `self._resolve_registry_workspace()` |
| `tools/file_search_tool.py` | `self._resolve_registry_workspace()` |
| `tools/refactor_tool.py` | `self._resolve_registry_workspace()` |
| `tools/search_codebase.py` | `self._resolve_registry_workspace()` |
| `tools/docker_code_runner.py` | `self._resolve_registry_workspace()` |
| `tools/workspace/worker.py` | `self._resolve_registry_workspace()` |
| `tools/workspace/edit_dockerfile.py` | Registry-first session/workspace resolution |
| `tools/workspace/check_system.py` | Previously refactored in first pass |

### Files Not Requiring Changes
- `tools/file_editor.py` — uses `_validate_path()` (inherited from ToolBase, now registry-first)
- `tools/utils.py` — only references field name in schema string set
- `tools/workspace/` sub-tools already refactored in first pass

## 2026-07-20 — Session File Architecture (R1 Research)

**Date:** 2026-0...

## Session File Architecture (R1 Research)

**Date:** 2026-07-21

**File:** `docs/research/r1-session-file.md`

**Storage locations:**
- Legacy: `~/.thoughtmachine/sessions/<name>_<short_id>.json`
- Workspace-scoped: `~/.thoughtmachine/workspaces/<ws_id>/sessions/<name>_<short_id>.json`
- Lightweight metadata: `_meta_<session_id>.json` alongside session files
- State files (open sessions, current marker): `~/.thoughtmachine/state/`

**Serialization:** `Session.to_persistable_dict()` → JSON via `json.dump(data, indent=2, default=str)`

**Sysprompt location:** `session.metadata['agent_config']['system_prompt']` — full copy of AgentConfig at save time (excluding api_key). Set in `bridge.py:save_session()` line 1313.

**Key fields persisted:** session_id, created_at, updated_at, user_history (messages), containers, preset_name, workspace_id, mode, last_active, metadata (includes agent_config with sysprompt), security_config, version, summary, total_input_tokens, total_output_tokens, context_length

**Fields NOT persisted (excluded from to_persistable_dict()):** agent_context (derived), agent_instance (runtime), next_seq (reconstructed on load), runtime_params (derived from metadata.agent_config), callbacks, conversation_version

**Atomic writes:** .tmp file + Path.replace() + FileLock for concurrent safety

## 2026-07-20 — R6 — Worker Config Value Flow

**Date:** 2025-07-16

**Co...

## R6 — Worker Config Value Flow

**Date:** 2025-07-16

**Complete flow documented in:** `docs/research/r6-worker-config-value-flow.md`

**4-layer chain:**
1. **ToolExecutor** injects `agent_config` dict (temperature, provider, model, api_key, etc.) into every tool instance
2. **Worker._build_agent_config()** (ToolBase, line 1860) — shallow-copies the dict, adds workspace_path
3. **WorkerThread._build_agent_config()** (thread, line 791) — converts dict → AgentConfig, renames provider→provider_type, overrides worker-specific fields (system_prompt, max_turns, token thresholds, timeout) from definition
4. **Agent(config=agent_cfg_obj)** — worker gets its own Agent with the worker-specific AgentConfig

**Key finding:** Temperature is NOT overridable per worker — always inherited from parent. provider→provider_type rename is fragile. Permission model via `_restrictive_merge()` correctly enforces session as ceiling.

## 2026-07-20 — R7: Worker WebSocket Event Routing (Complete Trace)

### ...

## R7: Worker WebSocket Event Routing (Complete Trace)

### Two-Path Architecture

**Path A — Global EventBus (lifecycle events):**
1. `tools/workspace/worker.py` (WorkerThread.run, ~line 1079): Creates per-worker EventBus → `register_worker_event_bus()` → publishes `WORKER_SPAWNED` to **global_event_bus** (agent/events.py)
2. `web_ui/backend/bridge.py` (`_subscribe_to_worker_events`): `WebAgentBridge` subscribes to global_event_bus for event types: `WORKER_SPAWNED`, `WORKER_STATUS`, `WORKER_COMPLETED`, `WORKER_ERROR`, `TOKEN_WARNING`, `WORKER_MESSAGE`
3. Handler functions: `_on_worker_spawned`, `_on_worker_completed`, `_on_worker_error`, plus `_make_handler(WorkerStatusEvent)` and `_make_handler(BaseEvent)` for status/message
4. Events forwarded to frontend via `self._event_callbacks` dict (cb(event_dict)) with type prefix `worker:` (e.g., `worker:worker_spawned`)

**Path B — Per-worker EventBus (detailed events: tool_call, tool_result, etc.):**
1. `tools/workspace/worker.py`: Creates `WorkerBusAdapter` which publishes events to per-worker EventBus via `_publish()` method
2. On `WORKER_SPAWNED`, bridge calls `_subscribe_to_worker_bus(worker_name, worker_bus)` which subscribes to 14+ event types: tool_call, tool_result, worker_message, assistant_message, context_updated, context_cleared, context_summarized, token_recovery, token_warning, turn_warning, time_warning, user_message, system_notification
3. `_make_bus_handler(original_type)` creates handlers that forward events via `_event_callbacks` with `worker:` prefix
4. `_on_worker_completed`/`_on_worker_error` call `_unsubscribe_worker_bus(worker_name)` to clean up

**Late-arriving bridge guard:**
- `_discover_existing_workers(session_id)` called after initial subscriptions, uses `get_worker_event_buses_for_session(session_id)` to find already-running workers

**Server.py WebSocket routing:**
- `/ws` endpoint creates `WebAgentBridge`, calls `bridge.set_event_callback(event_callback, key=id(ws))` → `send_event` async function → `ws.send_json(event)`
- Bridge stored in `_session_bridges` dict keyed by session_id
- Events flow: agent → event bus → bridge handler → _event_callbacks → send_event → ws.send_json

### Frontend event handling:
- `web_ui/frontend/src/components/chat/adaptWorkerEvent.js`: Pure function transforming worker events (with `worker:` prefix stripped in WorkerOutputPanel) into MessageBubble-compatible format
- `WorkerOutputPanel.jsx`: Filters events by worker_name, processes context_updated for live token display, uses adaptWorkerEvent for message rendering
- Per-worker context_updated dedup: same formatted value (e.g., "78.3K") is skipped

### Worker file-based command mechanism:
- `command.json` in `workers/<session_id>/<name>/command.json` → read by `_poll_command()` in worker.py
- Currently supports: `{"action": "stop"}` and `{"action": "pause"}` (proposed)
- `status.json` in same directory → written by worker thread, read by REST API

## R8: Token Threshold Architecture (Complete)

### Defaults: warning=65000, critical=80000 tokens

### Files involved:
- `agent/config/models.py`: `AgentConfig` fields — `token_monitor_warning_threshold: int = 65000`, `token_monitor_critical_threshold: int = 80000`
- `agent/config/loader.py`: Legacy mapping — `warning_threshold` → `token_monitor_warning_threshold * 1000`, `critical_threshold` → `token_monitor_critical_threshold * 1000`
- `agent/core/state.py`: `TokenState` (LOW/WARNING/CRITICAL), `update_token_state()` determines state and yields warning/recovery events
- `agent/core/agent.py` (line 1514): Worker agent creation overrides defaults with same values
- `agent/logging/config_snapshot.py`: Captures critical threshold in snapshots
- `agent/presenter/state_bridge.py`: Reads config from API, maps legacy field names
- `session/models.py`: Workers get token thresholds from config
- `agent/models/worker_definition.py`: `critical_threshold_tokens: Optional[int] = 80000`
- `resources/worker_definition_schema.json`: `critical_threshold_tokens` default=80000

### Token Warning Event Path:
1. `AgentState.update_token_state()` in `agent/core/state.py` creates `token_warning` events (legacy dict format via `_create_event`)
2. These events are consumed by `Agent._update_tokens_after_tool()` and the main event loop — both inject [SYSTEM NOTIFICATION] messages into the conversation
3. For workers: `WorkerBusAdapter.forward_agent_event()` publishes `token_warning` events to the per-worker EventBus
4. Bridge's `_make_bus_handler('token_warning')` forwards as `worker:token_warning` via callback
5. Bridge's `_on_worker_token_warning` (global bus handler) catches **main agent** token warnings and forwards as `worker:system_notification` (skips worker-sourced to avoid duplicates)
6. Frontend `adaptWorkerEvent.js`: `case 'system_notification'` with `resp.type === 'token_warning'` → `tokenWarningMsg(evt)` → renders as system notification
7. Direct `worker:token_warning` events → handled by `WorkerOutputPanel` case 'token_warning' → displayed with warning styling

### Frontend defaults (ConfigPanel.jsx):
- warning=35000, critical=50000 (lower than backend defaults — conservative for user config)
- WorkerOutputPanel.jsx fallback: max_context_tokens=80000

## 2026-07-20 — Token Pipeline (Trace Analysis)

### Architecture Overvie...

## Token Pipeline (Trace Analysis)

### Architecture Overview
The token pipeline has 5 layers that form a reactive state machine:

**Layer 0 - TokenCounter** (`agent/core/token_counter.py`):
- Estimates tokens per message using tiktoken
- Used by agent.py `_estimate_tokens()` and `_update_conversation_token_estimate()`

**Layer 1 - Token Tracking** (`agent/core/agent.py`):
- `self._token_counts = {'input': ..., 'output': ...}` — tracking input/output separately
- Properties `total_input_tokens` / `total_output_tokens` bridge to Session model
- `self.state.current_conversation_tokens` — live total conversation tokens

**Layer 2 - State Updates** (`agent/core/state.py`):
- `AgentState.update_token_state(total_tokens)` compares against thresholds:
  - `token_monitor_warning_threshold` (default: 65000) → TokenState.WARNING
  - `token_monitor_critical_threshold` (default: 80000) → TokenState.CRITICAL
- WARNING fires once per cycle → injected as `[SYSTEM NOTIFICATION]`
- CRITICAL sets `restrictions_active = True`, `restriction_reason = 'token'`

**Layer 3 - Restriction Levels** (`agent/core/state.py`):
- `get_allowed_tools()`:
  - `token` CRITICAL: **Respond + SummarizeTool** (agent can summarize to free context)
  - `turn` CRITICAL: **Respond only** (agent must finish immediately)
  - `timeout` CRITICAL: **Respond only** (agent exceeded runtime limit)

**Layer 4 - Enforcement** (`agent/core/tool_executor.py`):
- Line 125: Before executing any tool call, checks `self.state.is_tool_allowed(tool_name)`
- Disallowed tools get rejection message via `_create_tool_rejection_message()`
- Rejection with `Respond` would be a bug — found to be impossible since Respond is always allowed

**Layer 5 - Recovery (Summarization)** (`agent/core/agent.py`):
- After SummarizeTool → `_apply_summary_pruning()` → `_update_conversation_token_estimate()` → `update_token_state()`
- If tokens drop below critical → `token_recovery` event → restrictions cleared
- `context_builder.emergency_mode` reset

### Warning Event Flow
```
update_token_state() → [token_warning event]
    → buffered in _pending_warnings
    → after turn_transaction.commit()
    → injected as [SYSTEM NOTIFICATION] Message into conversation
    → emitted via event bus for GUI
```

### Turn & Time Monitors (`agent/core/state.py`):
- **Turn**: WARNING at `max_turns-8`, CRITICAL at `max_turns-5` → `restriction_reason='turn'`
- **Time**: WARNING at `time_warning_threshold`, CRITICAL at `timeout_seconds` → `restriction_reason='timeout'`

### Provider Key Flow (verified consistent):
- Frontend sends: `{"provider": "openai"}` (ConfigPanel.jsx line 36)
- `server.py _translate_frontend_config()`: pops `provider`, sets `provider_type` (line 2080-2082)
- StateBridge `create_agent_config()`: `provider_type` → AgentConfig Pydantic field (state_bridge.py line 244)
- Back to frontend: `server.py _frontend_config_from_bridge()`: pops `provider_type`, sets `provider` (line 2244-2245)
- Worker path: `WorkerThread._build_agent_config()` does same rename (worker.py line 791)
- **No inconsistency** — the two translation functions handle the mapping correctly

### Threshold Config:
- Warning: 65k tokens (WorkerDefinition.warning_threshold_tokens)
- Critical: 80k tokens (WorkerDefinition.critical_threshold_tokens)
- These map to AgentConfig.token_monitor_warning_threshold/critical_threshold


## 2026-07-20 — SYSTEM INTEGRATION TEST — SummarizeTool under Timeout Con...

## SYSTEM INTEGRATION TEST — SummarizeTool under Timeout Conditions

**Date:** 2026-07-20
**Status:** ✅ PASS

**Test:** SummarizeTool was invoked after timeout restrictions (10s) were active. The tool executed successfully, summary was captured with `keep_recent_turns=0`, context was pruned, and a fresh context window was granted to the new agent instance.

**Key learning:** SummarizeTool is usable as a recovery mechanism even when timeout restrictions are theoretically in place. The tool's `skip_output_truncation=True` flag ensures full summary content is preserved.

**Event log reference:** `event_log.jsonl` entries #6–#37

## Mode ↔ Tools Pipeline

## 2026-07-20 — Mode ↔ Tools Pipeline — Complete Trace & Remaining Gaps

...

## Mode ↔ Tools Pipeline — Complete Trace & Remaining Gaps

### Verified: PyQt GUI Path (all four lifecycle methods pass mode correctly)

| Caller | File | Line | mode parameter |
|--------|------|------|---------------|
| `session_lifecycle.start_session()` | agent/presenter/session_lifecycle.py | 101 | `mode=session_mode` ✓ |
| `session_lifecycle.new_session()` | agent/presenter/session_lifecycle.py | 136 | `mode=session_mode` ✓ |
| `session_lifecycle.continue_session()` | agent/presenter/session_lifecycle.py | 201 | `mode=session_mode` ✓ |
| `session_lifecycle.restart_session()` | agent/presenter/session_lifecycle.py | 490 | `mode=session_mode` ✓ |
| `_build_session_from_current_state()` | (internal) | — | calls state_bridge directly with mode ✓ |

Where `session_mode = self.state_bridge.current_session.mode` (or None if no session).

### Verified: WebUI Path (mode flows through config_dict + session metadata)

1. **Create session**: `POST /api/session/create` → `body.mode` → `session.mode = body.mode`
2. **Save session**: `bridge.save_session()` → `session.metadata['agent_config'] = self._config.model_dump()` (includes `mode` field)
3. **Load session**: `bridge.load_session()` → deep-merges `session.metadata['agent_config']` (includes `mode`) into `self._config`
4. **Start agent**: `bridge.start(config_dict)` → deep-merges `config_dict` (includes `mode` from frontend) → `AgentConfig(**merged_config)`
5. All session responses include `mode=session.mode` ✓

### Verified: AgentConfig itself handles mode

- `FIELD_CATEGORIES['mode'] = RESTART_REQUIRED`
- `_apply_mode_system_prompt` validator loads correct system prompt text based on mode
- `get_tools_for_mode(mode)` filters `enabled_tools` based on mode presets

### Gap 1: `agent_presenter.create_agent_config()` passthrough (line 298-300)

```python
def create_agent_config(self) -> AgentConfig:
    return self.state_bridge.create_agent_config()
```

Calls `state_bridge.create_agent_config()` WITHOUT passing `mode`. When `mode=None`, `get_tools_for_mode(None)` returns `None` → no filtering → ALL tools enabled.

**Resolution**: This method has **zero callers** in the codebase — it's dead code. Not a real gap.

### Gap 2: `Session.mode` defaults to "agent" and is only set via `CreateSessionBody`

When creating a session through the PyQt GUI (not the WebUI API), the `Session.mode` may not be initialized to anything other than the default "agent". The lifecycle methods check `self.state_bridge.current_session.mode` — but if `current_session` is None, `session_mode` becomes None, and filtering is bypassed.

**Resolution**: The `Session` model has `mode: str = "agent"` default, so it defaults correctly. Edge case: if `state_bridge.current_session` is None, `session_mode` becomes None, but this only happens in `_build_session_from_current_state` which sets `current_session` before these methods are called.

### Gap 3: Bridge `start()` doesn't explicitly set `session.mode`

In `bridge.start()` (line 912), mode flows through `config_dict` from the frontend into `AgentConfig(...)`, but NOT into `self._session.mode`. The session is created at line 994-998:

```python
if self._loaded_session is not None:
    session = self._loaded_session
else:
    session = Session()
```

If it's a new session (no loaded session), `session.mode` stays at default `"agent"` regardless of what mode was in `config_dict`. This means the `Session.mode` field can be out of sync with the actual `AgentConfig.mode`.

**Severity**: Low-Medium. The actual tool filtering is driven by `AgentConfig.mode`, not `Session.mode`. The `Session.mode` is only used for display/list purposes in the WebUI. But it IS inconsistent.



## 2026-07-21 — Tools Toggle → Core Propagation Flow (Investigation, Feb ...

## Tools Toggle → Core Propagation Flow (Investigation, Feb 2025)

### The Full Pipeline
```
Frontend (tools toggle + Apply)
  │
  ▼ apply_config {tools: [{name, enabled}], mode, ...}
server.py _translate_frontend_config()
  │
  ▼ backend_config {enabled_tools: [...]}
bridge.py apply_config()
  │
  ▼ self._controller.request_config_update(validated)
controller.py request_config_update()
  │
  ▼ self.agent.request_config_update(config)  [mailbox: _pending_config]
agent/core/agent.py
  │
  ▼ at next process_query() → _apply_pending_config()
  │   ├─ _can_hot_swap()?  (temperature, top_p, enabled_tools only)
  │   │   └─ _hot_swap() — rebuilds tool_classes + tool_definitions + ToolExecutor
  │   └─ else → _restart_with_config() — full restart
  │
  ▼ _notify_config_change() — adds [SYSTEM NOTIFICATION] to conversation
```

### Two Key Issues Found

**Issue 1 — Mode-based tool preset override (BUG):**
In `_translate_frontend_config()` (server.py lines 2119-2125):
```python
mode = fe_config.get('mode') or cfg.get('mode')
if mode in ('agent', 'engineer'):
    from agent.config.presets import get_tools_for_mode
    cfg['enabled_tools'] = get_tools_for_mode(mode)
```
When mode is 'agent' or 'engineer', the frontend's `{tools: [{name, enabled}]}` list is translated to `enabled_tools`, then **immediately overwritten** by the mode preset. The user's toggles are silently discarded. The fix: only apply preset when frontend doesn't send a tools list, or use the preset as a base and apply user's toggles on top.

**Issue 2 — Notification already partially exists:**
In `agent/core/agent.py`, `_notify_config_change()` already detects `enabled_tools` changes and adds `"[SYSTEM NOTIFICATION] Configuration updated: tools updated, ..."` to the conversation (visible to the LLM). But there's **no dedicated `tools_changed` event** sent to the frontend WebSocket — only the generic `config_changed` event with the full config. If the frontend needs a toast/notification trigger, that would need to be added server-side.

**Issue 3 — Worker tool preset gap:**
`session/tool_presets.py` only includes `Worker` in `CUSTOM_TOOLS`, not in `AGENT_TOOLS` or `ENGINEER_TOOLS`. Default session mode is `"agent"`, and the UI creates sessions with `mode="agent"`. So workers never show up in agent mode by default.

## 2026-07-21 — Mode Enforcement Bridge Analysis (2026-07-22)

### Files ...

## Mode Enforcement Bridge Analysis (2026-07-22)

### Files involved:
- `agent/config/models.py` — AgentConfig mode field + _apply_mode_system_prompt after-validator
- `agent/config/presets.py` — AGENT_TOOLS/ENGINEER_TOOLS/CUSTOM_TOOLS + get_tools_for_mode()
- `agent/presenter/state_bridge.py` — PyQt path, DOES enforce mode tools in create_agent_config()
- `web_ui/backend/bridge.py` — WebUI path, DOES NOT enforce mode tools in start() or apply_config()
- `web_ui/backend/server.py` — _translate_frontend_config(), applies mode tools ONLY when enabled_tools is empty

### Bugs:
1. bridge.apply_config() has NO mode enforcement
2. bridge.start() has NO mode enforcement
3. server.py only enforces when enabled_tools is empty/falsy, not when mode is explicitly changed
4. Empty list from frontend ("explicitly disabled all tools") bypasses mode enforcement

## 2026-07-22 — SessionConfig integration in bridge.py

`web_ui/backend/b...

## SessionConfig integration in bridge.py

`web_ui/backend/bridge.py` now uses `SessionConfig` as the canonical format for storing session-level configuration in `Session.metadata['session_config']`.

**Storage:** `save_session()` converts the in-memory `AgentConfig` to `SessionConfig` via `SessionConfig.from_agent_config()` and stores it as `metadata['session_config']` (instead of the old raw `AgentConfig.model_dump()` under `metadata['agent_config']`).

**Loading:** `load_session()` reads `metadata['session_config']` (new format) first, with fallback to `metadata['agent_config']` (legacy). The new format reconstructs a `SessionConfig` and calls `to_agent_config()` to get the base `AgentConfig`, then merges with global config for fields outside the session scope.

**Backward compatibility:** Old sessions stored as `metadata['agent_config']` are still loaded correctly via the fallback path. New sessions use `metadata['session_config']`.

**New method:** `SessionConfig.from_agent_config(agent_config, workspace_id='')` in `agent/config/session_config.py` — extracts session-level fields (mode, tools, prompt, provider, model, temperature, etc.) from an `AgentConfig`.


## 2026-07-22 — Bridge SessionConfig Refactor (in progress)

Refactoring ...

## Bridge SessionConfig Refactor (in progress)

Refactoring `web_ui/backend/bridge.py` to use `SessionConfig` instead of `AgentConfig` for runtime session configuration.

### Changes planned:
1. **self._config → self._session_config** (type: `SessionConfig`)
2. **Remove module-level prompt loaders** - SessionConfig handles prompts internally
3. **Remove import of `AGENT_TOOLS, ENGINEER_TOOLS` and prompt path constants**
4. **Simplify start()** - Create SessionConfig from incoming data, convert via `.to_agent_config()`
5. **Simplify apply_config()** - Work with SessionConfig
6. **get_config()** - Return dict from SessionConfig (update callers in server.py)
7. **Simplify save_session()** - Store SessionConfig directly
8. **Simplify load_session()** - Use SessionConfig

## SessionConfig cleanup

## 2026-07-22 — 2026-07-22: Removed workspace_path and session_permission...

## 2026-07-22: Removed workspace_path and session_permissions from SessionConfig

- **What changed**: `workspace_path` and `session_permissions` fields removed from `SessionConfig` class in `agent/config/session_config.py`. These fields belong on `AgentConfig` (in `models.py`), not the session-level config.
- **Also fixed**: Accidentally deleted `mode` and `workspace_id` fields during initial attempt — restored them with the second edit.
- **Testing**: Validated via Docker import test — `SessionConfig.model_fields` now shows only session-level fields (`mode`, `workspace_id`, `enabled_tools`, `system_prompt`, `provider_id`, `model`, `base_url`, `temperature`, `top_p`, `max_tokens`, `api_key`).
- **Validator/methods tested**: Mode presets, update_tools, update_prompt, factory method all pass.
- **⚠️ Downstream impact**: `web_ui/backend/bridge.py` and `web_ui/backend/server.py` access `SessionConfig.workspace_path` directly, which will now fail at runtime (AttributeError). These need updating to get `workspace_path` from `AgentConfig` instead.


## 2026-07-24 — 2026-07-23 — Security Architecture Proposal (for TM V2 re...

## 2026-07-23 — Security Architecture Proposal (for TM V2 review)

### Core Principles Adopted
1. **The Vault Is Sacred** — ~/.thoughtmachine must be off-limits to all agent tools
2. **No Session Without a Workspace** — every session must be workspace-bound
3. **Defaults Must Be Restrictive** — ship safe, let users expand
4. **Defense in Depth** — enforce at tool base, security gate, and Docker layers
5. **Auditability** — every security decision logged and visible

### Key Findings
- `resources/default_config.json` has `network: true, container: true, filesystem: "write"` — fully permissive
- `validate_path()` has NO vault protection — doesn't block `~/.thoughtmachine`
- Two conflicting `WorkspaceCapabilities` models (Pydantic with `network=False` vs dataclass with `allow_network=True`)
- `CheckSystem` reads vault files directly
- `agent_config` (including `api_key`) is injected into all tools
- Workers inherit full credentials with no isolation

### See full proposal in WorkingDocument c10989c79acd "Proposal"

## 2026-07-27 — Session Lifecycle (complete flow)

### Key Components
1. ...

## Session Lifecycle (complete flow)

### Key Components
1. **WebAgentBridge** (`web_ui/backend/bridge.py`) - manages one agent session, thread-safe, emits events via callbacks
2. **Session** (`session/models.py`) - data model with session_id, user_history, metadata, workspace_id
3. **FileSystemSessionStore** (`session/store.py`) - persistence layer, singleton, JSON files + meta files
4. **SessionRegistry** (`session/session_registry.py`) - in-memory index rebuilt from disk
5. **Server** (`web_ui/backend/server.py`) - FastAPI WS endpoint, parses commands, creates/destroys bridges
6. **SessionConfig** (`agent/config/session_config.py`) - user-configurable settings with mode enforcement
7. **AgentController** (`agent/controller.py`) - optional controller wrapping the Agent

### Flow: start_session
1. Frontend sends `{command: "start_session", query: "...", config: {...}}`
2. server.py translates config via `_translate_frontend_config()`, creates new AgentController + WebAgentBridge
3. bridge.set_controller(controller) wires them together
4. bridge.start(query, SessionConfig(**config_dict)):
   - Resolves API key from ProviderManager if needed
   - If controller exists: delegates to `controller.start(query, agent_config, session=session)`
   - Otherwise: creates Agent with config and session, spawns daemon thread running _run_loop

### Flow: continue_session
1. Frontend sends `{command: "continue_session", query: "...", config?: {...}}`
2. server.py checks 3 cases:
   - Case 1: Bridge has _loaded_session (loaded from disk) but not running — starts fresh with session
   - Case 2: Bridge is running — calls bridge.continue_session(query, config_dict)
   - Case 3: No active session — error
3. bridge.continue_session():
   - Optionally applies config via apply_config()
   - If using controller: controller.continue_session(query)
   - If standalone: puts query on _query_queue

### Flow: apply_config (normal, no workspace change)
1. server.py translates config, calls bridge.apply_config(backend_config)
2. bridge.apply_config():
   - Updates mutable fields (provider_id, model, temperature, max_tokens, session_permissions)
   - Mode-locked: tools/prompt only mutable in 'custom' mode
   - Mode only mutable before session starts
   - Resolves provider credentials on provider_id change
   - Pushes to controller via request_config_update()
   - Re-syncs Docker container
   - Saves session to disk

### Flow: apply_config (with workspace change)
1. server.py detects workspace_path change
2. Saves current session, stops bridge
3. Creates fresh bridge + controller for new workspace
4. Resolves/registers workspace via WorkspaceRegistry
5. Session strategy:
   - Existing session with conversation → creates NEW session for new workspace (opens new tab)
   - Existing session empty/blank → updates workspace_id in-place (reuses tab)
   - No existing session → creates fresh session
6. Applies config to new bridge

### Flow: save_session
1. bridge.save_session(name=None):
   - Uses active self._session if available
   - Falls back to building from self._loaded_session
   - Sets metadata (session_config, source, name)
   - Calls session_store.save_session(session, workspace_id)
   - Loads persisted worker contexts
2. store.save_session() does atomic write via temp file + FileLock

### Flow: save_open_session
1. bridge.save_open_session(session_id=None):
   - Calls save_session() first
   - Then calls session_store.add_open_session(sid)
   - Used by server's atexit _shutdown_save

### Flow: close_session
1. bridge.close_session(session_id=None):
   - Stops bridge (stops agent thread)
   - Saves session (captures final messages)
   - Removes from open sessions
   - Shuts down workers
   - Clears state

### Flow: load_session
1. Frontend sends `{command: "load_session", session_id: "...", limit: 50, offset: 0}`
2. server.py loads from store, creates fresh controller+bridge, sends session_loaded to frontend

### Flow: shutdown (server.py atexit)
1. _shutdown_save() iterates _session_bridges, saves each open session
2. atexit + signal handlers (SIGINT/SIGTERM) ensure sessions survive Ctrl+C

### No session_lifecycle.py exists
- `agent/presenter/session_lifecycle.py` was referenced in summaries but does NOT exist in the codebase
- All lifecycle logic lives in server.py (WS handler) and bridge.py (bridge class)

## 2026-07-27 — 2025-07-22: Docker pipeline trace completed. Key finding: `d...

2025-07-22: Docker pipeline trace completed. Key finding: `docker/requirements-docker.txt` contains ML bloat (sentence-transformers, chromadb, langchain, langchain-community) pulling ~3GB of unnecessary GPU packages (torch, nvidia-cudnn, nvidia-cublas, etc.) into the executor container. The earlier Dockerfile fix (COPY + pip install requirements-docker.txt) fixed the missing fast-json-repair bug but introduced massive bloat because requirements-docker.txt was never cleaned up. Full audit at `docs/infrastructure/docker-pipeline-trace.md`.

## 2026-07-29 — WebAgentBridge Decomposition Research

### Source Files S...

## WebAgentBridge Decomposition Research

### Source Files Status
- **bridge.py**: 2,260 lines, ~55+ methods — full `__init__` (L199-263), all method signatures, `start()` (L916-980), event callback system (L861-892), worker lifecycle (L688-800), registry/broadcast (L801-820), public properties (L824-858), all read
- **server.py**: 2,545 lines, 29 functions — 26 WebSocket command handlers, all read: `start_session`, `continue_session`, `apply_config` (L687-910), `new_session` (L1530-1678), `delete_session` (L1369-1454), `translate_frontend_config` (L2144-2185), `_frontend_config_from_bridge` (L2197-2234), `_backend_to_frontend_config` (L2312-2349), `_default_frontend_config` (L2352-2357), `_load_global_defaults` (L2288-2309), `_FALLBACK_FRONTEND_CONFIG` (L2238-2285)
- **session_config.py**: 200 lines — `SessionConfig` class (pydantic BaseModel), `to_agent_config()` method, `from_mode()` factory method, workspace_path stored on bridge not session

### Key Observations
1. bridge.py is the largest/most complex file (2260 lines) — handles: lifecycle (start/stop/pause/resume), event forwarding, worker subscriptions, session management, config merging
2. server.py is the WebSocket dispatcher (2545 lines) — command routing + config translation functions
3. Config translation has two formats: frontend format (provider/tools as list of dicts) ↔ backend AgentConfig format (provider_type/enabled_tools)
4. Worker handling is split: subscriptions/lifecycle in bridge.py, event forwarding in bridge.py, no worker-specific server.py handlers
5. session_config.py is small (200 lines) — clean pydantic model

## 2026-07-29 — Phase 1 — Vault Restructuring (COMPLETE):
  - Created resour...

Phase 1 — Vault Restructuring (COMPLETE):
  - Created resources/factory_defaults.json — immutable base config
  - Created thoughtmachine/vault.py — vault module with ensure_vault_structure(), ensure_vault_defaults(), load_factory_defaults(), vault_root()
  - Updated resources/MANIFEST.json — added factory_defaults.json entry (source → system/factory_defaults.json)
  - Modified thoughtmachine/security.py — added credentials path blocking in validate_path()
  - Modified thoughtmachine/bootstrap.py — ensure_user_defaults() now imports from thoughtmachine.vault

All 5 tasks completed. Next: Phase 2 (KB integration) or Phase 3 (migration).

## 2026-07-29 — **Phase 4 Facts (2025-02-20):** Vault has 6 subdirs: credent...

**Phase 4 Facts (2025-02-20):** Vault has 6 subdirs: credentials, knowledge, sessions, state, system, worker_templates. `factory_defaults.json` (vault) has 5 fields under `config` key: max_turns=50, temperature=0.7, provider_id="", model="", system_prompt="". `default_config.json` (AgentConfig) has 40+ flat fields. `loader.load_factory_config()` uses default_config.json, NOT factory_defaults.json. Working doc at `.thoughtmachine/working_docs/phase4_facts.json`.

## 2026-07-30 — End-to-End Provider Client Instantiation Analysis (Comple...

## End-to-End Provider Client Instantiation Analysis (Complete)

Completed a deep trace of the entire LLM provider client creation path — from config loading through factory dispatch to actual HTTP client construction. Below is the full chain documented for reference.

### Entry Point: Agent.initialize()
File: `agent/core/agent.py`
- `Agent.initialize()` is async, called by session lifecycle.
- It calls `self._initialize_llm()` which delegates to `LLMClient.initialize()`.

### Config Load Chain
`agent/config/`:
1. `loader.py` → `ConfigLoader.load()` merges default_config.json, factory_defaults.json, user config, env vars, CLI args
2. `models.py` → Uses `AppConfig`, `LLMConfig`, `ProviderConfig` (Pydantic models with field validation)
3. `service.py` → `ConfigService` acts as a facade, caching parsed config

### Credential Injection
`agent/credentials/injector.py`:
- `ConfigInjector.inject_credentials()` runs after base config load
- Scans `ProviderConfig` for fields matching `{PROVIDER}_api_key` pattern
- If `api_key` field is empty, reads from `THOUGHTMACHINE_{PROVIDER}_API_KEY` environment variable
- Replaces `${ENV_VAR}` patterns in config values
- All providers go through the same credential injection logic

### LLM Client Initialization
`agent/core/llm_client.py`:
- `LLMClient.initialize()` receives the fully resolved `AppConfig`
- Creates an `LLMConfig` from `app_config.llm` (or uses defaults)
- Calls `LLMClient._create_provider_client()`

### Factory Pattern
`llm_providers/factory.py`:
- `create_provider()` is the factory function
- Takes `provider_name` (str) and `config` (ProviderConfig)
- Normalizes provider name via `ProviderType` enum (maps "anthropic", "openai", etc.)
- Dispatch dict: `{"openai": OpenAIProvider, "anthropic": AnthropicProvider, ...}`
- Instantiates the matching provider class with the config

### Provider Base Class
`llm_providers/base.py`:
- `BaseProvider.__init__()` stores config, validates presence of api_key
- Calls `self._create_client()` — abstract method subclasses must implement

### OpenAI-Compatible Providers
`llm_providers/openai_compatible.py`:
- `OpenAICompatibleProvider.__init__()`:
  1. Calls `super().__init__(config)` → validates api_key
  2. Sets `self.base_url` from `config.base_url` or defaults
  3. Calls `self._create_client()`:
     - Creates `openai.AsyncOpenAI(api_key=..., base_url=...)`
     - Stores in `self._client`
  4. Sets `self.model` from `config.model`
  5. Returns the `AsyncOpenAI` client object

### Anthropic Provider
`llm_providers/anthropic_provider.py`:
- `AnthropicProvider.__init__()`:
  1. Calls `super().__init__(config)` → validates api_key
  2. Sets `self.base_url` from `config.base_url`
  3. Calls `self._create_client()`:
     - Creates `anthropic.AsyncAnthropic(api_key=..., base_url=...)`
     - Stores in `self._client`
  4. Sets `self.model` from `config.model`
  5. Returns the `AsyncAnthropic` client object

### Return Path
- The provider instance (with ready-to-use HTTP client) propagates back through:
  `LLMClient._create_provider_client()` → `LLMClient.initialize()` → `Agent._initialize_llm()` → `Agent.initialize()`
- `LLMClient` stores the provider as `self._provider` for subsequent `chat_completion()` calls

### Security Layer
`thoughtmachine/security.py`:
- Not directly involved in provider creation but wraps config loading
- `SecureConfigLoader` can encrypt/decrypt sensitive config values (api keys at rest)
- Keys are decrypted before being passed to the provider config

### Vault (Bootstrap)
`thoughtmachine/vault.py`, `thoughtmachine/bootstrap.py`:
- `Vault` initializes the workspace structure
- `bootstrap.py` orchestrates initial setup (directory creation, default config copying)
- Not directly in the hot path of provider creation after initial setup

### Key Design Observations
1. **Two factory patterns**: The config system uses `ConfigService` as a facade; the provider system uses `create_provider()` function dispatch
2. **Credential injection is decoupled**: Happens at config level, not provider level — providers always see fully-resolved credentials
3. **Client creation is the provider's responsibility**: Each provider subclass implements `_create_client()` differently
4. **No lazy initialization**: Providers create their HTTP clients eagerly in `__init__()` — failures surface immediately
5. **Config defaults cascade**: `factory_defaults.json` → `default_config.json` → user config → env vars → CLI args (later overrides earlier)


## SOURCE: development_guides.md — archived (stale/superseded)
## Current Status
- No guides recorded yet.

## Setup
(To be populated)

## Conventions
(To be populated)

## Workflows
(To be populated)

## DockerCodeRunner Usage

## 2026-05-07 — Phase 1 Complete: Branch Creation & Switching

### New Me...

## Phase 1 Complete: Branch Creation & Switching

## Phase 1-3: Git Branch Operations — 🔴 NEVER IMPLEMENTED

**Correction (2026-KB-AUDIT):** The Git branch operations documented below in Phases 1-3 (`create_agent_branch`, `switch_branch`, `cleanup_agent_branch`, `commit_on_agent_branch`, `sync_agent_with_dev`, `merge_agent_to_dev`) were **planned but never implemented** in the actual codebase.

`GitInfoTool` at `tools/git_info_tool.py` only supports: `status, diff, log, branch, show, remote, blame, config, commit, init, clone`. None of the agent-branch-specific operations exist.

If these features are needed in the future, the original design docs are preserved below as a starting point for implementation.

### Archived Design — Phase 1: Branch Creation & Switching (Never Implemented)
**Planned methods (3 methods, ~220 lines):**
1. `_create_agent_branch(repo_root)` — Creates `agent_{base}_{suffix}` branch from base, validates suffix format, checks for duplicates
2. `_switch_branch(repo_root)` — Switches to existing branch (agent branches + readonly branches allowed)
3. `_cleanup_agent_branch(repo_root)` — Stashes changes, switches to merge target, safe-deletes branch

**Planned fields:** `branch_suffix: Optional[str]`, `base_branch: str = "dev"`, `branch_name: Optional[str]`

**Planned operations:** `"create_agent_branch"`, `"switch_branch"`, `"cleanup_agent_branch"`

### Archived Design — Phase 2: Commit on Agent Branches (Never Implemented)
**Planned fields:** `commit_message: Optional[str]`, `file_paths: Optional[List[str]]`, `add_all: bool = False`

**Planned operation:** `"commit_on_agent_branch"`

### Archived Design — Phase 3: Sync and Merge (Never Implemented)
**Planned fields:** `prose_message: Optional[str]` (200 char max)

**Planned operations:** `"sync_agent_with_dev"`, `"merge_agent_to_dev"`

## 2026-05-07 — Phase 2 Complete: Commit on Agent Branches

**New Pydanti...

## Phase 2 Complete: Commit on Agent Branches

## Phase 2 Complete: Commit on Agent Branches

**🔴 NOT IMPLEMENTED — See "Phase 1-3: Git Branch Operations" section above for archived design docs.**

## Phase 3 Complete: Sync and Merge

## Phase 3 Complete: Sync and Merge

**🔴 NOT IMPLEMENTED — See "Phase 1-3: Git Branch Operations" section above for archived design docs.**

## 2026-05-07 — Phase 3 Complete: Sync and Merge

**New Pydantic fields:*...

## Phase 3 Complete: Sync and Merge

**New Pydantic fields:**
- `prose_message: Optional[str]` — merge commit message (200 char max, required for merge_agent_to_dev)

**New operation Literal values:**
- `"sync_agent_with_dev"`
- `"merge_agent_to_dev"`

**Readonly guard integration:**
- Added `readonly_guarded_ops` set in `execute()` — calls `_assert_not_readonly_branch()` for Phase 1/2 ops but NOT for sync/merge (which legitimately write to dev)

### `_sync_agent_with_dev(repo_root)`
1. Validates on agent branch (not detached HEAD, starts with prefix)
2. Checks no uncommitted changes (`git status --porcelain`)
3. `git fetch origin dev`
4. `git merge origin/dev --no-edit`
5. On `GitWriteError`: `git merge --abort`, then `git diff --name-only --diff-filter=U` to list conflicted files
6. Returns success or conflict report (never auto-resolves)

### `_merge_agent_to_dev(repo_root)`
1. Validates on agent branch
2. Validates `prose_message` is non-empty and ≤200 chars
3. Checks no uncommitted changes
4. `git checkout dev`, `git pull origin dev`
5. `git merge --no-ff <agent_branch> -m "<prose_message>"`
6. On conflict: abort, list conflicted files
7. Post-merge: checks `delete_agent_after_merge` config flag (default `False`), safe-deletes branch if True

**Phase 1-3 complete.** Full write operations: create branch, switch, cleanup, commit, sync with dev, merge to dev.

## 2026-05-09 — FileEditor `line_number` vs `line_numbers` — subtle disti...

## 2026-05-14 — Chat Display Overhaul (2025-01-17)

Replaced `ChatPanel.j...

## Chat Display Overhaul (2025-01-17)

Replaced `ChatPanel.jsx` with a full-featured chat display supporting:
- **Markdown rendering** via `react-markdown` + `remark-gfm` for assistant and reasoning content
- **Tool calls** displayed as expandable `<details>` with 🛠️ icon and formatted JSON args
- **Long tool results** truncated at 500 chars with "▼ Show more" toggle
- **Reasoning blocks** as 💭 Thinking collapsible `<details>` with markdown rendering
- All roles (user, assistant, tool_call, tool_result, system) get distinct bubbles
- Prop renamed from `history` to `messages` to match SessionTab usage
- Added ~200 lines of CSS in `styles.css` for markdown styling, reasoning blocks, tool call details, and truncation toggle

**Files changed:**
- `frontend/src/components/ChatPanel.jsx` — full rewrite (51→137 lines)
- `frontend/src/styles.css` — appended ~200 lines of new CSS
- `frontend/src/components/SessionTab.jsx` — already updated with `messages={state.history}`

## 2026-05-14 — Bridge debug logging added

## Bridge Debug Logging Added (2025-01-17)

Added debug logging to `web_ui/backend/bridge.py`:

1. **`_emit` method** — Logs structured `conversation_changed` events with:
   - Message count and roles array
   - Per-message `reasoning_content` presence flags
   - A `sample_tool_msg` (first tool_call or tool_result message found) for diagnostic inspection

2. **`_on_controller_event` method** — Logs raw controller events before translation for types: `turn`, `tool_call`, `tool_result`, `user_query`, `final`, `execution_state_change`, `token_update`, `reasoning`
   - Includes full event dict and keys list

3. **Truncation** — Both `log()` calls use default `truncate_hint=None`, which means `_truncate_data` passes data through unchanged, preserving full diagnostic data in JSONL file logs. Console output may still truncate the display line per `TM_DEBUG_TRUNCATE_LENGTH`.

## 2026-05-30 — Feature: `--serve-frontend` CLI flag (2026-06-02)

Added ...

## 2026-05-30 — Install & Run Scripts (created 2026-05-30)

Two scripts w...

## Pre-Release Fixes Applied

## 2026-05-31 — **2026-06-02 — Three pre-release fixes applied:**

1. **Fixe...

**2026-06-02 — Three pre-release fixes applied:**

1. **Fixed hardcoded workspace path** (`resources/default_config.json`): Changed `workspace_path` from `"/home/jojo/PycharmProjects/ThoughtMachine-dev"` to `""` — new users no longer get a broken path copied to their config.

2. **Cleaned stale tool names** (`resources/default_config.json`): Removed `"Final"`, `"FinalReport"`, `"RequestUserInteraction"` from `enabled_tools` — these were consolidated into `"Respond"` and no longer exist as tools.

3. **Install script polish** (`install_thoughtmachine.sh`):
   - Added `chmod +x` for both `start_thoughtmachine.sh` and `install_thoughtmachine.sh` at end of install
   - Improved completion message: numbered next-steps, mentions auto-config creation, shows URL

4. **Handbook correction** (`resources/global_kb/handbook.md`): Updated "First Run" section to reflect that config is auto-created by the server bootstrap, not manually.

**Files changed:** resources/default_config.json, install_thoughtmachine.sh, resources/global_kb/handbook.md

## Build Scripts

## 2026-05-31 — Build Scripts

**2025-07-14**: Created two build scripts ...

## Build Scripts

**2025-07-14**: Created two build scripts for PyInstaller packaging:

- **`build_thoughtmachine_exe.sh`** (Linux/macOS) — Bash script, 5 steps: (1) build React frontend, (2-4) check/install Python deps, (5) run PyInstaller in one-folder (default via `thoughtmachine.spec`) or one-file mode (`ONE_FILE=1`).
- **`build_thoughtmachine_exe.bat`** (Windows) — Batch script equivalent with same 5 steps. Uses `set ONE_FILE=1` for one-file mode. Uses Windows path separators throughout. Helpers: `:info`, `:ok`, `:warn`, `:err` subroutines.

## Requirements

## 2026-05-31 — Requirements Split (2025-07-14)

Split `requirements.txt`...

## Requirements Split (2025-07-14)

Split `requirements.txt` into core + optional RAG to reduce venv bloat:

- **`requirements.txt`** — Core dependencies only (FastAPI, uvicorn, pydantic, openai, anthropic, tiktoken, docker, etc.). ~200 MB venv.
- **`requirements-rag.txt`** — Optional RAG stack (CPU-only PyTorch via `--index-url`, sentence-transformers, chromadb, langchain). ~500 MB extra.
- **`install_thoughtmachine.sh`** — Now accepts `--with-rag` flag to install RAG deps.

Removed from core: `PyQt6` (legacy GUI, not needed for web UI), `sentence-transformers`, `chromadb`, `langchain`, `langchain-community`.


## 2026-06-01 — 2026-06-03 — New User Onboarding System (Created)

### Wh...

## 2026-06-03 — New User Onboarding System (Created)

### What was done
1. **Created `user/onboarding_guide.md` in global KB** — A friendly, non-technical guide for new ThoughtMachine users. Explains concepts in plain language (workspace, session, KB). Gives suggested "first things to say." Doesn't assume prior knowledge. Warm, guided tone.

2. **Added Rule 14 to system prompt** — Both `system_prompt.txt` (root, actually loaded) and `resources/default_system_prompt.txt` (template) now contain:
   > *"When interacting with someone who seems new to ThoughtMachine, offer a guided, friendly experience. Do not assume prior knowledge — explain concepts like workspaces, sessions, and the knowledge base in plain language. Check the global KB's `user/onboarding_guide.md` for a ready-to-use friendly introduction. Suggest clear next steps. Invite questions."*

### Still open / not implemented
- **No first-time user detection mechanism** — The agent needs some way to know it's talking to a new user. Options: check for a marker in global KB (e.g., `user/user_profile.md`), or simply run onboarding when the user seems confused.
- **The "View Artifact" tool** — Previously brainstormed idea. Could pair well with onboarding (agent generates a welcome page and presents it visually).


## 2026-06-07 — # 🕵️ The Case of the Missing Panel — Full Investigation Repo...

# 🕵️ The Case of the Missing Panel — Full Investigation Report

**To**: GUI Engineer  
**From**: ThoughtMachine AI  
**Date**: 2026-06-07  
**Subject**: Complete investigation log — ConfigPanel sidebar + Docker panel feature request

---

## 1. How It Started

The user (jojo) reported: *"my Config panel sidebar was once there but not currently."* Meaning: the gray sidebar on the left side of the session view (the one with config tabs like General, Model, Tools, Permissions, etc.) that they remember seeing before, is now absent from the screen.

At this point, we had uncommitted changes in the workspace:
- `agent/config/models.py` — modified
- `agent/config/provider_profile.py` — modified
(These are the "overwrite when non-empty" fix for provider profile resolution.)

## 2. Investigation Phase 1 — Is ConfigPanel Rendering?

We checked the code path:

- **SessionTab.jsx:446** — ConfigPanel IS rendered unconditionally:
  ```jsx
  <ConfigPanel
    config={state.config}
    sendCommand={sendCommand}
    providers={providers}
    ...
  />
  ```
  No `if` guard, no `isDeferred` check. If the tab is loaded, ConfigPanel renders.

- **ConfigPanel.jsx:169** — The component signature:
  ```jsx
  function ConfigPanel({ config, sendCommand, providers, availableTools, panelWidth, wsConnected, ... })
  ```

- **ConfigPanel.jsx:302-306** — The only conditional is the loading state:
  ```jsx
  if (!config) {
    return <div style={{ padding: '1rem', ..., width: panelWidth || 280, ... }}>
      Loading config...
    </div>;
  }
  ```

- **ConfigPanel.jsx:342-343** — The real render:
  ```jsx
  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244',
                  color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500,
                  flexShrink: 0, overflowY: 'auto', height: '100%' }}>
  ```

### Key findings:
- ConfigPanel uses **inline styles entirely** — no CSS class like `.config-panel` on the outer div (the `.config-panel` class in `styles.css:100` is unused vestigial CSS)
- The resize handle (`.resize-handle`) sits between ConfigPanel and the chat panel — uses `width: 5px` CSS class
- ConfigPanel is resizable via drag, persisted per tab in `localStorage` key `config-panel-width:{tabId}`

## 3. Investigation Phase 2 — Is ConfigPanel in the DOM?

The user couldn't access browser DevTools (no Elements/Inspector tab available in their Firefox). We worked around this:

- Confirmed via WS message count (907 messages received) that **the tab IS loaded and active**
- The backend sends `config_changed` events after `load_session` (server.py:721-726) — so config should arrive
- No React errors in browser console
- The user eventually found CSS via browser inspection that **exactly matches** ConfigPanel's inline styles:
  ```
  padding: 1rem;
  font-family: sans-serif;
  background: rgb(49, 50, 68);  /* = #313244 */
  color: rgb(205, 214, 244);    /* = #cdd6f4 */
  width: 280px;                  /* = panelWidth || 280 */
  min-width: 200px;
  max-width: 500px;
  flex-shrink: 0;
  overflow-y: auto;
  height: 100%;
  ```
  **Verdict: ConfigPanel IS in the DOM with correct styles.**

## 4. Investigation Phase 3 — Why Is It Not Visible?

This is where it gets tricky. ConfigPanel exists in HTML but the user says it's not visible on screen. Possible causes (not fully resolved):

| Cause | Likelihood | Notes |
|---|---|---|
| **User is looking at deferred tab** | Medium | 4 of 5 tabs are deferred ("Click tab to load conversation") — possible user was on wrong tab |
| **CSS layout clipping** | Low | Parent `.app-main` has `overflow: hidden` but ConfigPanel has `flex-shrink: 0` and fixed width |
| **Browser zoom/scroll** | Low | Could be off-screen to the right |
| **ConfigPanel is there but user didn't notice** | Low | Unlikely given user's certainty |

## 5. Plot Twist — It's Not ConfigPanel!

After the investigation, the user revealed: **"we are looking for the container panel, the docker thing"**

So the entire investigation was a misunderstanding! The user was NOT looking for the Config sidebar. They were looking for a **Docker containers panel** — a UI component that:

- **Does not exist** in the codebase
- Was never built
- Has no placeholder, no route, no component file
- Only Docker-related code is the `DockerCodeRunner` tool listing and a "Container" permission toggle in ConfigPanel's Permissions tab (lines 633-653)

## 6. Current State

### Uncommitted changes (the provider profile fix):
```
 M agent/config/models.py
 M agent/config/provider_profile.py
```

### Branch situation:
- Currently on **detached HEAD** at `4b3dde3`
- Branch `master` exists
- User plans to create a new branch (likely named `docker-panel`), commit the changes, then build the Docker panel from scratch

### The user's plan:
1. ✅ Commit current changes to new branch
2. ❓ Provide full instructions for building the Docker panel UI
3. ❓ Build the Docker panel component

## 7. Technical Notes for the GUI Engineer

### Frontend architecture (relevant parts):
- **Stack**: React (Vite), vanilla CSS (inline styles + some CSS classes in `styles.css`)
- **State management**: Per-tab state via `useState`/`useCallback` in SessionTab — no Redux/Zustand
- **Backend communication**: WebSocket (`sendCommand()` / event listeners)
- **Tab system**: Up to 5 tabs, lazy-loaded (deferred pattern), state persisted per tab
- **Config delivery**: Backend sends `config_changed` event after `load_session`
- **Styling**: Catppuccin Mocha palette (`--bg-primary: #1e1e2e`, `--bg-surface: #313244`, etc.)

### ConfigPanel inline style pattern (for reference when building new panels):
```jsx
<div style={{
  padding: '1rem',
  fontFamily: 'sans-serif',
  background: '#313244',
  color: '#cdd6f4',
  width: panelWidth || 280,
  minWidth: 200,
  maxWidth: 500,
  flexShrink: 0,
  overflowY: 'auto',
  height: '100%'
}}>
```

### The `sendCommand` interface:
```jsx
sendCommand('command_name', { payload })
```
Available commands are handled in `bridge.py` and `server.py`.

---

**End of report.** Ready for Docker panel feature design.

## How to add a new permission toggle

## 2026-06-10 — How to add a new permission toggle

Every new trust domai...

## 2026-06-10 — Windows packaging: "Terminate batch job (Y/N)?" fix (2026...

## Windows packaging: "Terminate batch job (Y/N)?" fix (2026-06-03)

**Problem:** `start_thoughtmachine.bat` used `powershell Start-Process` to launch Python, but `cmd.exe` (the batch file's parent console) still owned the console. When the user pressed Ctrl+C, `cmd.exe` intercepted it and prompted "Terminate batch job (Y/N)?" before the batch could continue.

**Root cause:** `cmd.exe` runs batch files synchronously. When Ctrl+C is pressed in the console, `cmd.exe`'s default handler prompts before executing the next batch line — including `exit /b`.

**Fix:** Replaced `powershell -Command "Start-Process python ... -NoNewWindow -PassThru; $p.WaitForExit(); exit $p.ExitCode"` with `start "ThoughtMachine Backend" /wait python ...`. The `start` command launches Python in a new console window (its own process group), so Ctrl+C only reaches Python, not the parent `cmd.exe`. After Python exits, the new window closes, the batch continues to `exit /b`, and **no prompt appears**.

**Key principles:**
- `start "" /wait` creates a new process in its own console — Ctrl+C isolation
- `start /b` (same-window) makes the app ignore Ctrl+C — NOT what we want
- Vite was already launched in a separate window via `start "ThoughtMachine Vite" cmd /c "npm run dev"` — the backend now follows the same pattern
- The `exit /b %ERRORLEVEL%` after `start /wait` propagates Python's exit code

## 2026-07-01 — Second instance port configuration (2025-07-16)

### How ...

## 2026-07-01 — 2026-07-02 — Master Vault: Design Principles (10 Sacred R...

## 2026-07-09 — Cross-Session Worker Panel Access — Changes Implemented

...


## Adding a New Event Type — Checklist

Every new event typ...


## inventory_partial_results

## 2026-07-10 — **Root files:** _run_git_cmds.py (136L, funcs: run, main), _...

**Root files:** _run_git_cmds.py (136L, funcs: run, main), _runner.py (9L), check_syntax.py (0L), docker_executor.py (1051L, class:DockerExecutor, 13 funcs), run_git_commands.py (139L, funcs: run, main), setup_workspace.py (57L, func: main), thoughtmachine_entry.py (48L, func: main)

**agent/cli/:** __init__.py(1L), main.py(40L, func:main), rag_commands.py(200L, 5 funcs)
**agent/config/:** __init__.py(35L), loader.py(481L, 21 funcs), models.py(243L, class:AgentConfig), preset.py(117L, classes:Preset,PresetLoader), provider_profile.py(177L, classes:ProviderProfile,ProviderManager), service.py(284L, class:ConfigService)
**agent/controller/:** __init__.py(709L, class:AgentController, 23 methods)
**agent/core/:** __init__.py(13L), agent.py(1479L, class:Agent, 39 methods), conversation_manager.py(142L, class:ConversationManager), debug_context.py(101L, class:DebugContext), llm_client.py(172L, classes:LLMError,LLMClient), message.py(39L, class:Message), message_utils.py(199L, 2 funcs), state.py(323L, 6 classes: TokenState,TurnState,ExecutionState,TimeState,SessionState,AgentState), token_counter.py(111L, class:TokenCounter), tool_executor.py(344L, class:ToolExecutor), turn_transaction.py(196L, class:TurnTransaction), worker_context.py(216L, class:WorkerContext)
**agent/knowledge/:** base.py(73L, class:BaseKnowledgeBase), codebase_indexer.py(1279L, 24 funcs), codebase_kb.py(344L, class:LocalCodebaseKB), dependencies.py(90L, func:check_rag_dependencies), global_kb.py(169L, 4 funcs)
**agent/logging/:** __init__.py(711L, classes:LogLevel,LogCategory,LogEventType,_AgentLogger), debug_log_adapter.py(172L, class:LogAnalyzer), unified.py(549L, class:LogLevel, 20 funcs)
**agent/models/:** __init__.py(3L), worker_definition.py(62L, class:WorkerDefinition)
**agent/presenter/:** __init__.py(16L), agent_presenter.py(416L, class:RefactoredAgentPresenter), event_processor.py(307L, class:EventProcessor), gui_integration.py(95L, classes:GUIIntegration,_DummySignal), session_lifecycle.py(537L, class:SessionLifecycle), state_bridge.py(305L, class:StateBridge)
**agent/:** events.py(519L, 20 classes, 8 funcs), logging_helpers.py(46L, func:dump_messages), startup_health_check.py(331L, classes:CheckResult,HealthReport, 7 funcs), utils.py(42L, func:deep_merge)

**llm_providers/:** __init__.py(38L), anthropic_provider.py(203L, class:AnthropicProvider), base.py(121L, classes:LLMResponse,ProviderConfig,LLMProvider), exceptions.py(43L, 10 exception classes), factory.py(136L, class:ProviderFactory), openai_compatible.py(549L, class:OpenAICompatibleProvider), tool_converter.py(168L, class:ToolFormatConverter)

**security/:** __init__.py(1L), security_gate.py(445L, 8 funcs)

**session/:** context_builder.py(419L, classes:ContextBuilder,SummaryBuilder), event_schema.py(450L, 15 TypedDict classes, 18 funcs), history_provider.py(262L, class:HistoryProvider), history_pruner.py(387L, class:PruningPolicy, 6 funcs), lock.py(175L, classes:FileLockTimeoutError,FileLock), models.py(466L, classes:ObservableList,RuntimeParams,ContainerMetadata,Session), store.py(738L, classes:SessionStore,FileSystemSessionStore), utils.py(39L, func:normalize_conversation_for_hash)

**thoughtmachine/:** __init__.py(0L), audit_logger.py(74L, 3 funcs), bootstrap.py(172L, 10 funcs), security.py(1090L, 5 classes, 20 funcs), workspace_capabilities.py(471L, class:WorkspaceCapabilities, 14 funcs)

**mcp_examples/:** test_client.py(217L, 6 funcs), test_with_agent_mcp_client.py(74L)


## 2026-07-10 — Test Infrastructure Summary
- **44 test files** total (43...

## Worker Tool Usage Rules

## 2026-07-10 — Worker Tool Usage Rules (Critical)

**NEVER use `spawn` w...

## 2026-07-12 — 2026-07-12: Backend Startup Issue Fixed

**Problem:** Fas...

## 2026-07-12: Backend Startup Issue Fixed

**Problem:** FastAPI backend (web_ui/backend/server.py) was not running. Frontend (Vite on :5173) showed:
1. WebSocket connection failure to ws://localhost:8000/ws
2. CORS error on GET /api/logging/config (actually caused by backend being unreachable)
3. "Backend seems not running" - this was correct

**Root Cause:** Backend process was not started. Missing Python dependencies (`fast_json_repair`, `libcst`, `tiktoken` and many others from requirements.txt).

**Fix Applied:**
1. Installed all missing dependencies: `pip3 install -r requirements.txt`
2. Started backend: `python3 -m web_ui.backend.server --host 0.0.0.0 --port 8000`

**Verification:**
- Server listening on 0.0.0.0:8000 (confirmed via /proc/net/tcp)
- GET /api/logging/config → HTTP 200 with proper response
- CORS headers properly configured (allow_origins=["*"]), verified returning `access-control-allow-origin: http://localhost:5173`
- WebSocket route @app.websocket("/ws") is properly defined in server.py
- Vite proxy config correctly proxies /ws and /api to localhost:8000

**Note:** The CORS error "CORS request did not succeed" was misleading — it was actually a connection refused error because no server was listening on port 8000.


## 2026-07-18 — Phase 1, Step 1.1 — Workspace Registry

**Files created:*...

## Phase 1, Step 1.1 — Workspace Registry

**Files created:**
- `thoughtmachine/workspace_registry.py` — persistent workspace registry module
- `tests/workspace/test_workspace_registry.py` — 27 tests (all passing)

**Module API:**
- `WorkspaceRegistryEntry` — dataclass with `id`, `root_path`, `label`, `created_at`, `updated_at`, `last_opened`, `metadata`
- `WorkspaceRegistry` — thread-safe JSON-backed registry at `~/.thoughtmachine/workspace_registry.json`
  - `list_workspaces()` — sorted by label then id
  - `get_workspace(id)` — single entry lookup
  - `register_workspace(id, root_path, label, metadata)` — register new (raises on duplicate)
  - `unregister_workspace(id)` — remove entry
  - `update_workspace(id, **updates)` — update label, root_path, last_opened, metadata
  - `resolve_by_root(path)` — replace for ad-hoc `resolve_workspace_id()`
  - `get_default()` — cached singleton

**Key design decisions:**
- Standalone module (no imports from other thoughtmachine modules) to avoid circular deps
- Uses same `_user_dir()` / `Path.home` patching pattern as `workspace_capabilities.py`
- Atomic writes via `.tmp` + `os.replace`
- Thread-safe via `threading.Lock`

## 2026-07-18 — SessionCreationModal.jsx — Created

**File:** `web_ui/fro...

## SessionCreationModal.jsx — Created

**File:** `web_ui/frontend/src/components/SessionCreationModal.jsx`

A reusable modal for creating a new session with:
- **Mode selector** — Agent / Engineer / Custom (card-style buttons with icons + dynamic description below)
- **Workspace selector** — Toggle between Default / Recent (dropdown) / Custom Path (text input + inline Directory Browser using the `/api/browse` endpoint)
- **Sensitive directory warning** — Detects patterns like `/etc`, `/root`, `/sys`, `/proc`, `/dev`, `/boot`, `/.ssh`, `/.config`, `/.aws`, `/.kube`, `/.docker` and shows a yellow warning banner
- **Validation** — Blocks creation if recent workspace is unselected or custom path is empty
- **Styling** — Matches Catppuccin Mocha theme (`#313244` surface, `#1e1e2e` base, `#89b4fa` accent, `#a6e3a1` green button, `#f38ba8` errors, `#f9e2af` warnings)

**Props:**
- `onClose` — dismiss callback
- `onCreate({ mode, workspacePath })` — called with `mode` string ('agent'|'engineer'|'custom') and `workspacePath` (string or undefined for backend default)
- `recentWorkspaces` — array of `{ path, label }` for the dropdown
- `apiBase` — base URL for `/api/browse` (auto-derived from VITE_BACKEND_PORT if omitted)

**Integration notes for App.jsx:**
- Import the modal: `import SessionCreationModal from './components/SessionCreationModal'`
- Add state: `const [showCreationModal, setShowCreationModal] = useState(false)`
- Replace `handleNewTab` to open modal instead of directly sending `new_session`
- The modal's `onCreate` callback should send `new_session` with the selected options

## 2026-07-23 — Raw Tool Call Diagnostic Log

**File:** `logs/tool_calls_...

## Raw Tool Call Diagnostic Log

## Raw Tool Call Diagnostic Log

**File:** `logs/tool_calls_raw_debug.log` (in workspace root)
**Purpose:** Logs every tool call's raw `arguments` JSON string *before* `json.loads()` parsing, to debug whether empty `{}` args come from the LLM API or from processing.
**Mechanism:** Patch in `agent/core/tool_executor.py` at the `execute_tool_calls` method (around line 132), intercepts `arguments_str` before `json.loads()`.
**Size limit:** 2MB max. When exceeded, the file is **truncated** (wiped clean with a timestamp header). No rotated files, no debris.
**Usage:** When the empty `{}` bug is observed, check `logs/tool_calls_raw_debug.log` immediately — the relevant call will be near the bottom of the file.

## SOURCE: roadmap.md — completed phases (Phase 2, Phase 2.5, Tasks 1-4)
## 2026-05-11 — Phase 2.5 — Multi‑Session Tab Support & Full Session Rest...

## Phase 2.5 — Multi‑Session Tab Support & Full Session Restore 🟢 NOW

| Item | Description | Status |
|------|-------------|--------|
| 2.5.1a | **Backend: load_session also loads agent_config** — extract `session.metadata['agent_config']` and store as overrides so next `start_session` uses saved config (system prompt, tools, etc.) | Planned |
| 2.5.1b | **Backend: new `get_open_sessions` command** — returns session IDs from `open_sessions.json` | Planned |
| 2.5.1c | **Backend: new `close_session` command** — save session, remove from open list, stop bridge | Planned |
| 2.5.1d | **Backend: WebSocket disconnect handler** — treat unexpected close as tab close (save + remove from open list) | Planned |
| 2.5.2a | **Frontend: Tab bar component** — replace single-session view with tab manager; each tab has `tabId`, `sessionId`, `ws`, local chat state | Planned |
| 2.5.2b | **Frontend: Each tab is independent** — refactor `App.jsx` into `SessionTab` component with own WebSocket lifecycle; per-tab Zustand context or local state | Planned |
| 2.5.2c | **Frontend: Initialisation flow** — on load, send `get_open_sessions`, open tabs for each returned session_id via `load_session` | Planned |
| 2.5.2d | **Frontend: "+" button** — creates blank tab; on first query sends `start_session` with default config | Planned |
| 2.5.2e | **Frontend: Tab close** — send `close_session`, close WebSocket, remove tab | Planned |
| 2.5.2f | **Frontend: Config per session** (deferred to Phase 3) | Deferred |

### Architectural Mapping
| Old PyQt6 GUI | New Web GUI |
|---------------|-------------|
| Each `SessionTab` has its own Presenter + Controller | Each browser tab gets its own WebSocket → backend spawns `WebAgentBridge` + `AgentController` |
| `QTabWidget` with "+" button | React tab bar component, "+" creates new WebSocket connection |
| On start, restore open sessions from `open_sessions.json` | Frontend sends `get_open_sessions`, opens tabs for each session_id with `load_session` |
| Close tab → save session | Frontend sends `close_session`, backend saves + updates open list, client closes WebSocket |
| Load session from file → `presenter.load_session(filepath)` | WebSocket command `load_session { session_id }` → bridge loads session with full agent_config from metadata |

### Design Notes
- No agent logic changes — this is a thin-shell reproduction of the old PyQt6 tab system
- `load_session` must extract `session.metadata['agent_config']` so that system prompt, tools, and settings are restored exactly as saved
- `close_session` passes through to existing `SessionLifecycle`/`FileSystemSessionStore` methods — no new logic
- Per-tab WebSocket isolation ensures each tab is an independent "mini-app"

## 2026-06-12 — 🎉 **Phase 2 Complete** (2025-04-11): Workspace config files ...

🎉 **Phase 2 Complete** (2025-04-11): Workspace config files REST API is fully implemented.
- `GET/PUT /api/workspace/{ws_id}/domain_allowlist` (with atomic write)
- `GET /api/workspace/{ws_id}/dockerfile` (from workspace dir)
- `GET /api/workspace/{ws_id}/workers` (empty JSON)
- `GET /api/workspace/{ws_id}/mcp_servers` (empty JSON)
- `GET /api/workspace/{ws_id}/effective_permissions` (with session permission merging & fallback)
- `ensure_workspace_dirs()` creates all config files idempotently
- 18 tests covering bootstrap + API endpoints all passing


## 2026-07-22 — Task 1 — De-emojify GUI + remove Save button ✅
- **Config...

## Task 1 — De-emojify GUI + remove Save button ✅
- **ConfigPanel.jsx**: Mode badges (🤖→Agent, ⚙️→Engineer, 🎨→Custom), lock icons (🔒→(locked)), factory prompt emoji removed
- **SessionTab.jsx**: Save button removed entirely, Rename (✏️→Rename), Delete (🗑️ Delete→Delete), Yes/No confirm buttons de-emojified
- **SessionList.jsx**: Rename (✏️→Rename), Delete (🗑️→Delete)

## Task 2 — System prompt audit ✅
- Traced system_prompt save/load path: apply_config→update_prompt→model_dump→SessionConfig(**raw_dict)
- No bug found in Custom mode persistence — save/load pipeline is clean
- Mode-locked prompting in Agent/Engineer is intentional (validator re-reads factory file)

## Task 3 — Tool audit ✅
- **3a CodeModifier**: Syntax test PASS — py_compile exit code 0. Minor cosmetic formatting quirk (no blank line before decorator) but syntactically valid.
- **3b Tool call log**: Temporary tool_calls.log added to bridge.py at two dispatch points, then removed in final sweep.
- **3c Bash equivalence**: Table of 5 tools. Key finding: silent truncation across FileEditor, GlobTool, DirectoryTreeTool — LLMs get incomplete results without notification.

## Task 4 — Chat scroll anchoring ✅
- **R1 Auto-scroll at bottom**: Already worked (20px threshold in handleScroll + useLayoutEffect)
- **R2 No yank when reading history**: Already worked (isAtBottomRef guard)
- **R3 Force scroll after summary**: NEW — SessionTab.jsx detects compaction keywords (context now free, summar, compact, messages removed) and increments scrollToBottomKey prop; ChatPanel.jsx watches it and force-scrolls via double-rAF
- **R4 Jump buttons always visible**: Fixed — removed conditional `!isAtBottomRef.current` wrapper, buttons render unconditionally
