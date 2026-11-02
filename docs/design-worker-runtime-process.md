# Design: `WorkerRuntime` — one worker per OS process (P3)
**Citation convention.** Symbols (function / method / class / module-level constant /
field names) are authoritative and were grepped against branch `w7/isolation`. The
handful of line numbers below are a convenience snapshot and *will* drift; when they
disagree with a symbol, the symbol wins. Claims carried over from reading code are
tagged **[verified]**; claims that are design consequence rather than code fact are
tagged **[inferred]**. Section 14 is the evidence table.
## 0. Status, scope, and what this document decides
### 0.1 Status
Draft for review. P3 in the process-isolation phasing. No code exists for `WorkerRuntime`
yet; everything in §1 and §6–§8 describes code that ships **today**.
### 0.2 Scope
In: moving the body of one worker (`WorkerThread.run()` and everything it calls) into its
own OS process behind a `WorkerRuntime` façade; the supervisor that owns those processes;
the IPC contract; how the already-shipped `command.json` fence and the on-disk
`generation` counter become safe with a second process in the picture; staged migration.

Out: §12.
### 0.3 Decision register
| id | decision | one-line why |
|----|----------|--------------|
| D1 | One process per worker **instance**, not per session and not a pool | blast radius must equal one worker; registry and state files are already per worker dir |
| D2 | The supervisor *is* the existing uvicorn process; `WorkerManager` becomes it | `_write_status_file`'s docstring already assumes the API is a separate process — the split exists, we are deepening it |
| D3 | `spawn` a fresh interpreter, one `AF_UNIX` `socketpair` per runtime | no `fork` hazards, no ports, no auth |
| D4 | Socket is the **primary** control path; `command.json` survives as the crash-path recovery channel only | a socket dies with its peer; the file survives it |
| D5 | The fence stays filesystem-based; **do not** move it onto the socket | `resume()` deliberately writes a command instead of unlinking, precisely to keep a cross-process, crash-visible ordering primitive |
| D6 | The supervisor is the single generation minter; the runtime **verifies**, never allocates | today's read-modify-write in `_allocate_generation` is not atomic (§7.2) |
| D7 | `_turn_cancel` and the `tools.cancellation` thread-local binding stay inside the runtime, unchanged | cancellation is per-thread by construction; the boundary carries the *request*, not the token |
| D8 | No durable job record in P3; in-flight jobs are declared `runtime_lost` | `WorkerJobRegistry` is in-memory and event-fed — a durable record is a separate project |
| D9 | Reply correlation by `query_id` string, not by handing over a `queue.Queue` | the live `queue.Queue` in `_input_queue.put((query_id, query, reply_q))` is unserialisable |
| D10 | Frames are versioned `v:1`; unknown `kind` is a protocol error, unknown *extra keys* are ignored | fail-closed on structure, forward-compatible on payload |
| D11 | One flag, `WORKER_RUNTIME_MODE` ∈ `{thread, process}`, default `thread` through S4 | each increment must be independently revertible by a config flip |
| D12 | The runtime keeps emitting `event` frames that the supervisor re-publishes onto the existing per-worker `EventBus` | bridge / `EventForwarder` / `_active_tab_bridges` contracts stay untouched |
### 0.4 What this supersedes
Versus **`docs/design-process-isolation.md`**:

- **Superseded** — its treatment of the command fence as future work (§7.3, which quotes a
  TODO from the spawn site that no longer exists). The fence **shipped**: `CommandFence`,
  `write_command`, `claim_command`, `discard`, `quarantine`, `fence_rejection_reason`.
  §6 below is normative for the fence; §7.3 of that document is history.
- **Corrected** — it names `_pause_event`. There is no such attribute. The park flag is
  `_park_event`; the three-signal block is `_stop_event` / `_park_event` / `_resume_event`
  plus `_turn_cancel`.
- **Corrected** — its park-loop line references (`:2744`, `:3022`) are stale; both loops
  live in `run()` (the `_input_queue` path and the after-tool-loop path).
- **Answered** — its open question "no durable job record located": `WorkerJobRegistry` is
  in-memory, keyed by `query_id`, and fed by worker events (`_JOB_EVENT_TYPES`). Nothing is
  durable.
- **Extended** — its `control` kind is split here into `control` and `cancel` with distinct
  acks, and the frame envelope gains `epoch`.
- **Upheld** — `AF_UNIX` `SOCK_STREAM` socketpair, 4-byte big-endian length + UTF-8 JSON,
  `spawn` over `fork`/`forkserver`, death = EOF **and** `waitpid` agreement, and the
  reconcile order (mark `lost` → fail in-flight → reclaim containers by label → replace).

Versus **`docs/design-worker-lifecycle.md`**:

- **Superseded** — Q2B's "CancellationToken" as one process-agnostic token realised as a
  token file with `stat` polling. No such class exists anywhere in the tree; cancellation is
  per-thread (`tools/cancellation.py`'s `threading.local` holder plus `WorkerThread._turn_cancel`).
  P3 puts the `cancel` **frame** where Q2B put the token file. Q2B's file mechanism survives
  only as the crash-path fallback, i.e. as `command.json`.
- **Superseded** — Q2B's requested `schema` / `request_id` / `requested_at` keys inside
  command.json are **not implemented**: `write_command` writes `action`, the four fence
  fields and an optional `reason`, nothing else. See Q7 in §13.
- **Confirmed-partially** — Q2B's observability triple is one third real: `WORKER_STOPPING`
  exists on the event bus, but `status.json` carries `runtime_status` and has no `phase`,
  `cancel_reason` or `requested_at`.
- **Read-with-care** — Correction 15 says Wave-7 supersedes the Stop cascade. The *mechanism*
  did not go away: `detach()` still sets `_park_event` and the park loop is still the loop's
  idle gate. [inferred] Correction 15 supersedes the cascade's user-visible semantics, not
  the park machinery. This document treats the park loop as load-bearing.
## 1. Current state
### 1.1 Where workers actually live
`WorkerThread(threading.Thread)`, constructed with `daemon=True` and
`name=f"worker-{instance_label}"`, spawned from `_action_spawn` (the `Worker` tool), i.e.
**inside the process that hosts the FastAPI app**. That process is started as
`uvicorn web_ui.backend.server:app` — so the API, the tool loops and every worker share one
address space and one PID. `WorkerRegistry` keeps them in `_worker_registry`, keyed by the
3-tuple `(session_id, worker_name, instance_id)`, under `_registry_lock`, and calls
`atexit.register(self.shutdown_workers, session_id=ALL_SESSIONS)`.
### 1.2 The four in-process channels
1. **Dispatch.** `send_query` builds `reply_q = queue.Queue()` and puts
   `(query_id, query, reply_q)` on `_input_queue`. The reply object *is* the handoff.
2. **Control.** `WorkerManager._control_worker` does `getattr(thread, action)` then calls
   `method()`; `stop_worker`, `pause_worker`, `resume_worker` and `force_replace` all route
   through object identity fetched from the registry.
3. **State.** `status`, `last_heartbeat`, `current_task`, `_manual_only_pause`,
   `_detach_requested`, `_generation` are plain attributes read directly by callers
   (e.g. `getattr(thread, "status", None)` in `_control_worker`).
4. **Cancellation.** `_turn_cancel` is a `threading.Event` bound into tool threads through
   `tools.cancellation.bind_turn_cancel`, which stores it in `_tls`.
### 1.3 The one channel that already crosses a process boundary
`status.json` and `command.json` in the worker dir. `_write_status_file`'s docstring says it
exists "so the web API backend (which runs in a separate process) can read it", and the
`_poll_command` comment calls the command channel "cross-process control". `status.json`
carries `runtime_status`, `current_task`, `last_heartbeat`, `error`, `session_id`,
`current_context_tokens`, `max_context_tokens`, `started_at`, `last_query_at`,
`paused_manually`. So the filesystem-mediated pattern is not hypothetical — it is the
existing exception, and P3 generalises it.
### 1.4 What the fence already guarantees
A command is written atomically (temp file in the same dir, `flush` + `fsync`, `os.replace`)
with a four-field fence, claimed by `os.replace` to a unique `.claimed-<uuid4hex>` path
before it is ever parsed, and either `discard`ed or `quarantine`d — never deleted. A command
whose fence does not match is renamed to `command.json.rejected-<UTC>-<slug>` and logged. A
corrupt or unknown-action command is quarantined the same way.
## 2. Problem statement: five named couplings
| id | coupling | where it lives | consequence |
|----|----------|----------------|-------------|
| **C1** | Job handoff by live object | `_input_queue.put((query_id, query, reply_q))` | a `queue.Queue` cannot be serialised; dispatch is physically in-process |
| **C2** | Control by method call | `_control_worker`'s `getattr(thread, action)`; `deliver_query_and_block(worker_thread, …)` | the controller must hold a Python reference, so authority = object identity in `WorkerRegistry` |
| **C3** | State by shared mutable attribute | `status`, `last_heartbeat`, `_generation`, `_manual_only_pause` read by the routes' in-memory fast path | a second process sees a stale copy or nothing |
| **C4** | Liveness by process liveness | `threading.Thread`, `daemon=True`, `atexit` shutdown, `PeriodicStaleCheck(threading.Thread)` | one `SIGKILL` to uvicorn destroys every worker and every in-flight job |
| **C5** | Cancellation by in-thread Event | `_turn_cancel` + `tools.cancellation._tls` | a cancel minted in another process can only reach a tool loop through `command.json` |

C4 is why this phase exists: a worker that runs a long tool loop and a worker that crashes
the interpreter are today the same object graph. The blast radius of any worker is the whole
server.
## 3. Target architecture
### 3.1 Topology
Supervisor (the uvicorn process) holds, per worker instance: the socket, an outbound frame
queue, a reader thread, a `pending` map of `query_id -> waitable`, and the last known
`worker_state`. The runtime process holds exactly one `WorkerThread` — the class is
*reused*, not rewritten — and adapts frames to the calls it already makes.
### 3.2 Diagram
```
        supervisor process (uvicorn)                        runtime process (x N)
 +---------------------------------------+          +-------------------------------+
 | FastAPI routes                        |          | entry: worker_runtime_main    |
 |   /workers/{n}/{stop,kill,pause,...}  |          |                               |
 |            |                          |          |  WorkerThread.run()           |
 |            v                          |          |    _input_queue  <-- adapter  |
 | WorkerManager  (control/state)        |          |    _output_queue --> adapter  |
 |            |                          |          |    _poll_command   (recovery) |
 |            v                          |          |                               |
 | WorkerRuntimeHandle  ---------------->|  frame   |  ---> frame reader/writer     |
 |   .outbox (bounded deque)  ==========>|=========>|                               |
 |   .reader thread           <==========|<=========|  <=== outbox                 |
 |   .pending{query_id: waitable}        |  socket  |                               |
 |            |                          |  pair    |  _turn_cancel (unchanged)     |
 |            v                          |          |  agent.enter_reading_mode()   |
 | WorkerRegistry (key unchanged)        |          |                               |
 | EventBus -> bridge -> _active_tab_... |          |  writes status.json           |
 +---------------------------------------+          +-------------------------------+
            |                                                      |
            +------------------ worker_dir/ ----------------------+
               command.json  generation  context.json  status.json  runtime.pid
```
### 3.3 Responsibilities
`WorkerRuntimeHandle` (supervisor side) owns: framing, `seq` enforcement, the outbox and its
backpressure policy, correlation (`pending`), liveness (`heartbeat` frames + `waitpid`),
`runtime.pid` bookkeeping, and translating frames into registry state. `worker_runtime_main`
(runtime side) owns: verifying its `epoch`, constructing and starting the existing
`WorkerThread`, adapting `dispatch_query` → `_input_queue`, `reply` → the supervisor, and
`cancel` → `_turn_cancel`. It is the **only** command.json reader while it lives.
### 3.4 What the runtime does not own
Generation allocation, the cap (`WorkerCapExceeded` arithmetic), the registry, events
fan-out, detached-TTL accounting, container adoption. These are supervisor concerns and stay
in the supervisor.
### 3.5 Why `spawn` and not `fork`
`fork` copies the agent's locks, open containers, provider clients and the `_registry_lock`
itself into the child; a worker that ever touches them deadlocks silently. `spawn` gives a
clean interpreter. `forkserver` adds a third process class for no benefit at this size.
[inferred] Startup cost is dominated by imports either way; the mitigation is to keep one
interpreter warm per worker instance, not to share.
## 4. IPC contract
### 4.1 Transport
One `socket.socketpair(AF_UNIX, SOCK_STREAM)` per runtime, created before `spawn` and passed
as the child's standard fd. No TCP, no ports, no authentication, no network permission needed
(the container/network footprint of the child is unchanged).
### 4.2 Framing
4-byte big-endian unsigned length, then that many bytes of UTF-8 JSON. Maximum frame 1 MiB;
zero-length and over-limit frames are protocol errors (§4.7). Envelope:

```
{ "v": 1, "kind": "<str>", "seq": <int>, "worker": "<name>",
  "instance": <int>, "epoch": <int>, "payload": { ... } }
```

`worker` / `instance` / `epoch` mirror `CommandFence` exactly, for exactly the same reason:
so a frame addressed to a different incarnation is identifiable without trusting the socket's
provenance. `seq` is per-direction, per-connection, starts at 1, strictly increasing.
### 4.3 Supervisor → runtime
| kind | payload | notes |
|------|---------|-------|
| `dispatch_query` | `query_id: str, query: str, timeout_seconds: float` | replaces the `(query_id, query, reply_q)` tuple; `reply_q` becomes `pending[query_id]` |
| `cancel` | `request_id: str, reason: str, deadline_ms: int` | `reason` ∈ the lifecycle set (`operator_kill`, `ceiling`, `worker_budget`, `tool_call_budget`, `ttl_expired`, `detached_ttl`) |
| `control` | `request_id: str, action: "stop"\|"pause"\|"resume"\|"kill"` | `stop` means **detach** (`detach()`), not terminate |
| `shutdown` | `grace_ms: int` | supervisor is exiting; not an error condition |
| `ping` | `nonce: int` | only used when the socket is otherwise silent |
### 4.4 Runtime → supervisor
| kind | payload | notes |
|------|---------|-------|
| `hello` | `pid, started_at, runtime_version, epoch_seen` | first frame; supervisor asserts `pid` == child pid and `epoch_seen` == expected |
| `query_accepted` | `query_id` | lets the supervisor start its wait clock before any reply |
| `reply` | `query_id, ok: bool, envelope: {...}, elapsed_seconds: float` | `envelope` is the same JSON envelope `deliver_query_and_block` normalises today |
| `event` | `event_type: str, data: {...}` | what `_publish_event` / `_publish_global_event` send today |
| `heartbeat` | `at, status, current_context_tokens, max_context_tokens` | keeps `HEARTBEAT_STALE_AFTER_S` / `WorkerLifecycleObserver._heartbeat_age` semantics |
| `worker_state` | `status, current_task, error, paused_manually` | field names match `status.json` so the supervisor can mirror it verbatim |
| `cancel_ack` | `request_id, phase: "accepted"\|"observed"\|"completed"\|"refused"` | see §5.4 |
| `control_ack` | `request_id, action, ok: bool, status` | |
| `fatal` | `reason: str, traceback_tail: str` | runtime is about to exit non-zero |
| `bye` | `{}` | orderly completion of `shutdown` |
### 4.5 Versioning
`v` is an integer and only a **breaking** change bumps it; additive payload keys do not.
Receivers must ignore unknown keys inside a known `kind` and must never ignore an unknown
`kind`. A supervisor refuses to `dispatch_query` to a runtime whose `v` it does not support
(it answers `unsupported_version` at the call site rather than sending a frame the child will
misparse) and instead escalates to `shutdown`. Version skew can only arise across a rolling
upgrade, since the supervisor spawns the binary it is running.

Asymmetry worth naming: **`command.json` is unversioned** — `write_command` writes
`{"action", session_id, worker_name, instance_id, epoch[, reason]}` and nothing else. A
`schema` key would be safe to add additively (§13 Q7) because `fence_rejection_reason` looks
only at the four fence fields; but the four fence fields themselves must never become
optional.
### 4.6 Backpressure
The supervisor never writes from a request thread. Each handle has one writer with a bounded
outbox and a per-kind policy:

- **Coalesce (drop-oldest, keep-latest per key):** `heartbeat`, `worker_state`, `event`.
  Losing an intermediate heartbeat is harmless because staleness is an age comparison.
- **Never drop:** `reply`, `query_accepted`, `cancel_ack`, `control_ack`, `fatal`. These are
  correctness-bearing; if the outbox is full of these, the supervisor applies reader-side
  pressure (it stops consuming `event` frames first) [inferred].
- **Refuse:** `dispatch_query` when the outbox is non-empty and the runtime has no capacity —
  the call fails fast with a `runtime_backpressure` error rather than blocking a tool loop.
  This is the same posture as `WorkerCapExceeded`.

Defaults (1024 frames or 8 MiB, whichever is hit first) are a knob, not a measured value
[inferred]; there is no number in the tree to copy.
### 4.7 Malformed frames
| condition | action |
|-----------|--------|
| length 0, or length > 1 MiB | protocol error → close → `SIGKILL` → mark `lost{protocol_error}` |
| not valid UTF-8, or not JSON | same |
| not a JSON object, or missing any of `v`/`kind`/`seq` | same |
| `seq` not strictly increasing | same (desync) |
| unknown `kind` | same — a stream we cannot interpret may be mid-frame |
| known `kind`, unknown extra payload keys | ignore the extra keys |
| `epoch` ≠ the supervisor's current generation | **drop the frame, log it, increment `protocol_rejects`**; not fatal; above a threshold the runtime is killed [inferred] |

The last rule is the socket analogue of `fence_rejection_reason` + `quarantine`. It is
deliberately *not* a silent drop: the command channel's rule — a mismatch is always logged
and always leaves evidence — is the standard being matched. The difference is that frames are
not worth persisting, so the evidence is the log line plus a counter.
### 4.8 Ordering and correlation
`seq` gives a total order per direction. `reply` correlates on `query_id`, which is already a
`uuid4().hex` minted at dispatch. A `reply` with an unknown or already-satisfied `query_id`
is logged and dropped — the same rule that `send_query`'s drain of the shared fallback
queue enforces today ("a late reply can never satisfy this (or the next) call").
## 5. Cancellation across the boundary
### 5.1 What exists today
Two independent paths set the same `threading.Event`:

- in-process: `detach()` sets `_turn_cancel` (a graceful stop cancels the in-flight turn and
  leaves `_stop_event` clear), `pause()` sets `_park_event`;
- cross-process: `_poll_command`'s `"stop"` branch sets `_turn_cancel` too, then parks.

`tools.cancellation` is a per-thread holder: `bind_turn_cancel(event)`, `interrupt(reason)`,
`reset()`, `is_interrupted()`, `cancel_reason()`, all over `threading.local()` with a fallback
event. It is process-local by construction and stays that way.
### 5.2 Where a cancellation is minted
Unchanged: whoever decides — an operator route, a ceiling check, `PeriodicStaleCheck`'s
detached-TTL sweep, a tool-call budget — mints a `request_id` in the **supervisor** and sends
a `cancel` frame. The runtime never decides to cancel itself [inferred: no such path exists
today].
### 5.3 A frame, not a signal
`SIGINT`/`SIGTERM` land at an arbitrary bytecode boundary and carry no attribution: a handler
cannot say which turn it interrupted, and a second signal during unwinding is
indistinguishable from a crash. The cooperative path is already idempotent and already
thread-targeted. Signals are reserved for escalation only.
### 5.4 Ack ladder and escalation
`accepted` (frame parsed) → `observed` (the runtime's loop noticed; the park loop and the
tool loop are the two observation points) → `completed` (turn unwound, `tools.cancellation.reset()`
called). Each ack carries the `request_id`. The supervisor arms `deadline_ms` from the
`cancel` payload; on expiry the ladder is:

1. still heartbeating → do **not** kill; mark the turn `cancel_unconfirmed` and surface it.
2. heartbeat stalled past `HEARTBEAT_STALE_AFTER_S` → `_is_stale` / `_hung_action_for` applies.
3. explicit `kill` → `control{action:"kill"}` → `SIGTERM` → grace → `SIGKILL`.

Today there is no `SIGTERM` rung at all: `kill()` is in-process and terminal. P3 introduces
`SIGTERM`-then-`SIGKILL` with a fixed grace (a new constant; `QUERY_WAIT_GRACE_SECONDS` is the
closest existing shape and should not be reused for this).
### 5.5 Cancelled versus crashed
- **cancelled**: runtime alive, `cancel_ack{phase:"completed"}` received, status returns to
  `ready`; the worker dir, `generation` and context survive; nothing is lost.
- **crashed**: EOF/`waitpid` agreement with no `completed` ack. Mark `lost`, fail every
  `pending` entry with `runtime_lost`, emit the failure event so `WorkerJobRegistry` closes
  its `submit_query` jobs, reclaim containers by label, keep `generation`.

The discriminator is the ack **plus** process aliveness — never the absence of a `reply`.
Today an absent reply is a timeout (`WORKER_TIMEOUT`, `reason: "query_timeout"`), which would
mislabel a crash as slowness.
## 6. The command fence
### 6.1 The actual keys
`CommandFence` is a frozen dataclass with exactly four fields, and `_FENCE_FIELDS` validates
them by `type(v) is int` (so `True` is rejected as an int):

```
session_id:  str
worker_name: str
instance_id: int
epoch:       int
```

`WorkerThread._fence()` builds it fresh on every call — never cached — as
`CommandFence(session_id=self.session_id or "", worker_name=self.worker_name,
instance_id=self.instance_id, epoch=self._generation)`. Its docstring states the epoch is
`_generation` and is "deliberately NOT a second counter".
### 6.2 Who mints, who validates
Two minters, one counter:

- the thread, from in-memory `_generation` (`_fence()`, used when it echoes its own actions
  into `command.json` from `detach()`, `pause()`, `resume()`);
- the API route, from the on-disk sidecar (`_read_worker_generation` → `_write_worker_command`).

The validator is `_poll_command`, which calls `fence_rejection_reason(self._fence(), data)`.
The shared authority is therefore `worker_dir/generation`; in-memory `_generation` is only a
cache of it. That is precisely what makes a third process possible.
### 6.3 A stale writer cannot stop the new generation
Because `_generation` is re-allocated on every spawn, a command written for the previous
incarnation carries the previous epoch and is rejected. The spawn site documents this as the
reason a stale `command.json` is left in place rather than unlinked: a command "a PREVIOUS
incarnation (or another session sharing the directory) wrote can never match the new epoch
and is QUARANTINED by `_poll_command` on the first poll", and unlinking it "would destroy the
only evidence that a foreign writer addressed this worker — and could delete a command that is
mid-claim".
### 6.4 Atomicity
`write_command`: `tempfile.mkstemp(dir=worker_dir, prefix=".command.json.", suffix=".tmp")`
(same directory, so `os.replace` cannot cross a filesystem), `json.dump`, `flush`,
`os.fsync`, `os.replace`; the temp path is unlinked on every failure path; the function never
raises and returns `False` on `OSError`. The reader side is the other half: `claim_command`
renames the live path to a unique `.claimed-<uuid4hex>` and reads **the claimed file**, so the
consumer never touches the live path and two consumers cannot both win.
### 6.5 Ordering
The command is written **before** the in-memory side effect is observable by the park loop:
`detach()` writes its command before enqueuing `_PARK_SENTINEL`; `resume()` writes its command
and that write "MUST precede `_park_event.clear()`". The rule generalises to P3: a control
whose command write fails must not be reported as applied.
### 6.6 Mismatch is logged, never silently dropped
`fence_rejection_reason` returns `None` on match, `"no fence in payload"` when any of the four
keys is missing, `"malformed fence in payload"` when `CommandFence.from_payload` yields
`None`, and otherwise a comma-joined `field expected != actual` list. On any non-`None` value
`_poll_command` calls `quarantine` and logs a WARNING naming the worker and reason. Unknown
actions are quarantined the same way. `quarantine` renames under `REJECTED_MARKER` with a
UTC timestamp and a sanitised slug, adding a numeric suffix on collision, and must never
delete.
### 6.7 Known defect: the unfenced fallback
`_write_worker_command`'s docstring says an unreadable generation results in an UNFENCED
`{"action": action}` "rather than dropped, so the caller's request is never silently
ignored". That is false at the only point that matters: `fence_rejection_reason` rejects a
payload with no fence as `"no fence in payload"`, so `_poll_command` quarantines it and logs a
WARNING. The request is *neither applied nor surfaced to the caller* — it is preserved as
evidence and nothing else. Recommendation in §13 Q6: after P3 the producer must refuse
(`control_unavailable`) instead of writing something it knows the consumer will reject.
### 6.8 One reader today, two candidates during migration
There is exactly one `_poll_command` loop today (the thread). Adding a runtime process is safe
only because claim-by-rename is exclusive — but "safe" is not "correct": a command claimed by
the wrong reader is lost to the right one. The hard rule for P3: **the process that hosts the
runtime is the only command reader; the supervisor never claims**. During the flag window the
supervisor's in-memory control path is a *sender*, never a reader.
## 7. `_generation` ownership
### 7.1 The counter and its sidecar
`worker_dir/generation` holds the integer; `context.json` mirrors it. `_allocate_generation`
reads `max(sidecar, context.json generation)`, computes `+ 1`, writes via
`mkstemp(prefix=".generation_")` + `FileLock(str(gen_file))` + `os.replace`, swallowing
`OSError`. `_load_context` then does `self._generation = max(self._generation, file_gen)`, and
`_save_context` re-writes the value under the same mkstemp+lock+replace pattern.
### 7.2 One minter, one persister
Today `WorkerThread.__init__` is the minter (`self._generation = self._allocate_generation()`),
and the same object is the persister. With two processes, that breaks: `_allocate_generation`
takes the file lock **only around `os.replace`** — the read of the sidecar and of context.json
happen outside it — so two racers can both compute the same `new_gen` and both "succeed". That
is not a fence bypass in-process (spawn is serialised by `_registry_lock` and double-checked
locking), but it is exactly the failure a second spawner would produce.

P3 rules:

- **Minter**: the supervisor, once, immediately before spawn, with the read and the write
  inside one held lock.
- **Persister of allocation**: the supervisor writes `worker_dir/generation` only.
- **Persister of state**: the runtime writes `context.json` (and `status.json`) only, carrying
  the epoch it was given.
- **Verifier**: the runtime asserts `int(sidecar) == epoch` at startup and, on mismatch, sends
  `fatal{reason:"epoch_mismatch"}` and exits non-zero. Fail closed.
- `_save_context`'s stale-writer guard (`if existing_gen > self._generation: skip`) stays as
  the second line of defence; it only works if the runtime is the sole writer of
  `context.json`, which is why the split above is a rule and not a preference.
### 7.3 Everyone else compares
Routes never mint: `_read_worker_generation` reads, `_write_worker_command` compares against
what it read, and `_poll_command` compares `_fence()` against the payload. No third
allocation site may be added.
### 7.4 Restart
A restart allocates epoch+1; every command and frame addressed to epoch N becomes
unmatchable. `_load_context`'s `max()` keeps a runtime that loaded a *newer* file from ever
writing an older value. Missing sidecar + missing context.json yields `new_gen == 1`, so 1 is
today's "unknown" value; recommend reserving 0 as invalid and never inferring identity from a
missing file [inferred].
### 7.5 Relation to `instance_id` and worker name
- `(session_id, worker_name, instance_id)` is the registry key; `instance_label` renders
  `name#N` (1 renders bare) and names the OS thread `worker-{instance_label}`.
- `instance_id` is allocated in `_action_spawn` as `max(live same-name instance ids) + 1` — a
  **reuse slot**, recycled once the old instance unregisters.
- `_generation` is monotonic on disk and never recycled.

Therefore `instance_id` cannot identify an incarnation across restarts, and `epoch` alone
cannot address the registry. `_FENCE_FIELDS` needs both, and so does the frame envelope. No
attempt should be made to derive one from the other.
### 7.6 Defect: `_reset_for_new_generation`
`_fence()`'s docstring references `_reset_for_new_generation`, which does not exist. The real
method is `reset()` (which bumps via `_allocate_generation()`, unlinks `context.json`, and
returns `False` with reason `"busy"` when the worker is mid-turn). Recommend a one-line
docstring fix as a follow-up; it is a trap for anyone reading `_fence()` to understand
generation churn.
## 8. Lifecycle across the boundary
Status strings that the code actually assigns: `ready`, `busy`, `detaching`, `detached`,
`paused`, `killed`, `error`. `lost` is **new in P3** and belongs to the supervisor.

| state | truth owner today | written/observable | supervisor action on entry (P3) |
|-------|-------------------|--------------------|---------------------------------|
| `ready` | runtime | `status.json` via `_write_status_file` | none; may `dispatch_query`; start the busy guard |
| `busy` | runtime | `status.json` | refuse `dispatch_query` (busy guard), accept `cancel` |
| `detaching` | runtime (`detach()` sets it first) | `status.json` | arm detached-TTL clock; do not respawn; do not reuse the slot |
| `detached` | runtime (parked: `_park_event` set, `_detach_requested` set, `_stop_event` clear) | `status.json` + `WORKER_STOPPING` event | keep the process; `PeriodicStaleCheck._check_detached_ttl` → `_announce_detached_ttl_kill` → `_reject_queued_queries` → `_kill_detached_worker` on expiry |
| `paused` | runtime (parked) | `status.json`, `worker_paused` event | accept `control{resume}`; refuse dispatch |
| `killed` | runtime (`kill()`, `_stop_event` set) | `status.json` + `WORKER_STOPPING` | reap via `waitpid`, unregister, keep `generation`, allow respawn at epoch+1 |
| `error` | runtime | `status.json` | surface; no automatic respawn [inferred: recommend explicit] |
| `lost` | **supervisor** | log + event | fail `pending` with `runtime_lost`, close job-registry entries, reclaim containers by label, unregister, keep `generation`, replace |

Ordering constraints that already exist and must survive encapsulation: registration before
the status write in `detach()` (so the TTL sweep never sees `detached` for a worker it does
not know), and the command write before the park flag change in `resume()`.
## 9. Failure modes
### 9.1 Runtime crash
Detection requires **both** EOF and `waitpid` agreement (or heartbeat age past
`HEARTBEAT_STALE_AFTER_S` as the fallback). Then: mark `lost` → fail every `pending` waitable
with `runtime_lost` → emit the failure event (this is how `WorkerJobRegistry` closes its
entries, since it is event-fed) → reclaim containers by label → keep `generation` →
optionally replace. Without the pending-fail step, `deliver_query_and_block` would block until
its own timeout and then try to cooperatively stop a dead process (`stop_on_timeout`).
### 9.2 Supervisor crash
The runtime's socket peer disappears (it reads EOF). Policy: finish the current turn, write
its `reply` into the void, write `status.json` as `detached`, and exit after `grace_ms`
[inferred: needs a decision — §13 Q1]. A runtime must never wait forever for a supervisor that
is gone, and must never adopt a *new* supervisor's frames: the new supervisor sees a stale
`runtime.pid` and reaps.
### 9.3 Orphan reaping
Needs a durable record, because `atexit.register(self.shutdown_workers, …)` does not run under
`SIGKILL`. Add `worker_dir/runtime.pid` containing `{pid, epoch, started_at}`; the next
supervisor's stale check reads it and: pid alive + epoch matches expectation → adopt or
`shutdown`; pid alive + epoch mismatch → `SIGKILL` (foreign incarnation); pid dead → remove
the file. Reaping is by `(pid, epoch)`, never by memory.
### 9.4 Double spawn
Today double-spawn is prevented in-process only (`_registry_lock` plus the documented
double-checked locking in `_action_spawn`). That lock does not cross processes, and the
generation read-modify-write is not atomic (§7.2). P3 needs the on-disk analogue: a per-worker
spawn lock file held for the allocate-then-spawn window, with the loser failing closed and
reporting `WorkerCapExceeded`-style. Anything weaker admits two runtimes both claiming epoch N.
### 9.5 Protocol desync
No mid-stream resynchronisation is attempted: a length-prefixed stream cannot be resynchronised
without an in-band marker, and adding one costs more than the kill. Desync ⇒ `SIGKILL` ⇒
`lost{protocol_error}` ⇒ replace. The quarantined evidence is the supervisor log plus the frame
counter, not a file.
### 9.6 Disk full
The worst failure in the whole system, and it is already partly silent:

- `write_command` returns `False` — a cross-process control is not written; the route logs a
  WARNING and continues.
- `_allocate_generation` swallows `OSError`, so `new_gen` is returned but **not persisted**.
  A subsequent spawn re-reads the old value and can hand out the *same* epoch. That destroys
  the fence's only invariant, silently.
- `_write_status_file` logs an error and leaves a temp file behind.

Recommendation: make a failed generation persist fatal to spawn (fail closed, surface to the
caller) rather than best-effort. Everything else can degrade.
## 10. Staged migration
| stage | change | shippable alone? | flag | rollback |
|-------|--------|------------------|------|----------|
| **S0** | `WorkerRuntimeHandle` façade that *is* the thread; message dataclasses exist and are exercised in-process | yes, no behaviour change | `WORKER_RUNTIME_MODE=thread` | n/a |
| **S1** | Move generation allocation into the supervisor under one lock; runtime verifies `epoch` | yes — fixes a real race on its own | none needed | revert the commit |
| **S2** | `dispatch_query` / `query_accepted` / `reply` only; control still `command.json` | yes, per worker | `process` per worker | `thread` |
| **S3** | `control` + `cancel`/`cancel_ack` frames; runtime becomes the sole command reader; `command.json` demoted to recovery | yes | per worker | `thread` |
| **S4** | `event` / `heartbeat` / `worker_state` frames; supervisor stops reading `last_heartbeat` from memory | yes | per worker | `thread` |
| **S5** | `runtime.pid`, orphan reaping, `SIGTERM`→`SIGKILL` ladder; flip the default to `process` | yes | flip default last | flip back |

Both code paths live in the same build for one release; the flag is read at spawn time so a
single worker can be moved without restarting the server.
## 11. Test strategy
### 11.1 Per stage
- **S0** — frame codec round-trip; `seq` monotonicity; unknown-key tolerance; the façade must
  produce byte-identical observable behaviour to the thread for the existing suite.
- **S1** — concurrent allocation: N threads × M allocations must yield M distinct strictly
  increasing epochs; simulated `OSError` on the sidecar write must fail the spawn, not pass.
- **S2** — two-process dispatch with the real `spawn` start method: reply correlation under two
  concurrent queries, a late reply for a timed-out query never satisfying the next one.
- **S3** — cancellation ladder: `accepted`/`observed`/`completed` under a parked worker and
  under a running tool loop; `SIGTERM` then `SIGKILL` observed by the parent; exactly one
  consumer of `command.json` (assert no claim by the supervisor).
- **S4** — event ordering and duplication: no event may be re-published twice; a coalesced
  heartbeat must still advance staleness.
- **S5** — chaos: `SIGKILL` the runtime mid-turn, assert `pending` failed with `runtime_lost`,
  the job registry closed, no orphan pid after the sweep, and the replacement at epoch+1.
  Reaping tests must run against a *killed* supervisor to prove `atexit` is not the mechanism.
### 11.2 Regressions not to reintroduce
1. **Dead park.** A parked worker must still observe control. The park loop already polls
   `_poll_command` for exactly this reason ("the ONLY terminating action" was invisible while
   parked). After P3 a parked runtime must keep reading its socket — never park with the fd
   unread.
2. **Detached-registration ordering.** Register before writing the `detached` status.
3. **`resume()` must write, not unlink.** The producer must never delete the live path
   (read/unlink race), and claim-then-discard would be content-blind and could eat a pending
   `kill`.
4. **Quarantine never deletes.** A rejected command is evidence.
5. **Claim before read.** The consumer must parse the claimed file, never the live path.
6. **Stale-writer guard in `_save_context`.** An old incarnation must never overwrite newer
   context.
7. **Stop ≠ kill.** `detach()` must not set `_stop_event`; `resume()` must refuse when it is set
   or status is `killed`.
8. **Fence rejection reasons** stay exactly three shapes (`None`, "no fence in payload",
   "malformed fence in payload") plus the diff form.
## 12. Non-goals
- Not a scheduler and not a work queue; `SPAWN_QUEUE_TIMEOUT` stays reserved.
- Not a security boundary. Process isolation here is a fault/containment boundary; `pids_limit`
  and the host-write gate remain separate concerns, and a runtime is not sandboxed from its
  user.
- Not durable jobs (D8), not crash-resumable turns, not multi-host.
- Not removing `command.json`, and not changing `tools/cancellation.py`.
- Not changing UI contracts: `instance_label`, the routes' shapes, `status.json` keys,
  `EventForwarder` / `_active_tab_bridges` fan-out.
- Not touching provider/model plumbing or the WLM fast path (`_process_query_via_wlm`).
## 13. Open questions
1. **Does a runtime outlive its supervisor?** *Recommendation: no.* Finish the turn, write
   `detached`, exit after a grace. Adoption is a reaping decision made by the next supervisor
   from `(pid, epoch)`, not a handshake.
2. **Durable job records before or after P3?** *Recommendation: after.* Accept
   `runtime_lost` in P3; revisit only if the UI must promise resumability.
3. **Does the fence move onto the socket?** *Recommendation: no* (D5). The socket is already
   the fast path; the file is what survives the fast path's death.
4. **Who writes `status.json` after P3?** *Recommendation: the runtime.* Single writer,
   crash-visible, and the routes keep reading it unchanged. The supervisor mirrors `worker_state`
   frames for in-memory queries only.
5. **`epoch` or a separate `runtime_id`?** *Recommendation: `epoch` only.* A third counter is a
   third thing to get wrong, and `instance_id` is already a misuse magnet because it is
   recycled (§7.5).
6. **What to do about the unfenced fallback?** *Recommendation: refuse at the producer*
   (`control_unavailable`) rather than writing a payload `fence_rejection_reason` will reject.
   Keeping the write changes "silently ignored" into "quarantined", which is better but still
   not an answer to the operator.
7. **Should `command.json` gain `schema` / `request_id`?** *Recommendation: yes, additively, and
   only after the fence fields are proven load-bearing.* `fence_rejection_reason` ignores extra
   keys, so additive is safe; making any fence field optional is not.
8. **What grace before `SIGKILL`?** *Recommendation: a new constant; do not reuse
   `QUERY_WAIT_GRACE_SECONDS`.* They measure different things (waiting for a reply vs unwinding
   a turn) and must be tunable separately.
## 14. Evidence table
| symbol | path:line |
|--------|-----------|
| `WorkerThread` | `tools/workspace/worker_thread.py:920` |
| `_generation` init | `tools/workspace/worker_thread.py:972` |
| `_park_event` / `_resume_event` / `_turn_cancel` | `tools/workspace/worker_thread.py:1141`, `:1142`, `:1151` |
| `_PARK_SENTINEL` | `tools/workspace/worker_thread.py:77` |
| `send_query` (`reply_q`) | `tools/workspace/worker_thread.py:1331`, `:1361-1362` |
| `detach()` (command write) | `tools/workspace/worker_thread.py:1498`, `:1546` |
| `kill()` | `tools/workspace/worker_thread.py:1550` |
| `pause()` | `tools/workspace/worker_thread.py:1634`, `:1658` |
| `resume()` | `tools/workspace/worker_thread.py:1662`, `:1706` |
| `reset()` | `tools/workspace/worker_thread.py:1862` |
| `_fence()` | `tools/workspace/worker_thread.py:1921` |
| `_poll_command()` | `tools/workspace/worker_thread.py:1940` |
| `run()` park loops | `tools/workspace/worker_thread.py:2857`, `:2889`, `:3167` |
| `_allocate_generation()` | `tools/workspace/worker_thread.py:3467` |
| `_load_context()` generation restore | `tools/workspace/worker_thread.py:3540` |
| `_write_status_file()` | `tools/workspace/worker_thread.py:3632` |
| `_save_context()` generation write | `tools/workspace/worker_thread.py:3677`, `:3718` |
| `_publish_event()` | `tools/workspace/worker_thread.py:3745` |
| `_heartbeat_tick()` | `tools/workspace/worker_thread.py:3792` |
| spawn site / double-checked locking | `tools/workspace/worker_thread.py:4896`, `:4945` |
| `spawn_iid` allocation | `tools/workspace/worker_thread.py:4581`, `:4690` |
| `COMMAND_FILENAME`, `_FENCE_FIELDS`, `CommandFence` | `tools/workspace/command_channel.py:57`, `:64`, `:78` |
| `write_command` / `claim_command` / `quarantine` | `tools/workspace/command_channel.py:163`, `:218`, `:263` |
| `fence_rejection_reason` | `tools/workspace/command_channel.py:295` |
| `_control_worker` | `tools/workspace/worker_manager.py:771` |
| `PeriodicStaleCheck` | `tools/workspace/worker_manager.py:107` |
| `request_worker` (spawner callable) | `tools/workspace/worker_manager.py:647` |
| `_worker_registry` | `tools/workspace/worker_registry.py:58` |
| `WorkerJobRegistry` (in-memory, event-fed) | `tools/workspace/job_registry.py` |
| `HEARTBEAT_STALE_AFTER_S` | `agent/config/defaults.py:43` |
| `WorkerLifecycleObserver._is_stale` | `tools/workspace/worker_lifecycle.py:143` |
| `bind_turn_cancel` | `tools/cancellation.py:20` |
| `deliver_query_and_block` | `tools/workspace/worker_query.py:379` |
| `Agent.enter_reading_mode` / `leave_reading_mode` | `agent/core/agent.py:1184`, `:1212` |
| `_read_worker_generation` / `_write_worker_command` | `web_ui/backend/workspace_routes.py:315`, `:352` |
| `WORKER_STOPPING` | `agent/events.py:88` |
| uvicorn entry (`app`, `uvicorn.run`) | `web_ui/backend/server.py:1164`, `:6294` |
| `_active_tab_bridges` | `web_ui/backend/event_forwarder.py:25` |
### 14.1 Not groundable in code
- Process counts, frame sizes, outbox limits, `grace_ms`, and the cancel deadline: no numbers
  exist in the tree, so every value in §4.6 and §5.4 is a proposed default, not a measurement.
- `runtime.pid`, `lost`, `WorkerRuntimeHandle`, `worker_runtime_main`, `WORKER_RUNTIME_MODE`,
  and the `cancel` / `cancel_ack` / `hello` / `bye` / `ping` / `shutdown` kinds are **new**;
  no such symbols exist yet.
- The claim that Wave-7 Correction 15 supersedes the cascade's semantics but not its mechanism
  is an interpretation of prose, not a code fact.
