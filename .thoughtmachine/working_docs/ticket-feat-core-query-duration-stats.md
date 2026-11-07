# Ticket: feat/core-query-duration-stats

**Filed:** 2026-10-29, from operator request ("Memory & Stats Wave").
**Verified at:** HEAD `84bfd019` — every line reference below was read at that SHA.
**Severity:** Medium — pure addition, no behaviour change to existing paths.
**Status:** filed, not dispatched.
**Naming note:** filed as `ticket-*.md` because that is the only pattern
`.thoughtmachine/working_docs/` whitelists (`.gitignore:120-124`). The briefing's
proposed `feat/core-query-duration-stats.md` would have been gitignored and lost.

## STATUS OVERRIDE — read this before dispatching

This is **C6** in `knowledge/project/core.md`, which currently records it as
**PARKED / NOT URGENT** with the operator's own earlier ruling attached:

> *"We are not in urgent need of it, but we would like to get this feature at
> some point."*

The operator has now promoted it to an **immediate** feature. That is a
deliberate reversal, not a mis-filing, and `core.md` should be updated to say so
when this is dispatched — otherwise the next agent reads "parked" from the KB and
re-parks a ticket the operator is actively waiting on. Recording the override
here rather than silently promoting it is the whole point.

## Relationship to the ticket that already exists

**`ticket-feat-dispatch-report-duration.md` (Gen 44, 2026-10-16) already covers
the operator's underlying need** — "how long does a job take an agent?" — and
should *not* be re-filed or duplicated. It is scoped to a different layer:

| | Existing ticket (Gen 44) | This ticket |
|---|---|---|
| Surface | Worker dispatch **report header** (`duration_wall: 205 s`) | **Event stream** → live UI display |
| Audience | Main reading a report | Operator watching a run |
| Aggregation | per-dispatch report schema | per-turn + per-session events |
| Blocked on | dispatch mechanism | nothing |

The two are complementary: the report header answers "how long did that wave
take" after the fact, this answers "how long did this run take" while it is
running and on the summary line. Whoever dispatches either should say which is
authoritative for the operator's "overnight run" question — this ticket's
reading is that **both** are wanted and this one is a prerequisite (the report
header can be stamped from the same measurement).

## What already exists (do not rebuild it)

Duration is **already measured**. It is discarded before it reaches any event.

| What | Where | Feeds |
|---|---|---|
| per-turn wall duration | `agent/core/agent.py:1624, 1729, 1765, 1784` — `turn_duration = time.time() - turn_start_time` | `logger.log_turn_complete(..., {'duration_ms': …})` **only** |
| per-LLM-call latency | `agent/core/agent.py:1454` — `llm_duration_ms` | `logger.log_latency('llm_call', …)` **only** |

So the numbers land in the **log file** and nowhere else. Neither is placed on
the yielded event dicts.

## What is actually missing

1. **`turn` event carries no duration.**
   `agent/core/agent.py:1646` builds it with
   `content, assistant_content, tool_calls, turn, context_length, usage`
   (plus optional `reasoning`). No `duration_ms`. Add it here, and at the other
   `turn`-shaped yields for parity.
2. **`session_stop` carries no duration.** Two producers, both bare:
   - `agent/controller/__init__.py:764` — `{'type': 'session_stop',
     'stop_reason': stop_reason or 'completed'}` (normal completion)
   - `agent/controller/__init__.py:418` — `{'type': 'session_stop',
     'stop_reason': 'paused'}` (idle pause)
3. **No session-level start timestamp exists to diff against.** The controller
   `_run` loop takes a query at `agent/controller/__init__.py:721`
   (`self._processing_query = True`) and emits `session_stop` in its `finally`
   at `:764`, with nothing captured in between. A `session/query start` stamp
   must be added at `:721` (per query, not per process).
4. **The presenter does not read it.** `_process_session_stop_event`
   (`agent/presenter/event_processor.py:246-251`) reads only `stop_reason` and
   emits `f'Session stopped: {stop_reason}'`. To show "Completed in 4m 12s" it
   must format a duration instead. There is no duration formatter anywhere in
   the presenter — this is new presentation code, not a wiring change.
5. **Worker sessions need the same treatment.** Worker lifecycle
   (`agent/workers/worker_thread.py`, class at ~`:598`) runs its own session
   loop; check that whatever is added to the controller path is added there too,
   or worker runs will report no duration and the operator's overnight runs are
   exactly the worker case.

## Design decisions (not one-liners)

- **D1. Which number does the operator want?** "How long did the dispatch take"
  is a **session/query** span, not a sum of turns. The turn durations already
  exist and are the wrong answer on their own: turns exclude queueing and
  between-turn gaps. Emit both, but the headline "Completed in 4m 12s" must be
  the session span.
- **D2. `time.time()` vs `monotonic()`.** Every existing measurement uses
  `time.time()` (wall clock). There is **no** `monotonic`/`perf_counter` anywhere
  under `agent/` (verified). Overnight runs are precisely the case where a
  wall-clock delta is wrong: NTP steps and suspend/resume make `time.time()`
  deltas jump or go negative. Use `time.monotonic()` for the duration being
  *displayed*, and keep `time.time()` only if an absolute timestamp is also
  wanted. Capture both if in doubt — they answer different questions
  ("4m 12s elapsed" vs "finished at 03:14").
- **D3. Field name and units.** Existing convention in the log path is
  `duration_ms` (float ms). Keep `duration_ms` on the event too rather than
  inventing `duration_wall` here — the report-header ticket owns that name.
- **D4. Paused/interrupted/errored runs.** `session_stop` fires for pause
  (`:418`), completion (`:764`), and stop reasons including error. A duration on
  an errored run is still true and still useful; do not suppress it.
- **D5. Must not break the stop path.** `session_stop` is the terminal event the
  presenter uses to return the session to READY
  (`agent/presenter/event_processor.py:248`). Adding a field is safe; adding a
  *required* field or making the handler raise is not. `agent/controller/__init__.py:542-547`
  wraps event callbacks in try/except, so a raising handler is logged rather than
  fatal — which means a bug here fails **silently**. Keep it a plain field read
  with a fallback.

## Acceptance

- `turn` and `session_stop` event dicts each carry `duration_ms`.
- A completed run shows "Completed in 4m 12s" (unit-appropriate above 60 min).
- An overnight run's reported duration matches wall-clock to within a second,
  and does not go negative across a clock step.
- Worker sessions report duration on the same terms as agent sessions.
- Zero change to any existing consumer of these events (all new fields
  optional).

## MUST NOT TOUCH

Standard list. In particular: do not remove the existing
`logger.log_turn_complete` / `log_latency` calls — the log file is currently the
*only* place these numbers exist, and deleting them to "avoid duplication" before
the event path is proven would lose the data entirely.
