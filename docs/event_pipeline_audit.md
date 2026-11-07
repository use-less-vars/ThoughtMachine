# Event Pipeline Architecture: Main Agent vs Worker Agent

**Audit Date:** 2026-07-12  
**Scope:** `web_ui/backend/bridge.py`, `agent/events.py`, `agent/core/agent.py`, `agent/core/worker_context.py`, frontend event routing  
**Status:** First audit complete

---

## 1. Executive Summary

The system has **two completely separate event paths** that converge at the Bridge and are forwarded to the frontend over a single WebSocket:

1. **Main Agent path** — uses raw Python dicts yielded synchronously from `process_query()` generator
2. **Worker Agent path** — uses typed EventBus events published asynchronously via pub/sub

These two paths differ in event format, transport mechanism, producer code, bridge handling, and frontend consumer routing. This document describes both paths end-to-end and identifies gaps.

---

## 2. Path 1: Main Agent → Frontend

### 2.1 Producer

**File:** `agent/core/agent.py`  
**Method:** `process_query()` (generator)

The main agent yields raw Python dictionary objects. These are NOT typed events and do NOT use the EventBus system.

### 2.2 Raw Dict Event Types

| Event Type | Description | Contains |
|---|---|---|
| `execution_state_change` | Agent state transition (thinking, waiting, done, error) | `state` |
| `token_update` | Token count changes | `total_tokens`, `input_tokens`, `output_tokens`, `context_length` |
| `turn` | Turn count increment | `turn` |
| `tool_call` | Tool call initiated | `name`, `arguments`, `tool_call_id` |
| `tool_result` | Tool result received | `name`, `content`, `tool_call_id` |
| `user_query` | User query received | `content` |
| `final` | Final response produced | `content` |
| `paused` | Agent paused (security prompt) | `reason` |
| `stopped` | Agent stopped | — |
| `max_turns` | Max turns reached | `turns` |
| `error` | Error occurred | `error`, `message` |
| `rate_limit_warning` | Rate limit warning | — |
| `stop_reason` | LLM stop reason | `stop_reason` |
| `agent_responded` | Agent produced a response | `content` |
| `session_stop` | Session stopped | — |
| `security_prompt` | Security prompt triggered | `prompt`, `tool` |

### 2.3 Transport

**Mechanism:** Generator iteration (synchronous, blocking)  
**Flow:** The Bridge's background thread (`_run_loop`) or controller callback (`_on_controller_event`) iterates the generator and processes each yielded dict.

### 2.4 Bridge Handling

**File:** `web_ui/backend/bridge.py`

Two entry points:

- **Mode A (Standalone Agent):** `_run_loop()` thread (line ~1780) pulls queries from `_query_queue`, calls `self._agent.process_query(query)`, iterates results
- **Mode B (Controller):** `_on_controller_event()` callback (line ~1598) receives dicts from the controller's agent thread

Both call `self._map_and_emit(raw_event)` for each dict.

### 2.5 Mapping: Raw Dict → Frontend Protocol

The `_map_and_emit()` method (line ~1643) transforms raw dict types into frontend event types:

| Raw Dict Type | Frontend Event Type | Data Source |
|---|---|---|
| `execution_state_change` | `state_changed` | dict content |
| `token_update` | `tokens_updated` + `context_updated` | dict content |
| `turn` | `conversation_changed` | `Session.user_history` |
| `tool_call` | `conversation_changed` | `Session.user_history` |
| `tool_result` | `conversation_changed` | `Session.user_history` |
| `user_query` | `conversation_changed` | `Session.user_history` |
| `final` | `conversation_changed` | `Session.user_history` |
| `token_warning` | `conversation_changed` | `Session.user_history` |
| `turn_warning` | `conversation_changed` | `Session.user_history` |
| `agent_responded` | `conversation_changed` + `state_changed` | Session + dict |
| `error` | `status_message` + `conversation_changed` | dict + Session |
| `session_stop` | `state_changed` | dict content |
| `security_prompt` | `security_prompt` | dict content |

**Key design choice:** Conversation events always re-read `Session.user_history` — the event dict content is NOT used for message data.

### 2.6 Frontend Consumer

**File:** `web_ui/frontend/src/components/SessionTab.jsx`

| Frontend Event | Handler |
|---|---|
| `state_changed` | Updates agent state machine |
| `tokens_updated` | Updates token counters |
| `context_updated` | Updates context length display |
| `conversation_changed` | `setMessages()` → re-renders conversation |
| `status_message` | Shows flash message |
| `security_prompt` | Shows security dialog |

---

## 3. Path 2: Worker Agent → Frontend

### 3.1 Producer

**File:** `tools/workspace/worker.py` (not analyzed in this audit — assumed)

The worker executor code publishes typed EventBus events. The worker context proxy (`agent/core/worker_context.py`) does **NOT** publish events.

### 3.2 Typed EventBus Event Types

Events defined in `agent/events.py` with `EventType` enum:

| EventType | Event Class | Purpose |
|---|---|---|
| `WORKER_SPAWNED` | `WorkerSpawnedEvent` | Worker instance created |
| `WORKER_STATUS` | `WorkerStatusEvent` | Status update (running, paused, etc.) |
| `WORKER_COMPLETED` | `WorkerCompletedEvent` | Worker finished successfully |
| `WORKER_ERROR` | `WorkerErrorEvent` | Worker encountered an error |
| `TOKEN_WARNING` | `TokenWarningEvent` | Token threshold warning |
| `WORKER_MESSAGE` | `WorkerMessageEvent` | Arbitrary worker message |
| `TOOL_CALL` | `ToolCallEvent` | Worker called a tool |
| `TOOL_RESULT` | `ToolResultEvent` | Worker received tool result |
| `ASSISTANT_MESSAGE` | `AssistantMessageEvent` | Worker produced assistant message |
| `SECURITY_PROMPT` | `SecurityPromptEvent` | Security prompt triggered |

Each event has:
- `.data` (dict) — event-specific payload
- `.type` (EventType) — event type
- `.metadata` object with:
  - `.timestamp` — ISO timestamp
  - `.source` — source identifier
  - `.session_id` — session identifier

### 3.3 Transport

**Mechanism:** EventBus pub/sub (asynchronous, non-blocking)

Two bus layers:
1. **Per-worker EventBus** — keyed by `(session_id, worker_name)`, carries all worker events
2. **Global EventBus** — singleton, carries lifecycle events (spawn, complete, error, token warning)

### 3.4 Bridge Subscriptions

**File:** `web_ui/backend/bridge.py`

#### Global EventBus Subscriptions (~line 662)

| EventType | Handler | Forwarded As |
|---|---|---|
| `WORKER_SPAWNED` | `_on_worker_spawned()` | Sets up per-worker sub, forwards details |
| `WORKER_STATUS` | Generic handler | `worker:worker_status` |
| `WORKER_COMPLETED` | `_on_worker_completed()` | Forwards + cleans up per-worker sub |
| `WORKER_ERROR` | `_on_worker_error()` | Forwards + cleans up per-worker sub |
| `TOKEN_WARNING` | `_on_worker_token_warning()` | `worker:system_notification` (if source starts with 'worker:') |
| `WORKER_MESSAGE` | Generic handler | `worker:worker_message` |
| `SECURITY_PROMPT` | `_security_prompt_handler()` | `security_prompt` |

#### Per-worker EventBus Subscriptions (~line 730)

| EventType | Forwarded As |
|---|---|
| `tool_call` | `worker:tool_call` |
| `tool_result` | `worker:tool_result` |
| `token_warning` | `worker:token_warning` |
| `worker_message` | `worker:worker_message` |
| `assistant_message` | `worker:assistant_message` |

**Filtering:** Bridge only forwards events where `data.get('session_id')` matches `self._session_id`.

### 3.5 Frontend Consumer

1. **`SessionTab.jsx`** — detects `'worker:*'` prefix → dispatches to WorkerOutputPanel
2. **`WorkerOutputPanel.jsx`** (~23KB) — dual-channel design (WebSocket + polling)
   - Maintains worker state: running, completed, error
   - Uses `adaptWorkerEvent.js` to transform raw data into display format
3. **`adaptWorkerEvent.js`** (~11KB) — transforms raw worker event data into frontend message format
   - Handles `worker_name`, `role` (assistant/system), `content` extraction
4. **`MessageBubble.jsx`** (~7KB) — renders individual messages
   - Supports roles: user, assistant, system, worker
   - Handles markdown rendering

---

## 4. Comparison Table

| Aspect | Main Agent Path | Worker Agent Path |
|---|---|---|
| **Event format** | Raw Python dicts | Typed EventBus (EventType enum + event classes) |
| **Transport** | Generator iteration (sync, blocking) | EventBus pub/sub (async, non-blocking) |
| **Producer code** | `agent/core/agent.py` (`process_query`) | Worker executor code (`tools/workspace/worker.py`) |
| **Bus used** | None | `global_event_bus` + per-worker bus |
| **Context proxy publishes?** | N/A | No (`worker_context.py` is silent) |
| **Bridge entry point** | `_map_and_emit()` direct call | EventBus subscription callbacks |
| **Conversation data source** | `Session.user_history` (re-read) | Event `.data` dict (inline) |
| **Frontend routing** | By type name directly | `'worker:{type}'` prefix |
| **Message rendering** | `SessionTab.jsx` directly | `WorkerOutputPanel.jsx` → `MessageBubble.jsx` |
| **Schema validation** | None (raw dict, no TypedDict) | Typed event classes with `.data`/`.metadata` |

---

## 5. Data Flow Diagrams

### 5.1 Main Agent Path

```
┌──────────────────────────────────────────────────────────────────────┐
│                        MAIN AGENT PATH                                │
│                                                                       │
│  process_query()          Bridge._run_loop()          Frontend        │
│  ┌─────────────────┐     ┌──────────────────┐       ┌──────────────┐ │
│  │ yield dict       │────>│ _map_and_emit()   │──────>│ SessionTab   │ │
│  │ {type, data, ...}│     │  dict → protocol  │       │ .jsx         │ │
│  │                  │     │  _emit() → ws.send│       │ setMessages()│ │
│  └────────┬────────┘     └──────────────────┘       └──────────────┘ │
│           │                                                           │
│           │ Session.user_history (re-read on every conversation event)│
│           ▼                                                           │
│  ┌─────────────────┐                                                  │
│  │ Session          │                                                  │
│  │ (mutated in-     │                                                  │
│  │  place by agent) │                                                  │
│  └─────────────────┘                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.2 Worker Agent Path

```
┌──────────────────────────────────────────────────────────────────────┐
│                        WORKER AGENT PATH                              │
│                                                                       │
│  Worker Executor      EventBus              Bridge Subscriptions     │
│  ┌────────────────┐   ┌──────────┐         ┌───────────────────┐    │
│  │ publish(        │──>│ per-     │────────>│ _on_worker_spawned│    │
│  │  WORKER_SPAWNED │   │ worker   │         │ _on_worker_compl. │    │
│  │  )              │   │ bus      │         │ _on_worker_error  │    │
│  └────────┬───────┘   └──────────┘         │ _on_token_warning │    │
│           │                                  └────────┬──────────┘    │
│           │                                   ┌───────┴───────────┐  │
│           └───────────────────────────────────> _emit() → ws.send  │  │
│                                     worker:    │ 'worker:{type}'   │  │
│                                                └───────┬───────────┘  │
│                                                        │              │
│                                                        ▼              │
│                                                ┌──────────────────┐  │
│                                                │ SessionTab       │  │
│                                                │ detects worker:* │  │
│                                                │ → WorkerOutput   │  │
│                                                │   Panel          │  │
│                                                │ → MessageBubble  │  │
│                                                └──────────────────┘  │
│                                                                      │
│  Global EventBus (lifecycle only)                                    │
│  ┌──────────────────────────────┐                                    │
│  │ WORKER_SPAWNED, COMPLETED,   │                                    │
│  │ ERROR, TOKEN_WARNING         │                                    │
│  └──────────────────────────────┘                                    │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.3 Combined Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                      COMBINED EVENT ARCHITECTURE                      │
│                                                                       │
│   ┌────────────────────────────────────────────────────────────┐      │
│   │                     BRIDGE                                  │      │
│   │                                                             │      │
│   │   ┌─────────────────────┐   ┌──────────────────────────┐   │      │
│   │   │ Mode A: Standalone  │   │ Mode B: Controller       │   │      │
│   │   │ _run_loop() thread  │   │ _on_controller_event()   │   │      │
│   │   │ iterates generator  │   │ receives dicts from      │   │      │
│   │   │ → _map_and_emit()   │   │ controller → _map_and_   │   │      │
│   │   └─────────┬───────────┘   │ emit()                   │   │      │
│   │             │               └───────────┬──────────────┘   │      │
│   │             ▼                           ▼                  │      │
│   │   ┌────────────────────────────────────────────────┐       │      │
│   │   │              _map_and_emit()                    │       │      │
│   │   │  raw dict → frontend protocol                  │       │      │
│   │   └──────────────────────┬─────────────────────────┘       │      │
│   │                          │                                  │      │
│   │   ┌──────────────────────▼─────────────────────────┐       │      │
│   │   │                 _emit()                         │       │      │
│   │   │  broadcasts to all registered callbacks        │       │      │
│   │   │  (WebSocket send functions)                    │       │      │
│   │   └──────────────────────┬─────────────────────────┘       │      │
│   │                          │                                  │      │
│   │   ┌──────────────────────▼─────────────────────────┐       │      │
│   │   │  EventBus Subscription Handlers                 │       │      │
│   │   │  (global + per-worker buses)                   │       │      │
│   │   │  → _emit()                                      │       │      │
│   │   └─────────────────────────────────────────────────┘       │      │
│   └────────────────────────────────────────────────────────────┘      │
│                              │                                        │
│                              ▼                                        │
│   ┌────────────────────────────────────────────────────────────┐      │
│   │                   WebSocket                                 │      │
│   │  Main agent: state_changed, tokens_updated,                │      │
│   │             conversation_changed, status_message,          │      │
│   │             security_prompt                                │      │
│   │  Workers:    worker:worker_status, worker:tool_call,       │      │
│   │             worker:worker_message, worker:system_notification│    │
│   └──────────────────────────┬─────────────────────────────────┘      │
│                              │                                        │
│                              ▼                                        │
│   ┌────────────────────────────────────────────────────────────┐      │
│   │                   FRONTEND                                  │      │
│   │                                                             │      │
│   │   SessionTab.jsx routes by type:                           │      │
│   │   - state_changed / tokens_updated / context_updated       │      │
│   │   - conversation_changed → setMessages()                   │      │
│   │   - status_message → flash                                 │      │
│   │   - worker:* → WorkerOutputPanel                           │      │
│   │   - security_prompt → security dialog                     │      │
│   └────────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Gaps, Inconsistencies & Improvement Areas

### 6.1 Dual Event Protocol

| Severity | Medium |
|---|---|
| **Problem** | Main agent uses raw dicts; workers use typed EventBus events. Two different protocols must be maintained and understood. |
| **Risk** | New developers must learn both systems. The mapping code in `_map_and_emit()` is the only bridge between raw dict format and frontend protocol. |
| **Suggestion** | Consider migrating main agent events to use EventBus for consistency. This would require adding EventType values for main agent events and changing `process_query()` to publish typed events instead of yielding dicts. |

### 6.2 Conversation Source Inconsistency

| Severity | Medium |
|---|---|
| **Problem** | Main agent conversation events read from `Session.user_history` (re-read on every event). Worker conversation events carry message data inline in the event dict. |
| **Risk** | If `Session.user_history` is out of sync or delayed, the frontend may show stale data for main agent events. Worker events bypass Session entirely for message content. |
| **Suggestion** | Align approaches — either have all events carry inline message content, or have all events reference Session. The former is more robust for real-time updates. |

### 6.3 No Typed Events for Main Agent

| Severity | Low |
|---|---|
| **Problem** | Main agent events are untyped dicts with string 'type' keys. No schema validation, no metadata, no source tracking. |
| **Risk** | A typo in a dict key ('execution_state' vs 'execution_state_change') would silently fail to propagate to frontend. |
| **Suggestion** | Define a `MainAgentEvent` TypedDict or use the EventBus Event base class for main agent events. |

### 6.4 Potential Duplicate Events from Dual Bus Subscriptions

| Severity | Low |
|---|---|
| **Problem** | The bridge subscribes to both global_event_bus (for lifecycle events like WORKER_COMPLETED) and per-worker buses (for detail events). If a lifecycle event is published to both buses, the bridge could receive and forward it twice. |
| **Risk** | Duplicate events on the frontend (e.g., two "worker completed" messages). |
| **Suggestion** | Audit whether lifecycle events are published to both buses. If so, either deduplicate at the bridge or ensure each bus has exclusive event types. |

### 6.5 Worker Context Proxy publishes No Events

| Severity | Low |
|---|---|
| **Problem** | `worker_context.py` is a lightweight Session proxy that does NOT publish events. The worker tool executor code (`tools/workspace/worker.py`) is responsible for publishing. This separation is undocumented. |
| **Risk** | If someone modifies `worker_context.py` to add event publishing, they might produce duplicate events (or miss events they expect). |
| **Suggestion** | Document the responsibility split clearly, or move event publishing into the context proxy for consistency. |

### 6.6 Session as Single Source of Truth vs Event Stream

| Severity | Medium |
|---|---|
| **Problem** | The main agent path fundamentally relies on `Session.user_history` as the authoritative conversation store. The event stream is a side effect of Session mutations. The frontend doesn't "follow events" for conversation — it re-reads the Session. |
| **Risk** | If the event stream is processed faster than Session writes, the frontend could see stale data. Currently unlikely because the generator is synchronous, but if async is introduced, timing bugs could appear. |
| **Suggestion** | Add explicit synchronization or switch to event-carried message data for conversation updates. |

### 6.7 Security Prompt Event Path Ambiguity

| Severity | Low |
|---|---|
| **Problem** | The main agent's `process_query()` can yield `'security_prompt'` as a raw dict (handled by `_map_and_emit`). The docstring also mentions `SecurityPromptEvent` from `global_event_bus`. It's unclear if both paths are active or if one is dead. |
| **Suggestion** | Audit whether the tool executor publishes `SecurityPromptEvent` to EventBus. If not, remove the EventBus subscription or add the publishing logic. |

### 6.8 Frontend Dual-Channel for Workers

| Severity | Low |
|---|---|
| **Problem** | `WorkerOutputPanel.jsx` uses both WebSocket (event-driven) and polling (HTTP GET) for worker output. The polling path is a fallback, but its purpose and trigger conditions are not well-documented. |
| **Risk** | If WebSocket events are lost, polling may show stale data. If both paths deliver the same data, the frontend may show duplicates. |
| **Suggestion** | Document when polling is activated and ensure deduplication logic exists. |

---

## 7. Recommendations (Priority Order)

1. **Audit dual bus publication** — Verify whether lifecycle events are published to both global and per-worker buses. If so, deduplicate or partition event types.

2. **Document worker event publishing** — Clarify that `worker_context.py` does NOT publish events and that the worker executor code is responsible. Add docstrings.

3. **Explore EventBus unification** — Investigate migrating main agent events to EventBus for protocol consistency. This is a large refactor but would eliminate the dual-protocol burden.

4. **Align conversation source** — Decide whether all events should carry inline message data or all should reference Session. Document the chosen approach and implement consistently.

5. **Add TypedDict for main agent events** — Even without full EventBus migration, define a TypedDict schema for main agent yield events to catch typos at lint time.

6. **Document polling fallback** — Add comments explaining when/why WorkerOutputPanel uses polling vs WebSocket.

---

## 8. File Reference

| File | Lines | Role |
|---|---|---|
| `agent/events.py` | ~200 | Typed EventBus system, EventType enum, event classes |
| `agent/core/agent.py` | ~800+ | Main agent, `process_query()` generator yields raw dicts |
| `agent/core/worker_context.py` | ~60 | Lightweight Session proxy, does NOT publish events |
| `web_ui/backend/bridge.py` | 1856 | Bridge: dual-mode event router, mapping, subscriptions |
| `web_ui/frontend/src/components/SessionTab.jsx` | ~27KB | Primary WebSocket event receiver |
| `web_ui/frontend/src/components/WorkerOutputPanel.jsx` | ~23KB | Dual-channel worker event consumer |
| `web_ui/frontend/src/utils/adaptWorkerEvent.js` | ~11KB | Worker event data transformation |
| `web_ui/frontend/src/components/MessageBubble.jsx` | ~7KB | Message rendering |

---

*End of audit report*
