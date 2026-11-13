# Design: the worker process substrate — P3 revised against shipped code (C63)

**Status: revision, not proposal.** This document re-decides the twelve decisions of
`docs/design-worker-runtime-process.md` (P3, cited below as **P3**) against the substrate that
now ships: `tools/workspace/worker_runtime.py`, `worker_runtime_process.py`,
`worker_runtime_main.py`, `worker_runtime_protocol.py`, `worker_stale_watchdog.py`.
Where P3 and the code disagree, the code wins and the P3 decision is recorded as
**OVERTURNED**. Three decisions are overturned: **D4, D6, D11**.

**Citation convention.** Symbols are authoritative; line numbers are a snapshot taken on this
branch and will drift. Where a claim is code fact it carries `path:line` or a symbol. Where it
is design consequence it is tagged `[inferred]`. Where it could not be checked it is
`UNVERIFIED` — never guessed, and no line number is invented for it.

**Relationship to P3.** P3 stays in the tree as history and is not rewritten. This document
**supersedes P3 wherever the table below says OVERTURNED**, and is additive elsewhere: the
five questions in §B are new, and they are what the substrate work is actually about.

## A. Decision register, re-decided

| id | P3 said (quoted) | verdict | evidence today |
|----|------------------|---------|----------------|
| D1 | "One process per worker **instance**, not per session and not a pool" | **KEPT** | `worker_runtime_process.py:8` — "One :class:`ProcessWorkerRuntime` per worker *instance* (D1)". The spec's identity axes are `worker`/`instance`/`epoch` (`spec_from_worker`, `worker_runtime_process.py:237-243`). |
| D2 | "The supervisor *is* the existing uvicorn process; `WorkerManager` becomes it" | **KEPT (in substance)** | No `WorkerManager` class was renamed; the supervisor role landed as a module-level handle registry in the API process: `_RUNTIME_HANDLES` (`worker_runtime.py:112`), populated at spawn (`worker_thread.py:6918`) and documented as "POPULATED IN PRODUCTION since S7a-3" (`worker_runtime.py:106-112`). The *process* claim (supervisor = uvicorn PID) holds; the *naming* claim did not happen. |
| D3 | "`spawn` a fresh interpreter, one `AF_UNIX` `socketpair` per runtime" | **KEPT** | `worker_runtime_process.py:416-417`: `multiprocessing.get_context(self._start_method)` then `socket.socketpair(...)`. No `fork`, no TCP. |
| D4 | "Socket is the **primary** control path; `command.json` survives as the crash-path recovery channel only" | **OVERTURNED** | The file channel was not demoted. The supervisor's own `WorkerThread` is still started unconditionally (`worker_thread.py:7087` `thread.start()`), and `worker_thread.py` has **no mode switch at all** (grep for `MODE_PROCESS`/`runtime_mode` finds only a `"runtime_mode": None` dict key at `:6264`, `:6281`). So the in-process body still runs *and* the child runs: two `_poll_command` readers on one `worker_dir`, not one reader plus a recovery file. P3's own §6.8 rule — "the process that hosts the runtime is the only command reader" — is violated by construction. |
| D5 | "The fence stays filesystem-based; **do not** move it onto the socket" | **KEPT, with a new cost** | `CommandFence`/`fence_rejection_reason` unchanged and still the only arbiter of `command.json`. But the frame envelope now carries its *own* `worker`/`instance`/`epoch` triple (`worker_runtime_process.py:441-443`), populated from the pickled spec, so the fence is asserted in two places from two sources that can disagree — see D6. |
| D6 | "The supervisor is the single generation minter; the runtime **verifies**, never allocates" | **OVERTURNED** | The runtime allocates. The child is built by `_build_worker` (`worker_runtime_main.py:748-762`) → `WorkerThread(**kwargs)` → `self._generation: int = self._allocate_generation()` (`worker_thread.py:1544`; again in `reset()`, `:2621`). The supervisor hands over its *own* thread's counter and takes it back as an echo: `spec_from_worker(self._worker, epoch=getattr(self._worker, "_generation", 0))` (`worker_runtime.py:847` region), checked as `hello.epoch_seen != int(self._spec["epoch"])` → `WorkerRuntimeLost(reply=_terminal_reply("", "epoch_mismatch"))` (`worker_runtime_process.py:467-473`). That check compares the child against **the value the supervisor just sent it**, not against `worker_dir/generation`; P3's "assert `int(sidecar) == epoch` … fail closed" was never implemented. |
| D7 | "`_turn_cancel` … stay inside the runtime, unchanged" | **KEPT** | Cancellation is still per-thread in the child; the boundary carries the request. `CANCEL_TERMINAL_STATUSES` is the single acceptability rule for both modes (`worker_runtime.py:98`), consulted by the in-process handle and by `worker_runtime_main._handle_cancel` (`worker_runtime.py:92-103` names both). |
| D8 | "No durable job record in P3; in-flight jobs are declared `runtime_lost`" | **KEPT** | `runtime_lost` is the terminal reply everywhere a runtime is gone: `worker_runtime_process.py:537`, `:794`, `:805`, `:816`, `:826`, `:1041`. Whether `WorkerJobRegistry` actually closes its entries on that path is **UNVERIFIED** (not read this pass). |
| D9 | "Reply correlation by `query_id` string, not by handing over a `queue.Queue`" | **KEPT** | The boundary carries `query_id`; the `queue.Queue` survives only *inside* the child, where it is legal: `worker_runtime_main.py:345` `reply_q: "queue.Queue[Any]" = queue.Queue()` feeding `worker._input_queue.put((query_id, query, reply_q))`. Supervisor side is a pending map of futures, not a queue (`worker_runtime_process.py:8-14`). |
| D10 | "Frames are versioned `v:1`; unknown `kind` is a protocol error, unknown *extra keys* are ignored" | **KEPT** | `PROTOCOL_VERSION = 1` (`worker_runtime_protocol.py:105`), `REQUIRED_ENVELOPE_KEYS = ("v", "kind", "seq")` (`:119`), version refusal at `:804`. |
| D11 | "One flag, `WORKER_RUNTIME_MODE` ∈ `{thread, process}`, default **`thread`** through S4" | **OVERTURNED** | `DEFAULT_RUNTIME_MODE = MODE_PROCESS` (`worker_runtime.py:83`, "per C62"); `thread` is now "the explicit opt-in" (`worker_runtime.py:81`). Resolution still fails closed and is still read per spawn (`resolve_runtime_mode`, `:310-331`). |
| D12 | "The runtime keeps emitting `event` frames that the supervisor re-publishes onto the existing per-worker `EventBus`" | **KEPT** | Child side installs the frame sink and subscribes its bus: `worker._frame_sink = outbox.put` (`worker_runtime_main.py:311`) and `_subscribe_child_events` / `_subscribe_worker_bus` (`:257-265`). |

## B. The five questions the substrate must answer

Each question is stated as a rule in MUST / NEVER form, then answered against today's code.

### B1. The spawn seam, and the body that is still in the supervisor

**MUST:** exactly one body per worker incarnation owns `worker_dir` — one writer of
`status.json`, one claimer of `command.json`, one holder of `_generation`.
**NEVER:** two live bodies may run the same worker's loop, and a control command may be claimed
by any body other than the incarnation it was fenced for.

**Today: there are two bodies.** The spawn seam builds the supervisor-side handle and then
starts the thread unconditionally:
`attach_thread_frame_sink(thread)` at `worker_thread.py:6918` (its comment says the handle "is
built here and never started"), immediately followed by `thread.start()` at `worker_thread.py:7087`.
Nothing between them reads the runtime mode, and `worker_thread.py` contains no mode branch
(the only `runtime_mode` occurrence is a `"runtime_mode": None` dict key at `:6264`, `:6281`).
The child is spawned lazily at the first verb (`_ensure_process_runtime`, `worker_runtime.py:819-847`),
and it builds *another* `WorkerThread` and starts it (`worker_runtime_main.py:300`, `:311`).

Consequences, all of them "two bodies writing the same files":

| file | supervisor body | runtime body | why it is not benign |
|------|-----------------|--------------|----------------------|
| `status.json` | `_write_status_file` from the started thread | the same function, in the child | both resolve the same path: the spec passes `str(worker.workspace_dir)` (`worker_runtime_process.py:225`), so the child writes the supervisor's file with its own status. Last writer wins; readers cannot tell which body answered. |
| `command.json` | `_poll_command` in the started thread | `_poll_command` in the child | claim-by-rename makes the claim exclusive but **not correct**: whichever body claims first consumes the command, and the other never sees it. A `kill` aimed at the child can be eaten by the supervisor's thread. `[inferred from the claim mechanism; the loss is not observed in a test]` |
| `worker_dir/generation` | holds `_generation` from construction | `_allocate_generation()` again at `worker_thread.py:1544` | the sidecar advances while the supervisor's cache does not — see B2. |

So the spawn seam is real (a handle exists and is registered), but the **seam is not a cut**:
the in-process body was never retired. That is the substance of the C66 gap.

### B2. Identity handover: inheritance, or re-derivation from disk?

**MUST:** the incarnation's epoch is handed to the child **as data** in the spawn spec, and the
child treats it as read-only.
**NEVER:** the child may mint or re-derive its own epoch; and no second source of the epoch may
exist that can disagree with the first.

**Today: handover *and* re-derivation, i.e. both.** Handover is real and picklable:
`spec_from_worker(worker, *, epoch=None)` puts `{"worker", "instance", "epoch",
"worker_factory", "worker_kwargs"}` in the spec (`worker_runtime_process.py:189-243`, `epoch` at
`:240`); the child unpacks it (`epoch = int(spec["epoch"])`, `worker_runtime_main.py:286`); the
emitter stamps every frame with it (`worker_runtime_process.py:441-443`) and `hello` echoes it
back as `epoch_seen` (`worker_runtime_main.py:325`). That half is exactly P3's D4/D10 shape.

Re-derivation is also real: the child constructs the body with `WorkerThread(**kwargs)`
(`_build_worker`, `worker_runtime_main.py:748-762`), whose `__init__` runs
`self._generation: int = self._allocate_generation()` (`worker_thread.py:1544`). So the child
opens the sidecar, computes `+1`, and writes it. The supervisor's `WorkerThread` object still
holds the pre-increment value it passed as `epoch`. **Two different epochs now describe the same
incarnation**: the supervisor's (`E`, what it fenced the spec with) and the child's (`E+1`, what
the child will fence its own `command.json` writes with, via `_fence()`).

The failure this produces is silent, not loud: `Hello.epoch_seen` is compared to the
supervisor's own `spec["epoch"]` (`worker_runtime_process.py:467-473`), which the child echoes
verbatim — so the check passes while the child's *actual* fence epoch is `E+1`. Nothing compares
either value to `worker_dir/generation`, and `fence_rejection_reason` — the only validator — is
never asked. Net effect: a control command written by the supervisor for epoch `E` is
quarantined by the child that believes it is `E+1`, and vice versa. `[inferred: mechanically
follows from `_fence()` using `_generation`; not proven by a test in this pass]`

**Answer:** identity is inherited *as data* and re-derived *as state*. The doc-level rule should
be: inheritance wins, re-derivation is forbidden — the child must accept `epoch` as given (no
`_allocate_generation()` call in the child construction path) or the fence and the envelope must
be made to share one source. Until one of those lands, do **not** claim "one epoch per
incarnation".

### B3. The IPC contract: frames, not queues; and what a mid-stream death looks like

**MUST:** the supervisor↔runtime boundary is length-prefixed frames on one `AF_UNIX`
`socketpair` (4-byte big-endian length + UTF-8 JSON), one emitter and one decoder per direction,
`seq` strictly increasing per direction, envelope `v`/`kind`/`seq`/`worker`/`instance`/`epoch`.
**NEVER:** a `queue.Queue`, a `multiprocessing.Connection`, a `Pipe` or any object reference may
cross the boundary; and a partially-read frame may never be interpreted.

**Today: frames, correctly.** `socket.socketpair` at `worker_runtime_process.py:417`; one
`FrameEmitter` per direction constructed with the fence triple (`:441-443`), one `FrameDecoder`
per direction (`:445` supervisor side, `worker_runtime_main.py:334` child side); `PROTOCOL_VERSION
= 1` and the required-key tuple in `worker_runtime_protocol.py:105`, `:119`. `queue.Queue`
appears twice and **never crosses**: `_Outbox._q` (`worker_runtime_process.py:258`) is a
supervisor-internal handoff, and `reply_q` (`worker_runtime_main.py:345`) is the child's
translation into the thread's existing `_input_queue` triple — which is exactly what D9 licenses.
No `Connection` or `Pipe` is used anywhere in the substrate.

**Mid-stream child death.** Child side: the read loop is `chunk = sock.recv(RECV_CHUNK_BYTES)`;
`if not chunk:` is treated as "the supervisor is gone, so nothing else will ever stop this
worker" → `_kill_on_link_loss(worker)` then `break` (`worker_runtime_main.py` recv loop, `:339`
region, `:345` inside it). Supervisor side: a dedicated reader thread
(`worker-runtime-reader-<name>`, `worker_runtime_process.py:446-452`) decodes what arrived;
death fails every pending waitable with a terminal `runtime_lost` reply
(`worker_runtime_process.py:1041`) rather than leaving a caller parked. A **truncated** frame is
never interpreted: the decoder reassembles from `RECV_CHUNK_BYTES`-sized reads and only yields
whole frames (`RECV_CHUNK_BYTES` = 65536, `:82-85` region). What is *not* implemented is
mid-stream *resynchronisation* — P3 §9.5 said kill, and the code agrees by construction.

### B4. Lifecycle: death must be an EOF, not a poll interval

**MUST:** death of the child is detected from the transport (EOF on the socketpair) plus
`waitpid` agreement, on the order of one socket read — not from a cache age.
**NEVER:** a heartbeat or stale age of tens of seconds may be the *only* death signal, and a
crash may never be reported as a query timeout.

**Today (measured constants, not a measured latency):**

| signal | value | where |
|--------|-------|-------|
| `HEARTBEAT_INTERVAL_S` | 30 s | `agent/config/defaults.py:41` |
| `HEARTBEAT_STALE_AFTER_S` | 600 s | `agent/config/defaults.py:43` |
| `WORKER_STALE_WATCHDOG_INTERVAL_S` | 30 s | `agent/config/defaults.py:90` |
| watchdog inner poll | 0.05 s | `worker_stale_watchdog.py:156` (`_POLL_INTERVAL_S`) |
| hello timeout | 30 s | `worker_runtime_process.py:87` (`HELLO_TIMEOUT_SECONDS`) |

Read together: the transport *can* report death immediately (EOF, above), but the **supervisor's
lifecycle machinery still ticks at 30 s**, and the only sub-second poll inside it is the
watchdog's own loop, which is bounded by the 30 s cadence of `WORKER_STALE_WATCHDOG_INTERVAL_S`.
The "<100 ms" figure in circulation is **NOT MEASURED**: no constant in the tree states a death
latency budget (grep for `100 ms`/`<100ms` across `docs/*.md` returns nothing), and no test in
this pass timed an EOF-to-`lost` transition. Treat `<100 ms` as a **target to be established**,
not as current behaviour. Closing this is the C66 gap.

The watchdog's design does deserve credit for one thing D4 does not give it: it *asks* before it
acts — `Ping`/`Pong` answered "from its FRAME PUMP rather than its work loop", so "quiet and
busy" and "quiet and wedged" are distinguishable (`worker_stale_watchdog.py:60-68`).

### B5. Pause, stop, timeout: STOP ≠ KILL, and the reply that arrives too late

**MUST:** `stop` means detach (park, keep the process, keep the dir); `resume` refuses when
`_stop_event` is set or the status is terminal; `kill` is the terminal verb and only it may go on
to `SIGTERM`→`SIGKILL`; `cancel` is bounded by its own `deadline_ms`; a `Respond` payload that
arrives after a pause/stop is **held by the supervisor and delivered on resume**, never dropped
and never written into a turn that is no longer running.
**NEVER:** `stop` may terminate the process; a timeout may be reported as a crash (or a crash as
a timeout); a late reply may satisfy the next query.

**Today:** routing is right, the late-reply half does not exist.
- Verbs are frames: `stop`/`pause`/`resume`/`kill` each go through `_ensure_process_runtime()`
  (`worker_runtime.py:1066`, `:1080`, `:1099`, `:1118`), so **STOP ≠ KILL holds at the seam**:
  `stop` becomes a `Control` frame, and `kill` a separate verb; neither is `SIGTERM`.
- `cancel` is its own frame with its own ack (`worker_runtime.py:1164`), `CANCEL_DEADLINE_MS`
  riding in the payload (`worker_runtime_process.py` constant, ~`:106`), and one acceptability
  rule shared with the child (`CANCEL_TERMINAL_STATUSES`, `worker_runtime.py:98`).
- Timeouts are distinguished from death by *which exception* a caller gets
  (`WorkerQueryTimeout` vs `WorkerRuntimeLost`, both documented at
  `worker_runtime_process.py:14-21`), which is the right shape: death is a lost runtime, not a
  slow query.
- **The held late reply is absent.** There is no supervisor-side buffer for a `Respond` that
  arrives while a worker is parked; grep finds no `held_reply` / `deferred_reply` / `late_reply`
  mechanism in `tools/workspace/`. A reply frame that arrives for an unknown or already-satisfied
  `query_id` is the case P3 §4.8 said to "log and drop". So the requirement above is **open**:
  today's substrate drops where the rule says hold.

## C. Lifecycle, delta, and enforcement

### C1. Ordered lifecycle as it ships (supervisor = S, runtime = R)

```mermaid
sequenceDiagram
    participant S as supervisor (uvicorn PID)
    participant R as runtime child (spawn)
    participant D as worker_dir
    Note over S: spawn: attach_thread_frame_sink(thread) builds a handle<br/>"never started" (worker_thread.py:6918)
    S->>S: thread.start() — UNCONDITIONAL (worker_thread.py:7087)
    Note over S: so the in-process body runs too: it writes status.json<br/>and polls command.json (B1)
    S->>S: first verb -> _ensure_process_runtime (worker_runtime.py:819)
    S->>R: spawn + socketpair (worker_runtime_process.py:416-417)
    Note over R: WorkerThread(**kwargs) -> _allocate_generation()<br/>(worker_thread.py:1544) — epoch becomes E+1 (B2)
    R->>S: hello{pid, epoch_seen=E, capabilities}
    S->>S: pid match + epoch_seen == spec.epoch (process.py:467-473)
    S->>D: command.json (fence epoch E)
    R->>D: _poll_command claims it — if R's _fence() is E+1, quarantine
    S->>R: dispatch_query{query_id}
    R->>S: query_accepted{query_id}
    R->>S: heartbeat / worker_state / event
    S->>R: control{stop} => detach (park, keep process; STOP != KILL)
    S->>R: control{kill} => terminal (no SIGTERM rung shipped)
    S->>R: cancel{request_id, deadline_ms} -> cancel_ack{phase}
    alt child dies mid-stream
        R--xS: EOF on socketpair
        S->>S: reader thread -> pending futures fail with runtime_lost (process.py:1041)
        Note over S: transport knows at once; the lifecycle sweep still ticks at 30 s (B4)
    else supervisor dies
        S--xR: EOF
        R->>R: _kill_on_link_loss(worker) then break
    end
```

### C2. DELTA against today's code

| # | change | status |
|---|--------|--------|
| 1 | `AF_UNIX` socketpair + length-prefixed JSON frames + `v:1` + fence triple in the envelope | **ALREADY TRUE** (`worker_runtime_process.py:417`, `:441-443`; `worker_runtime_protocol.py:105`, `:119`) |
| 2 | reply correlation by `query_id` on the boundary; no object crosses | **ALREADY TRUE** (D9, `worker_runtime_main.py:345` is child-local) |
| 3 | STOP ≠ KILL; cancel as a frame with an ack ladder and shared acceptability rule | **ALREADY TRUE** (`worker_runtime.py:1066`, `:1118`, `:1164`, `:98`) |
| 4 | event/heartbeat/worker_state frames onto the existing bus | **ALREADY TRUE** (`worker_runtime_main.py:257-265`, `:311`) |
| 5 | default mode is `process`; flag read per spawn, fails closed | **ALREADY TRUE** (`worker_runtime.py:83`, `:310-331`) |
| 6 | retire the in-process body when the mode is `process` — do not start the supervisor's `WorkerThread` | **MUST CHANGE** (the whole of B1; `worker_thread.py:7087`) |
| 7 | one epoch per incarnation: child accepts `epoch` from the spec, never calls `_allocate_generation()`; verify against `worker_dir/generation` and fail closed | **MUST CHANGE** (B2; `worker_thread.py:1544`, `worker_runtime_process.py:467-473`) |
| 8 | one `command.json` reader: the body that owns the dir, and only it | **MUST CHANGE** (B1; consequence of 6/8 together) |
| 9 | death from EOF + `waitpid`, not from a 30 s cadence | **MUST CHANGE** (B4; `agent/config/defaults.py:90`) |
| 10 | supervisor holds a late `Respond` payload and delivers it on resume | **MUST CHANGE** (B5; absent — no `held_reply` anywhere in `tools/workspace/`) |
| 11 | `SIGTERM`→grace→`SIGKILL` rung for `kill` | **MUST CHANGE** (P3 §5.4 promised it; only `close()`'s grace+SIGKILL exists for *shutdown*, `worker_runtime_process.py:474-500`) |
| 12 | observed death latency `<100 ms` | **UNTESTED / NOT MEASURED** — no constant, no timing test (B4) |

### C3. Enforcement: `tests/test_worker_process_contract.py`

That file **does not exist** (`tests/test_worker_process_contract.py`: no such file). The
contract it should pin, one assertion per rule, each written so it fails on today's code exactly
where the delta says MUST CHANGE:

1. `assert thread.is_alive() is False` for the supervisor's `WorkerThread` when the resolved mode
   is `process` — i.e. the handle registered at spawn wraps a body that was never started
   (B1; fails today, `worker_thread.py:7087`).
2. `assert sum(1 for b in bodies if b.polls_command()) == 1` — one `command.json` claimer per
   worker dir; exercised by writing one fenced command and asserting exactly one consumer
   transitions it to `.claimed-*` (B1/B4; fails today).
3. Epoch law: for a spawned child,
   `int((worker_dir / "generation").read_text()) == handle.epoch == child_epoch` — all three
   equal (B2; fails today by one).
4. `fence_rejection_reason` returns `None` for a command written by the supervisor with the
   child's own epoch, and non-None for `epoch - 1` (B2; the quarantine path must be *seen*).
5. Frame laws: a `Connection`/`queue.Queue` passed where a socket is expected must raise; a
   truncated frame must yield no message; a frame with an extra payload key must be accepted
   while an unknown `kind` must raise (B3; partially covered today by
   `tests/test_worker_runtime_protocol.py`).
6. `hello` with `epoch_seen != spec["epoch"]` → `WorkerRuntimeLost` (already true,
   `worker_runtime_process.py:467-473`); add `hello` with `pid` of another process → same.
7. Death latency: `SIGKILL` the child, then assert the pending future is failed with
   `runtime_lost` within a stated bound (a number must be chosen — `<100 ms` is the target, and
   the test is what makes it a claim) (B4; no such test today).
8. `stop` then assert `process.is_alive()` and status `detached`/`paused`, never `killed`;
   `kill` then assert reaping. STOP ≠ KILL as an observable, not a docstring (B5; verbs are
   covered in `tests/test_worker_runtime_control.py` — 15 tests — but the *process survives*
   assertion is the new part).
9. Late reply: dispatch, park the worker, deliver a `reply` frame for the parked query, resume,
   assert the payload reaches the caller exactly once and is not written into the next turn
   (B5; fails today — nothing holds it).

**Coverage of the five questions today**, from the tests that exist
(`test_worker_process_e2e.py`, `test_worker_is_a_real_process.py`, `test_worker_runtime_control.py`,
`test_worker_pid_isolation.py`, `test_worker_runtime_protocol.py`, `test_worker_runtime_ping.py`,
`test_worker_multi_process_adoption.py`, …):

| question | has a test? |
|----------|-------------|
| B1 two bodies / one writer | **no** — the e2e tests assert the *mode resolves to process* and that a child answers (`test_the_production_spawn_path_resolves_process_mode`, `test_process_mode_envelope_comes_back_from_a_spawned_child`); none asserts the supervisor body is not running, and none counts `command.json` consumers. |
| B2 one epoch per incarnation | **no** — no test compares `spec["epoch"]`, the child's `_generation`, and `worker_dir/generation`. |
| B3 frames, not queues | **yes, partially** — protocol round-trip and envelope tests exist; the "never a `Connection`/`Queue` across the boundary" half is enforced by construction, not by a test. |
| B4 death is an EOF, `<100 ms` | **no** — no timing test, no EOF→`lost` latency assertion. |
| B5 STOP ≠ KILL, held late reply | **partially** — `test_worker_runtime_control.py` covers the verb acks and cancel acceptability; the "process survives `stop`" and "late reply is held" halves have **no** test (the second cannot: the feature does not exist). |

### C4. What is not measured

- Death latency, frame sizes, outbox limits, `grace_ms`, `CANCEL_DEADLINE_MS`: the constants are
  read above, but **no value is measured** against a target. `HELLO_TIMEOUT_SECONDS = 30.0`
  (`worker_runtime_process.py:87`) is a start bound, not a death bound, and must not be cited as
  one.
- Whether a two-body write to `status.json` is observable in production (`UNVERIFIED` — the
  code path is shared and unconditional, the interference is not observed by a test).
- Whether `WorkerJobRegistry` closes its entries on `runtime_lost` (`UNVERIFIED`, not read).
- Whether the e2e tests actually spawn a real child in CI on this host (`UNVERIFIED` — the tests
  are written to, but this pass did not run them).
