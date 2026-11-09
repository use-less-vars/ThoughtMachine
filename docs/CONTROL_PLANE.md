# Control Plane: Pause, Stop, and Timeout

**Status:** design / contract document, partly landed. Nothing described in
"Code changes needed to enforce this contract" was implemented when this
document was written, and every such item is marked **NOT YET IMPLEMENTED** —
but two things have moved since. The instrumentation pass — §6.5 "step 0", §8
item 3 — **HAS LANDED**. The run-state machine (`ExecutionState.PAUSED` /
`ExecutionState.STOPPED`, with `controller.pause_event` as the canonical
interrupt) is being implemented under the Gen 61 control-plane work. The
behaviour changes — Stop discarding the turn (§8 item 4) and the fail-closed
allow-list (§8 item 1) — remain **NOT YET IMPLEMENTED**, and §8 still marks
each item individually.

**Scope:** the three operator-visible ways to interrupt work — **Pause**,
**Stop**, **Timeout** — across the main agent and detached workers, plus one
fail-open bug in the enforcement path that currently breaks the Stop and
Timeout halves of the contract.

**Every `file:line` citation in this document was read in the tree it
describes.** Where a fact could not be established by reading code, it is
flagged as *unverified* rather than asserted.

---

## Summary

| Control | Intent | Turn survives? | Final words? | Where the work goes |
|---|---|---|---|---|
| **Pause** | orderly wind-down | **yes** — finishes, committed | **yes**, exactly one `Respond` | parks (`paused`), resumable |
| **Stop** | abort now | **no** — discarded | **no** summary, no extra LLM turn | parks (`detached`), resumable |
| **Timeout** | caller gives up waiting | **yes**, in the worker | the worker keeps working and answers later | worker keeps running; caller got a `timeout` envelope |

The three are deliberately different mechanisms, not three settings of one
knob: once `controller.pause_event` is bound, Pause **is** *preemptive* — it
interrupts sleeps and in-flight tool waits as well as refusing the next batch,
and the tool allow-list is still the single enforcement point for *what* may
run; Stop is *preemptive-plus-discard* (a per-turn cancellation token that
tools poll, plus the turn itself thrown away); and Timeout is *degenerate* (the
caller stops waiting; nothing is cancelled).

The bug: the tool allow-list — the only thing that actually makes Pause and
Stop hard, because the tool gate is the single enforcement point — is
computed behind a `getattr(..., 'restrictions_active', False)` guard that
silently returns "no restriction" when the attribute is missing or falsy.
The gate then treats an empty allow-list as "everything allowed". Both
halves fail **open** (see §6).

---

## 1. Actors and vocabulary

| Actor | Thread | Role in the control plane |
|---|---|---|
| **Operator** | UI / REST | presses Pause, Stop; writes worker command files |
| **Bridge** | bridge thread (`web_ui/backend/bridge.py`) | receives the operator action, sets the main agent's flags/token, calls into the worker controller |
| **Agent (turn)** | agent thread (`agent/core/agent.py::process_query`) | runs the LLM loop; owns `_turn_cancel_event` for *its* turn |
| **Tool** | same thread as the turn | polls the turn token / reads the allow-list; the only code that can actually refuse work |
| **Worker** | its own thread (`tools/workspace/worker_thread.py`) | a detached sub-agent with its own agent, its own turn token, its own park flags |
| **Caller** | main agent | issues `strict_wait_for_reply` to a worker and is subject to timeouts |

Vocabulary used consistently below:

- **reading mode** — `state.restrict(reason)` has been applied; the agent may
  only call what `state.get_allowed_tools()` returns.
- **nudge / notice** — a synthetic `system` conversation entry
  (`make_system_notice`) telling the agent what just happened.
- **checkpoint** — a point in `process_query` where the control flags are
  re-read before work is dispatched.
- **park** — the worker stops pulling queries but keeps its thread, containers
  and registry entry. `_park_event` ("wake up and re-read the park flags",
  `tools/workspace/worker_thread.py:87-97`).
- **detach** — the parking flavour used by Stop
  (`worker_thread.py:1771`), as opposed to `kill()`
  (`worker_thread.py:1823`, "the ONLY termination path — `stop` detaches",
  1826).
- **turn token** — `threading.Event` bound per thread via
  `tools/cancellation.py` (`_tls` at `tools/cancellation.py:17`,
  `bind_turn_cancel` at 20, `unbind` at 26, `is_interrupted` at 72,
  `cancel_reason` at 77).

---

## 2. Contract at a glance

```
operator action ──► flag/token ──► checkpoint ──► tool gate ──► outcome
```

| | **PAUSE** | **STOP** | **TIMEOUT** |
|---|---|---|---|
| Operator-visible name | "Pause" | "Stop" | (none — a wall-clock budget expires) |
| Signal | `state.restrict('pause')` + `_pause_requested` + `controller.pause_event` (set) | `_turn_cancel_event.set()` (main) / `_turn_cancel.set()` (worker) | `thread.send_query(..., timeout=…)` raises |
| Reaches a blocked tool? | yes, once `pause_event` is bound: sliced waits observe the event, the in-flight tool returns an `interrupted` envelope and the turn winds down (before Gen 61: indirectly — the batch already dispatched was *allowed to finish*, only the next batch was refused) | yes: tools poll `is_interrupted()` between wait slices | no: nothing is cancelled |
| Current turn's LLM output | kept, committed | **should be** dropped (§4 gap) | kept by the worker |
| Extra LLM call | **no** — the nudge rides the existing turn | **no** | **no** |
| Final message to operator | one `Respond` (the agent's own summary) | a machine envelope, not prose | the timeout envelope |
| Worker end state | `paused` | `detached` | `running` (unchanged) |
| Resumable | yes (`resume()`) | yes (`resume()`, `worker_thread.py:1959`) | n/a |
| Context preserved | yes | yes (`_save_context()` on detach) | yes |

---

## 3. PAUSE — orderly wind-down

### 3.1 What the operator does

The bridge calls `request_pause()`, a thin alias for `enter_reading_mode('pause')`
(`agent/core/agent.py:1355`, delegating at 1362-1363). On a worker, `pause()`
does the same *and* sets the park flag
(`tools/workspace/worker_thread.py:1931`).

### 3.2 Mechanism, step by step

1. **Restriction applied** — `enter_reading_mode` sets
   `changed = self.state.restrict(reason)` (`agent.py:1288`) and
   `self._pause_requested = True` (`agent.py:1289`). It runs on the *bridge*
   thread, not the turn thread; it does not touch the turn.
   `state.restrict` (`agent/core/state.py:392`) sets
   `restrictions_active = True` (416), `restrictions_pending = False` (417),
   `restriction_reason = reason` (418).
2. **Checkpoint fires** — the turn re-reads the flag at
   `agent.py:1909` (after the LLM response, before tool dispatch) and
   `agent.py:2060` (after tools).
3. **Notice injected once** — `_apply_pause_reading_mode`
   (`agent.py:1313`) guards on
   `if getattr(self.state, 'restriction_reason', None) != 'pause': return`
   (`agent.py:1336-1337`), fires at most once per pause
   (`if not self._pause_nudge_sent and tool_calls`, `agent.py:1338`), builds
   the notice at `agent.py:1339-1345` with current text:

   > "You were paused by the user. Do not start new work and do not call
   > tools — tool calls are refused. Summarize what you have done and what
   > remains, then reply with Respond. The session stays paused until it is
   > resumed."

   It is appended to `self._pending_warnings` (`agent.py:1346`, field
   declared `agent.py:486`) and `_pause_nudge_sent = True` (`agent.py:1347`).
4. **Notice delivered** — the *same* buffer flushes in the tool branch at
   `agent.py:1986-1993` (append loop 1987-1988, clear 1992), inline in the
   checkpoint-3 path at `agent.py:2060-2063`, and in the no-tool branch at
   2069-2076.
5. **Tool gate refuses the rest** — `get_allowed_tools()`
   (`state.py:449`) returns `['Respond']` for `pause` (`state.py:474`).
   The gate is `agent/core/tool_executor.py:272-280`: a refused tool becomes
   `self._create_tool_rejection_message(tool_name)` (`tool_executor.py:882`,
   reason at 890, message text at 894/897) and the loop `continue`s.
6. **The turn is concluded, not truncated** — nothing amputates the turn and
   no terminal message is injected in the model's name. Tools the executor has
   already reached are stopped at the batch boundary: once an entry reports an
   interrupt and `state.restriction_reason == 'pause'`, every remaining call in
   the batch is answered with the interrupted envelope, one result per
   `tool_call` id (`tool_executor.py:412-437`), so the assistant `tool_calls`
   message keeps its pairing and the provider accepts it. The dispatch-loop gate
   on `is_interrupted_tool_entry(...)` (`agent.py:2123-2137`) then sets
   `_pause_winddown` and `continue`s to another turn; the only new instruction
   the model receives is the existing pause notice
   (`agent.py:1361-1396`, text `:1387-1393`), and the turn ends when the model
   calls Respond (`agent.py:2081-2109`). That — not a hard truncation — is what
   makes Pause a wind-down rather than an advisory notice.
7. **The worker parks** — in the run loop, `if self._park_event.is_set():`
   (`worker_thread.py:2974`) reports the pause exactly once
   (2975-2986: `request_pause()`, `status = "paused"`, status file, a
   `worker_paused` event), then keeps draining events until the agent's own
   conclusion arrives. When a resume lands inside the same turn, the flag is
   cleared so a later pause reports again (`worker_thread.py:3012-3015`).
8. **Resume** — `resume()` (`worker_thread.py:1959`) writes a fenced
   `resume` command (2003), clears `_park_event` (2004), sets `_resume_event`
   (2005); the agent side is `leave_reading_mode` (`agent.py:1293`) →
   `state.clear_restriction(reason)` (`agent.py:1306`), which also clears
   `_pause_requested`/`_pause_nudge_sent` (1308-1309).

### 3.3 Known deviations from the stated wording

- **Notice wording** differs from the contract text ("finish your current
  tool call and write a report"): the shipped text says "Do not start new
  work … Summarize … reply with Respond". Functionally equivalent; if the
  wording is a contract, the string at `agent.py:1339-1342` is the place to
  change.
- **Ordering**: the nudge is *flushed after* the in-flight tool batch has
  already executed (`agent.py:1986-1993` runs after tool results/commit), so
  "tool calls are refused" is true only for the *next* batch. That is the
  intended semantic ("finish the current call"), but it means the pause is
  not instantaneous by construction — a long single tool call can run for
  minutes after Pause. (Pre-Gen 61 that was true by construction; it no longer
  is once `pause_event` is bound, because the wait is interruptible. A process
  already running *inside* a container is still not killed, though — see §9
  question 2 for that residual.)

### 3.4 PAUSE state table

| State | `restrictions_active` | `restriction_reason` | `_pause_requested` | `_pause_nudge_sent` | `_park_event` | worker `status` |
|---|---|---|---|---|---|---|
| running | False | None | False | False | clear | `running` |
| pause requested, turn mid-flight | True | `pause` | True | False | set | `running` |
| notice delivered | True | `pause` | False | True | set | `paused` |
| resumed | False | None | False | False | clear | `running` |

---

## 4. STOP — abort now

### 4.1 What the operator does

`bridge.request_stop()` (`web_ui/backend/bridge.py:1910`) calls
`_interrupt_agent_turn()` (`bridge.py:1884`, called at 1941) and then the
worker controller's `request_halt()` (1954-1960).

`_interrupt_agent_turn` is deliberately *not* routed through
`tools.cancellation.interrupt`: that helper is per-thread, and Stop runs on
the bridge thread while the turn runs on the agent thread (docstring
`bridge.py:1885-1896`). It therefore reaches into the agent's own token:
`event = agent._turn_cancel_event` (`bridge.py:1898`),
`event.reason = 'stopped_by_operator'` (1902), `event.set()` (1903).

### 4.2 Mechanism

- **The token is created per turn**: `_turn_cancel = threading.Event()`
  (`agent/core/agent.py:1383`), `bind_turn_cancel(_turn_cancel)`
  (`agent.py:1386`), `self._turn_cancel_event = _turn_cancel`
  (`agent.py:1390`), cleared to `None` at `agent.py:2135`.
- **Halt is a separate signal from cancel**: `self.halt_check`
  (`agent.py:512-515`, comment: "Hard stop (halt) is deliberately SEPARATE
  from stop_check"), `should_halt` (`agent.py:556`), a second checkpoint at
  `agent.py:578`, and a poll inside the stream loop at
  `agent.py:1653-1656` (`if self.should_halt(): halted = True; break`).
- **A blocked tool yields**: container exec runs on a daemon thread; the wait
  is sliced (`_WAIT_SLICE_SECONDS = 0.5`, `infra/container_manager.py:2582`),
  and each slice checks `if _is_interrupted():` (`container_manager.py:2610`)
  returning
  `{"status": "interrupted", "interrupted": True, "reason": …, "exit_code": None, "stdout": "", "stderr": "", "note": "Stopped WAITING because the turn was cancelled. The process inside the container was NOT killed and may still be running: …"}`
  (2611-2628). The comment at 2574-2581 is explicit that the *wait* is what
  is interrupted — `exec_run` hands back no killable handle.
- **Tools translate it** — `tools/container_control.py:377-393` and
  `tools/docker_code_runner.py:465` both branch on
  `result.get("status") == "interrupted"` and surface an `interrupted`
  payload plus a user-facing note (`docker_code_runner.py:484`).
- **Workers coalesce** — `worker_thread.py:6866-6869` binds
  `cancel_reason, is_interrupted` and returns
  `cancel_reason() or "turn_cancelled"` from its stop-check helper.
  Worker-side sets: `_turn_cancel.set()` at `worker_thread.py:1794`, 1879,
  2515; clear at 2756.

### 4.3 THE STOP GAP (contract violation, present today)

Read `agent/core/agent.py:1715-1758`. On halt the code:

1. builds `halt_tx` (1721),
2. builds `halt_msg = {'role': 'assistant', 'content': content}` (1722),
3. **commits it** — `halt_tx.commit_assistant_only()` (`agent.py:1727`),
4. **injects a notice** —
   `make_system_notice('You were stopped by the user. Review your current progress and resume.', kind='stop_notice')`
   (`agent.py:1738-1741`, guarded by `_stop_notice_sent` at 1737),
5. yields `{'type': 'interrupted', 'stop_reason': 'interrupted', 'content': content, …}`
   (1745-1757) and returns (1758).

Two things to separate:

- **What matches the contract:** no extra LLM call and no summary is
  generated. Stop does not buy a wind-down turn.
- **What does not match:** the partial assistant content *is persisted* and a
  `stop_notice` is *injected into the conversation*. The operator therefore
  sees final words that the contract says should not exist, and the next turn
  reads a "review your progress" instruction that the operator never
  authorized. If Stop means "no final words", `agent.py:1721-1741` is the
  code that has to change.

### 4.4 Worker-side stop = detach

`detach()` (`worker_thread.py:1771`, docstring 1764-1768: "`stop` is an alias
for detach. Termination is `kill`") is *deliberately not cooperative*:

| Line | Effect |
|---|---|
| 1784 | `status = "detaching"` (observable before the park) |
| 1786-1787 | `_detach_requested = True`, `_detached_at = time.time()` |
| 1788-1789 | `_park_event.set()`, `_resume_event.clear()` |
| 1794 | `self._turn_cancel.set()` — "Graceful stop CANCELS the in-flight turn" (1790-1793) |
| 1798 | `_save_context()` — context persisted for revival |
| 1809 | `_set_detached_registration(True)` — stale-container sweep now skips it |
| 1810 | `status = "detached"` |
| 1819 | fenced `write_command(..., "stop", ...)` for cross-process signalling |
| 1821 | `_input_queue.put(_PARK_SENTINEL)` to unblock the queue wait |

And the run loop honours it immediately: `if self._detach_requested:`
(`worker_thread.py:2987`) builds the terminal envelope
(`{"status": "paused", "message": "Worker paused by user"}`, 2988-2991),
writes the inflight journal as `STATUS_RECOVERABLE` with
`reason=_JOURNAL_DETACH_REASON` (3001-3009,
`_JOURNAL_DETACH_REASON = "detached-midturn-park"` at `worker_thread.py:97`),
terminalizes the job (`_journal_interrupt_job(final_content)`, 3010) and
`break`s (3011). The comment at 2992-3000 is the design statement: "DETACH
abandons the turn … The work under way is DISCARDED (unlike a pause, which
concludes the turn)".

**This is exactly the semantic Stop should have on the main agent.** The gap
in §4.3 is the main agent not following the worker's rule.

### 4.5 STOP state table

| | main agent | worker |
|---|---|---|
| signal | `_turn_cancel_event.set()` (`bridge.py:1903`) + `request_halt()` | `_turn_cancel.set()` + `_park_event.set()` (`worker_thread.py:1794/1788`) |
| blocked tool | wait sliced, returns `interrupted` | wait sliced, returns `interrupted` |
| in-flight LLM stream | broken out at `agent.py:1653-1656` | same mechanism, own token |
| turn record | **committed partial + `stop_notice`** (`agent.py:1721-1741`) ← gap | journaled `recoverable`, job `interrupted` (3001-3010) |
| end state | turn ends | `detached` |
| resumable | next operator turn | `resume()` (`worker_thread.py:1959`) |

---

## 5. TIMEOUT — the caller stops waiting, the worker does not stop

Timeout is **not a control signal at all**. Nothing is cancelled. It is the
caller's wall-clock budget expiring on a *query*.

### 5.1 Mechanism

`strict_wait_for_reply` (`tools/workspace/worker_query.py:167`) is the only
sanctioned path (`worker_query.py:419` is the call site). Docstring
175-189 states the guarantee: "The call never surfaces a raw `TimeoutError`:
a timeout is converted into a strict timeout envelope".

```
effective = _worker_query_wait_timeout(timeout_seconds, fallback=fallback)   # 190
response  = thread.send_query(query, timeout=effective)                      # 204
except TimeoutError:                                                         # 205
    return build_timeout_envelope(...)                                       # 207-215
```

- The wait budget is the caller's timeout *plus* `QUERY_WAIT_GRACE_SECONDS`,
  so the worker's own cooperative envelope has a chance to arrive first
  (docstring 177-181); an explicit `grace=` recomputes it as
  `timeout + grace` (191-198).
- The envelope carries a note — `f"Worker {name!r} did not respond within {effective:.0f}s ({exc})."`
  (209-212) — plus `instance_label` and `elapsed_seconds`.
- `build_timeout_envelope` (`worker_query.py:79`, default status `"timeout"`
  at 85); helpers `is_timeout_envelope` (114), `envelope_status` (130),
  `validate_query_reply` (137).
- Budget plumbing lives in `tools/workspace/worker_timeout.py`:
  `_worker_timeout_detected` (28), `_worker_query_wait_timeout` (61,
  fallback `300.0`), `clamped_wait_for_job_timeout` (83, `cap=300.0`).
- **The worker is not told.** The envelope goes to the *caller*. The worker's
  run loop merely observes its own overrun through
  `_worker_timeout_detected` (`worker_thread.py:3221`, imported at
  `worker_thread.py:204`) and, per the comment at `worker_thread.py:3017-3019`,
  the worker loop keeps running while the supervisor enforces the deadline.
  There is no "your caller timed out" notice to the worker. **Contract
  consequence:** treat a timeout envelope as *"result still coming"*, never as
  *"result lost"*.
- **No queueing.** A second query to a busy worker is refused, not stacked:
  `WorkerBusyError` (`worker_query.py:226`, docstring 227-234: "W2 has no
  query queueing … a loud, explicit rejection (never a silent drop, never an
  unbounded wait)"). So after a timeout, the caller cannot retry blindly — the
  worker may still be busy, and the retry may raise.

### 5.2 TIMEOUT state table

| | caller (main agent) | worker |
|---|---|---|
| before | blocked in `send_query` | `running`, processing the query |
| at expiry | `TimeoutError` → `timeout` envelope (207-215) | unchanged, still `running` |
| after | free to do other work; the answer is queued in the worker's own reply path | finishes the turn, writes its reply / journal |
| data loss | none — `elapsed_seconds` + note recorded | none |
| cancellation | **none** | **none** |

---

## 6. The fail-open bug

### 6.1 The code

`agent/core/state.py:449` `get_allowed_tools()`:

```python
# state.py:467-468
if not getattr(self, 'restrictions_active', False):
    return []
```

and `state.py:479-480`:

```python
if not allowed:
    return True
```

### 6.2 Why that is fail-open

The enforcement point for the *entire* control plane is the tool gate,
`agent/core/tool_executor.py:272-280`:

```python
if not self.state.is_tool_allowed(tool_name):
    tool_result = self._create_tool_rejection_message(tool_name)
    ...
    continue
```

Everything else — the nudge (`agent.py:1339-1345`), the checkpoints
(`agent.py:1909`, `agent.py:2060`), the stop notice — is advice to the model.
The gate is the only thing that *makes* Pause and Stop hard. That gate is
driven by `get_allowed_tools()`, which has two independent fail-open edges:

1. **Missing/falsy guard falls through to "no restriction".** `getattr(…,
   False)` means an object without `restrictions_active` (an older pickled
   state, a stub, a state class refactor, a mock in a test double) is treated
   as *unrestricted*, so `get_allowed_tools()` returns `[]`.
2. **Empty allow-list means "allow everything".** `is_tool_allowed`
   (`state.py:479-480`) returns `True` when the list is empty. So the value
   produced by edge 1 — and the value produced whenever a caller forgets to
   check `restrictions_active` first — is the *most permissive* answer.

The two edges compose: any path where the restriction flag is not visible
turns a paused or stopped agent into a fully-armed agent, silently. The
rejection message would even be wrong in the other direction: with an empty
list the message at `tool_executor.py:897` reads
`"Tool not allowed. Available tools: none"` — but that string is only reached
from the `not allowed` branch, which the empty list never enters.

**Fail-closed constraint (the rule this bug violates):** *absence of a
restriction decision must never be expressed as an empty allow-list, and an
empty allow-list must never mean "unrestricted".* "No restriction" is
`is_tool_allowed → True` for every tool; "restricted to nothing" is
`is_tool_allowed → False` for every tool. Today the two are the same value.

### 6.3 Fix options

**Option A — sentinel return (`None` = unrestricted).**
`get_allowed_tools()` returns `None` (not `[]`) when no restriction is
active; `is_tool_allowed` treats `None` as "allowed" and an empty list as
"denied". Guard becomes
`if not getattr(self, 'restrictions_active', True): return ['Respond']`
(fail *closed* on a missing attribute). Smallest diff, kills both edges.
Risk: every existing caller of `get_allowed_tools()` must tolerate `None`
(today `tool_executor.py:889` uses it for the message only).

**Option B — one predicate, no list.**
Replace the boolean logic with a single `is_tool_allowed(tool_name)` that
reads `restrictions_active` and `restriction_reason` directly and computes
the decision inline, and keep `get_allowed_tools()` as a *display-only*
helper. Fail-closed by construction because there is no intermediate
permissive default to leak. Larger diff, but removes the class of bug rather
than the instance.

**Option C — fail-closed default + explicit unrestricted reason.**
Keep `[]`-means-unrestricted but make the guard
`getattr(self, 'restrictions_active', True)` and add an explicit
`'unrestricted'` pseudo-reason instead of relying on a falsy flag. Cheap, but
keeps two representations of "no restriction" and therefore keeps the trap;
not recommended.

**Recommendation: Option B, with Option A as the immediate patch.**
A is a one-line change to the guard plus a `None` return and closes the
observed hole today; B removes the reason the hole was possible. Given that
the failure mode is "a stopped agent keeps calling tools", the patch should
land first and the refactor second — and the constraint in §6.2 should be
written into the docstring of `state.py:449` either way.

### 6.4 Test the constraint, not the code path

Whichever option is chosen, the invariant worth pinning is behavioural:

- state with no restriction → `is_tool_allowed('anything')` is `True`;
- state with `restrict('pause')` → only `Respond` is allowed
  (`state.py:474`);
- state with `restrict('token')` → `Respond` and `SummarizeTool`
  (`state.py:470-471`);
- state object with `restrictions_active` **missing** → *not* unrestricted.

The last line is the one that is false today.

### 6.5 Where the pause state must live (the propagation question)

§6.3 fixes the *representation* of a restriction. It says nothing about
*which object* holds it — and today a restriction only stops tools on the agent
object that happened to receive it. The pause decision is applied to one
`AgentState` instance; the executor asks that instance; nothing else in the
process can see the decision. This section is about the missing link.

**What is proven vs hypothesised**

Proven, from the code read for this document:

- The dispatch gate reads exactly one object: `tool_executor.py:272`
  `if not self.state.is_tool_allowed(tool_name)`, where `self.state` was bound
  once at construction (`tool_executor.py:181`; the docstring at 173 calls it
  "state: AgentState instance for tool allowance checking"). The decision is
  therefore a property of the *executor's reference*, not of the session.
- That reference is patched in after construction on the two main paths:
  `agent.py:497` passes `None`, then `agent.py:530` creates
  `AgentState(self.config, self.logger)` and `agent.py:531` assigns
  `self.tool_executor.state = self.state`. The config hot-swap path repeats the
  same pattern at `agent.py:801-806` → `agent.py:807`. Only
  `_restart_with_config` passes it inline (`agent.py:948`). So a live agent has
  a window in which `tool_executor.state` is not yet the agent's state.
- A worker builds **its own** `Agent` — imported at `worker_thread.py:3692`,
  constructed at `worker_thread.py:3704`, behind a lazy field set to `None` at
  `worker_thread.py:2187` — and therefore its own `AgentState` (`agent.py:530`).
  A pause applied to the session agent does not reach it.
- A worker has two distinct pause entry points:
  `worker_thread.py:1948` (inside `def pause()`, 1931) and a second
  `enter_reading_mode("pause")` call at `worker_thread.py:2544`.
- The controller records a session pause on the agent it owns and nowhere
  else: `execution_state = PAUSING` at `agent/controller/__init__.py:542`, then
  `_agent_enter_reading_mode(...)` (`:373`, which calls
  `agent.enter_reading_mode('pause')` at `:390`, legacy fallback
  `request_pause()` at `:393`) from `:448` (idle path) and `:550` (active
  path). The only other session-level marks are the controller's own
  `pause_event` / `_pause_requested` (`:564-565`). There is no process-wide
  pause record anywhere.
- Pause state can outlive the turn that produced it: `agent.py:393`
  `_discard_session_main_pause(...)` exists precisely to clear *stranded*
  main-pause markers on a new operator turn, and `_restart_with_config` clears
  the bookkeeping (`agent.py:929-932`) while reusing the same state object
  (`agent.py:948`).

Hypothesised, and therefore needing step 0 before any fix is chosen:

- That the observed "paused agent keeps calling tools" reproduces on a
  *second* `AgentState`/executor pair rather than on the one the controller
  paused. Plausible from the construction pattern above; unproven.
- That a worker turn can be in flight while the session-level pause has already
  been applied to the main agent. The structure permits it (two agents, two
  states); no log has been produced that it happens.
- Whether `restrictions_active` / `restriction_reason` survive
  `_restart_with_config`. The bookkeeping is cleared (929-932) but the state
  object is reused (948); the effect of `set_execution_state` on the
  restriction fields was not read.

**How each structure becomes possible**

Only mechanisms that follow from the code above — no speculation about paths
nobody has read:

1. *Different object.* A worker's `AgentState` (created at `agent.py:530` inside
   the agent built at `worker_thread.py:3704`) is a different instance from the
   session agent's. Restrict one and the other is untouched; each executor asks
   its own (`tool_executor.py:272`).
2. *Stale reference.* The executor caches `self.state`
   (`tool_executor.py:181`) and is re-pointed only at `agent.py:531`,
   `agent.py:807`, or by re-construction at `agent.py:948`. Any path that
   installs a new `AgentState` without one of those three assignments leaves
   the gate reading the old object.
3. *Unset reference.* The `None` at `agent.py:497` and `agent.py:802` means an
   executor can exist with no state at all before the patch line runs; anything
   dispatched in that window evaluates `self.state.is_tool_allowed` on `None`
   (`tool_executor.py:272`).
4. *Silently skipped pause.* A worker has two `enter_reading_mode("pause")`
   sites — `worker_thread.py:1948` in `pause()` and `worker_thread.py:2544`,
   deliberately duplicated per the comment at 2538-2540 — and both target
   `self._agent` and both are guarded on it being non-`None` (`1945-1946`,
   `2541-2542`; the comment at 1944 says "_agent is None before spawn and after
   teardown"). A pause that lands in either window restricts nothing, and
   nothing records that it was dropped.
5. *Leaked or deliberately dropped state.* `_discard_session_main_pause`
   (`agent.py:393`) documents that pause markers can be stranded past their
   turn; `_restart_with_config` (`agent.py:929-932`) documents the opposite —
   bookkeeping dropped while the state object is kept (`agent.py:948`). A state
   that is not itself the authority can therefore outlive, or precede, the
   decision it is meant to express.

**Step 0 — instrumentation (do this before any fix)**

None of the above is observable today. Before choosing a propagation shape, add
three log lines behind the existing debug logger, changing no behaviour:

1. At the mutation point — wherever `restrict('pause')` lands in `state.py` —
   log `f"restrict({reason!r}) on AgentState id={id(self)}"`, plus an
   owning session/worker label only if the state object happens to carry one
   (`AgentState` is constructed from `(config, logger)` at `agent.py:530`, so it
   may not).
2. At the decision point — `state.py:476` `is_tool_allowed` — log the same
   `id(self)` and the outcome, so each decision can be matched to the object
   that made it.
3. At dispatch — immediately before the gate at `tool_executor.py:272` — log
   `f"gate on AgentState id={id(self.state)} worker={current_worker_name()!r} session={session_id!r}"`.
   `session_id` is already a parameter of `execute_tool_calls`
   (`tool_executor.py:193`) and `current_worker_name` is already imported into
   the executor (`tool_executor.py:48-52`), so this needs no new plumbing.

The decisive comparison is `id(self.state)` at (3) against `id(self)` at (1). If
they match on every refused tool call, the propagation question is closed and
§6.3 alone is the fix. If they never match on the affected path, the pause
reached a *different* `AgentState` and no amount of allow-list correctness in
`state.py` will help. One secondary check belongs in the same pass: log
`self.tool_executor.state is self.state` at `agent.py:531` and `agent.py:807`,
to prove that the post-hoc patch is what actually runs.

**Where the state SHOULD live**

*(a) Push — one authoritative object, pushed to every consumer.*

- *Synchronous at dispatch because:* every executor holds the same object, so a
  mutation on it (`restrict`) is visible to all of them on the next gate call
  (`tool_executor.py:272`). No lookup, no key.
- *Failure modes:* consumers must be enumerated at push time. A worker that has
  not yet built its `Agent` (`worker_thread.py:2187` lazy field, constructed at
  `:3704`) is not a consumer yet and silently misses the push; a push that
  races an `AgentState` replacement (`agent.py:530`) or a restart
  (`agent.py:948`) targets an object nobody reads; and it does not cross a
  process boundary.
- *Blast radius:* `AgentState` plus every construction site
  (`agent.py:497/530-531`, `801-807`, `948`, `worker_thread.py:3704`).
- *Migration cost:* medium — a consumer registry, plus an ordering rule for
  "register before push".

*(b) Pull — process-wide registry keyed by session/worker id, read at dispatch.*

- *Synchronous at dispatch because:* the check happens at the moment of the
  decision rather than the moment of the push, so ordering problems disappear.
  The key material is already at the gate: `session_id` and `workspace_id` are
  parameters of `execute_tool_calls` (`tool_executor.py:193`) and
  `current_worker_name()` is in scope (`tool_executor.py:48-52`).
- *Failure modes:* it becomes possible to pause a *future* turn that inherited a
  key — exactly the class of leak `_discard_session_main_pause` (`agent.py:393`)
  exists to clean up, so it needs a lease/expiry to avoid recurring; it adds a
  lookup to the hot path of every tool call; and it still does not cover a
  worker in another process, which today is signalled by fenced command files
  (`worker_thread.py:1819`).
- *Blast radius:* the largest — new process-wide mutable state consulted by the
  dispatch gate.
- *Migration cost:* medium-high — registry, keying, lease/expiry, and the
  cross-process story.

*(c) Per-turn injection — pass the owning state/record into the turn.*

- *Synchronous at dispatch because:* the value is an argument of the call that
  dispatches, so it cannot be stale with respect to that turn.
  `execute_tool_calls` (`tool_executor.py:193`) already takes `session_id`,
  `workspace_id` and `turn_transaction`; an explicit `state=` parameter is the
  same shape.
- *Failure modes:* every call site must thread it (the main-agent dispatch path
  is one, `agent.py:1952`); a tool that starts a nested agent or executor
  inherits whatever it was handed and cannot see a pause applied mid-turn to a
  different object; and it says nothing about a worker in another process.
- *Blast radius:* the smallest — one signature plus its call sites, no new
  global state.
- *Migration cost:* low — the gate line itself (`tool_executor.py:272`) does not
  change.

**A recommendation** (separate from §6.3's, and not a substitute for it)

Order matters, and the first step is not this section:

1. **Land §6.3 Option A first.** It is proven, one line, and independent of
   everything above — it makes a *stopped* agent's allow-list correct whichever
   object ends up holding it. Do not gate it on instrumentation.
2. **Then step 0.** One instrumentation pass, read against a real reproduction,
   decides whether §6.5 is a bug at all.
3. **Then (c) per-turn injection** as the propagation fix, if step 0 shows a
   mismatch. It is the smallest change that makes the decision synchronous at
   dispatch without adding global state.
4. **Escalate to (b) only** if step 0 shows a split that injection cannot carry
   — a worker turn dispatched on a path with no per-turn argument to carry the
   record, or genuine cross-process propagation. (a) is not recommended on its
   own: its failure mode, missing a consumer that does not exist yet, is the
   same class of bug as the one being fixed.

---

## 7. Relationship to `web_ui/backend/PAUSE_PROPAGATION_DESIGN.md`

That document (356 lines) is the prior art for pause propagation to workers,
and this document builds on it rather than replacing it. Two points of
alignment and one contradiction:

- **Aligned:** pause is a *soft stop* — finish the current turn, preserve
  context, show the worker as paused. That is exactly §3 and matches the code
  at `worker_thread.py:2974-2986`.
- **Aligned:** the notice-then-Respond shape, the `Respond`-only allow-list
  (`state.py:474`) and the resumability of a parked worker.
- **Contradiction, and it must be resolved:** PAUSE_PROPAGATION_DESIGN.md
  describes Stop as *"Hard stop — finish the current turn, preserve context,
  show as completed"*. That is **not** the contract in this document. Here
  Stop is an immediate abort that discards the turn and parks the worker —
  which is what the worker code already does (`detach()`,
  `worker_thread.py:2992-3011`) and what the main agent does **not** yet do
  (§4.3). Either the old sentence is stale and should be amended, or this
  contract is wrong and Stop should be deleted as a distinct control. The
  code is on this document's side for workers and on the old document's side
  for the main agent, which is precisely the inconsistency to fix.

**Citation hygiene:** PAUSE_PROPAGATION_DESIGN.md cites line numbers that no
longer hold (`request_pause()` "agent.py line 761" — it is `agent.py:1355`;
`WorkerThread.__init__` 283/285/307; `_poll_command()` "worker.py lines
420-442"; `shutdown_workers()` "worker.py line 134"; `_action_spawn` "around
1287"), and it names `tools/workspace/worker.py`, which still exists alongside
`tools/workspace/worker_thread.py`. Treat those citations as symbol names, not
addresses, and prefer this document's line numbers (read against the current
tree) when the two disagree.

---

## 8. Code changes needed to enforce this contract

Ordered, minimal, **NOT YET IMPLEMENTED**. No diffs here on purpose — the
list is a work plan, and each item is independently reviewable.

1. **NOT YET IMPLEMENTED — Close the fail-open hole (§6).**
   `agent/core/state.py:467-468`: change the guard so a missing
   `restrictions_active` is treated as *restricted*, and change the `[]`
   return to `None` (Option A); then `state.py:479-480` must treat `None` as
   allowed and `[]` as denied. Update the `get_allowed_tools` docstring
   (`state.py:450-466`) with the fail-closed constraint.
2. **NOT YET IMPLEMENTED — Collapse to one predicate (Option B).**
   Make `is_tool_allowed` compute the decision from `restrictions_active` +
   `restriction_reason`; keep `get_allowed_tools()` display-only.
3. **NOT YET IMPLEMENTED — Instrument the pause gate, then choose the propagation
   fix (§6.5).** Step 0 first: log `id(state)` at the `restrict()` mutation
   point, at `is_tool_allowed` (`state.py:476`) and immediately before the
   dispatch gate (`tool_executor.py:272`, with `current_worker_name()`, already
   imported at `tool_executor.py:48-52`); log `self.tool_executor.state is
   self.state` at `agent.py:531` and `agent.py:807`. Only if the ids disagree,
   make the pause record per-turn and explicit: add it as a parameter of
   `execute_tool_calls` (`tool_executor.py:193`) and pass it from the
   main-agent call site (`agent.py:1952`), rather than adding a process-wide
   registry. The registry is the fallback, not the default (§6.5).
4. **NOT YET IMPLEMENTED — Make Stop drop the turn on the main agent (§4.3).**
   `agent.py:1721-1741`: do not `commit_assistant_only()` the partial content
   and do not inject the `stop_notice`; yield the `interrupted` event
   (`agent.py:1745-1757`) without prose. Mirror what the worker does at
   `worker_thread.py:2987-3011`. This is the only item that changes
   operator-visible behaviour.
5. **NOT YET IMPLEMENTED — Decide the fate of `stop_notice`.**
   If it survives anywhere, it belongs in the UI as an event, not in the
   conversation as a `system` entry; today it mutates the model's next-turn
   instructions (`agent.py:1738-1741`).
6. **NOT YET IMPLEMENTED — Align the pause notice text (§3.3).**
   `agent.py:1339-1342` is the single string; either amend the contract
   wording or the string, and delete the discrepancy rather than leaving two
   authorities.
7. **NOT YET IMPLEMENTED — Make the timeout path explicit about "no cancel" (§5).**
   The envelope already says the worker did not answer (209-212); it should
   also say the worker is still running and that a retry may raise
   `WorkerBusyError` (`worker_query.py:226`), so the caller cannot mistake a
   timeout for a lost result.
8. **NOT YET IMPLEMENTED — Amend PAUSE_PROPAGATION_DESIGN.md's Stop sentence
   (§7)** once the contradiction is resolved, and refresh its stale line
   citations.
9. **NOT YET IMPLEMENTED — Pin the fail-closed invariant with a test** (§6.4),
   specifically the "missing `restrictions_active`" case.

Items 1-2 are behaviour-preserving except in the failure case, so they can
land alone. Item 3 is diagnostic and can land alone too (it changes no
behaviour) and should land *before* any propagation fix. Item 4 changes what
the operator sees when they press Stop and should be reviewed against the UI's
"completed vs interrupted" display.

---

## 9. Open questions for the Operator

1. **Stop and partial content.** Should the text the model had already
   produced be shown to the operator at all — as UI-only history — or
   discarded entirely (`agent.py:1727`)? The contract says no final words; it
   does not say whether the bytes may be *displayed* without being committed to
   the conversation.
2. **Stop during a long tool call.** The container wait is interruptible
   (`container_manager.py:2610`) but the process inside is not killed
   (note at 2611-2628). Is "Stop" allowed to leave a container process
   running, or must it escalate to killing the container?
3. **Timeout semantics.** Is a timed-out worker query meant to be *recoverable*
   (ask again later, race `WorkerBusyError`) or *terminal* (abandon the work)?
   §5 assumes recoverable; the code supports both readings.
4. **Queueing.** `WorkerBusyError` (`worker_query.py:226`) means a timeout
   cannot be retried while the worker is busy. Is a bounded queue wanted, or
   is refusal the intended contract?
5. **Pause granularity.** Is it acceptable that Pause lets the current tool
   batch finish (`agent.py:1986-1993`), i.e. that Pause is not instantaneous?
   If not, Pause needs a turn-token component, which would make it overlap
   with Stop.
6. **Which document wins.** §7 records a direct contradiction about Stop.
   Until one of the two documents is amended, "Stop = finish the turn" and
   "Stop = discard the turn" are both cited in-tree.
7. **Per-agent pause, or process-wide pause?** §6.5 prefers carrying the pause
   record per turn over a process-wide, session-keyed registry, which keeps
   "paused" a property of one agent. That is only correct if a pause is *meant*
   to be per-agent: a worker built lazily (`worker_thread.py:3704`) or restarted
   (`agent.py:948`) then inherits no pause, so a session-level Pause reaches it
   only if the signal is deliberately routed there. Is that the contract, or
   must a session pause be a process-wide fact that any agent, present or
   future, is born subject to? The second reading needs the registry — and then
   needs a rule for when a stale entry must not pause a fresh turn.

---

## 10. Implementation: the four pieces and the run-state machine

§1-§9 fix the behaviour; this section fixes which object owns which fact, so the
implementation cannot grow a second pause flag next to the first. Every
statement below is a ruling unless a `file:line` is cited, in which case the
citation is current behaviour and is exactly what may have to change.

### 10.1 Three axes, three objects

| Axis | Object | Values | Owner | Must never be used for |
|---|---|---|---|---|
| session lifecycle | `SessionState` | `new`, `continuing`, `resetting` | session / bridge layer | pause or stop |
| run state | `ExecutionState` | `running`, `pausing`, `ready` today; `paused`, `stopped` added here | `AgentController` | session existence |
| worker availability | worker `status` | worker-defined string | worker run loop | operator intent |

**Adding `PAUSED` to `SessionState` is rejected.** A paused session has not
ended, and neither has a stopped one; session lifecycle and run state answer
different questions, and a single enum cannot answer both. Conflating them makes
the verb "resume" ambiguous between *continue this turn* and *start a new
session*, which is the ambiguity this section exists to remove.

### 10.2 The run-state machine

| From | Event | To | Side effects |
|---|---|---|---|
| `RUNNING` | `pause()` | `PAUSING` | `pause_event` set, pause restriction applied, `execution_state_change` emitted |
| `PAUSING` | turn winds down | `PAUSED` | the model's own summary, worker parks |
| `PAUSED` | `resume()` | `RUNNING` | `pause_event` cleared, restriction cleared |
| `PAUSED` | new operator query | `RUNNING` | implicit unpause, only while `restriction_reason == 'pause'` |
| `RUNNING` | `stop()` | `STOPPED` | turn token set, partial output discarded, no summary, worker parked or detached |
| `PAUSED` | `stop()` | `STOPPED` | as above |
| `STOPPED` | `resume()` | `RUNNING` | `pause_event` cleared, restriction cleared |
| `STOPPED` | new operator query | `STOPPED` | none — implicit unpause does not apply |

Every transition goes through `set_execution_state()` so that
`execution_state_change` is emitted. `agent/controller/__init__.py:542`
currently assigns `PAUSING` to the field directly, so that one transition
emits nothing; Phase 2a must route it through the setter. The event payload is
flat — `old_state` and `new_state` at the top level, no `data` sub-dict.

### 10.3 The four semantics

**Pause** is a soft wind-down: the current turn is asked to stop, is summarised,
and the agent parks with state intact and resumable.

**Stop** is a hard abort: the turn is abandoned, its partial output is not
committed, no summary is produced, and the worker is parked or detached.

**Timeout** is a detach: the caller stops waiting; it says nothing about what
the work is doing. Resumability is §9 question 3.

**Implicit unpause** clears the pause restriction and nothing else. It is not a
resume of a stopped turn, and it never clears the token restriction (§6.3).

### 10.4 The eight pieces and their Phase-2 owner sites

| Piece | Symbol | Owner site (file:line today) | Phase slice |
|---|---|---|---|
| run-state values | `ExecutionState.PAUSED`, `ExecutionState.STOPPED` | enum home `agent/core/state.py` (imported from there at `tests/test_control_plane_contract.py:47`); consumed by `agent/controller/__init__.py` | 2a |
| setter routing | `set_execution_state()` for every transition | `_set_execution_state()` at `agent/controller/__init__.py:415`; the pause write goes through it at `:559` (`PAUSING`) and `:575` (`PAUSED`) | 2a |
| canonical interrupt | `controller.pause_event` (cleared = running, set = pause requested); `tools.cancellation.bind_pause_event(event)` | created `agent/controller/__init__.py:39`, set on pause `:554`, cleared on resume `:582`; the binding is `tools/cancellation.py:58` (`bind_pause_event`), installed on the turn thread at `agent/controller/__init__.py:814` | 2b |
| wait conversion | `Sleep` wakes on `pause_event` with `reason == "paused"`; `time.sleep` becomes `Event` waits | helper `agent/core/agent.py:439` (`_pause_aware_sleep`), reading the bound event at `:459`; the two sleep sites are `agent/core/agent.py:1668` and `agent/core/agent.py:1929` | 2b |
| interrupt recognition | `is_interrupted_tool_entry(entry)` plus a dispatch-loop gate | helper `agent/core/tool_executor.py:164`; batch stop + per-id padding `tool_executor.py:412-437`; gate `agent/core/agent.py:2147-2152` (the tool-less terminal is the `:2153` branch that follows it), i.e. AFTER the assistant commit (`:1997`) and the `turn` yield (`:2006`), and after `_apply_pause_reading_mode()` at `:2133` plus its notice flush (`:2134-2136`) | 2c |
| implicit unpause | `send_query` clears the pause restriction only, ownership-safe (no invented `thread._controller`) | `tools/workspace/worker_thread.py` (`send_query`) | 2d |
| interrupted envelope | `status: "interrupted"`, `exit_code: None` instead of `-3` | `tools/docker_code_runner.py:465-484` | 2e |
| worker-tool pause proxy | an agent-side pause must reach the detached worker's own `Agent` | helper `tools/workspace/worker_thread.py:879` (`_pause_join_target`), called from `_action_join` at `:7005` | 2f |
| symmetric resume | lifting the session main-pause must lift the worker pauses the proxy applied, and only those | recorder `tools/workspace/worker_thread.py:928` (`_record_proxied_join_target`), called at `:7006` only when `_pause_join_target` (`:879`) returned True at `:7005`; per-session record `:4874` (`_PROXIED_JOIN_TARGETS`); lifter `:959` (`_resume_proxied_join_targets`), called from `_discard_session_main_pause` at `agent/core/agent.py:430` | 2g |

**How the gate actually concludes a paused turn — five facts, with sites.**

1. *Recognition is a helper, not a sniff.* `is_interrupted_tool_entry(entry)`
   (`agent/core/tool_executor.py:164`) accepts both dialects a stop can leave
   behind: the sleep dialect
   (`{"tool":"sleep","ok":false,"interrupted":true,"reason":...}`) and the
   container dialect (`{"status":"interrupted","interrupted":true,
   "exit_code":null}`).
2. *The batch is stopped at the boundary, never dropped.*
   `tool_executor.py:412-437`: every call after the interrupted one is answered
   with `{"status":"interrupted","interrupted":true,"reason":"paused",
   "skipped":true,...}` and recorded in `executed_tools`, one result per
   `tool_call` id — the provider rejects an assistant `tool_calls` message whose
   ids are not all answered, so padding is mandatory.
3. *The gate continues the loop; it does not synthesise a summary.*
   `agent/core/agent.py:2123-2137`: if any executed entry is interrupted and the
   restriction is `'pause'`, the loop sets `_pause_winddown` and `continue`s to
   another turn. No hardcoded terminal message is handed to the model.
4. *Ordering: persistence and emission come first.* The gate sits after
   `turn_transaction.commit()` (`agent.py:2033-2034`), after the `tool_call`
   (`:2035-2038`) and `tool_result` (`:2039-2042`) yields, after the pending
   warning flush (`:2043-2052`), and after `_apply_pause_reading_mode()` at
   `:2119` plus its notice flush (`:2120-2122`). Nothing already sent to the
   frontend is retracted.
5. *The summary is the model's own, prompted by the existing notice, and the
   wind-down is bounded.* `_apply_pause_reading_mode()` (`agent.py:1361-1396`,
   text `:1387-1393`) is the only new instruction the model sees ("Do not start
   new work and do not call tools … Summarize what you have done and what
   remains, then reply with Respond."). The grant is scoped to one query
   (reset at `agent.py:1506-1507`, released by `leave_reading_mode('pause')` at
   `:1352-1357`), the turn-start checkpoint skips `stop_check` only while it is
   held (`:1544-1550`), and the loop is bounded by
   `range(self.config.max_turns)` (`:1536`).

`tests/test_control_plane_contract.py` is the executable form of this section:
13 tests, all green today, every one tagged `# PIN` — including the ones that
were written red-first and are now satisfied (`ExecutionState` defines
`paused`/`stopped`, the controller settles on `PAUSED`, the interrupted
envelope is recognised).

### 10.5 What is still not interrupted

The shared `Event` solves wake-up and in-flight waits: anything blocking on that
event returns promptly with `interrupted: True`. A process already running
inside a container is not killed, and the envelope must say so rather than
report a fake success or a fake exit code (see §9 question 2). That is the one
residual gap, and it is accepted.

### 10.6 Invariants that must not regress

- `tests/test_agent_reading_mode.py:150-165` — the after-turn checkpoint must
  not move `execution_state`; `PAUSED` is owned by `controller.pause()` and
  `enter_reading_mode`.
- `tests/test_worker_pause_reading_mode.py:102-113` — the worker's `pause()`
  must not write `status`; the worker run loop owns that field.
- `tests/test_state_pause_restriction.py:104-108` — `get_allowed_tools()`
  returns `[]` when unrestricted, so §6.3 Option A's `None` sentinel
  (doc L421-424) is unavailable without a fresh operator ruling.
