# Ticket: bug/paused-state-never-emitted

**Filed:** 2026-11-02, from the W8 P3-S0 backlog pass, while auditing the `execution_state_change` → `state_changed` mapping against the `ExecutionState` enum.
**Severity:** HIGH — `PAUSED` is never emitted, so the UI's Resume button is permanently disabled and paused *workers* have no reachable resume path.
**Verified at:** HEAD `73b1d325` — every line reference below was read at that SHA, not exercised at runtime. NOT blocking, NOT to be fixed in this wave.
**Status:** open, report-only. This ticket authorises nothing.

## Finding

`_map_and_emit` translates the agent's `new_state` into a frontend state through a
nine-entry dict (`web_ui/backend/bridge.py:3160-3170`):

```python
frontend_state = {
    "running": "RUNNING",
    "pausing": "PAUSING",
    "paused": "PAUSED",
    "idle": "IDLE",
    "error": "IDLE",
    "stopped": "IDLE",
    "completed": "IDLE",
    "ready": "IDLE",
    "waiting": "WAITING_FOR_USER",
}.get(new_state.lower(), new_state.upper())
```

**Only three of those nine keys are reachable.** `ExecutionState` (`agent/core/state.py:22-26`)
has exactly three members and no `PAUSED`:

```python
class ExecutionState(enum.Enum):
    """Unified execution state for agent and GUI."""
    RUNNING = 'running'
    PAUSING = 'pausing'
    READY = 'ready'
```

Every production producer of `new_state` emits one of those three values.
`AgentState.set_execution_state` (`agent/core/state.py:339-349`) builds the payload from
`new_state.value`; its call sites are `state.py:388`, `agent/core/agent.py:846, 1383, 1388, 1416`,
and `agent/controller/__init__.py:804` — all `READY` / `RUNNING` / `PAUSING`. The four remaining
literal emitters are `agent/controller/__init__.py:426, 440, 544`, each `'new_state': 'pausing'`.

So `paused`, `idle`, `stopped`, `completed`, `error` and `waiting` are dead keys. `waiting` is
doubly dead *because* the live path bypasses the dict: `bridge.py:3303` broadcasts the literal
string `"WAITING_FOR_USER"` from the `agent_responded` / `response_type == 'question'` branch.

The `paused` **event type** never reaches the bridge either. The `_map_and_emit` dispatch chain is
`bridge.py:3158, 3176, 3209, 3217, 3228, 3242, 3258, 3270, 3294, 3313` and then `:3333`
(`session_stop`) — there is no `paused` branch. `session/event_schema.py:293 create_paused()` has
**zero callers**; `'type': 'paused'` occurs only in tests
(`tests/integration/test_state_transitions.py:491`, `tests/test_event_processor_persistence.py:195`).

The reconnect path does not rescue it. `bridge.py:2695-2701` records explicitly that the server no
longer sends a `state_changed` after `session_loaded` — `session_loaded` carries `is_running` only.
The remaining literal `state_changed` broadcasts are all `"IDLE"`
(`bridge.py:2861, 3308, 3342, 3359, 3442`) or `"WAITING_FOR_USER"` (`:3303`), plus
`web_ui/backend/server.py:3190` `"IDLE"`.

`web_ui/backend/server.py:76` still documents `PAUSED` as a state the server sends:

```
state_changed       { "type": "state_changed",       "state": "IDLE|RUNNING|PAUSED|WAITING_FOR_USER", "is_running": bool }
```

## Why it matters

The frontend built a real, load-bearing consumer for a state that is never produced.

**`Resume` can never be enabled.** `web_ui/frontend/src/components/QueryBar.jsx:34` reads
`const isPaused = status === 'PAUSED'`, and `:136` reads:

```jsx
<button className="btn btn-resume" onClick={handleResume} disabled={!isPaused}>
```

`isPaused` is permanently `false`, so that button is permanently `disabled`. The same unreachable
state also kills `handleToggle`'s `isPaused` branch (`:63-68`, `sendCommand('continue_session', …)`),
the Enter-to-resume path (`:78`, `isIdle || isWaiting || isPaused`), `TabBar.jsx:23`
(`case 'PAUSED':`) and `StatusBar.jsx:19` (`case 'PAUSED':`).

**This is a functional loss, not a cosmetic one, because `resume_session` is the only path that
resumes paused workers.** `web_ui/backend/server.py:1530` maps `resume_session` → `bridge.resume()`
(`bridge.py:1738`), and that method's worker loop is the sole caller of `thread.resume()` for parked
workers (`bridge.py:1755-1776`):

```python
for key, thread in list(_worker_registry.items()):
    ...
    if sid == self._session_id:
        if getattr(thread, "status", None) != "paused":
            continue
        outcome = thread.resume()
```

Those workers *are* parked by `bridge.pause()` (`:1717-1736`), which calls `thread.pause()` on every
worker of the session. `continue_session` does not reach the worker loop: in the controller-alive
branch it returns at `:1683` via `self._controller.continue_session(query)`; the
`self.resume()` at `:1705` is only on the no-controller branch. Net effect: **pause parks the
workers, and no reachable UI control un-parks them.** The only fallback is the worker tool's own
`action: "resume"` (`tools/workspace/worker_thread.py:2047`), which requires the operator to know the
worker name and go through the tool surface.

The test suite pins the ghost rather than catching it. `tests/integration/test_state_transitions.py:390`
parametrises `("paused", "PAUSED")` inside `test_execution_state_change_mapping` (`:393`), which calls
`bridge._map_and_emit({"type": "execution_state_change", "new_state": "paused"})` — a payload no
production code can construct. `web_ui/frontend/src/components/__tests__/SessionTabHubStatus.test.jsx:188`
asserts the full `RUNNING → PAUSING → PAUSED → IDLE` transition through a synthetic
`sendStateChanged` helper (`:175, 214`), so the JSX suite is green on a sequence the backend never
emits. Both suites will stay green through any fix here — they test the mapping, not the production
reachability of its keys.

After a pause the session actually lands on `READY` (`agent/core/agent.py:1416` sets `PAUSING`, then
`state.py:388` / `agent/core/agent.py:846` set `READY`), which the bridge maps to `IDLE`. So the
operator sees RUNNING → PAUSING → IDLE, with no state in which Resume is offered.

## Disposition options

- **A. Make `PAUSED` real.** Add `PAUSED = 'paused'` to `ExecutionState` (`state.py:22-26`) and emit it
  from the pause-completion path — the natural site is `agent/controller/__init__.py:418`
  (`session_stop` with `stop_reason='paused'`), or the `PAUSING → READY` transition at
  `state.py:388` when the stop reason is a pause. Delete the six dead keys from
  `bridge.py:3160-3170` in the same diff and add a `paused` branch to `_map_and_emit` if the event
  type is kept. *This is the recommendation* — it preserves the operator's Pause/Resume verb, which the
  worker-resume path depends on, and it makes the dict say only what can happen.
- **B. Remove the Resume control.** Drop `disabled={!isPaused}` and the `isPaused` branches from
  `QueryBar.jsx`, add a `bridge.resume()` call to whatever replaces them (or to the `continue_session`
  path at `:1683`), and prune the dead dict keys. Smaller diff inside the UI, but it silently changes
  the operator's Pause/Resume model and needs the worker-resume reachability re-established by hand.
- **C. Leave the mapping, fix only the reachability of workers.** Add the `thread.resume()` loop to the
  `continue_session` path so a pause is recoverable via Run. Cheapest, but it leaves a permanent
  `disabled` button in the UI as a trap for the next reader, and leaves `server.py:76` lying.

Recommendation: **A**. If the operator has already stopped using Pause in favour of Stop, **B** is the
honest version and A's enum addition should be dropped with it.

## Acceptance

- `state_changed` with `state: "PAUSED"` is observed in a real session after `pause_session`, at or
  after the pause checkpoint.
- The Resume button in `QueryBar.jsx` enables in exactly that state and `resume_session` reaches
  `bridge.resume()`.
- A worker paused by `bridge.pause()` is resumed by the same operator action, observable as
  `Worker resumed: <name>` in the bridge log (`bridge.py:1776`).
- Every key remaining in `bridge.py:3160-3170` is reachable from a call site under `agent/`; no key
  is reachable only from a test.
- `server.py:76`'s documented state list matches the set actually emitted.
- `tests/integration/test_state_transitions.py:390` keeps its `("paused", "PAUSED")` case *and* the
  case becomes producible — otherwise delete the case with the key.

## MUST NOT TOUCH

- `bridge.py:3303` — the literal `"WAITING_FOR_USER"` broadcast is the live path for that state; the
  dict entry is not. Removing either without checking both leaves the frontend without a waiting state.
- `bridge.py:2695-2701` — the comment and behaviour that `session_loaded` carries `is_running` but no
  `state_changed`. Re-adding a state broadcast after `session_loaded` is "Fix 2a" being reverted; not
  this ticket's business.
- `state.py:22-26`'s member set as a whole — `RUNNING` / `PAUSING` / `READY` are each consumed by
  `is_busy` (`bridge.py:3156`, `web_ui/frontend/src/components/QueryBar.jsx:33-36`) and by
  `is_running` on the reconnect path. Adding `PAUSED` must not change `is_busy`'s meaning for the
  existing three.
- `bridge.py:1755-1776`'s `getattr(thread, "status", None) != "paused"` guard — the `paused` guard is
  correct; the worker-side `paused` status is real (`tools/workspace/worker_thread.py:2363`), unlike
  the agent-side enum value. Do not "fix" the worker status to match the agent enum.

## CORRECTION (appended, verified at HEAD by read-only recon)

**Status:** the premise of this ticket is stale; the reported symptom is real but arrives by a different mechanism.

### What was claimed vs. what is true now

| claim in this ticket | actual state at HEAD |
|---|---|
| `ExecutionState` has no `paused` | `agent/core/state.py:22-28` defines RUNNING/PAUSING/PAUSED/STOPPED/READY |
| the state mapping lives at `bridge.py:3160-3170` and has dead keys | `bridge.py:3160-3170` is `_core_persisted_error_notice`; the real mapping is **`bridge.py:3233-3249`**, 9 keys, including `"paused": "PAUSED"` and `"pausing": "PAUSING"` |
| nothing produces PAUSED | `agent/controller/__init__.py:559` sets PAUSING, `:575` sets PAUSED |
| no test proves the mapping | `tests/integration/test_state_transitions.py:388-419` is a green parametrized test asserting `("paused","PAUSED")`, `("pausing","PAUSING")`, `("ready","IDLE")`, `("waiting","WAITING_FOR_USER")` |

### The real root cause of the reported symptom

A **load/reconnect** path collapses the state (`store/useStore.js:179`):
`state: payload?.is_running ? 'RUNNING' : 'IDLE'`

`session_loaded` (`bridge.py:2767-2783`) carries only the boolean `is_running`, computed as
`controller.is_busy` = `execution_state in (RUNNING, PAUSING)` (`agent/controller/__init__.py:113`).
Therefore on reload:

- PAUSED -> `is_running=False` -> painted **IDLE**
- PAUSING -> `is_running=True` -> painted **RUNNING** (wrong in the other direction)

And there is no recovery: `session_loaded` carries no state string / `is_paused` / `can_resume`;
`bridge.py:2773-2776` documents that the server deliberately no longer sends a follow-up
`state_changed` (Fix 2a); and `grep` for `get_state|request_state|is_paused|can_resume` in non-test
frontend `src` returns **zero hits**. `QueryBar.jsx:136` is `disabled={!isPaused}`, so a reloaded
paused session shows Idle **and its Resume button is dead** — the reported symptom, reproduced
without any missing execution state.

Live sessions are unaffected: `SessionTab.jsx:579-589` + `store/useStore.js:283-293` store the real
`state_changed` value faithfully.

### Also verified NOT broken (do not "fix" these)

- Pause/stop system notices are persisted and rendered. `agent/core/message.py:48-63` builds them
  (`role='user'`, `is_system_notification=True`, `metadata.kind` = `pause_nudge`/`stop_notice`);
  `bridge.py:2476-2485` re-normalises and injects the flag; `bridge.py:3016-3057` persists +
  broadcasts via `conversation_changed`; `MessageBubble` renders them through `SystemNoticeText`.
  There is no transient notice event.
- `pending_warnings` and `pause_nudge` have **zero hits** in `web_ui/frontend/src`;
  `role === 'system'` has **zero hits**. No frontend filter hides these notices.
- The only message filter, `SessionTab.jsx:693-701`, drops stale
  `[SYSTEM NOTIFICATION] Configuration updated` duplicates (with a TODO that dedup belongs on the
  backend). Unrelated to pause/stop.

### Remaining real defects (recommend re-scoping this ticket to these)

1. `store/useStore.js:179` load-path collapse (above). Candidate fix: add the execution state string
   to the `session_loaded` payload and prefer it client-side, falling back to the current
   `is_running` logic. *This adds a field; it does not re-add the `state_changed` broadcast that
   Fix 2a removed.*
2. `SessionTab.jsx:46-58` `messagesEqual` compares only length + first/last role+content, and is the
   zustand equality fn at `SessionTab.jsx:143`, so a mid-list message change is treated as equal and
   never re-renders. Appends and streaming-tail updates are fine.
3. Coverage gaps: `store/__tests__/useStore.test.js:537-538` only loads `is_running:true`, so the
   collapse is untested. `SessionTabHubStatus.test.jsx` only sends synthetic `state_changed` payloads
   and never exercises the Python mapping, and `StatusBar`/`TabBar` have almost no direct
   paused/pausing coverage.

