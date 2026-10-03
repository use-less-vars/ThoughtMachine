# Tool-call "streaming": backend contract for live tool-call START events

Status: **Implemented (PR #TBD) — the tool_call_start event was added by this PR.**
Date: task6 (implemented, PR #TBD)
Scope: web_ui/frontend team. Backend modified (PR #TBD).

## TL;DR

There is now a **backend event that fires when a tool call STARTS** —
`tool_call_start`, emitted **pre-execution** by the agent turn loop
(`agent/core/agent.py`, before `execute_tool_calls`), forwarded by
`WorkerBusAdapter.forward_agent_event` (`tools/workspace/worker_thread.py`),
subscribed by the bridge (`web_ui/backend/bridge.py`), and rendered by the
frontend as a pending tool-call row (`WorkerOutputPanel.jsx`, `status:'running'`);
see PR #TBD. The existing `tool_call` events are
emitted **back-to-back with `tool_result`, strictly AFTER the tool has
finished executing**. The frontend therefore already has complete rendering
vocabulary for tool calls, and can now trigger a *live/streaming* tool-call
row during execution. This document records the implemented event contract
(PR #TBD): the backend emission point, the frontend consumption plan, and the
full evidence trail.

Pre-completion visibility now includes both a live **`tool_call_start`** row
(PR #TBD) and the **history snapshot**
(assistant message containing `tool_calls`, no result yet) which reaches the
main chat because the assistant message is committed to `user_history` before
execution begins. Worker panels render a pending `tool_call_start` row before the completed
`tool_call`+`tool_result` pair is published.

## 1. Current emission timing (the core finding)

### 1.1 Main-agent raw event stream — `agent/core/agent.py`

```
1295-1296  turn_transaction.commit_assistant_only()   # assistant msg incl. tool_calls
                                                      # persisted to user_history NOW
1298       turn_event built with 'tool_calls': []     # tool_calls field EMPTIED
1301-1304  reasoning/tool_calls handling; yield turn_event   # <-- pre-execution yield
1306       executed_tools, ... = self.tool_executor.execute_tool_calls(tool_calls, ...)
                                                      # SYNCHRONOUS; tool(s) run here;
                                                      # NO events yielded meanwhile
1317-1320  turn_transaction.commit()                  # ALL tool results committed to
                                                      # user_history BEFORE next yield
                                                      # ("so a pause between yields
                                                      #  doesn't lose data")
1321-1324  yield {'type': 'tool_call', 'tool_name', 'arguments', 'success', 'error', 'turn'}
1325-1328  yield {'type': 'tool_result', ...}          # both strictly POST-completion
```

Every `tool_call`/`tool_result` pair is derived from the `executed_tools`
return value of the synchronous call — i.e. both are emitted **after** the tool
finished. Note the emitted `tool_call` event does **not** carry `tool_call_id`
(only `tool_name`/`arguments`/`success`/`error`/`turn`).

### 1.2 Tool executor — `agent/core/tool_executor.py`

`execute_tool_calls` (lines 145-280) is a per-call loop:

```
182   for _tc_index, tool_call in enumerate(tool_calls):
207-214  tool_name lookup / disallowed-tool shortcut (result written synchronously)
215-233  arguments JSON parse / repair
238-245  tool class resolution
252     tool_execution_result = self._execute_single_tool(...)   # SYNCHRONOUS run()
271-274  tool result msg buffered via turn_transaction.add_tool_result(...)
279     executed_tools.append({'name', 'arguments', 'result'})
```

- No event is published **by the executor before/during** execution; the
  pre-execution `tool_call_start` is emitted upstream by the agent turn loop
  (§1.1, PR #TBD). `self._event_bus` is stored (`__init__` :141) but never
  emitted to anywhere in this flow.
- `executed_tools` entries (the raw material for the later `tool_call` yields)
  only gain `container_id`/`exec_id`/`tool_call_id`-style fields **after** the
  tool runs and its processed result is returned — the executor emits no
  pre-execution bus event; the START signal is the agent-side `tool_call_start`
  (§1.1, PR #TBD).

### 1.3 Worker path — `tools/workspace/worker_thread.py`

Workers consume the **same** stream, and now forward the pre-execution
`tool_call_start` (PR #TBD) alongside the post-completion events:

```
1750  for event in self._agent.process_query(query):
1819-1836  event_type = event.get('type'); per-event:
          EventProcessor.process_event(event)
          self._worker_bus_adapter.forward_agent_event(event)
```

`WorkerBusAdapter.forward_agent_event` payloads (what the per-worker EventBus
receives):

```
429-436  event_type == 'tool_call'    -> _publish("tool_call",
                                     {"tool_name": ..., "arguments": ...})
                                     # NOTE: tool_call_id NOT forwarded
438-449  event_type == 'tool_result'  -> _publish("tool_result",
                                     {"tool_name", "success", "error",
                                      "result": str(result)[:1000]})
                                     # NOTE: tool_call_id NOT forwarded
```

(Truncation: result capped at 1000 chars, :448.)

### 1.4 WebSocket payloads today — `web_ui/backend/bridge.py`

Per-worker bus subscription list includes `'tool_call_start', 'tool_call', 'tool_result'`
(:709-715). The `_make_bus_handler` has no dedicated branch for them — they
fall through the **generic** else branch (:790-802):

```json
{
  "type": "worker:tool_call",        //  = "worker:" + original_type
  "worker_name": "...",
  "instance_id": "...",
  "instance_label": "...",
  "timestamp": "...",
  "data": { "tool_name": "...", "arguments": "..." }    // raw bus payload passthrough
}
```

i.e. `worker:tool_call` data = `{tool_name, arguments}` and
`worker:tool_result` data = `{tool_name, success, error, result}`.
(Special-cased siblings: `tokens_updated` flattened :733-742,
`context_updated` :743-775, `context_summarized` :776-789.)

Main-agent chat is different: raw `tool_call`/`tool_result`/`turn` events are
not forwarded as live events at all — bridge broadcasts a full
`conversation_changed` history snapshot on each (:2447-2461), because by the
time those yields happen the session is already committed.

## 2. What the frontend already does (vocabulary is complete)

- `SessionTab.jsx:902-939` — `worker:tool_call` / `worker:tool_result` cases
  route to `onWorkerEvent` (no dedup/suppression at this layer).
- `WorkerOutputPanel.jsx:333-355` — WS → panel shape conversion; `tool_call`
  case (:340-350) reads `e.data.tool_name` / `e.data.arguments` (string **or**
  object, `JSON.parse` fallback) → `request = {tool, args}`; `tool_result`
  case (:351-355) → `request = {tool, success}`, `response = {result}`.
- `chat/adaptWorkerEvent.js:159-171` — panel shape `tool_call` → MessageBubble
  role `'tool_call'` with content `{name: req.tool, arguments: req.args}`;
  :174-186 `tool_result` → role `'tool_result'`.
- Dedup before render: `WorkerOutputPanel.jsx:323-331` —
  `makeDedupKey(rawType, timestamp)` where `rawType` strips the `worker:`
  prefix; already-seen `type|timestamp` keys are dropped.
- Tests: `chat/adaptWorkerEvent.test.js:303-342` (tool_call/tool_result).
- `MessageBubble` renders roles `tool_call`/`tool_result`; WorkspacePanel
  `EVENT_BADGE_COLORS` includes `tool_call` (WorkspacePanel.jsx:63-69).

## 3. The former gap (why no streaming UI was built before PR #TBD)

| Path | Pre-execution signal exists? |
|---|---|
| Main-agent raw stream | Yes — the turn loop yields `tool_call_start` pre-execution (PR #TBD); first tool visibility was the history snapshot from the `turn` yield (commit_assistant_only at agent.py:1295), which renders as a pending tool_calls bubble only if the chat normalizes assistant messages with `tool_calls`; the dedicated `tool_call` event is post-completion |
| Tool executor | No — no event_bus emission anywhere in `execute_tool_calls` |
| Worker bus / WS | Yes — now forwards the pre-execution `tool_call_start` in addition to the post-completion yields (PR #TBD) |
| Frontend worker panel | Shows a pending `tool_call_start` row before the (completed) `tool_call`+`tool_result` pair arrives (PR #TBD) |

Building UI "streaming support" (adaptWorkerEvent/routing/dedup/vitest) is now
backed by the live `tool_call_start` event, so it fires (PR #TBD).

## 4. Backend contract (implemented — PR #TBD)

For when real-time tool-call visibility is wanted, the minimal contract is one
new START event emitted **before** `execute_tool_calls` runs.

### 4.1 Raw agent stream

Suggested emission point: `agent/core/agent.py`, in the turn loop **between**
the `turn_event` yield (:1304) and the synchronous `execute_tool_calls` call
(:1306) — iterate the LLM `tool_calls` param (already in hand there) and yield
one event per call:

```python
{'type': 'tool_call_start',
 'tool_name': tc['function']['name'],
 'arguments': tc['function']['arguments'],   # raw JSON string (as sent by the model)
 'tool_call_id': tc['id'],                   # AVAILABLE here; absent from the
                                             # post-completion tool_call yield
 'turn': ...}
```

Rationale for a distinct `tool_call_start` type (vs. overloading the existing
`tool_call` with a phase flag): the existing `tool_call` event is derived from
the executed-tools results (agent.py:1321) and its consumers already treat it
as "call happened + we know the outcome"; a new type keeps ordering semantics
unambiguous (start always precedes the paired completion events) and lets the
worker panel render a distinct "running" state.

### 4.2 Worker bus + WS (no extra backend work beyond the adapter branch)

- `tools/workspace/worker_thread.py` `forward_agent_event`: add a branch

```python
elif event_type == "tool_call_start":
    self._publish("tool_call_start", {
        "tool_name": event.get("tool_name", ""),
        "arguments": event.get("arguments", ""),
        "tool_call_id": event.get("tool_call_id", ""),   # recommend adding!
    })
```

- `web_ui/backend/bridge.py:709-715`: add `'tool_call_start'` to
  `subscribed_types`. The generic handler else-branch (:790-802) then
  automatically produces the WS message:

```json
{ "type": "worker:tool_call_start",
  "worker_name": "...", "instance_id": "...", "instance_label": "...",
  "timestamp": "...",
  "data": { "tool_name": "...", "arguments": "...", "tool_call_id": "..." } }
```

### 4.3 Payload contract — summary for frontend

```
event                data (WS, under .data)                          timing
worker:tool_call     {tool_name, arguments}                          post-completion (today)
worker:tool_call_start  {tool_name, arguments, tool_call_id?}        pre-execution (implemented, PR #TBD)
worker:tool_result   {tool_name, success, error, result[:1000]}      post-completion (today)
```

Main-agent chat would additionally need the assistant-message-with-tool_calls
snapshot to render pending bubbles (already available via the committed
history at agent.py:1295), or a main-agent-side start broadcast — out of scope
here.

## 5. Frontend consumption plan (backend contract landed, PR #TBD)

1. `WorkerOutputPanel.jsx` transform switch (:339+): add
   `case 'tool_call_start'` mirroring `tool_call` (:340-350) — build
   `request = {tool, args}`; optionally tag the row as running
   (`status: 'running'`) until the matching `tool_result` arrives.
2. `chat/adaptWorkerEvent.js`: handle `tool_call_start` identically to
   `tool_call` (:159-171) or add an `is_running` flag to the returned msg.
3. Dedup: current key is `type|timestamp` (WorkerOutputPanel.jsx:328) so a
   distinct `tool_call_start` type renders independently — but for reliable
   start→result **pairing** in multi-call turns, prefer keying on
   `tool_call_id` once it is present in the payload (see 4.1/4.2 — it is
   currently dropped at worker_thread.py:433-436 and not emitted at
   agent.py:1321).
4. Tests: extend `chat/adaptWorkerEvent.test.js` with `tool_call_start`
   fixtures (mirror :303-342); run with
   `cd /workspace/web_ui/frontend && /workspace/node-bin/bin/node node_modules/vitest/vitest.mjs run --maxWorkers=1 --testTimeout=15000`.

## 6. Evidence trail

| Claim | Evidence |
|---|---|
| Assistant msg committed pre-execution | agent/core/agent.py:1295-1296 (`commit_assistant_only`) |
| turn_event tool_calls emptied | agent/core/agent.py:1298 |
| turn yield precedes execution | agent/core/agent.py:1301-1306 |
| Tools run synchronously, no yields during | agent/core/agent.py:1306 (`execute_tool_calls` call) |
| Results committed before further yields | agent/core/agent.py:1317-1320 (+ comment) |
| tool_call/tool_result yielded post-completion, back-to-back | agent/core/agent.py:1321-1328 |
| No pre-execution emission in executor | agent/core/tool_executor.py:182-280 (loop body), :252 sync `_execute_single_tool`; `_event_bus` stored at :141, never published |
| Tool executor result fields only post-run | agent/core/tool_executor.py:271-279 |
| commit_assistant_only / commit semantics | agent/core/turn_transaction.py:72-100, :102-140 |
| Worker consumes same stream per-event | tools/workspace/worker_thread.py:1750, :1819-1836 |
| Worker bus tool_call payload (no tool_call_id) | tools/workspace/worker_thread.py:429-436 |
| Worker bus tool_result payload + truncation | tools/workspace/worker_thread.py:438-449 |
| Bridge subscribes tool_call/tool_result | web_ui/backend/bridge.py:709-715 |
| Generic WS mapping (worker:<type>, data passthrough) | web_ui/backend/bridge.py:790-802 |
| Special-case siblings only for tokens/context | web_ui/backend/bridge.py:733-789 |
| Main-agent chat syncs via full history on raw events | web_ui/backend/bridge.py:2447-2461 |
| Frontend routes worker:tool_call/result | web_ui/frontend/src/components/SessionTab.jsx:902-939 |
| WS→panel shape conversion | web_ui/frontend/src/components/WorkerOutputPanel.jsx:333-355 |
| Panel render path adaptWorkerEvent | web_ui/frontend/src/components/WorkerOutputPanel.jsx:843-849 |
| adaptWorkerEvent tool_call/tool_result | web_ui/frontend/src/components/chat/adaptWorkerEvent.js:159-186 |
| Dedup key type\|timestamp | web_ui/frontend/src/components/WorkerOutputPanel.jsx:323-331 |
| Existing tests | web_ui/frontend/src/components/chat/adaptWorkerEvent.test.js:303-342 |

## 7. Recommendation

The "keep the UI as-is" stance is superseded: tool-call streaming is now wanted,
so the backend START event is implemented (section 4) and the small frontend plan
(section 5) applied, in PR #TBD. This PR modifies the backend files
tools/workspace/worker_thread.py, web_ui/backend/bridge.py, agent/core/agent.py
and agent/events.py; this doc is the deliverable for the "start event now exists"
decision branch.
