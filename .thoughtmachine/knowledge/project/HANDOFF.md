# HANDOFF — T0 bootstrap

**T0 state:** `dev` at `387bdbc4d2c81fd7eb42e2838eeb8a5464e38fc2`.
The **Control Plane phase is CLOSED**. **Phase 10 (EM-Solver / Resources) is OPEN.**

## Read in this order

1. `.thoughtmachine/knowledge/project/PRIORITIES.md` — the live work queue.
2. `docs/CONTROL_PLANE.md` — the pause/stop/timeout contract. Doctrine, not history. Note it lives in `docs/`, not in the knowledge base.
3. `.thoughtmachine/knowledge/project/vision_ledger.md` — the five standing commitments.
4. `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md` — the environment you are working in.

## First task

Start **C4 (Agent Sysprompt Addendum)**, which is open in `PRIORITIES.md`. **C25 (Worker State Reattachment)** is also open there but carries a name and no scope yet — its first task is to write the scope. Pick by appetite; neither blocks the other.

## What Gen 61 landed (the Control Plane)

- **C26 — symmetric resume notice.** `3f839766`. Pause had a notice; resume did not, so a resumed agent went on obeying a stale pause instruction. `Agent.leave_reading_mode()` is the only `clear_restriction()` caller outside `state.py`, so every resume path funnels through it: the flag is armed there and the notice is materialised on the turn thread and written **straight to history** rather than to `_pending_warnings` — every flush site for that buffer runs *after* the LLM call, so a buffered notice would miss the first post-resume call, which was the entire bug.
- **C27 — resume left the banner grey.** `e5260677`. `Controller.resume()` released the pause flags but never left `ExecutionState.PAUSED`/`PAUSING`, so no `execution_state_change` fired, no `state_changed` reached the UI, and the banner stayed grey until F5. Resume now settles to `RUNNING` (turn in flight) or `READY`.
- **C28 — one tool call, two bubbles.** `dc7b0899` (backend) + `d8d65dd6` (frontend). One stored call yields two rows — the assistant `tool_calls` array and the `{"role": "tool", "tool_call_id": …}` result — and the chat had no pairing step. Both emit paths now carry `tool_call_id` and the UI folds on it.
- **C29 — worker pause never reached container exec.** `9082e2cd`. `bind_pause_event` was bound only for the main agent; it is now bound at the worker turn boundary and polled in both container managers.

**Merge state at T0:** C29 (`9082e2cd`) is in `dev`. C26, C27 and C28 are **not** — they sit on `fix/gen61-control-plane-ui-cases` and await merge. Do not look for that code in `dev` and conclude it was never written.

## Open questions carried forward

1. **`git_read diff` returns silently empty** when `file_path` is a list. Pass a single path, or run it once per file. Unfixed.
2. **Harness `TMPDIR`.** Test runs need `TMPDIR=/tmp`; `/var/tmp` is read-only in this environment and the failure presents as a misleading `ENOENT`. Unfixed.

## Do not re-litigate

- **Pause = soft wind-down:** the turn finishes and the agent writes exactly one `Respond`. **Stop = hard abort:** the turn is discarded, the work parks, context is saved, it is resumable. These are different mechanisms, not two settings of one knob.
- **The 4 Lego Pieces are law:** Persistent State, Interrupt Signal, Tool Return Contract, Loop Gate. Measure every control-plane change against them instead of arguing around them.
