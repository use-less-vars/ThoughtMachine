# Design: workers as things — definitions, instances, dispatches, jobs

**Status:** decision record, open for review. Dated 2026-11-26. Branch `docs/design-workers`, base `dev` `62591903`. Fourth of four: turn lifecycle → resources → credentials → **workers as things**.
**Consistency obligation:** every behaviour claim below names a `path:line` measured at HEAD `62591903`, and anything unmeasured is marked as such. Where this document and the code disagree the code is right; section 8 and the verification log say where that happened. The rulings inherited from `docs/DESIGN_TURN_LIFECYCLE.md` are referenced, never restated, and its numbering (`D1`, `D2`) is used as-is.
**Legend:** `MEASURED` = the code was read at HEAD and is quoted with an anchor. `INFERRED` = reasoning from names, docstrings or adjacent code. `UNMEASURED` = not establishable from this repository.
**Method:** this document rules on the object model (section 2), the verb table (section 3) and twelve invariants (section 4), and leaves five questions explicitly open with recommendations (section 7). Section 5 is not a design: it is today's behaviour in four measured subsections, and it is what sections 1 and 4 are arguing about.

---

## 1. Thesis: a worker outlives the dispatch, and that is a decision

The system has four objects that behave like one. That sentence is the whole document.

The **definition** is a template: name, prompt, tool set, permissions, ceilings. The **instance** is an incarnation: a session, a name, an `instance_id`, an epoch, plus a body (thread or process), containers, a conversation and a status. The **dispatch** is one query with one deadline and one reply. The **job** is the durable record of a dispatch.

Every defect of the last week is an operation addressed to one of these that the code performed on another. Four, named:

(i) A pause was addressed to the **instance** and could only be honoured by its **body**, which was inside a container call that cannot see the latch; the verb reported done while the body kept running. `WorkerThread.pause()` sets `_park_event` and `_manual_only_pause` synchronously (`tools/workspace/worker_thread.py:3228-3283`, latch set at `:3249`), while `status = "paused"` is only written when the body next reaches its boundary (`:4632-4637`). MEASURED.

(ii) A **dispatch** deadline was treated as an **instance** lifetime: the synchronous `query` path converts a reply timeout into `thread.kill()` plus a container reclaim (`worker_thread.py:9202-9221`), so the writer is destroyed because the reader stopped waiting. MEASURED, and it is the single most cited cause in this document.

(iii) A reply with no reader becomes no record, which becomes "nothing happened": the synchronous path registers no job at all — `registry.register` is called only on the detached and `submit_query` paths (`worker_thread.py:9097-9102`, `:9401-9406`) — so an expired dispatch leaves nothing to find. MEASURED.

(iv) A job record filed under an **instance** nobody can name any more, so a finished turn looks like it never ran: `_action_job_status` passes the caller's *current* `instance_id` into `registry.jobs(...)` (`worker_thread.py:9473-9477`), and `WorkerJobRegistry.jobs` filters on it (`tools/workspace/job_registry.py:336-337`). A job whose instance changed, or whose instance is gone, is invisible. MEASURED.

**The claim of this document:** the four objects are separately addressable, and the verbs must say which object they act on. Where the code cannot say, the code is wrong — not the vocabulary.

---

## 2. The four objects

A **definition** is a row in `workers.json` per workspace: name, system prompt, tool set, permission footprint, ceilings. It has no runtime. It is the only one of the four objects an operator edits directly, and it is the only one whose identity is a plain string (`name`). MEASURED (`web_ui/backend/workspace_routes.py:937-950`, `:1020-1092` for the definition CRUD surface).

An **instance** is one live incarnation of a definition in one session: `(session_id, worker_name, instance_id, epoch)`, plus a body, plus containers, a conversation and a status. Instances are named on disk as `workers/<session_id>/<name>[/<name>#<N>]` and every cross-process command carries that identity as a *fence*, so a worker that reuses a directory can reject a command meant for a different incarnation (`worker_thread.py:3924-3940`, `web_ui/backend/workspace_routes.py:1374-1385`). MEASURED. The epoch is the fifth component nobody writes down in prose but every teardown path already uses (`worker_thread.py:7800-7807`).

A **dispatch** is one query delivered to one instance with one deadline and one reply channel. Its identity is `query_id`. It is not a lifetime: it can end — by reply, by expiry — while the instance runs on. `deliver_query_and_block` is the whole contract today (`tools/workspace/worker_query.py:379-434`), and it is addressed to a *thread object*, not to a dispatch: the function has no `query_id` in its signature. MEASURED.

A **job** is the durable record of a dispatch, keyed by `query_id`, held in `WorkerJobRegistry` (`job_registry.py:58-70`). It survives its dispatch as a record; today it does not survive the process (section 8).

Three rules follow, and sections 3 and 4 are only these three rules applied:

1. **Addressing rule.** An operation is legal only if the object it is addressed to is the object it acts on, and the envelope must name that object. `stop` addressed to an instance and performed on a registration, or a deadline addressed to a dispatch and enforced on a body, is a defect regardless of whether the caller notices.
2. **Identity rule.** An instance is identified by `(session, name, instance_id, epoch)`; a dispatch and a job are identified by `query_id`. No verb may *infer* one of these from the caller's current state when the caller did not name it. A verb that defaults a lookup to "whatever instance the caller is currently holding" answers a question nobody asked (this is the mechanism of defect (iv)).
3. **Reporting rule.** An envelope reports what was done to which object, in the object's own words: `killed by <operator> at <time>`, `detached, body still alive`, `dispatch expired; instance orphaned`. An envelope that reports success without naming the object has not reported anything.

---

## 3. The verb table

Five verbs, each with exactly one object. "Refusal it owes" is the sentence the envelope must return when the verb declines — a refusal that does not name the object and the time is a silence.

| verb | addressed to | may do | must NOT do | refusal it owes |
|---|---|---|---|---|
| **kill** | the INSTANCE | terminate the body and reclaim its containers | be available to a model as a target choice (an operator names it) | `killed by <operator> at <time>` |
| **detach** | the DISPATCH / LISTENER | park the instance and keep its body, containers and registration | be called `stop` — the current name *is* the bug (`stop` is an alias for detach; termination is `kill`) | `detached, body still alive` |
| **pause** | the INSTANCE | set cooperative intent, and report `pausing` until the body honours it | claim to have paused work that is inside a tool call which cannot see the latch | `pause pending Ns, last observed boundary <time>` |
| **expire** | the DISPATCH | end the caller's wait | touch the instance; or leave the instance looking idle — it records `dispatch_expired_at` and marks the instance `orphaned` | `dispatch expired; instance orphaned; reply will be recorded against the job` |
| **adopt** | the INSTANCE | attach a new dispatch to an orphan | be implicit | `adopted orphaned instance <id>, previous dispatch expired at <time>` |

Two of these names are already in the code with the right meaning and the wrong reach. `stop` is documented as a detach — "``stop`` never terminates anything: the worker parks with status ``"detached"``, its thread, containers and registry entry all survive" (`worker_thread.py:9773-9780`) — and the same sentence proves the naming cost: every operator-facing route has to say "stop (DETACH) — or with ``terminate``, KILL" (`web_ui/backend/workspace_routes.py:1302`, and the command it writes at `:1379-1385`). `pause` exists as a verb with the right intent and no completion signal. `expire` and `adopt` do not exist at all.

---

## 4. Rulings

Twelve invariants. Each is a claim that can be falsified by a test; none of them is a preference.

### W1 — Lifetime is not dispatch

No dispatch verb may terminate an instance, and no instance may report `ready` while a dispatch it accepted has no reader. The two halves are one ruling because they are one confusion: if a deadline can kill a body, then a body's lifetime is a dispatch's, and if a dead dispatch leaves no mark, the instance is indistinguishable from an idle one. Today both halves are violated in the same file: a reply timeout calls `thread.kill()` and reclaims containers (`worker_thread.py:9202-9221`) while leaving no job record behind (defect (iii)).
**Consequence:** after an expired dispatch, `action='list'` shows `orphaned`, never `ready`.

### W2 — Three clocks, one declared number

The dispatch deadline, the instance turn budget and the soft timeout must be derived from ONE declared ceiling, and every envelope must report `granted` / `effective` / `source`. Three clocks that are merely *adjacent* drift: today the caller's wait is the worker's own budget **plus a grace constant** (`worker_query.py:26`, `:190-198`, `QUERY_WAIT_GRACE_SECONDS`), the thread's budget is `WORKER_TIMEOUT_SECONDS = 600` unless four other layers override it (`agent/config/defaults.py:61`), and the ceiling is re-derived per hop by `budget_for_thread(...)` (`worker_thread.py:9187-9193`). Measured mismatches are in section 6. A number that four layers can silently change is not a declaration; it is a negotiation.
**Consequence:** an envelope where `effective != granted` must name who changed it.

### W3 — Every accepted dispatch leaves a durable job record before it leaves a reader

The order in the sentence is the ruling: register first, then block. The reverse order — block, and record only if a reader is still there — is exactly how a reply becomes no record. The `submit_query` path already does it correctly and states the contract: "a submission is accepted only when it is BOTH queued and registered" (`worker_thread.py:9411-9430`). The synchronous `query` path does neither (section 5d). The invariant generalises the existing all-or-nothing rule from one verb to the object.
**Consequence:** a job can be listed as terminal even if its reply arrived after the reader stopped waiting.

### W4 — A job is findable without knowing an instance

Lookup by session (optionally worker name) must not be filtered to the caller's current `instance_id`. An answer you cannot find is an answer lost. The registry can already answer this question — `jobs(worker_name=..., session_id=...)` with `instance_id=None` means "no instance filter" (`job_registry.py:332-343`) — and the caller is what narrows it (`worker_thread.py:9473-9477`). So this is a one-argument defect in the consumer, not a missing feature in the store.
**Consequence:** `job_status` with no `job_id` returns the session's jobs, including those of instances that no longer exist.

### W5 — `ready` and `alive` are derived, never declared

They come from the runtime handle (`child exists` / `child alive` / `start_error`), not from a registry attribute that a body wrote about itself. A status a body asserts is a status the body can be wrong about, and "the thread never started in this process" is not the same fact as "the worker is dead" — the code says so itself (`worker_thread.py:2071-2076`, `worker_is_dispatchable`). PR #494 began this: `handle.process_start_error` is read (`:912-914`, `:1011-1018`), `alive is False` plus a recorded start error becomes `runtime_status = "failed"` with the error as the reason (`:1039-1041`), and `_list_entry_for` is the single row builder (`:988-1042`). W5 completes it by making every surface read that row.
**Consequence:** a process-mode worker whose start failed reports not-ready with the recorded start error.

### W6 — Pause is intent, not execution

A paused instance must not accept new work: every dispatch verb that can enqueue or deliver work must refuse while the manual pause latch is set. Today the latch is tested in exactly one place — on its own, above every status branch, with a comment that explains why it cannot live inside the `status == "paused"` branch (`worker_thread.py:9023-9052`) — and `_action_submit_query` has no such test at all (`:9333-9430`; it resolves the thread, checks `worker_is_dispatchable`, and enqueues). A pause that one verb honours and another does not is a pause with a hole in it, and `submit_query` is the verb that produces work with nobody waiting.
**Consequence:** a paused instance rejects `submit_query` with a named refusal.

### W7 — Unpauseable work must be visible

If a pause cannot be honoured inside a declared window, the instance reports `pause_pending_seconds` instead of a silent ignore. The pause plumbing reaches a tool that waits on the turn thread (`_bind_pause_event(self._park_event)`, `worker_thread.py:4482`, unbound at `:4868`), so `ContainerManager.exec` and `Sleep` can wake on a pause — but a `DockerCodeRunner` call that is already blocking the turn thread cannot, and the loop's own comment concedes that a pause is a wind-down observed at a boundary (`:4614-4670`). A latch nobody can observe from outside is indistinguishable from a pause that was ignored, and the operator's next action depends on which it was.
**Consequence:** an in-flight container call shows as pause-pending, not paused.

### W8 — Expiry is an event, not an absence

A dispatch that expires records `dispatch_expired_at` on the instance and is visible as state. "Nothing came back" is not a state; it is the absence of a state, which is why defect (iii) reads as "nothing happened". An event has a time, an owner and a name, so a later question ("why is this worker stuck?") has an answer. Nothing in the tree writes such a field today (`dispatch_expired_at` and `orphaned` do not occur in `worker_thread.py`; `orphaned` occurs once, at `:132`, in an unrelated comment about a lock inode).
**Consequence:** `inspect` shows the expiry, its time, and the job id it belongs to.

### W9 — No second writer

While an orphan exists, a new dispatch may be delivered only by an explicit `adopt`, or after the orphan is terminal; never silently queued behind it. A second writer on one conversation is how two turns interleave into one history nobody can attribute. The code already refuses one form of this — a busy worker raises `WorkerBusyError` rather than queueing (`worker_query.py:397-399`) — which is the right shape, and the wrong granularity: it is addressed to a thread object, not to an orphan.
**Consequence:** a spawn onto an orphaned instance either adopts it (and says so) or refuses.

### W10 — One source for status

`list`, `check`, `inspect`, the running-workers route and `job_status` must answer from the same objects, and any disagreement between them is a defect rather than a nuance. Today they are three actions and at least two sources: `list` reads `thread.status` through `_list_entry_for` and then *overwrites* it with `"stopped"` when nothing is alive (`worker_thread.py:7527-7560`), `check` resolves across sessions and falls back to foreign instances (`:8829-8850`), and `inspect` reads the runtime substrate — mode, child, `process_start_error`, `alive` — and never the body's `status` (`:7562-7600`). For a worker with no child process, `list` and `inspect` can both be right and disagree, which is the definition of a nuance nobody can act on.
**Consequence:** a worker with no child process answers the same status from all five surfaces, or the disagreement is reported as a defect.

### W11 — A delivered result is never discarded by a control verb

Pausing a supervisor must not drop a result that has already arrived for an outstanding dispatch. A result that arrives is evidence, and a control verb's job is to change what happens next, never what already happened. Measured today: a pause discarded the result of an in-flight worker dispatch from the agent's context (2026-11-26, this workspace, observed by the author — the envelope had arrived and was gone after the pause was honoured). This is the same class as W3 — a reply with no reader becomes no record — with a human action instead of a deadline as the cause.
**Consequence:** after a resume, an outstanding dispatch's result is either delivered or retrievable by job id — never silently lost.

### W12 — Kill requires an owner and a name

Termination is operator-only, from the UI, of a named instance; a model may not choose a kill target. This one is already engineered and is the model for the others: `kill` and `resume` are deliberately absent from the tool's action list, and asking for them returns a teaching refusal instead of a bare "unknown action" (`worker_thread.py:6804-6809`, `:6861-6877`). The operator path names its object — `_stop_worker_instance(ws_dir, name, instance_id, session_id, terminate)` then `thread.kill()` on the matched threads (`web_ui/backend/workspace_routes.py:1295-1324`, `:1403-1411`).
**Consequence:** no tool surface accepts a model-supplied kill target.


---

## 5. Measured inventory

This section is not a design. It is today's behaviour at HEAD `62591903`, in four parts, and every sentence in section 4 is an argument about one of them.

### 5a — Two control planes, and who can reach which verb

There are two entry planes and one execution plane, and the mismatch between them is the whole of W12.

**Plane 1, the model.** The `Worker` tool's action list is `["list", "spawn", "check", "query", "stop", "submit_query", "job_status", "join", "wait_for_job", "pause", "reset", "inspect"]` (`worker_thread.py:6809`). `kill` and `resume` are *deliberately* absent, and the comment above the list says why: "Termination (\"kill\") and revival (\"resume\") are deliberately NOT here: both are operator-only operations (web UI / workspace HTTP routes), never agent actions. The agent's \"stop\" DETACHES (recoverable park)" (`:6804-6808`). Asking for either returns a teaching refusal naming the operator route and steering to `stop` (`execute()` at `:6861`, refusal body `:6863-6877`). MEASURED.

**Plane 2, the operator.** The HTTP/UI routes write a command file and fast-path the in-memory registry: `_stop_worker_instance(ws_dir, name, instance_id, session_id, terminate)` resolves the worker directory, writes `{"action": "kill"}` or `{"action": "stop"}` with an identity fence, and then calls `thread.kill()` or `thread.stop()` on the threads matched by `(session, name, instance_id)` — outside the registry lock, because both touch the status file (`web_ui/backend/workspace_routes.py:1295-1324`, `:1374-1385`, `:1403-1411`). A parallel verb map sends the same four actions straight down a live runtime handle, "so a command routed through a handle and one polled off disk cannot disagree" (`:349-358`, `_dispatch_via_runtime` at `:361`). MEASURED.

**The execution plane.** `worker_thread.py:_poll_command` consumes one fenced payload from `<workspace>/workers/<label>/command.json` and rejects it unless the fence matches this incarnation (`:3924-3940`). Both entry planes converge here — the model's `pause` writes a `pause` command (`:3276-3279`), the operator's routes write `stop`/`kill`. So the *execution* plane has four verbs (`stop`, `kill`, `pause`, `resume`) while the *model* plane has one verb (`stop`) plus `pause`, and the two planes use the same word `stop` to mean detach in both. That coincidence is load-bearing and fragile: W12 is safe today only because the model's list is an allow-list, not because the verbs are named for their objects.

Verbs by who can reach them: `kill` — operator only. `resume` — operator only. `stop` (detach) — both. `pause` — both. `expire` and `adopt` — nobody; they do not exist.

### 5b — Pause wiring, and where the latch is observed

`WorkerThread.pause(manual_only=False)` does five things synchronously and one thing cooperatively (`worker_thread.py:3228-3283`): it delegates to the WLM if enabled (`:3236-3242`), sets `_park_event` and clears `_resume_event` (`:3243-3244`), sets `_manual_only_pause` when asked (`:3245-3249`) and records the same fact session-scoped so the latch survives the thread object (`_record_manual_only_pause`, `:1454-1481`, called at `:3261-3265`), puts the worker's agent into reading mode so the turn concludes with a Respond rather than being cut off (`:3268-3274`), writes a `pause` command for the cross-process plane and pushes `_PARK_SENTINEL` to unblock the input wait (`:3276-3283`). MEASURED.

The latch is then observed in exactly three places:

1. **Query delivery.** `_action_query` tests `getattr(thread, "_manual_only_pause", False)` on its own, above every status branch, and returns a `paused` envelope without sending the query (`:9033-9052`). The comment states the reason and is the best short defence of W6 in the tree: "the latch is tested here, on its own, ABOVE every status branch, so it refuses delivery whatever ``status`` currently says" (`:9023-9032`).
2. **The body's boundary.** The run loop's park branch, `if self._park_event.is_set():`, reports `status = "paused"` **once** per pause via `_pause_reported`, publishes `worker_paused`, and — for a detach only — abandons the turn and terminalises its job (`:4632-4680`). A pause is a wind-down: the loop keeps draining events until the agent's own conclusion arrives. MEASURED.
3. **Cross-process.** `_poll_command` forwards `pause`/`resume` to the body's own methods (`:3924-3985`, INFERRED for the dispatch body of that method; the verbs are the four in the operator map above, MEASURED at `workspace_routes.py:353-358`).

It is **not** observed by `_action_submit_query` (`:9333-9430`): that verb resolves the thread, checks `worker_is_dispatchable`, composes the follow-up, enqueues and registers the job — with no latch test anywhere. This is the measured basis for W6's consequence, and it is a hole rather than a nuance because `submit_query` is the verb whose work has nobody waiting.

Nor is it observed inside a tool call that cannot see the event. The only bridge from a pause to a blocking tool is a thread-local binding of the worker's own `_park_event` at the turn boundary (`_bind_pause_event(self._park_event)`, `:4482`, unbound at `:4868`), and the comment names its two consumers — `ContainerManager.exec` and `Sleep` (`:4473-4482`). No binding site for `DockerCodeRunner` exists in this file; INFERRED, therefore, that a container call in flight is exactly the work a pause cannot honour, which is W7's subject.

### 5c — Force-spawn, and what survives it

`force=True` kills stale instances of the same name before spawning (`worker_thread.py:7736`), then waits for exit. Two details matter and both are deliberate:

- The wait is **conditional on the thread having been started in this process**. If `thread.ident is None`, `join()` would raise "cannot join thread before it is started", so the join is skipped and `survived_force_stop` is decided solely by `is_alive()` (`:7739-7754`). In the default process runtime mode that is the normal shape, not an error. MEASURED.
- The join budget is `max(30, thread._timeout_seconds)` and the loop gives up when it elapses (`:7756-7764`).

What survives: a body that survives the kill **keeps its registry entry, on purpose**, with an `error`-level log line that says why — "its registry entry is kept so the instance is not orphaned" (`:7776-7785`). Only the non-survivor is popped from `_worker_registry` and has its runtime handles unregistered, fenced with its own `_generation` so the teardown cannot unplug the handle of the instance about to be created (`:7786-7803`). Containers survive with the body: a killed thread reclaims its containers on *its own* teardown, and a thread still inside a container call has not reached that teardown.

What does not happen: the surviving instance is not excluded from the spawn decision. It remains in `live_same`, and with `force` set the branch falls through to `spawn_iid = max(...) + 1` and spawns anyway (`:7895-7918`, read at `:7900-7918`). **Force-spawn can therefore produce two live bodies of one name, and says so only in a log line.** That is Q2, and it is not hypothetical: it is a branch the code takes.

### 5d — The job registry, and who can see a job

`WorkerJobRegistry` is one dict in one process: `self._jobs: dict = {}` under an `RLock` (`job_registry.py:66-70`), records keyed by `query_id` (`:58-64`), created by `register()` with worker name, session and instance attribution (`:128-169`), mutated by `update()`/`complete()`/`terminalize()`, and mirrored from `WORKER_*` events (`:90-124`). It never touches the filesystem: the module imports no `json` and opens no file. Capacity is `JOB_REGISTRY_MAX_JOBS = 200` (`agent/config/defaults.py:107`) with oldest-by-`created_at` eviction (`job_registry.py:365-371`), and terminality is the fixed set `("completed", "paused", "timeout", "error", "stopped", "interrupted")` (`defaults.py:114`). MEASURED.

Reads are filtered three ways and the caller chooses: `jobs(worker_name=, status=, instance_id=, session_id=)` skips a record only for arguments actually supplied (`:320-344`, the `instance_id` test at `:336-337`), and `job(job_id, session_id=)` denies a record belonging to another non-empty session, logging the mismatch (`:292-318`). Worker-name attribution is not a filter the store defaults to; the consumer supplies it.

Who can see a job, today:

- the caller of `submit_query`, and the caller of a `query` against a *detached* worker, both of which get `job_id` in the envelope (`worker_thread.py:9110-9133`, `:9427-9430`);
- `action='job_status'` by id — a session-scoped single read (`:9442-9473`);
- `action='job_status'` without an id — **filtered to the caller's current `instance_id`** (`:9473-9477`), which is W4's defect;
- the reporter closure that closes a record when the child answers (`_job_registry_reporter`, used at `:9069-9074` and `:9375-9384`).

A synchronous `query` that is answered registers nothing. A synchronous `query` that times out registers nothing and kills the body. Those two sentences are defects (ii) and (iii), and they are the same missing call.

---

## 6. Two clocks, one number

W2 is not a matter of taste. Two measured cases, one from each side of the desk:

1. **This workspace, 2026-11-26.** A spawn granted 420 s reported "no reply within 480.0s". 480 = 420 + 60: the caller's wait is the worker's own budget plus a grace constant, so the envelope's number is not the number the caller granted, and nothing in the envelope says which layer produced it. The mechanism is one function used twice: `strict_wait_for_reply` computes `effective = _worker_query_wait_timeout(timeout_seconds, fallback)` = budget + `QUERY_WAIT_GRACE_SECONDS` (`worker_query.py:190-198`, documented at `:26`, `:177-181`), and an explicit `grace=` argument recomputes it as `timeout + grace` (`:198`). `deliver_query_and_block` passes it through (`:419-426`) and classifies anything that comes back as `timeout` (`:427-434`).
2. **Independent Windows host, rev `63b47b5`.** `timeout_triggered=true` at 71.8 s of a 90 s grant. Here the *worker's* clock fired early rather than the caller's late, which is the same defect seen from the other end: the side that declared 90 s is not the side that measured 71.8 s, and the envelope carries no field in which the two numbers could be compared.

The mechanics visible in the code, which side sets what:

- the **caller** sets a wall-clock wait derived from the thread's `_timeout_seconds` plus grace (`worker_query.py:190-198`), and records the budget it believes it used on the envelope (`worker_thread.py:9187-9193`, `budget_for_thread(..., source="worker_thread.query")`);
- the **worker config** sets the body's own soft timeout plus a warning threshold at 80 % of it (`:4284-4288`), and the default when nothing is configured is `WORKER_TIMEOUT_SECONDS = 600` (`agent/config/defaults.py:61`);
- the **agent state machine** applies a third restriction when its own time ceiling trips (`agent/core/state.py:240-252`, cited as defect class #3 in `docs/DESIGN_TURN_LIFECYCLE.md` §1);
- the **dispatch timeout path** then enforces the caller's number with `kill()` and a bounded join (`worker_thread.py:9202-9246`).

Four layers, no declaration, and the only number exported is one of them. **The single deliverable:** one declared ceiling carried on the *instance*, exported in every envelope as `granted` / `effective` / `source`, with any discrepancy naming the layer that changed it. `docs/DESIGN_TURN_LIFECYCLE.md` rulings D1 and D2 are the unimplemented half of the same decision — the half that lives inside the turn rather than at its edge. This document does not restate them; it asks for the number they were written about.


---

## 7. Open questions

Five questions this document does **not** settle. Each carries a recommendation and the trade it costs, because a recommendation without its trade is a preference.

**Q1 — Does an orphan get to finish?**
*Recommendation:* yes, bounded by a declared `orphan_grace_seconds`, then `kill`. A parked writer holding containers is worse than a lost reply: it consumes the scarce thing (a container, a slot under `MAX_WORKERS_PER_SESSION`) while producing nothing, and the next dispatch onto it cannot legally be delivered under W9. The trade: we may kill work that was about to be salvageable. That trade is acceptable only because W3 and W8 make the loss *visible* — the job id, the expiry time and the orphaned status survive the kill, so "we killed it after the grace" is a fact on the record rather than a silence.

**Q2 — May force-spawn run two bodies?**
*Recommendation:* no. Force must mean kill, wait, then spawn; if the wait budget elapses, refuse the spawn. Section 5c measures the current behaviour: a survivor keeps its registry entry by design (`:7776-7785`) and the spawn proceeds anyway (`:7900-7918`), so one name can have two live bodies and only a log line records it. The trade: an operator who wants a clean slate must first accept the kill. That is the right cost — "force" that means "try harder and hope" is the shape of defect (ii), a control verb whose consequence depends on whether something else happened to be alive.

**Q3 — Can a pause interrupt a container call?**
*Recommendation:* not in this slice. Report `pause-pending` (W7) and keep the boundary check. The bridge that exists is a thread-local binding of the park event at the turn boundary (`:4482`), consumed by tools that wait on it (`ContainerManager.exec`, `Sleep` — the comment's own list, `:4473-4482`); a `DockerCodeRunner` call is not in that set, and making it interruptible is a change to the container tool's blocking contract, not to the pause verb. The trade: an operator who pauses during a 20-minute build gets an honest "not yet" rather than a prompt stop, and honest-but-slow is the failure mode this document prefers to dishonest-and-fast.

**Q4 — Is the job registry durable?**
*Recommendation:* yes — an append-only per-session file with a bounded ring, because a diagnostic that dies with the process cannot diagnose a crash. Today the registry is a dict (`job_registry.py:66-70`) with a 200-record in-memory ring (`defaults.py:107`), so the one event most worth diagnosing — the process that died — takes its evidence with it. The trade: a new write surface that needs a permission grain and a redaction review, because job records carry result previews (`PREVIEW_CAP = 8000`, `defaults.py:109`) and previews are model output. That is a cost this repository has paid twice already (`docs/DESIGN_CREDENTIALS.md` C2, C7) and it is worth paying once more rather than shipping a diagnostic nobody can read after a crash.

**Q5 — Who owns the dispatch deadline: the agent tool, the worker config, or the instance?**
*Recommendation:* the instance's declared ceiling, with the caller able to shorten but never lengthen it. Today all three claim it: the tool passes `timeout_seconds`, the thread resolves `_timeout_seconds` through the session-owned chain, and the wait is that budget plus grace (`worker_query.py:190-198`). A ceiling with three authors is how case 1 in section 6 happened — 420 granted, 480 waited. The trade: callers lose the ability to be generous on a whim, which is a real loss for a caller who knows its query is slow. It is mitigated by the `source` field W2 requires: a shortened effective must name the shortening layer, so the generosity that was removed becomes a request that is visible.

---

## 8. Honest gaps

What this document cannot claim, stated as plainly as the claims it can:

1. **No per-tool pause binding for container calls.** The only bridge from `pause` to a blocking tool is `_bind_pause_event(self._park_event)` at the turn boundary (`worker_thread.py:4482`), and its consumers are the two the comment names (`:4473-4482`). A `DockerCodeRunner` call in flight is not bound. W7 exists to make that visible; it does not fix it. INFERRED (no binding site was found in this file; the tool's own code was not read).
2. **A thread-mode body cannot be terminated safely.** The kill is cooperative: `run()` teardown is authoritative, and when the bounded join budget elapses the daemon thread is "left to terminate on its own" — stated twice, at `:8931-8941` (the `_wait_for_worker_exit` docstring) and `:9226-9246` (the timeout path's "Stop guarantee"). W1 forbids a dispatch verb from terminating an instance; it cannot make termination *possible*, which is a separate problem with no proposed answer here.
3. **Process-mode portability is verified on Linux only.** PR #494 (`handle.process_start_error`, `alive is False` for an attempted-and-failed start, `_list_entry_for`) is read at HEAD (`:912-914`, `:1011-1042`). The Windows matrix is green, but the AF_UNIX-absent path is only exercised by deleting the attribute in tests, so "works when there is no Unix socket" is a test-only claim, not a measured one. UNMEASURED on a real Windows host.
4. **The job registry is in-memory and per-process.** Section 5d. Consequence: after a crash nothing is findable, and `job_status` for a pre-crash job answers "Job not found" (`worker_thread.py:9449-9451`) with the same words it uses for a job that never existed. Those two facts should not read identically.
5. **Nothing in this document is implemented except W5's first half.** W12 is already engineered (`:6804-6809`, `:6861-6877`); W5's handle-derived half landed in PR #494. W1–W4 and W6–W11 are rulings against measured behaviour, not descriptions of it.

One further scope note: this document rules on *verbs and objects*, not on the substrate. `worker_runtime.py`, `worker_runtime_process.py` and the IPC protocol were read only where a claim above needed them; the resource half of the same worker (containers, reclaim, ceilings) lives in `docs/DESIGN_RESOURCES.md` and is referenced, never restated.

---

## 9. Order of work

Five slices, smallest first, each independently shippable and each falsifiable by one test.

**S1 — Expiry is an event, and `orphaned` is a status (W8, W1).** File: `tools/workspace/worker_thread.py` — the synchronous timeout branch (`:9202-9246`) records `dispatch_expired_at` and the job id on the instance and sets `orphaned` on a new envelope field, before the reclaim. Invariant test: expire a dispatch, then `action='list'`; the row says `orphaned` with a timestamp and a job id, never `ready`.

**S2 — Job findability (W3, W4).** Files: `worker_thread.py` (register the synchronous dispatch before it blocks, per the `submit_query` all-or-nothing pattern at `:9411-9430`; drop the `instance_id` argument at `:9473-9477`) and `tools/workspace/job_registry.py` (no change required — `jobs()` already treats `instance_id=None` as "no filter", `:320-344`). Invariant test: `job_status` with no id, called after the instance is gone, returns the session's jobs including that one.

**S3 — Pause gate on every dispatch verb (W6), and pause-pending reporting (W7).** File: `worker_thread.py` — lift the latch test at `:9033-9052` into the shared prologue of `_action_query` / `_action_submit_query` / `_action_spawn` / `_action_join`, and record `pause_pending_seconds` from the park branch (`:4632-4644`). Invariant test: with `_manual_only_pause` set, `submit_query` returns a named refusal and enqueues nothing; with a container call in flight, the instance reports pause-pending rather than paused.

**S4 — Adopt, and no second writer (W9).** File: `worker_thread.py` `_action_spawn` — the force/spawn decision (`:7900-7918`) must either adopt a registered orphan (and say so in the envelope, per the verb table) or refuse; the survivor-retention path (`:7776-7785`) becomes the orphan's registration rather than a log line. Invariant test: spawn onto an orphaned instance; the envelope either names the adopted instance id or refuses, and in no case are two bodies live under one name.

**S5 — One declared ceiling (W2).** Files: `agent/config/defaults.py` (the single ceiling constant, replacing the derived `QUERY_WAIT_GRACE_SECONDS` arithmetic at `worker_query.py:190-198`), `worker_thread.py` (carry `granted`/`effective`/`source` on every dispatch envelope, `:9187-9193` today), `tools/workspace/worker_query.py` (the wait becomes the ceiling, not ceiling+grace). Invariant test: an envelope with `effective != granted` names the layer that changed it; and the 420/480 case reproduces as 420/420 with `source="instance"`.

S1 first because every other slice needs the expiry to be visible; S5 last because it is the only slice that changes a number rather than an object.


---

## Verification log

Every anchor in this document was re-read at HEAD `62591903`. What follows is the diff against the anchors supplied with the brief, so a reviewer can see exactly where the tree moved under the prose. Sub-ranges inside a verified function start (e.g. "the `registry.jobs(...)` call at `:9473-9477`") were derived by counting from a grep-verified `def`, and carry an error bar of about three lines; function `def` lines, and every line quoted *as text*, were grep-verified exactly.

**Confirmed at the dictated line:** `worker_query.py:307` (`_resolve_terminal`), `:312` (`stop_on_timeout`), `:379` (`deliver_query_and_block`), `:387` (`stop_on_timeout` default); `worker_manager.py:64-66` (the `worker_query` import) and `:755`/`:782` (its two call sites); `web_ui/backend/global_routes.py:51` and `workspace_routes.py:63` (the only two importers of `worker_manager` found); `worker_thread.py:2604` (`_manual_only_pause`), `:1454` (`_record_manual_only_pause`), `:4288` (`stop_check`), `:4482` (`_bind_pause_event(self._park_event)`), `:4632` (`if self._park_event.is_set():`), `:9033` (the pause gate in `_action_query`), `:8931` (`_wait_for_worker_exit` start), `:7784` (the "survived force-stop" log), `:9442` (`_action_job_status`); `job_registry.py:128` (`register`), `:292` (`job`), `:320` (`jobs`), `:336` (the `instance_id` filter).

**Moved:** `pause()` def `3243 → 3228` (−15; its last line `3283` unchanged). `_action_submit_query` `9340 → 9333` (−7). The paused auto-resume branch `9131-9138 → 9134-9160` (+3). The force-spawn kill/join block `7745-7795 → 7736-7803` (kill at `:7736`, join `:7756-7764`, retention `:7776-7785`). `_wait_for_worker_exit` body `→ :8931-8977` (start exact; ~28 lines of new comments on the never-started-thread path). `job_registry` store `71 → 69` (−2), `register` `130 → 128` (−2), eviction `352 → 365` (+13), `jobs()` filtering `300-320 → 320-344` (+20). The park-observation *range* `4618-4641 → 4614-4644` (start −4, end +3); its anchor line `4632` is exact.

**Not found as dictated, and corrected in place:** (a) `worker_thread.py:7919-7923` is no longer a dispatch-error path; at HEAD it is the `D2` reuse-delivery comment, whose operative sentence — "The existing worker is NEVER stopped on timeout" — sits at `:7920-7927`. The dispatch-error path the brief means is the `RuntimeError` branch at `:9288-9332`, which explicitly refuses the timeout path's kill/reclaim reflex and leaves the registry entry intact; that is the one place where "the dispatch failed and nothing was stopped" is stated as a rule. (b) `running_workers` is not an action: `VALID_ACTIONS` (`:6809`) lists twelve and none is named `running_workers`; the surface exists as `GET /{ws_id}/workers/active` (`workspace_routes.py:829-834`), and W10's wording is kept with that mapping. (c) `dispatch_expired_at`, `pause_pending_seconds` and `orphaned`-as-status do not occur in `worker_thread.py` at HEAD (the single hit for "orphaned" is `:132`, an unrelated comment about a lock inode), which is why W7 and W8 are written as rulings rather than as descriptions.

**Where the code contradicts the brief's framing**, stated plainly because it is the most useful thing here: the synchronous `query` verb already terminates the instance it was only supposed to stop waiting for (`:9213-9214`, `thread.kill()` on `TimeoutError`, followed by a container reclaim and a bounded join). W1 forbids exactly that, so W1 is a prescription and not a description — and the same branch is the origin of defects (ii) and (iii) at once. Second: W2's premise that three clocks derive from one declared ceiling is not merely unimplemented, it is inverted in the code — the caller's wait is the declared budget **plus** `QUERY_WAIT_GRACE_SECONDS` (`worker_query.py:190-198`), so the number the caller is told has a second author before the worker has even seen it. Third, a smaller one: `worker_manager.py:847-849` states that "``worker_thread`` imports this module", and no such import exists in `worker_thread.py` at HEAD — the claim that `worker_manager` is reachable only from the two UI routers holds, but the docstring that says otherwise is stale and will mislead the next reader.

**Not re-verified:** the `D1`/`D2` labels in `docs/DESIGN_TURN_LIFECYCLE.md` are cross-referenced as the brief dictates, but that file's rulings were not re-read at HEAD (its §1 was, and it does list the three restriction clocks as defect classes 2–4). Its anchors are quoted from the document, not re-measured here. The `_poll_command` body's dispatch of the four command verbs (`:3924-3985`) is INFERRED: the method's entry and its fence handling were read, its per-verb branches were not.

