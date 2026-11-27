# A worker is a process. The pause stops at the door of a running tool.

**Report to the engineer · 2026-11-26 · measured on this machine, not recalled.**
Every claim below carries a root: `file:line` for code, or the instrument that observed it.
Where I could not establish something I say `NOT ESTABLISHED` and name the measurement that would settle it. A plausible guess is marked as a guess.

## 0. The correction, first

What I told you: *"Pause reaches the worker — and stops at the door of a running tool call. The UI button does fan out (`bridge.py:2094-2110` → `thread.pause(manual_only=True)` → `_park_event`), but the worker only observes the latch between model turns (`worker_thread.py:4632`)."*

What is wrong with it: not the facts, the **frame**. It describes a latch inside a loop, and says nothing about what a worker *is* on this machine. A worker is a **separate OS process** with a pid, which spawns **docker containers** that outlive it. My sentence had no pids in it, so it could not be checked against the machine — and that is how a thread-shaped claim survives in a process-shaped system for three generations.

Measured, this minute, through the tool (`Worker(action="inspect")`):

```
parent_pid: 1964997          <- the agent process (this session)
child_pid:  2100321          <- worker 'default', instance 1
runtime_mode: "process"       child_alive: true      same_process: false
```

And through `CheckSystem(containers)`, five long-lived containers for this workspace:

| container | uptime | workspace disk | what it was for |
|---|---|---|---|
| `worker-ipc-proof` | 3h 28m | 5.7 G | "2g (512m SIGKILLs the test file)" |
| `wt-mut` | 3h 28m | 5.7 G | mutation + stability checks |
| `ipc-timeout-step2` | 3h 20m | 5.7 G | "STEP2 mutation experiment" |
| `tm-forensics-check` | 42h 12m | 28 M | — |
| `agent-exec-persistent-…` | 30h 48m | 28 M | read-only CI diagnosis |

None of them is tied to a live dispatch. Three of them are mine, from experiments whose turns were killed. Meanwhile `session_sandbox_status` for this workspace reads **stopped**. So: the machine accumulates processes faster than any of our ledgers can name them, and the answer to "what is running?" is currently an opinion.

## 1. Where a pause actually goes

A pause is delivered **twice**, through two different mechanisms, to two different processes. That is the root defect, not the checkpoint positions.

Hop 1 — in the supervisor (the process that holds the tool):
- `tools/workspace/worker_thread.py:3361` `self._park_event.set()` — a `threading.Event` on the supervisor's own object.
- `:3396-3399` `write_command(self._worker_dir, "pause", self._fence(), control_seq=control_seq)` — a **fenced command on disk**, the cross-process channel.

Hop 2 — in the child process:
- `tools/workspace/worker_runtime_main.py:495-507` reads the command and dispatches `verb = _CONTROL_VERBS.get(message.action)` → `result = getattr(worker, verb)()`.
- So the child calls `pause()` on **its own** `WorkerThread`, setting **its own** `_park_event`.

Consequence: the two latches are separate facts with the same name. The child's latch is the one that matters. The supervisor's latch is set and — in process mode — I have not found its reader (`NOT ESTABLISHED`; measurement in §6, item M1). One fact, two holders, no reconciliation.

Then the child checks its latch in exactly three places:

| # | root | where in the turn |
|---|---|---|
| 1 | `worker_thread.py:4750` `if self._park_event.is_set():` | model-turn event boundary |
| 2 | `:5332`, park loop `:5369` | idle loop, after `_input_queue.get(timeout=2.0)` |
| 3 | `:5611`, park loop `:5687` | **after** the whole tool loop |

There is no checkpoint inside a tool call. The only way a pause touches running work is a tool that elects to bind the event itself:
- `worker_thread.py:4600` `_bind_pause_event(self._park_event)` — bound for the container `exec` wait and `sleep` only.
- `infra/container_manager.py:2669` `if _pause_requested():` → `:2676` the honest message: **"Stopped WAITING because the worker was paused. The process inside the container was NOT killed and may still be running"**.

That line is the whole report in one sentence, and it is already in the codebase: the pause stops the *waiting*, not the *work*. Everything else in the UI says otherwise.

## 2. What the caller's wait does when it is paused, cancelled, or impatient

`worker_query.py:203` `response = thread.send_query(query, timeout=effective)`. The wait itself is a **0.5 s slice loop**, not one blocking read:
- `worker_thread.py:102` `QUERY_WAIT_SLICE_SECONDS: float = 0.5`
- `:3021` `while True:` → `:3028` `slice_timeout = min(QUERY_WAIT_SLICE_SECONDS, remaining)` → `:3030-3031` `reply_q.get(..., timeout=slice_timeout)`
- `:3039` `interrupt_reason = self._wait_interrupt_reason()`; `:2917-2922` returns `"paused"` or the turn-cancel reason; `:3041` returns an **`interrupted`** envelope.

And the code is explicit that the wait ending is not the work ending:
- `:2925` *"Only the WAIT is over: the worker may …"*
- `:2932` *"Wait interrupted; the query was NOT cancelled and the worker may still be running it."*

Then `worker_query.py:427-435`: `if reply.get("status") == "timeout":` → `_resolve_terminal(...)`; **anything else**, including `interrupted`, falls to `_normalize_reply(...)`. So the one caller that gave up on the wait hands back a normal-looking reply and nobody records that a query is still running somewhere. `NOT ESTABLISHED` whether it reaches `_action_query`'s success path and therefore closes a job record as `completed` — that is exactly what PR #497 does not want, and it is the first defect I want re-executed (§6, M2).

## 3. Kill is a request, not a fact

- Process mode kill: `tools/workspace/worker_runtime_process.py:1074-1084` — `acknowledged = bool(self._control("kill", timeout=timeout).ok)` then `finally: self.close()`. A control message, not a signal.
- Escalation: `:843` `process.join(timeout=grace_seconds)` → `:844-846` `if process.is_alive(): process.kill(); process.join(5)` → `:847-850` `assert not process.is_alive()`.
- The child is a plain `multiprocessing.Process`, `daemon=False`, with **no** `start_new_session` / `setsid` / `preexec_fn` (`:715-744`; a sweep of that file for `killpg|start_new_session|preexec_fn|setsid` returns nothing). No new session, no process group ⇒ **a `killpg` is not even possible** with today's spawn.
- The only process-group kill in the tree belongs to the worker's own tracked tool subprocesses: `worker_execution.py:245-247` — `os.killpg(pid, SIGTERM)` when the pid is a group leader, else `os.kill(pid, SIGTERM)`, called from `worker_thread.py:3336`.

So `assert not process.is_alive()` is true the moment the *direct child* is reaped. Its descendants — the container exec, the pytest inside it, the containers themselves — are outside that assert by construction. Today's five containers are that assert being true while work continued.

## 4. Observability: a pid that nothing can find

- The child pid exists only in memory: `worker_runtime_process.py:1296-1298` `def pid(self) … return None if self._process is None else self._process.pid`; surfaced by `worker_runtime.py:1404` and `:1419-1422` (`child_pid`, `same_process`) — i.e. **my inspect call is the only reason that number was ever visible**.
- The one durable per-worker file has no pid field: `worker_thread.py:6366-6377` writes `status.json` with `runtime_status, current_task, last_heartbeat, error, session_id, current_context_tokens, max_context_tokens, started_at, last_query_at, paused_manually`.
- The "registry" is an in-process dict of live objects: `worker_registry.py:2-4` (*"in-process worker thread registry"*), `register_worker:241`, `unregister_worker:273`.
- The pid is used once, for an incarnation check, then discarded: `worker_runtime_process.py:775-779` `if hello.pid != process.pid:` … `"hello claims pid …, but the process we spawned is …"`.

Therefore: after a supervisor restart, no file on this disk names the processes it started. `status.json` says `runtime_status`; the OS says otherwise; nothing reconciles them. **No snapshot of authority may outlive the moment it was true** — today there is no snapshot at all, which is worse to audit and cheaper to fix.

## 5. Containers: ownership is a label, teardown is a survey

- Ownership: `infra/container_manager.py:2326-2327` — `if lifecycle_class == LIFECYCLE_EPHEMERAL: labels["thoughtmachine.worker"] = worker_name`; value built at `tools/workspace/worker_container.py:35-51` as `"<session_id or 'unknown'>:<worker_name>"`.
- Teardown: `worker_thread.py:5085` `listed = cm.list_containers()`, then `:5093-5096` `if not self._is_worker_owned_container(container): continue` → `cm.stop(target)` / `cm.remove(target)`. It **asks docker**, because no ledger exists to ask instead.
- The once-only guard is memory: `:5075-5078` `if getattr(self, "_containers_cleaned", False): return` / `self._containers_cleaned = True`.
- Failure mode: `:5086-5087` `except Exception: return` — if the listing fails, the sweep **silently reclaims nothing** and there is no record to retry from.
- No name is derived per worker or per dispatch; `infra/container_manager.py:5720-5726` `_container_name` is a display helper.

That is why three 5.7 G containers are still up: a killed turn never reached teardown, and nothing else ever looks.

## 6. The design questions I need ruled on

Each: the question, the options, the trade, my recommendation. D1–D3 are about the meaning of the two verbs the UI already shows. D4–D8 are about making the machine's processes knowable.

### D1. What does "paused" mean for work already running?
- (a) **Cooperative, honest**: pause means "no new turn, no new tool call". A running tool finishes. UI says *"pausing — current step will finish; the process inside the container is not stopped"*.
- (b) **Real freeze**: `setsid` at spawn, `SIGSTOP` the group on pause, `SIGCONT` on resume. Work genuinely stops.
- (c) **Two-phase**: cooperative, then a hard freeze after a stated deadline (e.g. 10 s), reported as *frozen* rather than *paused*.
- Trade: (b)/(c) make the heartbeat stop, so the stale watchdog must learn a *suspended* state or it will kill a worker for being paused. (b) also freezes a process holding a docker socket — containers stay locked, `docker ps` still shows them running.
- **Recommend (c)**, and ship (a)'s wording today regardless of the ruling, because today's wording is false.

### D2. One latch or two?
The supervisor sets its own `_park_event` (`:3361`) *and* writes the fenced command (`:3396-3399`), and the child sets its own latch from that command. Two holders of one fact, and I have not established that the supervisor's copy has any reader in process mode (M1). Options: delete the supervisor latch (the disk command is the fact), or make the supervisor the only writer and have the child poll a file. **Recommend: one latch, owned by the process that acts on it** — the child — with the supervisor holding only "the pause request I wrote and its sequence number".

### D3. `interrupted` needs a terminal meaning
Today the caller's wait can end as `interrupted`, the work keeps running, and `worker_query.py` only reclassifies `timeout`. The registry already has an `interrupted` status in `TERMINAL_STATUSES` (measured: `{completed, error, interrupted, paused, stopped, timeout}`) — we are simply not using it. **Recommend**: an interrupted reply terminalizes the job as `interrupted` with an explicit `work_may_still_be_running: true`, and `check` can then be asked the follow-up question. Also fixes a defect in my own PR #497 (a job closed `completed` while the work runs would be a lie) — pending measurement M2.

### D4. Should a kill be verifiable?
Options: (a) `setsid` at spawn + `killpg`, then verify; (b) leave the direct-child kill and add **verification**: after teardown, re-list the OS processes and the labelled containers and report survivors; (c) both. Trade: `killpg` does not help against anything that escapes into its own session, so **verification is the load-bearing half**. **Recommend (c), verification first.**

### D5. A durable process ledger
Record at birth: worker name, instance, dispatch id, pid, ppid, **process start time** (the only thing that defeats pid reuse), cmdline hash, container ids. Question for you: new file `process.json` beside `status.json`, or extend `status.json` (risk: existing readers). **Recommend a new file**, with the rule that it is *reconciled against `/proc` at read time* and never trusted alone.

### D6. One authoritative answer to "what is running?"
Today three: `list` (registry + handle), `check` (`worker_is_live`), `inspect` (`handle.substrate()`). **Recommend**: `inspect` is the OS answer; `list` decorates it; `check` may not answer "not running" from a registry alone — if the OS disagrees, it says so and names both sources. The UI reads the same three, so the same rule applies there.

### D7. Container retention policy
Today: sweep once per process lifetime, from a docker listing, failing silently. **Recommend**: a TTL per container (e.g. 24 h), a reaper at session start, and an *orphaned containers* list in the UI with a reclaim button — because the machine must not accumulate 5.7 G experiments that nobody can name. This is also the cheapest test of D5: a ledger makes "which container belongs to which dispatch" a lookup instead of a guess.

### D8. Vocabulary is a live defect, not cosmetics
The worker system prompt says *"isolated thread"*; `WorkerThread`, `WORKER_RUNTIME_MODE=thread` and `_park_event` all say thread; the UI says *paused* for a process that is running. The model reads that prompt and inherits the wrong mental model — which is how I wrote the sentence you corrected. **Recommend**: fix the prompt and every user-visible string now; leave class names for a separate, mechanical rename.

## 7. Cheap fixes I can land without a ruling (C1–C6)

| # | fix | cost | why now |
|---|---|---|---|
| C1 | interrupted reply terminalizes the job as `interrupted` (D3) | small | PR #497 may otherwise record `completed` for work that is still running |
| C2 | UI wording: *pausing — current step will finish*; surface the existing `container_manager` honesty message | small | the current label is false |
| C3 | pid + start time in the worker's durable state, reconciled on read | small | makes D5 a lookup, not a design |
| C4 | container reaper: ledger, TTL, startup sweep, UI list | medium | 3× 5.7 G are running right now |
| C5 | system prompt + strings: process with a pid, not a thread | small | it is what made me wrong |
| C6 | `check` may not answer "not running" from the registry alone | small | three answers to one question is a bug |

## 8. What I got wrong, stated plainly

1. I described a **loop checkpoint** as if it were the system's behaviour, without ever naming a process. True sentences, wrong altitude — and unfalsifiable without a pid, which is why it survived.
2. I built the S1/S2 dispatch work (`orphaned`, job records) on the *dispatch* clock, while the thing that keeps running is the **process**. Both are still correct and still needed; neither can see a container with 3h 28m of uptime.
3. My sprint order put "pause gate on `submit_query`" (S3) ahead of the question in D1. That was cheap-looking and is now obviously wrong: gating a submit while the semantics of a pause are unsettled adds a second place where "paused" means something different.

## 9. Measurements I have not made (and will make next, in this order)

- **M1** — does the supervisor's own `_park_event` have any reader in process mode? (`grep` for `_park_event` and read every site; then a live pause with an in-flight tool and a `py-spy`-free check of the child's state.) Settles D2.
- **M2** — does an `interrupted` reply reach `_action_query`'s success path? Re-execute the original case: pause during a query, read the envelope and the job record. Settles D3 and C1, and tells us whether PR #497 as merged is a lie.
- **M3** — one pause with a `docker exec` in flight: does the container process keep running after the worker reports *paused*? (The code says yes; measure it and keep the receipt.) Settles D1's wording.
- **M4** — kill a worker mid-`exec` and list survivors by pid and by container label. Settles D4.
- **M5** — after a supervisor restart, what on disk names the previous children? (Expect: nothing.) Settles D5's urgency.

## 10. Appendix — instruments used for this report

- `Worker(action="inspect")` → `runtime_mode=process`, `child_pid=2100321`, `parent_pid=1964997`, `child_alive=true`.
- `CheckSystem(containers)` → 5 running containers, uptimes 30m–42h, three at 5.7 G workspace.
- `CheckSystem(runtime_state)` → `session_sandbox_status: stopped`; permissions: `host_bash` banned, `network` banned, `container` true.
- Code truth: one read-only worker turn over `tools/workspace/` and `infra/`, every claim quoted with `file:line`; plus my own reads of `worker_query.py`, `worker_thread.py`, `job_registry.py`, `agent/config/defaults.py`.

*Nothing in this report was taken from a previous summary, a ledger, or my own memory. The three numbers I did not measure are labelled NOT ESTABLISHED with the measurement that would settle them.*
