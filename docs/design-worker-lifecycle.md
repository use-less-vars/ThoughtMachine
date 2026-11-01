# Design: Worker Lifecycle (Q1–Q5)

Scope: five questions about worker sub-agent lifecycle — stop semantics, timeout
semantics, config propagation into a live worker, job-handle (async) tool calls, and
the tools UI surface. Every factual claim below carries `path:LINE` plus a verbatim
quote of at most two lines. Where the codebase does not answer a question, the entry
reads `NOT FOUND — searched <patterns>`.

| # | Question (short) | Recommendation | Risk |
|---|---|---|---|
| Q1 | Does Stop/stop detach a worker or terminate it? | Keep terminate-and-persist; add explicit "detach" only as a separate verb, never reuse `stop` | medium |
| Q2 | What happens when a worker exceeds its timeout — is the work continued? | Keep timeout terminal (no auto-continue), and wire the already-resolved retry budget or delete it | high |
| Q3 | Does a config/addendum write reach an already-running worker? | Declare spawn-time snapshot as the contract; do not attempt write-through into worker agents | medium |
| Q4 | Should tool calls return a job handle instead of blocking the turn? | No for v1: keep tools synchronous; reuse the existing `submit_query`/`job_status` shape if async arrives | high |
| Q5 | Is there a UI surface for tools / tool authoring? | Keep `/api/tools` read-only; document "tools are code-defined" and do not build an authoring UI | low |

---

## Q1 — Stop semantics: terminate or detach?

### Question as understood

When a user or parent agent "stops" a worker (Stop button, `Worker` tool
`action='stop'`, or the per-worker REST route), does the worker survive as a
detached thread that can later be re-attached/queried, or is it torn down
terminally? And if it is terminal, can the same instance be continued afterwards?

### Current behaviour

Stop is **terminate**, cooperatively, and it is irreversible for that instance.

- The signal itself is cooperative, not a kill:
  `tools/workspace/worker_thread.py:1445` — `"""Signal the worker to stop after completing its current task."""`
- `stop()` sets the stop event and writes a file command:
  `tools/workspace/worker_thread.py:1467` — `cmd_path = self._worker_dir / "command.json"`
  (the payload is `{"action": "stop"}`, written immediately after that line).
- The `action='stop'` handler then does a bounded join and always pops the registry:
  `tools/workspace/worker_thread.py:5362` — `_join_budget = max(30, thread._timeout_seconds)`
  and `tools/workspace/worker_thread.py:5398` — `_worker_registry.pop((sid, self.worker_name, iid), None)`.
- A worker that ignores the budget is *still* dropped from the registry:
  `tools/workspace/worker_thread.py:5377-5378` —
  `"Worker '%s' did not stop within %ds budget. "` /
  `"Popping from registry anyway (daemon thread).",`
- Teardown persists the context and reclaims containers before the pop:
  `tools/workspace/worker_thread.py:5389-5390` — `thread._save_context()` /
  `thread._cleanup_worker_containers()`.
- Terminal state is written as `stopped`, not `completed`:
  `tools/workspace/worker_thread.py:5394-5395` — `if thread.status != "error":` /
  `thread.status = "stopped"`.
- `resume()` explicitly refuses a stopped instance:
  `tools/workspace/worker_thread.py:1524` —
  `if self._stop_event.is_set() or self.status in ("stopping", "stopped"):`
  and `tools/workspace/worker_thread.py:1521-1522` — `Callers that need a query to run` /
  `after a stop must spawn a fresh instance instead.`

- Session Stop is turn-scoped for the parent but kills the children:
  `web_ui/backend/server.py:1535-1536` — `# Stop button: truncate the running turn only — the bridge` /
  `# thread and the session survive (close_session is what`
  then `web_ui/backend/bridge.py:1881` — `workers_stopped = self._stop_session_workers()`
  whose docstring says `web_ui/backend/bridge.py:1790-1791` —
  `Worker sub-agents are children of the session, not of the parent's` /
  `current turn: unless something stops them here they outlive the Stop`
- The frontend simply sends the command:
  `web_ui/frontend/src/components/QueryBar.jsx:74` — `sendCommand('stop_session', {})`
- The per-worker REST route is fire-and-forget from the caller's point of view:
  `web_ui/backend/workspace_routes.py:1266` — `@router.post("/{ws_id}/workers/{name}/stop")`
  and `web_ui/backend/workspace_routes.py:1291` —
  `registry.  Returns immediately — the worker will transition to ``stopped```

So the *appearance* differs (REST returns at once, the tool action blocks on the
join) but the *effect* is identical: thread signalled, containers reclaimed, context
saved, registry entry removed, `status == "stopped"`, `resume()` refuses.

### Options

**Option A — keep terminate (status quo), document it.**
Tradeoff: one meaning for "stop", no silent background token burn
(`web_ui/backend/bridge.py:1791` names exactly that failure mode). Cost: a caller who
wanted to keep working must re-spawn and re-establish context; the only continuity is
whatever `_save_context()` persisted.

**Option B — make stop detach (thread survives, registry entry retained).**
Tradeoff: preserves in-flight work and lets a later query land on the same agent
memory. Cost: directly contradicts
`tools/workspace/worker_thread.py:1521-1522` (a stopped thread re-entering its loop
"would immediately report `{"status": "stopped"}`"), so it requires reworking the
stop flag, the input queue sentinel and the daemon-thread escape hatch at
`:5377-5378`.

**Option C — split the verbs: `stop` stays terminate, add `detach`/`hand_off`.**
Tradeoff: keeps the existing contract intact and gives the "let it finish in the
background" case its own explicit, discoverable verb. Cost: a second surface (tool
action + REST route) and a new lifecycle state to test; also re-introduces the
outliving-work hazard unless detach is bounded by
`agent/config/defaults.py:61` — `WORKER_TIMEOUT_SECONDS = 600`.

### Recommendation

**Option C.** Keep `stop` exactly as it is (terminate, persist, reclaim, pop) and
add `detach` as a separate verb only if a real caller needs background completion.
Rationale: `stop` is already uniform across the tool action, the REST route and the
session Stop cascade; changing its meaning would invalidate the resume-refusal
contract at `worker_thread.py:1524` and the pinned UI contract cited at
`web_ui/backend/server.py:1540-1541` (`is a pinned contract (test_state_transitions.py::`).

### Blast radius

- Files: `tools/workspace/worker_thread.py` (`_action_stop` ~5243-5404, `stop()` 1444,
  `resume()` 1513), `web_ui/backend/bridge.py` (`_stop_session_workers` 1787,
  `request_stop` 1858), `web_ui/backend/workspace_routes.py:1266`, frontend
  `QueryBar.jsx:74` / `WorkerManagementPanel.jsx`.
- Tests: `web_ui/frontend/src/components/__tests__/SessionTab.stop_resume.test.jsx`
  (Stop dispatches `stop_session`), plus the pinned backend test named in
  `web_ui/backend/server.py:1540-1541` (`test_ws_stop_session_status_only`).
- Risk: **medium** — no behaviour change under Option C, but any accidental change to
  the stop flag or the registry pop turns a bounded stop into a leaked daemon thread.

---

## Q2 — Timeout semantics: is the work continued or lost?

### Question as understood

When a worker exceeds its time budget, what exactly is enforced (soft budget, query
wait, hard deadline), is the in-flight work preserved or abandoned, and should the
system automatically retry/continue the timed-out work?

### Current behaviour

There are three nested clocks, and the outermost one wins; a timeout is **terminal**
for the instance and the answer is "re-spawn".

- The soft budget the worker's own time monitor enforces:
  `agent/config/defaults.py:61` — `WORKER_TIMEOUT_SECONDS = 600`
- The resolution chain is documented and code-backed:
  `tools/workspace/worker_thread.py:980-982` —
  `# explicit constructor param > worker definition ("timeout_seconds") >`
  `# agent_config ("worker_timeout_seconds") > session_config`
  and `docs/param_ownership_map.md:88-89` — `WorkerThread` resolves the effective worker timeout with the chain:
  `constructor `timeout_seconds` → `definition.timeout_seconds` →`.
- The soft timeout is *restrictions*, not a kill inside the agent:
  `agent/core/state.py:231` — `self.restriction_reason = 'timeout'`
  and it is explicitly reversible in-turn:
  `agent/core/state.py:247-248` — `# (mirrors the turn/token LOW branches; timeout is NOT a hard stop` /
  `# for the rest of the turn). Do NOT clear restrictions set by`
  with the clearing branch at `agent/core/state.py:252` —
  `if self.restriction_reason == 'timeout':`.
- Blocked tools while in that state:
  `agent/core/tool_executor.py:825` —
  `return f"Tool '{tool_name}' not allowed. Reason: {restriction_reason}. Available tools: {allowed_list}"`.
- The worker loop detects the crossing robustly (three ways):
  `tools/workspace/worker_thread.py:2105-2106` —
  `#   * restriction_reason == 'timeout' (state.py sets this when the` /
  `#     agent's time monitor crosses the timeout threshold), or`
  implemented at `tools/workspace/worker_thread.py:2113` — `if _worker_timeout_detected(`,
  whose first test is `tools/workspace/worker_timeout.py:46` — `if reason == "timeout":`.
- The caller's wait is deliberately longer than the worker's own budget so the
  worker's cooperative envelope can arrive first:
  `agent/config/defaults.py:37` — `QUERY_WAIT_GRACE_SECONDS = 60`
  used by `tools/workspace/worker_thread.py:4763-4765` — `wait = _worker_query_wait_timeout(` /
  `getattr(thread, "_timeout_seconds", None),` / `fallback=300.0,`.
- If that wait still expires, the query path stops the worker, reclaims containers
  and waits for exit — it does not hand the work on:
  `tools/workspace/worker_thread.py:4788` —
  `# Cooperative stop: the query deadline elapsed with no reply. Stop`
  `tools/workspace/worker_thread.py:4805` — `thread._cleanup_worker_containers()`
  `tools/workspace/worker_thread.py:4817` — `clean_exit = self._wait_for_worker_exit(thread)`.
- There is a retry budget on paper only — it is resolved and stored, and never read
  anywhere else:
  `tools/workspace/worker_thread.py:1006` — `_retries = _session_cfg.get("worker_max_retries")`
  then `tools/workspace/worker_thread.py:1009` — `self._worker_max_retries: int = _retries`
  (grep for `_worker_max_retries` across `tools/` and `agent/` returns no other read
  site; the default is `agent/config/defaults.py:64` — `WORKER_MAX_RETRIES = 3`).

### Options

**Option A — keep timeout terminal, delete the dead retry knob.**
Tradeoff: one truthful contract ("timeout ends the instance, re-spawn"), no config
that lies. Cost: loses the chance of a cheap automatic retry for the common
case (a slow LLM call, not a wedged worker).

**Option B — implement retries on the stored budget.**
Tradeoff: `WORKER_MAX_RETRIES = 3` starts meaning something
(`agent/config/defaults.py:64`) and transient overruns self-heal. Cost: retry must
distinguish "timed out but healthy" from `tools/workspace/worker_thread.py:4805`
(containers already reclaimed) — retrying after reclaim is a fresh spawn with a
stale context, i.e. a likely repeat timeout, and it multiplies token spend by up to
4x with no new signal.

**Option C — keep it terminal but make continuation explicit and cheap.**
Tradeoff: preserves the "timeout is final" invariant while giving the caller a
first-class resume path (`_save_context()` at `worker_thread.py:5389` +
`_load_context()` at `:3020`). Cost: the caller (LLM or UI) must make one more call;
the persisted context carries the pre-timeout history, which can re-trigger the same
overrun unless the new instance's budget is raised via `timeout_seconds`.

### Recommendation

**Option A, plus surfacing the retry knob's status.** Keep the terminal semantics
(the cleanup at `worker_thread.py:4805`-`4817` runs before any caller could resume
anything, so "continue in place" is not implementable without new teardown ordering),
and either wire `_worker_max_retries` or remove it. Leaving a resolved-but-unread
setting (`worker_thread.py:1009`) is the actual defect here, not the timeout policy.

### Blast radius

- Files: `tools/workspace/worker_timeout.py`, `tools/workspace/worker_query.py`,
  `tools/workspace/worker_thread.py` (query/join/timeout branches 4701-5090),
  `tools/workspace/worker_thread.py:1002-1009`, `agent/core/state.py:231/252`,
  `agent/core/tool_executor.py:821-825`, `agent/config/defaults.py:37/61/64/95/111-113`.
- Tests: `tests/` (worker/timeout suites) and the strict-envelope contract tests that
  exercise `tools/workspace/worker_query.py`; the join cap
  `tools/workspace/worker_thread.py:5003` — `total = min(requested_timeout, 300)` is
  pinned by the `join`/`wait_for_job` paths.
- Risk: **high** — three clocks, a grace window, a dead config key and a
  reclaim-before-resume ordering all interact; a wrong edit turns a bounded stop into
  either a hung query or a leaked thread.

---

## Q3 — Config propagation: does a write reach a live worker?

### Question as understood

Given the W1.5 addendum write-through (live session + store under one lock), does a
config or addendum change made while a worker is running take effect for that
worker, or does a worker only ever see the config snapshot taken at spawn?

### Current behaviour

A worker sees a **spawn-time snapshot**; write-through reaches the parent's live
session only.

- The worker's agent is built lazily, once per thread, on the first query:
  `tools/workspace/worker_thread.py:2512-2513` — `if self._agent is None:` /
  `agent_cfg = self._build_agent_config()`.
  Nothing re-runs `_build_agent_config()` for later queries in that instance
  (its only other call site builds the parent-side config dict,
  `tools/workspace/worker_thread.py:4244` — `agent_config = self._build_agent_config()`).
- The worker's agent is bound to the worker's own context object, not the parent
  session: `tools/workspace/worker_thread.py:2538` — `session=self._worker_ctx,`
  where `agent/core/worker_context.py:56` — `class WorkerContext:`
  (a worker-scoped context, constructed at `worker_thread.py:2315`).
- The W1.5 write-through mirrors onto live **bridges** and the store, not onto worker
  contexts: `web_ui/backend/addendum_routes.py:227` — `store.save_session(accepted)`,
  with the cold path described at `web_ui/backend/addendum_routes.py:210-211` —
  `Returns ``(result, session)``; ``result`` is ``None`` only on the cold path` /
  `when the session vanished between resolution and the read-modify-write`.
- Had the worker shared the parent session object, the addendum would be picked up
  per LLM call, because it is rendered at call time:
  `agent/core/llm_client.py:225` — `addendum_block = render_addendum_block(getattr(self, 'session', None))`.
- The product already states the snapshot rule for the neighbouring surface
  (worker blueprints): `web_ui/frontend/src/components/WorkerBlueprintPanel.jsx:26-27` —
  `const RESTART_TO_APPLY_HINT =` /
  `'Edits are saved to the blueprint and apply to FUTURE worker spawns — restart a worker to apply them now.'`

### Options

**Option A — declare the snapshot contract (status quo), document it.**
Tradeoff: zero code change, and it matches the blueprint copy already shipped
(`WorkerBlueprintPanel.jsx:27`). Cost: an operator who edits the addendum while a
worker is mid-flight sees no effect and no message explaining it; the
`/api/tools`-style read-only surface gives them no way to tell.

**Option B — per-query re-read of config in the worker loop.**
Tradeoff: predictable "next query sees the new config", and it composes with the
existing lazy build at `worker_thread.py:2512` by making it a rebuild instead of a
one-shot. Cost: `_build_agent_config()` builds a whole `AgentConfig`; rebuilding per
query discards agent state that is deliberately long-lived (tool registry, session
context) and risks the "rebuild silently drops a field" class of bug that
`worker_thread.py:983-996` already warns about for the timeout chain.

**Option C — extend the write-through to worker contexts.**
Tradeoff: one consistent invariant ("every live holder of the session is updated"),
mirroring `addendum_routes.py`'s bridge sweep. Cost: the worker context is not a
session copy (`agent/core/worker_context.py:56`), so this means inventing a
projection from session → worker context plus a cross-thread lock; the addendum is
rendered per call (`llm_client.py:225`) so correctness would then depend on
cross-thread visibility of that projection.

### Recommendation

**Option A.** Document "worker config is fixed at spawn; re-spawn to apply" as the
contract, and make the UI say it (the blueprint panel already does, at
`WorkerBlueprintPanel.jsx:27`). Option C buys correctness for a case the code does
not model, and Option B trades a predictable snapshot for a rebuild that
`worker_thread.py:983-996` shows is easy to get wrong.

### Blast radius

- Files: docs only for Option A; would touch `tools/workspace/worker_thread.py`
  (1663/2512/4244), `agent/core/worker_context.py:56`,
  `web_ui/backend/addendum_routes.py` under B/C.
- Tests: `web_ui/backend/tests/test_addendum_write_through.py`,
  `web_ui/backend/tests/test_addendum_live_chain.py`,
  `web_ui/backend/tests/test_addendum_routes.py`,
  `web_ui/frontend/src/components/__tests__/ConfigPanel.addendum.test.jsx`.
- Risk: **medium** — Option A is documentation and carries near-zero code risk, but
  the *claim* is load-bearing: if anyone later makes `_build_agent_config()` per-query
  without updating this doc, the contract becomes wrong silently.

---

## Q4 — Should tool calls return a job handle instead of blocking?

### Question as understood

Tool calls today block the agent's turn until they return a string. Should a tool be
able to hand back a job handle (like `submit_query` returns a `job_id`) so the turn
can continue and the result be polled later?

### Current behaviour

Tools are synchronous and the executor iterates them serially.

- The base contract is a blocking string return:
  `tools/base.py:41` — `They must implement execute() returning a string.`
- The dispatcher is a plain sequential loop:
  `agent/core/tool_executor.py:245` — `for _tc_index, tool_call in enumerate(tool_calls):`.
- Interruption is only possible *between* calls:
  `agent/core/tool_executor.py:246-247` — `# Cooperative cancellation: if a stop was requested between tool` /
  `# calls, skip execution and record a clean 'stopped' tool result`
  and the check is `getattr(self.config, "stop_check", None)` (called at `:252`).
- The known consequence is documented as an open problem:
  `docs/design-interruptible-tool-calls.md:4` —
  `Currently, a blocking tool call (e.g., `host_bash` running a 10-minute build) cannot be interrupted. The agent's `process_query` generator yields to `execute_tool_calls`, which blocks the thread.`

The async *precedent* already exists, one layer up — for worker queries, not tools:

- `tools/workspace/job_registry.py:4` — `non-blocking worker jobs (action='submit_query').`
  and `tools/workspace/job_registry.py:7` —
  `callers that used ``submit_query`` (which returns immediately with a job_id)`
- Polling is bounded and sliced: `tools/workspace/worker_thread.py:4966` —
  `Polls the job registry in 0.25s slices for a total of ``timeout_seconds```
  with `tools/workspace/worker_thread.py:5003` — `total = min(requested_timeout, 300)`
  and terminals `tools/workspace/worker_thread.py:4972` —
  `_JOB_TERMINAL = ("completed", "timeout", "error")`.
- The registry is bounded: `agent/config/defaults.py:88` — `JOB_REGISTRY_MAX_JOBS = 200`.

No tool currently returns a handle: `NOT FOUND — searched `job_id` and `job_registry`
in `tools/*.py` (only `tools/workspace/*` holds the registry; no tool module imports
it), and `execute()` signatures in `tools/base.py` (string return only).`

### Options

**Option A — keep tools synchronous.**
Tradeoff: the whole turn stays a straight line; the LLM sees results in-order and the
token accounting at `agent/core/tool_executor.py` stays simple. Cost: a long tool
call cannot be interrupted (verbatim at `docs/design-interruptible-tool-calls.md:4`)
and eats the worker's soft budget (`agent/config/defaults.py:61`) with nothing to
show if it times out.

**Option B — thread the tool, keep the string contract (wrapper only).**
Tradeoff: solves *interruption* without changing any tool's signature — exactly the
"Thin Layer" the existing design sketch proposes. Cost: does not give the caller a
handle to poll, so a slow tool still occupies the turn; and it introduces a
tool-thread cancellation protocol that the sketch itself flags as coupled to the
streaming cancellation design.

**Option C — introduce a job handle for long tools.**
Tradeoff: maximal flexibility, and the shape is already proven by
`job_registry.py:7` + `worker_thread.py:4966` (bounded, sliced, preview-capped).
Cost: every consumer of `execute()` must learn a second return type; the agent loop
must learn to finish a turn with a *pending* tool result, which conflicts with the
one-shot append at `agent/core/tool_executor.py:245`'s loop; and the LLM must be
taught a poll convention or it will simply call the tool again.

### Recommendation

**Option A for now; Option B if interruption becomes the requirement.** Do not
introduce a tool job handle: the executor's contract is one sequential pass that
appends one result per `tool_call_id` (`agent/core/tool_executor.py:245`), and a
pending result breaks that invariant far more deeply than it buys. Note explicitly in
any follow-up that the worker-level `submit_query` precedent
(`tools/workspace/job_registry.py:4`) is *not* a template for tools — it moves the
async boundary up to the parent, which is the cheaper lever.

### Blast radius

- Files: `agent/core/tool_executor.py` (dispatch 193-340, restrictions 806-825),
  `tools/base.py:36-75`, `docs/design-interruptible-tool-calls.md`,
  `tools/workspace/job_registry.py` (if reused), `agent/config/defaults.py:88-95`.
- Tests: `tests/` tool-executor suites; the registry's bounded/preview behaviour is
  pinned by `agent/config/defaults.py:88/90/92` values.
- Risk: **high** — touching the dispatch loop touches the stop_check checkpoint,
  the respond/summary tool types and the per-call token accounting in one place.

---

## Q5 — Is there a UI surface for tools?

Interpretation: this asks whether the web UI exposes the tool layer to an operator —
either as a viewer (see which tools exist and why one is gated) or as an authoring
surface (add/edit a tool without touching code). Both halves are answered below,
because the viewer exists and the authoring surface is the part that does not.

### Question as understood

Is there a tools panel/components in the frontend, and is there any way to author a
new tool from the UI?

### Current behaviour

A **read-only** tools list exists; there is no authoring surface and no write route.

- The only tools endpoint is a GET:
  `web_ui/backend/server.py:3951-3952` — `@app.get("/api/tools")` /
  `async def api_get_tools():`
- The frontend consumes it in two places: the config panel fetches on mount
  (`web_ui/frontend/src/components/ConfigPanel.jsx:257` — `fetch(apiUrl('/api/tools'))`)
  and the workspace detail page renders a `Tools` tab
  (`web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:783` —
  `) : activeTab === 'Tools' ? (`).
- The session config panel treats tools as a *selection* of existing tools, not a
  definition: `web_ui/frontend/src/components/ConfigPanel.jsx:56-57` —
  `tools: 'Tools',` / `enabled_tools: 'Tools',`
- Tools themselves are code-defined: the worker side resolves them from a static
  registry, `tools/workspace/worker_thread.py:3389` —
  `def _build_tool_registry() -> dict[str, type["ToolBase"]]:`
- **No create/update/delete route for tools exists.** `NOT FOUND — searched
  `@app.post` in `web_ui/backend/server.py` filtered for `tool` (0 matches) and
  `/api/tool` prefixes across `web_ui/backend/*.py` (only the GET at `:3951`).`
- The nearest *authoring* surface is for worker definitions, not tools:
  `web_ui/backend/workspace_routes.py:840` — `@router.put("/{ws_id}/workers/{name}")`
  with the panel described at
  `web_ui/frontend/src/components/WorkerBlueprintPanel.jsx:5-6` —
  `// Blueprints are the workspace's `workers.json` definitions exposed as` /
  `// normalised `WorkerDefinition` payloads.`

### Options

**Option A — keep it read-only, document "tools are code".**
Tradeoff: no new attack surface, no validation burden, and the listing already
answers the operator question "why is `host_bash` disabled?" (the endpoint carries
`disabled_reason`). Cost: adding a tool remains a code change plus a release.

**Option B — add a tools *editor* over a declarative tool manifest (no code exec).**
Tradeoff: allows parameterised wrappers (e.g. a fixed docker image/command) without a
release, and reuses the blueprint PATCH shape at `workspace_routes.py:840`. Cost: any
manifest that can name a command is a permission-bypass vector against the grain
model in `tools/base.py:50-60` (`session_permissions` / `effective_permissions`), so
it needs its own validation and its own tests before it can ship.

### Recommendation

**Option A.** Keep `/api/tools` read-only and add no authoring route. A tools editor
would have to re-derive the permission grain that `tools/base.py:50-60` injects from
the executor, and nothing in the current surface validates that (there is no write
route to extend — see the NOT FOUND note above). Document the read-only listing as
the supported surface.

### Blast radius

- Files: `web_ui/backend/server.py:3951`, `web_ui/frontend/src/components/ConfigPanel.jsx`
  (257, 654), `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:783`,
  `web_ui/frontend/src/components/workspace/workspaceApi.js`
  (`fetchTools`), `tools/workspace/worker_thread.py:3389`.
- Tests: `web_ui/frontend/src/components/__tests__/ConfigPanel.test.jsx`
  (fetches `/api/tools` on mount), `WorkspaceDetailPage.test.jsx` (Tools tab render +
  error/retry cases), `web_ui/frontend/src/components/__tests__/permissionVocab.test.jsx`.
- Risk: **low** — documentation only; no route, schema or permission change.
