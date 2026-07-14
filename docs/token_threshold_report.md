# Token Threshold Management System

**Report Date:** 2026-07-12  
**Scope:** Token monitoring, warning/critical thresholds, state transitions, pipeline logging  
**Status:** Active — fully instrumented with `pipeline.*` diagnostic logging

---

## 1. Overview

The token threshold management system tracks conversation token usage and emits warnings/critical alerts when configurable thresholds are exceeded. It is part of the broader `TokenMonitor` / `TokenState` subsystem in `agent/core/state.py` and is consumed by `agent/core/agent.py` during `process_query()` execution.

### Key Concepts

- **Token State Machine:** `NORMAL → WARNING → CRITICAL` (one-way, ratcheted)
- **Thresholds:** Configurable via `token_monitor_warning_threshold` and `token_monitor_critical_threshold` in `AgentConfig`
- **Warning Injection:** Token warnings are **buffered** during tool execution and **flushed** after the turn is committed, so they appear in correct chronological order
- **Pipeline Logging:** All threshold transitions, token updates, and warning flushes are logged under the `**pipeline.warning**` and `**pipeline.token_update**` categories

---

## 2. Threshold Configuration

| Setting | Default | Description |
|---|---|---|
| `token_monitor_warning_threshold` | 4000 | Token count at which `WARNING` state is entered |
| `token_monitor_critical_threshold` | 6000 | Token count at which `CRITICAL` state is entered |

Values are read from `AgentConfig` and can be overridden via the configuration UI or API.

Resolution order:
1. `config.token_monitor_warning_threshold` / `config.token_monitor_critical_threshold`
2. `config.warning_threshold * 1000` / `config.critical_threshold * 1000` (legacy, in k-tokens)

---

## 3. Token State Machine

Defined in `agent/core/state.py`, class `TokenState` (enum):

```
NORMAL ────→ WARNING ────→ CRITICAL
```

- **NORMAL:** Below warning threshold — no restrictions
- **WARNING:** Between warning and critical thresholds — soft warnings injected into conversation
- **CRITICAL:** Above critical threshold — hard restrictions, agent may refuse tool execution

### State Transition Logic (`update_token_state`)

```
update_token_state(total_tokens)
  → Determine new state from thresholds
  → Compare with current state (ratchet: only forward transitions allowed)
  → If transitioning WARNING or CRITICAL and not already warned:
      → Create token_warning event with warning_message
      → Log to **pipeline.warning**
      → Log via logger.log_token_warning()
  → Update token_state, last_token_warning_state
  → Return list of events (one per warning raised)
  → Log **pipeline.warning** with "CREATED token_warning event"
```

---

## 4. Warning Buffering and Flushing

### 4.1 Buffering Mechanism

Token warnings generated during tool execution must not be injected immediately into the conversation, because the turn's assistant message and tool results have not yet been committed to `user_history`. Injecting early would break chronological ordering.

**Buffer location:** `self._pending_warnings` (list of `Message` objects)  
**Buffer location (events):** `self._pending_warning_events` (list of raw event dicts)  

**Code path** (`_update_tokens_after_tool` in `agent/core/agent.py`):

```
for event in self.state.update_token_state(...):
    if event['type'] == 'token_warning':
        warning_msg = Message(role='user', content='[SYSTEM NOTIFICATION] ...', is_system_notification=True)
        self._pending_warnings.append(warning_msg)
        self._pending_warning_events.append(event)
        log('DEBUG', '**pipeline.warning**', f"Warning buffered in _update_tokens_after_tool: state=..., count=...")
```

### 4.2 Flush Point 1 — After Tool Execution

After `tool_executor.execute_tool_calls()` completes and `turn_transaction.commit()` is called:

```
# Flush any buffered token warnings after the turn is committed
log('DEBUG', '**pipeline.warning**', f"Flushing {len(self._pending_warnings)} buffered warnings after tool execution")
for warning in self._pending_warnings:
    self._add_to_conversation(warning)
for warning_event in self._pending_warning_events:
    yield warning_event
self._pending_warnings.clear()
self._pending_warning_events.clear()
```

### 4.3 Flush Point 2 — Non-Tool Branch

When the assistant produces a direct answer without tool calls:

```
# Flush any buffered token warnings (unlikely here, but be safe)
log('DEBUG', '**pipeline.warning**', f"Flushing {len(self._pending_warnings)} buffered warnings in non-tool branch")
for warning in self._pending_warnings:
    self._add_to_conversation(warning)
for warning_event in self._pending_warning_events:
    yield warning_event
self._pending_warnings.clear()
self._pending_warning_events.clear()
```

---

## 5. Token Update Event Points

Every `yield self._create_token_update_event()` in `agent/core/agent.py` is now preceded by a `**pipeline.token_update**` diagnostic log. There are **7 yield points**:

| # | Location | Trigger | Log Message |
|---|---|---|---|
| 1 | Line ~770 | After user query added to conversation | `Token update after user query: tokens=..., input=..., output=...` |
| 2 | Line ~887 | After `time_warning` injected | `Token update after time_warning: tokens=...` |
| 3 | Line ~904 | After `turn_warning` injected | `Token update after turn_warning: tokens=...` |
| 4 | Line ~910 | After turn state update (no warning) | `Token update after turn state: tokens=..., turn=...` |
| 5 | Line ~918 | After `token_warning` injected | `Token update after token_warning: tokens=...` |
| 6 | Line ~1159 | After assistant message committed | `Token update after assistant commit: tokens=...` |
| 7 | Line ~1226 | After summary pruning applied | `Token update after summary pruning: tokens=...` |

The `_create_token_update_event()` method produces:

```python
event = {
    'type': 'token_update',
    'context_length': self.state.current_conversation_tokens,
    'total_input': self.total_input_tokens,
    'total_output': self.total_output_tokens,
    # Plus conversation version data from _add_conversation_data_to_event()
}
```

---

## 6. Pipeline Logging Categories

All threshold-related logging uses the `**pipeline.warning**` and `**pipeline.token_update**` categories so they can be easily filtered from other debug logs.

### `**pipeline.warning**` — All warning lifecycle events

| Source | Message |
|---|---|
| `state.py:update_token_state` | `ENTER update_token_state: total_tokens=..., current_state=..., warning_threshold=..., critical_threshold=...` |
| `state.py:update_token_state` | `WARNING DETECTED: transitioning old -> new at ... tokens` |
| `state.py:update_token_state` | `CREATED token_warning event: old=..., new=..., count=...` |
| `state.py:update_time_state` | `ENTER update_time_state: elapsed=..., timeout=..., warning_at=...` |
| `state.py:update_time_state` | `WARNING: time warning at ...s, new_state=...` |
| `state.py:update_time_state` | `CREATED time_warning event: old=..., new=..., elapsed=...s` |
| `state.py:update_turn_state` | `ENTER update_turn_state: current_turn=..., max_turns=...` |
| `state.py:update_turn_state` | `WARNING: turn warning fired at .../...` |
| `state.py:update_turn_state` | `CREATED turn_warning event: old=..., new=..., count=...` |
| `agent.py:_update_tokens_after_tool` | `Warning buffered in _update_tokens_after_tool: state=..., count=...` |
| `agent.py:flush (tool branch)` | `Flushing ... buffered warnings after tool execution` |
| `agent.py:flush (non-tool branch)` | `Flushing ... buffered warnings in non-tool branch` |
| `event_processor.py` | `token_warning_received: message=..., token_count=...` |
| `event_processor.py` | `turn_warning_received: message=..., turn_count=...` |

### `**pipeline.token_update**` — All token update event emissions

| Source | Message |
|---|---|
| `state_bridge.py:update_token_totals` | `update_token_totals: input=..., output=...` |
| `state_bridge.py:update_context_length` | `update_context_length: context_length=...` |
| `event_processor.py` | `received: context_length=..., total_input=..., total_output=...` |
| `agent.py` (7 yield points) | `Token update after <trigger>: tokens=..., ...` |

---

## 7. Time and Turn Warning Systems

Token monitoring is complemented by time and turn monitoring, both using the same buffering/flushing pattern.

### Time Monitoring (`update_time_state`)

| State | Threshold | Effect |
|---|---|---|
| `TimeState.NORMAL` | elapsed < time_warning_threshold | No action |
| `TimeState.WARNING` | time_warning_threshold <= elapsed < timeout | Soft notification injected |
| `TimeState.CRITICAL` | elapsed >= timeout | Soft restriction logged |

### Turn Monitoring (`update_turn_state`)

| State | Threshold | Effect |
|---|---|---|
| `TurnState.NORMAL` | turn < max_turns - 3 | No action |
| `TurnState.WARNING` | turn >= max_turns - 3 | Soft notification injected, `restrictions_active = True` |
| `TurnState.CRITICAL` | (not explicitly defined — ratchet stops at WARNING) | — |

---

## 8. Testing Notes

To **verify pipeline logging** is working correctly:

1. **Trigger a token warning:** Set `token_monitor_warning_threshold` to a low value (e.g., 100 tokens). Run a query that produces enough content to exceed the threshold. Look for `**pipeline.warning**` entries in the logs showing the state transition and buffering.

2. **Trigger a token critical:** Set `token_monitor_critical_threshold` to a low value. Confirm the ratchet prevents regression from CRITICAL back to WARNING.

3. **Verify flush ordering:** After a tool-calling turn that exceeds the warning threshold, confirm that the `**pipeline.warning** "Flushing ..."` log appears *after* tool results and *before* any subsequent turn processing.

4. **Verify token update logs:** Filter for `**pipeline.token_update**` to confirm all 7 yield points fire in the expected sequence during a multi-turn session.

5. **Verify no token update log at warning flush points:** The flush points emit `**pipeline.warning**` logs but not `**pipeline.token_update**` logs (because token_update events are yielded as part of the original warning event processing, not re-generated here).
