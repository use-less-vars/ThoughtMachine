# Design: Process Isolation for the Worker Runtime

Status: design only. No source file is modified by this document, no code is
proposed as executable, and nothing here was derived from running the system.
Every architectural claim carries a `path:LINE` citation taken from static
reconnaissance. Claims that could not be pinned down are listed in §10 under
"Open questions / not verified" rather than asserted.

Two independent work items ride along with the isolation wave and are specified
here because they touch the same code paths: **item B** (`pids_limit` for
worker containers, §8) and **item C** (the ungated host write in the Visual
tool, §9). Item C is not actually coupled to isolation and can ship first.

---

## 1. Problem, and what isolation actually buys

Today every worker is a Python thread inside the FastAPI server process:

- `tools/workspace/worker_thread.py:912` — `class WorkerThread(threading.Thread):`
- `:949` — `super().__init__(daemon=True, name=f"worker-{instance_label}")`
- `:4751` — `thread = WorkerThread(` (construction site)
- `:4854` — `thread.start()`
- plus a second thread class in the supervisor: `tools/workspace/worker_manager.py:107`
  `class PeriodicStaleCheck(threading.Thread):`, `:137` (daemon), `:768 checker.start()`

Consequences that are true *because* they are threads in the API process:

1. **A single worker's crash can take down the API server.** The thread shares
   one interpreter, one import graph, one heap. An unhandled exception in a
   callback that runs on the event path (`web_ui/backend/bridge.py:1188
   worker_bus.subscribe(evt_enum, handler_fn)`) or a C-extension segfault
   inside a tool is an API-server outage, not a worker outage. The HTTP surface
   that the UI uses to *observe and control* workers dies with them.
2. **Runaway CPU is GIL contention, not a bounded cost.** A worker doing
   pure-Python work in a loop steals interpreter time from the request handlers
   that would otherwise let an operator notice and kill it.
3. **Cancellation and supervision share fate.** The control plane is
   `web_ui/backend/workspace_routes.py:1228 thread.kill()`, `:1534
   thread.pause()`, `:1641 outcome = thread.resume()` — method calls into
   objects in this process. If this process is the thing that is wedged, the
   controls are wedged too.
4. **Memory is unbounded per process.** N workers with N tool caches and N
   `ContainerManager` objects all land in one RSS with no per-worker ceiling.

What becomes true after the boundary exists: worker death (and worker-driven
`os._exit`/segfault/OOM-kill) no longer stops the API; the control plane stays
alive to observe and reconcile; runaway CPU is scheduled by the kernel instead
of arbitrated by the GIL; per-worker-runtime RSS has a hard boundary; and a
restart of a worker runtime does not restart the HTTP server.

What does **not** change and must not be claimed as a benefit: containers,
Docker daemon interaction, and the workspace filesystem all remain
out-of-process already — that part of the isolation story is already true
(§2, containers are label-addressable, `tools/workspace/worker_container.py:30-36`).

---

## 2. Current-state inventory

### 2.1 Control path (a) — direct in-process method call on a live object

| cite | verbatim |
|---|---|
| `web_ui/backend/workspace_routes.py:1228` | `thread.kill()` |
| `web_ui/backend/workspace_routes.py:1230` | `thread.stop()` |
| `web_ui/backend/workspace_routes.py:1534` | `thread.pause()` |
| `web_ui/backend/workspace_routes.py:1641` | `outcome = thread.resume()` |

These are real `WorkerThread` objects being commanded from the route layer.

### 2.2 Control path (b) — raw private-dict indirection holding live objects

| cite | verbatim / note |
|---|---|
| `web_ui/backend/workspace_routes.py:42` | `_worker_registry = _WorkerRegistry.get_instance()._worker_registry` — reaches into a *private* attribute |
| `:43` | `_registry_lock = _WorkerRegistry.get_instance()._registry_lock` |
| `:1218`, `:1527`, `:1633` | `for key, thread in list(_worker_registry.items()):` — the loop *is* the dispatch mechanism |
| `:638` | `manager.list_workers(sid)` |
| `web_ui/backend/global_routes.py:85` | `worker_manager.list_workers(session_id)` |
| `web_ui/backend/bridge.py:125` | `_worker_registry = _WorkerRegistry.get_instance()._worker_registry` |

Registry locks that guard this dict: `tools/workspace/worker_registry.py:52`
(`_instance_lock`), `:59` (`_registry_lock`), `:63` (`_bus_registry_lock`);
also `tools/workspace/worker_lifecycle.py:54`, `worker_manager.py:142`
(`self._stop_event = threading.Event()`), `:843` (`_MANAGER_LOCK`).

Manager façade over the same dict — `tools/workspace/worker_manager.py`:
`:447 get_worker`, `:464 list_workers`, `:517 stop_worker`, `:543 stop_all`,
`:567 cleanup_dead`, `:618 force_replace`, `:647 request_worker`,
`:733 pause_worker`, `:739 resume_worker`, `:847 get_manager()`, and the
string-keyed dispatch core `:771 _control_worker` → `:775 get_worker(...)` →
`:783 method = getattr(thread, action, None)` → `:791 method()`.

**Grep-verified and important:** the backend has essentially *no* call sites
for that manager surface — the only hits are the two `list_workers` calls above.
The manager is largely a façade over the raw private dict, which means the
amount of code that must be re-plumbed is much smaller than the API suggests,
but it also means the manager is not the chokepoint one would design against.

### 2.3 Control path (c) — filesystem-mediated, and it is the *primary* path

| cite | verbatim |
|---|---|
| `web_ui/backend/workspace_routes.py:1198` | `{"action": "kill" if terminate else "stop"},` |
| `:1199` | `worker_dir / "command.json",` |
| `:1511` | `_atomic_write_json({"action": "pause"}, worker_dir / "command.json")` |
| `:1620` | `_atomic_write_json({"action": "resume"}, ...)` (same shape) |
| `:1233` | `except Exception:` / `pass  # The file-based command will still work` |
| `:1621` | `# Fast-path: if the thread is in-memory (same process), signal directly.` |

Consumer side — `tools/workspace/worker_thread.py`:
`:1866 def _poll_command`, `:1876 cmd_path = self._worker_dir / "command.json"`,
call sites `:2183`, `:2688`, `:2754`, `:3027`.

Per-worker flags the command file ultimately sets: `:1131 _stop_event`,
`:1132 _pause_event`, `:1133 _resume_event`. Park loops (the W6 "deaf park"
fix): `:2744` and `:3022` —
`while self._pause_event.is_set() and not self._stop_event.is_set():`.

**Known defect that gates a second reader:** `:4787`
`# TODO(ticket): command.json carries no instance/id fence — a foreign`
(pre-start cleanup at `:4786-4790`). See §7.

### 2.4 Queues, reply channel, FIFO state

- `:1129 _input_queue: queue.Queue`, `:1130 _output_queue`
- `:1344 reply_q: queue.Queue` — created per synchronous call
- `:1140 _queued_job_ids: "collections.deque[str]"`, `:1141 _queued_lock` — the FIFO
- `web_ui/backend/bridge.py:461` — `and getattr(thread, "_current_reply_queue", None) is None`
  (a cross-module read of another object's private queue; it disappears with the boundary)

### 2.5 Event path — in-process only

| cite | verbatim / note |
|---|---|
| `web_ui/backend/global_routes.py:43` | `from agent.events import EventType, create_event, global_event_bus` |
| `:360` | `global_event_bus.publish(` |
| `tools/workspace/worker_registry.py:62` | `self._worker_event_bus_registry: Dict[Tuple[str, str, int], Any] = {}` |
| `worker_registry.py:151` / `:158` | `def register_event_bus(` / `def unregister_event_bus` |
| `web_ui/backend/bridge.py:799` | `current_bus = get_worker_event_bus(` (also `:1244`, `:1273`, `:1277`) |
| `:1188` | `worker_bus.subscribe(evt_enum, handler_fn)` |
| `:675`–`:749` | global subscriptions |
| `web_ui/backend/event_forwarder.py:52` | `def register_websocket(` — fan-out keyed by ws id |
| `web_ui/backend/server.py:2` | `FastAPI WebSocket server for the ThoughtMachine Web UI.` |

No broker, no `StreamingResponse`, no `asyncio.Queue`, no serialization layer
(verified absent by grep). The event path is a Python dict of live `EventBus`
objects plus a direct callback into `WebSocket.send()`.

### 2.6 Containers are already addressed by label

`tools/workspace/worker_thread.py:941 container_manager: Optional[Any] = None`,
`:957 self._container_manager`, `:98 from infra.container_manager import
ContainerManager as _CM`, `:4729` (None when Docker is unavailable), `:4741 _CM(`,
`:4763`, `:2471`, `:2341 container_id=_container_id`.
Ownership is a *label value*, not an object reference:
`tools/workspace/worker_container.py:54 def is_resource_container(container: Any) -> bool`,
`:61 ContainerManager.list_containers()`, `:64 labels = getattr(container, "labels", None)`,
exact `thoughtmachine.worker` label at `:30-36`, resource containers excluded at `:38`.

This is load-bearing for §4/§7: a brand-new process can reattach to its
containers by querying labels, so process replacement need not lose them.

---

## 3. Premise corrections

**(i) There is no `CancellationToken` class.** Grep for
`CancellationToken|cancel_token|class .*Canc|CancelScope|_cancelled|is_cancelled|cancel_event`
returns nothing. Cancellation today is per-worker `threading.Event` flags
(`worker_thread.py:1131 _stop_event`, `:1132 _pause_event`, `:1133 _resume_event`)
plus one module-global at `thoughtmachine/security.py:50 _prompt_cancelled`
(set `:1414`, cleared `:936`). The requirement is therefore **not** "move a
token across a boundary" — it is "make the *intent* to set those flags cross a
boundary, while the flags themselves stay per-worker objects wherever the
worker lives."

**(ii) `thoughtmachine/host_port.py` is a command launcher, not a write gate.**
Its docstring (`:1-8`) defines it as the single *host-execution* port —
":func:`host_run`", "the ONLY module in `thoughtmachine/` or `tools/` permitted
to import `subprocess`", re-checking identity, the workspace operator kill
switch, the effective grant, credential-shaped env, cwd containment and argv
typing. It never sees a file handle. Item C therefore routes through the
`security` gate (§9), not through `host_port`.

**(iii) The load-bearing finding: the command channel is already
filesystem-mediated.** `workspace_routes.py:1199` already writes a durable
`command.json`, and `worker_thread.py:1866/:1876` already polls it; the direct
method call is documented as a *fast-path*
(`workspace_routes.py:1621`) whose failure is explicitly survivable
(`:1233 # The file-based command will still work`).

So this wave is **not "invent IPC"**. Quantitatively:

- 4 command verbs × (1 file write that already exists at `:1199`, `:1511`,
  `:1620`) — no new command transport is required for correctness; the
  existing file path is the correctness path.
- What must actually be built: **1** event serialization + pump layer (§6),
  **1** reply channel for synchronous results (`:1344` `reply_q`, plus
  `bridge.py:461`'s private-queue read), **1** intent channel for the
  control verbs if we want them to stay synchronous/low-latency, and
  **1** fence on the existing file (`:4787`).
- What must be *retired*: the assumption that a supervisor can call
  `getattr(thread, action)()` on a live object (`worker_manager.py:783/:791`)
  and that a route can iterate `_worker_registry.items()` (`:1218/:1527/:1633`).

That is a materially smaller wave than "replace the threading model", and the
plan in §10 is phased accordingly: the two independent items (B, C) plus the
fence land first and alone.

---

## 4. DECISION — mechanism

**Chosen: one dedicated *worker-runtime process per session*, hosting all of
that session's workers, started with `multiprocessing.get_context("spawn")`,
supervised by the API process.**

Concretely: the API process keeps the route layer, the registry bookkeeping,
the WebSocket bridge, and one supervisor thread per live session runtime. Each
`WorkerRuntime` child process imports `tools.workspace.worker_thread` and runs
the *unchanged* `WorkerThread` objects (`worker_thread.py:912`) as threads
inside itself. Threads do not go away — they change **which process owns them**.

### 4.1 Why this one

| axis | this choice | why |
|---|---|---|
| FIFO `_queued_job_ids` (`worker_thread.py:1140/:1141`) | stays inside the runtime process, single writer | no cross-process lock is introduced for the ordering guarantee; `_queued_lock` remains a real (uncontended-by-strangers) lock |
| Memory per worker | one extra interpreter per *session*, amortised over its workers | the expensive per-worker resources — `ContainerManager` (`:4741 _CM(`) and tool caches — are already per-worker objects; a per-worker *process* would add an interpreter on top of each, not instead of it |
| Blast radius of one worker's crash | kills its session's runtime, not the API, not other sessions | session is already the natural coupling unit: `workspace_routes.py:1218/:1527/:1633` iterate *all* workers of a session to stop/pause/resume, and the workspace filesystem is shared per session anyway. Cross-session isolation is what the UI needs to stay alive |
| Container reattachment | free | containers are label-addressable (`worker_container.py:30-36`, `:54`, `:61`), so a replacement runtime adopts rather than recreates |
| Control plane survival | total | routes, registry and bridge stay in the API process, which no longer runs worker code |
| Registry ownership | single-writer | `worker_registry.py:52/:59/:63` locks stay meaningful because only the API process writes the authoritative registry; the child reports state, it does not mutate parent state |

### 4.2 Start method: `spawn`, not `fork`, not `forkserver`

- **`fork` is disqualified outright.** The API process is heavily threaded
  (`worker_manager.py:107 PeriodicStaleCheck(threading.Thread)`,
  `worker_thread.py:912`), holds Docker client sockets
  (`infra/container_manager.py:853 self.client = docker.from_env()`) and
  acquires locks (`worker_registry.py:52/:59/:63`,
  `worker_manager.py:142/:843`, `worker_lifecycle.py:54`). Forking duplicates
  live lock state and open sockets into the child; a lock held at fork time is
  held forever in the child. This is the classic fork-in-threaded-program
  deadlock and it is not a theoretical risk here.
- **`spawn` is chosen**: clean interpreter, no inherited FDs or locks, and the
  child re-imports what it needs. Cost is import time at runtime start — paid
  once per session, not per command.
- **`forkserver` rejected, with a caveat.** It would cut the import cost on
  repeated spawns, but it introduces a third process whose lifetime the operator
  must reason about, and it still copies the forkserver's state rather than a
  pristine one. Keep it as the documented fallback *only if* runtime spawn
  latency is measured to matter (unverified — §10).

Windows: `spawn` is the only supported method there anyway, and the repo
already uses loopback TCP for host-process signalling
(`start_windows.py:115 socket.socket(AF_INET, SOCK_STREAM)`,
`thoughtmachine/doctor.py:82`), so the transport in §5 has a precedent on that
platform.

### 4.3 Rejected alternatives

| rejected | reason |
|---|---|
| **One process per worker** | multiplies interpreter+import cost by worker count while the dominant per-worker memory (`ContainerManager`, tool caches) is unchanged; multiplies supervision state; and it buys crash isolation at a granularity finer than anything the product needs — the session is already the unit at which the UI kills/pauses/resumes (`workspace_routes.py:1218/:1527/:1633`) |
| **Separate deployable service** (its own process tree, possibly its own host/container) | needs transport security, discovery, deployment and a lifecycle story the project does not have; and it relocates the event problem (§6) rather than solving it — events still have to reach the API's `WebSocket.send()` |
| **Keep threads, harden them** (thread watchdog + `sys.settrace` + signal-based interrupt) | does not survive a segfault, an `os._exit`, or an OOM kill; GIL contention is unchanged; this is the status quo with more code |
| **`subprocess` + CLI protocol** | the child would have to re-implement structured job/reply/event traffic over argv/stdout; the socket in §5 is strictly simpler and the thread code needs no rewrite |
| **`fork`** | see 4.2 — live-lock inheritance in a threaded parent |

### 4.4 What the child does *not* do

The child never touches: the HTTP layer, `_worker_registry` (it reports worker
state via frames; the parent owns the dict), the WebSocket bridge, or
`command.json` writes (the parent keeps writing it,
`workspace_routes.py:1199/:1511/:1620`). It reads `command.json` — which is
exactly why the fence in §7 is a precondition, not an option.

---

## 5. IPC design — the contract

### 5.1 Transport

**One `AF_UNIX` `SOCK_STREAM` socketpair per runtime, created by the parent
before spawn and inherited by the child; length-prefixed JSON frames.**
On Windows, a loopback `AF_INET` connection with the port handed to the child
via spawn arguments (precedent: `start_windows.py:115`, `thoughtmachine/doctor.py:82`).

Why a socket rather than `multiprocessing.Queue`:

1. It is **duplex** — one object carries dispatch, reply, control and events,
   so there is exactly one ordering domain to reason about.
2. **EOF is a death signal.** A `Queue` gives no reliable peer-death edge; a
   socket read returning `b""` does, and §7 depends on it.
3. **No arbitrary pickling.** Payloads are JSON, so a frame cannot resurrect a
   `WorkerThread` object in the parent and quietly recreate the coupling we are
   removing.
4. It is **introspectable** — an operator can dump the frame stream.

Framing: 4-byte big-endian length, then UTF-8 JSON. `{"v": 1, "kind": ...,
"seq": ..., "worker": ..., "instance": ..., "payload": {...}}`.

### 5.2 Message inventory

| direction | kind | carries | replaces |
|---|---|---|---|
| parent → child | `dispatch_job` | `{job_id, query, context_preference, force}` | the in-process construction of a job in `worker_manager.py:647 request_worker` |
| child → parent | `job_accepted` | `{job_id, position}` | the sync return of the accepted job; `position` is read from `_queued_job_ids` (`:1140`) |
| child → parent | `reply` | `{job_id, ok, payload, error}` | `:1344 reply_q: queue.Queue` and the parent's `bridge.py:461 getattr(thread, "_current_reply_queue", None)` probe |
| parent → child | `control` | `{action: stop\|pause\|resume\|kill, worker_name, instance_id, fence}` | `workspace_routes.py:1228/:1230/:1534/:1641` direct calls |
| child → parent | `control_ack` | `{action, worker_name, instance_id, found, status}` | the dict returned by `worker_manager.py:771 _control_worker` (`"found"`, `"status": getattr(thread, "status", None)` at `:791`-region) |
| child → parent | `event` | `{seq, worker, instance, type, payload, ts}` | `EventBus.publish` inside the worker (`worker_registry.py:62` registry, `bridge.py:1188` subscribe) |
| child → parent | `heartbeat` | `{ts, live_count, queued}` | `worker_manager.py:336 _heartbeat_age(thread)` polling of live thread objects |
| child → parent | `worker_state` | `{worker_name, instance_id, status, container_id}` | parent-side reads of `getattr(thread, "status", None)` and `:2341 container_id=_container_id` |

### 5.3 Job dispatch and FIFO

The FIFO is *not* split. `_queued_job_ids` (`:1140`) and `_queued_lock` (`:1141`)
live in the child. The parent may submit `dispatch_job` frames faster than the
child drains them; the socket is a stream, so submission order == child arrival
order, and the child's deque remains the single source of ordering truth.
The parent must **not** keep a shadow queue — `position` from `job_accepted` is
the only thing the parent is allowed to believe.

### 5.4 Synchronous reply (`reply_q`, `:1344`)

The HTTP request is served by the parent. The child, on completing a job,
emits one `reply` frame carrying `job_id`. The parent keeps a
`{job_id: asyncio.Future}` map; the pump thread resolves the future from the
pump thread via `loop.call_soon_threadsafe`. `reply_q` (`:1344`) stays as the
in-child handoff *within* one worker thread's job — it just no longer terminates
in the parent process.

Invariant to preserve: exactly one `reply` per `job_id`, including on failure.
The child must emit a terminal `reply` (`ok: false`) on every abandoned job —
see §7's cancellation-death case.

### 5.5 stop / pause / resume / kill

Two paths, deliberately, mirroring today:

1. **Durable path (unchanged):** parent writes `command.json`
   (`workspace_routes.py:1199/:1511/:1620`) and the child's
   `worker_thread.py:1866 _poll_command` reads `:1876` it. This is the
   correctness path and it keeps working if the socket is wedged.
2. **Intent path (new):** a `control` frame. On receipt the child performs the
   *same* thing the parent used to do directly — call `thread.stop()` /
   `thread.pause()` / `thread.resume()` / `thread.kill()` — i.e. the semantics
   of `workspace_routes.py:1228/:1230/:1534/:1641` are preserved verbatim, just
   executed on the correct side of the boundary.

The parent's `except Exception: pass  # The file-based command will still work`
(`:1233`) is **upgraded**: a failed `control` frame must be logged at warning
with the worker/instance, because under isolation "the thread was in-memory"
(`:1621`) is never true.

### 5.6 Cancellation flag propagation — the triple

`_stop_event` / `_pause_event` / `_resume_event` (`:1131/:1132/:1133`) **do not
cross**. Only intent crosses. Rationale: an `Event` is a live object with
waiters; serialising it would mean reconstructing waiter semantics in the
parent, which is strictly worse than sending the verb.

The one real change inside the worker loop: the park loops at `:2744` and
`:3022` —

```
while self._pause_event.is_set() and not self._stop_event.is_set():
```

— must additionally become **socket-responsiveness-aware**, i.e. the park wait
must also wake on (a) `control` frame arrival and (b) socket EOF, so that
"resume" cannot be missed while parked and so that a dead parent does not leave
a permanently parked worker. This is the only modification to the worker's own
control flow that this wave requires, and it is small: replace the blind wait
with a wait whose timeout is short (e.g. the existing poll interval used by
`_poll_command` at `:1866`) plus an explicit EOF check.

The global `_prompt_cancelled` at `thoughtmachine/security.py:50` remains
process-local in the child. Note the consequence: a prompt-cancellation issued
by the parent's HTTP layer cannot reach a child directly — it must be a
`control` frame variant. This is a *new* requirement created by the boundary
and is listed in §10 for a decision on shape.

### 5.7 What the parent still owns

`_worker_registry` (`workspace_routes.py:42`), `_registry_lock` (`:43`), the
manager façade (`worker_manager.py:447/464/517/...`), the bridge
(`bridge.py:799/:1188`) and the WebSocket fan-out
(`event_forwarder.py:52`). The child is a *state reporter*, not a registry
writer — which is what keeps `worker_registry.py:52/:59/:63` single-writer.

---

## 6. Events and streaming to the UI — the genuine new build

This is the only part with no existing transport to lean on.

**What exists:** an in-memory dict of live `EventBus` objects keyed
`(session, name, instance)` (`worker_registry.py:62`), unregistered at `:158`,
looked up by name in the bridge (`bridge.py:799 get_worker_event_bus(`,
`:1244`, `:1273`, `:1277`), subscribed with a Python callable
(`:1188 worker_bus.subscribe(evt_enum, handler_fn)`), and delivered by a direct
callback into `WebSocket.send()` (`event_forwarder.py:52 register_websocket`).
There is no serialization layer, no broker, no `asyncio.Queue` (verified absent).

**Replacement:** the child serialises every event it publishes into an `event`
frame on the §5 socket, stamped with a monotonic `seq` that the child assigns
inside its own bus wrapper. The parent runs one **event pump thread per
runtime** that:

1. reads frames in arrival order,
2. re-publishes each into a parent-side `EventBus`-shaped *shim* registered in
   the existing registry (`worker_registry.py:151 register_event_bus`), so
   `bridge.py:1188` and `event_forwarder.py:52` need **no change** — they
   subscribe to the shim exactly as they subscribe to a real bus today;
3. drops frames whose `seq` is not `last_seq + 1` per `(worker, instance)`,
   logging a gap (the child is the only writer, so a gap is always a bug or a
   dropped connection, never legitimate reordering).

**Ordering argument:** per-worker order is guaranteed by construction — one
child, one socket, one `seq` issuer per worker. Global order across workers is
*not* claimed and is not needed: the UI already treats per-worker streams
independently (`event_forwarder.py:52` keys callbacks by WebSocket id, not by
worker). Cross-socket ordering is therefore out of scope by design, not by
oversight.

**Backpressure:** the pump must not block the worker. When the pump's queue is
over a high-water mark, the child's `event` emission must shed *lossy* event
classes (partial tokens) before *structural* ones (spawn/status/completed/error).
Classification of lossy vs structural is a decision to be made against
`agent.events.EventType` (§10 — I did not enumerate the enum).

---

## 7. Failure, recovery, and fencing

### 7.1 Detecting runtime death

Two independent signals, both required:

1. **EOF on the control socket** — a read returning `b""` (§5.1, reason 2).
2. **`waitpid` from the supervisor** — the parent's existing supervisor thread
   (`worker_manager.py:107 PeriodicStaleCheck(threading.Thread)`, started at
   `:768`) already exists to poll liveness; it gains child-process reaping.

EOF alone is insufficient: a half-closed peer or a child that closed the socket
before dying yields a false positive; `waitpid` alone is insufficient when the
child is alive but wedged. A runtime is declared dead when EOF **and**
`waitpid` agree, or when `heartbeat` (§5.2) is stale past a threshold —
`worker_manager.py:336 _heartbeat_age` is the existing precedent for the
staleness arithmetic and should be reused, not duplicated.

### 7.2 Reconciliation, and orphan containers

On runtime death the parent must, in this order:

1. Mark every worker of that session as `lost` in its authoritative registry
   (it owns `_worker_registry`, §5.7) — never leave the UI showing `busy` for a
   process that is gone.
2. Fail every in-flight job with a terminal `reply`-equivalent
   (`ok: false`, `error: "runtime_lost"`), so no HTTP request waits forever
   (§5.4 invariant).
3. Query containers by label — `worker_container.py:30-36` (exact
   `thoughtmachine.worker` label), `:54 is_resource_container`, `:61
   ContainerManager.list_containers()`, `:64 labels = getattr(container,
   "labels", None)` — and decide per container: **reclaim** (kill, the default,
   matching `stop` semantics) or **adopt** into a replacement runtime. Resource
   containers are excluded at `:38` and must never be touched by this path.
4. Only then start a replacement runtime, which re-registers its workers via
   frames and re-attaches adopted containers by label. No `ContainerManager`
   object survives the parent's restart — and it does not need to, because
   ownership was never object identity (`:64`, `:30-36`).

`bridge.py:125`'s and `workspace_routes.py:42`'s private-dict reads keep
working through all of this because the dict is still there; what changed is
*who writes it*.

### 7.3 The `command.json` fence — a hard precondition

`worker_thread.py:4787` carries the open ticket:

```
# TODO(ticket): command.json carries no instance/id fence — a foreign
```

with pre-start cleanup at `:4786-4790`. Today this is tolerable because there is
exactly one reader, in the same process as the only writer
(`workspace_routes.py:1199/:1511/:1620` → `worker_thread.py:1876`).

**Under this design the child process becomes a second reader, so the fence must
land before any child may read `command.json`.** Consequences if it does not:
a stale command file from a previous instance of the same worker name is
consumed by the wrong instance; the pre-start cleanup at `:4786-4790` races a
live writer; a `kill` intended for instance 1 terminates instance 2.

Fence shape (contract, not code): the file gains `session_id`, `worker_name`,
`instance_id` and a monotonic `epoch`/nonce generated by the parent. The child
applies a command only if all four match its own identity; on mismatch it
**ignores and renames** the file (never deletes) and logs the rejection with
both identities. Phase ordering in §10 enforces this as a gate: the fence ships
before the runtime does.

---

## 8. Item B — `pids_limit`

### 8.1 Where the field goes

| what | where | verbatim precedent |
|---|---|---|
| constant | `agent/config/defaults.py` next to `:241`/`:242` | `:241 DEFAULT_MEM_LIMIT = "512m"`, `:242 DEFAULT_CPU_QUOTA = 100000` |
| create spec field | `infra/container_create.py:286-287` | `:286 mem_limit: str = DEFAULT_MEM_LIMIT`, `:287 cpu_quota: int = DEFAULT_CPU_QUOTA` |
| profile spec field (must stay in parity) | `infra/container_registry.py:147-148` | `:147 mem_limit: str = DEFAULT_MEM_LIMIT`, `:148 cpu_quota: int = DEFAULT_CPU_QUOTA` |
| spec allowlist | `infra/container_registry.py:502` | `"mem_limit", "cpu_quota", "oom_score_adj", "labels", "environment",` |
| drift-comparison field tuple | `infra/container_manager.py:702-703` | `:702 "mem_limit",`, `:703 "cpu_quota",` |
| drift normaliser | `infra/container_manager.py:763` | `if key == "mem_limit":` — `pids_limit` is an int, so it needs no unit normalisation, but it *must* be listed or drift detection silently skips it |
| resource-container defaults | `infra/container_registry.py:693-694` | `:693 mem_limit=RESOURCE_MEM_LIMIT,`, `:694 cpu_quota=RESOURCE_CPU_QUOTA,` |

`pids_limit` is passable as a kwarg: containers are created through the Docker
**SDK**, whose single seam is `infra/container_manager.py:2102 def
_run_container(...)` with docstring `:2104 "the single ``run`` call site"`,
client at `:853 self.client = docker.from_env()`, and the kwargs dict built in
`infra/container_create.py:439-441` /
`:448 restart_policy=...`. No `subprocess`/docker-CLI creation path exists in
that module.

### 8.2 The seam, and the five hand-set sites

The kwarg is added at the seam — `infra/container_create.py:439-441`
(`mem_limit`, `memswap_limit`, `cpu_quota`) gains `pids_limit=spec.pids_limit`.
But the seam is not the whole surface: **five sites currently hand-copy limits**,
which is the drift surface.

| # | site | what it sets |
|---|---|---|
| 1 | `infra/container_create.py:395-396` | `mem_limit=spec.mem_limit,` / `cpu_quota=spec.cpu_quota,` on the admission `ContainerSpec` rebuild |
| 2 | `infra/container_create.py:439-441` | `mem_limit=` / `memswap_limit=spec.mem_limit,` / `cpu_quota=spec.cpu_quota,` on the actual create kwargs |
| 3 | `infra/container_registry.py:291-292` | `mem_limit=profile.mem_limit,` / `cpu_quota=profile.cpu_quota,` |
| 4 | `infra/container_registry.py:331-332` | `mem_limit=profile.mem_limit,` / `cpu_quota=profile.cpu_quota,` |
| 5 | `infra/container_registry.py:693-694` | `mem_limit=RESOURCE_MEM_LIMIT,` / `cpu_quota=RESOURCE_CPU_QUOTA,` |
| — | `infra/container_manager.py:813-814` | `mem_limit=DEFAULT_MEM_LIMIT,` / `cpu_quota=100000,` (constructor defaults) |

### 8.3 Latent defect to fix in the same change

`infra/container_manager.py:814 cpu_quota=100000,` hardcodes a literal where
`DEFAULT_CPU_QUOTA` belongs — `agent/config/defaults.py:242
DEFAULT_CPU_QUOTA = 100000`. The values are byte-identical today, which is
exactly why it has not been noticed: the constant can change and this site will
silently not follow. Fix it and pin the equality with a test.

### 8.4 The parity test that prevents recurrence

Mirror the `_DISK_FAIL_CLOSED_CEILING` lesson: a single constant enforced at one
call site does not constrain the others. The regression test must therefore be
**structural, not assertive**:

- Define one exported list of supported limit field names (the same list that
  seeds `container_registry.py:502` and `container_manager.py:702-703`).
- Walk every create site in §8.2 by introspection (the spec dataclasses at
  `container_create.py:286-287` and `container_registry.py:147-148`) and assert
  that each spec type carries **every** field in that list.
- Assert that each create/seam site passes every field, so a sixth site added
  later fails by construction rather than by review.
- Assert `container_manager.py:814`'s literal equals `DEFAULT_CPU_QUOTA`.

### 8.5 Exec surface

`pids_limit` bounds processes inside the container's cgroup, which includes
`docker exec` children, so the exec paths need no separate limit:
`infra/container_manager.py:2533`, `:2555 container.exec_run(**exec_kwargs)`,
`:3535`, `:3918`; `infra/containers/executor.py:1097`/`:1168`;
`infra/resource_container_manager.py:1548`; wrapper
`tools/workspace/worker_execution.py:373`. The ~25 s cap is **not** in
`infra/container_manager.py` (grep for `EXEC_TIMEOUT|DEFAULT_TIMEOUT|TIMEOUT_S`
there: none) — it is imposed by a caller, above the manager; `:2555`'s
`exec_kwargs` contains no timeout key (§10).

---

## 9. Item C — the host-write gate

### 9.1 The finding (closes the earlier open question)

`tools/visual_tool.py:295` **does** declare grains:

```
required_categories: ClassVar[List[str]] = ["visual:view", "container:true"]
```

It declares `visual:view` and `container:true` — and **not** `filesystem:write`.
So the tool is not "ungated"; it is gated on the wrong axes for the one thing it
does that touches the host. The artifact write at `tools/visual_tool.py:501
host_artifact.write_bytes(data)` (in `try:` / `except OSError`) has no
`filesystem:write` declaration and no per-operation re-check.

Write surface, for completeness: `:84 ARTIFACT_DIR = "artifacts/visual"`,
`:89 MAX_ARTIFACT_BYTES = 8 * 1024 * 1024`,
`:380 artifact_stem = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"`,
`:381 artifact_name`, `:382 host_dir = Path(workspace) / ARTIFACT_DIR`,
`:384 host_dir.mkdir(parents=True, exist_ok=True)`, `:390 host_artifact`,
`:491` size cap, `:495` PNG-magic check, `:501` the write.

### 9.2 The gate to reuse (pinned)

The callee at `tools/file_editor.py:130` is **`check_atomic_operation`**, imported
locally at `tools/file_editor.py:128`:

```
from security.security_gate import check_atomic_operation
```

Definition — `security/security_gate.py:1386`:

```
def check_atomic_operation(
    operation_required: str,
    effective_permissions: dict,
    tool_name: str,
    description: str = "",
) -> bool:
```

docstring `:1393` "Synchronous in-tool re-check of an operation." and `:1395`
"If the permission level is 'ask', treats it as DENIED (escalation not
pre-approved)."

Note this is **`security/security_gate.py`, not `thoughtmachine/security.py`** —
a correction to earlier recon, which cited `thoughtmachine/security.py:1066
is_allowed(agent_id, tool_name, security_config=None, **kwargs)`. That function
is the config-level tool allowance check; it is not the per-operation atomic
re-check and must not be used as the template for a single write.

### 9.3 The change (two parts, both declarative)

1. **Declare the grain** — add `"filesystem:write"` to the list at
   `tools/visual_tool.py:295`. It is consumed generically:
   `tools/base.py:81 required_categories: ClassVar[List[str]] = []`,
   `:105 def get_required_categories(cls, params: dict | None = None) -> list[str]:`,
   `:120 return cls.required_categories`. Same shape as
   `tools/file_editor.py:13`, `tools/apply_edits.py:218`,
   `tools/code_modifier.py:22`, `tools/directory_creator.py:9`,
   `tools/file_mover.py:10`, `tools/container_control.py:340`,
   `tools/docker_code_runner.py:61`.
2. **Re-check atomically immediately before the write** at
   `tools/visual_tool.py:501`, with the same four arguments as
   `tools/file_editor.py:130`: the grain string `"filesystem:write"`,
   `self.effective_permissions or {}`, the tool name `"VisualTool"`, and a
   description naming the destination path (`host_artifact`).

### 9.4 Refusal semantics — error payload, not exception

`tools/file_editor.py:135-136` returns, it does not raise:

```
return json.dumps({
    "error": f"Atomic permission check failed: filesystem:write required for {operation}",
```

`tools/visual_tool.py` already has the refusal plumbing to match: `:331
def _envelope(self, *, success: bool, duration: float,` and `:351 def _fail(self,
error: str, started: float) -> str:`. The gate refusal must go through `_fail`
with a message of the same shape, so the tool contract (structured envelope,
no exception escaping into the agent loop) is preserved. Because
`check_atomic_operation` treats `ask` as **denied** (`security_gate.py:1395`),
a non-pre-approved session gets a clean refusal rather than a prompt — identical
to `FileEditor`, which is the point.

### 9.5 Explicitly the wrong component

`thoughtmachine/host_port.py` must **not** be used here. Its docstring `:1-8`
defines it as the single *host-execution* port (`host_run`), "the ONLY module in
`thoughtmachine/` or `tools/` permitted to import `subprocess`", re-checking
identity, the workspace operator kill switch, the effective grant,
credential-shaped env, cwd containment and argv typing. It governs **spawning
processes on the host**, never writing files. Routing the Visual artifact write
through it would inherit none of the intended check and would misrepresent what
the audit trail means.

---

## 10. Phasing, tests, risks

### 10.1 Phases (each independently shippable, in this order)

| phase | content | tests | why here |
|---|---|---|---|
| **P0** | Item C: `filesystem:write` grain + atomic re-check in `tools/visual_tool.py` (§9) | refusal returns envelope (`:351 _fail`), allowed path unchanged, `ask` → denied | one file, no runtime coupling; can ship today |
| **P1** | Item B: `pids_limit` field + constant + seam + 5-site parity + `container_manager.py:814` literal fix (§8) | structural parity test over spec fields and create sites; literal-equals-constant test | no runtime coupling |
| **P2** | `command.json` fence (§7.3) | fence replay (foreign instance ignored, not applied), pre-start cleanup race test | **hard gate** for P3 — a second reader is only safe after this |
| **P3** | Extract `WorkerRuntime` behind the §5 socket; parent becomes a client for dispatch/reply/control | control parity table (stop/pause/resume/kill behave identically direct vs frame); FIFO order under concurrent submit; exactly-one-reply invariant; park-loop resume-not-missed while parked (`:2744`, `:3022`) | the boundary itself |
| **P4** | Event serialisation + per-runtime pump + registry shim (§6) | per-worker seq monotonicity; gap detected and logged; bridge/forwarder unchanged and passing their existing tests; backpressure sheds lossy before structural | needs P3's transport |
| **P5** | Death detection, reconciliation, label-based container reclaim/adopt (§7.1-7.2) | EOF+waitpid agree; stale heartbeat; `runtime_lost` replied to in-flight jobs; resource containers never touched (`:38`) | needs P3+P4 |
| **P6** | Retire the fast-path assumptions: `workspace_routes.py:1233` swallow → logged fallback; `worker_manager.py:783/:791` string-keyed dispatch → frame dispatch; remove `bridge.py:461` private-queue read | regression: no route iterates a live dict of thread objects for dispatch; `_current_reply_queue` gone | cleanup, after behaviour is proven |

P2-before-P3 is not a preference. It is the ordering that keeps the file channel
sound when the number of readers changes from one to two.

### 10.2 Risks

- **Serialisation of `query`** — `worker_manager.py:647 request_worker` takes
  `query: Any`; if job payloads carry non-JSON types today, spawn-by-JSON
  forces a normalisation layer. Not verified (§10.3).
- **Two-process lock ownership** — `worker_registry.py:52/:59/:63` and
  `worker_manager.py:142/:843` must stay parent-only. If any child acquires
  them, the single-writer argument in §4.1 collapses.
- **Spawn latency per session** — import cost is paid per runtime start; if the
  UI has a session-count-heavy usage pattern this is the argument for
  `forkserver` (§4.2).
- **Windows transport** — the §5.1 fallback is asserted from precedent
  (`start_windows.py:115`, `doctor.py:82`), not from a working prototype.
- **Prompt cancellation is now cross-process** —
  `thoughtmachine/security.py:50 _prompt_cancelled` (set `:1414`, cleared
  `:936`) is process-local; a parent-issued cancellation needs a `control`
  variant (§5.6). New work created by the boundary.

### 10.3 Open questions / not verified

1. Whether a **durable job record** exists that survives runtime death, so that
   `_queued_job_ids` (`worker_thread.py:1140`) can be re-queued after a crash.
   I did not locate one; if it does not exist, P5 must either add it or fail
   queued jobs explicitly with `runtime_lost`.
2. What else is constructed in-process at worker spawn around
   `worker_thread.py:4751` / `:4854` (tool singletons, caches) and therefore what
   `spawn` re-import cost actually is — I did not read that function's body.
3. Whether every job payload (`query: Any`, `worker_manager.py:647`) is
   JSON-serialisable end to end.
4. The location of the ~25 s exec cap: confirmed **absent** from
   `infra/container_manager.py`; not located above it.
5. The exact `_DISK_FAIL_CLOSED_CEILING` precedent (§8.4) is cited from the
   task brief; I did not open the ticket or test that names it.
6. Whether `bridge.py:461`'s `_current_reply_queue` has readers other than the
   one line found.
7. The `agent.events.EventType` enum was not enumerated, so the
   lossy-vs-structural event split in §6 is a requirement, not a classification.
8. Windows `AF_INET` transport for §5.1 is untested.

---

Authored from static reconnaissance only: no source file was modified, no
container was started, and no test was executed.
