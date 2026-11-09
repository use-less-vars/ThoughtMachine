# Design: Pause Is a Reading Mode, Not a Halt

Status: design only. No source file is modified by this document; it records the
pause model already present in the tree at the W7 isolation worktree, after the
worker-wiring commit `0565aec1`. Every architectural claim carries a `path:LINE`
citation taken from static reconnaissance of that worktree. Claims that could not
be pinned down are listed under "Open questions" rather than asserted.

**The model in one sentence:** Pause does not stop anything — it takes the agent
out of *acting* and puts it into *reading*. `AgentState` is restricted with
reason `'pause'`, the tool executor then refuses every tool call except
`Respond`, and the turn thread nudges the model once per turn to summarise and
reply. The turn concludes itself: nothing is truncated, nothing is killed, and
no new turn starts while the restriction is in force.

Pause is therefore the cheap, reversible control. Stop truncates the running
turn; Stop-all/close tears the session down; Kill tears a single worker down.
Pause only narrows what the agent is allowed to do. §7 compares the four.

This document supersedes the pause passages of `docs/design-worker-lifecycle.md`
(Q1's park/detach treatment of `_pause_event`); that document's Stop-cascade
passages now carry supersession notes pointing here.

---

## 1. Old semantics — and why they broke

### 1.1 What pause used to be

Pause was a *scheduling flag plus a caller-side special case*, in past tense:

- The worker owned a park event pair (`self._pause_event` /
  `self._resume_event`) and the agent loop blocked at a park gate while the
  pause event was set. The event pair still exists and still gates the loop —
  what changed is that parking is no longer what "pause" means (§5).
- The requesting side had to know something about the worker's internal
  activity before it could pause it. `web_ui/backend/bridge.py` carried a helper
  `_worker_is_running_async_job` that the pause path consulted, and the pause
  loop skipped workers it judged to be mid-job. That helper has since been
  deleted; it has no hits repo-wide. The pause loop now carries the comment
  "Reading mode makes a pause safe even mid async job … there is nothing to
  skip here any more" (`web_ui/backend/bridge.py:1726-1729`).
- The flag itself was a bare boolean on the agent (`_pause_requested`) that the
  turn loop read at checkpoints it chose, and that a resume cleared
  unconditionally. A legacy unconditional clear is still visible on the bridge
  path today (`web_ui/backend/bridge.py:1751`).

### 1.2 Why that stopped working

- **Nothing in the tool path consulted the flag.** A pause requested while the
  model was issuing tool calls changed nothing until the next checkpoint the
  loop happened to reach, and every tool in the batch ran to completion in the
  meantime. The pause bounded *scheduling*, not *action* — so it did not bound
  token burn, which is the whole point of a Pause button on an expensive agent.
- **"Paused" meant different things on different threads.** The requesting side
  (bridge, worker manager) and the observing side (the agent loop) each had
  their own notion, and the bridge's notion required a per-caller guess about
  internal job state. Every new execution path needed that guess kept in sync;
  a wrong guess either blocked a pause the operator asked for or paused a
  worker that was about to finish anyway.
- **There was no single rejection point.** Whether a tool call succeeded while
  paused depended on where the call entered the system, so the operator-visible
  error differed by path. The reading-mode design collapses this to one refusal
  (§3.1).
- **The flag was unattributed.** A resume cleared `_pause_requested` regardless
  of who set it, so any subsystem could clear another's pause. The replacement
  is owner-scoped on clear (§2.3), which is what makes a shared restriction
  field safe without a lock (§2.4).

---

## 2. The model: one restriction on `AgentState`

### 2.1 `restrict()` / `clear_restriction()`

`AgentState` carries `restriction_reason: Optional[str]`
(`agent/core/state.py:68`), set by `restrict()`
(`agent/core/state.py:392`) and cleared by `clear_restriction()`
(`agent/core/state.py:421`). The reasons already in use are `'token'`
(`agent/core/state.py:160`), `'timeout'` (`agent/core/state.py:231`),
`'turn'` (`agent/core/state.py:312`) and now `'pause'`.

`restrict()` is **last-writer-wins**: it writes the reason unconditionally and
reports whether the write changed the reason
(`agent/core/state.py:414-418`). `'pause'` is therefore just another occupant of
a field the monitors already race for.

The agent-facing wrappers are thin:

- `enter_reading_mode(reason='pause')` — `agent/core/agent.py:1182`: calls
  `state.restrict(reason)`, sets `_pause_requested = True`
  (`agent/core/agent.py:1206`) and returns whatever `restrict()` returned, i.e.
  `True` only when this call actually changed the restriction.
- `leave_reading_mode(reason='pause')` — `agent/core/agent.py:1210`: calls
  `state.clear_restriction(reason)` and, **only when it released**, resets
  `_pause_requested` and `_pause_nudge_sent` (`agent/core/agent.py:1223-1226`).
- `request_pause()` — `agent/core/agent.py:1277`: a thin alias kept for existing
  callers (`tools/workspace/worker_thread.py`, `web_ui/backend/bridge.py`).

### 2.2 `get_allowed_tools()` — the allow-list *is* the enforcement

`get_allowed_tools()` (`agent/core/state.py:449`) maps the reason to an
allow-list, and an empty list means "unrestricted":

| `restriction_reason` | allow-list |
|---|---|
| unrestricted (`restrictions_active` false) | `[]` — no restriction |
| `'token'` | `['Respond', 'SummarizeTool']` |
| `'pause'` | `['Respond']` |
| `'timeout'` | `['Respond']` |
| `'turn'` | `['Respond']` |
| unknown / `None` / empty string | `['Respond']` (fail-safe) |

`is_tool_allowed()` (`agent/core/state.py:476`) is the predicate the executor
calls. Note the fail-safe branch (`agent/core/state.py:470-474`): an
unrecognised reason grants the *narrowest* list, never the token list, so a
misspelled or future reason cannot silently re-enable tools.

The consequence worth stating plainly: while paused the agent can only reply.
Not even `SummarizeTool` (which `'token'` permits) is available, because a pause
needs no summarisation to recover.

### 2.3 Ownership on clear

`clear_restriction(reason)` is **owner-scoped**
(`agent/core/state.py:421-446`): when `reason` is not `None` and the stored
reason differs, it returns `False` and clears nothing. So
`leave_reading_mode('pause')` can never wipe a `'token'`, `'timeout'` or
`'turn'` restriction owned by a monitor. `reason=None` is the explicit
owner-agnostic clear. This is the property that replaced the old
unattributed-flag bug in §1.2.

### 2.4 Why there is no lock

Deliberate. Each field write is a single atomic attribute assignment and the
correctness argument comes from *ownership on clear*, not from mutual exclusion
— the docstrings state this directly (`agent/core/state.py:392-446`). Two
threads may therefore enter and leave reading mode concurrently without a
deadlock risk on the turn thread's hot path, and a monitor restricting in the
same window cannot be left holding a mutex it never took. Section 8 records the
one consequence of last-writer-wins that is visible to users.

---

## 3. What actually stops the tools

Two mechanisms, in this order. Neither of them halts the turn.

### 3.1 The executor refusal (the hard stop)

The tool executor checks the allow-list at dispatch, immediately after the
permission checks (`agent/core/tool_executor.py:272` —
`if not self.state.is_tool_allowed(tool_name):`), and returns a refusal string
rather than executing (`agent/core/tool_executor.py:825`):

> `Tool '<name>' not allowed. Reason: <restriction_reason>. Available tools: <allow-list>`

This is the *only* rejection path — `_apply_pause_reading_mode()` deliberately
does not hand-reject the pending tool calls
(`agent/core/agent.py:1245-1248`: "no early return, no state transition to
PAUSING, no halt flag, no truncation of the turn, and no hand-rejection of the
pending tool calls (the executor refuses them because of the restriction — one
rejection path, not two)"). The refusals are ordinary tool results, so the turn
continues with the model seeing why it could not act.

### 3.2 The one-per-turn `[SYSTEM NOTIFICATION]` nudge

`_apply_pause_reading_mode(tool_calls=None)` (`agent/core/agent.py:1230`) runs
**on the turn thread**, after the LLM response arrives and before the pending
tool calls execute. It no-ops unless the live restriction is `'pause'`
(`agent/core/agent.py:1252-1253`). Otherwise, and only if
`not self._pause_nudge_sent and tool_calls` (`agent/core/agent.py:1255`), it
buffers **one** `Message`:

- `role='user'`, `is_system_notification=True`, body beginning
  `[SYSTEM NOTIFICATION] The operator has paused this session. Do not start new
  work and do not call tools; tool calls will be refused. Summarize …`
  (`agent/core/agent.py:1256-1266`);
- `metadata={'warning_id': str(uuid.uuid4()), 'pause_nudge': True}`
  (`agent/core/agent.py:1267`);
- appended to `_pending_warnings` (`agent/core/agent.py:1268`) — the same buffer
  the token/timeout/turn monitors use (`agent/core/agent.py:385`), which the
  tool-execution branch flushes *after* `turn_transaction.commit()`
  (`agent/core/agent.py:1883`, flush at `agent/core/agent.py:1894-1900`).
  The nudge therefore lands in `user_history` **after** the tool results and is
  part of what the next LLM iteration reads. The after-turn checkpoint flushes
  the same buffer in place (`agent/core/agent.py:1968-1969`).
- `_pause_nudge_sent = True` (`agent/core/agent.py:1269`), reset on
  `leave_reading_mode()` (`agent/core/agent.py:1226`). One nudge per pause, not
  one per tool call.

The helper then consumes `_pause_requested`
(`agent/core/agent.py:1272-1275`): once reading mode is in force there is
nothing left to pause.

### 3.3 There is no inject / steer / queue API

The pause path deliberately adds **no** new control surface: no user-message
injection, no turn steering, no queued follow-up. The nudge is a buffered
warning, not a queued turn; the only thing the operator can do to a paused
session is resume it. Anything richer would have to be constructed off the turn
thread (see §4).

---

## 4. ⚠️ THE CROSS-THREAD RULE — flags and attributes only

> **Pause is requested from a non-turn thread. That thread may write flags and
> state attributes and nothing else. Every `Message` — and every touch of
> message history — is constructed on the turn thread.**

Who calls in from where:

- the web bridge's `pause()` (`web_ui/backend/bridge.py:1707`) — the Pause
  button, i.e. the bridge thread;
- a worker's `pause()` (`tools/workspace/worker_thread.py:1626`) and its
  command-file poller branch (`tools/workspace/worker_thread.py:1971`) — the
  manager/worker thread.

What those paths may do, and do:

- `state.restrict('pause')` / `state.clear_restriction('pause')` — single atomic
  attribute writes (`agent/core/state.py:392`, `:421`);
- set `_pause_requested` (`agent/core/agent.py:1206`) and clear
  `_pause_requested` / `_pause_nudge_sent` on a successful release
  (`agent/core/agent.py:1225-1226`).

What they must not do, and do not: construct or append a `Message`, touch
`messages` / `user_history`, drive `turn_transaction`, or emit presenter events.
Both wrappers say so explicitly (`agent/core/agent.py:1193-1201` and
`:1215-1216`).

**Why:** the message list and `turn_transaction` are owned by the turn thread. A
`Message` appended from the bridge would race the turn thread's own list
mutation, and — even if it survived — it would be interleaved at a point that
*precedes* the tool results, because the intended insertion point is the flush
that happens after `turn_transaction.commit()`
(`agent/core/agent.py:1883`, `:1894-1900`). Chronological order in the
transcript, and therefore the shape of the provider's message sequence, would be
wrong. Buffering the nudge on `_pending_warnings` and materialising it inside
`_apply_pause_reading_mode()` on the turn thread is what keeps the two halves
consistent: the pause-side write is a flag, the message is born on the turn
thread.

This is the rule to check first when extending pause: **if a new code path wants
to tell the model something, it must go through a buffer that the turn thread
drains, never through a direct `Message` append.**

---

## 5. What is unchanged

Reading mode was added *alongside* the old parking machinery; it did not replace
it.

- **`_park_event` is still set, and the idle gate still holds.** The park/wake
  event pair remains on the worker, and the agent loop still blocks on
  "park event set and not stopping" at its park loops. Parking is still what
  makes a paused worker consume no tokens between turns — reading mode only
  stops *tool dispatch*, it does not suspend the loop.
- **`status = "paused"` is written by the run loop, not by `pause()`.**
  `pause()` (`tools/workspace/worker_thread.py:1626`) sets the restriction and
  the park event; the string is written at
  `tools/workspace/worker_thread.py:2297`, after the loop reaches the park gate.
  This is unchanged behaviour.
- **State asymmetry.** There are two state machines and they do not update each
  other synchronously: the agent-side restriction (`restriction_reason ==
  'pause'`, live the instant `enter_reading_mode()` returns) and the worker-side
  status (`"paused"`, live only once the loop parks at
  `tools/workspace/worker_thread.py:2297`). "Paused but not yet parked" is a
  real, observable window; §8 records what reads it.

---

## 6. Worker wiring (`0565aec1`)

Four call sites, all added by the worker-wiring commit and all written the same
defensive way — `getattr(self, "_agent", None)` guard, `try/except`, `WARNING`
log — because a worker may legitimately have no agent yet:

| Site | Line | Call |
|---|---|---|
| `WorkerThread.pause()` | `tools/workspace/worker_thread.py:1643` | `agent.enter_reading_mode("pause")` |
| `WorkerThread.resume()` | `tools/workspace/worker_thread.py:1711` | `agent.leave_reading_mode("pause")` — placed *after* the killed-refusal guard, so a killed worker is never "resumed" into reading-mode release |
| `_poll_command` `'pause'` branch | `tools/workspace/worker_thread.py:1971` | `enter_reading_mode("pause")` |
| `_poll_command` `'resume'` branch | `tools/workspace/worker_thread.py:2005` | `leave_reading_mode("pause")` |

The two `_poll_command` branches **duplicate** the guarded call rather than
delegating to `self.pause()` / `self.resume()`, because the poller runs while
the worker loop owns the instance and the intent is the state change only, not
the surrounding park/status bookkeeping. The duplication is deliberate; it is
also the thing most likely to drift if the guarded-call shape changes (see
Open questions).

Bridge side (`web_ui/backend/bridge.py`):

- `pause()` (`:1707`) iterates session workers and has **no skip**: the
  `_worker_is_running_async_job` helper that used to gate it is gone, and the
  comment at `:1726-1729` records why ("Reading mode makes a pause safe even mid
  async job … there is nothing to skip here any more").
- `resume()` (`:1736`) releases the restriction *owner-scoped* —
  `self._agent.leave_reading_mode('pause')` at `:1748`, inside
  `try/except AttributeError: pass` for agents that predate the method — and
  then still performs the legacy unconditional `self._agent._pause_requested =
  False` (`:1751`). The legacy clear is kept for older agent objects and is
  harmless when the release already ran (§2.3 makes the release itself the
  authority).

`_stop_session_workers` is gone from the codebase entirely; the only reference
left is the assertion that it must not come back
(`tests/test_stop_is_turn_scoped.py:302`).

---

## 7. Stop vs Pause vs Stop-all vs Kill

Cross-referenced against `docs/design-worker-lifecycle.md` (which owns the
worker state machine and the Stop/Kill vocabulary; its Stop-cascade passages now
carry supersession notes because Stop is turn-scoped).

| Control | Scope | What it does | Reversible? | Where |
|---|---|---|---|---|
| **Pause** | one session's agent (and, via the worker hooks, one worker's agent) | restricts `AgentState` to `['Respond']`; nudges the model once to conclude; the turn finishes itself; new turns stay paused | yes — `leave_reading_mode('pause')` | this document; `agent/core/agent.py:1182`, `:1210` |
| **Stop** | the running turn | truncates the current turn only; the bridge thread and session survive | n/a (turn-scoped) | `docs/design-worker-lifecycle.md` Q1; `tests/test_stop_is_turn_scoped.py` |
| **Stop-all / close session** | the session and its worker fleet | ends the session; the old cascade over session workers (`_stop_session_workers`, formerly `web_ui/backend/bridge.py:1881`) was **deleted** — Stop no longer touches session workers | no | `docs/design-worker-lifecycle.md`; `tests/test_stop_is_turn_scoped.py:302` |
| **Kill** | one worker | terminate path: join with a budget, save context, reclaim containers, pop the registry | no | `docs/design-worker-lifecycle.md` Q1; `tools/workspace/worker_thread.py` (`_action_stop`, `:5243`) |

The distinction that matters operationally: Pause is the only one of the four
that is *expected* to be undone, and the only one that leaves the turn running
while denying it agency.

---

## 8. Known consequences and limits

1. **An in-flight tool finishes.** The refusal happens at dispatch
   (`agent/core/tool_executor.py:272`), so a tool already executing when Pause
   lands runs to completion — a long `DockerCodeRunner` call is not interrupted.
   Pause bounds *further* actions, not the current one.

> **AMENDED — Gen 61.** This section is superseded on two points. (1) Pause now interrupts in-flight waits: the tool-facing binding is `tools.cancellation.bind_pause_event(controller.pause_event)`, a process-wide `threading.Event` shared across the bridge thread and the turn thread (the thread-local `tools.cancellation.interrupt()` cannot cross threads, which is why the event exists). Polarity: CLEARED = running, SET = pause requested. `Sleep` wakes promptly and reports `reason == 'paused'`. (2) What is still NOT interrupted: a process already running inside a container is not killed — `DockerCodeRunner` returns `status: interrupted` with `exit_code: None`, and the process may still be running. The rest of this item stands.

2. **Paused-but-not-parked workers are still "busy", and the bridge filter
   misses them.** `bridge.resume()` only touches workers whose status it reads
   as `"paused"` (`web_ui/backend/bridge.py:1761` —
   `if getattr(thread, "status", None) != "paused":`). Because `status` is
   written by the run loop at `tools/workspace/worker_thread.py:2297`, a worker
   paused mid-turn still reports its previous status, so this filter skips it —
   the agent-side restriction is released elsewhere (the per-worker resume path
   at `tools/workspace/worker_thread.py:1711`), but the bridge-level sweep does
   not see it.
3. **The `'paused'` event handler is dead code.** `agent/presenter/event_processor.py:78`
   handles `elif event_type == 'paused':`, but nothing under `agent/` or
   `web_ui/` emits that event any more. It is a leftover of the pre-reading-mode
   pause protocol.
4. **The allow-list stays `['Respond']` until resume.** A paused session cannot
   be queried, cannot be summarised, and cannot be re-tasked; the operator
   resumes or stops it. This is intentional but worth knowing when triaging "the
   agent will not answer me".
5. **A monitor restriction can elide a pause.** `restrict()` is last-writer-wins
   (`agent/core/state.py:414-418`), so a `'token'`/`'timeout'`/`'turn'`
   restriction taken after the pause overwrites the reason; the pause-owned
   clear then returns `False` (`agent/core/state.py:439-446`) and
   `leave_reading_mode()` becomes a no-op. The pause is not re-applied
   automatically when the monitor releases.
6. **The nudge needs tool calls.** It is buffered only when the response carries
   pending tool calls (`agent/core/agent.py:1255`). A paused turn whose LLM
   response has none simply concludes — no notification is inserted, which is
   correct but means the transcript may show no trace of the pause at all.
7. **One nudge per pause**, gated by `_pause_nudge_sent`
   (`agent/core/agent.py:1255`, `:1269`) and reset only on release
   (`agent/core/agent.py:1226`). A pause that spans many turns nudges once.

---

## Open questions

1. **Should `pause()` write `status = "paused"` itself?** Today the string is
   written only by the run loop (`tools/workspace/worker_thread.py:2297`), which
   leaves the window described in §8.2 wide enough for the bridge's
   `status != "paused"` filter (`web_ui/backend/bridge.py:1761`) to skip a
   worker that is already restricted. Writing the status at
   `tools/workspace/worker_thread.py:1626` would close it, but changes what
   `status` means (intent vs observation) — not decided.
2. **Should pause survive a monitor restriction overwriting it?** §8.5 means a
   pause can be silently elided by a `'token'`/`'timeout'`/`'turn'` write. A
   re-apply on monitor release, or a multi-owner restriction set, would fix it
   at the cost of the simple single-reason model — not decided.
3. **Is the `'paused'` handler (`agent/presenter/event_processor.py:78`) to be
   deleted or re-emitted?** Nothing emits it today; deleting it is the cheaper
   option, but no presenter-side requirement has been stated.
4. **Should the two duplicated `_poll_command` branches delegate?**
   `tools/workspace/worker_thread.py:1971` and `:2005` repeat the guarded
   `enter_reading_mode` / `leave_reading_mode` calls instead of calling
   `self.pause()` / `self.resume()`. That is intentional today; if the guard
   shape changes, four sites must change together.
