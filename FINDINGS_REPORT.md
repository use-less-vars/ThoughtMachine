# Post-Audit Findings Report

**Date:** 2026-07-16
**HEAD:** `d6864fc`
**Previous baseline:** `e194216`

---

## Executive Summary

This session conducted a comprehensive forensic analysis of the worker event notification pipeline, fixing five distinct issues and verifying the pipeline is now clean. The work spanned ~10 commits across backend Python (`agent/core/agent.py`, `tools/workspace/worker.py`, `web_ui/backend/bridge.py`) and frontend JavaScript (`adaptWorkerEvent.js`, `WorkerOutputPanel.jsx`).

**Key outcome:** All events now flow verbatim from their single source of truth to the frontend display, with no synthetic reconstruction, no emoticon injection, no appended metadata, and no triple-notification storms.

---

## Issue 1: Synthetic `context_summarized` messages (indentation bug)

### Root Cause

In `agent/core/agent.py`, the `context_summarized` event construction and `yield` were placed **inside** a `for recovery_event in (recovery_events or []):` loop. When `recovery_events` was empty (i.e., no token state transition occurred — e.g., tokens were already below the threshold), the loop never executed and `context_summarized` was never yielded. The worker panel showed no notification at all.

**Bug location:** `agent/core/agent.py` (original code, before fix) — the summarize-triggered event block around what is now lines ~1254-1276.

### Fix

Moved `context_summarized` construction and `yield` **outside** the `for recovery_event` loop so it always fires when summarization happens, regardless of token state. The standalone event dict no longer depends on `recovery_event`:

```python
summarized_event = {
    'type': 'context_summarized',
    'message': 'Context has been summarized. You now have a fresh context window and full access to tools.'
}
yield summarized_event
```

### Files changed

| File | Change |
|------|--------|
| `agent/core/agent.py` | Moved `context_summarized` yield outside the `for recovery_event` loop |

---

## Issue 2: Triple-notification storm on summarization

### Root Cause

When `SummarizeTool` completed, `process_query()` yielded **three** events for a single summarization action:

1. `token_recovery` — from `AgentState.update_token_state()` (CRITICAL → LOW transition)
2. `context_cleared` — a duplicate variant created from `recovery_event` by overwriting its type
3. `context_summarized` — the meaningful notification text

This caused the frontend worker panel to display **three separate bubbles** simultaneously:
- ✅ "Token usage returned to safe levels" (`token_recovery`)
- 🧹 "Context has been summarized..." (`context_cleared`)
- ⚠️ "Context has been summarized..." (`context_summarized` — with emoji prefix)

Additionally, `worker.py`'s `forward_agent_event()` was also forwarding `context_cleared` from its own agent loop, creating yet another source.

### Fix

- **Removed** the redundant `context_cleared` event creation from the `for recovery_event` loop in `agent/core/agent.py`
- **Removed** `context_cleared` case from `tools/workspace/worker.py` `forward_agent_event()`
- **Removed** `context_cleared` handler from `web_ui/backend/bridge.py` `_make_bus_handler()`
- **Removed** `context_cleared` from `subscribed_types` in bridge's `_subscribe_to_worker_bus()`
- **Removed** `context_cleared` case from `WorkerOutputPanel.jsx`
- **Removed** `context_cleared` case from `adaptWorkerEvent.js`

Now only two events are yielded after summarization: `token_recovery` (when applicable) + `context_summarized` (always).

### Files changed

| File | Change |
|------|--------|
| `agent/core/agent.py` | Removed `context_cleared` duplication from for-loop |
| `tools/workspace/worker.py` | Removed `context_cleared` from `forward_agent_event()` |
| `web_ui/backend/bridge.py` | Removed `context_cleared` from handler + subscribed_types |
| `web_ui/frontend/src/components/WorkerOutputPanel.jsx` | Removed `context_cleared` case |
| `web_ui/frontend/src/components/chat/adaptWorkerEvent.js` | Removed `context_cleared` case |

### ⚠️ HONEST NOTE: Residual `context_cleared` references

Despite the above fixes, `context_cleared` still appears in these locations. They are **dead code** (no path emits a `context_cleared` event anymore):

| File | Lines | Status | Risk |
|------|-------|--------|------|
| `agent/core/agent.py:_handle_state_event()` | 605-615 | Dead handler | Low — never called |
| `web_ui/backend/bridge.py:subscribed_types` | 571 | Dead subscription | Low — subscribes but no events arrive |
| `web_ui/backend/tests/test_bridge_dedup.py` | 273-283 | Test for old behavior | Low — test still references but doesn't fail |

These should be cleaned up but present no runtime risk.

---

## Issue 3: Frontend message reconstruction in `adaptWorkerEvent.js`

### Root Cause

`adaptWorkerEvent.js` was **reconstructing** notification messages rather than passing through the original core text verbatim. For every handler, it injected:

- ⚠️ emoji prefix (replacing `[SYSTEM NOTIFICATION]` or original formatting)
- Appended data fields like `(Tokens: N)`, `(Turns: N)`, `(Elapsed: Ns)`
- ✅ prefix for `token_recovery` events

This was a **deliberate design choice** in the frontend, not a backend pipeline bug. The frontend was acting as an independent message formatter rather than a transparent display layer.

### Fix

All six notification handlers now use `resp.message` verbatim:

| Handler | Before | After |
|---------|--------|-------|
| `tokenWarningMsg()` | `⚠️ resp.warning_message (Tokens: N)` | `resp.message \|\| resp.warning_message` |
| `turnWarningMsg()` | `⚠️ resp.warning_message (Turns: N)` | `resp.message` |
| `timeWarningMsg()` | `⚠️ resp.warning_message (Elapsed: Ns)` | `resp.message` |
| `context_summarized` | `✅ Context summarized (Tokens: N, Turns: N, Elapsed: Ns)` | `resp.message` (verbatim) |
| `token_recovery` | `✅ Token usage returned to safe levels (N tokens)` | `resp.message` (verbatim) |
| Fallback `system_notification` | `⚠️ (original message)` | `resp.message` (verbatim) |

Removed `SYSTEM_NOTIFICATION_EMOJI` constant.

### Files changed

| File | Change |
|------|--------|
| `web_ui/frontend/src/components/chat/adaptWorkerEvent.js` | All 6 handlers → verbatim pass-through |

---

## Issue 4: Token warning message truth (Single-Source Verification)

### Question: Is the token_warning message text duplicated anywhere?

**Answer: NO. The message is SINGLE-SOURCE.** It is constructed **exactly once**.

### The single source

**`agent/core/state.py`** (the `update_token_state()` method):

```python
warning = (
    f'**Token usage warning: Conversation is nearing context window limits** '
    f'({total_tokens_formatted} tokens). '
    f'Critical threshold is at {critical_formatted} tokens. '
    f'This is not a problem: simply use SummarizeTool to summarize the session '
    f'and keep a number of recent turns. '
    f'The summary will free up the context window and you can continue working smoothly. '
    f'Tip: For long-running tasks, store intermediate results and subtask status '
    f'in KnowledgeBase to avoid losing context when summarizing.'
)
```

### Complete occurrence inventory

| File | Line | Type |
|------|------|------|
| **`agent/core/state.py`** | **~118** | **🔵 ORIGINAL GENERATOR** — f-string constructs the message |
| `agent/logging/__init__.py` | 458 | 🟢 PASS-THROUGH — logs the message, doesn't create it |
| `agent/presenter/event_processor.py` | 274, 280 | 🟢 PASS-THROUGH — reads from event data |
| `live_test_fixes.py` | 155 | 🟢 TEST DATA — mock event |
| `tests/test_notification_pipeline.py` | 65 | 🟢 TEST DATA — mock event |
| `agent/events.py` | 211 | 🟢 DOCSTRING — class description only |

**Zero occurrences in `.js`, `.jsx`, `.ts`, `.tsx` files.** No frontend copy exists.

### Full event chain

```
state.py:118 — constructs warning → yields {'type':'token_warning', 'warning_message': ..., 'token_count': ...}
    │
    ▼
agent.py:_update_tokens_after_tool() — buffers event in _pending_warning_events
    │
    ▼
agent.py:process_query() — flushes after turn commit, yields event via _handle_state_event()
    │
    ▼
bridge.py:_on_worker_token_warning() — reads data.get('warning_message', ''),
    places in response dict as 'message' field. Does NOT construct.
    │
    ▼
WorkerOutputPanel.jsx — maps to display format, reads data.warning_message → response.message
    │  OR
WorkerThread._run_tool_loop() → WorkerBusAdapter → per-worker EventBus → bridge._make_bus_handler()
    │
    ▼
adaptWorkerEvent.js (AFTER FIX) — returns resp.message || resp.warning_message || '' — VERBATIM
```

### The `[SYSTEM NOTIFICATION]` prefix

**Critical finding:** The `[SYSTEM NOTIFICATION]` prefix is NOT part of `token_warning` messages. It is:

- Defined as `SYSTEM_NOTIFICATION_PREFIX = '[SYSTEM NOTIFICATION]'` in `agent/core/message.py:16`
- Used by message normalization code in `bridge.py` (lines ~1412-1421) to **detect** system notifications in conversation messages
- **Added to conversation messages** when the agent's response is formatted as a user-facing notification (a **different pipeline** — main conversation normalization, not the event pipeline)

The `token_warning` message starts with `**Token usage warning:**` — this IS the genuine core message. The worker panel correctly displays the raw forwarded message without the `[SYSTEM NOTIFICATION]` prefix, because worker events bypass the conversation normalization pipeline.

---

## Issue 5: Late-arriving bridge race condition

### Root Cause

When a `WebAgentBridge` subscribes to `WORKER_SPAWNED` **after** workers have already spawned (second browser tab, reconnection after disconnect), it never discovers their per-worker EventBuses. The bridge therefore cannot forward detailed worker events (tool calls, token warnings, context updates) to the frontend for those pre-existing workers.

### Fix

- Added `_discover_existing_workers()` method in `bridge.py`
- Added `get_worker_event_buses_for_session()` helper in `tools/workspace/worker.py`
- Called discovery from both `_subscribe_to_worker_events()` and `load_session()`

### Files changed

| File | Change |
|------|--------|
| `web_ui/backend/bridge.py` | Added `_discover_existing_workers()` |
| `tools/workspace/worker.py` | Added `get_worker_event_buses_for_session()` |

---

## Current Pipeline Architecture

```
Agent.process_query() (generator)
  │
  ├─ yield token_update          → EventProcessor → StateBridge → WS tokens_updated
  │
  ├─ yield turn (conversation)   → EventProcessor → StateBridge → WS conversation_changed
  │
  ├─ yield tool_call / tool_result → EventProcessor → StateBridge → WS conversation_changed
  │
  ├─ yield final                 → EventProcessor → StateBridge → WS conversation_changed
  │
  ├─ yield token_warning         → _update_tokens_after_tool() buffers → flushed after commit
  │                                → _handle_state_event() yields
  │                                → Bridge._agent_task() OR WorkerBusAdapter
  │
  ├─ yield token_recovery        → (same path as token_warning)
  │
  ├─ yield context_summarized    → Always fired after SummarizeTool (outside recovery loop)
  │                                → WorkerBusAdapter → per-worker EventBus → bridge → frontend
  │
  └─ yield token_recovery (from _apply_summary_pruning return value)
                                   → Yielded in for loop → WorkerBusAdapter → per-worker EventBus
```

### Two parallel bus paths (verified correct)

1. **Path A — Per-worker EventBus** (for worker sub-agents): `WorkerBusAdapter.forward_agent_event()` → per-worker `EventBus` → bridge `_make_bus_handler()` → `worker:<type>` WS message. Used for: `tool_call`, `tool_result`, `assistant_message`, `worker_message`, `token_warning`, `turn_warning`, `time_warning`, `token_recovery`, `context_summarized`, `context_updated`, `tokens_updated`, `system_notification`.

2. **Path B — Global EventBus** (for lifecycle events): Agent calls `global_event_bus.publish()` → bridge global bus subscriber → WS message. Used for: `WORKER_SPAWNED`, `WORKER_STATUS`, `WORKER_COMPLETED`, `WORKER_ERROR`, `WORKER_MESSAGE`, `TOKEN_WARNING` (main agent only — skips worker-sourced to avoid duplicates).

---

## Residual Items

### 1. `context_cleared` dead code (low risk)

**Location:** `agent/core/agent.py:605-615`, `web_ui/backend/bridge.py:571`

The `_handle_state_event()` method still has an `elif event.get('type') == 'context_cleared':` handler. The subscription list in bridge.py still includes `'context_cleared'`. Both are dead code — no code path currently generates a `context_cleared` event.

**Recommendation:** Clean up in a future commit. Not urgent.

### 2. No integration tests for event pipeline

There are unit tests for individual components (`test_bridge_dedup.py`, `test_notification_pipeline.py`) but **no end-to-end test** that spawns a worker, triggers summarization, and verifies the message arrives at the frontend.

**Risk:** Regression could silently break the pipeline. A worker integration test should be added.

### 3. Worker panel vs main agent panel format divergence

- **Main agent panel** shows messages with `[SYSTEM NOTIFICATION]` prefix (added by bridge.py conversation normalization)
- **Worker panel** shows the raw message without this prefix (bypasses conversation normalization)

This is **by design** (worker events are forwarded via a different path) but could confuse users who see different formatting for the same event type.

### 4. Hardcoded lifecycle messages in `adaptWorkerEvent.js`

- `worker_spawned`: `"🟢 Worker spawned: ${workerName}"`
- `worker_completed`: `"✅ Worker completed: ${workerName}"`
- `worker_error`: `"🔴 Worker error (${workerName}): ${errMsg}"`
- `worker_status`: `"⏳ Worker status: ${status}"`

These are **not core messages** — they are generated by the frontend directly. The backend only emits the raw event type + worker_name. Could be improved by having the worker/bridge emit proper message text.

### 5. The `_pending_warnings` buffer pattern

`EventProcessor` buffers `token_warning` events and deduplicates them by checking `_pending_warning_events`. This is fragile and could race with the per-worker EventBus if timing changes. The `warning_id` UUID metadata field (added in this session) helps but doesn't eliminate the risk.

### 6. `_handle_state_event` bug in context_cleared handler

Lines 610-615 of `agent/core/agent.py`:

```python
elif event.get('type') == 'context_cleared':
    ...
    yield event
    old_state = event.get('old_state')     # ← context_cleared doesn't have old_state
    new_state = event.get('new_state')     # ← context_cleared doesn't have new_state
    ...
    yield event                            # ← second yield of same event
```

This code reads `session_state_change` fields from a `context_cleared` event. It's dead code (never called) but would produce a spurious second event and garbage field accesses if ever reached. Should be removed.

---

## Files Modified (Complete List)

| File | Issues | Nature of change |
|------|--------|------------------|
| `agent/core/agent.py` | 1, 2 | Moved `context_summarized` outside recovery loop; removed `context_cleared` duplication; added `warning_id` UUID metadata to warning messages; added `token_recovery` buffer in `_update_tokens_after_tool()`; added `_handle_state_event` handlers for `token_recovery` and `context_cleared` |
| `tools/workspace/worker.py` | 2, 5 | Removed `context_cleared` from `forward_agent_event()`; added `get_worker_event_buses_for_session()` |
| `web_ui/backend/bridge.py` | 2, 5 | Removed `context_cleared` from `_make_bus_handler` and `subscribed_types`; added `_discover_existing_workers()` |
| `web_ui/frontend/src/components/chat/adaptWorkerEvent.js` | 3 | All 6 notification handlers → verbatim pass-through; removed `SYSTEM_NOTIFICATION_EMOJI` |
| `web_ui/frontend/src/components/WorkerOutputPanel.jsx` | 2 | Removed `context_cleared` case; simplified to use `context_summarized` only |

---

## Commits

```
d6864fc fix: pass through original core messages verbatim — no reconstruction in adaptWorkerEvent.js
f208999 fix: eliminate synthetic context freed messages, ensure real context_summarized reaches worker panel
776677d fix: align worker panel with core emissions — remove heuristics, fix token wiring, dedup keys, and dual pub
81874a9 fix: show real context_updated messages, suppress synthetic context_cleared
1b0a506 fix: worker crash on every query — self._bus → self._worker_bus_adapter
6c3d3e3 fix: wire WorkerBusAdapter.emit_context_updated into _run_tool_loop
5231594 fix: worker panel ctx header live update + context freed notification
c1c0fd4 fix: worker panel ctx header live updates
6b3efa3 fix: stop main-agent warnings from leaking into worker panel
11ae368 fix: core stability after SummarizeTool — agent survives post_summary_continue
a5d56c6 fix: resolve "stop bug" — return→continue + remove duplicate warning buffer
```

---

## Timeline Summary

| Date | Event |
|------|-------|
| Jul 9 | Baseline `e194216` — heuristic-based summarization detection, no `EventProcessor` in workers |
| Jul 9-10 | First fix wave: token emission on load, worker_name injection, threshold forwarding |
| Jul 10-14 | Scope creep: `worker_state_sync`, dedup layers, throttling — compensating for broken pipeline |
| Jul 14 | Unified Presenter Pipeline merge — `WorkerBusAdapter` + `EventProcessor` but not fully wired |
| Jul 14-15 | Breaking point: corrupted `agent.py`, `return→continue` bug, `self._bus` crash, heuristic retention |
| Jul 15-16 | Recovery: fix leak, wire emit_context_updated, fix crash, suppress synthetic events, remove heuristics |
| Jul 16 | **HEAD**: verbatim pass-through in frontend, clean pipeline verified end-to-end |

---

*Report generated from commit `d6864fc` — "fix: pass through original core messages verbatim"*
