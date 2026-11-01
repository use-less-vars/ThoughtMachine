# Design: Worker Lifecycle (Q1–Q5)

Scope: five questions about worker sub-agent lifecycle — stop semantics, timeout
semantics, config propagation into a live worker, job-handle (async) tool calls, and
the tools UI surface. Every factual claim below carries `path:LINE` plus a verbatim
quote of at most two lines. Where the codebase does not answer a question, the entry
reads `NOT FOUND — searched <patterns>`.

This revision applies three operator rulings: **R1** Stop means *detach* (Q1), **R2**
Docker SDK calls need *hard per-call deadlines* (extends Q2), **R3** the agent needs a
*visual* capability, not merely a read-only tool listing (Q5). Q3 is unchanged; **Q4 is
amended by R3** — the `visual` tool is the one bounded exception to synchronous tools
(operator decision), recorded in Q4 itself.

| # | Question (short) | Recommendation | Risk |
|---|---|---|---|
| Q1 | Does Stop/stop detach a worker or terminate it? | **Stop = detach** (park the loop, save state, keep thread + registry); terminate becomes a separate `kill` verb; queries arriving while detached are **queued FIFO**, never rejected | high |
| Q2 | What happens when a worker exceeds its timeout — is the work continued? | Keep timeout terminal, add a **hard deadline on every blocking Docker SDK call**, and delete the dead retry knob | high |
| Q3 | Does a config/addendum write reach an already-running worker? | Declare spawn-time snapshot as the contract; do not attempt write-through into worker agents | medium |
| Q4 | Should tool calls return a job handle instead of blocking the turn? | No for v1: keep tools synchronous — the default for **every** tool; the `visual` tool (Q5) is the single, bounded exception and carries its own timeout + cancellation protocol; reuse the existing `submit_query`/`job_status` shape if async ever arrives | high |
| Q5 | Is there a UI surface for tools / tool authoring? | Keep authoring rejected; add **one** sandboxed, permission-gated `visual` tool so the agent can *see* | medium |

---

## Premise verification report

| Premise (as stated in the rulings / prior draft) | Verdict | Evidence | Action |
|---|---|---|---|
| `Worker` tool routes `action='stop'` to a terminate path | verified | `tools/workspace/worker_thread.py:3582` — `"stop": lambda: self._action_stop(workers),`; `:5243` — `def _action_stop(self, workers: list) -> dict:` | rewritten (Q1) |
| `_action_stop` blocks on a join, saves context, reclaims containers, pops the registry | verified | `:5362` — `_join_budget = max(30, thread._timeout_seconds)`; `:5389-5390` — `thread._save_context()` / `thread._cleanup_worker_containers()`; `:5398` — `_worker_registry.pop((sid, self.worker_name, iid), None)` | rewritten (Q1) |
| `stop()` is cooperative and writes `command.json` | verified | `:1445` — `"""Signal the worker to stop after completing its current task."""`; `:1468` — `cmd_path.write_text(json.dumps({"action": "stop"}), encoding="utf-8")` | rewritten (Q1) |
| `resume()` refuses a stopped instance | verified | `:1524` — `if self._stop_event.is_set() or self.status in ("stopping", "stopped"):`; `:1521-1522` — `Callers that need a query to run` / `after a stop must spawn a fresh instance instead.` | rewritten (Q1) |
| A pause/detach primitive already exists (event pair + park loop) | verified | `:1123-1124` — `self._pause_event = threading.Event()` / `self._resume_event = threading.Event()`; `:2436` and `:2685` — `while self._pause_event.is_set() and not self._stop_event.is_set():`; `:2438`/`:2687` — `self._resume_event.wait(1.0)` | reused as the detach mechanism (Q1) |
| Session Stop cascades to children | verified | `web_ui/backend/bridge.py:1881` — `workers_stopped = self._stop_session_workers()`; `:1790-1791` — `Worker sub-agents are children of the session, not of the parent's` / `current turn: unless something stops them here they outlive the Stop` | kept as `kill` (Q1) |
| Frontend `QueryBar` sends `stop_session` | verified | `web_ui/frontend/src/components/QueryBar.jsx:74` — `sendCommand('stop_session', {})` | unaffected (Q1) |
| Per-worker stop REST route is fire-and-forget | verified | `web_ui/backend/workspace_routes.py:1266` — `@router.post("/{ws_id}/workers/{name}/stop")`; `:1291` — `registry.  Returns immediately — the worker will transition to ``stopped``` | migrated to detach + new `/kill` (Q1) |
| `test_ws_stop_session_status_only` pins session Stop → `stop_session` | verified | `web_ui/backend/server.py:1540-1541` cites the pinned contract; test defined at `tests/integration/test_state_transitions.py:617` | unaffected if the *session* cascade stays terminate; per-worker state names migrate |
| Main agent can only pass a soft timeout to a worker | verified | `tools/workspace/worker_thread.py:4763-4765` — `wait = _worker_query_wait_timeout(` / `getattr(thread, "_timeout_seconds", None),` / `fallback=300.0,`; `agent/config/defaults.py:37` — `QUERY_WAIT_GRACE_SECONDS = 60` | extended (Q2 / R2) |
| `_wait_for_worker_exit` knowingly returns with the thread still running | verified | `:4660` docstring names `(e.g. stuck inside a` / `DockerCodeRunner call)` and `never Thread.kill`; warning `"Worker '%s' did not stop within %ds budget after query "` | extended (Q2 / R2) |
| `container_manager.exec()` is the only hard-timeout pattern in the Docker layer | verified | `infra/container_manager.py:2561-2570` — `thread.start()` / `thread.join(timeout)` / `container.kill()` / `raise TimeoutError(f"Command timed out after {timeout} seconds")` | generalised (Q2 / R2) |
| Docker SDK calls outside `exec()` have no deadline | verified | `:2498` `self.client.containers.get(container_id)`; `:2533` `container.exec_run(` (mkdir pre-step); `:3531` `container.reload()`; `:4437` `raw = container.logs(` | enumerated + deadline proposed (Q2 / R2) |
| `_worker_max_retries` is resolved and never read | verified | `:1006` — `_retries = _session_cfg.get("worker_max_retries")`; `:1009` — `self._worker_max_retries: int = _retries`; exactly one occurrence repo-wide | deleted (Q2) |
| Old Q2 quote of `agent/core/state.py:247-248` | **drifted** | actual `:247-248` — `self.last_time_warning_state = TimeState.LOW` / `# Time returned to LOW — this monitor clears its own restrictions`; the quoted "mirrors the turn/token LOW branches" text is not there | quote replaced (Q2, Corrections) |
| `agent/core/docker_executor.py` (named in the prior draft) | **false** | file does not exist; docker execution is `tools/docker_code_runner.py` | path corrected (Q2, Corrections) |
| Q1 recommendation "keep stop = terminate, add detach as a separate verb" | **false by ruling (R1)** | operator ruling: Stop *is* detach; Kill is the separate verb | recommendation reversed (Q1) |
| Q5 recommendation "read-only `/api/tools` suffices" | **false by ruling (R3)** | operator wants the *agent* to view, not the operator to list; `/api/tools` is `web_ui/backend/server.py:3951-3952` — `@app.get("/api/tools")` / `async def api_get_tools():` | recommendation replaced (Q5) |
| Tool *authoring* from the UI is correctly rejected | verified (ruling upholds it) | `tools/base.py:50-60` injects `session_permissions` / `effective_permissions` from the executor: `:57` — `effective_permissions: Optional[Dict[str, Any]] = Field(` / `:59` — `description='Effective merged permissions (session × workspace) for atomic checks.',` | retained in Q5 |
| `ImageBlock` exists but has no producer and no provider adapter | verified | `agent/core/ir.py:57` — `class ImageBlock:`; `:67` — `media_type: str  # e.g. "image/png"`; referenced only in `ir.py` + `tests/core/test_ir.py` | prerequisite named (Q5) |
| `ToolResultBlock.content` is a `str` | verified | `agent/core/ir.py:80` — `class ToolResultBlock:` (`content` = "the flattened result the executor already produces") | constrains return shape (Q5) |
| Permission vocabulary is a per-resource allow-list | verified | `security/resource_catalog.py:113-123` — `RESOURCE_CATALOG = {` … `"host_bash": ["banned", "ask", "allow"],`; ranks at `:146` — `GRANT_LEVEL_RANKS: Dict[str, float] = {` | new `visual` resource needed (Q5) |
| Only `host_bash` executes on the host | verified | `docs/ARCHITECTURE_MACHINE_MODEL.md:28` — `**`host_bash` is the one true host-execution grain.** Every other tool either runs in a container or is bounded in-process.` | visual tool ⇒ container (Q5) |

No premise required a STOP: every ruling is implementable against the current code.

---

## Q1 — Stop semantics: terminate or detach?

### Question as understood

When a user or parent agent "stops" a worker (the `Worker` tool's `action='stop'`, the
per-worker REST route, or the session Stop cascade), does the worker survive as a
detached thread that a later query can resume, or is it torn down terminally? Per
Ruling 1 the answer is now **detach**: stop parks the agent loop, saves state, and the
next query resumes where it left off. Termination stays available, but as a *separate*
verb (`kill`, working name).

### Current behaviour

Stop is **terminate**, cooperatively, and it is irreversible for that instance —
i.e. today's `stop` is what Ruling 1 calls `kill`.

- The signal itself is cooperative, not a kill:
  `tools/workspace/worker_thread.py:1445` — `"""Signal the worker to stop after completing its current task."""`
- `stop()` sets the stop event, writes `command.json`, and marks `stopping`:
  `tools/workspace/worker_thread.py:1448` — `self.status = "stopping"`
  `tools/workspace/worker_thread.py:1467` — `cmd_path = self._worker_dir / "command.json"`
- The dispatch table exposes exactly one stop verb:
  `tools/workspace/worker_thread.py:3582` — `"stop": lambda: self._action_stop(workers),`
- The `action='stop'` handler does a bounded join, persists, reclaims containers, and
  always pops the registry:
  `tools/workspace/worker_thread.py:5362` — `_join_budget = max(30, thread._timeout_seconds)`
  `tools/workspace/worker_thread.py:5389-5390` — `thread._save_context()` /
  `thread._cleanup_worker_containers()`
  `tools/workspace/worker_thread.py:5398` — `_worker_registry.pop((sid, self.worker_name, iid), None)`
- A worker that ignores the budget is *still* dropped from the registry:
  `tools/workspace/worker_thread.py:5377-5378` —
  `"Worker '%s' did not stop within %ds budget. "` /
  `"Popping from registry anyway (daemon thread).",`
- Terminal state is written as `stopped`, not `completed`:
  `tools/workspace/worker_thread.py:5394-5395` — `if thread.status != "error":` /
  `thread.status = "stopped"`.
- `resume()` explicitly refuses a stopped instance:
  `tools/workspace/worker_thread.py:1524` —
  `if self._stop_event.is_set() or self.status in ("stopping", "stopped"):`
  and `tools/workspace/worker_thread.py:1521-1522` — `Callers that need a query to run` /
  `after a stop must spawn a fresh instance instead.`
- A *detach* primitive already exists one door down, unused for stop:
  `tools/workspace/worker_thread.py:1123-1124` — `self._pause_event = threading.Event()` /
  `self._resume_event = threading.Event()`
  parked at `tools/workspace/worker_thread.py:2436` — `while self._pause_event.is_set() and not self._stop_event.is_set():`
  woken by `tools/workspace/worker_thread.py:2438` — `self._resume_event.wait(1.0)`
  with status written as `tools/workspace/worker_thread.py:2402` — `self.status = "paused"`
  and cleared back to ready at `tools/workspace/worker_thread.py:2442` —
  `self.status = "ready"`.
- Session Stop is turn-scoped for the parent but kills the children:
  `web_ui/backend/server.py:1535-1536` — `# Stop button: truncate the running turn only — the bridge` /
  `# thread and the session survive (close_session is what`
  then `web_ui/backend/bridge.py:1881` — `workers_stopped = self._stop_session_workers()`
  whose docstring says `web_ui/backend/bridge.py:1790-1791` —
  `Worker sub-agents are children of the session, not of the parent's` /
  `current turn: unless something stops them here they outlive the Stop`
- The frontend simply sends the session command:
  `web_ui/frontend/src/components/QueryBar.jsx:74` — `sendCommand('stop_session', {})`
- The per-worker REST route is fire-and-forget from the caller's point of view:
  `web_ui/backend/workspace_routes.py:1266` — `@router.post("/{ws_id}/workers/{name}/stop")`
  and `web_ui/backend/workspace_routes.py:1291` —
  `registry.  Returns immediately — the worker will transition to ``stopped```

### Every stop consumer, and its disposition under R1

| Consumer | Current effect | Disposition |
|---|---|---|
| `Worker` tool `action='stop'` (`tools/workspace/worker_thread.py:3582` → `_action_stop` `:5243`) | terminate: join, save, reclaim, pop | **repoints to detach**; add `action='kill'` → `_action_kill` |
| `POST /{ws_id}/workers/{name}/stop` (`web_ui/backend/workspace_routes.py:1266`) | enqueues the same stop action | **becomes detach** (URL kept — the user-facing word "stop" now means detach); new `POST /{ws_id}/workers/{name}/kill` |
| `bridge._stop_session_workers()` (`web_ui/backend/bridge.py:1787`, called from `request_stop` `:1881`) | cascades terminate over all session workers | **stays terminate → `kill`** — this is the "no silent background token burn" guard at `bridge.py:1791` |
| `QueryBar.jsx:74` `stop_session` | session-level turn truncation | **unaffected** (it never addressed workers directly) |
| `_action_reset` (`tools/workspace/worker_thread.py:5406`) — "Stop a worker, DROP its persisted context, and free its spawn slot." | terminate + delete `context.json`/`status.json`/`command.json` | **behaviour is preserved only because it must call `_action_kill` explicitly, NOT `_action_stop`** (which now detaches). Routing reset through the repointed `_action_stop` would silently turn a context-dropping reset into a detach — the specific implementation trap R1 creates. |
| `command.json` file channel (`{"action": "stop"}` written at `tools/workspace/worker_thread.py:1467-1468`) | file-watched action; carries whatever `stop` currently means | **inherits the repointed meaning → detach**, not terminate — the same vocabulary change as the tool action it mirrors; `kill` is **not** exposed on this channel in v1 (adding a `kill` verb to `command.json` is explicitly out of scope) |
| `_action_pause` / `_action_resume` (same dispatch table) | park/wake without teardown | **unaffected**; `detach` is implemented *on top of* them (see below) |

### Proposed state machine

```
spawning ──▶ ready ──▶ busy ──▶ ready            (normal turn)
                │        │
                │        ├─▶ detached ──▶ ready   (STOP: park loop, state saved)
                │        │       └────▶ killed    (kill, or detached TTL expiry)
                └────────┴──────▶ killed         (KILL: reclaim, persist, pop)
                                 └──▶ error ──▶ killed
```

- `stopping` / `stopped` (`worker_thread.py:1448`, `:2918`, `:5395`) are **renamed**
  `detaching` / `detached`; a new terminal `killed` is added. `paused` (`:1915`,
  `:2402`, `:2615`) is retained as the internal *mechanism* name and is not exposed as
  a state — the externally visible resting state after a Stop is `detached`.
- **Survives detach:** the thread, the built agent (`worker_thread.py:2512`), the
  `WorkerContext` (`:2315`), the registry entry `(sid, worker_name, iid)`, the persisted
  `context.json` (`:3182`) and `status.json` (`:3137`), and **the worker's containers** —
  so `_cleanup_worker_containers()` (`:2153`) must *not* run on detach.
- **Torn down on kill:** containers reclaimed (`:5390`), context saved but not resumable,
  status `killed` written, registry entry popped (`:5398`), `_stop_event` set.
- `resume()` must accept `detached`: the guard at `worker_thread.py:1524` narrows from
  `("stopping", "stopped")` to `("killed",)`, and the existing wake path
  (`:1546` `self._pause_event.clear()`, `:1547` `self._resume_event.set()`, `:1554`
  `self.status = "ready"`) is reused verbatim. The comment at `:1538` —
  `# This MUST happen before _pause_event.clear(): the pause park loop` — is the
  ordering constraint detach must respect.
- **Chained queries while detached:** the next `query`/`submit_query` on a detached
  worker must clear the park events and then dispatch (detach → resume → query), which
  is what makes "the next query resumes where it left off" true.

### Preserving "no silent background token burn"

`web_ui/backend/bridge.py:1791` — `current turn: unless something stops them here they outlive the Stop`
— names the failure mode detach would otherwise create. Three named enforcements:

1. **Park, not run.** Detach sets `_pause_event` (`worker_thread.py:1123`), so the agent
   loop blocks at `:2436`/`:2685` (`while self._pause_event.is_set() and ...`) and makes
   no LLM calls. A detached worker consumes no tokens by construction.
2. **Session Stop still kills.** `bridge._stop_session_workers()` (`:1787`) continues to
   call the *kill* path, so closing/stopping a session never leaves a fleet of parked
   workers behind (docstring at `:1790-1791` stays true).
3. **Detached TTL.** A detached worker that is never resumed is killed by the same
   budget that already bounds a turn: `agent/config/defaults.py:61` —
   `WORKER_TIMEOUT_SECONDS = 600`, evaluated as
   `now - detached_at > WORKER_TIMEOUT_SECONDS`. **Operator ruling, recorded verbatim:**
   "Use the manager tick. Add a distinct `_detached_worker_ttl` check. Explicitly modify
   `cleanup_stale_worker_containers` to skip containers whose worker status is `detached`.
   Containers survive detach; the reaper must not touch them." The TTL is therefore
   enforced by the worker-manager tick (the one that replaces the registry pop) through a
   distinct `_detached_worker_ttl` check, and
   `cleanup_stale_worker_containers` (`infra/container_manager.py:5104`) must be modified
   to **skip** any container belonging to a worker whose status is `detached` — left
   unmodified it destroys exactly the containers detach exists to preserve.

### Queries submitted while the worker is detached

A query that arrives for a `detached` worker is **accepted and queued, never rejected**.
Detach exists so that the work can continue *later*, so refusing the next query would
make the verb pointless: the caller keeps the existing `submit_query` contract and gets
its `job_id` back at enqueue time — `tools/workspace/job_registry.py:7` —
`callers that used ``submit_query`` (which returns immediately with a job_id)`.

- **Strict FIFO, one queue per worker instance.** Multiple submissions preserve arrival
  order, and the queue is drained head-first, so two callers cannot interleave or be
  reordered by whichever thread happens to wake first.
- **Single in flight, always.** The state machine's invariant is unchanged — never two
  concurrent queries. A queued entry is dispatched only when the worker is otherwise
  idle, one at a time, and each dispatch is `detach → resume → query`: the park events
  are cleared and set first (`worker_thread.py:1546` `self._pause_event.clear()`, `:1547`
  `self._resume_event.set()`) and only then is the query handed to the loop.
- **No token burn while queued.** A queued entry is inert until a resume signal: no
  polling, no periodic wake-ups, no model calls, nothing scheduled on its behalf. The
  only wait involved is the existing park loop — `worker_thread.py:2436-2443` and
  `:2685-2694`, `while self._pause_event.is_set() and not self._stop_event.is_set():` /
  `self._resume_event.wait(1.0)` — which is a once-per-second re-check of two events, not
  work. Enqueueing **must not** set `_resume_event`: setting it is what *starts* a turn,
  so a producer that woke the worker on enqueue would turn the queue into exactly the
  background token spend `web_ui/backend/bridge.py:1791` names as the failure mode.
- **Expiry rejects, loudly.** If the detached TTL elapses before the worker is resumed,
  the worker is killed *and* every queued query is rejected with an explicit error naming
  the TTL — `agent/config/defaults.py:61` — `WORKER_TIMEOUT_SECONDS = 600`, e.g.
  `worker 'X' detached for longer than WORKER_TIMEOUT_SECONDS (600 s) — queued query
  discarded`. Queued work never vanishes silently: no TTL path may drop the queue without
  a job result that says why.
- **Kill discards.** `kill` — and the session Stop cascade, which routes to it — drops the
  queue and rejects the queued queries the same way, because the instance (thread, agent,
  containers) no longer exists to run them.
- **Reconciling with the chained-query rule above.** "The next `query`/`submit_query` on a
  detached worker must clear the park events and then dispatch (detach → resume → query)"
  describes the *first dequeued* entry, not a way around the queue: while a turn is
  already running, later submissions stay queued behind it.

### Options

**Option A — make `stop` detach; add `kill` (Ruling 1).**
Tradeoff: `stop` matches the operator's intent and the button's affordance; in-flight
work is preserved; resume becomes a real operation instead of "spawn a fresh instance"
(`worker_thread.py:1521-1522`). Cost: two verbs, two routes, a new state, and a TTL
reaper; `_action_stop`'s teardown triple (`:5389-5390`, `:5398`) has to be *split* so
detach keeps containers and kill reclaims them.

**Option B — split the verbs the other way: `stop` stays terminate, add `detach`.**
Tradeoff: no change to any existing consumer, and the risky teardown path is untouched.
Cost: contradicts the ruling, keeps the confusing asymmetry where "Stop" destroys work,
and leaves `resume()`'s refusal (`:1524`) as the only resume story.

**Option C — replace Stop with pause and expose no new verb.**
Tradeoff: smallest diff — `_action_pause` already exists and works. Cost: `pause` does
not persist anything, so a process restart still loses the worker; and "pause" does not
communicate "your work is safe" the way "stop" being safe does.

### Recommendation

**Option A.** `stop` = detach; `kill` = the old `_action_stop` body unchanged (join,
`_save_context()`, `_cleanup_worker_containers()`, `status = "killed"`, registry pop).
Detach = `_pause_event.set()` + `_resume_event.clear()` + `_save_context()` +
`_write_status_file()` with `status = "detached"`, and **no** container reclamation.
Rationale: the mechanism already exists and is tested by the park loop
(`worker_thread.py:2436-2443`); only the teardown split, the widened `resume()` guard
(`:1524`) and the TTL are new.

### Blast radius

- Files: `tools/workspace/worker_thread.py` (`stop()` 1444, `_action_stop` 5243-5404,
  `resume()` 1513, dispatch table 3575-3588, park loops 2436/2685, status writes
  1448/2918/5395), `web_ui/backend/workspace_routes.py:1266` (+ `/kill`),
  `web_ui/backend/bridge.py:1787` (cascade → kill),
  `web_ui/frontend/src/components/QueryBar.jsx:74` (unchanged) and the worker panel
  that exposes the per-worker stop button.
- Tests: `tests/integration/test_state_transitions.py:617`
  (`test_ws_stop_session_status_only`) is unaffected *only if* the session cascade stays
  terminate — that is the point of routing it to `kill`;
  `tests/test_bridge_stop_semantics.py`; the frontend
  `web_ui/frontend/src/components/__tests__/SessionTab.stop_resume.test.jsx`; new tests
  for `detached` → `query` → `ready` and for detached-TTL kill.
- Risk: **high** — the registry pop (`:5398`) and container reclaim (`:5390`) are what
  currently prevent leaked daemon threads; detaching means *deliberately* keeping both
  alive, so the TTL reaper becomes load-bearing.

---

## Q2 — Timeout semantics: is the work continued or lost?

### Question as understood

When a worker exceeds its time budget, what exactly is enforced (soft budget, query
wait, hard deadline), is the in-flight work preserved or abandoned, and should the
system automatically retry/continue the timed-out work? Ruling 2 adds the infra half:
the main agent can pass only a *soft* timeout, which does not take effect while a
Docker SDK call is blocking — so which Docker calls can block, and what hard deadline
does each get?

### Current behaviour — the three clocks

There are three nested clocks, and the outermost one wins; a timeout is **terminal**
for the instance and the answer is "re-spawn".

- The soft budget the worker's own time monitor enforces:
  `agent/config/defaults.py:61` — `WORKER_TIMEOUT_SECONDS = 600`
- The resolution chain is documented and code-backed:
  `tools/workspace/worker_thread.py:980-982` —
  `# explicit constructor param > worker definition ("timeout_seconds") >`
  `# agent_config ("worker_timeout_seconds") > session_config`
- The soft timeout is *restrictions*, not a kill inside the agent:
  `agent/core/state.py:231` — `self.restriction_reason = 'timeout'`
  and it is explicitly reversible in-turn:
  `agent/core/state.py:252` — `if self.restriction_reason == 'timeout':`
  (the monitor clears its own restrictions once time returns to LOW —
  `agent/core/state.py:247-248` — `self.last_time_warning_state = TimeState.LOW` /
  `# Time returned to LOW — this monitor clears its own restrictions`).
- Blocked tools while in that state:
  `agent/core/tool_executor.py:825` —
  `return f"Tool '{tool_name}' not allowed. Reason: {restriction_reason}. Available tools: {allowed_list}"`.
- The worker loop detects the crossing robustly (three ways):
  `tools/workspace/worker_thread.py:2105-2106` —
  `#   * restriction_reason == 'timeout' (state.py sets this when the` /
  `#     agent's time monitor crosses the timeout threshold), or`
  implemented at `tools/workspace/worker_thread.py:2113` — `if _worker_timeout_detected(`,
  whose first test is `tools/workspace/worker_timeout.py:46` — `if reason == "timeout":`.
- The caller's wait is deliberately longer than the worker's own budget:
  `agent/config/defaults.py:37` — `QUERY_WAIT_GRACE_SECONDS = 60`
  used by `tools/workspace/worker_thread.py:4763-4765` — `wait = _worker_query_wait_timeout(` /
  `getattr(thread, "_timeout_seconds", None),` / `fallback=300.0,`.
- If that wait still expires, the query path stops the worker, reclaims containers
  and waits for exit — it does not hand the work on:
  `tools/workspace/worker_thread.py:4788` —
  `# Cooperative stop: the query deadline elapsed with no reply. Stop`
  `tools/workspace/worker_thread.py:4805` — `thread._cleanup_worker_containers()`
  `tools/workspace/worker_thread.py:4817` — `clean_exit = self._wait_for_worker_exit(thread)`.
- That final wait is explicitly **not** a kill and can give up with the thread alive:
  `tools/workspace/worker_thread.py:4660` — `def _wait_for_worker_exit(self, thread) -> bool:`
  whose docstring names the cause, `(e.g. stuck inside a` / `DockerCodeRunner call)`, and
  whose warning reads `"Worker '%s' did not stop within %ds budget after query "` /
  `"(daemon thread)"`.
- There is a retry budget on paper only — it is resolved and stored, and never read
  anywhere else:
  `tools/workspace/worker_thread.py:1006` — `_retries = _session_cfg.get("worker_max_retries")`
  then `tools/workspace/worker_thread.py:1009` — `self._worker_max_retries: int = _retries`
  (grep for `_worker_max_retries` repo-wide returns no other read site; the default is
  `agent/config/defaults.py:64` — `WORKER_MAX_RETRIES = 3`).

### Current behaviour — where a Docker SDK call can block (Ruling 2)

The SDK is synchronous and most call sites have **no** deadline: the only hard-timeout
pattern in the layer is `exec()`'s thread + `join(timeout)` + `kill()`.

- The one correct pattern, in `infra/container_manager.py:2561-2570` —
  `thread.start()` / `thread.join(timeout)` / `container.kill()` /
  `raise TimeoutError(f"Command timed out after {timeout} seconds")`
  under `:2496` — `def exec(self, container_id, command, timeout=30, workdir="/workspace", environment=None):`
- Its own pre-step is *not* covered: `:2533` — `container.exec_run(` (the `mkdir -p`
  guard) has no deadline, so a wedged daemon blocks before the timer starts.
- The soft variant abandons rather than kills: `:3907` —
  `def _exec_checked(self, container, command, timeout=10):` with docstring
  `Best-effort introspection helper: NEVER raises and NEVER kills the` /
  `container.` and `:3927` — `thread.join(timeout)` → returns `None` and leaks the thread.
- The exec-path quota guard blocks twice without a deadline: `:3531` —
  `container.reload()` and `:3535` — `container.exec_run(`.

| Blocking site | Call | Current behaviour | Proposed hard deadline |
|---|---|---|---|
| `infra/container_manager.py:2498` | `self.client.containers.get(container_id)` | unbounded | 10 s |
| `infra/container_manager.py:2533` | `container.exec_run(` (mkdir pre-step) | unbounded | 10 s, abandon thread |
| `infra/container_manager.py:2555` | `container.exec_run(**exec_kwargs)` | bounded by caller `timeout=30`, kills the container | keep — the reference pattern |
| `infra/container_manager.py:3531/3535` | `container.reload()` / quota `exec_run` | unbounded, on the exec hot path | 10 s, soft (abandon, treat as "not over quota") |
| `infra/container_manager.py:3918` | `_exec_checked` `exec_run` | soft 10 s, never kills | keep soft (introspection must not kill) |
| `infra/container_manager.py:4424/4437` | `containers.get` / `container.logs(` | unbounded | 30 s |
| `infra/container_manager.py:1820/1841`, `:2468` | `start()` → `get`, `record.attach(...)` | unbounded (attach opens the log stream) | 30 s start, 10 s attach |
| `infra/container_manager.py:3558`, `:3611/3614` | `stop()`/`_stop_container` → `get`, `.stop(`, `.kill(` | unbounded | 15 s per call |
| `infra/container_manager.py:3637/3672`, `:3717/3775`, `:3809`, `:3837` | `restart`, `remove`, `status`, `stats` | unbounded | 15 s (remove: 60 s — layering) |
| `infra/container_manager.py:4090/4602/5328/5532/5782` | `containers.list(` in `list_containers`, `_find_by_labels`, `sweep_exited…`, `_emit_summary`, `reap_container_record` | unbounded | 30 s |
| `infra/container_manager.py:5042-5084`, `:5125/5174`, `:4933/4937` | `cleanup_workspace`, `cleanup_stale_worker_containers`, `_remove_container` (`stop`/`remove`) | unbounded | 30 s per call + overall sweep budget |
| `infra/container_manager.py:795` | `client.images.get(image)` | unbounded | 15 s |
| `infra/container_manager.py:4324` | `images` build (`build_image`) | unbounded | 600 s (builds are legitimately slow; not on the query path) |
| `infra/container_create.py:426` | `client.containers.run(` (nested `def _run()` at `:425`) | unbounded — image pull + create + log attach | 300 s |
| `infra/container_create.py:459` | `handle.attach(container, state=STATE_RUNNING)` | unbounded | 10 s |
| `infra/resource_container_manager.py:1507` | `exec_run` (untimed pre-step) | unbounded | 10 s |
| `infra/resource_container_manager.py:1548-1563` | `_run()` + `join(timeout)` + `kill` | already hard-bounded | keep |
| `infra/resource_container_manager.py:1597/1635`, `:1655/1692/1696` | `stop()` / `remove()` | unbounded | 15 s / 60 s |
| `infra/container_registry.py:595/724/1213` | `.attach(` | unbounded | 10 s |
| `infra/container_registry.py:603/728/804/856` | `get(` | unbounded | 10 s |
| `infra/container_registry.py:811/872`, `:812/820/873` | `.stop(` / `.remove(` | unbounded | 15 s / 60 s |
| `infra/container_lifecycle.py:145/153` | `container.stop(timeout=…)` / `.remove(force=…)` | bounded by `intent.timeout or 5`, but wrapped in a bare `except Exception` | keep, tighten to a wall-clock join |

**Mechanism.** `signal.alarm` is not usable: workers are threads inside the server
process (Q1 / `docs/ARCHITECTURE_MACHINE_MODEL.md` — workers are "Host process, **as
threads in the server**"), and `SIGALRM` is delivered to the main thread only. The house
pattern already in the file is the right one — spawn a daemon thread, `join(timeout)`,
and on timeout mark the resource dead. Adopt one helper and route every site above
through it:

```python
def _call_with_deadline(fn, timeout, *, on_timeout="abandon", container=None):
    """Run a blocking SDK call with a hard wall-clock deadline.

    Returns (result, None) or (None, TimeoutError). 'abandon' leaks the daemon
    thread (introspection); 'kill' additionally kills the container (exec).
    on_timeout='kill' requires `container`; without it this raises ValueError.
    """
```

`concurrent.futures.ThreadPoolExecutor(...).result(timeout=...)` is equivalent (a
Python thread cannot be cancelled either way) and is acceptable, but it adds an
executor lifecycle per call; the inline thread keeps parity with
`infra/container_manager.py:2553-2570`.

The race is named rather than hidden: `container.kill()` is issued from the *caller*
thread the moment `join(timeout)` expires, while the abandoned daemon thread may still be
inside the SDK call — so the kill can land mid-call. That is harmless precisely because
the container dies: the still-blocked thread's eventual completion or failure operates on
a resource nobody will read, which is why the handle must be a parameter of the helper
rather than something it discovers itself. An `abandon` deadline, by contrast, leaks a
daemon thread that outlives the query; if the detached TTL reaper then kills the worker
and its containers, that leaked thread is operating on a dead container, so it must
swallow its own exception rather than propagate it into a worker that no longer exists.

**Per-call vs per-worker.** The hard deadline is a *per-call* primitive; the per-worker
budget stays soft. Composition rule:

```
worker wall clock  ≤  SOFT_BUDGET (WORKER_TIMEOUT_SECONDS = 600)
single docker call ≤  min(call_class_deadline, remaining worker wall clock)
```

so a call can never consume more than the turn has left, and the worker always gets to
emit its own timeout envelope.

**Interaction with `WORKER_TIMEOUT_SECONDS=600` / `QUERY_WAIT_GRACE_SECONDS=60`.**
The hard deadline **preempts** the individual call; it neither extends nor shortens
either clock:

- Call returns `TimeoutError` inside the budget → the tool returns an error string →
  the agent loop continues → the worker finishes normally with its own envelope. The
  600 s soft budget is untouched.
- Every blocking call is now bounded (max 300 s for `containers.run`), so the caller's
  wait (`timeout_seconds + 60`, `worker_thread.py:4763-4765`) is no longer raced by an
  unbounded SDK call. The path at `:4817` → `_wait_for_worker_exit` → "returning
  envelope with thread still running (daemon thread)" becomes the rare case instead of
  the designed-in one.
- The hard deadline does **not** kill the worker; the worker's own envelope
  (`:2917-2918`) remains the only thing that sets `stopped`.
- `container.kill()` at `infra/container_manager.py:2566` remains the only *site* at
  which a deadline kills a container; what changes is that it gains a second caller — the
  exec path and, per the Q4 amendment, the Q5 render deadline
  (`RENDER_TIMEOUT_SECONDS = 30`) both reach it. The mechanism and the blast radius are
  unchanged; the number of callers is not.

### Options

**Option A — keep timeout terminal, add hard call deadlines, delete the dead knob.**
Tradeoff: one truthful contract ("timeout ends the instance, re-spawn"), no config that
lies, and unbounded SDK calls stop being the reason a turn outlives its budget. Cost:
a helper threaded through ~25 call sites, and every "abandon" deadline leaks a daemon
thread until the SDK returns.

**Option B — implement retries on the stored budget.**
Tradeoff: `WORKER_MAX_RETRIES = 3` starts meaning something
(`agent/config/defaults.py:64`) and transient overruns self-heal. Cost: retry must
distinguish "timed out but healthy" from `tools/workspace/worker_thread.py:4805`
(containers already reclaimed) — retrying after reclaim is a fresh spawn with a stale
context, i.e. a likely repeat timeout, and it multiplies token spend by up to 4x with no
new signal. Hard call deadlines also remove the main *wedge* cause, so retries would
mostly fire on genuinely slow work.

**Option C — keep it terminal but make continuation explicit and cheap.**
Tradeoff: preserves "timeout is final" while giving the caller a first-class resume path
(`_save_context()` at `worker_thread.py:5389` + `_load_context()` at `:3020`). Cost: the
caller makes one more call; the persisted context carries the pre-timeout history, which
can re-trigger the same overrun unless `timeout_seconds` is raised. Note this becomes
strictly cheaper once Q1 lands, because Stop = detach already gives a resumable resting
state.

### Recommendation

**Option A.** Keep terminal timeout semantics (the cleanup at
`worker_thread.py:4805`-`4817` runs before any caller could resume anything, so
"continue in place" is not implementable without new teardown ordering), generalise the
`exec()` deadline pattern to every blocking Docker SDK call in the table above, and
**delete `_worker_max_retries`** (`worker_thread.py:1009`) together with the
`worker_max_retries` resolution at `:1006` and `WORKER_MAX_RETRIES`
(`agent/config/defaults.py:64`) — a knob that is resolved, stored and never read is the
actual defect here, and wiring it now would add a 4x token multiplier to a failure mode
that hard deadlines have just made rarer. Continuation, if it is ever wanted, is Q1's
detach/resume pair (Option C above), not a retry loop.

### Blast radius

- Files: `infra/container_manager.py` (~25 sites listed above),
  `infra/container_create.py:425-462`, `infra/resource_container_manager.py:1503-1563`,
  `infra/container_registry.py:595-873`, `infra/container_lifecycle.py:133-153`,
  `tools/workspace/worker_timeout.py`, `tools/workspace/worker_thread.py`
  (query/join/timeout branches 4701-5090, `_wait_for_worker_exit` 4660, retry chain
  1002-1009), `agent/core/state.py:231/252`, `agent/core/tool_executor.py:821-825`,
  `agent/config/defaults.py:37/61/64`.
- Tests: `tests/` (worker/timeout suites), the strict-envelope contract tests around
  `tools/workspace/worker_query.py`, `tests/docker/` + `tests/docker_integration/` for
  the SDK deadlines, and the join cap `tools/workspace/worker_thread.py:5003` —
  `total = min(requested_timeout, 300)` which the `join`/`wait_for_job` paths pin.
- Risk: **high** — three clocks, a grace window, ~25 new deadlines and a
  reclaim-before-resume ordering all interact; a wrong deadline either abandons a live
  SDK thread (leak) or kills a container mid-write (data loss).

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
This is the default for **every** tool. The `visual` tool (Q5) is the single named,
bounded exception, and it is the exception *because* it ships a dedicated timeout and
cancellation protocol — see the amendment below.

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

The interruption requirement for one tool has now arrived, and it is discharged by the
bounded exception in the amendment below — a call-site implementation inside a single tool
(thread + `join` + `container.kill()`), **not** Option B. Option B remains unrecommended
and its tool-thread cancellation protocol is not adopted.

Exactly one tool is excepted, and by operator decision rather than by this reasoning:
the `visual` tool (Q5), the single bounded exception with its own timeout and
cancellation protocol (amendment below).

### Amendment — the visual tool is the one bounded exception (operator decision)

**Operator decision, recorded verbatim:** "Tools are synchronous by default, with the
visual tool (Q5) being the specific, bounded exception requiring a dedicated timeout and
cancellation protocol."

This bounds the question rather than re-opening it. Option A stays the **default for all
tools**: nothing in the `execute() -> str` contract (`tools/base.py:41` — `They must
implement execute() returning a string.`) or in the sequential dispatch loop
(`agent/core/tool_executor.py:245`) changes for the existing toolset. Exactly one tool is
exempt — the container-executed `visual` tool specified in Q5
(`tools/visual_tool.py`, grain `visual: view`). The exception is not "async tools"; it is
one named tool whose call is long, external and cancellable.

Because the exception is bounded, it must carry its own budget and its own cancellation
path instead of inheriting the turn's:

- **Dedicated timeout.** **Operator ruling, recorded verbatim:** "Define
  `RENDER_TIMEOUT_SECONDS = 30` in the design doc. A 30-second hard cap is sufficient for
  a headless browser screenshot." `RENDER_TIMEOUT_SECONDS = 30` belongs in
  `agent/config/defaults.py` next to `WORKER_TIMEOUT_SECONDS` (`:61`); the render's
  deadline is *that* value, not the worker's soft budget, enforced in the container layer
  with the pattern R2 already standardises for Q2 — thread + `join(timeout)` +
  `container.kill()` at `infra/container_manager.py:2561-2570` (`raise TimeoutError(f"Command
  timed out after {timeout} seconds")`). A render that overruns is killed and returned as
  an error string, never as a pending result.
- **Dedicated cancellation.** The render runs as a container exec on its own thread and
  the tool waits for it in a bounded loop: `join(min(CHUNK_SECONDS, remaining))` per
  iteration (`CHUNK_SECONDS ≈ 0.5` s), calling `getattr(self.config, "stop_check", None)`
  at each wake-up and issuing `container.kill()` against the render container when it
  returns true. `agent/core/tool_executor.py:252` is the *hook name and existing call
  site* this mirrors — it is deliberately **not** the render's call site, and that
  citation is no longer load-bearing for this design. The between-calls-only checkpoint
  at `:246-247` (`# Cooperative cancellation: if a stop was requested between tool` /
  `# calls, skip execution and record a clean 'stopped' tool result`) cannot reach inside
  a render, which is exactly why the bounded loop exists. Cancelling kills the render
  container (ephemeral per Q5, so nothing leaks) and returns a `stopped` result in the
  established shape.
- **Everything else stays Option A.** No job handle, no poll convention, no pending tool
  result in the transcript: the one exception returns a value or an error,
  synchronously, under a deadline. Q5 owns the rest of the tool's contract — grain
  (`visual: view`), one PNG `ImageBlock` capped at 1 MB, and the IR/provider
  prerequisites.

The alternative — treating the visual tool as the thin end of a general async-tool wedge —
is what this amendment rules out. One named exception with a timeout and a cancellation
protocol is reviewable; a second return type on `execute()` is not.

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

### Question as understood

The prior draft answered two halves: an operator *viewer* (does the UI list tools and
explain why one is gated?) and an operator *authoring* surface. Ruling 3 keeps the
authoring rejection and replaces the rest: the missing surface is **the agent's own
vision** — a sandboxed, permission-gated capability that renders something and returns
visual data to the multimodal model. `/api/tools` listing tools is not that.

### Current behaviour

**(a) The tool listing exists and is read-only.** `web_ui/backend/server.py:3951-3952` —
`@app.get("/api/tools")` / `async def api_get_tools():`, consumed at
`web_ui/frontend/src/components/ConfigPanel.jsx:257` — `fetch(apiUrl('/api/tools'))`
and rendered as a tab at
`web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:783` —
`) : activeTab === 'Tools' ? (`. Tools are a *selection*
(`web_ui/frontend/src/components/ConfigPanel.jsx:56-57` — `tools: 'Tools',` /
`enabled_tools: 'Tools',`), never a definition.

**(b) Authoring is (correctly) not a UI concern.** Tools are code-defined per module —
`tools/workspace/worker_thread.py:3389` — `def _build_tool_registry() -> dict[str, type["ToolBase"]]:`
— and each tool receives its permissions *injected by the executor*:
`tools/base.py:50-53` — `session_permissions: Optional[Dict[str, Any]] = Field(` /
`default=None,` / `description='Session permissions dict injected by ToolExecutor.',` /
`)` and `tools/base.py:57-60` — `effective_permissions: Optional[Dict[str, Any]] = Field(` /
`default=None,` / `description='Effective merged permissions (session × workspace) for atomic checks.',` /
`)`. No create/update/delete route exists: `NOT FOUND — searched @app.post in
web_ui/backend/server.py filtered for tool (0 matches) and /api/tool prefixes across
web_ui/backend/*.py (only the GET at :3951).`

**(c) The agent has no way to see anything.** The IR already models an image —
`agent/core/ir.py:57` — `class ImageBlock:` with `:67` — `media_type: str  # e.g. "image/png"`
— but it has **no producer and no provider adapter**: `ImageBlock` appears only in
`agent/core/ir.py` and `tests/core/test_ir.py`, and `llm_providers/` (base, anthropic,
openai_compatible, factory, tool_converter) contains no image/vision handling. The
tool boundary is a string: `tools/base.py:41` — `They must implement execute() returning a string.`
and `agent/core/ir.py:80` — `class ToolResultBlock:` whose `content` is the flattened
`str`. A headless browser exists only as test infrastructure
(`requirements-dev.txt:8` — `playwright>=1.40`, plus `/workspace/chrome-libs/`), never as
a product capability.

### Proposed design — one tool, one grain, one format, one cap, four named outcomes

**The tool.** `visual` (`tools/visual_tool.py`, registered in
`tools/workspace/worker_thread.py:3389`), one action, two parameters:
`target` (a `file:///workspace/...` path or an `http(s)://` URL) and `full_page: bool`.
It renders in a **container** and returns a PNG. The tool enforces
`RENDER_TIMEOUT_SECONDS = 30` per call and honours `stop_check` during the render — the
Q4 amendment's dedicated timeout and cancellation protocol, restated here as the tool's
own contract.

**Execution class: Container, ephemeral.** Per
`docs/ARCHITECTURE_MACHINE_MODEL.md:28` — `**`host_bash` is the one true host-execution
grain.** Every other tool either runs in a container or is bounded in-process.` A
browser is neither bounded nor in-process, so it is a container, and per §3 it is
**ephemeral** — "created for one call with exactly the envelope that call needs, then
destroyed", which is exactly the isolation a renderer needs. Declaration mirrors
`tools/docker_code_runner.py:61` — `required_categories: ClassVar[List[str]] = ["filesystem:write", "container:true"]`
→ `["visual:view", "container:true"]`.

**Container lifecycle and detach interaction.** The container is created per call with
exactly that call's envelope — image, mounts, `network`, timeouts — and destroyed in a
`finally`, so it is reclaimed on success, on timeout, on cancellation and on exception
alike; no path leaves a render container behind. It is never reclaimed by
`_cleanup_worker_containers()` (`worker_thread.py:2153`), which detach must not run at all
(Q1). Detach mid-render does **not** interrupt an in-flight render: the park loop only
takes effect between iterations (`worker_thread.py:2436-2443`, `:2685-2694`), and the
render is bounded by `RENDER_TIMEOUT_SECONDS = 30`, so a detach issued during a render
takes effect at the next iteration boundary after at most that long. This is the design,
not a derived consequence: a render is allowed to finish its bounded call, and detach is a
park, not a preemption.

**Permission grain: one new resource `visual`, values `["banned", "ask", "view"]`.**
Three values, not four: `interact` (click/type/scroll) makes the browser a *mutating*
agent against third-party state and would need its own threat model, its own tests and
its own rate limits — out of scope, deliberately. `view` ranks at the read tier.
Implementation points: `security/resource_catalog.py:113-123` —
`RESOURCE_CATALOG = {` … `"host_bash": ["banned", "ask", "allow"],` (add
`"visual": ["banned", "ask", "view"]`), the rank table at `:146` —
`GRANT_LEVEL_RANKS: Dict[str, float] = {` (add `"view": 1`), the catalog entry in
`agent/config/resource_catalog.json` (per-resource `permission_grain_set`,
`execution_mode: "container"`, `tools: ["visual"]`, `ui_category: "visual"`), and the
session default `agent/config/defaults.py:262-269` —
`DEFAULT_SESSION_PERMISSIONS = {` (add `"visual": "banned"`).

**Return shape: four outcomes — one `ImageBlock`, a capped-text envelope, an error string, or `stopped`.**
`source` is a base64 string and `media_type` is pinned to `"image/png"`
(`agent/core/ir.py:57/67`). One format keeps the provider matrix trivial — Anthropic
accepts `image/png|jpeg|gif|webp` and OpenAI-compatible endpoints accept
`data:image/png;base64,` URLs, so **PNG is the intersection and is the only format the
tool emits**. The *mechanism* reuses the existing preview vocabulary rather than
inventing a second one — the `_truncate_output` envelope shape (`tools/base.py:312`)
described below. The *cap* cannot: `PREVIEW_CAP = 8000`
(`agent/config/defaults.py:90`) is a thumbnail, not an image limit, so one new constant is
added next to it —
`IMAGE_CAP_BYTES = 1_000_000` — and on overflow return the existing
`_truncate_output` shape (`tools/base.py:312` — `def _truncate_output(self, output: str, limit: Optional[int] = None) -> str:`)
as a text envelope (`{"path": ..., "media_type": "image/png", "bytes": N, "truncated": true}`),
i.e. the image is still on disk in the workspace for a later read. Both providers cap
around 5 MB per image, so 1 MB is safely inside.

Four outcomes, named so that none is implied: (a) one `ImageBlock` — PNG, inline base64 —
on success; (b) the JSON **text** envelope above on size overflow, i.e. the existing
`_truncate_output` shape (`tools/base.py:312`) rendered as
`{"path", "media_type", "bytes", "truncated": true}`; (c) an actionable **error
string** on early refusal — bad target, permission denied, image not allow-listed; (d) a
`stopped` result on cancellation, in the established stopped-tool-result shape.

**Invocation surface: the agent, not a new route.** No REST endpoint, no UI authoring
surface, no frontend tab. The Tools tab already lists tools
(`WorkspaceDetailPage.jsx:783`), so a new tool appears there for free; an artifact
viewer is explicitly out of scope.

**Interaction with `network: banned`.** Rendering a URL needs egress, so `visual:view`
must **not** imply it: the container gets exactly the session's network envelope
(`agent/config/defaults.py:264` — `"network": "banned",`). With `network: banned` the
tool accepts only `file:///workspace/...` targets (screenshot a generated HTML/report
artifact). The claim this replaces — that with `network` at `write`/`outbound` the tool
accepts `http(s)://` — is withdrawn: `banned` is the only network value this document can
evidence (`agent/config/defaults.py:264` — `"network": "banned",`), and a
`write`/`outbound` level is `NOT FOUND — searched defaults.py/resource_catalog for network
level names`. Consequence: in v1 `visual` renders `file:///workspace/...` only, and
`http(s)://` is gated on that unverified level — out of scope for v1. Enforcement lives in
the container profile, not in the tool's string handling — the tool refuses early with an
actionable error so the model does not retry blindly.

**Prerequisite, stated because it is the real cost:** `ImageBlock` has no producer and
no adapter today, so this design requires (1) `ToolResultBlock` to carry image parts
(today `content` is a `str`, `ir.py:80`), (2) an adapter in
`llm_providers/anthropic_provider.py` and `llm_providers/openai_compatible.py`, and
(3) an image base in an allow-listed image — `agent/config/defaults.py:159` —
`ADMISSION_IMAGE_ALLOWLIST = (DEFAULT_IMAGE, RESOURCE_IMAGE_TAG)` with `:135` —
`DEFAULT_IMAGE = "agent-executor"`. There is **no Dockerfile in this repository**
(`NOT FOUND — searched *Dockerfile* excluding worktrees and node_modules (0 files)`),
so the browser image is an operator-owned build artifact, and the tool must not name
an image outside the allow-list.

**[NEEDS MAIN RULING]** This requires a decision on whether to create a dedicated
`browser` resource image or extend `RESOURCE_IMAGE_TAG`.

### Options

**Option A — keep the read-only listing; the agent stays blind.**
Tradeoff: zero new attack surface and no provider work. Cost: rejects the ruling; the
agent cannot verify a rendered report, a UI change or a chart it produced, which is
exactly the gap the operator named.

**Option B — one `visual` tool with grain `[banned, ask, view]` and one PNG
`ImageBlock` return (recommended).**
Tradeoff: bounded — one tool, one grain, one format, one cap — and it composes with the
existing ephemeral-container model ("correct *by construction*"). Cost: a
cross-cutting change (IR + two provider adapters + resource catalog + a browser base
image), and every render is a container create+destroy, so it is not cheap per call.

**Option C — a full browser-control tool with `interact`.** Tradeoff: covers
click/type/scroll flows and screenshots-after-action. Cost: makes the agent a mutating
client of third-party state, needs its own permission grain, rate limits, credential
handling and a much larger test surface; premature for a first visual capability.

### Recommendation

**Option B.** One `visual` tool, container-executed, gated by
`visual: view` (rank = read tier), returning a single inline PNG `ImageBlock` capped at
1 MB. Keep the authoring surface rejected on the grounds already established
(`tools/base.py:50-60` injects the permission grain from the executor; nothing in the UI
can validate it). Ship the IR/provider half as the same change, because an
`ImageBlock` the model cannot receive is a wasted tool.

### Blast radius

- Files: `tools/visual_tool.py` (new), `tools/workspace/worker_thread.py:3389`
  (registry), `tools/base.py:312` (cap helper reuse), `agent/core/ir.py:57-80/124/251`
  (`ToolResultBlock` image parts), `llm_providers/anthropic_provider.py`,
  `llm_providers/openai_compatible.py`, `security/resource_catalog.py:113/146`,
  `agent/config/resource_catalog.json`, `agent/config/defaults.py:135/159/262` plus a
  new `IMAGE_CAP_BYTES`.
- Tests: `tests/core/test_ir.py:41/135` (the only current `ImageBlock` coverage),
  permission-vocabulary tests around `security/resource_catalog.py`, frontend
  `web_ui/frontend/src/components/__tests__/permissionVocab.test.jsx`, and the
  `defaults` pins in `tests/test_worker_lifecycle_trust.py:422`
  (`assert defaults.PREVIEW_CAP == 8000`).
- Risk: **medium** — the tool itself is small and container-isolated, but the change
  reaches the IR and two provider adapters, and a mis-set size cap turns a screenshot
  into a context-window blowout rather than an error.

---

## Corrections

1. **Q1 recommendation reversed.** The prior draft recommended keeping `stop` as
   terminate and adding `detach` as a separate verb. Ruling 1 inverts the pair:
   `stop` = detach, `kill` = terminate. Q1's "Current behaviour" section is retained
   (it is accurate — it now describes `kill`) and the recommendation is rewritten.
2. **Q1 file list named an unverified path.** The prior draft listed
   `WorkerManagementPanel.jsx` as a stop consumer. That path was **not** re-verified in
   this pass; the verified consumers are the four enumerated in Q1's disposition table
   (`worker_thread.py:3582`, `workspace_routes.py:1266`, `bridge.py:1787`,
   `QueryBar.jsx:74`).
3. **Q2 quote drift fixed.** The prior draft quoted
   `agent/core/state.py:247-248` as `# (mirrors the turn/token LOW branches; timeout is
   NOT a hard stop` / `# for the rest of the turn). Do NOT clear restrictions set by`.
   The actual lines are `self.last_time_warning_state = TimeState.LOW` /
   `# Time returned to LOW — this monitor clears its own restrictions`. Q2 now quotes
   the real text; the `:252` claim (`if self.restriction_reason == 'timeout':`) still
   holds.
4. **Dead path corrected.** The prior draft referenced `agent/core/docker_executor.py`;
   that file does not exist. Docker execution is `tools/docker_code_runner.py`
   (`tools/docker_code_runner.py:61` — `required_categories: ClassVar[List[str]] = ["filesystem:write", "container:true"]`).
   Q2 now cites the real module.
5. **Q2 extended, not re-decided.** The recommendation stays "timeout is terminal"
   (Option A), now with (a) a hard deadline on every blocking Docker SDK call, (b) the
   `exec()` thread+`join(timeout)`+`kill()` pattern at
   `infra/container_manager.py:2561-2570` promoted to the layer's one mechanism, and
   (c) an explicit rule on `_worker_max_retries` (`worker_thread.py:1009`): delete,
   together with `:1006` and `agent/config/defaults.py:64`.
6. **Q5 "read-only `/api/tools` suffices" was wrong.** Replaced by the `visual` tool
   design. The *authoring* rejection stands and is now justified from
   `tools/base.py:50-60` rather than from the absence of a route.
7. **`signal.alarm` ruled out.** Workers are threads in the server process
   (`docs/ARCHITECTURE_MACHINE_MODEL.md` — "Host process, **as threads in the
   server**"), and `SIGALRM` is delivered to the main thread only; hard deadlines use
   the thread + `join(timeout)` pattern instead. This is recorded here so a later
   implementer does not reach for signals first.
8. **Image capability accounting.** `ImageBlock` (`agent/core/ir.py:57`) is modelled but
   unproduced and unadapted; `ToolResultBlock.content` is a `str` (`ir.py:80`). Any
   visual design must change both, which is why Q5 lists them as prerequisites rather
   than as assumptions.
9. **Q4 amended, not left "unchanged".** The prior revision of this document asserted
   "Q3 and Q4 are unchanged" in its header while Q5 introduced a container-executed
   `visual` tool — i.e. it recommended synchronous tools in Q4 and a long, cancellable
   tool in Q5. A document asserting two contradictory recommendations is a bug, not a
   nuance: a reader cannot tell whether tools block. The operator decision closes it
   verbatim — "Tools are synchronous by default, with the visual tool (Q5) being the
   specific, bounded exception requiring a dedicated timeout and cancellation protocol."
   Q4's Option A therefore stays *the default for all tools*, the `visual` tool is named
   as the single bounded exception, and that exception is specified as a dedicated
   timeout (container-layer hard deadline) plus a dedicated cancellation path
   (`stop_check` honoured inside the render). The header claim and the summary-table Q4
   row are corrected to match, for traceability. The same commit also corrects the Q1 row of
   that summary table and adds the detached-queue subsection
   (`### Queries submitted while the worker is detached`, 38 lines); both belong to this
   amendment batch, not to the revision being corrected.
