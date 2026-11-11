# Design: Worker State Re-Attachment After a Process Restart (C44 Batch 2)

**Status:** Proposed (scoping)

**Scope:** what happens to a worker when the process that hosts it dies and comes back. Specifically:
which halves of "worker state" survive on disk, which halves live only in RAM, what re-attachment
machinery already exists, what is missing, and the smallest design that closes the dangerous gap
(container reaping of a worker a human still intends to resume) without breaking the existing
fences. This document is analysis + options; it proposes **no code and no test changes**. Container
lifecycle semantics, cross-machine distribution, and the tool-loop's own semantics are out of scope.

**Claim tags.** Claims in this document are tagged so a reader can tell reading from reasoning:

| Tag | Meaning |
|---|---|
| `[verified]` | Read from source in this worktree at the cited `path:line`. |
| `[inferred]` | Reasoned from verified code (control flow, call graph) but not executed. |
| `[unverified]` | Asserted from naming, comments or docstrings without confirming the code path. |

Every file:line below is relative to the repository root of this worktree.

---

## 1. Problem statement: worker state is two halves with radically different restart stories

"Worker state" is not one thing. It is two things that happen to be reachable from the same object,
and they behave in opposite ways when the host process restarts.

### 1.1 Half (a): conversation and context — durable, reloadable

The worker's *conversation* is a first-class on-disk artifact. `WorkerThread._context_path()` returns
`self._worker_dir / "context.json"` `[verified: tools/workspace/worker_thread.py:4474-4475]`.
`_save_context()` writes a payload containing the persistable `WorkerContext`, `status`, `error`,
`current_task`, `last_heartbeat`, `last_query`, `last_completed_query`, `last_query_id`,
`pruned_since_last_query` and `generation` `[verified: tools/workspace/worker_thread.py:4846-4857]`,
guarded by an F2 generation check that refuses to overwrite a newer on-disk generation
`[verified: tools/workspace/worker_thread.py:4828-4844]`, and written via temp file + `os.replace`
`[verified: tools/workspace/worker_thread.py:4860-4872]`. `_load_context()` reconstructs it through
`WorkerContext.from_persistable_dict` and folds the on-disk generation forward with
`_generation = max(self._generation, file_gen)` `[verified: tools/workspace/worker_thread.py:4614-4645,
:4639]`. In `run()` the context is loaded before anything else
`[verified: tools/workspace/worker_thread.py:3671]`; when present, the caller's `_initial_context` is
merged onto it after F1 / WLM pruning
`[verified: tools/workspace/worker_thread.py:3784-3790, :4647-4678]`; when absent the worker starts
fresh and queues the initial query `[verified: tools/workspace/worker_thread.py:3781-3782]`.

The main agent's own conversation is equally durable: `FileSystemSessionStore` keeps
`~/.thoughtmachine/workspaces/<ws_id>/sessions/<name>_<short_id>.json` plus
`_meta_<sid>.json`, with `FileLock` + atomic write
`[verified: session/store.py:346-350, :551-555, :311-344]`.

So: **kill the process, restart it, re-spawn the same worker name, and the conversation comes back.**
That half of the problem is already solved, and it is the half people think about when they say
"the worker remembers".

### 1.2 Half (b): execution, liveness and container ownership — in-memory, evaporates

Everything that makes the conversation *live* is a Python object graph owned by the process:

- The registry entry that lets anyone find the worker at all: `WorkerRegistry` keys
  `(session_id, worker_name, instance_id)` to a live `WorkerThread`, with epoch
  compare-and-set on `thread._generation` `[verified: tools/workspace/worker_registry.py]`. Nothing in
  it is persisted `[verified: tools/workspace/worker_registry.py]`. After a restart the registry is
  empty `[inferred: it is a plain in-process dict, constructed at import/`__init__`, and no
  rehydration path exists]`.
- The OS thread and its `queue.Queue` input queue, plus the `WorkerJobRegistry` jobs riding on it.
  Neither is persisted: the queue is a thread attribute `[verified: tools/workspace/worker_thread.py:2108]`
  and the job registry is a plain in-memory `dict` (`self._jobs`), fed by events, with no serializer on
  any path `[verified: tools/workspace/job_registry.py:53-65]` — see §2.
- The park/resume signalling pair `_park_event` / `_resume_event` — the entire mechanism by which a
  "detached" worker is revivable `[verified: tools/workspace/worker_thread.py:2075-2076, :2291-2292]`.
- The detached-worker container exemption: a module-level
  `_DETACHED_WORKER_IDENTITIES = set()` in the container manager
  `[verified: infra/container_manager.py:5215]`, populated by `register_detached_worker`
  `[verified: infra/container_manager.py:5218-5225]` via
  `WorkerThread._set_detached_registration`
  `[verified: tools/workspace/worker_thread.py:2176-2197]`.
- The detached TTL clock: `PeriodicStaleCheck` measures `WORKER_TIMEOUT_SECONDS` from the *thread's
  own* `_detached_at` attribute `[verified: tools/workspace/worker_manager.py:136-208, :208-240]`.
- The lifecycle observer's rings and staleness/hung bookkeeping
  `[verified: tools/workspace/worker_lifecycle.py]`.

Kill the process and all of that is gone. Not "stale" — gone. There is no boot-time sweep that walks
`workers/*` and rebuilds any of it `[verified negatively: the seven enumerations of `workers/*` outside
the worker package are all inside `web_ui` (bridge.py:3048, workspace_routes.py:1326/:1419/:1625/:1841/:1961,
health_routes.py:59), and all seven are read-only — §3.4]`.

### 1.3 The thesis

> After a restart, the *conversation* half of worker state is reloadable and the *execution* half is
> not. There is no boot-time re-attachment. What exists is (i) lazy context reload when *somebody
> re-spawns* the worker, (ii) journal adoption at turn start for a crashed turn, and (iii) a
> read-only UI enumeration of `context.json`. The dangerous consequence is not lost chat history — it
> is that a *previously detached* worker's containers become indistinguishable from a *crashed*
> worker's containers, because the only thing that distinguished them was a Python set.

The join between the two halves is stable and documented in §2.3: `<session_id>:<worker_name>`,
the `instance_id`, the `_generation` epoch, the `thoughtmachine.worker` container label, and the
`workers/<instance_label>/` directory. The problem is not a missing key — it is a missing *reader*.

---

## 2. Current state: what is on disk, what is in RAM, and how the two are keyed

### 2.1 The durable surface

| Artifact | Location | Writer | Contents that matter here | Citations |
|---|---|---|---|---|
| `context.json` | `<worker_dir>/context.json` | `WorkerThread._save_context()` | persistable `WorkerContext`, `status`, `error`, `current_task`, `last_heartbeat`, `last_query`, `last_completed_query`, `last_query_id`, `pruned_since_last_query`, `generation` | `[verified: tools/workspace/worker_thread.py:4474-4475, :4846-4857]` |
| `status.json` | `<worker_dir>/status.json` | `WorkerThread._write_status_file()` | status frame mirror; tempfile + `FileLock` + `os.replace`, frame published only after durable & unlocked | `[verified: tools/workspace/worker_thread.py:4756-4813, :4778-4792, :4793-4798]` |
| generation sidecar | `<worker_dir>/generation` | `_allocate_generation()` | monotonic per-worker epoch; single FileLock + threading lock; temp + `os.replace`; read-back verifies value **and** `st_ino` | `[verified: tools/workspace/worker_thread.py:4477+, :4560-4566, :4609-4612]` |
| `command.json` | `<worker_dir>/command.json` | `write_command(worker_dir, verb, fence)` | cross-process control channel (`stop` / `resume` / `kill`), fenced by epoch | `[verified: tools/workspace/worker_thread.py:2106, :2290; tools/workspace/command_channel.py:78-84]` |
| `inflight.json` | `<workspace>/workers/<instance_label>/inflight.json` | `write_inflight` / `adopt_inflight` | the turn journal: statuses `inflight` / `recoverable` / `cancelled` / `adopted` | `[verified: tools/workspace/inflight_journal.py:1-9, :134-145, :429-507, :525-600+]` |
| session JSON | `~/.thoughtmachine/workspaces/<ws_id>/sessions/<name>_<short_id>.json` (+ `_meta_<sid>.json`) | `FileSystemSessionStore.save_session` | the main agent conversation | `[verified: session/store.py:346-350, :551-555, :311-344]` |
| container records | vault (`thoughtmachine/container_record`, `find_by_docker_label`, `RECORD_LABEL_KEY`) | container manager | the owning record, the operator note, lifecycle class | `[verified: tools/workspace/worker_container.py:22-28; infra/container_manager.py:53-59]` |
| containers themselves | Docker daemon | — | labelled `thoughtmachine.worker=<session_id or 'unknown'>:<worker_name>` | `[verified: tools/workspace/worker_container.py:1-12, :30-51; `_WORKER_CONTAINER_LABEL` :37; `_RESOURCE_CONTAINER_LABEL` :41]` |

Note the asymmetry inside this table: the *worker* writes its own state files, but the *container
world* is described by records in the vault and by Docker labels. The worker-side index into that
world — "my containers are exempt from reaping because I am detached" — is the only piece that is
not on disk (§2.2).

`status.json` deserves emphasis because it is the design lever:

> `WorkerRuntimeHandle._status_json_path()` resolves `<worker_dir>/status.json`, and `state()` falls
> back to that file's mirror after the frame cache
> `[verified: tools/workspace/worker_runtime.py:969-985, :843, :863, :866]`.

That is, **`status.json` is the only thing a fresh process can learn about a worker without the
worker's cooperation** `[verified: tools/workspace/worker_runtime.py:843-866]`. And `status.json`
records `status == "detached"` on disk, because `detach()` writes it before it parks
`[verified: tools/workspace/worker_thread.py:2071-2072, :2097-2098]`. The fact needed to avoid
reaping a detached worker's containers is therefore already durable; what is missing is a reader.

### 2.2 The in-memory surface (all of it dies with the process)

| In-memory state | Where it lives | Why it matters after a restart | Citations |
|---|---|---|---|
| registry entries | `WorkerRegistry._worker_registry`, `_worker_epochs`, `_worker_event_bus_registry`, `_bus_epochs` | nothing can find a worker; `WorkerManager.resume_worker` degrades to a silent `{"found": False, "action": "resume", ...}` no-op (it raises `WorkerControlError` only when a *found* thread lacks a callable method) | `[verified: tools/workspace/worker_registry.py; tools/workspace/worker_manager.py:798, :861-887, :865-872]` |
| `_input_queue` | `queue.Queue` on the thread | queued queries are lost; a detached worker's backlog vanishes | `[verified: tools/workspace/worker_thread.py:2108, :2174]` |
| `WorkerJobRegistry` jobs | in-process `self._jobs: dict`, event-fed `[verified: tools/workspace/job_registry.py:53-65]` | `wait_for_job` has nothing to terminalize | `[verified: no serializer exists]` |
| `_agent`, `_wlm` | thread attributes | tool-loop state, reading-mode lease, WLM registration | `[verified: tools/workspace/worker_thread.py:2265-2271, :2307-2310]` |
| `_park_event`, `_resume_event` | thread attributes | the revive mechanism itself | `[verified: tools/workspace/worker_thread.py:2075-2076, :2291-2292]` |
| detached identity set | `infra.container_manager._DETACHED_WORKER_IDENTITIES` (module-level) | the stale-container sweep's only exemption signal | `[verified: infra/container_manager.py:5215, :5218-5238, :5241-5253, :5201-5214]` |
| detached TTL clock | `thread._detached_at` | TTL kill decisions measure from it | `[verified: tools/workspace/worker_manager.py:208-240]` |
| heartbeat ages | `last_heartbeat` / `_last_heartbeat_monotonic` on the thread | staleness detection | `[verified: tools/workspace/worker_manager.py:371-394]` |
| lifecycle rings, stale/hung flags | `WorkerLifecycleObserver._global_ring`, `_per_worker`, `_last_heartbeat`, `_stale_flagged`, `_terminal`, `_hung_notified`, `_worker_session` | timeout/hung notifications | `[verified: tools/workspace/worker_lifecycle.py]` |
| the stale monitor thread | `_MANAGER_INSTANCE = WorkerManager()`, `ensure_stale_monitor()`, `ensure_stale_check()` | `PeriodicStaleCheck` is re-created per process, hence starts from zero knowledge | `[verified: tools/workspace/worker_manager.py:1001-1007, :136-208]` |

`WorkerRegistry.__init__` registers `atexit.register(shutdown_workers, ALL_SESSIONS)`, and
`shutdown_workers` kills and bounded-joins, then compacts and saves context
`[verified: tools/workspace/worker_registry.py (~:350-380)]`; the comment there explains that the
detach-only `stop()` would otherwise leave a thread that never exits the bounded join
`[verified: tools/workspace/worker_registry.py:365]` (explanatory comment). This is a *graceful
shutdown* path. It does nothing for `SIGKILL`, a crash, an `os._exit`, or a container of the host
being bounced — and the `atexit` handler is itself in-memory knowledge.

### 2.3 The join keys

A restart-time re-attachment design has to be able to say "this disk artifact and this container
belong to the same logical worker". The keys already exist and are already used consistently:

| Key | Where it appears | Notes |
|---|---|---|
| `(session_id, worker_name, instance_id)` | `WorkerRegistry` key; `WorkerManager.register_worker` passes `epoch=getattr(thread, "_generation")` | `[verified: tools/workspace/worker_manager.py:463]` |
| `_generation` epoch | sidecar `<worker_dir>/generation`, `context.json["generation"]`, `CommandFence` | **one** counter, not two: `[verified: tools/workspace/command_channel.py:78-84 "NOT a second counter"]` |
| `thoughtmachine.worker` label value | `<session_id or 'unknown'>:<worker_name>` on every worker container | `[verified: tools/workspace/worker_container.py:30-51]`; ownership is established by **exact** label value `[verified: :37, :70+]` |
| `workers/<instance_label>/` dir | holds `context.json`, `status.json`, `command.json`, `inflight.json`, `generation` | dir name is the bare worker name when `spawn_iid == 1` `[verified: tools/workspace/worker_thread.py:1255-1267; tools/workspace/worker_registry.py:129-139]` |
| inflight fence | `read_inflight` (fenced, same incarnation) vs `read_inflight_unfenced` (adoption reader) | `[verified: tools/workspace/inflight_journal.py:33-73]` |

Two friction points worth naming now, because they constrain §5:

1. The registry key includes `instance_id`; the UI's disk catalogue is keyed by **bare directory
   name** `[verified: web_ui/backend/bridge.py:3021-3067]`. So `<name>` and `<name>_<iid>` directory
   layouts are conflated by the only existing enumerator.
2. The label value is `session_id:worker_name` — it carries **no** epoch and no `instance_id`
   `[verified: tools/workspace/worker_container.py:30-51]`. So a container cannot, by label alone, be
   attributed to a *specific incarnation* of a worker. Attribution across incarnations therefore has
   to go through the vault record or through the worker directory.

### 2.4 The restart transition, step by step

What actually happens today when the host process dies with a detached worker outstanding
`[inferred from the citations below]`:

1. `atexit` may or may not run. If it runs, `shutdown_workers` kills and saves context
   `[verified: tools/workspace/worker_registry.py:~350-380]`. If it does not (crash/kill), the last
   `_save_context()` is whatever the detach path wrote `[verified: tools/workspace/worker_thread.py:2085]`.
2. On disk: `context.json` (status `detached`), `status.json` (status `detached`), `generation`,
   `command.json` (last verb, e.g. `stop`, with the old fence), maybe `inflight.json`.
3. In Docker: the worker's containers, still labelled, still running or exited.
4. In the new process: registry empty; `_DETACHED_WORKER_IDENTITIES` empty; `PeriodicStaleCheck`
   just constructed with no threads to walk; lifecycle rings empty.
5. `resume()` is unreachable — there is no thread object to call it on. `WorkerManager.resume_worker` does
   not raise here: for an unregistered thread it returns `{"found": False, "action": "resume", ...}`, a
   silent no-op `[verified: tools/workspace/worker_manager.py:865-872]`. `WorkerControlError` is raised
   only when a thread *is* found but lacks a callable method `[verified: tools/workspace/worker_manager.py:798, :861-887]`.
6. Any stale-container sweep that runs now sees step 4's empty set and step 3's live containers, and
   — absent a `worker_status` argument — treats them exactly as it would a crashed worker's
   `[verified: infra/container_manager.py:5287-5311]`.

Step 6 is the hazard this design exists to prevent. It is worth being precise that the *mechanism*
is already fail-closed in one dimension: `_detached_state()` is tri-state, and `None` means "cannot
tell → do not reap" `[verified: infra/container_manager.py:5241-5253]`, with a long comment
explaining the reasoning `[verified: infra/container_manager.py:5201-5214]`. The problem is that
after a restart the set is not unknown — it is *empty and confident*. An empty set answers "not
detached" with false certainty `[inferred: `is_worker_detached` :5236-5238 returns False for any
identity not in the set]`.

---

## 3. The revive/resume path as it exists: three mechanisms, graded honestly

There are three distinct things in the codebase that a reader could reasonably call "re-attaching a
worker", plus a fourth that only looks like one. They are graded below with no generosity.

### 3.1 `detach()` / `resume()` — in-process revive, IMPLEMENTED and fenced (but thread-bound)

`stop()` is an alias of `detach()` `[verified: tools/workspace/worker_thread.py:2050, :2058]`. The
detach body does, in order: status `"detaching"` + status file write
`[:2071-2072]`, `_detach_requested = True` `[:2073]`, `_detached_at = time.time()` `[:2074]`,
`_park_event.set()` `[:2075]`, `_resume_event.clear()` `[:2076]`, `_turn_cancel.set()` `[:2081]`
(this cancels the in-flight *turn*, not the worker — `_stop_event` stays clear), `_save_context()`
`[:2085]`, `_set_detached_registration(True)` `[:2096]`, status `"detached"` + status file
`[:2097-2098]`, publish `WORKER_STOPPING` `[:2099]`, `write_command(worker_dir, "stop", fence)`
`[:2106]`, `_input_queue.put(_PARK_SENTINEL)` `[:2108]`
`[verified: tools/workspace/worker_thread.py:2058-2108]`, with an explicit ordering invariant
documented at `[:2089-2095]`.

`resume()` refuses — returns `False` and touches nothing — when `_stop_event.is_set()` or status is
`"killed"` `[verified: tools/workspace/worker_thread.py:2260]`; its docstring states that a DETACHED
worker IS revivable `[verified: tools/workspace/worker_thread.py:2257]`. It then: `wlm.resume()`
`[:2265-2271]`, `write_command(dir, "resume", self._fence())` `[:2290]` (the comment at `[:2272-2289]`
explains it never unlinks a live `command.json`; a fenced resume atomically replaces the stale pause
and is quarantined by a future epoch), `_park_event.clear()` `[:2291]`, `_resume_event.set()`
`[:2292]`, `_manual_only_pause = False` `[:2298]`, clears `_detach_requested` / `_detached_at`
`[:2301-2302]`, `_set_detached_registration(False)` `[:2303]`, `agent.leave_reading_mode("pause")`
`[:2307-2310]`.

The park loops live **inside** `run()`. First park at `[:3850-3912]`: set status `"detached"` if
`_detach_requested` else `"paused"` `[:3853]`, publish `worker_paused`, then

```
while self._park_event.is_set() and not self._stop_event.is_set():
    self._poll_command(); ...; self._heartbeat_tick(); self._resume_event.wait(1.0)
```

`[verified: tools/workspace/worker_thread.py:3887-3904]`, waking to status `"ready"` at `[:3908]`.
A second park loop of the same shape follows the tool loop at `[:4168-4192, :4185-4186]`. Both carry
the DEAF-PARK FIX comments `[:3888-3898, :4169-4176]`: a parked worker keeps polling `command.json`
so a cross-process `kill` terminates it and a cross-process `stop` detaches it.

**Grade: IMPLEMENTED, fenced, and correct — but only while the thread object is alive.** `resume()`
re-enters the *same* live thread. Containers, `_agent`, `_wlm`, the event-bus registration and the
`_input_queue` contents are all untouched because they were never dropped. That is precisely why
"a detached worker keeps its containers and queued queries" is true in-process, and precisely why
the statement is not a property of the *system* — it is a property of *not having restarted*.

`kill()` `[verified: tools/workspace/worker_thread.py:2110-2174]` is the only termination path: it
clears the detach markers `[:2170-2171]`, `_set_detached_registration(False)` `[:2172]`, and pushes
`None` onto the input queue `[:2174]`.

### 3.2 Adoption at spawn: crashed-turn journal replay, IMPLEMENTED (S7b-4a) — the one genuinely cross-restart mechanism

`_adopt_predecessor_journal(worker_dir, fence, generation)`
`[verified: tools/workspace/worker_thread.py:1236-1337]` is a module-level, hence monkeypatchable,
function. It reads with `read_inflight_unfenced`, adopts only records whose status is `inflight`
`[:1309]`, only when the on-disk epoch is **strictly lower** than this incarnation's generation
`[:1313-1317]`, and skips when `attempts + 1 > MAX_ATTEMPTS` `[:1318-1320]` (retry-budget territory
that the docstring assigns to the resume path). It then builds a bumped `InflightRecord`
`[:1321-1331]` and calls `adopt_inflight(worker_dir, fence, bumped)` `[:1332]`.

Why the successor owns the same directory: the spawn-side registry iteration filters `is_alive()`
`[verified: tools/workspace/worker_thread.py:5294-5300]`, so after a crash `live_same == []` and the
instance chain falls to `spawn_iid = 1` → bare worker name → `workers/<name>`, which *is* the dead
incarnation's directory `[verified: tools/workspace/worker_thread.py:1255-1267;
tools/workspace/worker_registry.py:129-139]`. The identity is reinstated by directory, not by a
lookup table.

Call site: `_run_tool_loop`, once per incarnation, before WRITE POINT 1 —
`if self._journal_adopted is None: _adopted_record = _adopt_predecessor_journal(...)`
`[verified: tools/workspace/worker_thread.py:3143-3148]`, log line
`"[journal] adopted query ... from a dead predecessor"` `[:3149-3152]`, then the dead turn is
**re-queued** as a `(query_id, query, None)` triple on `_input_queue` `[:3153-3187]` with a long
rationale at `[:3157-3181]`: the query id attributes the replay to the dead job so `wait_for_job`
can terminalize it, and `reply_q=None` falls back to the shared output queue.

**Grade: IMPLEMENTED, and it is the only path that actually re-attaches anything across a real
restart.** Two honest caveats:

- **It fires at turn start, not at boot.** Adoption requires the successor to already be executing a
  tool loop, i.e. somebody must have sent it a query. There is no boot-time scan that notices "this
  worker directory owes a replay" `[verified negatively: no other caller of
  `_adopt_predecessor_journal`; no `workers/*` scan anywhere outside `web_ui`, where the seven that do
  exist are read-only — §3.4]`.
- **`recoverable` is explicitly not adopted.** `recoverable` is the DETACH park's record and is
  the resume path's business `[verified: tools/workspace/worker_thread.py:1278-1279, :1310-1312]`.
  The docstring says "later slice"; grep found no other `adopt_inflight` caller, so treat
  resume-side replay as **MISSING** `[unverified: no resume-side replay implementation found]`.

The journal primitives it stands on are solid: `inflight` / `recoverable` / `cancelled` / `adopted`
statuses `[verified: tools/workspace/inflight_journal.py:134-145]`; `adopt_inflight` is the ONE
displacing writer and refuses cancelled / already-adopted / own-fence / attempt-count-mismatch
`[verified: tools/workspace/inflight_journal.py:525-600+]`; `write_inflight` fails closed on foreign
work-in-progress `[:429-507]`; the fenced vs unfenced reader distinction is documented at `[:33-73]`.

It is also already pinned by a test that this design's §7 will imitate:
`tests/test_worker_adoption_at_spawn.py` (616 lines) — successor adopts an `inflight` record from a
strictly older epoch once at turn start before WP1 via `_adopt_predecessor_journal`; the adopted
query is queued so the crashed caller's work RUNS exactly once; `cancelled` is terminal;
`recoverable` belongs to resume; nothing/corrupt/own-epoch ⇒ no adoption, no raise; plus a VACUITY
check with adoption neutered. No `thread.start()`, no sleeps, no Docker
`[verified: tests/test_worker_adoption_at_spawn.py:1-43]`, same pattern as
`tests/test_worker_journal_kill.py` (`_unstarted_thread` / `_wire` / `_ScriptedAgent`).

### 3.3 Re-attach after a real restart: MISSING

Taking the four requirements one at a time, none of them exists:

| Requirement | Status | Evidence |
|---|---|---|
| boot-time enumeration of worker directories | MISSING in the worker layer | seven enumerators exist, all in `web_ui`, all read-only, none creates a thread or registry entry: `bridge.py:3048` plus `workspace_routes.py:1326, :1419, :1625, :1841, :1961` and `health_routes.py:59`, each `for subdir in (ws_dir/"workers").iterdir()` `[verified]` |
| registry rebuild (a findable handle per worker) | MISSING | `WorkerRegistry` is in-memory only `[verified]` |
| container re-adoption / exemption reinstatement | MISSING | `_DETACHED_WORKER_IDENTITIES` starts empty `[verified: infra/container_manager.py:5215]` |
| detached-TTL clock restore | MISSING | `PeriodicStaleCheck` reads `thread._detached_at` off live objects `[verified: tools/workspace/worker_manager.py:208-240]` |
| queued-query / job restore | MISSING | `_input_queue` is a thread attribute `[verified: tools/workspace/worker_thread.py:2108, :2174]`; `WorkerJobRegistry` is an in-process `dict` with no serializer `[verified: tools/workspace/job_registry.py:53-65]` |
| resume reachable without a thread | MISSING | `WorkerManager.resume_worker` collapses to a silent `{"found": False, "action": "resume", ...}` no-op for an unregistered thread `[verified: tools/workspace/worker_manager.py:865-872]` |

What *is* available at boot, and is enough to build all six, is `status.json` + `context.json` +
the `generation` sidecar + the `thoughtmachine.worker` label (§2.1). The gap is a reader, not a
record.

### 3.4 The UI bridge catalogue: read-only re-attachment, PARTIAL

`web_ui/backend/bridge.py:3021-3067` `_load_worker_contexts()` scans `<ws_dir>/workers/*/context.json`
and fills `self._persisted_workers[name] = {"name", "context"}` (dict declared at `:624`); it is
called after `load_session()` `[:2397-2398, :2695-2697]` and cleared on session close `[:3005]`. The
docstring at `[:3026-3027]` says the point is "so that the bridge knows about idle workers that can be
resumed", and `resume_worker(name)` `[:3069-3080]` hands the persisted context dict back with the
docstring "The caller (typically the `Worker` tool) can use this to restore a worker's conversation
history before `spawn`" `[:3074-3075]`.

Four things it is not:

1. It is display/context only — it creates no thread, no registry entry, no container adoption.
2. It lives in `web_ui`, not in the worker layer, so a headless/MCP host gets none of it.
3. It is keyed by bare directory name, so `<name>` vs `<name>_<iid>` instance directories are
   conflated with respect to the registry's `(session, worker_name, instance_id)` key (§2.3).
4. It does not read `status.json`, so it cannot tell a `detached` worker from a crashed one — the
   exact distinction §4 says is dangerous.

**Grade: PARTIAL / read-only.** It is, however, proof that the enumeration is cheap and already
works in production, which is the strongest argument that §5 Option A is a small change and not a
new subsystem.

Relatedly, `web_ui/backend/workspace_routes.py:315-340` `_read_worker_generation(worker_dir)` reads
the `generation` sidecar with `context.json` fallback — proof that an operator/REST client can
already fence a command against a dead incarnation's epoch from disk alone.

**A second, stronger reader already exists: `web_ui/backend/health_routes.py:59-97`.** It walks
`(ws_dir/"workers").iterdir()`, reads each `status.json`, and returns a **workspace-wide**, not
session-scoped, catalogue per worker: `worker_name`, `session_id`, `runtime_status`,
`last_heartbeat`, `current_task` and the token counts
`[verified: web_ui/backend/health_routes.py:59-97, reading `data.get("runtime_status", "unknown")`]`.
That is Option A's L1 reader in miniature and it is already in production, already keyed the way the
sweep needs it (directory-derived, workspace-wide), and already reading the correct `status.json` key
(§3.5). It is still read-only: it creates no thread and no registry entry. What it does **not** do is
the part that costs real work in §5 Option A — it does not read the `generation` sidecar or
`context.json`, it does not live in the worker layer (so a headless/MCP host gets none of it), and it
is not fed to any sweep. So Option A's L1 half is an extraction and generalisation of shipping code
rather than a new subsystem, which is why its cost estimate in §5.1 is lowered accordingly — while the
L2 half stays expensive for a different reason (§5.1.1).

### 3.5 Claim audit: "a detached worker keeps its containers and queued queries"

This claim is load-bearing in the product narrative and it is **conditional**. Decomposed:

| Sub-claim | In-process | After a restart |
|---|---|---|
| the conversation survives | TRUE — `context.json` `[verified: :4846-4857]` | TRUE — reloaded by the successor's `run()` `[verified: :3671]` |
| the containers survive | TRUE — exemption via `_set_detached_registration(True)` `[verified: :2096]` → `register_detached_worker` `[verified: infra/container_manager.py:5218-5225]`, and the sweep skips on `_detached_state` `[verified: :5287-5311]` | **FALSE, at risk** — the set is empty in the new process `[verified: :5215]`, so `is_worker_detached` answers False `[verified: :5236-5238]` and the sweep proceeds |
| the containers are still *found* | TRUE — label `thoughtmachine.worker=<sid>:<name>` `[verified: tools/workspace/worker_container.py:30-51]` | TRUE — labels live in the daemon `[verified]` |
| queued queries survive | TRUE — they are entries in the thread's `_input_queue` `[verified: :2108]` | FALSE — the queue object does not exist any more `[inferred]` |
| the worker is resumable | TRUE — `resume()` re-enters the live thread `[verified: :2246-2310]` | FALSE — no thread to call it on; `WorkerManager.resume_worker` returns `{"found": False, "action": "resume", ...}`, a silent no-op a caller can mistake for success `[verified: tools/workspace/worker_manager.py:865-872]` |
| the worker is discoverable | TRUE — registry entry `[verified]` | FALSE — registry empty `[verified]` |
| TTL policing still applies | TRUE — `PeriodicStaleCheck._check_detached_ttl` `[verified: tools/workspace/worker_manager.py:208-240]` | FALSE — no thread to measure; the TTL clock silently resets, so a detached worker can outlive `WORKER_TIMEOUT_SECONDS` indefinitely `[inferred]` |

**Verdict.** In-process the claim is true for a structural reason: everything is one live object
graph, and detach only flips park flags and saves context
`[verified: tools/workspace/worker_thread.py:2058-2108]`. Across a restart the claim is false for a
structural reason too: each of the four sureties (exemption set, queue, registry entry,
`_detached_at`) is a Python object, and the only durable witness of "this worker was deliberately
detached" is `status.json["runtime_status"] == "detached"` `[verified, and the key is `runtime_status`
not `status`: written at tools/workspace/worker_thread.py:4767 (`"runtime_status": self.status`), frame
emit `:4801`, fresh-process reader tools/workspace/worker_runtime.py:953 (`data.get("runtime_status")`),
and the shipped REST reader web_ui/backend/health_routes.py:59-97
(`data.get("runtime_status", "unknown")`)]` — which nothing in the sweep path reads today
`[verified: the `worker_status` parameter exists at infra/container_manager.py:5256, but **no** caller
supplies it — the single production call site, infra/container_manager.py:2310, passes two positional
arguments only. The audit resolved the earlier open question: this is not "[unverified] whether any
caller does"; no caller does. See §5.1.1 for why the obvious fix there is wrong]`.

The consequence to state plainly: **a restart converts every detached worker into a crashed worker
from the sweep's point of view.** Whether its containers are actually destroyed then depends on
whether some boot-path code calls `cleanup_stale_worker_containers` for that owner identity. The
reap itself is forceful by construction: it routes through
`ContainerLifecycle.reconcile(..., ContainerIntent(kind="reap", explicit=True,
reason="stale_worker_cleanup", force=True))` `[verified: infra/container_manager.py:5343-5350]`,
after a `thoughtmachine.worker=<owner_identity>` label listing `[:5312-5316]`, an exact-label
re-check `[:5324]`, a `_gc_should_skip` for resource/lifecycle-owned containers `[:5328]`, and a
state filter that admits only `{'created','exited','dead'}` `[:5299, :5336]`.


---

## 4. Gap inventory

Each gap is stated as *what breaks*, *why it is dangerous* (or merely annoying), and its severity.
Severity vocabulary: **SEVERE** = destroys user data or running work; **SILENT** = lost work with no
error surfaced; **DEGRADED** = recoverable by a human with effort; **COSMETIC**.

### G1 — No boot-time enumeration or registry rebuild (SEVERE, root cause)

Nothing walks `workers/*` at boot and reconstructs any handle. There are **seven** enumerators in the
tree and all seven are in `web_ui` — `bridge.py:3048`, `workspace_routes.py:1326, :1419, :1625, :1841,
:1961` and `health_routes.py:59`, each a `for subdir in (ws_dir/"workers").iterdir()` `[verified]` —
and **none of the seven rebuilds a handle**: no `WorkerThread`, no registry entry, no container
adoption (§3.3, §3.4). So the gap is not "nobody can see the directories"; it is "seeing a directory
creates nothing callable". Everything else in this list is a consequence of G1: because no object
exists for a persisted worker, no *method* on that object can be called.

### G2 — The detached-container exemption is lost, so a detached worker looks crashed (SEVERE)

`_DETACHED_WORKER_IDENTITIES` is a module-level `set` `[verified: infra/container_manager.py:5215]`,
so the new process starts with it empty. `is_worker_detached` then returns `False` for a worker that
is in fact detached `[verified: infra/container_manager.py:5236-5238]`, and
`cleanup_stale_worker_containers` — which evaluates its gate *before* listing containers
`[verified: infra/container_manager.py:5287-5311]` — proceeds to the reap. The reap is
`explicit=True, reason="stale_worker_cleanup", force=True`
`[verified: infra/container_manager.py:5343-5350]`. This is the gap that turns "we support detaching"
into "we support detaching until the next deploy".

The mitigation looks closer than it is. The function already takes `worker_status`, and
`worker_status == "detached"` ⇒ skip `[verified: infra/container_manager.py:5267-5311]`; the data is
on disk, under `status.json["runtime_status"]` `[verified: tools/workspace/worker_runtime.py:953;
web_ui/backend/health_routes.py:59-97]`. What is missing is neither the parameter nor the data but a
**call site with the right purpose**: there is no boot-path caller of
`cleanup_stale_worker_containers` at all, and the single production call site is
`infra/container_manager.py:2310`, inside `ContainerManager.start()`'s pre-create self-heal, which
reaps on purpose so that "a name collision can never block a worker respawn"
`[verified: infra/container_manager.py:2305-2310]`. Passing `worker_status="detached"` **there** would
disable the self-heal for the very worker being respawned. So the discriminator cannot be
`worker_status` alone; it has to be the calling context's purpose — a supervisor/boot sweep reaps
nothing a persisted worker owns, a pre-create self-heal reaps by name. §5.1.1 carries this through to
the cost estimate and the recommendation.

### G3 — Queued queries are lost without an error (SILENT)

`_input_queue` is a `queue.Queue` attribute of the thread
`[verified: tools/workspace/worker_thread.py:2108, :2174]`; `WorkerJobRegistry` is an in-process
`dict` (`self._jobs`), populated from events, with no serializer on any path
`[verified: tools/workspace/job_registry.py:53-65]`. A detached worker may legitimately hold a backlog
(queries accepted while parked are queued, by design). After a restart that backlog is gone and nothing
records that it existed. The caller waiting in `wait_for_job` either times out or — worse — is not
waiting because it too died with
the process; the *only* durable trace of an accepted-but-never-run query would be the inflight
journal, which by construction only exists once a turn started writing

`[inferred: journal write points are inside `_run_tool_loop`, §3.2 WRITE POINT 1]`.
`[unverified: whether the accept path (`submit`/`request_worker`) records anything durable]`

### G4 — The detached-TTL clock resets, so nothing ever expires (DEGRADED → SEVERE over time)

`_check_detached_ttl` measures from `thread._detached_at`
`[verified: tools/workspace/worker_manager.py:208-240]`, and `PeriodicStaleCheck` enumerates
`self._registry.get_all_workers()` `[verified: tools/workspace/worker_manager.py:136-208]`. With an
empty registry the loop walks zero workers, so a detached worker's containers are never reclaimed by
TTL either — the mirror image of G2: the same restart makes reaping *too eager* for workers the sweep
happens to see by label, and *impossible* for workers it can only see via the registry. Both failure
directions come from the same missing re-attachment.

### G5 — `resume()` is unreachable without a live thread (SEVERE for the feature)

`WorkerManager.resume_worker` is a thin `_control_worker("resume", ...)`, and for an unregistered thread
it does **not** raise — `_control_worker` returns `{"found": False, "action": "resume", ...}`
`[verified: tools/workspace/worker_manager.py:865-872]`. `WorkerControlError` is raised only when a
thread *is* found but lacks a callable method `[verified: tools/workspace/worker_manager.py:861-887]`.
So after a restart there is no API to resume a detached worker — and the failure mode is **worse**
than the raise this section originally claimed: a caller receives a well-formed dict with
`found: False` and can log or report **success**, so a "resume after restart" path can silently do
nothing while the user believes the worker is running again. A loud `WorkerControlError` would be the
safer contract; the silent `found: False` no-op is a safety problem in its own right, independent of
whether re-attachment ever ships. Note
`worker_manager.py:919` ("thread.stop() now DETACHES … a manager stop that leaves the instance
running must call kill()") — the manager's own comments assume liveness, which is exactly the
assumption a restart violates.

### G6 — `recoverable` journal records have no replay path (SEVERE for detached-with-interrupted-turn)

`recoverable` is the DETACH park's record, and both the writer's docstring and the adopter's comment
route it to the resume path rather than to spawn
`[verified: tools/workspace/worker_thread.py:1278-1279, :1310-1312]`. No resume-side replay
implementation was found `[unverified: grep found no second `adopt_inflight` caller]`. So the one
journal status that describes "a human detached this mid-turn" is the one status nobody consumes.

### G7 — Adoption fires only at turn start, so recovery needs an unrelated query (DEGRADED)

Genuinely implemented (§3.2) but triggered from `_run_tool_loop`
`[verified: tools/workspace/worker_thread.py:3143-3148]`. Consequence: a crashed worker's turn is
replayed only when somebody sends it *new* work. A crashed worker with no follow-up traffic keeps its
`inflight` record forever, and the recorded query never runs. This is a liveness gap, not a
correctness gap — the exactly-once machinery is already right
`[verified: tools/workspace/inflight_journal.py:525-600+]`.

### G8 — Key mismatch between the disk catalogue and the registry (DEGRADED, blocks reuse)

The registry keys on `(session_id, worker_name, instance_id)`
`[verified: tools/workspace/worker_manager.py:463]`; the UI catalogue keys on the bare directory name
`[verified: web_ui/backend/bridge.py:3021-3067]`. Any design that wants to reuse the existing
enumerator must decide the instance-id question explicitly rather than inherit the conflation.

### G9 — The process-mode runtime advertises "stays lost" and contradicts its own docstring

For `MODE_PROCESS`, `ProcessWorkerRuntime`'s module docstring states: "There are no
worker_state/heartbeat frames (§10, S4), no container adoption (§3.4: the supervisor owns that, not
the socket), and no retry. A lost runtime is reported once and stays lost until a new
`ProcessWorkerRuntime` is built, because silently re-spawning a worker would discard the conversation
state the old process held." `[verified: tools/workspace/worker_runtime_process.py:24-34]`

But the same file binds an `on_frame` sink at construction `[:331]` and routes
`event`/`heartbeat`/`worker_state` `[:929-935]`. The frames do exist
`[verified: tools/workspace/worker_runtime_protocol.py:571-586 (`WorkerState`, `KIND="worker_state"`,
fields mirror `status.json` verbatim), :740-751 (frame kinds: hello, query_accepted, reply, event,
heartbeat, worker_state, cancel_ack, control_ack, config_ack, fatal, bye)]`. **Flag this
contradiction explicitly: the docstring is stale relative to the code.** What survives from the
docstring as a real constraint is (i) *no container adoption over the socket* — the supervisor owns
containers, not the child, and (ii) *a lost runtime stays lost* — no silent re-spawn, on the stated
grounds that re-spawning would discard the conversation the old process held. Note that ground is
weaker than it was, since the conversation is on disk (§1.1); what re-spawning would actually discard
is the *execution* half. That distinction is the whole of this document, restated in one sentence.

`WorkerRuntimeHandle.state()` prefers the frame cache and falls back to the `status.json` mirror
`[verified: tools/workspace/worker_runtime.py:843, :863, :866, :929, :969-985]`, and reports
`UNKNOWN_WORKER_STATUS = "unknown"` when it cannot tell `[verified: tools/workspace/worker_runtime.py:330]`
— the third tri-state in the system, and the same design instinct as `_detached_state()`'s `None`.

### Gap summary

| ID | Gap | Severity | Closed by |
|---|---|---|---|
| G1 | no boot enumeration / registry rebuild | SEVERE (root) | A |
| G2 | detached exemption lost ⇒ reap of detached worker's containers | SEVERE | A |
| G3 | queued queries lost silently | SILENT | C (+ durability work) |
| G4 | detached-TTL clock resets | DEGRADED→SEVERE | A |
| G5 | `resume()` unreachable after restart | SEVERE | C |
| G6 | `recoverable` has no replay path | SEVERE | C |
| G7 | adoption only at turn start | DEGRADED | C |
| G8 | dir-name vs instance-id key mismatch | DEGRADED | A (must fix as part of A) |
| G9 | process-mode "stays lost" + stale docstring | DEGRADED (doc hazard) | A (doc fix) + C |

---

## 5. Design options

### 5.0 Framing: three levels of "re-attachment"

Conflating these is how the current state got confusing. Any proposal must say which level it reaches:

- **L1 — describe.** A fresh process can *list* persisted workers and their last known status,
  without creating anything. Cheap, read-only, no fences involved. Today: PARTIAL (§3.4, in `web_ui`
  only).
- **L2 — protect.** A fresh process does not *destroy* things that belong to a worker it can only
  see on disk — chiefly: does not reap a detached worker's containers, and does not reset an
  inconvenient TTL to zero in the destructive direction. Today: MISSING (G2, G4).
- **L3 — revive.** A fresh process can put the worker back to work: rebuilt handle, adopted
  containers, replayed interrupted turn, delivered backlog. Today: MISSING (G1, G3, G5, G6, G7),
  except for the turn-start adoption of `inflight` (§3.2).

The dangerous gap is **L2**, not L3. L3 is a feature; L2 is a data-loss bug. A design that ships L1
and L2 and explicitly defers L3 is a good design. A design that ships L3 first is mis-prioritised.

### 5.1 Option A — boot-time re-attachment catalogue + status-aware sweep (L1 + L2)

**Shape.** At process start (or at session load, for the session-scoped variant), enumerate
`<workspace>/workers/*/`, and for each directory read `status.json` (status, `last_heartbeat`),
`context.json` (`generation`, `last_query`, `last_completed_query`) and the `generation` sidecar, and
build an immutable in-memory catalogue of `PersistedWorker` records:

```
PersistedWorker(
    worker_name, instance_id (parsed from dir or absent),
    worker_dir, generation,
    status,            # from status.json, may be missing/corrupt -> "unknown"
    last_heartbeat, last_query, last_completed_query,
)
```

Then:

1. **Feed it to a boot sweep that does not exist yet — §5.1.1.** This is the one place the first draft
   of this document was wrong, and the correction changes the shape of Option A.

   **§5.1.1 The missing piece is the call site, not the argument.** The sweep already takes
   `worker_status`, and `worker_status == "detached"` ⇒ skip
   `[verified: infra/container_manager.py:5267-5311]`; `"unknown"`/missing ⇒ do not reap (fail-closed,
   matching `_detached_state()`'s `None` semantics `[verified: infra/container_manager.py:5241-5253]`).
   But three facts change the picture:

   - **There is no boot-path caller of `cleanup_stale_worker_containers` at all** `[verified: the
     caller set was enumerated; it is empty]`. So "thread `worker_status` through the boot-path
     callers" describes a call site that does not exist.
   - **The single production caller is a pre-create self-heal, and it needs the reap.**
     `infra/container_manager.py:2310` calls
     `cleanup_stale_worker_containers(self.client, worker_name)` — two positional arguments, no
     `worker_status` — immediately before a fresh create, under a comment that states the intent:
     "Remove them BEFORE the fresh create so a name collision can never block a worker respawn"
     `[verified: infra/container_manager.py:2305-2310]`.
   - **Passing `worker_status` there would be a bug.** If that call site supplied
     `worker_status="detached"` for the worker being respawned, the gate would skip the reap and the
     respawn would collide with its own leftovers. Its *purpose* is destruction; a boot sweep's purpose
     is preservation. They must not share a path keyed only on `worker_status`.

   Therefore the discriminator is the **calling context's purpose**, not the argument's value: a new
   supervisor/boot sweep, living outside `ContainerManager.start()`, that asks "does any persisted
   worker own these containers?" and answers from the catalogue — while `ContainerManager.start()` keeps
   its unconditional self-heal untouched. Option A is thus **catalogue + a new call site**, and the new
   call site is the real unit of work: it needs an owner (the supervisor, §8 q2), a position in the boot
   order, and its own tests (T4/T5 must exercise the new sweep, not the existing self-heal).
2. **Expose it as the L1 surface** used by both the UI bridge and any headless consumer, replacing
   `bridge._load_worker_contexts()`'s private dict `[verified: web_ui/backend/bridge.py:3021-3067]`
   with one reader in the worker layer. Keep the bridge's dict as a cache, not the source of truth.
3. **Do not create threads.** No registry entries, no `WorkerThread` construction, no fabricated
   heartbeats. The catalogue is a *witness about disk*, not a worker.
4. **Report, don't act.** Optionally publish an event/log line per catalogued worker ("found persisted
   detached worker X at epoch N, containers exempt") so operators can see that the process noticed.

**Explicitly rejected sub-variants.** Do not register catalogue entries in `WorkerRegistry` under
synthetic epochs; do not let `list_workers()` return them. The registry's epoch CAS is the system's
identity discipline `[verified: tools/workspace/worker_manager.py:463]`; poisoning it with
disk-derived epochs invites exactly the double-writer bugs the fenced command channel exists to stop
`[verified: tools/workspace/command_channel.py:78-84]`.

**Cost (corrected: cheaper on the reader, dearer on delivery).** The reader is cheaper than first
estimated: `web_ui/backend/health_routes.py:59-97` already enumerates `workers/*`, reads `status.json` and
returns a workspace-wide per-worker catalogue (`worker_name`, `session_id`, `runtime_status`,
`last_heartbeat`, `current_task`, token counts), so L1 is an extraction and generalisation of shipping
code — add the `generation` sidecar and the `context.json` fields, move it into the worker layer — not a
new subsystem (§3.4). What is *not* cheaper is L2: one enumeration function **+ a new sweep call site
that does not exist yet** (§5.1.1) **+ its owner in the boot order** (§8 q2) **+ tests**. Still no new
state, no new thread, no new fence.

**Risk:** low on the reader. The only behaviour change is *fewer* reaps, i.e. strictly fail-closed. The
two things to get right are (a) the `instance_id`/dir-name question (G8): parse `<name>_<iid>`
deliberately, or key the catalogue by directory and let consumers resolve; and (b) **not** routing the
`ContainerManager.start()` self-heal through the catalogue (§5.1.1), which would convert a fail-closed
change into a fail-open one.

### 5.2 Option B — full rehydration: resurrect handles for persisted workers (L3, aggressive)

**Shape.** At boot, for every catalogued worker, construct a `WorkerThread` object (without starting
its thread) and register it, so `resume()`/`resume_worker()` work again, `PeriodicStaleCheck` sees
it, and the lifecycle observer gets a ring.

**Why it is attractive.** It closes G4 and G5 with the *existing* code paths: `PeriodicStaleCheck`
walks the registry `[verified: tools/workspace/worker_manager.py:136-208]`, and `resume()` only needs
a thread object with `_park_event` set and a valid fence (§3.1).

**Why it is dangerous, concretely:**

- `resume()`'s first act is `wlm.resume()` and `write_command(dir, "resume", self._fence())`
  `[verified: tools/workspace/worker_thread.py:2265-2290]`. A fabricated thread writing a `resume`
  command into a directory whose real incarnation is *gone* is fine; writing it into a directory whose
  real incarnation is *alive elsewhere* (two hosts, or a supervisor that respawned the child) is a
  second writer on a channel designed to have one, quarantined only by the fence
  `[verified: tools/workspace/worker_thread.py:2272-2289]`.
- A rehydrated thread has no `_agent`, no `_wlm` registration, no containers adopted
  (`ProcessWorkerRuntime` docstring: "no container adoption … the supervisor owns that"
  `[verified: tools/workspace/worker_runtime_process.py:24-34]`). So `resume()` succeeding would be a
  lie: it clears the park events `[:2291-2292]` on a thread that has no loop running to consume them.
- The heartbeat and TTL machinery reads thread attributes
  `[verified: tools/workspace/worker_manager.py:208-240, :371-394]`; a fabricated thread starts with
  a zeroed `_detached_at`, so a worker detached 5 minutes before the crash instantly looks freshly
  detached — the TTL is extended, not restored. Fixing that means adding a *restore* path to
  `PeriodicStaleCheck`, i.e. real new state, not reuse.

**Verdict: reject as stated.** The narrow salvageable idea inside it is "a *resume handle*" — an
object that is explicitly not a worker, only a target for `WorkerManager.resume_worker`, whose
`resume()` implementation means "ask the supervisor to spawn this worker with `intent=resume`". That
is Option C with extra steps; do C directly.

### 5.3 Option C — disk-driven revive: fenced command + spawn-with-intent (L3)

**Shape.** Make revive a *spawn* operation that reads disk state, not an operation on a live object:

1. A control surface (REST / MCP tool) resolves the worker directory from disk and reads its epoch
   with the same routine the REST layer already has:
   `_read_worker_generation(worker_dir)` — sidecar first, `context.json` fallback
   `[verified: web_ui/backend/workspace_routes.py:315-340]`.
2. It spawns the worker through the normal spawn path — the same path that already reinstates the
   identity by directory when `spawn_iid == 1` `[verified: tools/workspace/worker_thread.py:1255-1267;
   tools/workspace/worker_registry.py:129-139]` — with an explicit revive intent.
3. `run()` already loads `context.json` `[verified: tools/workspace/worker_thread.py:3671]` and merges
   the caller's initial context after pruning `[:3784-3790]`, so the conversation comes back with no
   new mechanism.
4. The *container* half is the supervisor's job (already stated as such:
   `[verified: tools/workspace/worker_runtime_process.py:24-34]`): before/concurrently with the
   spawn, the supervise layer must (i) not reap (Option A), and (ii) re-adopt, i.e. reinstate the
   detached registration if `status.json` says `detached`. Re-adoption is a one-liner against
   `register_detached_worker(owner_identity)`
   `[verified: infra/container_manager.py:5218-5225]` — but only *after* a successful spawn, or the
   exemption will outlive the intent.
5. The `recoverable` journal gap (G6) closes here: the revive path is the "resume side" that the
   `recoverable` docstrings have been pointing at
   `[verified: tools/workspace/worker_thread.py:1278-1279, :1310-1312]`. The new incarnation calls the
   adoption routine with `recoverable` admitted (relaxing `[:1309]`'s `inflight`-only filter) and
   replays through the same re-queue mechanism `[:3153-3187]`.

**Ordering is the whole design.** The safest sequence is:

```
read disk (status, generation)  ->  do NOT reap (A)  ->  spawn (context reload)  ->
mark detached-registered if status was detached  ->  adopt journal (inflight|recoverable)  ->
let the turn loop run the adopted query
```

Every step is already implemented except the *decision to start* at step 1 from disk, and the
`recoverable` relaxation at step 5. Note the trap: if step 3 fails, step 4 must not run — otherwise
the exemption set contains an identity with no worker, which is a quiet way to leak containers past
the sweep forever. Order 4 after 3, and make 4's failure non-fatal (it already is: the registration
helper never raises `[verified: tools/workspace/worker_thread.py:2176-2197]`).

### 5.4 Option D — do nothing, state the truth (L0)

Ship only documentation: "detach survives a restart only in the conversation dimension; operational
practice must be: resume before restarting the host, or accept container loss." Plus, if we want to
avoid destroying detached containers, the sweep hardening of §5.1.1 — which is **not** the one-argument
change this document originally claimed: the argument already exists and no boot call site does, so even
the hardening needs the new call site.

**When D is the right answer:** if the operational reality is that hosts restart rarely and detach is
short-lived. **When it is not:** if detach is advertised as "park a worker overnight", because then a
nightly deploy silently reaps its containers.

### 5.5 Recommendation

Ship **A now** (L1 + L2), with the `instance_id` key question (G8) decided as part of it, and **C as
the follow-on slice** (L3), which is where `recoverable` replay and backlog restoration belong.
**Reject B.** Treat **D** as the fallback if A is not funded.

**This recommendation survives the §5.1.1 correction, with its cost restated.** The correction removes
"one argument" from the pitch and adds "one new call site with an owner". The *ranking* does not move,
because the deciding fact is unchanged: **L2 is the only level that prevents data loss**, and it is
still the cheapest thing that does. What changes is the packaging — the L2 half can no longer be sold
as a one-line patch that ships without the catalogue, because it needs the catalogue's answer ("is this
worker persisted and detached?") *and* a place to ask the question. Consequently the previous
fallback ("ship the sweep hardening on its own if A is not funded") is withdrawn: standalone hardening
needs the same new call site and the same disk read, i.e. most of A for a fraction of the value. If A is
not funded, the honest recommendation is **D plus an explicit statement that detached containers are
reaped on the next restart** (§5.4).

Rationale in one line each:

- A is small, read-only, strictly fail-closed, and closes the only gap that destroys user work
  (G2) plus the silent one (G4).
- C reuses `run()`'s existing context reload, the existing directory-identity reinstatement, and the
  existing exactly-once adoption machinery; it adds a *trigger*, not a mechanism.
- B invents liveness, and every consumer of liveness in this codebase (`PeriodicStaleCheck`,
  `_heartbeat_age`, `WorkerLifecycleObserver`) reads thread attributes that a fabricated thread
  cannot honestly supply.


---

## 6. Invariants any implementation must preserve

These are not style preferences; each one is a fence the current code has already paid for. A
re-attachment feature touches the boundary between "disk" and "live object", so it is exactly the
kind of change that can quietly break them.

**I1 — One epoch, not two.** `CommandFence`'s epoch is the same per-worker counter that
`_allocate_generation()` maintains; the comment is explicit that it is "NOT a second counter"
`[verified: tools/workspace/command_channel.py:78-84]`, and the sidecar is folded with
`context.json["generation"]` on load
`[verified: tools/workspace/worker_thread.py:4560-4566, :4639]`. A catalogue that reports a
generation MUST read it from the same two sources in the same precedence, or fences computed from
catalogue data will be wrong.

**I2 — Fence-strict comparisons.** Adoption requires the on-disk epoch to be *strictly* lower than
this incarnation's `[verified: tools/workspace/worker_thread.py:1313-1317]`. A revive path must not
"helpfully" accept equal epochs; equal means "my own incarnation", and adopting your own record is the
double-run bug `adopt_inflight` refuses `[verified: tools/workspace/inflight_journal.py:525-600+]`.

**I3 — Durability before announcement.** `_write_status_file` publishes the `worker_state` frame only
after the file is durable and the lock is released
`[verified: tools/workspace/worker_thread.py:4778-4798]`. Any new writer of worker-visible state must
keep that order; a reader that consumes a frame as proof of disk state is entitled to that guarantee.

**I4 — Fail closed on unknown.** Three independent tri-states already encode this:
`_detached_state()` returns `True`/`False`/`None` with "None means cannot tell → do not reap"
`[verified: infra/container_manager.py:5241-5253]`; `UNKNOWN_WORKER_STATUS = "unknown"`
`[verified: tools/workspace/worker_runtime.py:330]`; absent/corrupt journal ⇒ no adoption, no raise
`[verified: tests/test_worker_adoption_at_spawn.py:1-43]`. A catalogue whose `status.json` is missing
or corrupt must yield `"unknown"`, and `"unknown"` must never authorise destruction.

**I5 — Exact-label ownership.** Container ownership is established by exact `thoughtmachine.worker`
label *value*, re-checked per container after listing
`[verified: tools/workspace/worker_container.py:30-51, :70+; infra/container_manager.py:5312-5316,
:5324]`. No prefix matching on `session:worker` — `worker` is a prefix of `worker2`.

**I6 — Exactly-once replay.** The adopted query is re-queued with the dead job's `query_id` so
`wait_for_job` can terminalise it, and with `reply_q=None` so replies use the shared output queue
`[verified: tools/workspace/worker_thread.py:3153-3187]`; attempts are bumped
`[:1321-1331]` and capped by `MAX_ATTEMPTS` `[:1318-1320]`. A revive path that replays must go through
`adopt_inflight`, not by re-reading and re-posting.

**I7 — Detach ordering.** `detach()` writes status `"detaching"`, saves context, then flips
registration and writes `"detached"`
`[verified: tools/workspace/worker_thread.py:2071-2108, invariant comment :2089-2095]`. A revival
path is the mirror image and must unwind in the reverse order, or a crash mid-revive leaves an
exemption registered for a worker that never came back (the §5.3 trap).

**I8 — No silent re-spawn.** A lost runtime stays lost until somebody builds a new one, on the stated
grounds that re-spawning discards the conversation the old process held
`[verified: tools/workspace/worker_runtime_process.py:24-34]`. A revive *feature* is not a violation
of this — the violation would be an automatic, unrequested re-spawn. Any reviver must be
operator/caller-initiated and observable.

**I9 — The catalogue is a witness, not a worker.** No thread construction, no registry insertion, no
fabricated heartbeats (§5.1, §5.2). `list_workers()` must keep meaning "live workers".

**I10 — Cheap gate before expensive work.** `cleanup_stale_worker_containers` evaluates its detached
gate *before* listing containers `[verified: infra/container_manager.py:5287-5311]`. Keep the
catalogue lookup on the pre-list side; making the gate require a container listing would turn every
sweep into a Docker round-trip even when nothing is at risk.

**I11 — Reset means reset.** `reset`/teardown deletes `context.json`, `status.json`, `command.json`
and the inflight file `[verified: tools/workspace/worker_thread.py:7402-7431, files loop :7423-7428]`.
A boot catalogue must therefore tolerate half-deleted directories (missing `context.json` with a live
`status.json` is a legal intermediate state during teardown) and must not resurrect a worker that a
concurrent reset removed. Conversely, spawn never deletes `context.json`
`[verified: tools/workspace/worker_thread.py:6308-6312]` — that is what makes revive possible at all;
do not "clean up" on spawn.

---

## 7. Test strategy

The pattern to reuse already exists and is the reason the adoption slice could be built without
flakiness: `tests/test_worker_adoption_at_spawn.py` (616 lines) drives the machinery with
`thread.start()` never called — an `_unstarted_thread` wired to a `_ScriptedAgent`, deterministic
`_wire` plumbing, no sleeps and no Docker — and includes a VACUITY check with the feature neutered so
the test cannot pass because it asserts nothing
`[verified: tests/test_worker_adoption_at_spawn.py:1-43]`, same shape as
`tests/test_worker_journal_kill.py`. Every test below must follow that shape.

### 7.1 The four test pillars

1. **Deterministic wiring.** Unstarted threads, scripted agents, direct calls to `_run_tool_loop`
   or the module-level adoption/catalogue functions. No `sleep`, no `thread.start()`
   `[verified: tests/test_worker_adoption_at_spawn.py:1-43]`.
2. **Fake container surface.** A fake Docker client + fake lifecycle recording `ContainerIntent`s, so
   "was it reaped?" is an assertion on recorded intents rather than a real daemon call
   `[inferred: the reap routes through `ContainerLifecycle.reconcile(... ContainerIntent(kind="reap",
   ...))` at infra/container_manager.py:5343-5350, which is the observable seam]`.
3. **Vacuity checks.** For every test that asserts "X did not happen", include a paired positive run
   with the guard disabled that *does* produce X — otherwise the test passes when the code path is
   never reached at all. This is the discipline the adoption tests already use
   `[verified: :1-43]`.
4. **Negative/robustness cases.** Missing file, corrupt JSON, own epoch, equal epoch, foreign fence,
   half-deleted directory (§I11) — each must be a no-op, not an exception
   `[verified: tests/test_worker_adoption_at_spawn.py:1-43 for the pattern]`.

### 7.2 Test list

Option A (L1 + L2):

| ID | Test | Asserts |
|---|---|---|
| T1 | catalogue over a synthetic `workers/` tree | one record per directory; fields read from `status.json` / `context.json` / sidecar; generation precedence sidecar-then-context (§I1) |
| T2 | catalogue robustness | missing `status.json` ⇒ `"unknown"`; corrupt JSON ⇒ `"unknown"`, no raise (§I4); missing `context.json` with live `status.json` ⇒ still catalogued (§I11) |
| T3 | catalogue is read-only | no `WorkerThread` constructed, registry unchanged before/after, no status frame emitted (§I9) |
| T4 | sweep skips a detached worker found only on disk | `cleanup_stale_worker_containers(..., worker_status="detached")` records zero `ContainerIntent` reaps |
| T5 | sweep still reaps a genuinely stale worker | VACUITY partner for T4: `worker_status="ready"` (or absent on-disk worker) ⇒ reap intent `reason="stale_worker_cleanup"`, `force=True` recorded, exactly as today |
| T6 | `"unknown"` fails closed | `worker_status` absent/`"unknown"` ⇒ no reap (§I4) |
| T7 | instance-id keying | a `<name>_2` directory and a `<name>` directory do not collide; the catalogue's key can be resolved to the registry's `(session, worker_name, instance_id)` (§G8) |
| T8 | exact-label discipline | a container labelled `sid:worker2` is not attributed to `sid:worker` (§I5) — the negative half of any label-driven lookup |

Option C (L3), when scoped:

| ID | Test | Asserts |
|---|---|---|
| T9 | revive triggers adoption of `recoverable` | the revived incarnation adopts the `recoverable` record exactly once and the query is re-queued with the original `query_id` (§G6, §I6) |
| T10 | revive ordering | successful spawn precedes detached re-registration; a failed spawn leaves the exemption set unchanged (§5.3, §I7) |
| T11 | fence computed from disk | a revive against a directory whose sidecar epoch is N writes its command at fence N+1 (the same epoch space as `CommandFence`, §I1) |
| T12 | no silent re-spawn | nothing in the boot path constructs a thread or emits a status frame for a catalogued worker (§I8, §I9) — the direct counterpart of the `worker_runtime_process` docstring constraint |

### 7.3 Documentation-verification tests

The claim "there is no boot-time scan" is a *negative* claim, so it should be pinned mechanically
rather than by prose. The test must assert three things:

(a) `_adopt_predecessor_journal` has exactly one production caller
`[verified now: tools/workspace/worker_thread.py:3143-3148]`;
(b) `workers/*` directory enumeration is **confined to `web_ui`** — `bridge.py`, `workspace_routes.py`,
`health_routes.py` — and appears nowhere in the worker layer (`tools/workspace/`, `infra/`, `session/`).
Note this is *not* the assertion this document originally proposed. The original wording — "enumeration
outside the worker package occurs only in `web_ui/backend/bridge.py`" — was **wrong, and as written the
test would have failed red on the day it was committed**: there are seven enumerators across three
`web_ui` modules (`bridge.py:3048`, `workspace_routes.py:1326, :1419, :1625, :1841, :1961`,
`health_routes.py:59`) `[verified]`. A one-file assertion would have produced a permanently red test
and, worse, a false "the doc is fiction" signal about a true doc;
(c) **none of those enumerators constructs anything**: no `WorkerThread(...)`, no `WorkerRegistry`
registration, no `register_detached_worker(...)` reachable from any of them (§3.4) — the property that
actually distinguishes "a listing" from "a boot scan", and the one a future slice will break.

If a future slice adds a boot scan or teaches an existing enumerator to build a handle, (b) or (c) goes
red and points the reader at this document. This is cheap and prevents the doc from silently becoming
fiction — the exact failure mode `worker_runtime_process.py:24-34` already exhibits (§G9).

### 7.4 What must NOT be tested with sleeps

Anything involving park loops. The park loops block on `_resume_event.wait(1.0)`
`[verified: tools/workspace/worker_thread.py:3887-3904, :4185-4186]` and poll `command.json`
(`_poll_command`, DEAF-PARK FIX `[:3888-3898, :4169-4176]`). Any test of revive semantics must drive
the event/command state directly rather than wait for a wall-clock tick.

---

## 8. Open questions for the main agent / product owner

1. **Is detach meant to survive a restart at all?** If detach is "park within my working session",
   Option A + documentation is complete and Option C is unnecessary scope. If detach is "park
   overnight", C is required. The current documentation does not settle this, and §3.5 shows the
   honest answer today is "only the conversation survives".
2. **Who owns the boot sweep in process mode?** `ProcessWorkerRuntime` assigns container adoption to
   the supervisor `[verified: tools/workspace/worker_runtime_process.py:24-34]`. If a supervisor
   process exists, Option A belongs there, not in the `WorkerThread`. If it does not yet exist,
   Option A's catalogue is the smallest thing that can live in either place.
3. **Should the catalogue be workspace-scoped or session-scoped?** `bridge._load_worker_contexts()`
   is session-scoped (loaded after `load_session()`, cleared on close
   `[verified: web_ui/backend/bridge.py:2397-2398, :2695-2697, :3005]`), but worker directories live
   under `<workspace>/workers/` `[verified: tools/workspace/inflight_journal.py:1-9]`. The sweep is
   workspace-and-label driven. A boot catalogue that is session-scoped will miss workers of sessions
   nobody loaded, which is precisely the set at risk of being reaped.
4. **G3 (durable backlog) — is it in scope anywhere?** Making accepted-but-unrun queries durable is a
   genuinely larger change than everything proposed above, and no mechanism currently records them
   `[unverified: accept-path durability]`. Naming it as a known limitation may be preferable to
   solving it now.

---

## Appendix A — items left unverified in this document

Listed so the next reader does not mistake silence for confirmation.

**Resolved in this revision** (kept visible so the change is auditable rather than quietly vanishing):

- **`WorkerJobRegistry` persistence — was item 1.** Resolved: the module *was* opened.
  `tools/workspace/job_registry.py:53-65` — `self._jobs: dict = {}`, populated from events, no
  serializer anywhere. It is verifiably in-memory, so §2.2's "in-process" and G3's "lost" are
  `[verified]`, not inference.
- **Callers supplying `worker_status` — was item 2, the "single highest-value follow-up check".**
  Resolved, and it changed §5.1: **no boot-path caller of `cleanup_stale_worker_containers` exists at
  all**; the single production caller is `infra/container_manager.py:2310`, inside
  `ContainerManager.start()`'s pre-create self-heal, passing two positional arguments, and it *needs*
  the reap `[verified: infra/container_manager.py:2305-2310]`. Consequence: G2's severity stands
  unchanged, but Option A is a catalogue **plus a new call site**, not a catalogue plus one argument
  (§5.1.1). The audited `status.json` key is `runtime_status`, not `status` (§3.5).
- **`resume_worker`'s failure mode — not listed before; found during the audit.** It returns
  `{"found": False, "action": "resume", ...}` for an unregistered thread rather than raising
  `[verified: tools/workspace/worker_manager.py:865-872]`. §3.5 and G5 are corrected, and G5's severity
  reasoning is now "silent no-op mistaken for success", not "loud raise".

Still open:

1. **Whether a resume-side replay for `recoverable` exists anywhere** — grep found no second
   `adopt_inflight` caller, so it is treated as MISSING, but "not found by grep" is not proof for
   dynamic dispatch or a differently-named helper.
2. **Whether the accept path (`submit` / `request_worker`) records anything durable** for a query that
   is queued but not started — determines whether §G3 is "silent loss" or "silent loss with a
   recoverable trace".
3. **`shutdown_workers` line numbers** — cited from the module's shape (~:350-380) rather than an exact
   read of each line; the comment at `:365` was read exactly.
4. **`worker_lifecycle.py` line numbers** — the observer's fields are cited by name, not by line; the
   file was read but per-field line anchors were not recorded.
5. **`WorkerRegistry` internals** — keys and the epoch CAS are cited by name
   (`_worker_registry`, `_worker_epochs`, `_worker_event_bus_registry`, `_bus_epochs`,
   `stored_epoch_for()`), without line anchors.
6. **`inflight_journal.adopt_inflight` end line** — cited as `:525-600+`; the body was read past 600
   but the terminal line was not recorded.

## Appendix B — cited locations, grouped

**Worker thread** `tools/workspace/worker_thread.py`: context path/read/write `:4474-4475`, `:4614-4645`,
`:4815-4881`; generation `:4477+`, `:4560-4566`, `:4609-4612`; run() bootstrap `:3671-3790`; pruning
`:4647-4678`, `:4680+`; status file `:4756-4813`; detach `:2050-2108`; kill `:2110-2174`; detached
registration `:2176-2197`; resume `:2246-2310`; park loops `:3850-3912`, `:4168-4192`; adoption routine
`:1236-1337`; adoption call site + re-queue `:3143-3187`; spawn chain `:5294-5300`; spawn never deletes
context `:6308-6312`; reset file deletion `:7402-7431`.

**Command channel** `tools/workspace/command_channel.py`: single-epoch rule `:78-84`.

**Journal** `tools/workspace/inflight_journal.py`: purpose `:1-9`; readers `:33-73`; statuses `:134-145`;
`write_inflight` `:429-507`; `adopt_inflight` `:525-600+`.

**Container manager** `infra/container_manager.py`: records `:53-59`; detached design comment `:5201-5214`;
set + helpers `:5215-5238`; tri-state `:5241-5253`; sweep `:5256-5360` (gate `:5287-5311`, states
`:5299`, `:5336`, label list `:5312-5316`, exact re-check `:5324`, `_gc_should_skip` `:5328`, reap
`:5343-5350`).

**Worker containers** `tools/workspace/worker_container.py`: label scheme `:1-12`, `:30-51`; labels
`:37`, `:41`; sweep `:70+`; record imports `:22-28`.

**Registry / manager / lifecycle** `tools/workspace/worker_registry.py` (in-memory dicts, atexit
`shutdown_workers`, `:365` comment, `:129-139` label);
`tools/workspace/worker_manager.py` (`PeriodicStaleCheck` `:136-208`, detached TTL `:208-240`,
`_heartbeat_age` `:371-394`, delegation `:430-887`, `_stop_thread` `:919`, module singletons
`:1001-1007`); `tools/workspace/worker_lifecycle.py` (rings, stale/hung, `WORKER_TIMEOUT`).

**Runtime seam** `tools/workspace/worker_runtime.py` (`MODE_*`, `UNKNOWN_WORKER_STATUS` `:330`, `state()`
`:843-866`, `on_frame` `:875-927`, `_mirrored_state` `:929`, `_status_json_path` `:969-985`);
`tools/workspace/worker_runtime_process.py` (docstring `:24-34`, `:331`, `:929-935`);
`tools/workspace/worker_runtime_protocol.py` (`WorkerState` `:571-586`, frame kinds `:740-751`).

**UI / REST** `web_ui/backend/bridge.py` (`_persisted_workers` `:624`, `_load_worker_contexts`
`:3021-3067`, call sites `:2397-2398`, `:2695-2697`, clear `:3005`, `resume_worker` `:3069-3080`);
`web_ui/backend/workspace_routes.py` (`_read_worker_generation` `:315-340`, `workers/*` enumerations
`:1326, :1419, :1625, :1841, :1961`); `web_ui/backend/health_routes.py` (workspace-wide worker
catalogue over `(ws_dir/"workers").iterdir()` reading `status.json` `:59-97`).

**Session store** `session/store.py` (`_save_session_metadata` `:311-344`, `save_session` `:346-350`,
`load_session` `:551-555`).

**Tests** `tests/test_worker_adoption_at_spawn.py:1-43` (docstring: what it pins, and the deterministic
no-thread/no-sleep/no-Docker pattern); `tests/test_worker_journal_kill.py` (same pattern).

