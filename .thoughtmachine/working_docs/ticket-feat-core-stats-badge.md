# Ticket: feat/core-stats-badge

**Filed:** 2026-11-02, from the W8 P3-S0 backlog pass, while reading `StatusBar.jsx` against the values the bridge already broadcasts.
**Severity:** MEDIUM — pure addition, no behaviour change to existing paths; three of the four wanted numbers do not reach any UI surface today.
**Verified at:** HEAD `73b1d325` — every line below was read at that SHA. Items marked *to confirm at implementation* were NOT verified and must not be asserted.
**Status:** filed, not dispatched. NOT to be built in this wave.

## Relationship to the ticket that already exists

**`ticket-feat-core-query-duration-stats.md` is the shared prerequisite, not a duplicate.** That ticket
owns the *monotonic elapsed measurement* itself (its D2: every existing measurement in the tree uses
`time.time()`, and no `monotonic`/`perf_counter` exists under `agent/`), plus the `duration_ms` field on
`turn` and `session_stop`. This ticket owns only the **display surface** and the **non-duration
fields** (tokens, history size).

| | `feat/core-query-duration-stats` | This ticket |
|---|---|---|
| Owns | the elapsed measurement + `duration_ms` on events | the badge that renders it |
| Owns | monotonic-vs-wall-clock decision | — |
| Does not own | — | token totals, history size, per-worker runtime |

**What this ticket explicitly does NOT cover:** it does not define the duration measurement, does not
add `duration_ms` to any event, and does not touch the dispatch report header
(`ticket-feat-dispatch-report-duration.md`, Gen 44). If the duration ticket is not dispatched first,
this ticket's "runtime/elapsed" line has no source and must wait.

## What already exists (do not rebuild it)

`web_ui/frontend/src/components/StatusBar.jsx` is **42 lines** and is the only session-stats surface in
`web_ui/frontend/src/components/` — there is no `Stats*` or `Memory*` panel (directory listing checked).

```jsx
function StatusBar({ status, tokensIn, tokensOut, contextLength }) {
...
      <span>Tokens: In={tokensIn} / Out={tokensOut}</span>
```

So token totals and context length are **already displayed**; elapsed time and history size are not.

**Values already plumbed to the frontend:**

| Value | Source | Reaching the UI as |
|---|---|---|
| cumulative input/output tokens | `session.total_input_tokens` / `.total_output_tokens` | `tokens_updated {input, output}` — `bridge.py:2727-2731` (initial emit on load) |
| live token deltas | `token_update` event | `bridge.py:3176+` |
| context length | `session.context_length` | `context_updated {context_length}` — `bridge.py:2721-2723` |
| history size | `len(session.user_history)` | **not on the badge**, but computed at `bridge.py:2738-2739` (logged), `:2684` (`total_count`), and sent as `session_loaded.message_count` (`:2693`) |

## What is actually missing

1. **Elapsed / runtime is measured but never leaves `agent/core/`.** `AgentState.time_start`
   (`agent/core/state.py:67`) is set at `agent/core/agent.py:1400`
   (`self.state.time_start = time.time()`), cleared at `state.py:384`, and read at exactly one place —
   the soft-budget warning at `agent/core/agent.py:1430-1431`
   (`elapsed = time.time() - self.state.time_start`). **No `bridge.py` code reads `time_start` or
   computes elapsed** (grep: zero hits outside `agent/core/`). It also uses `time.time()`, not a
   monotonic clock — which is why the duration ticket's D2 governs here too.
2. **History size is computed and then dropped on the floor**, three times, at the lines in the table
   above. Nothing renders it.
3. **Per-worker runtime has a real source, but only inside the worker thread.** `tools/workspace/worker_thread.py`
   carries `started_at` (ISO string, set at `:2711`, declared `:1108`), `last_heartbeat` (ISO string, set
   at `:2380` and `:2708`, declared `:1102`), `_budget_started_at` (`time.monotonic()`, `:1096`) and
   `_last_heartbeat_monotonic` (`:1103`). `started_at` is already included in the worker payloads
   emitted at `:2908, :2964, :3073, :3151`. **To confirm at implementation:** the exact envelope of
   those four payloads (`worker_status` vs `worker_completed` vs the status-dict shape) was not read
   this pass, so it is not asserted here that `started_at` reaches the bridge intact or that the bridge
   forwards it to the frontend.
4. **There is nowhere to put the badge.** `StatusBar.jsx` takes four flat props and renders two
   `<span>`s. Adding a runtime and a history count is either two more props on the same bar or a new
   component — *to confirm at implementation*, together with whether `SessionTab.jsx` passes the props
   through or the bar reads the store directly.

## Design decisions (not one-liners)

- **D1. Clock.** Runtime shown to the operator must be computed from a monotonic source, not
  `time.time()` — same reasoning and same clock as `ticket-feat-core-query-duration-stats` D2
  (NTP steps and suspend/resume make wall-clock deltas jump or go negative). Do not add a second,
  differently-clocked elapsed measurement to `state.py`; consume the duration ticket's measurement.
- **D2. Scope of "runtime".** Per-query span, per-session span, and per-worker span are three different
  numbers. The operator's badge most likely wants the **session** span (it is the one that survives
  between turns), with per-worker runtime as a secondary line — but say which, because
  `time_start` today is reset per query (`agent/core/agent.py:1400`), not per session.
- **D3. Tokens are already there — do not re-plumb.** `tokens_updated` and `context_updated` exist and
  are replayed on load (`bridge.py:2721-2731`). A badge that re-derives them from `token_update`
  events alone will read 0/0 after a reconnect; that is the bug the initial emits exist to prevent.
- **D4. History size units.** `len(session.user_history)` counts *entries*, including tool results —
  it is not a turn count and not a token count. Label it as entries, or compute turns separately.
  `session_loaded.message_count` (`bridge.py:2693`) uses the same list, so the numbers will agree.
- **D5. Must not slow the status path.** `_map_and_emit` runs on every agent event
  (`bridge.py:3158-3345`); anything added there is on the hot path. A recomputed
  `len(self._session.user_history)` per event is O(1) but the normalisation around it is not — read
  the value at the emit sites already listed instead.

## Acceptance

- The status surface shows, at minimum: elapsed runtime, cumulative tokens in/out, context length, and
  history size.
- Elapsed runtime does not go negative or jump across a wall-clock step (monotonic, per D1).
- After a page reload / session reconnect the badge shows the same numbers as before the reload — no
  zeroing (exercises D3 and `bridge.py:2721-2731`).
- Per-worker runtime, if included, is sourced from the worker's own `started_at` / `last_heartbeat`
  and is labelled per-worker rather than folded into the session number.
- Zero change to any existing broadcast payload shape (all additions optional).
- No new elapsed measurement is introduced in `agent/core/state.py` — the duration ticket's
  measurement is consumed, not duplicated.

## MUST NOT TOUCH

- The `tokens_updated` / `context_updated` initial emits at `bridge.py:2721-2731` — they are what keeps
  the bar correct after a reconnect, and a badge rework is the likeliest place they get "simplified away".
- `agent/core/agent.py:1430-1431` and `agent/core/state.py:67, 384` — `time_start` is load-bearing for
  the soft-budget timeout path. A display feature must not repurpose or reset it, and must not change
  its clock underneath the timeout logic.
- The worker status/heartbeat fields (`tools/workspace/worker_thread.py:1096, 1102-1103, 1108, 2711,
  2380`) — the liveliness/staleness check at `:1407-1423` reads `last_heartbeat`, and the budget kill at
  `:1301` reads `_budget_started_at`. Read them from the display side only.
- `agent/core/state.py`'s `ExecutionState` — see `ticket-bug/paused-state-never-emitted`; the status
  indicator's state set is under repair there and must not be edited for badge reasons.
