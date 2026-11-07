# Ticket: bug/worker-context-not-shown-in-panel

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, isolation-audit pass (gen-47); line numbers read from the working tree of the linked worktree `ThoughtMachine-dev`, dev `9cd06b34`. Provenance in the original draft (`feat/workspace-panel`, `b23c26b`) was template-contaminated and is withdrawn.
**Severity:** MEDIUM — revised upward in review from the LOW filed on first pass. No data is lost and nothing unsafe happens; the worker is running correctly and the panel simply cannot display what it is doing. Filed because it is a payload-contract mismatch between two halves of the same feature, and because the naive diagnosis ("the frontend does not render the field") is wrong and would send a fix to the wrong file. Revisit at HIGH if the worker panel becomes load-bearing for supervising unattended workers in V3 — at that point the finding is absence of observability, not a missing field.
**Status:** open; verified by read. Verdict: **SERVER-PAYLOAD** — the field the panel reads is absent from the endpoint the panel polls, while three other surfaces carry it.

## Finding

The report was "the worker's current context / current task is not shown in the panel". It is a real defect, and the cause is on the server side, not in the component.

**The panel reads the field.** `web_ui/frontend/src/components/WorkerManagementPanel.jsx` defines `fetchWorkers` (`:605-641`), which GETs `/api/workspace/${workspaceId}/workers/active` (`:608`) and maps each entry to a row that includes `current_task: e.current_task` (`:635`). The table then renders it conditionally at `:1285` (`w.current_task &&`), with `title={w.current_task}` at `:1302` and the body at `:1304` (`<strong>{w.instance_label || w.name}:</strong> {truncate(w.current_task, 100)}`). Poll interval is 3000 ms (`:643-647`). So the frontend half is correct and unconditional: if `current_task` arrives, it displays.

**The endpoint does not send it.** `GET /api/workspace/{ws_id}/workers/active` → `web_ui/backend/workspace_routes.py:613-634` (`get_active_workers`) → `_collect_active_workers` (`:532-610`) → `_worker_active_entry(thread)` (`:510-529`). That last function builds the payload and returns **exactly eight keys** (`:520-529`): `worker_name`, `instance_id`, `status`, `elapsed`, `name`, `session_id`, `last_heartbeat`, `container_id`. There is no `current_task`, and no `current_context_tokens`, `max_context_tokens` or `paused_manually` either. The endpoint docstring at `:620-625` enumerates the same eight fields, so this is the intended contract, not an oversight in one return statement.

Consequence: `e.current_task` in the panel is always `undefined` → `w.current_task &&` at `:1285` is always falsy → the task line never renders → the row shows only the status chip. That is exactly the reported symptom, and it explains why the symptom is stable rather than intermittent: the poll succeeds every 3 s and every response is missing the key.

**The data exists and is exported elsewhere.** `WorkerThread` owns a live `current_task`, and three other surfaces publish it:
- `tools/workspace/worker_thread.py:3135-3156` (`_write_status_file`) writes `"current_task": self.current_task` (`:3147`) into `status.json`; it is read back and re-exported by `web_ui/backend/health_routes.py:82` and `:101` (`"current_task": data.get("current_task"),`).
- The `/workers` definitions/list endpoint emits it — `worker_thread.py:4643` `"current_task": thread.current_task,` (also `:4590`, `:4603`, `:4629`, and `:3855` `entry["current_task"] = thread.current_task`).
- The WebSocket `worker_status` / `WORKER_RUNNING` events carry it — `worker_thread.py:2543` `self._publish_event('worker_status', {'status': 'busy', 'current_task': self.current_task})`, with the `EventType.WORKER_STATUS` data dict at `:2549-2561` including `"current_task": self.current_task` (`:2555`), plus `:2568-2575`, `:2570`.

So the field is **present-but-omitted**, not present-but-not-fetched: the omission is in one payload builder, and `_worker_active_entry` is the thing to change. Adding `"current_task": getattr(thread, "current_task", None)` there is the whole fix, and the frontend needs no edit.

**Secondary defect, smaller and separate.** The consumer that *does* receive the WS event — `WorkerOutputPanel.jsx:288` merges `current_task: e.data?.current_task || prev?.current_task` and renders `workerInfo?.current_task` at `:790-792` — gets `null` at the busy transition, because the event is published from `worker_thread.py:2541-2543` (`self.status = "busy"` → status-file write → publish) *before* `self.current_task = query[:200]` is assigned at `:1817`, inside `_run_tool_loop`, which is only entered at `:2597`. So the busy event races the task assignment: the first busy event always carries the *previous* (or `None`) task, and only a later status write carries the new one. Same class of bug, different hop; do not conflate the two in a fix. `self.current_task` is cleared at `:1599`, `:2095`, `:2431`, `:2646`, `:2770`.

Path correction to the filed report: `worker_thread.py` is at **`tools/workspace/worker_thread.py`** (5535 lines), not under `agent/`.

## Evidence

Frontend reads the field:

- `web_ui/frontend/src/components/WorkerManagementPanel.jsx:608` — `fetch(\`/api/workspace/${workspaceId}/workers/active${query}\`)`
- `web_ui/frontend/src/components/WorkerManagementPanel.jsx:635` — `current_task: e.current_task,`
- `web_ui/frontend/src/components/WorkerManagementPanel.jsx:643-647` — 3000 ms poll.
- `web_ui/frontend/src/components/WorkerManagementPanel.jsx:1285`, `:1302`, `:1304` — `w.current_task &&` / `title={w.current_task}` / `{truncate(w.current_task, 100)}`.
- `web_ui/frontend/src/components/WorkerOutputPanel.jsx:288` — `const update = { runtime_status: status, current_task: e.data?.current_task || prev?.current_task };`; `:790-792` renders `workerInfo?.current_task`.
- `web_ui/frontend/src/data/apiContracts.js:252` — `@property {string|null} currentTask — backend \`current_task\``; `web_ui/frontend/src/store/workspaceStore.js:196` — `currentTask: w.current_task ?? null`.

Server omits it (the defect):

- `web_ui/backend/workspace_routes.py:613-634` — `get_active_workers` (route handler).
- `web_ui/backend/workspace_routes.py:532-610` — `_collect_active_workers`.
- `web_ui/backend/workspace_routes.py:510-529` — `_worker_active_entry`; the returned dict at `:520-529` is `worker_name`, `instance_id`, `status`, `elapsed`, `name`, `session_id`, `last_heartbeat`, `container_id`, and nothing else:
  - `:520` `return {`
  - `:521` `"worker_name": getattr(thread, "worker_name", ""),`
  - `:522` `"instance_id": getattr(thread, "instance_id", 1),`
  - `:523` `"status": getattr(thread, "status", "unknown"),`
  - `:524` `"elapsed": _worker_elapsed_seconds(getattr(thread, "started_at", None)),`
  - `:525` `"name": getattr(thread, "worker_name", ""),`
  - `:526` `"session_id": getattr(thread, "session_id", "") or "",`
  - `:527` `"last_heartbeat": getattr(thread, "last_heartbeat", None),`
  - `:528` `"container_id": getattr(thread, "container_id", None),`
  - `:529` `}`
- `web_ui/backend/workspace_routes.py:620-625` — endpoint docstring enumerating the same eight fields (confirms intended contract, not a stray omission).

The field exists on other surfaces:

- `tools/workspace/worker_thread.py:3147` — `"current_task": self.current_task` in `_write_status_file` (`:3135-3156`).
- `web_ui/backend/health_routes.py:82`, `:101` — reads `status.json`, re-exports `"current_task": data.get("current_task"),`.
- `tools/workspace/worker_thread.py:4643` — `"current_task": thread.current_task,` (also `:4590`, `:4603`, `:4629`, `:3855`).
- `tools/workspace/worker_thread.py:2543` — `self._publish_event('worker_status', {'status': 'busy', 'current_task': self.current_task})`; `:2549-2561` with `:2555` `"current_task": self.current_task`; also `:2568-2575`, `:2570`.

Secondary (WS race):

- `tools/workspace/worker_thread.py:2541-2543` — status set busy, status file written, event published.
- `tools/workspace/worker_thread.py:1817` — `self.current_task = query[:200]` (inside `_run_tool_loop`).
- `tools/workspace/worker_thread.py:2597` — the call site that enters `_run_tool_loop`, i.e. after the publish.
- clear sites: `:1599`, `:2095`, `:2431`, `:2646`, `:2770`.

## What this is not

- Not a frontend bug. `WorkerManagementPanel.jsx` reads and renders `current_task` correctly; the value never arrives. A fix in the JSX would be a fix to nothing.
- Not "the field isn't tracked". `WorkerThread.current_task` is set, cleared and published; only the `/workers/active` builder omits it.
- Not a duplicate of `ticket-hygiene-worker-context-null-fields.md`. That one is about `worker/*/context.json` on disk having `state`/`spawned_at`/`stopped_at`/`stop_reason`/`tool_calls_made` null; this one is about an HTTP payload omitting a field the server already holds. Different artefact, different fix.
- Not a duplicate of the `/workers` list endpoint's behaviour — that one already includes the field, which is why the same field can be visible in one part of the UI and missing in another.
- Not a claim that `/workers/active` should mirror `/workers` wholesale. It is the *active* set with a deliberately small row shape; adding one field is a contract change worth noting in `apiContracts.js`, not a merge of the two endpoints.
- Not proof of a runtime reproduction. No server was started; the conclusion is a read of both ends of one request/response pair plus the docstring that states the payload contract.

## Disposition

Primary: add `current_task` to `_worker_active_entry` (`web_ui/backend/workspace_routes.py:520-529`) — `getattr(thread, "current_task", None)` matches the defensive style of its eight neighbours — and update the `:620-625` docstring plus `web_ui/frontend/src/data/apiContracts.js:252` so the contract and the code agree. Frontend: no change required.

Secondary, separate ticket-or-not decision: the `worker_status` busy event at `worker_thread.py:2541-2543` is published before `current_task` is assigned at `:1817`. Reordering the assignment ahead of the publish fixes the WS consumers (`WorkerOutputPanel`) but touches the thread's state sequence, so it should not ride along with the payload fix.

If `current_context_tokens` / `max_context_tokens` are also wanted in the panel — the report's "current context" phrasing suggests they may be — decide that as a contract change, not as part of this fix; none of the four are present in the payload today.

## MUST NOT TOUCH

- `web_ui/frontend/src/components/WorkerManagementPanel.jsx:605-647`, `:1285-1304` — the read side is correct; changing it would mask the server contract instead of fixing it.
- `tools/workspace/worker_thread.py:3135-3156` (`_write_status_file`) and `web_ui/backend/health_routes.py:82`, `:101` — the status-file surface already works and is consumed elsewhere; do not refactor it into the fix.
- `web_ui/backend/workspace_routes.py:532-610` (`_collect_active_workers`) — the filtering/aggregation logic is not implicated; the omission is one dict in `_worker_active_entry`.
- The WebSocket `worker_status` event shape (`worker_thread.py:2543`, `:2549-2561`) — consumers key off it; the secondary finding is an ordering question, not a shape change.
