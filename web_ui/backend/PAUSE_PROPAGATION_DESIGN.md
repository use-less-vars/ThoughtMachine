# Pause Propagation: Main Agent → Workers

## Goal

When the user clicks "Pause" on the main agent, all running workers should also be paused (soft stop — finish current turn, then pause). Workers should show a **"paused"** status separately from "completed" or "error", so the user knows the worker can be resumed later.

```
User clicks [Pause]
       │
       ▼
  Main agent pauses
       │
       ├──▶ For each running worker:
       │      POST /api/workspace/{ws_id}/workers/{name}/pause
       │      ──▶ writes {"action": "pause"} to command.json
       │      ──▶ worker sees it, calls request_pause()
       │      ──▶ finishes current turn, sets status="paused"
       │
       └──▶ UI shows all workers as "paused" (yellow icon)
```

---

## Architecture

### Existing File-Based Command Mechanism

The system already has a file-based command mechanism for controlling workers from outside the agent process (e.g., from the REST API):

| File | Location | Purpose |
|------|----------|---------|
| `command.json` | `workers/<session_id>/<name>/command.json` | Written by the REST API; read by the worker thread |
| `status.json` | `workers/<session_id>/<name>/status.json` | Written by the worker thread; read by the REST API |

**`command.json`** currently supports one action:

```json
{"action": "stop"}
```

**`_poll_command()`** (worker.py, lines 420–442) checks for this file on every event iteration in the tool loop. When found with `"action": "stop"`:

1. Delete the command file
2. Set `self._stop_event.set()`
3. Unblock the input queue with `self._input_queue.put(None)`

The tool loop then checks `self._stop_event.is_set()` and calls `self._agent.request_pause()` before breaking.

### Worker Directory Layout

Two layouts are supported (both handled by `GET /api/workspace/{ws_id}/workers`):

- **Session-scoped**: `workers/<session_id>/<name>/` (created by `WorkerThread.__init__` when `session_id` is set — worker.py line 283)
- **Legacy**: `workers/<name>/` (worker.py line 285, fallback when no `session_id`)

### Agent.request_pause()

`agent/core/agent.py` line 761 already has `request_pause()`:

```python
def request_pause(self):
    self._pause_requested = True
```

The agent's event loop checks `self._pause_requested` and stops yielding new events. The worker's `_run_tool_loop` already calls `self._agent.request_pause()` at line 574 when `_stop_event.is_set()`.

**Key insight**: The stop mechanism already *is* a pause — the agent finishes its current turn, then the worker loop exits gracefully. We just need a separate command and status to distinguish "paused (can resume)" from "completed (done)".

---

## Current Worker Thread Lifecycle

Status state machine (worker.py line 257):

```
                         ┌─────────────────────────────┐
                         │                             │
                         ▼                             │
ready ──spawn──▶ ready ──query──▶ busy ──done──▶ ready─┘
  ▲                    │              │
  │                    ├──stop──────▶ completed
  │                    └──error─────▶ error
  │
  └────────────────── spawn again ◄──┘
```

Status values: `"ready"`, `"busy"`, `"completed"`, `"error"`

---

## Proposed Changes

### 1. New Status: `"paused"`

**File**: `tools/workspace/worker.py`

Add `"paused"` to the valid status set in `WorkerThread.__init__` (line 307):

```python
# Before:
self.status: str = "ready"      # ready | busy | completed | error

# After:
self.status: str = "ready"      # ready | busy | paused | completed | error
```

Update the lifecycle diagram in the docstring (line 257):

```
                         ┌──────────────────────────────┐
                         │                              │
                         ▼                              │
ready ──spawn──▶ ready ──query──▶ busy ──done──▶ ready──┘
  ▲                    │            │                   │
  │                    ├──stop────▶ completed            │
  │                    ├──pause───▶ paused ──resume──▶ ready
  │                    └──error───▶ error                │
  │                                                      │
  └──────────────────── spawn again ◄────────────────────┘
```

### 2. Handle `"action": "pause"` in `_poll_command()`

**File**: `tools/workspace/worker.py`, method `_poll_command()` (lines 420–442)

Add a new branch for `"action": "pause"`:

```python
def _poll_command(self) -> None:
    cmd_path = self._worker_dir / "command.json"
    if not cmd_path.is_file():
        return
    try:
        data = json.loads(cmd_path.read_text(encoding="utf-8"))
        action = data.get("action")
        if action == "stop":
            cmd_path.unlink(missing_ok=True)
            self._stop_event.set()
            self._input_queue.put(None)
        elif action == "pause":
            cmd_path.unlink(missing_ok=True)
            self.status = "paused"
            self._write_status_file()
            self._stop_event.set()
            self._input_queue.put(None)
    except (json.JSONDecodeError, OSError):
        try:
            cmd_path.unlink(missing_ok=True)
        except OSError:
            pass
```

**Why this works**: The existing `_run_tool_loop` already checks `self._stop_event.is_set()` and calls `self._agent.request_pause()` before breaking. By setting `self.status = "paused"` before signalling the stop event, the status file is written with `"paused"` before the tool loop exits.

### 3. Update `run()` Lifecycle Handling

**File**: `tools/workspace/worker.py`, around lines 872–889

The `run()` method's exception/else blocks set status to `"error"` or `"completed"` after the tool loop exits. We need to preserve the `"paused"` status:

```python
except Exception as exc:
    # Only set error if not already paused
    if self.status != "paused":
        logger.exception("Worker thread %s failed", self.worker_name)
        self.status = "error"
        self.error = str(exc)
        ...
else:
    # Only set completed if not already paused
    if self.status != "paused":
        self.status = "completed"
        self._write_status_file()
        ...
```

This ensures that when the tool loop exits due to a pause signal, the status stays as `"paused"` rather than being overwritten to `"completed"`.

### 4. New API Endpoint: `POST /api/workspace/{ws_id}/workers/{name}/pause`

**File**: `web_ui/backend/workspace_routes.py`

Add a new pause endpoint following the same directory-discovery pattern as the fixed `stop_worker`:

```python
@router.post("/{ws_id}/workers/{name}/pause")
async def pause_worker(ws_id: str, name: str):
    """Pause a running worker after it completes its current turn.

    Writes ``{"action": "pause"}`` to the worker's ``command.json`` so that
    the worker thread picks it up, finishes its current turn, and transitions
    to ``paused`` status.

    Supports both directory layouts (session-scoped and legacy).
    """
    ensure_workspace_dirs(ws_id)
    ws_dir = _workspace_dir(ws_id)
    workers_dir = ws_dir / "workers"

    # Find the worker directory (session-scoped first, then legacy)
    worker_dir: Optional[Path] = None

    if workers_dir.is_dir():
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            first_child = next(subdir.iterdir(), None) if subdir.is_dir() else None
            if first_child is not None and first_child.is_dir():
                # Session-scoped: workers/<session_id>/<name>/
                candidate = subdir / name
                if candidate.is_dir():
                    worker_dir = candidate
                    break
            else:
                # Legacy: workers/<name>/
                if subdir.name == name:
                    worker_dir = subdir
                    break

    if worker_dir is None or not worker_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"status": "not_found", "name": name},
        )

    # Write the pause command
    _atomic_write_json({"action": "pause"}, worker_dir / "command.json")

    # Immediately write status.json with "paused" for optimistic UI update
    _atomic_write_json({
        "runtime_status": "paused",
        "current_task": None,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }, worker_dir / "status.json")

    # Fast-path: signal in-memory thread
    with _registry_lock:
        for (sid, wname), thread in list(_worker_registry.items()):
            if wname == name:
                try:
                    thread.stop()  # stop() sets _stop_event; we've already set status="paused" via command
                except Exception:
                    pass

    return {"status": "ok", "name": name}
```

**Note**: The pause endpoint reuses `thread.stop()` internally (which sets `_stop_event`). However, the `_poll_command()` has already set `self.status = "paused"` by the time `_stop_event` is checked in `_run_tool_loop`. The fast-path can also just set a flag directly if needed, but using `stop()` is safe because the pause command file has already written the desired paused status.

### 5. UI Integration (Frontend)

When the user clicks the main "Pause" button:

1. The frontend already sends a pause WebSocket event to the main agent
2. **New behavior**: After sending the pause event to the main agent, the frontend should also:
   - Fetch all workers: `GET /api/workspace/{ws_id}/workers`
   - Filter workers where `runtime_status` is `"busy"` or `"ready"`
   - For each, call: `POST /api/workspace/{ws_id}/workers/{name}/pause`
3. The frontend should show a "pausing..." spinner next to each worker being paused
4. On the next status poll, workers with `runtime_status: "paused"` should show a **yellow/pause icon**

### 6. Resume Mechanism

Workers in `"paused"` state can be resumed:

- **Context is already persisted**: The `run()` method's `finally` block calls `_save_context()` before the thread exits
- **Restarting**: The main agent can call the Worker tool's `spawn` action again for the same worker name
- **No fresh context**: When spawning without `_initial_context`, `_load_context()` restores the full conversation history from `context.json`
- **Thread cleanup**: The paused thread should be cleaned up from the registry (either on pause or on next spawn)

```python
# In Worker._action_spawn (worker.py around line 1287):
# The force-respawn logic already handles stopping stale instances
# and _find_all_worker_threads() finds them across all sessions.
```

---

## Decision Points

### Should paused workers be resumable?

**Yes.** Context is already persisted via `_save_context()`. When the worker is spawned again (with no fresh context provided), `_load_context()` in `run()` restores the full conversation. The worker picks up exactly where it left off, including any tool results and LLM responses.

### Separate "paused" icon in UI?

**Recommend a yellow/pause icon (❚❚).** Distinct from:

| Status | Icon | Color | Meaning |
|--------|------|-------|---------|
| `ready` | ● | Gray | Idle, waiting for work |
| `busy` | ⟳ | Blue | Actively processing |
| `completed` | ✓ | Green | Finished successfully |
| `error` | ✗ | Red | Failed |
| `paused` | ❚❚ | Yellow | Suspended, can resume |

### Should main agent "Stop" cascade to worker stop too?

**Yes, and it already does.** `shutdown_workers()` (worker.py line 134) is registered as an `atexit` handler and called from the bridge's `close_session`. It iterates all registry entries and calls `thread.stop()` on each. No changes needed here.

"Pause" and "Stop" are distinct user actions:
- **Pause**: Soft stop — finish current turn, preserve context, show as paused
- **Stop**: Hard stop — finish current turn, preserve context, show as completed

### What about workers that are "ready" (idle) when pause is triggered?

Idle workers should also transition to `"paused"`. Since they're not actively processing, the transition is instantaneous:
1. Write `{"action": "pause"}` to `command.json`
2. Set `runtime_status: "paused"` in `status.json`
3. If in-memory, call `thread.stop()` to wake the thread from its input queue wait

The thread's `run()` loop (around line 837) will check `_stop_event` on the next iteration and exit, preserving the `"paused"` status.

### Should the main agent auto-resume workers when unpaused?

**Not automatically, at least not in v1.** The user should manually resume workers via the UI (re-spawn them). Auto-resume is complex — the main agent may have changed context, permissions, or tool sets while paused. Manual resume gives the user control.

---

## Files Modified

| File | Changes | Est. Time |
|------|---------|-----------|
| `tools/workspace/worker.py` | `_poll_command()` — add `"action": "pause"` handler | 15 min |
| `tools/workspace/worker.py` | `WorkerThread.__init__` — add `"paused"` to status docstring/type hint | 5 min |
| `tools/workspace/worker.py` | `run()` — preserve `"paused"` status in except/else blocks | 15 min |
| `web_ui/backend/workspace_routes.py` | Add `POST /pause` endpoint | 30 min |
| Frontend (TBD) | Pause button handler — iterate workers, call pause endpoint | 30 min |
| Frontend (TBD) | Add yellow/paused icon rendering | 15 min |
| **Total** | | **~2 hours** |

---

## Implementation Order

1. **`_poll_command()` in worker.py** — Add `"pause"` action (~15 min)
2. **WorkerThread status updates** — Add "paused" to type hints and lifecycle handling (~15 min)
3. **API endpoint in workspace_routes.py** — Add pause endpoint (~30 min)
4. **Test** — Manual: spawn a worker, call pause endpoint, verify status changes to "paused" (~15 min)
5. **Frontend integration** — Wire pause button to worker pause calls (~30 min)
6. **Frontend UI** — Add paused icon/color (~15 min)

Total: **~2 hours**

---

## Open Questions

1. **Confirmation dialog?** Should the main agent's pause button show a dialog like "Also pause N running workers?" — or just pause everything silently? Recommendation: pause silently for v1; add confirmation in v2 if users request it.

2. **Grace period?** Should long-running worker tasks (e.g., a file search that's 80% done) get a grace period to finish? Recommendation: no — the "finish current turn" semantics already give the worker a natural stopping point (the current LLM call or tool execution).

3. **Agent restart?** If the main agent is paused and then the user closes the browser, should workers be stopped (not left paused)? Recommendation: yes — treat browser disconnect as session end, which already triggers `shutdown_workers()`.

4. **Race condition?** What if the pause command file is written just as the worker finishes its last turn naturally? The worker would see `"paused"` status, then the `run()` loop's else block would try to set `"completed"` — but the `if self.status != "paused"` guard prevents that. Safe.
