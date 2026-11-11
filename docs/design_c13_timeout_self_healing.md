# C13 — Persistent Container Timeout Self-Healing: Design Proposal

Design pass only. No production code, tests, or `PRIORITIES.md` were modified to
produce this document. All line numbers are snapshots of the working tree read at
design time, not stable addresses (per the convention stated at
`.thoughtmachine/knowledge/project/PRIORITIES.md:12-14`).

Verdict up front: the ticket's premise is partly stale. Self-healing for the *git
tool* path already exists (`infra/resource_container_manager.py:1477-1486`, reached
from `tools/git_info_tool.py:928` → `:1466`). What is actually broken is (i) the
needless `container.kill()` on timeout, (ii) the missing structured payload on the
timeout path (the ticket's `container_killed=False` describes the *interrupted*
path, not the timeout path), and (iii) the flat, non-retryable error the model sees.

---

## Ticket (verbatim)

Source: `.thoughtmachine/knowledge/project/PRIORITIES.md:66-82`.

```
- **C13: Persistent Container Timeout Self-Healing.** **Open.** Filed by Gen 61
  (dispatch 6) under the operator's Ruling 1. `ResourceContainerManager.exec`
  now has three outcomes: success; timeout (`container.kill()` but deliberately
  NOT remove, then `TimeoutError`); interrupted (returns a payload, container
  untouched). The timeout path leaves a **killed-but-retained** container:
  `_container_id()` resolves it by label, so every later git command on that
  workspace targets a dead container and fails until someone intervenes.
  Deliberately NOT fixed in C11-prime: the green tests
  `tests/test_resource_container_exec_timeout_survival.py` and
  `..._recovery.py` pin `container.removed == []` ("survive the timeout, do not
  delete"), and rebuilding the git sandbox on a timeout risks uncommitted work.
  **Ruling in force (Option A):** status quo plus explicit reporting in the
  payload (`container_killed=False`, `container_removed=False`). Do not
  self-heal via `ensure_container()` without a dedicated design pass.
  **Open questions for that pass:** who owns sandbox recreation; can uncommitted
  work be detected/preserved before teardown; should a timeout instead be
  surfaced to the model as a retryable condition rather than a hard failure. Also requires capturing the exec id via `exec_inspect` to allow true kill-on-cancel, since `exec_run` returns no killable handle and `ContainerManager.exec()` abandons the in-container PID by design (see C11).
```

This document is that dedicated design pass. The Option-A ban on *implementing*
self-healing via `ensure_container()` without a pass is respected: no code is
changed here, and the recommendation below deliberately keeps `ensure_resource`
as the single recreation owner rather than adding a second one.

### Corrections to the ticket's premise

1. **`container_killed=False` does not describe the timeout path.** It is a key of
   the *interrupted* payload, `infra/resource_container_manager.py:1633-1634`
   (inside `_interrupted_payload`, `:1620-1640`). The timeout path has no payload
   at all: it calls `container.kill()` and raises a bare
   `TimeoutError(f"Git resource command timed out after {timeout} seconds")` —
   `infra/resource_container_manager.py:1664-1671`. So Option A as literally
   written ("status quo plus explicit reporting in the payload") describes
   something that **does not exist on the timeout path**; it has to be built.
2. **"fails until someone intervenes" is stale for the git tool path.** Every git
   tool call first runs `ensure_resource("git")`
   (`tools/git_info_tool.py:928` → `_resolve_resource_execution`, `:1371-1476`,
   `self._resource_manager.ensure_resource("git")` at `:1466`), and
   `ensure_resource` restarts a found-but-not-running container at
   `infra/resource_container_manager.py:1477-1486` (the container is found with
   `all=True`, `:1500-1503`, so an exited container *is* resolved). The claim
   remains true only for a direct `exec()` caller that skips `ensure_resource`.
3. **`exec_inspect` is not implemented anywhere.** It appears in the repo as a
   comment only: `infra/container_manager.py:2586`. Grep over `infra/` returns that
   one match. NOT FOUND as code.
4. **No green test exercises the real failure.** The survival fake's `kill()`
   records the call but leaves `status == "running"`
   (`tests/test_resource_container_exec_timeout_survival.py:59-60`), and the
   recovery fake has no status mutation on kill at all. The "killed → exited →
   next `exec_run` raises" mode the ticket is about is therefore never
   reproduced by any green test.

---

## Code map

`infra/resource_container_manager.py` **exists** (2603 lines). The brief's file
reference is correct; no file correction needed.

### (a) Creation and registration of the persistent/resource container

- Single factory: `_create_resource_container()`
  (`infra/resource_container_manager.py:1036-1233`).
- Reached from `ensure_resource(name)` (`:1317-1492`), which is the public entry
  point: registry lookup `:1343` → policy check `:1351-1361` → image ensure
  `:1363-1372` → `_find_resource_container()` `:1375` → create-if-`None`
  `:1376-1391`.
- Discovery is by docker label, not by name index:
  `_find_resource_container()` `:1006-1034` and `_container_id()` `:1494-1514`
  (`client.containers.list(all=True, filters={"label":
  f"{WORKSPACE_LABEL}={workspace_id}"})` `:1500-1503`, matched on
  `CONTAINER_NAME_LABEL` + `RESOURCE_KIND` `:1509-1512`).
- Orchestration wrapper: `provision_workspace_resource()`
  (`:2449-2522`) returns `manager.ensure_resource("git")` `:2514`.
- A second, privileged twin factory exists for the orchestration path:
  `infra/container_registry.py:627-642` ("sanctioned resource-container factory"),
  whose docstring cites `resource_container_manager.py L404-546` for the same
  create path that now lives at `:1036-1233` — direct evidence that even in-repo
  line citations are dated snapshots.
- Naming/labels: `tm-res-<sha256(workspace_path)[:12]>-git`,
  `RESOURCE_IMAGE_TAG = "tm-resource-git"`
  (`tools/workspace/worker_execution.py:19-21`).

### (b) The idle/lifetime timeout: **NOT FOUND**

There is no idle timeout and no TTL for a resource container. Verified:

- `__init__` (`:917-944`) has no TTL/idle/max-age parameter. Fixed resources only:
  `mem_limit 512m` `:942`, `cpu_quota 50000` `:943`.
- The only sweeper, `sweep_stale_resource_containers(registered_workspace_ids,
  max_age_s=86400)` (`:2324-2446`), explicitly refuses to touch live workspaces:
  docstring `:2330-2331` — "Containers of registered workspaces are NEVER touched —
  even stopped ones (their workspace may just be idle)"; fail-closed on an empty
  registry `:2357-2366`; age gate `:2396-2406`. It never restarts and never
  reaps a registered workspace's killed container either.
- `cleanup_workspace_resources(workspace_id)` (`:2157-2299`) is workspace
  **teardown** only (unregister path): per-container funnel reason
  `workspace_cleanup` `:2214-2228`, non-destructive refusal logs and skips
  `:2229-2236`, `container.remove(force=True)` `:2237`.
- Remaining stop/remove triggers are all explicit calls: `stop()`
  `:1684-1747` and `remove()` `:1749-1818`, both funnelled with
  `resource_stopped` / `resource_removed` reasons.
- The one lazy-on-next-use mechanism is **drift**, not idleness: inside
  `ensure_resource`, staleness is image-id `:1396-1398`, workspace/kind label
  `:1399-1403`, and user drift `:1410-1411`; stale → funnel `:1423-1434` →
  non-destructive `host_fallback` refusal `:1442-1448` → remove `:1453` →
  recreate `:1462`.
- The codebase states the intended contract explicitly: `resource_status()`
  reports `detail="container stopped — will restart on first use"`
  (`:2138-2143`).

So: **nothing times a resource container out.** The lifetime trigger is a
caller-driven `ensure_resource` on next use, plus explicit teardown. Any "idle
timeout" belief in the ticket's framing belongs to the *ephemeral* manager, whose
`exec` does kill on timeout (`infra/container_manager.py:2664`) and drops the
container from its cache (`:2667`).

### (c) What a caller observes if a timeout already fired

Three different observations, depending on the path — this is the crux of the
ticket:

1. **Direct `exec()` on the killed container.** `exec` resolves the container with
   no status check and no restart (`:1547-1552`); `exec_run` against an exited
   container fails inside the worker thread; the exception is re-raised verbatim at
   `:1677-1678`. The caller sees a docker `APIError` (409/"container is not
   running" for `exec_run`; the precise status is `(inferred)` — this site does not
   translate it).
2. **Git tool path: silent revival.** Every git tool call runs
   `ensure_resource("git")` first (`tools/git_info_tool.py:928` →
   `_resolve_resource_execution` `:1371-1476` → `:1466`), and `ensure_resource`
   restarts a found-but-not-running container at
   `infra/resource_container_manager.py:1477-1486` (`if container.status !=
   "running": container.start()`). The tool therefore sees a normal result and
   **nothing tells the model a timeout ever happened**.
3. **The timeout error itself is flattened.** `TimeoutError` is raised at
   `:1669-1671`. At the tool boundary, `tools/git_info_tool.py:817-822` re-raises
   only `(RuntimeError, PermissionError)` (`:818-821`) and otherwise *returns*
   `f"Error executing git operation: {e}"` (`:822`), so the exception never reaches
   `agent/core/tool_executor.py` (whose catch-all would have produced
   `{'success': False, 'result': 'Error executing tool: ...'}` at `:836-837`).
   The model receives one flat string: no `timed_out`, no retryability marker, no
   container state.

### (d) Host bind mount, not container-local storage

The workspace is bind-mounted read-write into the resource container, so
uncommitted work lives on the **host**, not in the container filesystem:

- workspace rw bind mount — `infra/resource_container_manager.py:1064-1076`
- worktree main-repo rw bind mount — `:1077-1095`
- tmpfs for `/tmp` and `/home/agent` — `:1096-1104`
  (all inside `_create_resource_container`, `:1036-1233`).

Consequence: `kill()`, `remove()` and even full recreation cannot destroy
uncommitted work. What *is* container-local and lost on a kill is (i) any
in-flight process, and (ii) everything under `/tmp` and `/home/agent`.


---

## Q1 — Ownership: who recreates the sandbox?

### The real call stack

Git tool path (read and write both):

```
GitReadTool.execute()            tools/git_info_tool.py:658-822
 └ _run_git()                    tools/git_info_tool.py:824-882
    └ _run_git_raw()             tools/git_info_tool.py:884-943   (:928)
       ├ _resolve_resource_execution()  tools/git_info_tool.py:1371-1476
       │   ├ ResourceContainerManager(...)      :1455-1463
       │   └ ensure_resource("git")             :1466
       └ _exec_container_raw()          tools/git_info_tool.py:1129-1261
          └ ResourceContainerManager.exec(...)  :1238-1243
```

`GitWriteTool(GitReadTool)` (`tools/git_write_tool.py:102`) reuses the same
`_run_git_raw` at `:638`, `:1196`, `:1244`, `:1271`, `:1314`, `:1334`.

Call-site census: the **only** `ResourceContainerManager.exec` call site in the
tree is `tools/git_info_tool.py:1238`. All other `manager.exec(` hits in `tools/`
are on the *ephemeral* `ContainerManager`: `tools/container_control.py:370`,
`tools/visual_tool.py:486` and `:515`, `tools/docker_code_runner.py:452`,
`tools/workspace/check_system.py:739`. `ResourceContainerManager` is constructed
in exactly one place: `tools/git_info_tool.py:1455`.

### Recommendation: `ensure_resource()` owns recreation; `exec()` never creates

It already does, and the repo's own contract says so:

- `exec`'s docstring names the ensure path as the revival point:
  "on timeout the container is killed but NOT removed, so a later
  `ensure_container()` can restart it" (`infra/resource_container_manager.py:1523-1533`).
  (The claim is factually wrong today — see Corrections — but the *ownership
  intent* is unambiguous.)
- `ensure_resource` already owns exactly one recreation policy (drift funnel
  `:1396-1462`) plus one revival step (`:1477-1486`). Adding creation inside
  `exec` would give two policies that can silently disagree.
- `exec` cannot distinguish a single command from the middle of a multi-step
  operation: `GitReadTool` issues several `_run_git_raw` calls per tool call
  (`tools/git_info_tool.py:638`, `:1196`, `:1244`). An `exec`-level recreate
  could fire mid-sequence, invalidating `workdir`/environment assumptions held by
  the caller.
- The failure mode the ticket forbids — recreation destroying uncommitted work —
  applies to *any* creator. Keeping the only creator on the `ensure_resource`
  drift path means recreation fires only when the image/labels/user actually
  changed, never merely because a command was slow.
- Empirical constraint: the recovery fake has no `images` and no `containers.run`
  (`tests/test_resource_container_exec_timeout_recovery.py:92-99`) and pins
  `_ensure_resource_image` to a constant (`:126`, `:139`); it asserts
  `seed.exec_runs == 2` on the *same* container object (`:166`). Any
  create-from-`exec` is untestable in that harness and instant red.
- A supervisor above (`WorkerThread`, `ToolExecutor`) is the worst owner: no
  docker knowledge, and its catch-all flattens everything to a string
  (`agent/core/tool_executor.py:836-837`).

**Concrete shape:**

1. `exec()` gains **no** creation, **no** removal. It may restart *in place*
   (`container.start()`) behind `start_if_stopped: bool = True` — idempotent, uses
   only the existing container handle, and is a no-op on the git path because
   `ensure_resource` already restarted at `:1477-1486`.
2. `ensure_resource()` is unchanged: sole creation/recreation owner.
3. `sweep_stale_resource_containers` stays hands-off for live workspaces
   (`:2330-2331`); `cleanup_workspace_resources` (`:2157-2299`) stays
   teardown-only, and must not grow a "revive" branch.
4. Callers wanting fail-fast semantics pass `start_if_stopped=False`.

Note: because the git tool is the only current `exec` consumer, the "later git
commands on a dead container" risk is already covered on the production path
(`tools/git_info_tool.py:928` → `:1466` → `:1477-1486`). The ownership question is
therefore mostly about **future** callers — which is exactly why the rule belongs
in `exec`'s contract, not in each caller.


---

## Q2 — Work preservation: can uncommitted work be detected/preserved before teardown?

### The mechanism that exists: the work is not in the container

The repository is a host bind mount, not a copy into the container image:

- workspace rw bind mount — `infra/resource_container_manager.py:1064-1076`
- worktree main-repo rw bind mount — `:1077-1095`
- tmpfs `/tmp` + `/home/agent` — `:1096-1104`

Therefore every git-visible artifact — working tree, index, refs, objects, and any
lock files — lives on the **host**. `kill()`, `stop()` (`:1684-1747`),
`remove()` (`:1749-1818`) and recreation (`:1453`, `:1462`) all operate on the
container; none of them unmounts or deletes the bind mount. **No preservation
machinery is required for the timeout path, because the timeout path removes
nothing** (`:1664-1668` kills the container process and retains the container
object; the kill itself is best-effort with exceptions swallowed).

### What is genuinely NOT recoverable

1. **In-flight processes.** A killed `git` process is gone; any partially executed
   multi-step operation (e.g. a rebase/merge mid-sequence) stops where it is.
2. **tmpfs contents.** `/tmp` and `/home/agent` (`:1096-1104`) are tmpfs: every
   scratch file, credential helper state, and any tool caches there are lost on
   kill, and are fresh (empty) after a restart — even though the bind-mounted repo
   is intact.
3. **Lock residue — the one durable harm.** Because `.git` is on the host mount, a
   `git` process killed at the wrong instant can leave `.git/index.lock` (and
   sibling `*.lock` files) behind *on the host*, and nothing in
   `infra/resource_container_manager.py` ever clears them: `stop()` `:1684-1747`,
   `remove()` `:1749-1818` and `cleanup_workspace_resources()` `:2157-2299` issue
   docker verbs only. The next git command then fails with git's own
   "Unable to create '.../index.lock': File exists" (`(inferred)`: standard git
   behaviour; not reproduced here — no git binary used in this pass). This failure
   *survives container restart*, so `ensure_resource`'s revival at `:1477-1486`
   does not fix it. It is not mentioned anywhere in the ticket.

   Git's object store is not the risk: git writes loose objects to temp files and
   renames them, so SIGKILL mid-write leaves garbage temp files, not corruption
   (`(inferred)`).

### Detection: NOT FOUND

There is no pre-teardown inspection of the working tree anywhere. No caller runs
`git status --porcelain`, reads the index, or probes for lock files before
`stop()`/`remove()`/recreate. The only pre-teardown gates that exist are
infrastructural, not work-related: the destructive-action funnel used by
`stop`/`remove`/`cleanup_workspace_resources` (`:2214-2228`) and the
non-destructive refusal branch (`:2229-2236`, `:1442-1448`).

### Recommendation

- **Do not build write-detection gating.** The ticket's stated fear ("rebuilding
  the git sandbox on a timeout risks uncommitted work") is not supported by the
  mount configuration: files are on the host and survive. Gating on a
  `git status` probe would add a git invocation into a teardown path where git may
  itself be the hung component — a deadlock-shaped design.
- **Do build the two cheap things that are actually missing:**
  (i) on the revive/restart path, a stale-lock probe (`<repo>/.git/index.lock`
  existence + age) with an explicit refusal or a surfaced diagnostic rather than a
  silent second failure — this turns the *real* residue into a legible error;
  (ii) report `tmpfs_reset: true` and `container_restarted: true` in the timeout
  payload so the model knows scratch state is gone while the working tree is not.
- If a future ticket wants true preservation, the correct primitive is
  `git stash --include-untracked` executed **from the host side** on the bind mount
  before any recreate, because that is the only actor guaranteed to be alive when
  the container is not. That is out of scope here.


---

## Q3 — Retryability: should a timeout reach the model as retryable?

**Yes — but only as a *returned structured value*, never as an escaping exception
class.** Today the model gets a flat sentence with no state and no marker
(`tools/git_info_tool.py:817-822`).

### Constraints that pin the shape

1. `GitReadTool.execute()` returns `str` (`tools/git_info_tool.py:658`).
2. `_run_git()` returns `str` (`:829`) and explicitly forbids a "timed out" string
   as the failure signal at *that* layer: `:861-869` re-raises
   `subprocess.TimeoutExpired` and `TimeoutError` with the rationale that callers
   parse the return value as a branch/ref and a returned "timed out" string would
   be treated as a valid ref and open a security gate.
3. An exception escaping the tool is flattened by
   `agent/core/tool_executor.py:836-837` into
   `{'result': 'Error executing tool: ...', 'success': False}` — no fields survive.
   So a new exception class alone cannot carry retryability to the model.
4. Precedent for structured timeout results already exists:
   `tools/container_control.py:164-166` (`_respond`), `:398`
   (`timed_out = result["exit_code"] == -2`), `:412-419`
   (`except TimeoutError: _respond(False, exit_code=-2, timed_out=True, ...)`),
   and `tools/docker_code_runner.py:518-525`.

### Exact signal shape — three layers, one payload

**Layer 1 — the manager raises, and attaches a payload.**
`infra/resource_container_manager.py:1669-1671` keeps
`raise TimeoutError(...)` (the survival test asserts
`pytest.raises(TimeoutError)`, `tests/test_resource_container_exec_timeout_survival.py:173-174`;
returning a dict instead would turn it red) and adds an attribute:

```python
exc.container_payload = {
    "timed_out": True, "exit_code": -2, "timeout_seconds": timeout,
    "container_id": container.id, "container_status": container.status,
    "container_killed": <bool>, "container_removed": False,
}
```

Field names deliberately match the existing interrupted payload
(`infra/resource_container_manager.py:1620-1640`, keys at `:1633-1634`) so one
schema describes both non-success paths.

**Layer 2 — the tool translates.** `_exec_container_raw`
(`tools/git_info_tool.py:1129-1261`) currently catches only the interrupted case
(`:1255-1260`) and lets `TimeoutError` through. Add a `TimeoutError` branch that
re-raises `GitExecutionTimeout(RuntimeError)` carrying `container_payload`.
`RuntimeError` is load-bearing: `:818-821` re-raises exactly
`(RuntimeError, PermissionError)`.

**Layer 3 — `execute()` returns JSON text.** Catch `GitExecutionTimeout` ahead of
the generic handler at `:817-822` and *return*:

```json
{"success": false, "timed_out": true, "exit_code": -2, "retryable": true,
 "operation": "log",
 "error": "git command timed out after 30s",
 "container": {"id": "…", "status": "running", "killed": false, "removed": false},
 "guidance": "The git sandbox container is alive; retry the same operation."}
```

`execute() -> str` (`:658`) is preserved; the value is model-facing text, exactly
as `tools/container_control.py:412-419` already does.

### Why this does not violate rule 2

`_run_git`'s ban (`:861-869`) is about internal callers parsing a returned string
as a ref. The JSON is emitted only at the outermost tool boundary, after every
internal ref-parsing call has completed, and `_run_git`/`_run_git_raw` keep
raising. Internal callers never see the JSON.

### Retryable semantics

- `retryable: true` for read-only verbs only (status/log/diff/show/rev-parse/…).
- For mutating verbs (shared path: `GitWriteTool(GitReadTool)`,
  `tools/git_write_tool.py:102`, calls at `:638`, `:1196`, `:1244`, `:1271`,
  `:1314`, `:1334`), emit `retryable: false` plus
  `"may_have_partially_applied": true` — a timed-out `commit`/`merge`/`rebase` is
  not safely retryable. `execute()` knows the verb, so the discrimination belongs
  there.
- Reuse the existing vocabulary where it exists (`timed_out`, `exit_code: -2`);
  `retryable` is new for tools (the only prior mention is a docstring at
  `tools/workspace/worker_runtime_process.py:86`) — call this out in review as a
  convention introduction rather than silently coining it.
- **No automatic retry loop** in the tool layer. The marker is advisory; an
  internal retry would re-enter a possibly-hung container and multiply the leaked
  worker threads described in Q4.


---

## Q4 — Kill handle: how `exec_inspect` interacts with the timeout path

### Confirming the brief's references (they are approximately right; exact lines below)

`infra/container_manager.py`, `ContainerManager.exec()` spans `:2505-2680`:

- worker thread that performs `exec_run` is created and started at `:2561-2571`
  (the exact `exec_run(...)` statement sits inside that thread body — `(inferred)`
  to a ±3-line window);
- the explanatory comment is at **`:2581-2588`**, not 2574-2581: "``exec_run``
  hands us no handle to signal … Real cleanup would need the exec id captured from
  ``exec_inspect`` (or the in-container pid) and signalled explicitly -- future
  work";
- slice polling from `:2589`; interrupted payload `:2615-2629`;
- timeout path: `container.kill()` `:2664`, `self._drop_container(container_id)`
  `:2667`, `raise TimeoutError(f"Command timed out after {timeout} seconds")`
  `:2668`;
- `_drop_container()` `:4794-4797` **pops name-index entries only** — it issues no
  docker removal. So the ephemeral precedent is already "kill + forget, container
  retained in docker".

So: **the brief's line numbers are ~7-10 lines low but the substance is correct**;
`exec_run` yields no killable handle and the in-container PID is abandoned by
design.

### What the missing handle forces

Because `exec_run` returns only after the exec finishes, at the deadline the only
primitives available are container-level. That is exactly why the timeout path
kills the container: `infra/resource_container_manager.py:1664-1668` (kill,
exceptions swallowed) and `infra/container_manager.py:2664`. **C13's symptom —
"killed-but-retained container" — is a direct consequence of the missing kill
handle, not an independent bug.**

### Does a hung exec block the timeout reaper? No — but it leaks

- The reaper is the wait loop inside `exec`
  (`infra/resource_container_manager.py:1644-1662`): it polls
  `thread.is_alive()` in `_WAIT_SLICE_SECONDS = 0.5` slices (`:1594`) and breaks on
  the deadline (`:1661-1662`). It never joins, so a hung `exec_run` cannot block
  it. Same shape in `ContainerManager.exec` (`:2561-2571`, `:2589`).
- What a hung exec *does* do: the worker thread stays alive holding an open docker
  exec stream. Threads are daemon (`:1568-1578`), so if the container is not killed
  the thread and its socket survive until process exit — **one leaked thread +
  socket per timed-out exec**. `_drop_container` (`:4794-4797`) compounds this on
  the ephemeral path by forgetting the container while the thread still holds it.
- Worse for git specifically: a hung `git` process still holds
  `.git/index.lock` on the host bind mount. If the container is *not* killed, the
  lock is **held**, not merely stale, so the model's retry queues behind it and
  fails again for a different reason. This is the argument that kills the naive
  reading of the green tests ("survive the timeout, do not delete").

### Recommendation — three options, and the one to pick

| Option | Behaviour at deadline | Container afterwards | Hung process | Verdict |
|---|---|---|---|---|
| A status quo | `container.kill()` `:1664-1668` | exited, retained, revived only via `ensure_resource:1477-1486` | SIGKILLed, locks left as files | fragile; needs the revival dependency |
| B **restart in place** | `container.restart()` (fallback `kill()`+`start()`) | **running**, same object, same bind mounts | SIGKILLed, locks left as files | **recommended** |
| C abandon, never kill | return/raise, thread left running | running | **alive, holds the lock** | trap — only viable once a PID kill handle exists |

**Pick B.** `restart()` closes the exited-window entirely: `_container_id()`
(`:1494-1514`) and `ensure_resource`'s restart step (`:1477-1486`) become
irrelevant to correctness, no concurrent `exec` can observe an exited container,
and the "who owns recreation" question stops being load-bearing. Cost is identical
to A in work terms (bind-mounted files survive, `:1064-1095`; tmpfs and in-flight
processes are lost either way) plus one container start latency.

Option C is what the survival/recovery tests superficially endorse, and it is the
one to reject: without `exec_inspect` there is no way to terminate the hung
process, so "do not kill" converts a recoverable dead container into a live
container with a poisoned repo lock.

### How `exec_inspect` changes the picture (separate ticket)

With the exec id captured (docker-py `client.api.exec_create` →
`exec_start` → `exec_inspect` for `Pid`), the manager can `kill -TERM <pid>` inside
the container instead of killing the container, which *releases the git lock* while
keeping the sandbox alive — the only option that is simultaneously safe for the
container and for the repo. It requires bypassing `exec_run` (the id must be known
**before** the blocking call), and it is named as future work at
`infra/container_manager.py:2586`. It is NOT implemented anywhere:
`exec_inspect` has exactly one hit in `infra/` — that comment. NOT FOUND as code.

Sequencing: ship B now (it removes the C13 symptom with no new capability); ship
`exec_inspect` later (it removes the leaked-process/lock residue and unlocks true
kill-on-cancel for C11). B is not made obsolete by `exec_inspect`; when the handle
lands, B degrades to the fallback branch.


---

## Risks and unknowns

1. **The fakes have no `restart()`.** The survival fake records
   `started/removed/killed/stopped` (`tests/test_resource_container_exec_timeout_survival.py:48-51`)
   and implements `kill()` `:59-60` and `remove()` `:65-66`, but no `restart()`.
   Calling `container.restart()` on it raises `AttributeError`. It must be guarded
   (`hasattr` + fallback to `kill()`+`start()`) and kept inside the existing
   best-effort `try/except` at `infra/resource_container_manager.py:1664-1668`, or
   the fakes must be extended in the same change.
2. **`pytest.raises(TimeoutError)` is load-bearing.**
   `tests/test_resource_container_exec_timeout_survival.py:173-174` forbids
   replacing the raise with a returned dict at the manager layer. All payload work
   must ride on exception attributes.
3. **`ensure_resource`'s restart (`:1477-1486`) is currently the only thing keeping
   the git path alive, and it is untested.** No test asserts that an *exited*
   container is restarted there; the recovery test's fake never leaves the exited
   state (`(inferred)` from its lack of status mutation). Option B removes the
   dependency but does not test this path either — flag as a coverage hole.
4. **Lock residue after any kill.** A killed `git` leaves `.git/index.lock` on the
   host bind mount (`:1064-1095` mounts the repo rw); nothing in the manager clears
   it (`stop() :1684-1747`, `remove() :1749-1818`, `cleanup_workspace_resources()
   :2157-2299` are docker-only). A restart does not remove it either, so a retry
   can fail with git's own lock error after the timeout is "fixed". `(inferred)`
   from standard git behaviour; not reproduced in this pass.
5. **Exact docker error for `exec_run` on an exited container** is `(inferred)` to
   be `APIError` 409. `infra/resource_container_manager.py:1677-1678` re-raises
   whatever the worker caught, unmodified.
6. **Model-facing text change.** Emitting JSON from `execute()` (`:817-822`)
   replaces `"Error executing git operation: …"`. No survey was made of code or
   prompts that grep that prefix — **NOT VERIFIED**.
7. **`retryable` is a new tool-level vocabulary word.** Prior art is docstring-only
   (`tools/workspace/worker_runtime_process.py:86`). Introducing it is a (small)
   convention decision, not an established pattern.
8. **Restart discards tmpfs.** `/tmp` and `/home/agent` (`:1096-1104`) come back
   empty; any tool relying on scratch state across calls breaks silently. Mitigate
   with the `tmpfs_reset` hint in the payload.
9. **Concurrency.** A hand-rolled `kill()`+`start()` leaves an exited-window in
   which a concurrent exec (a second `GitReadTool` instance in another worker —
   `(inferred)` possible, each instance builds its own manager at
   `tools/git_info_tool.py:1455`) sees the container down. `container.restart()` is
   the narrower window; prefer it when available.
10. **Same-cost judgement, not measured.** "Restart is cheaper than the current
    kill-then-lazy-revive" is an architectural argument here, not a benchmark.
    No timing was taken in this pass.

---

## Minimum viable step ordering

Each step is independently shippable and independently testable. No step removes a
green test.

**Step 0 — Documentation truth (no behaviour change).**
Fix the false docstring at `infra/resource_container_manager.py:1523-1533` ("…so a
later `ensure_container()` can restart it") and the comment at `:1588-1593` to state
what the code actually does: kill, retain, raise `TimeoutError` with no payload,
and revival happens only if the caller goes through `ensure_resource`
(`:1477-1486`).
*Pins it:* nothing — a doc-only change is not test-pinned. The pair
`tests/test_resource_container_exec_timeout_survival.py:178` and
`tests/test_resource_container_exec_timeout_recovery.py:166` must remain green to
prove no behaviour moved with the wording.

**Step 1 — Attach a payload to the timeout raise (no behaviour change).**
At `:1669-1671`, build the payload dict (schema from Q3) and attach it as
`exc.container_payload`, keeping `TimeoutError` and keeping the `kill()` at
`:1664-1668`.
*Pins it:* a NEW test (e.g. `tests/test_resource_container_exec_timeout_payload.py`)
asserting `container_killed is True`, `container_removed is False`,
`exit_code == -2`. The survival test's `pytest.raises(TimeoutError)`
(`:173-174`) and `removed == []` (`:178`) stay green unchanged.

**Step 2 — Restart in place instead of leaving the container dead.**
Replace the bare `kill()` at `:1664-1668` with `restart()`-if-available /
`kill()`+`start()`-fallback, still inside the swallowing `try/except`, and flip the
payload's `container_killed` accordingly.
*Pins it:* the two existing assertions
(`tests/test_resource_container_exec_timeout_survival.py:178`,
`tests/test_resource_container_exec_timeout_recovery.py:159` and `:166`) plus a NEW
assertion that `started` was recorded and `killed == []` in the restart path — which
requires adding a `restart()` method to the fake (`:48-51`, `:59-60`).

**Step 3 — Translate the timeout at the tool seam.**
In `_exec_container_raw` (`tools/git_info_tool.py:1129-1261`), catch `TimeoutError`
and re-raise `GitExecutionTimeout(RuntimeError)` carrying the payload; leave the
`RuntimeError` contract at `:818-821` untouched.
*Pins it:* a NEW tool-level test with a stub manager whose `exec` raises
`TimeoutError` with a payload, asserting the raised type and that the payload
survives. No existing test covers `:1129-1261`'s timeout path — this is net-new
coverage.

**Step 4 — Structured, retryable, model-facing result.**
Catch `GitExecutionTimeout` ahead of the generic handler at
`tools/git_info_tool.py:817-822` and return the JSON document described in Q3.
*Pins it:* a NEW test asserting
`json.loads(result)["timed_out"] is True`, `["retryable"] is True` for a read verb
and `False` for a write verb, and `["exit_code"] == -2`; plus a regression
assertion that `_run_git` still *raises* (`:861-869`) rather than returning text.

**Step 5 — Stale-lock probe (optional, low risk).**
A helper that, on the revive/restart path, detects `<repo>/.git/index.lock` and
surfaces a diagnostic instead of letting the next git command fail obscurely.
*Pins it:* a NEW pure-unit test on the helper (filesystem fixture, no docker).

**Step 6 — Separate ticket: `exec_inspect` kill handle.**
Capture the exec id (`client.api.exec_create` → `exec_start` → `exec_inspect`) so a
hung command can be signalled by PID. Not part of C13's minimal fix; see Q4.

Rationale for the order: steps 0-2 are inside one file and provably
green-test-preserving; step 3-4 cross the tool boundary and need new tests; step 5
is orthogonal; step 6 is a different capability with a different blast radius.

---

## Explicit non-goals

- **No remove+recreate on timeout.** It breaks
  `tests/test_resource_container_exec_timeout_survival.py:178` and
  `tests/test_resource_container_exec_timeout_recovery.py:159`/`:166`, and it is
  the exact self-healing the ticket bans without a design pass.
- **No host fallback on timeout.** `tools/git_info_tool.py:944-947` deliberately
  fails loud for `host_fallback`/`unavailable`; a timeout must not be re-routed to
  host git.
- **No new TTL / idle-timeout knob.** `infra/resource_container_manager.py:917-944`
  gains no parameter; "containers of registered workspaces are NEVER touched"
  (`:2330-2331`) stays true.
- **No change to `cleanup_workspace_resources()` (`:2157-2299`) or
  `sweep_stale_resource_containers()` (`:2324-2446`).** Neither becomes a reviver.
- **No `exec_inspect` implementation in this pass** (comment-only today,
  `infra/container_manager.py:2586`); it is step 6 / a separate ticket.
- **No change to `ContainerManager.exec()` (`:2505-2680`).** The ephemeral
  kill+`_drop_container` (`:2664`, `:2667`, `:4794-4797`) is correct for a
  throwaway container and shares none of C13's constraints.
- **No automatic retry loop** anywhere in the git tool layer; the marker is
  advisory to the model.
- **No write-detection / stash gating** before teardown (Q2: the bind mount makes
  it unnecessary and it would put a git call inside the code path that runs when
  git is the hung component).

