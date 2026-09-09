# Tool-call "streaming": backend contract for live tool-call START events

Status: **Investigation document — no UI streaming feature built (decision 3)**
Date: task6 investigation
Scope: web_ui/frontend team. Backend untouched.

## TL;DR

There is **no backend event that fires when a tool call STARTS** on any path
(main agent, worker, tool executor). The existing `tool_call` events are
emitted **back-to-back with `tool_result`, strictly AFTER the tool has
finished executing**. The frontend therefore already has complete rendering
vocabulary for tool calls, but nothing can trigger a *live/streaming* tool-call
row during execution. Per decision rule (3): do NOT build untriggerable UI;
this document records the missing event contract, the suggested backend
emission point (file/function level only — no backend edits made), the
frontend consumption plan, and the full evidence trail.

The only pre-completion visibility that exists today is a **history snapshot**
(assistant message containing `tool_calls`, no result yet) which reaches the
main chat because the assistant message is committed to `user_history` before
execution begins. Worker panels see nothing until the completed
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

- No event is published **before/during** execution. `self._event_bus` is
  stored (`__init__` :141) but never emitted to anywhere in this flow.
- `executed_tools` entries (the raw material for the later `tool_call` yields)
  only gain `container_id`/`exec_id`/`tool_call_id`-style fields **after** the
  tool runs and its processed result is returned — there is no pre-execution
  bus event that could serve as a START signal.

### 1.3 Worker path — `tools/workspace/worker_thread.py`

Workers consume the **same** post-completion stream, so per-worker bus events
are post-completion too:

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

Per-worker bus subscription list includes `'tool_call', 'tool_result'`
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

## 3. The gap (why no streaming UI was built)

| Path | Pre-execution signal exists? |
|---|---|
| Main-agent raw stream | No — first tool visibility is the history snapshot from the `turn` yield (commit_assistant_only at agent.py:1295), which renders as a pending tool_calls bubble only if the chat normalizes assistant messages with `tool_calls`; the dedicated `tool_call` event is post-completion |
| Tool executor | No — no event_bus emission anywhere in `execute_tool_calls` |
| Worker bus / WS | No — purely derived from the same post-completion yields |
| Frontend worker panel | Shows the `tool_call` row only when the (completed) `tool_call`+`tool_result` pair arrives |

Building UI "streaming support" (adaptWorkerEvent/routing/dedup/vitest) now
would produce code that can never fire. Hence decision (3).

## 4. Proposed backend contract (NOT implemented)

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
worker:tool_call_start  {tool_name, arguments, tool_call_id?}        pre-execution (proposed)
worker:tool_result   {tool_name, success, error, result[:1000]}      post-completion (today)
```

Main-agent chat would additionally need the assistant-message-with-tool_calls
snapshot to render pending bubbles (already available via the committed
history at agent.py:1295), or a main-agent-side start broadcast — out of scope
here.

## 5. Frontend consumption plan (when the backend contract lands)

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

Keep the UI as-is. When tool-call streaming is actually wanted, implement the
backend START event first (section 4), then apply the small frontend plan
(section 5). No backend files were modified by this investigation; this doc is
the deliverable for the "no start event exists" decision branch.
